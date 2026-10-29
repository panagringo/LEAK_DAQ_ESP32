"""
analyze_run.py - find the steady plateaus in a leak-rig log and average them.

Standalone: reads a CSV from logs/, never touches the ESP32 code or the logger.
Works on every DAQ revision; it tells them apart by the column names in the log:

    REV-A  A_mpx_box_room ...   A = MPX box - room,  B, C = BME box - room
    REV-B  A_pitot_room ...     A = MPX pitot - room, B, C = BME box - room
    REV-C  B_box_room ...       A = MPX pitot - box (q), B = MPX box - room, C = MPX pitot - room,
                                D = BME box - room (cross-check)

    python tools/analyze_run.py                      newest run in logs/ (any revision folder)
    python tools/analyze_run.py logs/REV-B/run_....csv     a specific run
    python tools/analyze_run.py --trim 3 --min 8     trim 3 s more off each plateau edge, keep plateaus >= 8 s
    python tools/analyze_run.py --k 0.94             pitot coefficient once it has been calibrated (default 1)

How it finds the settling periods (no need for the area labels to be right):
  1. box = the box - room signal (REV-C: B; REV-B: average of B and C; REV-A: A).
  2. At every row, compare the mean of the WINDOW seconds before with the WINDOW seconds
     after. A big difference in box (or in the pitot channel A) means the slider/system is changing.
  3. Rows that are not changing, grouped together, are plateaus. TRIM seconds are cut off
     both ends of each plateau, and plateaus shorter than MIN seconds are dropped.

For each plateau with the system on it also works out (REV-B and REV-C):
    q       = pitot - box                          (REV-C: A directly; REV-B: A - box)
    Q_cfm   = k * A_pipe * sqrt(2 q / rho)         inflow, rho from room P, T (and RH if logged)
    CdA_mm2 = k * A_pipe * sqrt(q / box)           effective leak area (instructor's formula)
k is the pitot coefficient (1 until it is calibrated on the box), A_pipe = 2669 mm2 (58.3 mm bore).
REV-C adds closure_Pa = C - (A + B), which should be near 0: it checks the three MPX sensors agree.

Writes, next to the log:
  <run>_trimmed.csv   one row per plateau: means and standard deviations (opens in Excel)
  <run>_trimmed.png   plot of the run with the kept rows shaded (if matplotlib is installed)
"""

import argparse
import csv
import glob
import math
import os
import statistics as st
import sys

PA_PER_UNIT = {"inH2O": 248.64, "mmH2O": 9.789, "Pa": 1.0, "kPa": 1000.0}
PRESSURE_PREFIX = ("A_", "B_", "C_", "D_")

# step thresholds in Pa: box noise is ~2 Pa, pitot noise ~40 Pa (row to row)
BOX_STEP_PA = 6.0
PITOT_STEP_PA = 80.0
OFF_PA = 5.0          # box and pitot both within this of 0 -> system OFF
MIN_BOX_PA = 1.0      # below this the leak-area formula is meaningless
A_PIPE_MM2 = 2669.0   # 58.3 mm bore of the pitot flange tube
R_AIR = 287.05
M3S_TO_CFM = 2118.88

# what each channel is, per revision.  box: channels averaged into the box - room signal;
# flow: channel watched for steps besides box; q: how velocity pressure is formed (None = not measured)
REVS = {
    "REV-A": {"cols": {"A": "A_mpx", "B": "B_", "C": "C_"}, "box": ["A"], "flow": None, "q": None,
              "names": {"A": "A  MPX box − room", "B": "B  BME box 1 − room", "C": "C  BME box 2 − room"},
              "short": {"A": "A MPX box", "B": "B BME box 1", "C": "C BME box 2"},
              "left": ["A", "B", "C"], "right": []},
    "REV-B": {"cols": {"A": "A_pitot", "B": "B_", "C": "C_"}, "box": ["B", "C"], "flow": "A", "q": "A-box",
              "names": {"A": "A  pitot − room", "B": "B  BME box 1 − room", "C": "C  BME box 2 − room"},
              "short": {"A": "A pitot", "B": "B BME box 1", "C": "C BME box 2"},
              "left": ["B", "C"], "right": ["A"]},
    "REV-C": {"cols": {"A": "A_q", "B": "B_box", "C": "C_pitot", "D": "D_"}, "box": ["B"], "flow": "A", "q": "A",
              "names": {"A": "A  q = pitot − box", "B": "B  box − room", "C": "C  pitot − room",
                        "D": "D  BME box − room"},
              "short": {"A": "A q", "B": "B box", "C": "C pitot", "D": "D BME box"},
              "left": ["B", "D"], "right": ["A", "C"]},
}
COLORS = {"A": "0.55", "B": "tab:blue", "C": "tab:purple", "D": "tab:orange"}


def detect_rev(header):
    if any(h.startswith("B_box_room") for h in header):
        return "REV-C"
    if any(h.startswith("A_pitot") for h in header):
        return "REV-B"
    if any(h.startswith("A_mpx_box_room") for h in header):
        return "REV-A"
    return "REV-B"


def newest_log(project):
    logs = [p for p in glob.glob(os.path.join(project, "logs", "**", "run_*.csv"), recursive=True)
            if not p.endswith(("_summary.csv", "_trimmed.csv"))]
    if not logs:
        sys.exit("No logs found in %s" % os.path.join(project, "logs"))
    return max(logs, key=os.path.getmtime)


def num(x):
    try:
        v = float(x)
        return v if not math.isnan(v) else None
    except (TypeError, ValueError):
        return None


def mean(v):
    v = [x for x in v if x is not None]
    return st.mean(v) if v else None


def sd(v):
    v = [x for x in v if x is not None]
    return st.stdev(v) if len(v) > 1 else None


def find_col(header, prefix):
    for h in header:
        if h and h.startswith(prefix):
            return h
    return None


def load(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("Empty log: %s" % path)
    bad = [i + 2 for i, r in enumerate(rows) if num(r.get("t_s")) is None]   # +2: header + 1-based
    if bad:
        print("Warning: skipping %d corrupted row(s) (unreadable t_s) at line(s) %s"
              % (len(bad), ", ".join(map(str, bad))))
        rows = [r for r in rows if num(r.get("t_s")) is not None]
    header = list(rows[0].keys())
    unit_col = find_col(header, "units=")          # old logs: a separate units=... column
    if unit_col:
        units = unit_col.split("=", 1)[1]
    else:                                          # newer logs: A_pitot_room_inH2O etc.
        a = find_col(header, "A_") or ""
        units = next((u for u in PA_PER_UNIT if a.endswith("_" + u)), "inH2O")
    return rows, header, to_kpa(rows, header, units), units


def to_kpa(rows, header, units):
    # everything downstream (CSV, table, plot) works in kPa, whatever units were logged.
    # 0.0001 kPa = 0.1 Pa matches the sensor resolution; the manometer is read by eye to ~1 Pa.
    k = PA_PER_UNIT.get(units, 1.0) / 1000.0
    for h in header:
        if h and h.startswith(PRESSURE_PREFIX + ("mano",)):
            f = "%.3f" if h.startswith("mano") else "%.4f"
            for r in rows:
                v = num(r.get(h))
                r[h] = "" if v is None else f % (v * k)
    return "kPa"


def decimals(values):
    # most decimal places the ESP32 printed in a column
    return max((len(v) - v.index(".") - 1 for v in values if v and "." in v), default=0)


def pdecimals(rows):
    return decimals(v for r in rows for k, v in r.items() if k and k.startswith(PRESSURE_PREFIX)) + 1


def data_cols(header):
    # every numeric column worth averaging, in log order
    return [h for h in header if h and h not in ("time", "t_s", "area", "flags") and not h.startswith("units=")]


def air_density(p_pa, t_c, rh_pct=None):
    """Moist-air density (kg/m3) from absolute pressure, temperature and (optional) humidity."""
    if p_pa is None or t_c is None:
        return 1.2
    tk = t_c + 273.15
    pv = 0.0
    if rh_pct is not None:
        pv = rh_pct / 100.0 * 610.94 * math.exp(17.625 * t_c / (t_c + 243.04))   # Magnus
    return (p_pa - pv) / (R_AIR * tk) + pv / (461.5 * tk)


def detect(rows, header, units, rev, window_s, trim_s, min_s, k, a_pipe_mm2):
    spec = REVS[rev]
    to_pa = PA_PER_UNIT.get(units, 1.0)
    cols = {ch: find_col(header, pre) for ch, pre in spec["cols"].items()}
    t = [num(r["t_s"]) for r in rows]
    S = {ch: [num(r[c]) if c else None for r in rows] for ch, c in cols.items()}
    box = [mean([S[ch][i] for ch in spec["box"]]) for i in range(len(rows))]
    flow = S[spec["flow"]] if spec["flow"] else [None] * len(rows)
    dt = st.median([b - a for a, b in zip(t, t[1:])]) if len(t) > 1 else 0.5
    W = max(2, round(window_s / dt))
    n = len(rows)

    # 1-2. moving rows: big change between the window before and the window after
    moving = [False] * n
    for i in range(n):
        for sig, thr in ((box, BOX_STEP_PA), (flow, PITOT_STEP_PA)):
            before, after = mean(sig[max(0, i - W):i]), mean(sig[i:i + W])
            if before is not None and after is not None and abs(after - before) * to_pa > thr:
                moving[i] = True

    # 3. group the steady rows, trim the edges, drop the short ones
    trim_rows = round(trim_s / dt)
    plateaus, skipped, i = [], [], 0
    while i < n:
        if moving[i]:
            i += 1
            continue
        j = i
        while j < n and not moving[j]:
            j += 1
        lo, hi = i + trim_rows if i > 0 else i, j - trim_rows if j < n else j   # no trim at the run's ends
        if hi > lo and (t[hi - 1] - t[lo]) + dt >= min_s:
            plateaus.append((lo, hi))
        else:
            skipped.append((t[i], t[j - 1], mean(box[i:j])))
        i = j

    cM = find_col(header, "mano")
    results = []
    for p, (lo, hi) in enumerate(plateaus, 1):
        seg = rows[lo:hi]
        labels = sorted({r.get("area", "") for r in seg if r.get("area")})
        m = {ch: mean(v[lo:hi]) for ch, v in S.items()}
        bx = mean(box[lo:hi])
        off = all(abs(x or 0) * to_pa < OFF_PA for x in (bx, mean(flow[lo:hi])))
        res = {
            "plateau": p,
            "state": "OFF" if off else "ON",
            "area_labels": " ".join(labels),
            "time_start": seg[0].get("time", ""), "time_end": seg[-1].get("time", ""),
            "t_start_s": t[lo], "t_end_s": t[hi - 1],
            "seconds_used": round(t[hi - 1] - t[lo] + dt, 1), "rows": hi - lo,
        }
        for ch in S:
            res[ch + "_mean"], res[ch + "_sd"] = m[ch], sd(S[ch][lo:hi])
        res["box_mean"] = bx
        res["box_mean_Pa"] = bx * to_pa if bx is not None else None
        if rev == "REV-B":
            res["B_minus_C"] = (m["B"] - m["C"]) if m["B"] is not None and m["C"] is not None else None
        # velocity pressure, closure, flow and leak area
        q = None
        if spec["q"] == "A" and m["A"] is not None:
            q = m["A"] * to_pa
        elif spec["q"] == "A-box" and m["A"] is not None and bx is not None:
            q = (m["A"] - bx) * to_pa
        res["q_Pa"] = q
        if rev == "REV-C":
            res["closure_Pa"] = ((m["C"] - m["A"] - m["B"]) * to_pa
                                 if None not in (m["A"], m["B"], m["C"]) else None)
        res["Q_cfm"] = res["CdA_mm2"] = None
        if spec["q"] and not off and q is not None and q > 0:
            rho = air_density(mean(num(r.get("P_room_Pa")) for r in seg),
                              mean(num(r.get("T_room_C")) for r in seg),
                              mean(num(r.get("RH_room_pct")) for r in seg))
            res["Q_cfm"] = k * a_pipe_mm2 * 1e-6 * math.sqrt(2 * q / rho) * M3S_TO_CFM
            if res["box_mean_Pa"] is not None and res["box_mean_Pa"] > MIN_BOX_PA:
                res["CdA_mm2"] = k * a_pipe_mm2 * math.sqrt(q / res["box_mean_Pa"])
        res["mano"] = mean(num(r.get(cM)) for r in seg) if cM else None
        # every other logged column too (A_avg, P_room, temperatures, Vs ...)
        for h in data_cols(header):
            if h not in cols.values() and h != cM:
                res[h] = mean(num(r.get(h)) for r in seg)
        results.append(res)
    skipped = [(a, b, x * to_pa if x is not None else None) for a, b, x in skipped]
    return results, plateaus, (t, S, moving), skipped


def fmt(v, d=4):
    return "" if v is None else ("%.*f" % (d, v) if isinstance(v, float) else str(v))


def has_mano(results):
    return any(r["mano"] is not None for r in results)


TEXT_KEYS = ("plateau", "state", "area_labels", "time_start", "time_end", "rows")
FIXED_DEC = {"t_start_s": 3, "t_end_s": 3, "seconds_used": 1, "box_mean_Pa": 2, "q_Pa": 1,
             "closure_Pa": 1, "Q_cfm": 1, "CdA_mm2": 0}


def is_pressure_key(c):
    return c[:2] in PRESSURE_PREFIX and c.endswith(("_mean", "_sd")) or c in ("box_mean", "B_minus_C", "mano")


def write_csv(results, rows, units, path):
    cols = [c for c in results[0].keys() if (c != "mano" or has_mano(results))
            and (c not in ("time_start", "time_end") or results[0][c])
            and any(r.get(c) is not None for r in results)]
    # a plateau mean is good to one more decimal than one logged row; t columns are logged to the ms
    pdec = pdecimals(rows)
    dec = {c: decimals(r.get(c) for r in rows) + 1 for c in cols if c not in TEXT_KEYS}
    dec.update({c: pdec for c in cols if is_pressure_key(c)}, **FIXED_DEC)
    dec["mano"] = decimals(r.get(find_col(rows[0].keys(), "mano")) for r in rows) + 1   # read by eye: coarser
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["%s_%s" % (c, units) if is_pressure_key(c) or c.startswith("A_avg") else c for c in cols])
        for r in results:
            w.writerow([fmt(r.get(c), dec.get(c, 4)) for c in cols])


def table_spec(rev, d, short=False):
    """(heading, function(result) -> text) for the printed table and the plot's table."""
    pm = lambda m, s: "—" if m is None else "%s ± %s" % (fmt(m, d), fmt(s, d) if s is not None else "—")  # noqa: E731
    val = lambda key, dd: (lambda r: fmt(r.get(key), dd) or "—")  # noqa: E731
    chans = [ch for ch in ("A", "B", "C", "D") if ch in REVS[rev]["cols"]]
    names = REVS[rev]["short" if short else "names"]
    spec = [(names[ch], (lambda ch: lambda r: pm(r[ch + "_mean"], r[ch + "_sd"]))(ch)) for ch in chans]
    spec.append(("Box (Pa)", val("box_mean_Pa", 1)))
    if rev == "REV-C":
        spec.append(("C−(A+B) (Pa)", val("closure_Pa", 1)))
    if REVS[rev]["q"]:
        spec += [("q (Pa)", val("q_Pa", 1)), ("Q (CFM)", val("Q_cfm", 1)), ("CdA (mm²)", val("CdA_mm2", 0))]
    return spec


def print_table(results, rows, units, rev):
    mano = has_mano(results)   # the mano column only appears when manometer readings were typed
    d = pdecimals(rows)
    dm = decimals(r.get(find_col(rows[0].keys(), "mano")) for r in rows) + 1
    spec = table_spec(rev, d)
    heads = [h.split("  ", 1)[0] for h, _ in spec]           # short headings: "A", "B", "Box (Pa)" ...
    cells = [[f(r) for _, f in spec] for r in results]
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, h in enumerate(heads)]
    print("\n %-3s %-4s %-6s %8s %8s %5s  %s%s" % ("#", "", "labels", "t_from_s", "t_to_s", "used",
          "  ".join(h.rjust(w) for h, w in zip(heads, widths)), "  %8s" % "mano" if mano else ""))
    print("     (%s: mean ± sd in %s%s)" % (", ".join(h for h, _ in spec if h[:2] in ("A ", "B ", "C ", "D ")),
                                                units, "; mano in %s" % units if mano else ""))
    for r, c in zip(results, cells):
        print(" %-3d %-4s %-6s %8.3f %8.3f %4.0fs  %s%s" % (
            r["plateau"], r["state"], r["area_labels"] or "-", r["t_start_s"], r["t_end_s"], r["seconds_used"],
            "  ".join(x.rjust(w) for x, w in zip(c, widths)), "  %8s" % fmt(r["mano"], dm) if mano else ""))
        extras = ["%s %s" % (k, fmt(r[k], 2 if k.endswith(("_C", "_Pa", "_pct")) else 4))
                  for k in r if k.endswith(("_C", "_V", "_Pa", "_pct")) and k not in
                  ("box_mean_Pa", "q_Pa", "closure_Pa", "B_minus_C") and r[k] is not None]
        if r.get("time_start"):
            extras.insert(0, "%s -> %s" % (r["time_start"], r["time_end"][11:]))
        if extras:
            print("        " + "   ".join(extras))


ON_COLOR, OFF_COLOR = "#2ca02c", "#9aa0a6"   # shading for kept rows: system on / system off (zero)


def section_name(r):
    if r["state"] == "OFF":
        return "Zero (system off)"
    labels = r["area_labels"].split()
    return "Area " + " / ".join(labels) if labels else "Unlabeled"


def tint(color, alpha):
    # a solid colour that looks like `color` at `alpha` over white (table cells can't do alpha)
    from matplotlib.colors import to_rgb
    return tuple(1 - alpha * (1 - c) for c in to_rgb(color))


def plot(series, plateaus, results, rows, units, log_units, rev, path, title, trim_s, k):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    t, S, _ = series
    spec = REVS[rev]
    nan = lambda v: [x if x is not None else float("nan") for x in v]   # noqa: E731
    d = pdecimals(rows)
    tspec = [("#", lambda r: "%d" % r["plateau"]), ("Section", section_name),
             ("Time (s)", lambda r: "%.1f – %.1f" % (r["t_start_s"], r["t_end_s"])),
             ("Used", lambda r: "%.0f s" % r["seconds_used"])] + table_spec(rev, d, short=True)

    # figure: header text, graph, results table (table grows with the number of sections)
    table_h = 0.42 * (len(results) + 1)
    fig = plt.figure(figsize=(max(13, 1.45 * len(tspec)), 5.2 + table_h + 1.7))
    gs = fig.add_gridspec(2, 1, height_ratios=[5.2, table_h], hspace=0.32,
                          left=0.065, right=0.935, top=1 - 1.4 / fig.get_figheight(),
                          bottom=0.55 / fig.get_figheight())

    # header
    when = ""
    if rows[0].get("time"):
        when = "%s  to  %s" % (rows[0]["time"][:19], rows[-1]["time"][11:19])
    env = []
    for col, fmt_s in (("P_room_Pa", "room %.0f Pa"), ("T_room_C", "room %.1f °C"), ("RH_room_pct", "%.0f %% RH"),
                       ("T_box_C", "box %.1f °C"), ("Vs_V", "supply %.2f V")):
        m = mean(num(r.get(col)) for r in rows) if col in rows[0] else None
        if m is not None:
            env.append(fmt_s % m)
    fig.text(0.065, 1 - 0.32 / fig.get_figheight(), "Leak Rig Run  —  %s  ·  DAQ %s" % (title.replace(".csv", ""), rev),
             fontsize=15, fontweight="bold", color="#1f2933", va="top")
    fig.text(0.065, 1 - 0.68 / fig.get_figheight(),
             "   ·   ".join(x for x in (when, "%.0f s, %d rows" % (t[-1], len(rows)), "units: kPa (logged in %s)" % log_units,
                                            "   ".join(env)) if x),
             fontsize=9.5, color="#52606d", va="top")

    # graph: box-type signals on the left axis, pitot-type on the right
    ax = fig.add_subplot(gs[0])
    ax2 = ax.twinx() if spec["right"] else None
    for ch in spec["right"]:
        ax2.plot(t, nan(S[ch]), color=COLORS[ch], lw=1, alpha=0.8 if ch == "C" else 1,
                 label="%s  (right axis)" % spec["names"][ch])
    for ch in spec["left"]:
        if S.get(ch) and any(v is not None for v in S[ch]):
            ax.plot(t, nan(S[ch]), color=COLORS[ch], lw=1.0 if ch == "D" else 1.6, ls="--" if ch == "D" else "-",
                    label=spec["names"][ch])
    if ax2:
        ax.set_zorder(ax2.get_zorder() + 1)   # box traces on top of the pitot traces
        ax.patch.set_visible(False)
    for (lo, hi), r in zip(plateaus, results):
        on = r["state"] == "ON"
        ax.axvspan(t[lo], t[hi - 1], color=ON_COLOR if on else OFF_COLOR, alpha=0.16 if on else 0.18, lw=0)
        ax.text((t[lo] + t[hi - 1]) / 2, 1.015, "#%d\n%s" % (r["plateau"], section_name(r) if on else "Zero"),
                transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=8.5,
                fontweight="bold", color="#1e6b1e" if on else "#5f6368", linespacing=1.1)
    ax.set_xlim(t[0], t[-1])
    ax.set_xlabel("Time since start of run (s)", fontsize=10)
    ax.set_ylabel("Box − room  (%s)" % units, fontsize=10)
    if ax2:
        ax2.set_ylabel("Pitot channels  (%s)" % units, fontsize=10, color="0.4")
        ax2.tick_params(axis="y", colors="0.4")
        ax2.spines["top"].set_visible(False)
    ax.grid(alpha=0.3)
    ax.spines["top"].set_visible(False)
    from matplotlib.patches import Patch
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels() if ax2 else ([], [])
    extra = [Patch(color=ON_COLOR, alpha=0.3, label="Kept rows (steady, trimmed)"),
             Patch(color=OFF_COLOR, alpha=0.35, label="Kept rows, system off (zero check)")]
    ax.legend(h1 + h2 + extra, l1 + l2 + [p.get_label() for p in extra], loc="upper left", fontsize=8.5,
              framealpha=0.92, edgecolor="#d0d5dc")

    # results table
    tax = fig.add_subplot(gs[1])
    tax.axis("off")
    heads = [h if not (h[:1] in "ABCD" and h[1:2] == " ") else "%s (%s)" % (h, units) for h, _ in tspec]
    cells = [[f(r) for _, f in tspec] for r in results]
    widths = [0.035, 0.12, 0.10, 0.05] + [0.0] * (len(tspec) - 4)
    rest = (1 - sum(widths[:4])) / (len(tspec) - 4)
    widths = widths[:4] + [rest] * (len(tspec) - 4)
    tab = tax.table(cellText=cells, colLabels=heads, cellLoc="center", bbox=[0, 0, 1, 1], colWidths=widths)
    tab.auto_set_font_size(False)
    tab.set_fontsize(9)
    hl = {i for i, (h, _) in enumerate(tspec) if h.startswith(("Box", "CdA", "Q "))}
    for (i, j), cell in tab.get_celld().items():
        cell.set_edgecolor("#d0d5dc")
        cell.set_linewidth(0.6)
        if i == 0:
            cell.set_facecolor("#2b3a4a")
            cell.get_text().set_color("white")
            cell.get_text().set_fontweight("bold")
            cell.get_text().set_wrap(True)
            continue
        r = results[i - 1]
        on = r["state"] == "ON"
        cell.set_facecolor("#f5f7fa" if i % 2 == 0 else "white")
        if j == 0:
            cell.set_facecolor(tint(ON_COLOR if on else OFF_COLOR, 0.35))
            cell.get_text().set_fontweight("bold")
        if j == 1:
            cell.get_text().set_fontweight("bold")
            cell.get_text().set_ha("left")
            cell.PAD = 0.04
        if j in hl:
            cell.get_text().set_fontweight("bold")
    tax.set_title("Trimmed averages by section   (mean ± standard deviation of the kept rows only)",
                  fontsize=11, fontweight="bold", color="#1f2933", loc="left", pad=8)
    notes = ["Settling periods are detected automatically and excluded; each section is trimmed %.1f s at both ends." % trim_s,
             "Logged in %s (1 %s = %.2f Pa), converted to kPa." % (log_units, log_units, PA_PER_UNIT.get(log_units, 1.0))]
    if spec["q"]:
        notes.append("Q and CdA use k = %.3g%s and A_pipe = %.0f mm²." % (k, " (uncalibrated)" if k == 1 else "", A_PIPE_MM2))
    fig.text(0.065, 0.2 / fig.get_figheight(), "  ".join(notes), fontsize=8.5, color="#52606d", va="bottom")
    fig.savefig(path, dpi=130, facecolor="white")
    plt.close(fig)
    return True


def main():
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Average the steady plateaus of a leak-rig log, skipping settling periods.")
    ap.add_argument("csv", nargs="?", help="log file (default: newest in logs/)")
    ap.add_argument("--trim", type=float, default=1.0, help="extra seconds cut off each end of a plateau (default 1)")
    ap.add_argument("--min", type=float, default=3.0, help="shortest plateau kept after trimming, seconds (default 3)")
    ap.add_argument("--window", type=float, default=3.0, help="seconds compared before/after each row (default 3)")
    ap.add_argument("--k", type=float, default=1.0, help="pitot coefficient for Q and CdA (default 1, uncalibrated)")
    ap.add_argument("--pipe-mm2", type=float, default=A_PIPE_MM2, help="pitot tube bore area, mm2 (default 2669)")
    ap.add_argument("--open", action="store_true", help="open the plot when done")
    args = ap.parse_args()

    path = args.csv or newest_log(project)
    rows, header, units, log_units = load(path)
    rev = detect_rev(header)
    results, plateaus, series, skipped = detect(rows, header, units, rev, args.window, args.trim, args.min,
                                                args.k, args.pipe_mm2)
    print("Run: %s  (DAQ %s, %d rows, %.0f s, units %s, logged in %s)  trim %.1f s, min %.1f s"
          % (os.path.basename(path), rev, len(rows), series[0][-1], units, log_units, args.trim, args.min))
    if not results:
        print("No steady plateaus found. Try --min smaller or --window smaller.")
        return
    print_table(results, rows, units, rev)
    if REVS[rev]["q"]:
        print("\n   Q and CdA use k = %g%s, A_pipe = %.0f mm2." % (args.k, " (not calibrated yet)" if args.k == 1 else "",
                                                               args.pipe_mm2))
    for a, b, bx in skipped:
        print("   skipped %.1f-%.1f s (box %s Pa): too short once trimmed - hold each slider position longer"
              % (a, b, fmt(bx, 1)))

    base = path[:-4]
    write_csv(results, rows, units, base + "_trimmed.csv")
    print("\nSaved %s" % (base + "_trimmed.csv"))
    if plot(series, plateaus, results, rows, units, log_units, rev, base + "_trimmed.png", os.path.basename(path),
            args.trim, args.k):
        print("Saved %s" % (base + "_trimmed.png"))
        if args.open and hasattr(os, "startfile"):
            os.startfile(base + "_trimmed.png")


if __name__ == "__main__":
    main()
