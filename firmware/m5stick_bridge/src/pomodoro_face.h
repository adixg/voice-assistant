// A pomodoro countdown screen, styled identically to clock_face.h: same
// Catppuccin Macchiato palette, same hand-drawn big-digit font, same mantle
// plate and ambient decorations. Reached by cycling BtnA past the clock (see
// AltScreen in clock_face.h, which this file assumes is already included).
//
// The digit grid is literally clock_face.h's HH:MM layout re-purposed for
// MM:SS — same drawBigDigit(), same CLK_* spacing constants — which is what
// "should also be used for the pomodoro timer" means here: it isn't a
// second font tuned to look similar, it's the same font.
//
// BtnB starts/pauses the countdown (see main.cpp's BtnB handling); there is
// no conversation in view on this screen for BtnB's usual "reset" meaning.
#pragma once

enum PomoPhase { POMO_FOCUS, POMO_BREAK };

static const uint32_t POMO_FOCUS_MS = 50UL * 60 * 1000;  // 50 min
static const uint32_t POMO_BREAK_MS = 10UL * 60 * 1000;  // 10 min

// The countdown is wall-clock-independent (unlike the clock face, which just
// reads the RTC): it is our own state, advanced by millis() deltas, so it
// has to keep advancing every loop() regardless of which screen is on top —
// switching to Rina or the clock mid-pomodoro must not pause it by accident.
static PomoPhase pomoPhase       = POMO_FOCUS;
static bool      pomoRunning     = false;
static uint32_t  pomoRemainingMs = POMO_FOCUS_MS;
static uint32_t  pomoLastTickMs  = 0;

// This screen's own redraw-diff cache, independent of the clock's — sharing
// one cache between two screens would make switching between them skip a
// redraw whenever the two happened to agree on a value.
static int      pomoLastSec      = -1;
static bool     pomoLastRunning  = false;
static PomoPhase pomoLastPhase   = POMO_FOCUS;
static int      pomoLastPct      = -1;
static bool     pomoLastCharging = false;
static bool     pomoLastLink     = false;

// Advances the countdown and flips FOCUS<->BREAK at zero. Call every loop(),
// unconditionally — see the comment on the state above.
static void pomodoroTick() {
  uint32_t now = millis();
  uint32_t elapsed = now - pomoLastTickMs;
  pomoLastTickMs = now;
  if (!pomoRunning) return;  // still rebase pomoLastTickMs above, so a long
                              // pause doesn't show up as elapsed time on resume
  if (elapsed >= pomoRemainingMs) {
    pomoPhase = (pomoPhase == POMO_FOCUS) ? POMO_BREAK : POMO_FOCUS;
    pomoRemainingMs = (pomoPhase == POMO_FOCUS) ? POMO_FOCUS_MS : POMO_BREAK_MS;
  } else {
    pomoRemainingMs -= elapsed;
  }
}

static void pomodoroToggleRun() {
  pomoRunning = !pomoRunning;
}

static void drawPomodoroFace() {
  bool link = bleLinkUp();
  uint16_t phaseColour = (pomoPhase == POMO_FOCUS) ? COL_RED : COL_GREEN;

  canvas.fillSprite(COL_BASE);
  canvas.drawRect(0, 0, canvas.width(), canvas.height(), COL_CRUST);
  canvas.fillRoundRect(46, CLK_TIME_Y - 10, canvas.width() - 92, CLK_DIGIT_H + 20, 6, COL_MANTLE);

  // ---- the countdown, MM:SS in the clock's own digit grid
  int totalSec = pomoRemainingMs / 1000;
  int mm = totalSec / 60, ss = totalSec % 60;
  const int totalW = 4 * CLK_DIGIT_W + 2 * CLK_KERN + CLK_COLON_W + 2 * CLK_COLON_GAP;
  int x = (canvas.width() - totalW) / 2;
  drawBigDigit(mm / 10, x, CLK_TIME_Y, COL_TEXT);
  x += CLK_DIGIT_W + CLK_KERN;
  drawBigDigit(mm % 10, x, CLK_TIME_Y, COL_TEXT);
  x += CLK_DIGIT_W + CLK_COLON_GAP;
  // The colon carries the phase colour (tomato red / rest green) so the
  // pomodoro identity survives even though the grid is borrowed wholesale
  // from the clock, where the same two blocks are always mauve.
  canvas.fillRect(x, CLK_TIME_Y + CLK_PX * 2, CLK_COLON_W, CLK_PX, phaseColour);
  canvas.fillRect(x, CLK_TIME_Y + CLK_PX * 4, CLK_COLON_W, CLK_PX, phaseColour);
  x += CLK_COLON_W + CLK_COLON_GAP;
  drawBigDigit(ss / 10, x, CLK_TIME_Y, COL_TEXT);
  x += CLK_DIGIT_W + CLK_KERN;
  drawBigDigit(ss % 10, x, CLK_TIME_Y, COL_TEXT);

  // ---- phase label + a play/pause glyph, where the clock shows its date
  canvas.setFont(&fonts::Font0);
  canvas.setTextSize(2);
  canvas.setTextColor(COL_SUBTEXT1, COL_BASE);
  const char *label = (pomoPhase == POMO_FOCUS) ? "FOCUS" : "BREAK";
  int lw = (int)strlen(label) * 12;      // Font0 advances 6px/char; x2 = 12
  const int glyphGap = 14;
  int dx = (canvas.width() - (lw + glyphGap)) / 2;
  const int dy = 92;
  canvas.setCursor(dx, dy);
  canvas.print(label);
  // A glyph rather than a second word ("RUNNING"/"PAUSED"), so this line
  // stays exactly as short as the clock's date line rather than growing it.
  int gx = dx + lw + 4, gy = dy + 2;
  if (pomoRunning) {
    canvas.fillTriangle(gx, gy, gx, gy + 8, gx + 7, gy + 4, phaseColour);
  } else {
    canvas.fillRect(gx, gy, 2, 8, phaseColour);
    canvas.fillRect(gx + 4, gy, 2, 8, phaseColour);
  }

  // ---- status, deliberately quiet — identical to the clock's
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
  drawLinkIcon(canvas.width() - 16, canvas.height() - 16, link);

  drawAmbientDecorations();

  // ---- bottom-centre progress bar, replacing the clock's wave-row: how far
  // through the current phase, filled in the phase colour a segment at a time.
  const int segN = 10, segW = 8, segGap = 2, segH = 6;
  const int barW = segN * segW + (segN - 1) * segGap;
  int bx = (canvas.width() - barW) / 2;
  const int by = 119;
  uint32_t fullMs = (pomoPhase == POMO_FOCUS) ? POMO_FOCUS_MS : POMO_BREAK_MS;
  float frac = 1.0f - (float)pomoRemainingMs / (float)fullMs;
  int filled = (int)(frac * segN + 0.5f);
  for (int i = 0; i < segN; ++i) {
    canvas.fillRect(bx + i * (segW + segGap), by, segW, segH,
                    (i < filled) ? phaseColour : COL_MANTLE);
  }

  canvas.pushSprite(0, 0);
}

// Call every loop() while the pomodoro screen is up. Re-composes only when
// something visible changed — the countdown only needs whole-second
// precision, so this is at most one push a second, not eleven. (The ambient
// twinkle can lag up to ~0.4s behind its own 1.4s cycle here, since it isn't
// itself part of the diff below; imperceptible against a once-a-second redraw
// and not worth a second diff variable.)
static void pomodoroFaceTick() {
  bool link = bleLinkUp();
  int sec = pomoRemainingMs / 1000;

  bool changed = (sec != pomoLastSec) || (pomoRunning != pomoLastRunning) ||
                 (pomoPhase != pomoLastPhase) || (clockBattPct != pomoLastPct) ||
                 (clockCharging != pomoLastCharging) || (link != pomoLastLink);
  if (!changed) return;
  pomoLastSec = sec;
  pomoLastRunning = pomoRunning;
  pomoLastPhase = pomoPhase;
  pomoLastPct = clockBattPct;
  pomoLastCharging = clockCharging;
  pomoLastLink = link;
  drawPomodoroFace();
}

// Force a full compose on the next tick — used when entering the screen.
static void pomodoroFaceInvalidate() {
  pomoLastSec = -2;
}

// Back to a fresh 50-minute FOCUS block, stopped rather than left counting
// down unnoticed. Bound to BtnB double-click (main.cpp) rather than
// wasClicked(), which fires on every raw release, including each half of a
// double-click — using it here would toggle run/pause twice before this
// ever ran. wasClicked() stays in use for the plain reset-conversation
// meaning on the other screens, since that one should stay instant.
static void pomodoroReset() {
  pomoPhase = POMO_FOCUS;
  pomoRemainingMs = POMO_FOCUS_MS;
  pomoRunning = false;
  pomodoroFaceInvalidate();
}
