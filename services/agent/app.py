"""LLM turn-taking as an HTTP service: a thin FastAPI wrapper around
voicepipe's LLM registry (llama.cpp/OpenAI-compatible, Ollama, and other backends). Same backend-selection
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
from prometheus_client import REGISTRY
from prometheus_client.core import CounterMetricFamily

from services.metrics import install_http_metrics, set_agent_llm_target
from tools.companion_control_mcp import claude_usage_summary, read_claude_usage
from services.telemetry import install_tracing

class ClaudeUsageCollector:
    """ask_claude's usage ledger (tools/companion_control_mcp.py writes it) as
    Prometheus counters, read on each scrape: the ledger is the one record, so
    these survive restarts of either process and can't drift from it."""

    def collect(self):
        try:
            totals = claude_usage_summary(read_claude_usage())
        except Exception:  # noqa: BLE001 - a bad ledger must not break /metrics
            return
        yield CounterMetricFamily("aicompanion_claude_calls", "Questions answered by Claude",
                                  value=totals["calls"])
        tokens = CounterMetricFamily("aicompanion_claude_tokens", "Claude API tokens", labels=["direction"])
        tokens.add_metric(["input"], totals["input_tokens"])
        tokens.add_metric(["output"], totals["output_tokens"])
        yield tokens
        yield CounterMetricFamily("aicompanion_claude_cost_usd",
                                  "Estimated Claude API cost (USD, from token counts and configured prices)",
                                  value=totals["estimated_cost_usd"])


REGISTRY.register(ClaudeUsageCollector())
app = FastAPI(title="aicompanion-agent")
install_http_metrics(app, "agent")
install_tracing(app, "agent")
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
    # Keep the legacy flags too: an already-deployed Ollama-configured agent
    # must remain able to start while its manifest rolls over to llama.cpp.
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                    help=argparse.SUPPRESS)
    ap.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", "rina"),
                    help=argparse.SUPPRESS)
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
    if hasattr(args, "openai_compatible_url"):
        set_agent_llm_target(args.openai_compatible_url, args.openai_compatible_model)
    uvicorn.run(app, host="0.0.0.0", port=args.host_port)


if __name__ == "__main__":
    main()
