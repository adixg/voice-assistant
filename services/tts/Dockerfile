# Build from the repo root:
#   docker build -f services/tts/Dockerfile --build-arg REQUIREMENTS_FILE=chatterbox.txt -t aicompanion-tts .
#   docker build -f services/tts/Dockerfile --build-arg REQUIREMENTS_FILE=uma-tts.txt -t aicompanion-tts-vits .
#
# CUDA runtime base: chatterbox needs a real GPU (see docs/hardware-budget.md
# for measured VRAM -- Nano-on-CUDA is 1857 MiB, the bridge's default). vits
# is CPU-only and free on VRAM but can still run in this same image.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ARG REQUIREMENTS_FILE=chatterbox.txt
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ffmpeg espeak-ng git git-lfs && rm -rf /var/lib/apt/lists/*
# The base image's apt-installed pip is old enough to mis-handle the git+
# install below: it discards the built wheel with "produced metadata for
# project name unknown" even though chatterbox's pyproject.toml declares
# `name = "chatterbox-tts"` correctly -- reproduced locally, a current pip
# (26.2.1) installs the identical git URL cleanly, so it's a pip-version
# bug, not a real problem with the dependency.
RUN pip3 install --no-cache-dir --upgrade pip

WORKDIR /app
COPY services/tts/requirements.txt services/tts/requirements.txt
RUN pip3 install --no-cache-dir -r services/tts/requirements.txt

# The backend-specific deps: requirements/chatterbox.txt,
# requirements/uma-tts.txt, or requirements/kokoro.txt, from the repo root.
COPY requirements/${REQUIREMENTS_FILE} requirements/backend.txt
RUN pip3 install --no-cache-dir -r requirements/backend.txt

COPY voicepipe/ voicepipe/
COPY services/tts/app.py services/tts/app.py
COPY services/tts/elevenlabs.py services/tts/elevenlabs.py
COPY services/metrics.py services/metrics.py
COPY services/telemetry.py services/telemetry.py
COPY services/__init__.py services/__init__.py

# Both CLIs are the actual worker command voicepipe/backends/vits.py and
# chatterbox.py spawn as a subprocess (see WorkerVoice in voicepipe/subproc.py)
# -- neither backend does anything without its CLI script physically present.
COPY tts_cli.py tts_cli.py
COPY chatterbox_cli.py chatterbox_cli.py

# vits (requirements/uma-tts.txt) needs the cloned HF Space -- model code
# plus ~1.5G of git-lfs pretrained weights -- sitting next to tts_cli.py; see
# that script's docstring and setup_envs.sh's identical dev-machine clone.
# chatterbox downloads its weights from Hugging Face at first run instead,
# so it needs no build-time clone and this is a no-op for that image.
RUN if [ "$REQUIREMENTS_FILE" = "uma-tts.txt" ]; then \
      git lfs install && \
      git clone https://huggingface.co/spaces/Plachta/VITS-Umamusume-voice-synthesizer; \
    fi

EXPOSE 8003
ENTRYPOINT ["python3", "-m", "services.tts.app"]
CMD ["--tts-backend", "chatterbox", "--chatterbox-nano"]
