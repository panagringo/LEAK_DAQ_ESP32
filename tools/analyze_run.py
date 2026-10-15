"""
analyze_run.py - find the steady plateaus in a leak-rig log and average them.

Standalone: reads a CSV from logs/, never touches the ESP32 code or the logger.

    python tools/analyze_run.py                      newest run in logs/
    python tools/analyze_run.py logs/run_....csv     a specific run
    python tools/analyze_run.py --trim 3 --min 8     trim 3 s more off each plateau edge, keep plateaus >= 8 s

How it finds the settling periods (no need for the area labels to be right):
  1. box = average of B and C (the box sensors).
  2. At every row, compare the mean of the WINDOW seconds before with the WINDOW seconds
     after. A big difference in box (or in pitot A) means the slider/system is changing.
  3. Rows that are not changing, grouped together, are plateaus. TRIM seconds are cut off
     both ends of each plateau, and plateaus shorter than MIN seconds are dropped.

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

PA_PER_UNIT = {"inH2O": 248.64, "mmH2O": 9.789, "Pa": 1.0}

# step thresholds in Pa: box noise is ~2 Pa, pitot noise ~40 Pa (row to row)
BOX_STEP_PA = 6.0
PITOT_STEP_PA = 80.0
OFF_PA = 5.0          # box and pitot both within this of 0 -> system OFF


def newest_log(project):
    logs = [p for p in glob.glob(os.path.join(project, "logs", "run_*.csv"))
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
        if h.startswith(prefix):
            return h
    return None


def load(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("Empty log: %s" % path)
    header = list(rows[0].keys())
    unit_col = find_col(header, "units=")          # old logs: a separate units=... column
    if unit_col:
        units = unit_col.split("=", 1)[1]
    else:                                          # new logs: A_pitot_room_inH2O etc.
        a = find_col(header, "A_pitot") or ""
        units = next((u for u in PA_PER_UNIT if a.endswith("_" + u)), "inH2O")
    return rows, header, units


def decimals(values):
    # most decimal places the ESP32 printed in a column
    return max((len(v) - v.index(".") - 1 for v in values if v and "." in v), default=0)


def data_cols(header):
    # every numeric column worth averaging, in log order
    return [h for h in header if h and h not in ("time", "t_s", "area", "flags") and not h.startswith("units=")]


def detect(rows, header, units, window_s, trim_s, min_s):
    to_pa = PA_PER_UNIT.get(units, 1.0)
    cA, cB, cC = find_col(header, "A_pitot"), find_col(header, "B_"), find_col(header, "C_")
    if not cA:
        cA = find_col(header, "A_")
    t = [num(r["t_s"]) for r in rows]
    A = [num(r[cA]) if cA else None for r in rows]
    B = [num(r[cB]) if cB else None for r in rows]
    C = [num(r[cC]) if cC else None for r in rows]
    box = [mean([b, c]) for b, c in zip(B, C)]
    dt = st.median([b - a for a, b in zip(t, t[1:])]) if len(t) > 1 else 0.5
    W = max(2, round(window_s / dt))
    n = len(rows)

    # 1-2. moving rows: big change between the window before and the window after
    moving = [False] * n
    for i in range(n):
        for sig, thr in ((box, BOX_STEP_PA), (A, PITOT_STEP_PA)):
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

    results = []
    for k, (lo, hi) in enumerate(plateaus, 1):
        seg = rows[lo:hi]
        labels = sorted({r.get("area", "") for r in seg if r.get("area")})
        a, b, c, bx = A[lo:hi], B[lo:hi], C[lo:hi], box[lo:hi]
        off = all(abs(x) * to_pa < OFF_PA for x in (mean(a) or 0, mean(bx) or 0))
        cM = find_col(header, "mano")
        res = {
            "plateau": k,
            "state": "OFF" if off else "ON",
            "area_labels": " ".join(labels),
            "time_start": seg[0].get("time", ""), "time_end": seg[-1].get("time", ""),
            "t_start_s": t[lo], "t_end_s": t[hi - 1],
            "seconds_used": round(t[hi - 1] - t[lo] + dt, 1), "rows": hi - lo,
            "A_mean": mean(a), "A_sd": sd(a),
            "B_mean": mean(b), "B_sd": sd(b),
            "C_mean": mean(c), "C_sd": sd(c),
            "box_mean": mean(bx),
            "B_minus_C": (mean(b) - mean(c)) if mean(b) is not None and mean(c) is not None else None,
            "box_mean_Pa": mean(bx) * to_pa if mean(bx) is not None else None,
            "mano": mean(num(r.get(cM)) for r in seg) if cM else None,
        }
        # every other logged column too (A_avg, P_room, temperatures, Vs ...)
        for h in data_cols(header):
            if h not in (cA, cB, cC, cM):
                res[h] = mean(num(r.get(h)) for r in seg)
        results.append(res)
    skipped = [(a, b, bx * to_pa if bx is not None else None) for a, b, bx in skipped]
    return results, plateaus, (t, A, B, C, moving), units, skipped


def fmt(v, d=4):
    return "" if v is None else ("%.*f" % (d, v) if isinstance(v, float) else str(v))


def has_mano(results):
    return any(r["mano"] is not None for r in results)


PRESSURE_KEYS = ("A_mean", "A_sd", "B_mean", "B_sd", "C_mean", "C_sd", "box_mean", "B_minus_C", "mano")
TEXT_KEYS = ("plateau", "state", "area_labels", "time_start", "time_end", "rows")


def write_csv(results, rows, units, path):
    cols = [c for c in results[0].keys() if (c != "mano" or has_mano(results))
            and (c not in ("time_start", "time_end") or results[0][c])]
    # a plateau mean is good to one more decimal than one logged row; t columns are logged to the ms
    pdec = decimals(v for r in rows for k, v in r.items() if k and k.startswith(("A_", "B_", "C_"))) + 1
    dec = {c: decimals(r.get(c) for r in rows) + 1 for c in cols if c not in TEXT_KEYS}
    dec.update({c: pdec for c in PRESSURE_KEYS}, t_start_s=3, t_end_s=3, seconds_used=1, box_mean_Pa=2)
    dec["mano"] = decimals(r.get(find_col(rows[0].keys(), "mano")) for r in rows) + 1   # read by eye: coarser
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["%s_%s" % (c, units) if c in PRESSURE_KEYS else c for c in cols])
        for r in results:
            w.writerow([fmt(r[c], dec.get(c, 4)) for c in cols])


def print_table(results, rows, units):
    mano = has_mano(results)   # the mano column only appears when manometer readings were typed
    d = decimals(v for r in rows for k, v in r.items() if k and k.startswith(("A_", "B_", "C_"))) + 1
    dm = decimals(r.get(find_col(rows[0].keys(), "mano")) for r in rows) + 1
    w = d + 5
    print("\n %-3s %-4s %-6s %8s %8s %5s  %*s %*s  %*s %*s  %*s %*s  %8s%s" % (
        "#", "", "labels", "t_from_s", "t_to_s", "used",
        w, "A_pitot", w, "sd", w, "B_box1", w, "sd", w, "C_box2", w, "sd",
        "box_Pa", " %*s" % (w, "mano") if mano else ""))
    print("     (A, B, C, sd%s in %s)" % (", mano" if mano else "", units))
    for r in results:
        print(" %-3d %-4s %-6s %8.3f %8.3f %4.0fs  %*s %*s  %*s %*s  %*s %*s  %8s%s" % (
            r["plateau"], r["state"], r["area_labels"] or "-", r["t_start_s"], r["t_end_s"], r["seconds_used"],
            w, fmt(r["A_mean"], d), w, fmt(r["A_sd"], d), w, fmt(r["B_mean"], d), w, fmt(r["B_sd"], d),
            w, fmt(r["C_mean"], d), w, fmt(r["C_sd"], d), fmt(r["box_mean_Pa"], 2),
            " %*s" % (w, fmt(r["mano"], dm)) if mano else ""))
        extras = ["%s %s" % (c, fmt(r[c], 2 if c.endswith(("_C", "_Pa")) else 4))
                  for c in r if c.endswith(("_C", "_V", "_Pa")) and c not in ("box_mean_Pa", "B_minus_C")
                  and r[c] is not None]
        if r.get("time_start"):
            extras.insert(0, "%s -> %s" % (r["time_start"], r["time_end"][11:]))
        if extras:
            print("        " + "   ".join(extras))


def plot(series, plateaus, results, units, path, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    t, A, B, C, _ = series
    fig, ax = plt.subplots(figsize=(12, 5))
    ax2 = ax.twinx()
    ax2.plot(t, [x if x is not None else float("nan") for x in A], color="0.7", lw=1, label="A pitot (right axis)")
    ax.plot(t, [x if x is not None else float("nan") for x in B], color="tab:blue", lw=1.5, label="B box1")
    ax.plot(t, [x if x is not None else float("nan") for x in C], color="tab:orange", lw=1.5, label="C box2")
    for (lo, hi), r in zip(plateaus, results):
        ax.axvspan(t[lo], t[hi - 1], color="tab:green" if r["state"] == "ON" else "0.85", alpha=0.18)
        ax.text((t[lo] + t[hi - 1]) / 2, 1.01, "%d%s" % (r["plateau"], " " + r["area_labels"] if r["area_labels"] else ""),
                transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=9)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("box - room (%s)" % units)
    ax2.set_ylabel("pitot - room (%s)" % units)
    ax.grid(alpha=0.3)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=9)
    ax.set_title(title + "   (shaded = rows kept)", fontsize=10, pad=18)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return True


def main():
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="Average the steady plateaus of a leak-rig log, skipping settling periods.")
    ap.add_argument("csv", nargs="?", help="log file (default: newest in logs/)")
    ap.add_argument("--trim", type=float, default=1.0, help="extra seconds cut off each end of a plateau (default 1)")
    ap.add_argument("--min", type=float, default=3.0, help="shortest plateau kept after trimming, seconds (default 3)")
    ap.add_argument("--window", type=float, default=3.0, help="seconds compared before/after each row (default 3)")
    args = ap.parse_args()

    path = args.csv or newest_log(project)
    rows, header, units = load(path)
    results, plateaus, series, units, skipped = detect(rows, header, units, args.window, args.trim, args.min)
    print("Run: %s  (%d rows, %.0f s, units %s)  trim %.1f s, min %.1f s"
          % (os.path.basename(path), len(rows), series[0][-1], units, args.trim, args.min))
    if not results:
        print("No steady plateaus found. Try --min smaller or --window smaller.")
        return
    print_table(results, rows, units)
    for a, b, bx in skipped:
        print("   skipped %.1f-%.1f s (box %s Pa): too short once trimmed - hold each slider position longer"
              % (a, b, fmt(bx, 1)))

    base = path[:-4]
    write_csv(results, rows, units, base + "_trimmed.csv")
    print("\nSaved %s" % (base + "_trimmed.csv"))
    if plot(series, plateaus, results, units, base + "_trimmed.png", os.path.basename(path)):
        print("Saved %s" % (base + "_trimmed.png"))


if __name__ == "__main__":
    main()
