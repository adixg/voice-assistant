# Built from the repo root: see .github/workflows/ci.yml ("gpu-exporter").
FROM python:3.12-slim
RUN pip install --no-cache-dir nvidia-ml-py==13.610.43 prometheus-client==0.22.1
COPY services/gpu_exporter/exporter.py /app/exporter.py
USER 65534
ENTRYPOINT ["python", "-u", "/app/exporter.py"]
