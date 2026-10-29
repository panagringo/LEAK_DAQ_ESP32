# DAQ REV-A (15 Oct 2026, first two runs)

**Hardware:** ESP32 DevKit V1 + ADS1115 + 1 × MPX5010DP + 4 × BME/BMP280. Same wiring as REV-B
(`../REV-B/wiring_diagram.png`). The only difference is where the MPX tubes went.

| Ch | Sensor | Measured |
|---|---|---|
| A | MPX5010DP on ADS1115 A0 | box − room (P1 = box, P2 = room) |
| B | BME "box1" | box − room |
| C | BME "box2" | box − room |

**Logs:** `logs/REV-A/run_2026-10-15_14-00-08.csv` and `run_2026-10-15_14-18-54.csv`. Their columns are
`A_mpx_box_room, B_bme_box1, C_bme_box2, units=inH2O, ...`, with no area labels and no PC timestamp.

**Firmware:** not saved. The MPX was moved to the pitot and the firmware became REV-B (area labels,
manometer column, `A_pitot_room`) before the project was first committed to git on 15 Oct.

`tools/analyze_run.py` on `main` recognises these logs and treats A as the box signal.
