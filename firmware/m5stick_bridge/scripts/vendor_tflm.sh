#!/usr/bin/env bash
# Vendors the on-device wake-word runtime into lib/tflm_esp/, from pinned sources:
#   espressif/esp-tflite-micro v1.3.3  TensorFlow Lite Micro for ESP32 chips
#                                       (the version ESPHome's micro_wake_word pins)
#   espressif/esp-nn v1.1.2             the ESP32-S3 optimized int8 kernels it calls
#   puddly/pymicro-features @ PYMICRO_REF
#                                       the audio frontend (TFLM microfrontend), the
#                                       same C code microWakeWord computes features
#                                       with while training
# PlatformIO's Arduino build can't pull ESP-IDF components, so these become a
# local library; scripts/tflm_library.json (copied in as library.json) compiles
# the same sources Espressif's CMakeLists does. Re-run to update. Licenses are
# copied along.
set -euo pipefail
cd "$(dirname "$0")/.."
PYMICRO_REF=${PYMICRO_REF:-02de1b1fb32887aed952b3ac5e0c92d77d3b3166}
DEST=lib/tflm_esp
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

curl -sSL https://github.com/espressif/esp-tflite-micro/archive/refs/tags/v1.3.3.tar.gz | tar xz -C "$TMP"
curl -sSL https://github.com/espressif/esp-nn/archive/refs/tags/v1.1.2.tar.gz | tar xz -C "$TMP"
git clone -q https://github.com/puddly/pymicro-features "$TMP/pymicro-features"
git -C "$TMP/pymicro-features" checkout -q "$PYMICRO_REF"
echo "pymicro-features at $(git -C "$TMP/pymicro-features" rev-parse HEAD)"

TF="$TMP/esp-tflite-micro-1.3.3"
rm -rf "$DEST"
mkdir -p "$DEST/esp-nn"
cp scripts/tflm_library.json "$DEST/library.json"
cp "$TF/LICENSE" "$DEST/LICENSE.tflite-micro"

# The runtime's headers and sources; tests, examples and tooling are dropped.
(cd "$TF" && find tensorflow third_party -type f \
    \( -name '*.h' -o -name '*.hpp' -o -name '*.c' -o -name '*.cc' -o -name 'LICENSE*' -o -name 'COPYING' \) \
    ! -path '*/test*' ! -name '*_test.cc' ! -path '*/examples/*' ! -path '*/tools/*' \
    ! -path '*/python/*' ! -path '*/benchmarks/*' ! -path '*/testing/*' \
    | tar -cf - -T -) | tar -xf - -C "$DEST"
# micro_ops.h includes the signal library's kernel headers (its ops aren't
# registered here, so only the headers are needed).
(cd "$TF" && find signal -type f -name '*.h' | tar -cf - -T -) | tar -xf - -C "$DEST"
# kiss_fftr lives under third_party/kissfft/tools/ and the frontend needs it.
mkdir -p "$DEST/third_party/kissfft/tools"
cp "$TF"/third_party/kissfft/tools/kiss_fftr.* "$DEST/third_party/kissfft/tools/"

NN="$TMP/esp-nn-1.1.2"
cp -r "$NN/include" "$NN/src" "$NN/LICENSE" "$DEST/esp-nn/"

FE_SRC="$TMP/pymicro-features/tensorflow/lite/experimental/microfrontend/lib"
FE="$DEST/tensorflow/lite/experimental/microfrontend/lib"
mkdir -p "$FE"
cp "$FE_SRC"/*.h "$FE/"
for f in fft.cc fft_util.cc filterbank.c filterbank_util.c frontend.c frontend_util.c \
         kiss_fft_int16.cc log_lut.c log_scale.c log_scale_util.c noise_reduction.c \
         noise_reduction_util.c pcan_gain_control.c pcan_gain_control_util.c window.c window_util.c; do
    cp "$FE_SRC/$f" "$FE/"
done
cp "$TMP/pymicro-features/LICENSE" "$DEST/LICENSE.pymicro-features"
du -sh "$DEST"
