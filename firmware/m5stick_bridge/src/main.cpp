// M5StickS3 push-to-talk mic/speaker bridge for bridge_server.py.
//
// Protocol over one WebSocket connection (raw PCM s16le mono @ SAMPLE_RATE
// both ways, framed with tiny text control messages):
//   BtnA held   -> client sends text "start", then binary mic chunks
//   BtnA release-> client sends text "stop"
//   server replies with text "reply:<text>" (shown on screen), then binary
//   audio chunks, then text "end" -> client plays the buffered reply. A
//   reply text starting with "(" is bridge_server.py's error-sentinel
//   convention ("(ollama error: ...)", "(didn't catch that)") — used here
//   to pick ERROR vs SUCCESS once playback finishes.
//
// BtnA pressed while SPEAKING -> stops playback cleanly (interrupt) instead
//   of starting a new recording immediately, since the mic and speaker share
//   one I2S codec and switching modes mid-playback glitches the audio.
// BtnB click       -> reset conversation history (tells the server)
// BtnB long-press  -> toggle standby: Wi-Fi/mic/speaker off, screen dimmed.
//                     No dedicated hardware power button is wired up on this
//                     board (checked M5Unified's board table directly), so
//                     this is a software standby, not true deep sleep — the
//                     MCU keeps running and polling BtnB, which means there's
//                     no risk of the device going unresponsive and needing a
//                     USB reflash to recover. Long-press again to wake.
//
// The face is the hand-designed pixel-art sprite sheet (sprites.h, generated
// by tools/make_face_sprites.py from a reference PNG), blitted from an
// off-screen M5Canvas so nothing flickers. Boot/Wi-Fi-connect and standby
// still use small procedural shapes — no sprite was drawn for those. All of
// it (state, mic/TTS amplitude) is already local to the Stick; nothing here
// needs anything from the server beyond what the protocol above already sends.
#include <M5Unified.h>
#include <M5GFX.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <math.h>
#include "secrets.h"
#include "sprites.h"

static const uint32_t SAMPLE_RATE = 16000;
static const size_t MIC_CHUNK_SAMPLES = 512;  // ~32ms/chunk

WebSocketsClient webSocket;
static M5Canvas canvas(&M5.Display);

// ---------------------------------------------------------------- palette
static inline uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b) {
  return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}
static const uint16_t COL_BG     = rgb565(8, 10, 18);
static const uint16_t COL_LISTEN = rgb565(70, 220, 130);  // green, for the waveform bars
static const uint16_t COL_DIM    = rgb565(70, 70, 80);    // standby dots

enum UiState { UI_BOOT, UI_WIFI, UI_IDLE, UI_LISTENING, UI_THINKING, UI_SPEAKING,
               UI_SUCCESS, UI_ERROR, UI_INTERRUPTED, UI_SLEEPING };
static UiState uiState = UI_BOOT;
static float micLevel = 0.0f;    // live 0..1 level while listening
static float speakLevel = 0.0f;  // live 0..1 level while speaking
static uint32_t speakStartMs = 0;
static bool lastReplyWasError = false;
static String replyText;  // shown at the bottom of the screen; cleared on a new recording/reset
static uint32_t transientUntil = 0;  // auto-revert-to-idle deadline for brief states

static const int HIST_N = 14;
static float levelHist[HIST_N] = {0};
static int histPos = 0;

enum RecState { REC_IDLE, RECORDING };
static RecState recState = REC_IDLE;
static int16_t micBuf[MIC_CHUNK_SAMPLES];

static uint8_t *replyBuf = nullptr;
static size_t replyCap = 0;
static size_t replyLen = 0;
static bool receivingReply = false;

static float rms16(const int16_t *data, size_t n) {
  if (n == 0) return 0.0f;
  double sumsq = 0;
  for (size_t i = 0; i < n; i++) sumsq += (double)data[i] * data[i];
  float level = (sqrtf(sumsq / n) / 32768.0f) * 8.0f;  // same gain as the desktop orb
  return level > 1.0f ? 1.0f : level;
}

static void ensureReplyCap(size_t need) {
  if (replyCap >= need) return;
  size_t newCap = need + 64 * 1024;
  uint8_t *nb = (uint8_t *)ps_realloc(replyBuf, newCap);
  if (nb) {
    replyBuf = nb;
    replyCap = newCap;
  }
}

// Screen shows only her, no text — this still logs every transition to
// Serial for debugging over USB.
static void setStatus(const String &line1, const String &line2 = "") {
  Serial.printf("[status] %s %s\n", line1.c_str(), line2.c_str());
}

// briefly show a state, then auto-revert to idle after `ms`
static void showTransient(UiState st, const String &l1, const String &l2, uint32_t ms) {
  uiState = st;
  setStatus(l1, l2);
  transientUntil = millis() + ms;
}

// ---------------------------------------------------------------- display
// She sits near the top; the strip below is reserved for reply text
// (LISTENING uses that same strip for the reactive waveform instead).
static const int SPRITE_TOP_CENTERED = 6;
static const int SPRITE_TOP_LISTENING = 4;

static void drawSpriteCentered(const uint16_t *data, int y) {
  canvas.pushImage((canvas.width() - kSpriteW) / 2, y, kSpriteW, kSpriteH, data);
}

// Greedy word-wrap: returns the text[startIdx..] that fits in maxChars,
// breaking on the last space at or before the limit, and reports where
// the next line should pick up via nextIdx.
static String wrapLine(const String &text, int maxChars, int startIdx, int &nextIdx) {
  int n = text.length();
  if (startIdx >= n) {
    nextIdx = startIdx;
    return "";
  }
  int end = startIdx + maxChars;
  if (end >= n) {
    nextIdx = n;
    return text.substring(startIdx);
  }
  int cut = text.lastIndexOf(' ', end);
  if (cut <= startIdx) cut = end;  // no space to break on — hard cut
  nextIdx = cut + 1;
  return text.substring(startIdx, cut);
}

static void drawReplyText() {
  if (replyText.length() == 0) return;
  if (uiState == UI_BOOT || uiState == UI_WIFI || uiState == UI_SLEEPING || uiState == UI_LISTENING) return;

  static const int MAX_LINES = 2;
  static const int MAX_CHARS = 36;
  canvas.setTextColor(TFT_WHITE);
  canvas.setTextSize(1);
  canvas.setTextDatum(top_left);

  int y = 135 - 20;
  int idx = 0;
  for (int line = 0; line < MAX_LINES && idx < (int)replyText.length(); line++) {
    int next;
    String l = wrapLine(replyText, MAX_CHARS, idx, next);
    if (line == MAX_LINES - 1 && next < (int)replyText.length()) {
      while (l.length() > (unsigned)(MAX_CHARS - 3)) l.remove(l.length() - 1);
      l += "...";
    }
    canvas.setCursor(4, y + line * 9);
    canvas.println(l);
    idx = next;
  }
}

static void drawFace() {
  int w = canvas.width(), h = canvas.height();
  canvas.fillScreen(COL_BG);
  int cx = w / 2, cy = h / 2 - 10;
  uint32_t t = millis();

  switch (uiState) {
    case UI_BOOT:
    case UI_WIFI: {
      float a = (t % 1000) / 1000.0f * 2 * PI;
      for (int i = 0; i < 8; i++) {
        float ai = a + i * PI / 4;
        uint8_t bright = 255 - i * 28;
        canvas.fillCircle(cx + cosf(ai) * 14, cy + sinf(ai) * 14, 2,
                           canvas.color565(bright, bright, bright));
      }
      break;
    }

    case UI_IDLE: {
      // gentle bob, occasional blink, and every ~9s a brief idle flourish
      int bob = (int)(sinf(t / 2600.0f) * 2.0f);
      bool blink = (t % 4200) > 4000;
      bool flourish = (t % 9000) > 8400;
      const uint16_t *spr = flourish ? kSprite_idle_alt : (blink ? kSprite_blink : kSprite_idle);
      drawSpriteCentered(spr, SPRITE_TOP_CENTERED + bob);
      break;
    }

    case UI_LISTENING: {
      drawSpriteCentered(kSprite_listening, SPRITE_TOP_LISTENING);
      // a real waveform of recent mic levels, right under the face
      int barW = 6, gap = 3;
      int totalW = HIST_N * (barW + gap) - gap;
      int x0 = cx - totalW / 2;
      int barBase = SPRITE_TOP_LISTENING + kSpriteH + 18;
      for (int i = 0; i < HIST_N; i++) {
        int idx = (histPos + i) % HIST_N;  // oldest..newest, left to right
        int barH = 3 + (int)(levelHist[idx] * 18);
        canvas.fillRoundRect(x0 + i * (barW + gap), barBase - barH, barW, barH, 1, COL_LISTEN);
      }
      break;
    }

    case UI_THINKING: {
      const uint16_t *spr = ((t / 450) % 2) ? kSprite_thinking_b : kSprite_thinking_a;
      drawSpriteCentered(spr, SPRITE_TOP_CENTERED);
      break;
    }

    case UI_SPEAKING: {
      // mouth sprite switches by amplitude threshold (3 discrete tiers)
      const uint16_t *spr = speakLevel > 0.35f ? kSprite_speaking_high
                           : speakLevel > 0.12f ? kSprite_speaking_mid
                                                 : kSprite_speaking_low;
      drawSpriteCentered(spr, SPRITE_TOP_CENTERED);
      break;
    }

    case UI_SUCCESS:
      drawSpriteCentered(kSprite_success, SPRITE_TOP_CENTERED);
      break;

    case UI_ERROR:
      drawSpriteCentered(kSprite_error, SPRITE_TOP_CENTERED);
      break;

    case UI_INTERRUPTED:
      drawSpriteCentered(kSprite_interrupted, SPRITE_TOP_CENTERED);
      break;

    case UI_SLEEPING: {
      canvas.fillCircle(cx - 20, cy, 2, COL_DIM);
      canvas.fillCircle(cx + 20, cy, 2, COL_DIM);
      break;
    }
  }

  drawReplyText();
  canvas.pushSprite(0, 0);
}

static uint32_t lastDraw = 0;
static void maybeDrawFace() {
  uint32_t now = millis();
  if (now - lastDraw >= 40) {  // ~25fps
    lastDraw = now;
    drawFace();
  }
}

// ---------------------------------------------------------------- network
static void webSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      setStatus("connected", WS_HOST);
      break;
    case WStype_DISCONNECTED:
      setStatus("disconnected...");
      break;
    case WStype_TEXT: {
      String msg((char *)payload, length);
      if (msg.startsWith("reply:")) {
        replyText = msg.substring(6);
        lastReplyWasError = replyText.startsWith("(");
        uiState = UI_THINKING;
        setStatus("Generating", replyText);
        receivingReply = true;
        replyLen = 0;
      } else if (msg == "end") {
        receivingReply = false;
        if (replyLen > 0) {
          M5.Mic.end();
          M5.Speaker.begin();
          M5.Speaker.playRaw((int16_t *)replyBuf, replyLen / 2, SAMPLE_RATE, false);
          speakStartMs = millis();
          uiState = UI_SPEAKING;
        } else if (lastReplyWasError) {
          showTransient(UI_ERROR, "error", "", 700);
        } else {
          uiState = UI_IDLE;
          setStatus("ready", "hold BtnA to talk");
        }
      }
      break;
    }
    case WStype_BIN:
      if (receivingReply) {
        ensureReplyCap(replyLen + length);
        memcpy(replyBuf + replyLen, payload, length);
        replyLen += length;
      }
      break;
    default:
      break;
  }
}

static void connectNetwork() {
  uiState = UI_WIFI;
  setStatus("wifi...", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(150);
    M5.update();
    maybeDrawFace();
    if (M5.BtnB.wasReleasedAfterHold()) return;  // bail out of a stuck reconnect back to standby
  }
  setStatus("wifi ok", WiFi.localIP().toString());
  webSocket.begin(WS_HOST, WS_PORT, WS_PATH);
  M5.Mic.begin();
  uiState = UI_IDLE;
  setStatus("ready", "hold BtnA to talk");
}

// ---------------------------------------------------------------- standby
static void enterStandby() {
  uiState = UI_SLEEPING;
  setStatus("standby", "hold BtnB to wake");
  webSocket.disconnect();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  M5.Mic.end();
  M5.Speaker.end();
  M5.Display.setBrightness(20);
}

static void wakeFromStandby() {
  M5.Display.setBrightness(200);
  connectNetwork();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[boot] m5stick_bridge starting");

  auto cfg = M5.config();
  M5.begin(cfg);

  M5.Display.setRotation(1);
  // 16-bit to match sprites.h's RGB565 data exactly — 8-bit here was
  // forcing every pushImage() to downconvert 65536 colors to ~256,
  // mangling the pixel art. Costs ~65KB more canvas RAM (240x135x2),
  // trivial against the S3's 320KB.
  canvas.setColorDepth(16);
  canvas.createSprite(M5.Display.width(), M5.Display.height());
  // LovyanGFX's _swapBytes has inverted-sounding semantics: false (the
  // default) means "treat incoming uint16_t* as already byte-swapped",
  // true means "treat it as plain packed RGB565" — which is exactly what
  // sprites.h contains ((r<<11)|(g<<5)|b, no swapping). Without this,
  // pushImage() silently re-parses every sprite's bits in the wrong
  // layout (confirmed by tracing LGFXBase::create_pc_fast in M5GFX).
  canvas.setSwapBytes(true);

  {
    auto mic_cfg = M5.Mic.config();
    mic_cfg.sample_rate = SAMPLE_RATE;
    mic_cfg.stereo = false;
    M5.Mic.config(mic_cfg);

    auto spk_cfg = M5.Speaker.config();
    spk_cfg.sample_rate = SAMPLE_RATE;
    spk_cfg.stereo = false;
    M5.Speaker.config(spk_cfg);
    M5.Speaker.setVolume(255);  // M5Unified defaults to a conservative volume
  }

  replyCap = 200 * 1024;
  replyBuf = (uint8_t *)ps_malloc(replyCap);

  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(3000);

  connectNetwork();
}

void loop() {
  M5.update();

  if (transientUntil && millis() > transientUntil) {
    transientUntil = 0;
    uiState = UI_IDLE;
    setStatus("ready", "hold BtnA to talk");
  }

  if (M5.BtnB.wasClicked() && uiState != UI_SLEEPING) {
    webSocket.sendTXT("reset");
    setStatus("history cleared");
    replyText = "";
  }
  if (M5.BtnB.wasReleasedAfterHold()) {
    if (uiState == UI_SLEEPING) {
      wakeFromStandby();
    } else {
      enterStandby();
    }
  }

  if (uiState == UI_SLEEPING) {
    maybeDrawFace();
    return;  // no Wi-Fi/mic/speaker work while in standby
  }

  webSocket.loop();

  // interrupt: stop playback cleanly rather than switching mic/speaker mid-stream
  if (uiState == UI_SPEAKING && M5.BtnA.wasPressed()) {
    M5.Speaker.stop();
    receivingReply = false;
    speakLevel = 0.0f;
    showTransient(UI_INTERRUPTED, "interrupted", "", 300);
  } else if (recState == REC_IDLE && uiState != UI_SPEAKING && uiState != UI_SLEEPING &&
             M5.BtnA.wasPressed()) {
    recState = RECORDING;
    uiState = UI_LISTENING;
    replyText = "";
    M5.Speaker.end();
    M5.Mic.begin();
    webSocket.sendTXT("start");
    setStatus("listening...");
  }

  if (recState == RECORDING) {
    if (M5.Mic.record(micBuf, MIC_CHUNK_SAMPLES, SAMPLE_RATE)) {
      micLevel = rms16(micBuf, MIC_CHUNK_SAMPLES);
      levelHist[histPos] = micLevel;
      histPos = (histPos + 1) % HIST_N;
      webSocket.sendBIN((uint8_t *)micBuf, MIC_CHUNK_SAMPLES * sizeof(int16_t));
    }
    if (M5.BtnA.wasReleased()) {
      recState = REC_IDLE;
      uiState = UI_THINKING;
      micLevel = 0.0f;
      webSocket.sendTXT("stop");
      setStatus("Processing");
    }
  }

  if (uiState == UI_SPEAKING) {
    if (M5.Speaker.isPlaying()) {
      uint32_t elapsedMs = millis() - speakStartMs;
      size_t totalSamples = replyLen / 2;
      size_t sampleIdx = (size_t)((uint64_t)elapsedMs * SAMPLE_RATE / 1000);
      size_t window = 320;  // 20ms
      if (sampleIdx >= totalSamples) {
        window = 0;
      } else if (sampleIdx + window > totalSamples) {
        window = totalSamples - sampleIdx;
      }
      speakLevel = window ? rms16((int16_t *)replyBuf + sampleIdx, window) : 0.0f;
    } else {
      speakLevel = 0.0f;
      showTransient(lastReplyWasError ? UI_ERROR : UI_SUCCESS,
                     lastReplyWasError ? "error" : "done", "", 500);
    }
  }

  maybeDrawFace();
}
