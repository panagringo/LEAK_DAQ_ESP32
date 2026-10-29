/*
  leak_sensors.ino  -  sensor readout + manometer calibrationn
  ESP32 DevKit V1 + ADS1115 + 1 x MPX5010DP + 4 x BMP280/BME280

  I2C bus 0 (D21 SDA, D22 SCL): ADS1115 0x48, BME room1 0x76, BME room2 0x77
  I2C bus 1 (D33 SDA, D32 SCL): BME box1 0x76, BME box2 0x77

  CHANNELS (all are pressure DIFFERENCES in Pa; each can be calibrated)
    A  MPX on ADS A0   P1 (loalgo) = pitot tube facing into the flow, P2 = open to room   (pitot - room, see MPX_SIGN)
    B  BME box1 minus average of the room BMEs
    C  BME box2 minus average of the room BMEs
  Also printed: room pressure, temperatures, 5 V supply.

  MANOMETER: Dwyer U-tube, 0.826 SG red fluid, scale in inH2O for VERTICAL use,
  zero in the middle counting up both ways. Mounted on the box side at MANO_ANGLE_DEG
  (30) from horizontal for 2x resolution. You type what each side reads, e.g. "1.20 1.15":
      dP [inH2O] = (left + right) * sin(30)     (fluid SG is built into the scale)

  STARTUP: power the ESP32 with the system OFF. It auto-zeros after SETTLE_MS,
  prints "Ready", then turn the system on.

  CALIBRATION (Serial Monitor, 115200 baud, "Newline")
    z            zero again: all ports open to room air, vac off
    cal A        guided calibration of channel A against the manometer
    units in     units for the live stream: in (inH2O), mm (mmH2O) or pa
    p            print calibration     s  pause/resume stream    h  help

  Libraries (Arduino Library Manager): Adafruit BME280 Library, Adafruit ADS1X15,
  Adafruit Unified Sensor.  
   Board: "ESP32 Dev Module".
*/


#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#include <Adafruit_BMP280.h>
#include <Adafruit_ADS1X15.h>
#include <Preferences.h>
#include <math.h>
#include <string.h>
#include <stdlib.h>
#include <ctype.h>

// ---------------------------------------------------------------- pins / setup
#define SDA0 21
#define SCL0 22
#define SDA1 33
#define SCL1 32
#define ADS_ADDR 0x48

const int ADS_AVG = 8;                           // ADC samples averaged per reading
const unsigned long STREAM_MS = 500;             // one CSV line every 0.5 s
const unsigned long A_AVG_N = 10;                // smoothed A column = mean of the last 10 lines (5 s)

const float RHO_WATER = 998.2f;                  // kg/m3, water at 20 C
const float G0 = 9.80665f;
const float PA_PER_IN = RHO_WATER * G0 * 0.0254f;   // 248.64 Pa per inch of water (vertical)
const float PA_PER_MM = RHO_WATER * G0 * 0.001f;    // 9.789 Pa per mm of water
const float MPX_SENS = 0.09f;                    // Vout/Vs per kPa (datasheet)
const float MPX_ZERO_NOM = 0.04f;                // Vout/Vs at 0 kPa (datasheet)
// The MPX5010DP only reads P1 > P2. Pitot setup: the pipe tube faces INTO the flow on P1 (logo),
// P2 open to the room, so A = pitot (total) pressure - room and reads positive.
// If the pipe tube is ever moved to P2 to read suction, set -1 so A still reads pipe - room.
const float MPX_SIGN = 1.0f;
const float MPX_FLOOR = 0.004f;                  // Vout/Vs below this = output pinned at 0 V (reversed past range)
const unsigned long SETTLE_MS = 5000;            // sensors settle this long before the boot auto-zero

// ---------------------------------------------------------------- channels
const int NCH = 3;                   // 0 = MPX on ADS A0; 1, 2 = box BMEs minus room
const char CH_NAME[NCH] = {'A', 'B', 'C'};
const char *CH_DESC[NCH] = {"MPX pitot-room","BME box1-room", "BME box2-room"};
const int CH_BME[NCH] = {-1, 1, 3};  // which bme[] slot each box channel reads

struct Cal {
  uint32_t version = 4;
  float zero[NCH] = {MPX_ZERO_NOM, 0, 0};  // MPX: ratio; BME: Pa
  float gain[NCH] = {1, 1, 1};
  float bmeOff[4] = {0, 0, 0, 0};    // Pa, each BME relative to room1 at zero
  float divK = 2.0f;                 // Vs = divK * V(A3)
  uint8_t units = 0;                 // 0 inH2O, 1 mmH2O, 2 Pa (live stream only)
} cal;

// manometer: total scale reading (left + right) -> Pa. The scale includes the 0.826 fluid
// but assumes vertical, so the 30 deg tilt (fluid travels 1/sin further) is taken out here.
const float MANO_ANGLE_DEG = 30.0f;              // manometer tilt from horizontal, fixed
const float MANO_SIN = sinf(MANO_ANGLE_DEG * M_PI / 180.0f);
float manoPa(float scaleTotal) { return PA_PER_IN * scaleTotal * MANO_SIN; }

void printMano() {
  Serial.printf("# Manometer: Dwyer U-tube at %.0f deg, enter LEFT RIGHT readings. 1.00 total on the scale = %.3f inH2O = %.2f Pa.\n",
                MANO_ANGLE_DEG, MANO_SIN, manoPa(1.0f));
}

Adafruit_ADS1115 ads;
Adafruit_BME280 bme[4];              // 0 room1, 2 room2 (bus 0)   1 box1, 3 box2 (bus 1)
Adafruit_BMP280 *bmp[4] = {nullptr, nullptr, nullptr, nullptr};   // used instead when the board is a BMP280
const char *BME_NAME[4] = {"room1", "box1", "room2", "box2"};
bool okAds = false, okBme[4] = {false, false, false, false};
bool bus1Ok = true;                  // false when a bus-1 line is stuck low at boot

// "GY-BME/PMP280" boards can be either chip; each slot uses whichever library fits
float baroPressure(int i) { return bmp[i] ? bmp[i]->readPressure() : bme[i].readPressure(); }
float baroTemp(int i)     { return bmp[i] ? bmp[i]->readTemperature() : bme[i].readTemperature(); }
Preferences prefs;

struct Reading {
  float vs = NAN;
  float raw[NCH];                    // MPX: Vout/Vs ratio; BME channels: Pa difference before cal
  float dp[NCH];                     // calibrated Pa
  bool over = false;                 // MPX past the ADC's range
  bool under = false;                // MPX output pinned at 0 V: pressure on the wrong port, or tube on the wrong side
  float p[4], t[4];
};

// ---------------------------------------------------------------- storage
void saveCal() { prefs.begin("leaksens", false); prefs.putBytes("cal", &cal, sizeof(cal)); prefs.end(); }
void loadCal() {
  prefs.begin("leaksens", true);
  if (prefs.getBytesLength("cal") == sizeof(cal)) {
    Cal tmp; prefs.getBytes("cal", &tmp, sizeof(tmp));
    if (tmp.version == cal.version) cal = tmp;
  }
  prefs.end();
}

float toUnits(float pa) { return cal.units == 0 ? pa / PA_PER_IN : cal.units == 1 ? pa / PA_PER_MM : pa; }
const char *unitName() { return cal.units == 0 ? "inH2O" : cal.units == 1 ? "mmH2O" : "Pa"; }
// decimals worth printing in the stream units. The sensors resolve ~0.1 Pa (ADS1115 + MPX: 0.07 Pa/LSB on
// the fine range; BME280: 0.18 Pa) = 0.0004 inH2O = 0.01 mmH2O. The manometer is read by eye to 0.01 on
// the 30 deg ruler = 0.005 inH2O = 1.2 Pa, so it gets fewer.
int unitDec() { return cal.units == 0 ? 4 : cal.units == 1 ? 2 : 1; }
int manoDec() { return cal.units == 0 ? 3 : cal.units == 1 ? 1 : 0; }

// ---------------------------------------------------------------- reading
float fsr(adsGain_t g) {
  switch (g) {
    case GAIN_ONE:  return 4.096f;
    case GAIN_FOUR: return 1.024f;
    default:        return 4.096f;
  }
}

float adsVolts(uint8_t ch, adsGain_t g, int n, float &vmax) {
  ads.setGain(g);
  ads.readADC_SingleEnded(ch);       // throw away the first conversion after switching
  long sum = 0; int16_t mx = -32768;
  for (int i = 0; i < n; i++) {
    int16_t r = ads.readADC_SingleEnded(ch);
    sum += r; if (r > mx) mx = r;
  }
  vmax = mx * fsr(g) / 32768.0f;
  return (float)sum / n * fsr(g) / 32768.0f;
}

// fine range (+/-1.024 V) up to about 1.7 kPa, then the coarse range up to about 6.9 kPa
float mpxVolts(uint8_t ch, bool &over) {
  float vmax;
  float v = adsVolts(ch, GAIN_FOUR, ADS_AVG, vmax);
  if (vmax >= 0.98f) v = adsVolts(ch, GAIN_ONE, ADS_AVG, vmax);
  over = vmax >= 3.25f;              // the ADS1115 on 3.3 V can't read above this
  return v;
}

Reading readAll() {
  Reading r;
  for (int c = 0; c < NCH; c++) { r.raw[c] = NAN; r.dp[c] = NAN; }
  for (int i = 0; i < 4; i++) { r.p[i] = r.t[i] = NAN; }

  if (okAds) {
    float vmax;
    r.vs = cal.divK * adsVolts(3, GAIN_ONE, ADS_AVG, vmax);
    r.raw[0] = mpxVolts(0, r.over) / r.vs;
    r.dp[0] = MPX_SIGN * cal.gain[0] * (r.raw[0] - cal.zero[0]) / MPX_SENS * 1000.0f;
    r.under = r.raw[0] < MPX_FLOOR;
  }
  for (int i = 0; i < 4; i++) {
    if (!okBme[i]) continue;
    r.p[i] = baroPressure(i) - cal.bmeOff[i];
    r.t[i] = baroTemp(i);
  }
  float room = NAN;
  if (okBme[0] && okBme[2]) room = 0.5f * (r.p[0] + r.p[2]);
  else if (okBme[0]) room = r.p[0];
  else if (okBme[2]) room = r.p[2];
  if (!isnan(room))
    for (int c = 1; c < NCH; c++) {
      if (!okBme[CH_BME[c]]) continue;
      r.raw[c] = r.p[CH_BME[c]] - room;
      r.dp[c] = cal.gain[c] * (r.raw[c] - cal.zero[c]);
    }
  return r;
}

// unscaled Pa for a channel (zero removed, gain NOT applied) - what calibration fits against
float uncal(const Reading &r, int c) {
  if (c == 0) return MPX_SIGN * (r.raw[0] - cal.zero[0]) / MPX_SENS * 1000.0f;
  return r.raw[c] - cal.zero[c];
}

bool chanOk(int c) {
  if (c == 0) return okAds;
  return (okBme[0] || okBme[2]) && okBme[CH_BME[c]];
}

// average a channel over ms milliseconds; returns mean and standard deviation (unscaled Pa)
void sampleChan(int c, unsigned long ms, float &mean, float &sd, int &n) {
  double s = 0, s2 = 0; n = 0;
  unsigned long t0 = millis();
  while (millis() - t0 < ms || n < 3) {
    float v = uncal(readAll(), c);
    if (!isnan(v)) { s += v; s2 += (double)v * v; n++; }
  }
  mean = n ? s / n : NAN;
  sd = n > 1 ? sqrt(fmax(0.0, (s2 - s * s / n) / (n - 1))) : 0;
}

// ---------------------------------------------------------------- serial line input
char line[64]; int lineLen = 0;

void areaNext();
void areaMoving();

// returns true when a full line is in `line`
// "+" or "-" typed at the start of a line act at once, no Enter: next area / slider moving
bool pollLine() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (lineLen == 0 && ch == '+') { areaNext(); continue; }
    if (lineLen == 0 && ch == '-') { areaMoving(); continue; }
    if (ch == '\r' || ch == '\n') {
      if (lineLen == 0) continue;
      line[lineLen] = 0; lineLen = 0;
      return true;
    }
    if (lineLen < (int)sizeof(line) - 1) line[lineLen++] = ch;
  }
  return false;
}

void lower(char *s) { for (; *s; s++) *s = tolower(*s); }

int chanIndex(char ch) {
  ch = toupper(ch);
  for (int c = 0; c < NCH; c++) if (CH_NAME[c] == ch) return c;
  return -1;
}

// ---------------------------------------------------------------- zero
void doZero() {
  Serial.println("# ZERO: vac OFF, every sensor port open to room air. Hold still for 5 s...");
  const int N = 25;
  double sr[NCH] = {0}, sp[4] = {0};
  int nr[NCH] = {0};
  // BME offsets first (relative to room1), so the BME channels below use them
  float oldOff[4]; memcpy(oldOff, cal.bmeOff, sizeof(oldOff));
  for (int i = 0; i < 4; i++) cal.bmeOff[i] = 0;
  for (int k = 0; k < N; k++) {
    Reading r = readAll();
    for (int i = 0; i < 4; i++) if (okBme[i]) sp[i] += r.p[i];
  }
  if (okBme[0]) for (int i = 0; i < 4; i++) cal.bmeOff[i] = okBme[i] ? (sp[i] - sp[0]) / N : 0;
  else memcpy(cal.bmeOff, oldOff, sizeof(oldOff));
  for (int c = 1; c < NCH; c++) cal.zero[c] = 0;
  for (int k = 0; k < N; k++) {
    Reading r = readAll();
    for (int c = 0; c < NCH; c++) if (!isnan(r.raw[c])) { sr[c] += r.raw[c]; nr[c]++; }
  }
  for (int c = 0; c < NCH; c++) if (nr[c]) cal.zero[c] = sr[c] / nr[c];
  saveCal();
  Serial.println("# ZERO done.");
  if (chanOk(0) && (cal.zero[0] < -0.005f || cal.zero[0] > 0.085f))   // datasheet offset 0-0.425 V at 5 V
    Serial.printf("# WARNING: MPX zero (%.4f of supply) is outside the datasheet band. Check its wiring.\n", cal.zero[0]);
  for (int i = 1; i < 4; i++)
    if (okBme[i] && fabsf(cal.bmeOff[i]) > 150)
      Serial.printf("# NOTE: BME %s reads %.0f Pa off room1 (normal is under about 100). Offset stored.\n",
                    BME_NAME[i], cal.bmeOff[i]);
}

// ---------------------------------------------------------------- calibration wizard
void calWizard(int c) {
  if (!chanOk(c)) { Serial.printf("# Channel %c is not available (sensor missing).\n", CH_NAME[c]); return; }
  const int MAXP = 20;
  float man[MAXP], sen[MAXP]; int n = 0;

  Serial.printf("\n# ===== CALIBRATE %c (%s) =====\n", CH_NAME[c], CH_DESC[c]);
  printMano();
  Serial.println("# 1. Manometer: one leg open to the room, the other leg teed onto this sensor's HIGH side.");
  if (c == 0) Serial.println("#    For the MPX, open its P2 (low) port to the room while calibrating.");
  else Serial.println("#    For a box BME, the manometer goes on a box tap; the room BMEs stay in the room.");
  Serial.println("# 2. First point: system OFF, type 0.");
  Serial.println("# 3. System on. Set the outlet slider, wait until the fluid is still.");
  Serial.println("# 4. Type what EACH SIDE of the ruler reads (left right), then Enter, e.g.  1.20 1.15");
  Serial.println("#    The ESP32 averages 3 s - hold the slider still. Repeat 3-4 for 5-8 slider positions.");
  Serial.println("#    Commands: reading = add point, u = undo last, done = fit and save, q = quit.");

  unsigned long lastShow = 0;
  while (true) {
    if (millis() - lastShow > 1000) {
      lastShow = millis();
      Reading r = readAll();
      float u = uncal(r, c);
      float g = cal.gain[c] * u;
      Serial.printf("#   live %c: %.1f Pa = %.2f total on your ruler (uncalibrated %.1f Pa)%s   points: %d\n",
                    CH_NAME[c], g, g / manoPa(1.0f), u, (c == 0 && r.over) ? "  OVER RANGE" : (c == 0 && r.under) ? "  PINNED AT 0 V - swap the MPX tubes" : "", n);
    }
    if (!pollLine()) { delay(10); continue; }
    lower(line);
    if (!strcmp(line, "q")) { Serial.println("# Calibration cancelled, nothing changed."); return; }
    if (!strcmp(line, "u")) { if (n) { n--; Serial.printf("# Removed point %d.\n", n + 1); } continue; }
    if (!strcmp(line, "done")) break;
    char *end; float v = strtof(line, &end);
    if (end == line) { Serial.println("# Type a reading, u, done or q."); continue; }
    char *end2; float v2 = strtof(end, &end2);
    float left = fabsf(v), right = 0;
    if (end2 != end) right = fabsf(v2);
    else if (v != 0) Serial.println("#   (one number = the total of both sides)");
    v = left + right;                            // centre-zero scale: total deflection = left + right
    if (n >= MAXP) { Serial.println("# 20 points max. Type done."); continue; }
    float mean, sd; int ns;
    Serial.println("#   averaging...");
    sampleChan(c, 3000, mean, sd, ns);
    // the manometer gives the size; the sensor gives the direction (vacuum = negative)
    man[n] = mean < 0 ? -manoPa(v) : manoPa(v); sen[n] = mean; n++;
    Serial.printf("#   point %d: %.2f + %.2f = %.2f on ruler = %.3f inH2O = %.1f Pa | sensor %.1f Pa (scatter %.1f Pa, %d samples)%s\n",
                  n, left, right, v, v * MANO_SIN, man[n - 1], mean, sd, ns,
                  sd > 0.02f * fabsf(mean) + 3 ? "  <- unsteady, consider u and redo" : "");
  }

  if (n < 3) { Serial.println("# Need at least 3 points. Nothing changed."); return; }
  double sx = 0, sy = 0, sxx = 0, sxy = 0;
  for (int i = 0; i < n; i++) { sx += man[i]; sy += sen[i]; sxx += (double)man[i] * man[i]; sxy += (double)man[i] * sen[i]; }
  double den = n * sxx - sx * sx;
  if (fabs(den) < 1e-6) { Serial.println("# All points at the same pressure. Nothing changed."); return; }
  double a = (n * sxy - sx * sy) / den, b = (sy - a * sx) / n;      // sensor = a * manometer + b
  if (a <= 0) { Serial.println("# Slope is negative: the tubes are on the wrong ports. Nothing changed."); return; }
  double ssr = 0, sst = 0, my = sy / n;
  for (int i = 0; i < n; i++) {
    double e = sen[i] - (a * man[i] + b); ssr += e * e; sst += (sen[i] - my) * (sen[i] - my);
  }
  float newGain = 1.0 / a;
  Serial.println("#   point   manometer Pa   sensor after cal Pa   error Pa");
  for (int i = 0; i < n; i++)
    Serial.printf("#   %3d   %10.1f   %12.1f   %+8.1f\n", i + 1, man[i], newGain * sen[i], newGain * sen[i] - man[i]);
  Serial.printf("# FIT %c: gain %.4f (was %.4f), offset %+.1f Pa, R^2 %.5f, rms error %.1f Pa\n",
                CH_NAME[c], newGain, cal.gain[c], b, sst > 0 ? 1 - ssr / sst : 0, sqrt(ssr / n) / a);
  if (newGain < 0.8f || newGain > 1.25f)
    Serial.println("# WARNING: gain is far from 1. Check the manometer is level and zeroed, and for a leaking tee.");
  if (fabs(b) > 5) Serial.println("# WARNING: offset over 5 Pa. Run z (zero) again, then recalibrate.");
  cal.gain[c] = newGain;
  saveCal();
  Serial.println("# Saved. It stays after power-off.\n");
}

// ---------------------------------------------------------------- misc commands
void printCal() {
  Serial.printf("# stream units %s, 5 V divider factor %.4f\n", unitName(), cal.divK);
  printMano();
  for (int c = 0; c < NCH; c++)
    Serial.printf("# %c %-16s zero %10.5f  gain %.4f  %s\n", CH_NAME[c], CH_DESC[c], cal.zero[c], cal.gain[c],
                  chanOk(c) ? "" : "(not available)");
  for (int i = 0; i < 4; i++)
    Serial.printf("# BME %-6s %s  offset %+.1f Pa\n", BME_NAME[i], okBme[i] ? "OK " : "---", cal.bmeOff[i]);
}

void printHelp() {
  Serial.println(F(
    "# z            zero again (done automatically at power-up; system must be OFF)\n"
    "# cal A        calibrate channel A against the manometer (A, B or C)\n"
    "# units in     units for the live stream: in, mm or pa\n"
    "# vs 5.02      the 5 V pin as read on a multimeter (once)\n"
    "# run / log    start recording (saved to logs/ on the PC)    stop   end it\n"
    "# +            (one key, no Enter) next area label A, B, C...     -   slider moving (blank)\n"
    "# area B       label the rows from now on with outlet area B    area   blank (slider moving)\n"
    "# mano 0.30 0.28   manometer left right readings for the current area (goes in the mano column)\n"
    "# p            print calibration\n"
    "# reset        all calibration back to defaults\n"
    "# scan         list I2C devices on both buses and identify the BME chips\n"
    "# h            this help"));
}

void printHeader() {
  const char *u = unitName();
  Serial.printf("t_s,area,A_pitot_room_%s,A_avg_%lus_%s,B_bme_box1_%s,C_bme_box2_%s,mano_%s,"
                "P_room_Pa,T_room_C,T_box_C,Vs_V,flags\n", u, A_AVG_N * STREAM_MS / 1000, u, u, u, u);
}

bool streaming = false;              // off until "run" / "log"; the monitor's csvlog filter saves the run
unsigned long lastOut = 0, runStart = 0;
float aHist[A_AVG_N]; int aCount = 0;   // recent A readings (Pa) for the smoothed column; reset each run
char areaLabel[16] = "";             // outlet-area label stamped on every row ("" while the slider is moving)
float manoReading = NAN;             // manometer reading (Pa) typed for the current area; cleared when the area changes
int nextArea = 0;                    // "+" labels the next letter: 0 = A, 1 = B, ...

void areaMoving() {
  areaLabel[0] = 0; manoReading = NAN;
  Serial.println("# AREA - moving (rows unlabelled)");
}

void areaNext() {
  manoReading = NAN;
  if (nextArea < 26) snprintf(areaLabel, sizeof(areaLabel), "%c", 'A' + nextArea);
  else snprintf(areaLabel, sizeof(areaLabel), "A%d", nextArea);
  nextArea++;
  Serial.printf("# AREA = %s\n", areaLabel);
}
void scanAll();

void handleCommand() {
  lower(line);
  char *cmd = strtok(line, " ");
  char *arg = strtok(NULL, " ");
  if (!cmd) return;
  if (!strcmp(cmd, "z")) doZero();
  else if (!strcmp(cmd, "cal")) {
    int c = arg ? chanIndex(arg[0]) : -1;
    while (c < 0) {                              // no sensor given: ask for one
      Serial.println("# Calibrate which sensor?  A = pitot   B = box sensor 1   C = box sensor 2   (q = cancel)");
      while (!pollLine()) delay(10);
      lower(line);
      if (!strcmp(line, "q")) return;
      c = chanIndex(line[0]);
    }
    calWizard(c);
    if (streaming) printHeader();
  }
  else if (!strcmp(cmd, "units") && arg) {
    if (!strcmp(arg, "in")) cal.units = 0; else if (!strcmp(arg, "mm")) cal.units = 1;
    else if (!strcmp(arg, "pa")) cal.units = 2; else { Serial.println("# units in, mm or pa"); return; }
    saveCal(); Serial.printf("# units now %s\n", unitName()); if (streaming) printHeader();
  }
  else if (!strcmp(cmd, "vs") && arg && okAds) {
    float vmax, v3 = adsVolts(3, GAIN_ONE, 64, vmax);
    cal.divK = atof(arg) / v3; saveCal(); printCal();
  }
  else if (!strcmp(cmd, "p")) printCal();
  else if (!strcmp(cmd, "run") || !strcmp(cmd, "log")) {
    if (streaming) { Serial.println("# already running - type stop first"); return; }
    streaming = true; lastOut = millis() - STREAM_MS; runStart = millis(); aCount = 0;
    areaLabel[0] = 0; manoReading = NAN; nextArea = 0;
    Serial.println("# LOG START"); printHeader();
    Serial.println("# Press + for the next area (A, B, C...), - while moving the slider. No Enter needed.");
  }
  else if (!strcmp(cmd, "area")) {
    // area B  -> label rows "B" from now on;  area (alone) -> blank, while the slider is moving
    areaLabel[0] = 0; manoReading = NAN;
    if (arg) {
      strncpy(areaLabel, arg, sizeof(areaLabel) - 1); areaLabel[sizeof(areaLabel) - 1] = 0;
      for (char *s = areaLabel; *s; s++) *s = (*s == ',') ? '_' : toupper(*s);   // no commas in a CSV field
      Serial.printf("# AREA = %s\n", areaLabel);
    } else Serial.println("# AREA cleared (rows unlabelled while you move the slider)");
  }
  else if (!strcmp(cmd, "mano") && arg) {
    // mano 0.30 0.28 -> left + right on the 30 deg ruler, stamped on this area's rows
    char *arg2 = strtok(NULL, " ");
    float total = fabsf(atof(arg)) + (arg2 ? fabsf(atof(arg2)) : 0);
    manoReading = manoPa(total);
    Serial.printf("# MANO %.2f on ruler = %.3f inH2O = %.1f Pa  (area %s)\n", total, total * MANO_SIN, manoReading,
                  areaLabel[0] ? areaLabel : "none");
  }
  else if (!strcmp(cmd, "stop")) {
    if (!streaming) { Serial.println("# not running"); return; }
    streaming = false; Serial.println("# LOG STOP");
  }
  else if (!strcmp(cmd, "reset")) {
    Cal keep = cal; cal = Cal(); cal.units = keep.units;
    saveCal(); printCal();
  }
  else if (!strcmp(cmd, "scan")) { scanAll(); printCal(); }
  else if (!strcmp(cmd, "h")) printHelp();
  else Serial.println("# unknown command, type h");
}

// ---------------------------------------------------------------- I2C diagnostics
// lists every device on a bus; for 0x76/0x77 also reads the chip-ID register (0xD0)
// chip-ID register 0xD0: 0x60 BME280, 0x56-0x58 BMP280; 0 = nothing answered
uint8_t chipId(TwoWire &w, uint8_t a) {
  w.beginTransmission(a); w.write(0xD0);
  if (w.endTransmission(false) != 0) return 0;
  if (w.requestFrom(a, (uint8_t)1) != 1) return 0;
  return w.read();
}

void scanBus(TwoWire &w, int busNum) {
  Serial.printf("# I2C scan bus%d:", busNum);
  int found = 0;
  for (uint8_t a = 0x08; a < 0x78; a++) {
    w.beginTransmission(a);
    if (w.endTransmission() == 0) { Serial.printf(" 0x%02X", a); found++; }
  }
  Serial.println(found ? "" : " nothing (check SDA/SCL, 3.3 V and GND to the sensors)");
  for (uint8_t a = 0x76; a <= 0x77; a++) {
    uint8_t id = chipId(w, a);
    if (!id) continue;
    const char *what = id == 0x60 ? "BME280"
                     : (id >= 0x56 && id <= 0x58) ? "BMP280 (pressure + temp, no humidity)"
                     : id == 0x61 ? "BME680 - wrong library"
                     : "unknown chip";
    Serial.printf("#   0x%02X chip ID 0x%02X = %s\n", a, id, what);
  }
}

void scanAll() { scanBus(Wire, 0); if (bus1Ok) scanBus(Wire1, 1); }

// ---------------------------------------------------------------- setup / loop
void setup() {
  Serial.begin(115200);
  delay(400);
  Serial.println("\n# Leak rig sensors starting");
  // idle I2C lines must sit HIGH (pulled up by the sensor boards); LOW = short, missing pull-up or unpowered board
  const int idlePins[4] = {SDA0, SCL0, SDA1, SCL1};
  const char *idleName[4] = {"bus0 SDA D21", "bus0 SCL D22", "bus1 SDA D33", "bus1 SCL D32"};
  for (int k = 0; k < 4; k++) {
    pinMode(idlePins[k], INPUT);
    delay(2);
    if (!digitalRead(idlePins[k])) {
      Serial.printf("# WARNING: %s is LOW at idle (no pull-up, short to GND, or board unpowered)\n", idleName[k]);
      if (k >= 2) bus1Ok = false;
    }
  }
  Wire.begin(SDA0, SCL0, 100000);
  Wire.setTimeOut(20);
  if (bus1Ok) { Wire1.begin(SDA1, SCL1, 100000); Wire1.setTimeOut(20); }
  else Serial.println("# bus1 SKIPPED (a stuck line would hang the ESP32): box sensors B/C disabled until fixed");
  loadCal();
  scanAll();

  okAds = ads.begin(ADS_ADDR, &Wire);
  if (okAds) ads.setDataRate(RATE_ADS1115_475SPS);
  Serial.printf("# ADS1115 0x48 bus0: %s\n", okAds ? "OK" : "NOT FOUND");

  const uint8_t addr[4] = {0x76, 0x76, 0x77, 0x77};
  TwoWire *bus[4] = {&Wire, &Wire1, &Wire, &Wire1};   // room on bus 0, box on bus 1
  for (int i = 0; i < 4; i++) {
    uint8_t id = (bus[i] == &Wire1 && !bus1Ok) ? 0 : chipId(*bus[i], addr[i]);
    const char *type = "---";
    if (id == 0x60) {
      okBme[i] = bme[i].begin(addr[i], bus[i]);
      if (okBme[i])
        bme[i].setSampling(Adafruit_BME280::MODE_NORMAL, Adafruit_BME280::SAMPLING_X2,
                           Adafruit_BME280::SAMPLING_X16, Adafruit_BME280::SAMPLING_X1,
                           Adafruit_BME280::FILTER_X4, Adafruit_BME280::STANDBY_MS_0_5);
      type = "BME280";
    } else if (id >= 0x56 && id <= 0x58) {
      bmp[i] = new Adafruit_BMP280(bus[i]);
      okBme[i] = bmp[i]->begin(addr[i], id);
      if (okBme[i])
        bmp[i]->setSampling(Adafruit_BMP280::MODE_NORMAL, Adafruit_BMP280::SAMPLING_X2,
                            Adafruit_BMP280::SAMPLING_X16, Adafruit_BMP280::FILTER_X4,
                            Adafruit_BMP280::STANDBY_MS_1);
      else { delete bmp[i]; bmp[i] = nullptr; }
      type = "BMP280";
    }
    Serial.printf("# %-6s %-6s bus%d 0x%02X: ", type, BME_NAME[i], (i == 1 || i == 3) ? 1 : 0, addr[i]);
    if (okBme[i]) Serial.println("OK");
    else if (id) Serial.printf("FAILED (chip ID 0x%02X)\n", id);
    else Serial.println("NOT FOUND (no answer: wiring, power, or SDO pin sets the other address)");
  }
  printCal();
  printHelp();

  // auto-zero: the ESP32 is always powered before the system, so everything reads room air now
  Serial.printf("# AUTO-ZERO in %lu s - keep the system OFF until \"ZERO done\".\n", SETTLE_MS / 1000);
  delay(SETTLE_MS);
  doZero();
  Serial.println("# Ready: turn the system on, then type run (or log) to record, stop to end.");
}

void loop() {
  if (pollLine()) handleCommand();
  if (!streaming || millis() - lastOut < STREAM_MS) return;
  lastOut = millis();

  Reading r = readAll();
  float pRoom = okBme[0] ? r.p[0] : (okBme[2] ? r.p[2] : NAN);
  float tRoom = okBme[0] ? r.t[0] : (okBme[2] ? r.t[2] : NAN);
  float tBox = okBme[1] ? r.t[1] : (okBme[3] ? r.t[3] : NAN);

  char flags[48] = "";
  if (r.over) strcat(flags, "OVERA;");
  if (r.under) strcat(flags, "PINNEDA;");
  if (okAds && (r.vs < 4.5f || r.vs > 5.3f)) strcat(flags, "VS;");
  if (!okAds) strcat(flags, "NO_ADS;");

  // smoothed A: mean of the last A_AVG_N readings (fewer at the start of a run)
  float aAvg = NAN;
  if (!isnan(r.dp[0])) {
    aHist[aCount % A_AVG_N] = r.dp[0]; aCount++;
    int n = aCount < A_AVG_N ? aCount : A_AVG_N;
    float s = 0; for (int i = 0; i < n; i++) s += aHist[i];
    aAvg = s / n;
  }

  char mano[12] = "";
  if (!isnan(manoReading)) snprintf(mano, sizeof(mano), "%.*f", manoDec(), toUnits(manoReading));

  // t_s to the ms, P 0.1 Pa, T 0.01 C (BME280 resolution), Vs 0.1 mV (ADS1115 LSB x divider = 0.25 mV, 8x averaged)
  int d = unitDec();
  Serial.printf("%.3f,%s,%.*f,%.*f,%.*f,%.*f,%s,%.1f,%.2f,%.2f,%.4f,%s\n",
                (millis() - runStart) / 1000.0, areaLabel, d, toUnits(r.dp[0]), d, toUnits(aAvg), d, toUnits(r.dp[1]),
                d, toUnits(r.dp[2]), mano, pRoom, tRoom, tBox, r.vs, flags);
}
