# Build from the repo root:
#   docker build -f services/agent/Dockerfile -t aicompanion-agent .
#
# CPU-only on purpose: with --llm-backend openai-compatible this container is
# a thin HTTP client (see voicepipe/backends/openai_compatible.py) -- the
# model itself runs in a separate llama.cpp Deployment on whichever GPU node
# the gateway is told to
# use (see controller/gpu_scheduler/). No CUDA base needed here.
FROM python:3.12-slim

WORKDIR /app
COPY services/agent/requirements.txt services/agent/requirements.txt
RUN pip install --no-cache-dir -r services/agent/requirements.txt

COPY voicepipe/ voicepipe/
COPY services/agent/app.py services/agent/app.py
COPY services/__init__.py services/__init__.py

EXPOSE 8002
ENTRYPOINT ["python", "-m", "services.agent.app"]
CMD ["--llm-backend", "openai-compatible"]
