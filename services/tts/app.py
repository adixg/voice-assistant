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
from services.tts.voices import BACKEND_ENGINE, VoiceError, VoiceSwitcher

app = FastAPI(title="aicompanion-tts")
install_http_metrics(app, "tts")
install_tracing(app, "tts")
_backend = None
# Set when the backend is one the voice can be switched on (kitten or
# kokoro-onnx): synthesis then goes through it, and /voice changes it.
_voices = None


class SynthRequest(BaseModel):
    text: str


class VoiceRequest(BaseModel):
    voice: str
    engine: str | None = None
    speed: float | None = None


class SynthResponse(BaseModel):
    chunks_b64: list[str]


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tts-backend", default="chatterbox", choices=[*TTS.names(), "elevenlabs"])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8003)
    ap.add_argument("--voice-state", default=None, metavar="FILE",
                    help="save the voice picked through POST /voice here, and start with it "
                         "(kitten/kokoro-onnx only); without it a switch lasts until restart")
    TTS.add_arguments(ap)
    return ap


# Which registry backend this process runs and with which options, set in
# main(). /health reports it so a benchmark can record what it measured
# (benchmarks/pipeline/bench_turn.py) rather than assume.
_config = None


@app.get("/health")
def health():
    current = _voices.backend if _voices else _backend
    body = {"status": "ok", "backend": current.__class__.__name__ if current else None}
    if _config:
        body["config"] = _config
    if _voices:
        body["voice"] = _voices.current()
    return body


@app.get("/voices")
def voices():
    if _voices is None:
        raise HTTPException(409, "this backend's voice can't be switched at runtime")
    return _voices.catalog()


@app.post("/voice")
def set_voice(req: VoiceRequest):
    if _voices is None:
        raise HTTPException(409, "this backend's voice can't be switched at runtime")
    try:
        chosen = _voices.switch(req.voice, req.engine, req.speed)
    except VoiceError as e:
        raise HTTPException(422, str(e)) from e
    print(f"  voice -> {chosen}", flush=True)
    return chosen


@app.post("/synth", response_model=SynthResponse)
def synth(req: SynthRequest):
    if _backend is None:
        raise HTTPException(503, "backend not ready")
    chunk_paths = _voices.synth(req.text) if _voices else _backend.synth(req.text)
    chunks_b64 = []
    for path in chunk_paths:
        with open(path, "rb") as f:
            chunks_b64.append(base64.b64encode(f.read()).decode("ascii"))
    return SynthResponse(chunks_b64=chunks_b64)


def main():
    global _backend, _config, _voices
    args = build_parser().parse_args()
    _backend = (ElevenLabsTTS.from_environment() if args.tts_backend == "elevenlabs"
                else TTS.build(args.tts_backend, args))
    _config = {"name": args.tts_backend,
               "options": {} if args.tts_backend == "elevenlabs" else TTS.options(args.tts_backend, args)}
    engine = BACKEND_ENGINE.get(args.tts_backend)
    if engine:
        prefix = args.tts_backend.replace("-", "_")
        _voices = VoiceSwitcher(_backend, engine, getattr(args, f"{prefix}_voice"),
                                getattr(args, f"{prefix}_speed"), args.voice_state)
        _voices.restore()
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        (_voices or _backend).close()


if __name__ == "__main__":
    main()
