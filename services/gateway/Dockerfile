# Build from the repo root:
#   docker build -f services/gateway/Dockerfile -t aicompanion-gateway .
FROM python:3.12-slim

WORKDIR /app
COPY services/gateway/requirements.txt services/gateway/requirements.txt
RUN pip install --no-cache-dir -r services/gateway/requirements.txt

COPY voicepipe/ voicepipe/
COPY services/gateway/app.py services/gateway/app.py
COPY services/__init__.py services/__init__.py

EXPOSE 8000
ENTRYPOINT ["python", "-m", "services.gateway.app"]
CMD ["--stt-url", "http://stt:8001", "--agent-url", "http://agent:8002", "--tts-url", "http://tts:8003"]
