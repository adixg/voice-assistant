// A cozy pixel-art clock face for the M5StickS3, in Catppuccin Macchiato.
//
// Included from main.cpp *after* the palette block, because it uses those
// COL_* constants and main.cpp's rgb565() rather than defining a second copy.
//
// Screen is 240x135 landscape (M5.Display.setRotation(1), as in setup()).
// Everything is composed into the shared off-screen `canvas` and pushed in one
// go, so the panel only ever receives whole frames — there is no partial-draw
// flicker to avoid in the first place. What *is* avoided is redrawing at all:
// clockFaceTick() re-composes only when something visible actually changed
// (the minute, the battery reading, the link state, or the slow twinkle), so
// an idle clock costs one push a minute rather than eleven a second.
//
// The big digits are hand-drawn from a 4x7 bitmap scaled x6. M5GFX's Font7 is
// a seven-segment face, which is exactly the "conventional digital clock" look
// this is meant not to have; the small text reuses M5GFX's built-in Font0,
// which is already a 5x7 bitmap font and reads as pixel art when scaled.
// pomodoro_face.h (included right after this) reuses the digit font, the
// battery/wifi icons and the ambient decorations declared here, so the two
// alt screens can't visually drift apart the way two independent copies
// eventually would.
#pragma once

#include <time.h>

// ---------------------------------------------------------------- screens
// BtnA cycles through these. SCREEN_RINA is the normal face (mouth/eyes,
// waveform bars); the others are declared here rather than in main.cpp
// because this is the first alt-screen header included, and every alt
// screen after this one needs the same enum to compare against.
enum AltScreen { SCREEN_RINA, SCREEN_CLOCK, SCREEN_POMODORO, SCREEN_COUNT };
static AltScreen currentScreen = SCREEN_RINA;

// America/New_York, with US DST rules baked into the POSIX TZ string:
// EST is UTC-5, EDT starts the 2nd Sunday in March and ends the 1st Sunday in
// November, both at 02:00 local. The ESP32's libc applies these itself, so
// nothing here has to know today's date.
static const char *CLOCK_TZ = "EST5EDT,M3.2.0/2,M11.1.0/2";

// ---------------------------------------------------------------- big digits
// 4 wide x 7 tall, one bit per pixel, MSB = leftmost. Blocky on purpose.
// Shared with the pomodoro screen's MM:SS countdown — same grid, same font.
static const uint8_t kBigDigits[10][7] = {
    {0xF, 0x9, 0x9, 0x9, 0x9, 0x9, 0xF},  // 0
    {0x2, 0x6, 0x2, 0x2, 0x2, 0x2, 0x7},  // 1
    {0xF, 0x1, 0x1, 0xF, 0x8, 0x8, 0xF},  // 2
    {0xF, 0x1, 0x1, 0x7, 0x1, 0x1, 0xF},  // 3
    {0x9, 0x9, 0x9, 0xF, 0x1, 0x1, 0x1},  // 4
    {0xF, 0x8, 0x8, 0xF, 0x1, 0x1, 0xF},  // 5
    {0xF, 0x8, 0x8, 0xF, 0x9, 0x9, 0xF},  // 6
    {0xF, 0x1, 0x1, 0x2, 0x2, 0x4, 0x4},  // 7
    {0xF, 0x9, 0x9, 0xF, 0x9, 0x9, 0xF},  // 8
    {0xF, 0x9, 0x9, 0xF, 0x1, 0x1, 0xF},  // 9
};

static const int CLK_PX     = 6;   // one font pixel, in screen pixels
static const int CLK_DIGIT_W = 4 * CLK_PX;   // 24
static const int CLK_DIGIT_H = 7 * CLK_PX;   // 42
static const int CLK_KERN    = 4;            // between the two digits of a pair
static const int CLK_COLON_W = CLK_PX;
static const int CLK_COLON_GAP = 9;          // either side of the colon
static const int CLK_TIME_Y  = 34;

static void drawBigDigit(int d, int x, int y, uint16_t colour) {
  if (d < 0 || d > 9) return;
  for (int row = 0; row < 7; ++row) {
    uint8_t bits = kBigDigits[d][row];
    for (int col = 0; col < 4; ++col) {
      if (bits & (0x8 >> col)) {
        canvas.fillRect(x + col * CLK_PX, y + row * CLK_PX, CLK_PX, CLK_PX, colour);
      }
    }
  }
}

// ---------------------------------------------------------------- decorations
// Drawn from primitives rather than typed as Unicode: a four-point star with
// tapered arms reads as game UI, where a "✦" glyph reads as text.
static void drawSparkle(int cx, int cy, int arm, uint16_t colour) {
  canvas.drawFastVLine(cx, cy - arm, arm * 2 + 1, colour);
  canvas.drawFastHLine(cx - arm, cy, arm * 2 + 1, colour);
  if (arm >= 2) {  // soften the join so it reads as a star, not a plus
    canvas.drawPixel(cx - 1, cy - 1, colour);
    canvas.drawPixel(cx + 1, cy - 1, colour);
    canvas.drawPixel(cx - 1, cy + 1, colour);
    canvas.drawPixel(cx + 1, cy + 1, colour);
  }
}

// A crescent: one disc, then a second disc in the background colour bitten out
// of it. Offsetting the bite up-left leaves the classic waxing shape.
static void drawCrescent(int cx, int cy, int r, uint16_t colour, uint16_t bg) {
  canvas.fillCircle(cx, cy, r, colour);
  canvas.fillCircle(cx + r - 1, cy - 1, r, bg);
}

// A 5px wave, one pixel thick — the "~" in the mock-up, as pixels.
static void drawWave(int x, int y, uint16_t colour) {
  canvas.drawPixel(x,     y + 1, colour);
  canvas.drawPixel(x + 1, y,     colour);
  canvas.drawPixel(x + 2, y,     colour);
  canvas.drawPixel(x + 3, y + 1, colour);
  canvas.drawPixel(x + 4, y + 2, colour);
  canvas.drawPixel(x + 5, y + 2, colour);
  canvas.drawPixel(x + 6, y + 1, colour);
}

// Battery: outline, nub, and a fill proportional to charge. Peach when low,
// green while charging, subtext otherwise — never louder than the time.
static void drawBatteryIcon(int x, int y, int pct, bool charging) {
  const int w = 20, h = 10;
  uint16_t body = (pct <= 15 && !charging) ? COL_PEACH
                : charging                 ? COL_GREEN
                                           : COL_SUBTEXT;
  canvas.drawRect(x, y, w, h, body);
  canvas.fillRect(x + w, y + 3, 2, 4, body);          // the nub
  int fill = (pct < 0) ? 0 : (pct > 100 ? 100 : pct);
  fill = ((w - 4) * fill) / 100;
  if (fill > 0) canvas.fillRect(x + 2, y + 2, fill, h - 4, body);
}

// Three rising bars plus a base dot. Teal when associated, crust-grey when not,
// so a dropped hotspot is visible without shouting about it.
static void drawWifiIcon(int x, int y, bool up) {
  uint16_t on = up ? COL_TEAL : COL_SUBTEXT;
  uint16_t off = COL_MANTLE;
  canvas.fillRect(x,     y + 6, 2, 3, on);
  canvas.fillRect(x + 3, y + 3, 2, 6, up ? on : off);
  canvas.fillRect(x + 6, y,     2, 9, up ? on : off);
}

// ---------------------------------------------------------------- state
// Battery/link/twinkle are read by every alt screen, not just the clock, so
// they are updated by screenAmbientTick() (below) independently of which
// screen is on top — a screen that isn't showing must not go stale for when
// it's switched back to.
static bool     clockTimeSynced  = false;
static int      clockLastMin     = -1;
static int      clockLastPct     = -1;
static bool     clockLastCharging = false;
static bool     clockLastLink    = false;
static uint8_t  clockTwinkle     = 0;
static uint32_t clockLastTwinkleMs = 0;
static uint32_t clockLastBattMs  = 0;
static int      clockBattPct     = -1;
static bool     clockCharging    = false;

// Top-of-screen ornaments shared by every alt screen, so "make it look like
// the clock" holds by construction rather than by hand-copying coordinates
// into each new screen.
static void drawAmbientDecorations() {
  drawCrescent(16, 14, 6, COL_YELLOW, COL_BASE);
  drawSparkle(32, 9, 2, clockTwinkle ? COL_PINK : COL_MAUVE);
  drawSparkle(canvas.width() / 2 - 78, 66, 2, COL_LAVENDER);
  drawSparkle(canvas.width() / 2 + 78, 52, clockTwinkle ? 3 : 2, COL_PINK);
  // Kept clear of the mantle plate (x 46..194) so they read as sky, not as
  // specks on the clock face.
  canvas.fillRect(canvas.width() / 2 + 84, 78, 2, 2, COL_BLUE);
  canvas.fillRect(canvas.width() / 2 - 86, 40, 2, 2, COL_MAUVE);
}

// Kick off SNTP with the timezone applied. Safe to call again on reconnect —
// the ESP-IDF SNTP client just re-arms.
static void clockBeginNtp() {
  configTzTime(CLOCK_TZ, "pool.ntp.org", "time.google.com", "time.cloudflare.com");
}

// True once the RTC holds a plausible wall-clock date. After this the ESP32's
// own clock keeps running with no network at all, which is the whole point of
// checking a year rather than checking Wi-Fi.
static bool clockHasTime(struct tm *out) {
  if (!getLocalTime(out, 0)) return false;
  return out->tm_year > (2023 - 1900);
}

static const char *kWeekdays[7] = {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};
static const char *kMonths[12]  = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                   "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"};

static void drawClockFace() {
  struct tm now;
  bool have = clockHasTime(&now);
  bool link = (WiFi.status() == WL_CONNECTED);

  canvas.fillSprite(COL_BASE);
  // A one-pixel crust border plus a slightly darker plate behind the time:
  // enough depth to feel like a little device, not enough to be busy.
  canvas.drawRect(0, 0, canvas.width(), canvas.height(), COL_CRUST);
  canvas.fillRoundRect(46, CLK_TIME_Y - 10, canvas.width() - 92, CLK_DIGIT_H + 20, 6, COL_MANTLE);

  // ---- the time, the whole point of the screen
  const int totalW = 4 * CLK_DIGIT_W + 2 * CLK_KERN + CLK_COLON_W + 2 * CLK_COLON_GAP;
  int x = (canvas.width() - totalW) / 2;
  if (have) {
    int hh = now.tm_hour, mm = now.tm_min;
    drawBigDigit(hh / 10, x, CLK_TIME_Y, COL_TEXT);
    x += CLK_DIGIT_W + CLK_KERN;
    drawBigDigit(hh % 10, x, CLK_TIME_Y, COL_TEXT);
    x += CLK_DIGIT_W + CLK_COLON_GAP;
    // Colon blocks sit on the 2nd and 5th font rows so they read as part of
    // the same grid as the digits rather than floating between them.
    canvas.fillRect(x, CLK_TIME_Y + CLK_PX * 2, CLK_COLON_W, CLK_PX, COL_MAUVE);
    canvas.fillRect(x, CLK_TIME_Y + CLK_PX * 4, CLK_COLON_W, CLK_PX, COL_MAUVE);
    x += CLK_COLON_W + CLK_COLON_GAP;
    drawBigDigit(mm / 10, x, CLK_TIME_Y, COL_TEXT);
    x += CLK_DIGIT_W + CLK_KERN;
    drawBigDigit(mm % 10, x, CLK_TIME_Y, COL_TEXT);
  } else {
    // Waiting on NTP: dashes in the same grid, so the layout doesn't jump when
    // the first sync lands.
    for (int i = 0; i < 4; ++i) {
      int dx = x + i * (CLK_DIGIT_W + CLK_KERN) + (i >= 2 ? CLK_COLON_W + 2 * CLK_COLON_GAP - CLK_KERN : 0);
      canvas.fillRect(dx + CLK_PX, CLK_TIME_Y + CLK_PX * 3, CLK_PX * 2, CLK_PX, COL_SUBTEXT);
    }
  }

  // ---- date line: TUE · SEP 08, no year
  canvas.setFont(&fonts::Font0);
  canvas.setTextSize(2);
  canvas.setTextColor(COL_SUBTEXT1, COL_BASE);
  if (have) {
    char left[8], right[12];
    snprintf(left, sizeof(left), "%s", kWeekdays[now.tm_wday % 7]);
    snprintf(right, sizeof(right), "%s %02d", kMonths[now.tm_mon % 12], now.tm_mday);
    // Font0 advances 6px per char; x2 makes that 12.
    int lw = (int)strlen(left) * 12, rw = (int)strlen(right) * 12;
    const int sepW = 14;
    int dx = (canvas.width() - (lw + sepW + rw)) / 2;
    const int dy = 92;
    canvas.setCursor(dx, dy);
    canvas.print(left);
    // The separator dot, drawn rather than typed — Font0 has no middot.
    canvas.fillRect(dx + lw + sepW / 2 - 1, dy + 6, 3, 3, COL_MAUVE);
    canvas.setCursor(dx + lw + sepW, dy);
    canvas.print(right);
  }

  // ---- status, deliberately quiet
  canvas.setTextSize(1);
  canvas.setTextColor(COL_SUBTEXT, COL_BASE);
  if (clockBattPct >= 0) {
    char pct[6];
    snprintf(pct, sizeof(pct), "%d%%", clockBattPct);
    int pw = (int)strlen(pct) * 6;
    canvas.setCursor(canvas.width() - 8 - 22 - 4 - pw, 6);
    canvas.print(pct);
  }
  drawBatteryIcon(canvas.width() - 8 - 22, 5, clockBattPct, clockCharging);
  drawWifiIcon(canvas.width() - 16, canvas.height() - 16, link);

  drawAmbientDecorations();

  // bottom-left run: ~ ˚ ✦ ˚ ~
  const int by = 120;
  drawWave(10, by, COL_BLUE);
  canvas.fillRect(24, by, 2, 2, COL_LAVENDER);
  drawSparkle(36, by + 1, 3, clockTwinkle ? COL_MAUVE : COL_PINK);
  canvas.fillRect(48, by, 2, 2, COL_LAVENDER);
  drawWave(58, by, COL_BLUE);

  canvas.pushSprite(0, 0);
}

// Battery reading, link-status twinkle, and NTP-sync detection: shared by
// every alt screen, and updated regardless of which one is currently shown
// (call every loop()) so a screen doesn't display a stale battery reading
// just because it wasn't the visible one when the real value changed.
static void screenAmbientTick() {
  uint32_t t = millis();
  if (t - clockLastBattMs >= 30000 || clockBattPct < 0) {
    clockLastBattMs = t;
    clockBattPct = M5.Power.getBatteryLevel();
    clockCharging = ((int)M5.Power.isCharging() == 1);
  }
  if (t - clockLastTwinkleMs >= 1400) {   // slow enough to read as ambience
    clockLastTwinkleMs = t;
    clockTwinkle ^= 1;
  }
  if (!clockTimeSynced && WiFi.status() == WL_CONNECTED) {
    struct tm probe;
    if (clockHasTime(&probe)) clockTimeSynced = true;
  }
}

// Call every loop() while the clock is up. Re-composes only on a real change.
static void clockFaceTick() {
  struct tm now;
  int minNow = clockHasTime(&now) ? (now.tm_hour * 60 + now.tm_min) : -1;
  bool link = (WiFi.status() == WL_CONNECTED);

  static uint8_t lastTwinkle = 0xFF;
  bool changed = (minNow != clockLastMin) || (clockBattPct != clockLastPct) ||
                 (clockCharging != clockLastCharging) || (link != clockLastLink) ||
                 (clockTwinkle != lastTwinkle);

  if (!changed) return;
  lastTwinkle = clockTwinkle;
  clockLastMin = minNow;
  clockLastPct = clockBattPct;
  clockLastCharging = clockCharging;
  clockLastLink = link;
  drawClockFace();
}

// Force a full compose on the next tick — used when entering the mode, so the
// screen doesn't keep whatever the face left behind.
static void clockFaceInvalidate() {
  clockLastMin = -2;
}
