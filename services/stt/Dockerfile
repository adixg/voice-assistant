# Build from the repo root so this can COPY voicepipe/:
#   docker build -f services/stt/Dockerfile -t aicompanion-stt .
#
# CUDA runtime base because faster-whisper (ctranslate2) dlopen()s cuBLAS/
# cuDNN when run with --device cuda; harmless if the pod ends up on a
# CPU-only node and told --device cpu instead (see hardware budget numbers
# in docs/hardware-budget.md for which device size makes sense where).
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --upgrade pip

WORKDIR /app
COPY services/stt/requirements.txt services/stt/requirements.txt
RUN pip3 install --no-cache-dir -r services/stt/requirements.txt

COPY voicepipe/ voicepipe/
COPY services/stt/app.py services/stt/app.py
COPY services/__init__.py services/__init__.py

EXPOSE 8001
ENTRYPOINT ["python3", "-m", "services.stt.app"]
CMD ["--stt-backend", "faster-whisper", "--model", "small"]
