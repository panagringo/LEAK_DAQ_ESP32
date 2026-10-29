"""
PlatformIO monitor filter: saves each run to logs/run_YYYY-MM-DD_HH-MM-SS.csv

Every saved row starts with a "time" column: the PC's clock (YYYY-MM-DD HH:MM:SS.mmm) when the
line arrived, since the ESP32 has no real-time clock. t_s is still the ESP32's own run time.

The ESP32 prints "# LOG START" when you type run / log and "# LOG STOP" when you
type stop. Everything between them that isn't a "#" comment line goes into the file.
When the run ends, a second file ..._summary.csv holds the average of every column
for each outlet area label (rows with a blank area are left out).
Enabled by "csvlog" in monitor_filters (platformio.ini).
"""

import csv
import os
from datetime import datetime

from platformio.public import DeviceMonitorFilterBase


class CsvLog(DeviceMonitorFilterBase):
    NAME = "csvlog"

    def __call__(self):
        self._buf = ""
        self._file = None
        self._path = None
        self._rows = []
        return self

    def _open(self):
        notes = self._close()
        log_dir = os.path.join(self.project_dir or os.getcwd(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        self._path = os.path.join(log_dir, datetime.now().strftime("run_%Y-%m-%d_%H-%M-%S.csv"))
        self._file = open(self._path, "w", encoding="utf-8", newline="")
        self._rows = []
        return notes + "# >>> saving to %s\n" % self._path

    def _close(self):
        if not self._file:
            return ""
        self._file.close()
        self._file = None
        notes = "# >>> saved %d rows to %s\n" % (max(len(self._rows) - 1, 0), self._path)
        try:
            notes += self._summarize()
        except Exception as e:  # never lose the run because the summary failed
            notes += "# >>> summary failed: %s\n" % e
        return notes

    def _summarize(self):
        rows = list(csv.reader(self._rows))
        if len(rows) < 2 or "area" not in rows[0]:
            return ""
        header, data = rows[0], rows[1:]
        ia, it = header.index("area"), header.index("t_s")
        iw = header.index("time") if "time" in header else None
        skip = {it, ia, iw}  # time, t_s and area are not averaged
        groups = {}     # area -> list of rows, in the order areas first appear
        for r in data:
            if len(r) == len(header) and r[ia]:
                groups.setdefault(r[ia], []).append(r)
        if not groups:
            return "# >>> no rows had an area label, so no summary\n"

        cols = [i for i, h in enumerate(header) if i not in skip and h and not h.startswith("units=") and h != "flags"]
        dec = {i: decimals(r[i] for r in data if len(r) == len(header)) for i in cols}
        out = [["area", "rows", "time_start", "time_end", "t_start_s", "t_end_s"] + [header[i] + "_mean" for i in cols]
               + [header[i] + "_sd" for i in cols if header[i].startswith(("A_", "B_", "C_"))]]
        for area, rs in groups.items():
            line = [area, len(rs), rs[0][iw] if iw is not None else "", rs[-1][iw] if iw is not None else "",
                    rs[0][it], rs[-1][it]]
            sds = []
            for i in cols:
                v = []
                for r in rs:
                    try:
                        v.append(float(r[i]))
                    except ValueError:
                        pass
                mean = sum(v) / len(v) if v else ""
                # a mean of many rows is good to one more decimal than a single row
                line.append("%.*f" % (dec[i] + 1, mean) if v else "")
                if header[i].startswith(("A_", "B_", "C_")):
                    sd = (sum((x - mean) ** 2 for x in v) / (len(v) - 1)) ** 0.5 if len(v) > 1 else ""
                    sds.append("%.*f" % (dec[i] + 1, sd) if sd != "" else "")
            out.append(line + sds)

        path = self._path[:-4] + "_summary.csv"
        with open(path, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(out)
        return "# >>> per-area averages (%d areas) saved to %s\n" % (len(groups), path)

    def rx(self, text):
        # works on whole lines; while recording, only every SHOW_EVERY-th data line is
        # shown on screen (all of them are saved) so there is room to type
        self._buf += text
        shown = ""
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.startswith("# LOG START"):
                shown += line + "\n" + self._open()
            elif line.startswith("# LOG STOP") or line.startswith("# Leak rig sensors starting"):
                shown += line + "\n" + self._close()      # stop typed, or the ESP32 reset mid-run
            elif self._file and line and not line.startswith("#"):
                if line.startswith("t_s,"):       # header (sent again after cal / units)
                    line = "time," + line
                else:
                    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "," + line
                self._file.write(line + "\n")
                self._file.flush()
                self._rows.append(line)
                if len(self._rows) == 1 or (len(self._rows) - 1) % self.SHOW_EVERY == 1:
                    shown += line + "\n"
            else:
                shown += line + "\n"
        return shown

    SHOW_EVERY = 4   # 4 lines x 0.5 s = one line on screen every 2 s while recording


def decimals(values):
    # most decimal places used in a column (how finely the ESP32 printed it)
    return max((len(v) - v.index(".") - 1 for v in values if "." in v), default=0)
