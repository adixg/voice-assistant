#!/usr/bin/env bash
# One-shot setup for the voice-assistant: two conda envs + the VITS Space.
#
#   ./setup_envs.sh
#
# Creates:
#   chat     - runs chat_loop.py  (faster-whisper STT, ollama client, PySide6 orb)
#   uma-tts  - runs tts_cli.py    (VITS-Umamusume synthesizer, torch / CUDA 12.1)
#
# Clones (once, ~1.5G of git-lfs weights):
#   VITS-Umamusume-voice-synthesizer/   next to this script
#
# Still need, from your system package manager (not pip): ffmpeg, ffplay.
#
# Env overrides:  PY_VER (default 3.10), CHAT_ENV, TTS_ENV.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY_VER="${PY_VER:-3.10}"
CHAT_ENV="${CHAT_ENV:-chat}"
TTS_ENV="${TTS_ENV:-uma-tts}"

command -v conda >/dev/null || { echo "conda not on PATH"; exit 1; }
source "$(conda info --base)/etc/profile.d/conda.sh"

make_env() {
  local name="$1" reqs="$2"
  if conda env list | awk '{print $1}' | grep -qx "$name"; then
    echo "== env '$name' exists, updating from $reqs"
  else
    echo "== creating env '$name' (python $PY_VER)"
    conda create -y -n "$name" "python=$PY_VER"
  fi
  conda run -n "$name" python -m pip install --upgrade pip
  conda run -n "$name" python -m pip install -r "$reqs"
}

make_env "$CHAT_ENV" requirements-chat.txt
make_env "$TTS_ENV"  requirements-uma-tts.txt

# ---- VITS-Umamusume Space (model code + pretrained weights) ----------------
if [ ! -f VITS-Umamusume-voice-synthesizer/pretrained_models/G_trilingual.pth ]; then
  echo "== cloning the VITS-Umamusume Space (git-lfs, ~1.5G)"
  command -v git-lfs >/dev/null || { echo "install git-lfs first"; exit 1; }
  git lfs install
  git clone https://huggingface.co/spaces/Plachta/VITS-Umamusume-voice-synthesizer
else
  echo "== VITS Space already present"
fi

cat <<EOF

done.  smoke tests:
  conda run -n $TTS_ENV  python tts_cli.py --list-speakers -m trilingual | head
  conda run -n $TTS_ENV  python tts_cli.py "Hello there." -m trilingual -l en -s 10 -o /tmp/hi.wav

run it:
  conda activate $CHAT_ENV
  python chat_loop.py --host http://100.70.0.38:11434 --model qwen3:8b
EOF
