// End-of-speech detection ("endpointing") for hands-free listening: decides
// when someone has started talking and when they have stopped, from the mic's
// loudness against the room's own background noise. No model -- a loudness
// test with an adaptive noise floor, which is nearly free on the Stick.
//
// Plain C++ with no Arduino dependency, so tests/test_firmware_vad.py can
// compile and exercise it on the host with g++.
//
// Per 32 ms mic chunk, feed() returns what to do next:
//   WAITING       no speech yet (keep buffering, send nothing)
//   SPEECH_START  speech confirmed: start the turn, send the buffered pre-roll
//   CONTINUE      mid-utterance (a short pause counts as mid-utterance)
//   END           speech then HANGOVER_MS of quiet: stop, send the turn
//   NO_SPEECH     nothing said within NO_SPEECH_MS: cancel, nothing was sent
//   MAX_LENGTH    MAX_MS reached (a TV, a noisy room): stop anyway
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <math.h>

namespace Vad {

enum Decision { WAITING, SPEECH_START, CONTINUE, END, NO_SPEECH, MAX_LENGTH };

struct Config {
  uint32_t chunkMs = 32;          // 512 samples at 16 kHz
  float speechRatio = 3.2f;       // speech = louder than 3.2x the noise floor (~+10 dB)
  float minSpeechRms = 250.0f;    // ...and at least this loud in absolute terms
  float floorMin = 20.0f;         // a dead-silent mic must not make every click "speech"
  uint32_t onsetChunks = 3;       // ~100 ms of consecutive speech to count as started
  uint32_t calibrateChunks = 4;   // first ~130 ms seeds the noise floor
  uint32_t hangoverMs = 800;      // quiet this long after speech ends the turn
  uint32_t noSpeechMs = 5000;     // give up if nothing is said in this long
  uint32_t maxMs = 20000;         // hard cap on one utterance
};

// Root mean square of 16-bit PCM, in raw sample units (0..32767).
inline float rms(const int16_t *pcm, size_t n) {
  if (n == 0) return 0.0f;
  double sumsq = 0;
  for (size_t i = 0; i < n; i++) sumsq += (double)pcm[i] * pcm[i];
  return (float)sqrt(sumsq / n);
}

class Endpointer {
 public:
  explicit Endpointer(const Config &cfg = Config()) : cfg_(cfg) { reset(); }

  void reset() {
    chunks_ = 0;
    floor_ = 0.0f;
    calibSum_ = 0.0f;
    speechRun_ = 0;
    quietMs_ = 0;
    started_ = false;
  }

  bool started() const { return started_; }
  float noiseFloor() const { return floor_; }
  float lastRms() const { return last_; }

  Decision feed(const int16_t *pcm, size_t n) { return feedRms(rms(pcm, n)); }

  // The decision logic, on a precomputed loudness (what the tests drive).
  Decision feedRms(float level) {
    last_ = level;
    chunks_++;
    uint32_t elapsed = chunks_ * cfg_.chunkMs;

    if (chunks_ <= cfg_.calibrateChunks) {  // learn the room before judging it
      calibSum_ += level;
      floor_ = calibSum_ / chunks_;
      if (floor_ < cfg_.floorMin) floor_ = cfg_.floorMin;
      return started_ ? CONTINUE : WAITING;
    }

    bool speech = level > floor_ * cfg_.speechRatio && level > cfg_.minSpeechRms;
    if (!speech) {
      // Track the background slowly, only on non-speech, so talking never
      // raises the bar it is measured against.
      floor_ = floor_ * 0.95f + level * 0.05f;
      if (floor_ < cfg_.floorMin) floor_ = cfg_.floorMin;
    }

    if (!started_) {
      speechRun_ = speech ? speechRun_ + 1 : 0;
      if (speechRun_ >= cfg_.onsetChunks) {
        started_ = true;
        quietMs_ = 0;
        return SPEECH_START;
      }
      return elapsed >= cfg_.noSpeechMs ? NO_SPEECH : WAITING;
    }

    if (elapsed >= cfg_.maxMs) return MAX_LENGTH;
    quietMs_ = speech ? 0 : quietMs_ + cfg_.chunkMs;
    return quietMs_ >= cfg_.hangoverMs ? END : CONTINUE;
  }

 private:
  Config cfg_;
  uint32_t chunks_ = 0, speechRun_ = 0, quietMs_ = 0;
  float floor_ = 0.0f, calibSum_ = 0.0f, last_ = 0.0f;
  bool started_ = false;
};

}  // namespace Vad
