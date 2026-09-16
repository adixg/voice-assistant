"""LLM turn-taking as an HTTP service: a thin FastAPI wrapper around
voicepipe's LLM registry (ollama/hermes-agent/...). Same backend-selection
contract as services/stt/app.py -- see that file's docstring.

This is the service the GPU scheduler controller (controller/gpu_scheduler/)
is meant to route around: with `--llm-backend ollama --host <url>`, this
container does no GPU work itself, it only proxies to whichever Ollama
instance `--host` points at (see voicepipe/backends/ollama.py) -- the actual
model runs in a separate Ollama Deployment, one per GPU node. So this
service's own container is CPU-only and cheap to run on either node; what
changes per-request is which Ollama endpoint it's told to use.

Run:
    python -m services.agent.app --llm-backend ollama --host http://ollama-rtx4060:11434 --model rina --port 8002
"""
import argparse
import json
import os

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from voicepipe import backends  # noqa: F401 - registers backends as a side effect
from voicepipe.registry import LLM, stream_reply

app = FastAPI(title="aigf-agent")
_backend = None


class AskRequest(BaseModel):
    messages: list[dict]
    think: bool | None = None


class AskResponse(BaseModel):
    reply: str


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--llm-backend", default="ollama", choices=LLM.names())
    # Env default matches voicepipe/cli.py's convention, so the k8s
    # Deployment (or the GPU scheduler controller) can retarget which Ollama
    # instance this talks to with `kubectl set env`, no image rebuild.
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    ap.add_argument("--model", default="rina")
    ap.add_argument("--host-port", type=int, default=8002, dest="host_port")
    LLM.add_arguments(ap)
    return ap


@app.get("/health")
def health():
    return {"status": "ok", "backend": _backend.__class__.__name__ if _backend else None}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    if _backend is None:
        raise HTTPException(503, "backend not ready")
    reply = _backend.ask(req.messages, req.think)
    return AskResponse(reply=reply)


@app.post("/ask_stream")
def ask_stream(req: AskRequest):
    if _backend is None:
        raise HTTPException(503, "backend not ready")

    def events():
        for kind, text in stream_reply(_backend, req.messages, req.think):
            yield json.dumps({"kind": kind, "text": text}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


def main():
    global _backend
    args = build_parser().parse_args()
    _backend = LLM.build(args.llm_backend, args)
    uvicorn.run(app, host="0.0.0.0", port=args.host_port)


if __name__ == "__main__":
    main()
