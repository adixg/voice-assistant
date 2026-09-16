"""Gateway: the M5StickS3's WebSocket peer, ported out of bridge_server.py.

*** Phase 1 skeleton -- proves the split pipeline end-to-end, not yet a
drop-in replacement for bridge_server.py. *** It accepts the firmware's
audio-turn message, calls out to the stt/agent/tts services over HTTP
instead of importing backends in-process, and returns a reply. It does
*not* yet port over bridge_server.py's speaker-verification gate,
encouragement loop, proactive `announce`, or the iter_in_thread()/
call_in_thread() event-loop-must-never-block handling -- see
docs/deployment-architecture.md for the phase plan and bridge_server.py for
the reference behavior each of those needs to be ported from. Until that
port is done, bridge_server.py remains the one actually flashed against in
firmware/m5stick_bridge/, and this gateway is dev/integration-tested against
tools/echo_server.py-style clients, not the real Stick.

Run:
    python -m services.gateway.app --stt-url http://stt:8001 \\
        --agent-url http://agent:8002 --tts-url http://tts:8003 --port 8000
"""
import argparse
import json

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI(title="aicompanion-gateway")
_urls = {}


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stt-url", default="http://localhost:8001")
    ap.add_argument("--agent-url", default="http://localhost:8002")
    ap.add_argument("--tts-url", default="http://localhost:8003")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    return ap


@app.get("/health")
def health():
    return {"status": "ok", "downstream": _urls}


async def run_turn(client: httpx.AsyncClient, wav_bytes: bytes, messages: list) -> dict:
    """STT -> agent -> TTS for one turn. Returns the reply text plus the
    synthesized audio chunks (base64), same shape services/tts/app.py hands
    back -- the firmware-protocol framing of these into the wire message the
    Stick expects is part of the Phase 2 port, not this skeleton."""
    stt_resp = await client.post(f"{_urls['stt']}/transcribe",
                                  files={"audio": ("turn.wav", wav_bytes, "audio/wav")})
    stt_resp.raise_for_status()
    text = stt_resp.json()["text"]

    messages = messages + [{"role": "user", "content": text}]
    agent_resp = await client.post(f"{_urls['agent']}/ask", json={"messages": messages})
    agent_resp.raise_for_status()
    reply = agent_resp.json()["reply"]

    tts_resp = await client.post(f"{_urls['tts']}/synth", json={"text": reply})
    tts_resp.raise_for_status()
    chunks_b64 = tts_resp.json()["chunks_b64"]

    return {"heard": text, "reply": reply, "chunks_b64": chunks_b64}


@app.websocket("/turn")
async def turn_ws(ws: WebSocket):
    """Minimal integration-test endpoint: client sends raw wav bytes, gets
    back the JSON turn result. Not the firmware's actual wire protocol."""
    await ws.accept()
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            while True:
                wav_bytes = await ws.receive_bytes()
                result = await run_turn(client, wav_bytes, messages=[])
                await ws.send_text(json.dumps(result))
        except WebSocketDisconnect:
            pass


def main():
    global _urls
    args = build_parser().parse_args()
    _urls = {"stt": args.stt_url, "agent": args.agent_url, "tts": args.tts_url}
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
