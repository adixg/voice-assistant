# Build from the repo root:
#   docker build -f services/gateway/Dockerfile -t aicompanion-gateway .
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/gateway/requirements.txt services/gateway/requirements.txt
RUN pip install --no-cache-dir -r services/gateway/requirements.txt

COPY voicepipe/ voicepipe/
# Bake the speaker-verification model (~24MB) into the image so the gate never
# needs the network at run time. It used to be fetched on the first utterance by
# shelling out to `curl`, which this image does not have, so every utterance
# failed the check and was let through. (The download uses urllib now, but a
# pod restart should not depend on Hugging Face being reachable either.)
ENV VOICEPIPE_CACHE_DIR=/opt/voicepipe
RUN python -c "from voicepipe.backends.wespeaker import ensure_model; print(ensure_model())"
COPY services/gateway/app.py services/gateway/app.py
COPY services/metrics.py services/metrics.py
COPY services/telemetry.py services/telemetry.py
COPY services/__init__.py services/__init__.py

EXPOSE 8000
ENTRYPOINT ["python", "-m", "services.gateway.app"]
CMD ["--stt-url", "http://stt:8001", "--agent-url", "http://agent:8002", "--tts-url", "http://tts:8003"]
