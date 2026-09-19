"""STT as an HTTP service: a thin FastAPI wrapper around voicepipe's STT
registry, so faster-whisper runs in its own container/pod instead of inline
in bridge_server.py.

Backend choice and its own options (model size, device, ...) are still
ordinary CLI flags, via the same `add_arguments`/`from_args` contract every
other entrypoint in this repo uses (see voicepipe/registry.py) -- this
service adds no new configuration mechanism, it just serves one registry
entry over HTTP instead of calling it in-process. A Kubernetes Deployment
passes these the normal way, as `args:` on the container.

Run:
    python -m services.stt.app --stt-backend faster-whisper --model small --port 8001
"""
import argparse
import os
import tempfile

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from pydantic import BaseModel

from voicepipe import backends  # noqa: F401 - registers backends as a side effect
from voicepipe.registry import STT
from services.metrics import install_http_metrics
from services.telemetry import install_tracing

app = FastAPI(title="aicompanion-stt")
install_http_metrics(app, "stt")
install_tracing(app, "stt")
_backend = None


class TranscribeResponse(BaseModel):
    text: str


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stt-backend", default="faster-whisper", choices=STT.names())
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8001)
    STT.add_arguments(ap)
    return ap


@app.get("/health")
def health():
    return {"status": "ok", "backend": _backend.__class__.__name__ if _backend else None}


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(audio: UploadFile, lang: str | None = None):
    if _backend is None:
        raise HTTPException(503, "backend not ready")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name
    try:
        text = _backend.transcribe(tmp_path, lang)
    finally:
        os.unlink(tmp_path)
    return TranscribeResponse(text=text)


def main():
    global _backend
    args = build_parser().parse_args()
    _backend = STT.build(args.stt_backend, args)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
