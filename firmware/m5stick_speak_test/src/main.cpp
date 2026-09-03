// Speaker-only sanity check for the M5StickS3: press BtnA, hear a fixed,
// pre-baked clip (Rina's real VITS voice, embedded via clip.h) played back.
// No Wi-Fi, no mic, no server, no STT/LLM — isolates the codec/speaker/
// volume path using real synthesized speech instead of a synthetic tone.
// Regenerate the clip with tools/make_test_clip.sh.
#include <M5Unified.h>
#include "clip.h"

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
  Serial.println("\n[boot] m5stick_speak_test starting");

  auto cfg = M5.config();
  M5.begin(cfg);

  M5.Display.setRotation(1);
  M5.Display.setTextSize(2);

  auto spk_cfg = M5.Speaker.config();
  spk_cfg.sample_rate = kClipSampleRate;
  spk_cfg.stereo = false;
  M5.Speaker.config(spk_cfg);
  M5.Speaker.setVolume(255);
  M5.Speaker.begin();

  showStatus("ready", "press BtnA");
}

static bool wasPlaying = false;

void loop() {
  M5.update();
  if (M5.BtnA.wasPressed() && !M5.Speaker.isPlaying()) {
    showStatus("speaking...");
    M5.Speaker.playRaw(kClipSamples, kClipSampleCount, kClipSampleRate, false);
  }
  bool playing = M5.Speaker.isPlaying();
  if (wasPlaying && !playing) showStatus("ready", "press BtnA");
  wasPlaying = playing;
}
