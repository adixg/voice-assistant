#!/usr/bin/env bash
# Positive "Rina-chan" clips (several spellings, speeds, voices) and
# similar-sounding negatives, generated on CPU with Piper (libritts_r, 904 voices).
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
export PYTHONPATH=$PWD/piper-sample-generator CUDA_VISIBLE_DEVICES=""
MODEL=piper-sample-generator/models/en_US-libritts_r-medium.pt
gen() {  # text count outdir
  local tmp; tmp=$(mktemp -d -p .)
  python -m piper_sample_generator "$1" --model $MODEL --max-samples "$2" --batch-size 100 \
      --max-speakers 800 --length-scales 0.85 1.0 1.15 --noise-scales 0.667 0.9 \
      --noise-scale-ws 0.8 1.0 --output-dir "$tmp" >/dev/null 2>&1
  local slug; slug=$(echo "$1" | tr ' A-Z-' '__a-z_')
  mkdir -p "$3"
  for f in "$tmp"/*.wav; do mv "$f" "$3/${slug}_$(basename "$f")"; done
  rm -rf "$tmp"
  echo "$(date +%T) $1: $2 -> $3"
}
gen "Rina chahn" 4000 positives
gen "Rina chan"  3000 positives
gen "Rina-chan"  2000 positives
gen "Rina-chahn" 1000 positives
for w in "Tina chan" "Nina chan" "Rina can" "Reading" "Lena" "arena" "Regina" "Rina" \
         "chan" "Tina" "Katrina" "Rihanna" "Marina" "Serena" "Gina" "Christina" "chance" "Ina chan"; do
  gen "$w" 200 adversarial
done
echo done
