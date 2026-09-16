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
    python3 python3-pip ffmpeg git && rm -rf /var/lib/apt/lists/*
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

# The backend-specific deps: requirements/chatterbox.txt or
# requirements/uma-tts.txt, from the repo root (see README.md's Layout).
COPY requirements/${REQUIREMENTS_FILE} requirements/backend.txt
RUN pip3 install --no-cache-dir -r requirements/backend.txt

COPY voicepipe/ voicepipe/
COPY services/tts/app.py services/tts/app.py
COPY services/__init__.py services/__init__.py

EXPOSE 8003
ENTRYPOINT ["python3", "-m", "services.tts.app"]
CMD ["--tts-backend", "chatterbox", "--chatterbox-nano"]
