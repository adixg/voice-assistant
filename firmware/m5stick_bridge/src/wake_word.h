// On-device wake word ("Rina-chan"): a microWakeWord streaming model run by
// TensorFlow Lite Micro on the Stick itself, on the idle microphone. Nothing
// leaves the Stick until it fires; then main.cpp starts the same hands-free
// turn a BtnB hold does (vad.h ends it).
//
// This mirrors ESPHome's micro_wake_word component, which runs the same kind
// of model on the same chip (esphome/components/micro_wake_word):
//   - features: the TFLM microfrontend with microWakeWord's settings (40
//     channels, 30 ms window, 10 ms step), the same C code the model was
//     trained on (pymicro-features), converted to int8 the way ESPHome does;
//   - the model takes `stride` feature frames (dims[1] of its input) per
//     inference and keeps its own state in resource variables;
//   - it fires when the mean of the last SLIDING_WINDOW probabilities is over
//     the cutoff, and ignores the first MIN_SLICES_BEFORE_DETECTION slices
//     after a (re)start so a detection can't immediately repeat.
//
// The runtime is vendored in lib/tflm_esp (scripts/vendor_tflm.sh); the model
// is compiled in from wake_word_model.h (scripts/tflite_to_header.py).
#pragma once
#include <Arduino.h>
#include <esp_heap_caps.h>

#include "tensorflow/lite/experimental/microfrontend/lib/frontend.h"
#include "tensorflow/lite/experimental/microfrontend/lib/frontend_util.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "wake_word_model.h"

namespace WakeWord {

static const int FEATURE_SIZE = 40;
static const int MIN_SLICES_BEFORE_DETECTION = 100;
static const size_t VARIABLE_ARENA_SIZE = 1024;
static const int MAX_WINDOW = 10;

static FrontendConfig frontendConfig;
static FrontendState frontendState;
static tflite::MicroMutableOpResolver<20> resolver;
static tflite::MicroInterpreter *interpreter = nullptr;
static uint8_t *tensorArena = nullptr;
static uint8_t *variableArena = nullptr;
static bool ready = false;

static uint8_t probs[MAX_WINDOW];
static int probIndex = 0;
static int ignoreSlices = -MIN_SLICES_BEFORE_DETECTION;
static int strideStep = 0;
static uint8_t lastProbability = 0;
static uint32_t lastInvokeUs = 0;

static void reset() {
  memset(probs, 0, sizeof(probs));
  ignoreSlices = -MIN_SLICES_BEFORE_DETECTION;
  strideStep = 0;
}

static bool begin() {
  frontendConfig.window.size_ms = 30;
  frontendConfig.window.step_size_ms = WAKE_WORD_FEATURE_STEP_MS;
  frontendConfig.filterbank.num_channels = FEATURE_SIZE;
  frontendConfig.filterbank.lower_band_limit = 125.0;
  frontendConfig.filterbank.upper_band_limit = 7500.0;
  frontendConfig.noise_reduction.smoothing_bits = 10;
  frontendConfig.noise_reduction.even_smoothing = 0.025;
  frontendConfig.noise_reduction.odd_smoothing = 0.06;
  frontendConfig.noise_reduction.min_signal_remaining = 0.05;
  frontendConfig.pcan_gain_control.enable_pcan = 1;
  frontendConfig.pcan_gain_control.strength = 0.95;
  frontendConfig.pcan_gain_control.offset = 80.0;
  frontendConfig.pcan_gain_control.gain_bits = 21;
  frontendConfig.log_scale.enable_log = 1;
  frontendConfig.log_scale.scale_shift = 6;
  if (!FrontendPopulateState(&frontendConfig, &frontendState, 16000)) {
    Serial.println("[wake] frontend allocation failed");
    return false;
  }

  bool ok = resolver.AddCallOnce() == kTfLiteOk && resolver.AddVarHandle() == kTfLiteOk &&
            resolver.AddReshape() == kTfLiteOk && resolver.AddReadVariable() == kTfLiteOk &&
            resolver.AddStridedSlice() == kTfLiteOk && resolver.AddConcatenation() == kTfLiteOk &&
            resolver.AddAssignVariable() == kTfLiteOk && resolver.AddConv2D() == kTfLiteOk &&
            resolver.AddMul() == kTfLiteOk && resolver.AddAdd() == kTfLiteOk &&
            resolver.AddMean() == kTfLiteOk && resolver.AddFullyConnected() == kTfLiteOk &&
            resolver.AddLogistic() == kTfLiteOk && resolver.AddQuantize() == kTfLiteOk &&
            resolver.AddDepthwiseConv2D() == kTfLiteOk && resolver.AddAveragePool2D() == kTfLiteOk &&
            resolver.AddMaxPool2D() == kTfLiteOk && resolver.AddPad() == kTfLiteOk &&
            resolver.AddPack() == kTfLiteOk && resolver.AddSplitV() == kTfLiteOk;
  if (!ok) {
    Serial.println("[wake] op registration failed");
    return false;
  }

  // Internal RAM: the model runs every 30 ms, and PSRAM would slow every read.
  size_t arenaSize = (WAKE_WORD_TENSOR_ARENA_SIZE + 15) & ~15;
  tensorArena = (uint8_t *)heap_caps_aligned_alloc(16, arenaSize, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  variableArena = (uint8_t *)heap_caps_aligned_alloc(16, VARIABLE_ARENA_SIZE, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (!tensorArena || !variableArena) {
    Serial.println("[wake] arena allocation failed");
    return false;
  }
  tflite::MicroAllocator *varAllocator = tflite::MicroAllocator::Create(variableArena, VARIABLE_ARENA_SIZE);
  tflite::MicroResourceVariables *variables = tflite::MicroResourceVariables::Create(varAllocator, 20);
  static tflite::MicroInterpreter staticInterpreter(tflite::GetModel(wake_word_model), resolver,
                                                    tensorArena, arenaSize, variables);
  interpreter = &staticInterpreter;
  if (interpreter->AllocateTensors() != kTfLiteOk) {
    Serial.println("[wake] AllocateTensors failed (tensor arena too small?)");
    interpreter = nullptr;
    return false;
  }
  TfLiteTensor *input = interpreter->input(0);
  if (input->dims->size < 3 || input->dims->data[1] < 1 || input->dims->data[2] != FEATURE_SIZE ||
      input->type != kTfLiteInt8) {
    Serial.println("[wake] unexpected model input shape; wake word disabled");
    interpreter = nullptr;
    return false;
  }
  Serial.printf("[wake] \"%s\" ready: input %dx%dx%d, arena %u used of %u, cutoff %u/255, window %d\n",
                WAKE_WORD_NAME, input->dims->data[0], input->dims->data[1], input->dims->data[2],
                (unsigned)interpreter->arena_used_bytes(), (unsigned)arenaSize,
                (unsigned)WAKE_WORD_PROBABILITY_CUTOFF, WAKE_WORD_SLIDING_WINDOW);
  reset();
  ready = true;
  return true;
}

// One feature frame into the model; true when the wake word fired.
static bool pushFeature(const int8_t features[FEATURE_SIZE]) {
  TfLiteTensor *input = interpreter->input(0);
  int stride = input->dims->data[1];
  strideStep %= stride;
  memcpy(tflite::GetTensorData<int8_t>(input) + FEATURE_SIZE * strideStep, features, FEATURE_SIZE);
  if (++strideStep < stride) return false;

  uint32_t t0 = micros();
  if (interpreter->Invoke() != kTfLiteOk) {
    Serial.println("[wake] invoke failed");
    return false;
  }
  lastInvokeUs = micros() - t0;
  lastProbability = interpreter->output(0)->data.uint8[0];
  probIndex = (probIndex + 1) % WAKE_WORD_SLIDING_WINDOW;
  probs[probIndex] = lastProbability;
  if (lastProbability < WAKE_WORD_PROBABILITY_CUTOFF) ignoreSlices = min(ignoreSlices + 1, 0);

  uint32_t sum = 0;
  for (int i = 0; i < WAKE_WORD_SLIDING_WINDOW; i++) sum += probs[i];

  // Near-miss log for tuning the cutoff: every ~2 s (66 inferences of 30 ms),
  // the highest single and windowed-mean probability seen, if it got to 25%.
  static int sinceReport = 0;
  static uint8_t peakProb = 0, peakMean = 0;
  peakProb = max(peakProb, lastProbability);
  peakMean = max(peakMean, (uint8_t)(sum / WAKE_WORD_SLIDING_WINDOW));
  if (++sinceReport >= 66) {
    if (peakProb >= 64) {
      Serial.printf("[wake] peak %u/255, mean-of-%d %u/255 (cutoff %u), invoke %lu us\n", peakProb,
                    WAKE_WORD_SLIDING_WINDOW, peakMean, (unsigned)WAKE_WORD_PROBABILITY_CUTOFF,
                    (unsigned long)lastInvokeUs);
    }
    sinceReport = 0;
    peakProb = peakMean = 0;
  }
  if (ignoreSlices < 0) return false;
  if (sum > (uint32_t)WAKE_WORD_PROBABILITY_CUTOFF * WAKE_WORD_SLIDING_WINDOW) {
    Serial.printf("[wake] \"%s\" detected (mean %u/255, last invoke %lu us)\n", WAKE_WORD_NAME,
                  (unsigned)(sum / WAKE_WORD_SLIDING_WINDOW), (unsigned long)lastInvokeUs);
    reset();
    return true;
  }
  return false;
}

// Feed 16 kHz mono samples from the idle microphone; true when it fired.
static bool feed(const int16_t *samples, size_t count) {
  if (!ready) return false;
  bool fired = false;
  while (count > 0) {
    size_t used = 0;
    FrontendOutput out = FrontendProcessSamples(&frontendState, samples, count, &used);
    samples += used;
    count -= used;
    if (out.size == FEATURE_SIZE) {
      int8_t features[FEATURE_SIZE];
      for (int i = 0; i < FEATURE_SIZE; i++) {
        // ESPHome's int8 conversion of the frontend's ~0-670 output:
        // ((value / 25.6) / 26.0) * 256 - 128, in integer math.
        int32_t v = ((int32_t)out.values[i] * 256 + 333) / 666 - 128;
        features[i] = (int8_t)constrain(v, -128, 127);
      }
      if (pushFeature(features)) fired = true;
    }
    if (used == 0) break;
  }
  return fired;
}

}  // namespace WakeWord
