"""LLM turn-taking as an HTTP service: a thin FastAPI wrapper around
voicepipe's LLM registry (llama.cpp/OpenAI-compatible, Ollama, Hermes...). Same backend-selection
contract as services/stt/app.py -- see that file's docstring.

This is the service the GPU scheduler controller (controller/gpu_scheduler/)
is meant to route around: with `--llm-backend openai-compatible`, this
container does no GPU work itself, it only proxies to whichever llama-server
instance its URL points at -- the actual model runs in a separate Deployment,
one per GPU node. So this
service's own container is CPU-only and cheap to run on either node; what
changes per-request is which Ollama endpoint it's told to use.

Run:
    python -m services.agent.app --llm-backend openai-compatible \
        --openai-compatible-url http://llama-cpp-rtx4060:8080/v1 \
        --openai-compatible-model qwen3-8b --host-port 8002
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

app = FastAPI(title="aicompanion-agent")
_backend = None


class AskRequest(BaseModel):
    messages: list[dict]
    think: bool | None = None


class AskResponse(BaseModel):
    reply: str


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--llm-backend", default="openai-compatible", choices=LLM.names())
    # The openai-compatible backend reads LLM_HOST and LLM_MODEL itself. The
    # controller patches those neutral variables together on a node switch.
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
