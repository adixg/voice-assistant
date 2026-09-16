# docker build -f controller/gpu_scheduler/Dockerfile -t aicompanion-gpu-scheduler controller/gpu_scheduler
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY controller.py .

CMD ["kopf", "run", "controller.py", "--namespace=aicompanion"]
