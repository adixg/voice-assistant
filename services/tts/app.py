"""TTS as an HTTP service: a thin FastAPI wrapper around voicepipe's TTS
registry (chatterbox/vits/...), so the model lives in its own container/pod
instead of inline in bridge_server.py. Same backend-selection contract as
services/stt/app.py -- see that file's docstring.

`synth()` returns a list of wav chunk paths (long replies are chunked so
playback can start before the whole reply is synthesized); this service
returns those chunks as a JSON list of base64-encoded wav bytes, in order.

Run:
    python -m services.tts.app --tts-backend chatterbox --chatterbox-nano --port 8003
"""
import argparse
import base64

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from voicepipe import backends  # noqa: F401 - registers backends as a side effect
from voicepipe.registry import TTS
from services.metrics import install_http_metrics
from services.telemetry import install_tracing
from services.tts.elevenlabs import ElevenLabsTTS

app = FastAPI(title="aicompanion-tts")
install_http_metrics(app, "tts")
install_tracing(app, "tts")
_backend = None


class SynthRequest(BaseModel):
    text: str


class SynthResponse(BaseModel):
    chunks_b64: list[str]


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tts-backend", default="chatterbox", choices=[*TTS.names(), "elevenlabs"])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8003)
    TTS.add_arguments(ap)
    return ap


@app.get("/health")
def health():
    return {"status": "ok", "backend": _backend.__class__.__name__ if _backend else None}


@app.post("/synth", response_model=SynthResponse)
def synth(req: SynthRequest):
    if _backend is None:
        raise HTTPException(503, "backend not ready")
    chunk_paths = _backend.synth(req.text)
    chunks_b64 = []
    for path in chunk_paths:
        with open(path, "rb") as f:
            chunks_b64.append(base64.b64encode(f.read()).decode("ascii"))
    return SynthResponse(chunks_b64=chunks_b64)


def main():
    global _backend
    args = build_parser().parse_args()
    _backend = (ElevenLabsTTS.from_environment() if args.tts_backend == "elevenlabs"
                else TTS.build(args.tts_backend, args))
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        _backend.close()


if __name__ == "__main__":
    main()
