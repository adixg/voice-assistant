// Pure-hardware mic/speaker loopback test for the M5StickS3 — no Wi-Fi, no
// server, no STT/LLM/TTS. Hold BtnA, talk, release: it plays back exactly
// what it heard. Use this to sanity-check the mic/codec/speaker/volume in
// isolation before blaming the network or the AI pipeline.
#include <M5Unified.h>

static const uint32_t SAMPLE_RATE = 16000;
static const size_t MIC_CHUNK_SAMPLES = 512;      // ~32ms/chunk
static const size_t MAX_SAMPLES = SAMPLE_RATE * 8;  // 8s cap, fits easily in PSRAM

static int16_t *buf = nullptr;
static size_t len = 0;  // samples captured so far

enum State { IDLE, RECORDING, PLAYING };
static State state = IDLE;

static void showStatus(const String &line1, const String &line2 = "") {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setCursor(0, 0);
  M5.Display.println(line1);
  if (line2.length()) M5.Display.println(line2);
  Serial.printf("[status] %s %s\n", line1.c_str(), line2.c_str());
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[boot] m5stick_echo_test starting");

  auto cfg = M5.config();
  M5.begin(cfg);

  M5.Display.setRotation(1);
  M5.Display.setTextSize(2);

  auto mic_cfg = M5.Mic.config();
  mic_cfg.sample_rate = SAMPLE_RATE;
  mic_cfg.stereo = false;
  M5.Mic.config(mic_cfg);

  auto spk_cfg = M5.Speaker.config();
  spk_cfg.sample_rate = SAMPLE_RATE;
  spk_cfg.stereo = false;
  M5.Speaker.config(spk_cfg);
  M5.Speaker.setVolume(255);

  buf = (int16_t *)ps_malloc(MAX_SAMPLES * sizeof(int16_t));
  if (!buf) {
    showStatus("PSRAM alloc failed!");
    while (true) delay(1000);
  }

  M5.Mic.begin();
  showStatus("ready", "hold BtnA to talk");
}

void loop() {
  M5.update();

  if (state == IDLE && M5.BtnA.wasPressed()) {
    state = RECORDING;
    len = 0;
    M5.Speaker.end();
    M5.Mic.begin();
    showStatus("recording...");
  }

  if (state == RECORDING) {
    size_t room = MAX_SAMPLES - len;
    size_t want = room < MIC_CHUNK_SAMPLES ? room : MIC_CHUNK_SAMPLES;
    if (want > 0 && M5.Mic.record(buf + len, want, SAMPLE_RATE)) {
      len += want;
    }
    bool full = len >= MAX_SAMPLES;
    if (full || M5.BtnA.wasReleased()) {
      state = PLAYING;
      float secs = (float)len / SAMPLE_RATE;
      showStatus("playing back", String(secs, 1) + "s");
      M5.Mic.end();
      M5.Speaker.begin();
      M5.Speaker.playRaw(buf, len, SAMPLE_RATE, false);
    }
  }

  if (state == PLAYING && !M5.Speaker.isPlaying()) {
    state = IDLE;
    showStatus("ready", "hold BtnA to talk");
  }
}
