LEAK_DAQ_ESP32  -  Leak rig data logger (ESP32 DevKit V1, 30-pin)
==================================================================

WHAT IS IN THIS FOLDER
----------------------
  src\main.cpp             the ESP32 program
  platformio.ini           build settings. Don't edit unless you know why.
  wiring_diagram.png       how every wire connects. Check this FIRST if a sensor is NOT FOUND.
  logs\                    your saved test runs (.csv) end up here automatically
  monitor\filter_csvlog.py saves runs to logs\ (runs by itself, don't touch)
  tools\analyze_run.py     finds the steady parts of a run and averages them
  tools\analyze_latest.bat double-click to analyze the newest run


WHAT YOU NEED (ONE TIME)
------------------------
  1. VS Code with the "PlatformIO IDE" extension installed
     (Extensions panel on the left -> search PlatformIO -> Install -> restart VS Code).
  2. A USB cable that carries DATA. Many cheap cables only charge.
     If the ESP32 never shows up, try a different cable before anything else.
  3. USB driver for the ESP32's USB chip (look at the small chip next to the USB port):
       says CP2102 / CP210x  -> install "CP210x USB to UART driver" from Silicon Labs
       says CH340 / CH9102   -> install the "CH340" driver from WCH
  4. Python (python.org) with matplotlib, only if you want the plots:
       open a terminal and type:   pip install matplotlib


OPEN THE PROJECT
----------------
  VS Code -> File -> Open Folder -> pick THIS folder (the one containing platformio.ini):
      Desktop\CAPSTONE\Leakage Test\LEAK_DAQ_ESP32
  Not CAPSTONE, not "Leakage Test" - THIS folder. If you open the wrong one, the
  PlatformIO buttons at the bottom won't appear.


THE SENSORS  (all readings are pressure DIFFERENCES)
---------------------------------------------------
  A   MPX5010DP on ADS1115 A0   pitot tube (facing into the flow) minus room
  B   BME/BMP280 "box1"         box minus room
  C   BME/BMP280 "box2"         box minus room
  Room pressure = average of the two room sensors (room1, room2).

  I2C bus 0 (D21 SDA, D22 SCL): ADS1115 0x48, room1 0x76, room2 0x77
  I2C bus 1 (D33 SDA, D32 SCL): box1 0x76,  box2 0x77


STEP 1 - UPLOAD THE PROGRAM TO THE ESP32  (only after main.cpp changes)
----------------------------------------------------------------------
  1. Plug the ESP32 into the laptop.
  2. In the blue bar at the very BOTTOM of VS Code, click the  ->  arrow (Upload).
  3. The first time, it downloads the ESP32 tools. This takes several minutes. Wait.
  4. If it sits on "Connecting......" : press and HOLD the BOOT button on the ESP32
     until you see "Writing at 0x..." then let go.
  5. Done when it says  SUCCESS  in green.


STEP 2 - START UP  (every session)
----------------------------------
  !! The vacuum / system must be OFF when the ESP32 starts. !!
  The ESP32 zeros itself 5 seconds after it powers up, using room air as zero.
  If the system is running then, every reading for the whole session is wrong.

  1. System OFF. All sensor tubes open to the room.
  2. Click the PLUG icon in the bottom bar (Serial Monitor).
  3. Press the EN button on the ESP32 so you see the startup from the top:

      # Leak rig sensors starting
      # ADS1115 0x48 bus0: OK
      # BME280 room1  bus0 0x76: OK
      # BME280 box1   bus1 0x76: OK
      # BME280 room2  bus0 0x77: OK
      # BME280 box2   bus1 0x77: OK
      # AUTO-ZERO in 5 s - keep the system OFF until "ZERO done".
      # ZERO done.
      # Ready: turn the system on, then type run (or log) to record, stop to end.

  4. Only after "Ready" -> turn the system on.

  "NOT FOUND" = that sensor isn't wired (or is wired wrong). Check wiring_diagram.png.
  "WARNING: ... is LOW at idle" = a wire on that I2C bus is shorted or unplugged.
  BMP280 instead of BME280 is fine - both work.

  Nothing scrolls until you type  run  - that is normal.

  To type a command: click inside the monitor, type it, press ENTER.


STEP 3 - RECORD A TEST RUN
--------------------------
  1. Type   run   and press Enter.
     You'll see  "# >>> saving to ...logs\run_<date>_<time>.csv"  and data starts scrolling.
  2. Label each outlet area as you go - this is what the averages get grouped by:
       +   (just press the + key, NO Enter)  = next area: A, then B, then C ...
       -   (just press the - key, NO Enter)  = "slider moving" (rows left unlabelled)
     So the rhythm is:   +  wait ~10 s  -  move slider  +  wait ~10 s  -  move slider ...
     Hold each slider position STILL for at least ~10 seconds.
  3. Optional: read the manometer while the area is steady and type both sides:
       mano 0.30 0.28    (left reading, right reading) -> saved in the mano column
  4. Type   stop   and press Enter. You'll see  "# >>> saved N rows to ..."

  While recording, the screen only shows every 4th line so you have room to type.
  ALL lines are still saved.
  If the ESP32 resets mid-run, the file is closed and kept automatically.

  Each run gives you, in logs\ :
     run_<date>_<time>.csv           every reading (opens in Excel)
     run_<date>_<time>_summary.csv   average of every column for each area label


STEP 4 - ANALYZE THE RUN
------------------------
  Easiest: double-click  tools\analyze_latest.bat
     -> analyzes the newest run, prints a table, opens the plot.
  Or in VS Code: PlatformIO sidebar (alien-head icon) -> PROJECT TASKS -> Custom ->
     "Analyze latest run".

  This finds the steady periods by itself, so it still works if you forgot to label.
  It creates next to the log:
     run_..._trimmed.csv   one row per steady period: averages + standard deviations
     run_..._trimmed.png   plot + table: each section shaded and labeled, trimmed averages underneath

  Message "skipped ... too short" = you didn't hold that position long enough.
  Message "No steady plateaus found" = in a terminal in this folder, try:
     python tools\analyze_run.py --min 2 --window 2


CALIBRATING AGAINST THE MANOMETER  (only when needed)
-----------------------------------------------------
  Calibration is SAVED inside the ESP32 and survives unplugging. Only redo it if you
  change a sensor or tubing, or the numbers disagree with the manometer.

  The manometer: Dwyer U-tube, red 0.826 fluid, mounted at 30 degrees. This is built
  into the code - you just type what the ruler says on each side.

  1. Do NOT type run. Type   cal A   (or cal B / cal C) and follow the screen:
  2. First point: system OFF, type   0   and Enter.
  3. System on. Set the slider, wait until the fluid is still.
  4. Type both sides of the ruler, e.g.   1.20 1.15   and Enter. Hold still 3 s.
  5. Repeat 3-4 for 5 to 8 slider positions.
  6. Type   done   to save.     u = undo last point     q = quit without saving
  It prints a FIT. Gain near 1.0 and R^2 near 1.0 = good.
  "Slope is negative" = the tubes are on the wrong ports. Swap them.


ALL COMMANDS  (type  h  in the monitor for the list on the ESP32 itself)
------------------------------------------------------------------------
  run   (or log)   start recording to logs\
  stop             stop recording
  +                next area label A, B, C...   (one key, no Enter)
  -                slider moving, no label      (one key, no Enter)
  area B           set the area label by hand.   area  alone = clear it
  mano 0.30 0.28   manometer left and right readings for the current area
  z                zero again (system OFF, all tubes open to room)
  cal A            calibrate a sensor (A, B or C) against the manometer
  units in         numbers in inH2O.  Also:  units mm   or   units pa
  vs 5.02          type what a multimeter reads on the ESP32 5V pin (one time)
  p                print current calibration
  scan             list every I2C device found (wiring check)
  reset            wipe calibration back to defaults
  h                help


THE flags COLUMN
----------------
  OVERA     pitot sensor maxed out (pressure too high for it)
  PINNEDA   pitot sensor stuck at 0 V: tubes are on the wrong ports, swap them
  VS        5 V supply out of range (4.5-5.3 V) - check USB power
  NO_ADS    the ADC board isn't answering - check its wiring


SOMETHING'S WRONG
-----------------
  No COM port / upload says "could not open port"
      -> different USB cable (data cable!), install the driver above, different USB port.
      -> close any other program using the port (Arduino IDE, another monitor window).
  Upload stuck on "Connecting......"
      -> hold BOOT on the ESP32 while it connects.
  Monitor shows garbage characters
      -> press EN to restart. (Speed is 115200, already set in platformio.ini.)
  Nothing is scrolling
      -> type  run.  Data only streams while recording.
  A sensor says NOT FOUND
      -> check wiring_diagram.png, then type  scan.  Loose jumper wires are the #1 cause.
  "bus1 SKIPPED"
      -> a box-sensor wire (D32/D33) is shorted or a box sensor has no power.
         B and C won't work until it's fixed. Fix it, then press EN.
  Readings aren't ~0 with the system off
      -> the system was on at startup. Turn it off, type  z  (or press EN).
  Nothing appears in logs\
      -> you didn't type  run  first, or you used a different serial monitor
         (only the PlatformIO plug-icon monitor saves files).
  + and - don't do anything
      -> click inside the monitor first, and press them at the START of a line
         (if you've typed something else, press Enter first).
  Analyze shows a table but no plot
      -> pip install matplotlib
