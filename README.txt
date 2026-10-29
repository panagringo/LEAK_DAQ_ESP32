LEAK_DAQ_ESP32  -  Leak rig data logger, DAQ REV-C (ESP32 DevKit V1, 30-pin)
=============================================================================
Plain-text quick guide. README.md has everything, with pictures (GitHub shows it nicely).

WHAT IS IN THIS FOLDER
----------------------
  src\main.cpp              the ESP32 program (REV-C)
  platformio.ini            build settings. Don't edit unless you know why.
  wiring\                   REV-C schematic, 70x90 board layout, small boards, solder guide
                            (open wiring\REV-C_solder_guide.html in a browser)
  logs\REV-C\               new test runs end up here automatically
  logs\REV-A\, logs\REV-B\  runs from the older hardware, kept as they were
  monitor\filter_csvlog.py  saves runs to logs\ (runs by itself, don't touch)
  tools\analyze_run.py      finds the steady parts of a run, averages them, works out q, flow, leak area
  tools\analyze_latest.bat  double-click to analyze the newest run
  revisions\                frozen copies of older firmware, tools, wiring and READMEs


REVISIONS
---------
  REV-A  15 Oct (2 runs)  1 MPX on box - room, BMEs on box - room. Logs only, firmware not saved.
  REV-B  15-16 Oct        1 MPX on pitot - room, BMEs on box - room.   git tag REV-B, revisions\REV-B\
  REV-C  from 27 Oct      3 MPX + 4 BME (below).                       this version
  Flash an old one: git checkout REV-B  ->  Upload  ->  git checkout main


THE SENSORS  (all readings are pressure DIFFERENCES; P1 = logo port = higher pressure)
--------------------------------------------------------------------------------------
  A   MPX A on ADS1115 A0   P1 pitot,   P2 box tap      q = pitot - box
  C   MPX C on ADS1115 A1   P1 pitot,   P2 room         pitot - room   (should equal A + B)
  B   MPX B on ADS1115 A2   P1 box tap, P2 room         box - room
  (pitot sensors on A0/A1, box sensor on A2; to remap, edit ADS_IN in src\main.cpp)
  D   BME box - BME room                                rough check on B
  ADS1115 A3 = 5 V supply through 10k/10k.  Each MPX output: 1 kOhm in series, 100 nF to GND.
  I2C bus 0  D21 SDA, D22 SCL : ADS1115 0x48, room1 0x76, room2 0x77
  I2C bus 1  D33 SDA, D32 SCL : box1 0x76, box2 0x77
  No MPX C fitted? Set HAVE_MPX[2] = false at the top of src\main.cpp.


EVERY SESSION
-------------
  1. System OFF, every tube open to the room.
  2. VS Code: plug icon (Serial Monitor) in the bottom bar, then press EN on the ESP32.
  3. Wait for "Ready" (it zeros itself 5 s after power-up), THEN turn the system on.
  4. Type  run  and Enter. Press  +  (no Enter) for each new slider area, and  -  while moving.
     Optional:  mano 0.30 0.28  = manometer left/right readings for this area.
  5. Type  stop  and Enter. The run is saved in logs\REV-C\.


ANALYZE
-------
  Double-click tools\analyze_latest.bat   (or Ctrl+Shift+B in VS Code)
  For each steady section it prints means, q, Q (CFM) and CdA (mm2) and saves
  run_..._trimmed.csv and run_..._trimmed.png next to the log.
  Q and CdA use k = 1 until the pitot is calibrated:   python tools\analyze_run.py --k 0.94


CALIBRATE (after the first REV-C upload, or after changing a sensor or tube)
---------------------------------------------------------------------------
  Type  cal A  (then B, C). Manometer teed onto that MPX's P1 line, its P2 open to the room.
  First point system OFF: type 0. Then 5-8 slider positions: type "left right", e.g. 1.20 1.15.
  Type done to save (u = undo, q = quit). Gain near 1 and R^2 near 1 = good.


FLAGS COLUMN
------------
  OVERA/B/C     that MPX is maxed out          PINNEDA/B/C   that MPX's tubes are swapped
  VS            5 V supply out of range        NO_ADS        ADC board not answering


SHARING
-------
  Ctrl+Alt+P in VS Code (commit, pull, push), or in PowerShell:
  git add -A; git commit -m "Runs 10/28"; git pull --rebase; git push
