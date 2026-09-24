"""Gateway: the M5StickS3's WebSocket peer, ported out of bridge_server.py.

Speaks the same wire protocol bridge_server.py does (one persistent WS
connection, PCM16 mono @ 16kHz both ways):
  Stick -> server: text "start", then binary mic PCM chunks, then text "stop";
                   or text "reset" at any time, to clear conversation history
  server -> Stick: text "heard:<transcript>" as soon as STT finishes, then
                   zero or more "status:<what it's doing>" while an agent runs
                   tools, then text "reply:<text>", then binary reply PCM
                   chunks, then text "end"

Every turn ends with exactly one "end", including failures.

Unlike bridge_server.py, STT/LLM/TTS run as separate HTTP calls to the
stt/agent/tts services instead of in-process -- see docs/deployment-
architecture.md for why. The speaker-verification gate is the one exception:
it's CPU-only and sits in the hot path of every utterance, so it runs
in-process here too, exactly as it does in bridge_server.py, rather than
paying a network round trip for something this cheap.

What's ported from bridge_server.py's Session: the speaker gate,
announce()/the announce socket, encourage_loop, and --persona/--system. What
is NOT ported: --enroll mode. Enrollment only touches the speaker model and a
voiceprint file on disk (see voicepipe/speaker.py) -- run bridge_server.py's
existing `--enroll` mode to build or extend a voiceprint; this gateway reads
the same file (`voicepipe.speaker.Voiceprint.load()`'s default path) and
needs no separate enrollment path of its own.

Run:
    python -m services.gateway.app --stt-url http://stt:8001 \\
        --agent-url http://agent:8002 --tts-url http://tts:8003 --port 8000
"""
import argparse
import asyncio
import base64
import json
import os
import random
import tempfile
import wave
from contextlib import suppress
from time import perf_counter

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from voicepipe import cli, encouragement
from voicepipe.personas import DEFAULT_PERSONA, PERSONAS
from voicepipe.registry import FINAL, STATUS, SV, TOOLS
from voicepipe.speaker import (
    ACCEPTED, CHECK_FAILED, ERROR_POLICIES, ERROR_REJECT, REJECTED, SHORT_ASK, SHORT_POLICIES, TOO_SHORT,
    check_failed_line, rejection_line, too_short_line,
)
from voicepipe.text import sentences, speakable
from voicepipe.utterances import DEFAULT_MAX_KEEP, keep as keep_utterance
from voicepipe.wire_audio import SAMPLE_RATE, SEND_CHUNK, resample_to_pcm16
from services.metrics import (DEVICE_SETTINGS_CHANGES, GATEWAY_STAGE_DURATION, GATEWAY_TURN_DURATION, GATEWAY_TURNS,
                              install_http_metrics, record_speaker_check, set_speaker_gate_state)
from services.telemetry import install_tracing

app = FastAPI(title="aicompanion-gateway")
install_http_metrics(app, "gateway")
install_tracing(app, "gateway")

MIN_UTTERANCE_BYTES = SAMPLE_RATE * 2 // 4  # ignore stray <0.25s blips, same threshold bridge_server.py uses

_urls = {}
_session = None


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stt-url", default="http://localhost:8001")
    ap.add_argument("--agent-url", default="http://localhost:8002")
    ap.add_argument("--tts-url", default="http://localhost:8003")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)

    ap.add_argument("--stt-lang", default="en",
                    help="STT language: en, ja, ... or 'auto' to detect per utterance (default: en)")
    ap.add_argument("--no-voice", action="store_true", help="text only, skip TTS entirely")
    ap.add_argument("--no-normalize", action="store_true",
                    help="send TTS audio to the Stick at its raw level instead of through "
                         "ffmpeg's speech normalizer (see wire_audio.NORMALIZE_FILTER)")
    ap.add_argument("--think", action="store_true",
                    help="let reasoning models (qwen3, r1, ...) do their <think> pass")

    ap.add_argument("--persona", default=DEFAULT_PERSONA, choices=sorted(PERSONAS),
                    help="built-in system prompt: 'partner' (romantic companion) or "
                         f"'assistant' (neutral helper) (default: {DEFAULT_PERSONA})")
    ap.add_argument("--system", default=None,
                    help="override --persona with a custom system prompt; pass --system '' "
                         "to send none at all")
    ap.add_argument("--profile", default=cli.PROFILE_PATH, metavar="ABOUT_ME.MD",
                    help=f"markdown file of facts about you, added to the system prompt "
                         f"(default: {cli.PROFILE_PATH})")
    ap.add_argument("--no-profile", action="store_true",
                    help="ignore the profile file even if it exists")

    # Same flags cli.build_speaker_gate(args) expects -- see that function.
    ap.add_argument("--speaker-backend", default="wespeaker", choices=SV.names(),
                    help="speaker-verification backend (default: wespeaker)")
    ap.add_argument("--speaker-threshold", type=float, default=None,
                    help="cosine score an utterance must beat to be accepted; defaults to "
                         "whatever the chosen backend measured for itself")
    ap.add_argument("--speaker-min-seconds", type=float, default=None,
                    help="below this many seconds an utterance isn't judged at all")
    ap.add_argument("--short-utterances", default=SHORT_ASK, choices=SHORT_POLICIES,
                    help="what to do with a clip too brief to verify: 'ask' (default) or 'allow'")
    ap.add_argument("--speaker-on-error", default=ERROR_REJECT, choices=ERROR_POLICIES,
                    help="when the speaker check itself fails (model missing, embedding crashed): "
                         "'reject' (default) refuses the utterance, 'allow' answers anyone")
    ap.add_argument("--keep-utterances", default=None, metavar="DIR",
                    help="keep the newest utterances (any verdict) in DIR, named with the gate's "
                         "verdict and score, so real Stick audio can be added to the voiceprint "
                         "with tools/voiceprint_add.py; off by default")
    ap.add_argument("--keep-max", type=int, default=DEFAULT_MAX_KEEP,
                    help=f"how many utterances --keep-utterances retains (default: {DEFAULT_MAX_KEEP})")
    ap.add_argument("--no-speaker-check", action="store_true",
                    help="answer anyone, even with a voiceprint enrolled")
    SV.add_arguments(ap)

    ap.add_argument("--announce-socket", default=os.path.join(tempfile.gettempdir(), "gateway-announce.sock"),
                    help="Unix socket that speaks any line written to it, same as "
                         "bridge_server.py's --announce-socket")
    ap.add_argument("--encourage", action="store_true",
                    help="say something encouraging unprompted every so often")
    ap.add_argument("--encourage-interval", type=float, nargs=2, default=(15.0, 20.0),
                    metavar=("MIN", "MAX"), help="minutes between encouragements (default: 15 20)")
    return ap


@app.get("/health")
def health():
    gate = _session.gate if _session else None
    return {"status": "ok", "downstream": _urls,
            # `gate` only says a voiceprint and backend are configured; it stayed
            # true for weeks while the model could not be downloaded and every
            # voice was accepted. `speaker_gate.ready` is whether the checker
            # can actually run.
            "gate": bool(gate and gate.enabled),
            "speaker_gate": gate.status() if gate else None}


class DeviceSettings(BaseModel):
    volume: int | None = Field(None, ge=0, le=255)
    brightness: int | None = Field(None, ge=0, le=255, description="0 turns the screen off")


def _check_control_token(authorization):
    """The device-control API is off unless GATEWAY_CONTROL_TOKEN is set, and
    then needs it as a bearer token: the gateway's port is reachable from the
    tailnet, and only the agent's MCP server should change the Stick."""
    token = os.environ.get("GATEWAY_CONTROL_TOKEN")
    if not token:
        raise HTTPException(503, "device control is disabled (GATEWAY_CONTROL_TOKEN unset)")
    if authorization != f"Bearer {token}":
        raise HTTPException(401, "bad or missing control token")


@app.get("/device/settings")
def get_device_settings(authorization: str | None = Header(None)):
    _check_control_token(authorization)
    if _session is None or _session.ws is None:
        raise HTTPException(503, "no Stick connected")
    if _session.device is None:
        raise HTTPException(503, "the Stick hasn't reported its settings yet")
    return _session.device


@app.post("/device/settings")
async def post_device_settings(body: DeviceSettings, authorization: str | None = Header(None)):
    _check_control_token(authorization)
    if body.volume is None and body.brightness is None:
        raise HTTPException(422, "give volume and/or brightness")
    try:
        applied = await _session.set_device(body.volume, body.brightness)
    except LookupError as e:
        DEVICE_SETTINGS_CHANGES.labels("no_stick").inc()
        raise HTTPException(503, str(e)) from e
    except (asyncio.TimeoutError, TimeoutError) as e:
        DEVICE_SETTINGS_CHANGES.labels("timeout").inc()
        raise HTTPException(504, "the Stick didn't confirm the change") from e
    DEVICE_SETTINGS_CHANGES.labels("applied").inc()
    print(f"  [device] settings changed via control API: {body.model_dump(exclude_none=True)}"
          f" -> volume={applied['volume']} brightness={applied['brightness']}", flush=True)
    return applied


class GatewaySession:
    """One M5StickS3's conversation state -- ported from bridge_server.py's
    Session, adapted to call stt/agent/tts over HTTP instead of in-process.
    Assumes a single Stick, same as the original."""

    def __init__(self, client, urls, gate, args):
        self.client = client
        self.urls = urls
        self.gate = gate
        self.args = args
        self.messages = []
        if args.system:
            self.messages.append({"role": "system", "content": args.system})
        self.wav_in = os.path.join(tempfile.mkdtemp(prefix="gateway-bridge-"), "utterance.wav")
        # The currently connected Stick, or None -- held so announce()/
        # encourage_loop can speak without a turn in progress.
        self.ws = None
        self.rejection_streak = 0
        # Same reasoning as bridge_server.py's Session.speaking: a turn is a
        # reply/audio/end frame sequence, not one message, so an announcement
        # waits for it rather than interleaving PCM into it.
        self.speaking = asyncio.Lock()
        self.normalize = not args.no_normalize
        self.stt_lang = None if args.stt_lang == "auto" else args.stt_lang
        # The Stick's volume/brightness as it last reported them (the relay
        # forwards each SETTINGS report as "settings:V,B,FIRMWARE"), or None.
        self.device = None
        self._device_changed = asyncio.Condition()

    async def on_device_report(self, text):
        """A "settings:V,B[,FIRMWARE]" report from the relay."""
        try:
            volume, brightness, *rest = text.split(",", 2)
            report = {"volume": int(volume), "brightness": int(brightness),
                      "firmware": rest[0] if rest else None}
        except ValueError:
            print(f"  ? malformed settings report: {text!r}", flush=True)
            return
        async with self._device_changed:
            self.device = report
            self._device_changed.notify_all()

    async def set_device(self, volume=None, brightness=None, timeout=5.0):
        """Ask the Stick for new settings and wait for it to report them back.

        Unset fields keep the Stick's current value, so this needs a report to
        have arrived first. Raises LookupError with no Stick (or no report yet)
        and TimeoutError if the Stick never confirms."""
        ws, current = self.ws, self.device
        if ws is None or current is None:
            raise LookupError("no Stick connected" if ws is None else "the Stick hasn't reported its settings yet")
        want = {"volume": current["volume"] if volume is None else volume,
                "brightness": current["brightness"] if brightness is None else brightness}
        await ws.send_text(f"settings:{want['volume']},{want['brightness']}")

        def confirmed():
            d = self.device
            return d is not None and d["volume"] == want["volume"] and d["brightness"] == want["brightness"]

        async with self._device_changed:
            await asyncio.wait_for(self._device_changed.wait_for(confirmed), timeout)
        return self.device

    def reset(self):
        self.messages = [m for m in self.messages if m["role"] == "system"]

    async def handle_utterance(self, ws, pcm):
        if len(pcm) < MIN_UTTERANCE_BYTES:
            print(f"  (ignored {len(pcm) / (SAMPLE_RATE * 2):.2f}s -- under the "
                  f"{MIN_UTTERANCE_BYTES / (SAMPLE_RATE * 2):.2f}s minimum)", flush=True)
            return
        async with self.speaking:
            try:
                await self._turn(ws, pcm)
            except Exception as e:  # noqa: BLE001 - a bad turn must not kill the connection
                print(f"  ! turn failed: {e}")
                await ws.send_text(f"reply:(error: {e})")
            finally:
                await ws.send_text("end")

    async def _say(self, ws, text):
        await ws.send_text(f"reply:{text}")
        await self._speak(ws, text)

    async def _synth(self, text):
        resp = await self.client.post(f"{self.urls['tts']}/synth", json={"text": text})
        resp.raise_for_status()
        return resp.json()["chunks_b64"]

    async def _speak(self, ws, text):
        """TTS `text` over HTTP and stream the resampled PCM to the Stick.

        The reply is spoken a sentence at a time: the first sentence's audio
        goes out as soon as it is synthesized, and the next sentence is
        already being synthesized while that audio is resampled and sent. One
        sentence of lookahead, not all of them at once -- the tts backend runs
        on the same CPU-bound node, so parallel synths would only slow each
        other down.
        """
        if self.args.no_voice:
            return
        text = speakable(text)
        if not text:  # nothing but emoji or markup: nothing to say
            return
        parts = sentences(text)
        started = perf_counter()
        first_audio = True
        pending = asyncio.create_task(self._synth(parts[0]))
        try:
            for i in range(len(parts)):
                chunks_b64 = await pending
                pending = (asyncio.create_task(self._synth(parts[i + 1]))
                           if i + 1 < len(parts) else None)
                for chunk_b64 in chunks_b64:
                    tmp_path = tempfile.mktemp(suffix=".wav")
                    try:
                        with open(tmp_path, "wb") as f:
                            f.write(base64.b64decode(chunk_b64))
                        pcm_out = await resample_to_pcm16(tmp_path, self.normalize)
                    finally:
                        with suppress(FileNotFoundError):
                            os.unlink(tmp_path)
                    for off in range(0, len(pcm_out), SEND_CHUNK):
                        await ws.send_bytes(pcm_out[off:off + SEND_CHUNK])
                        if first_audio:
                            first_audio = False
                            GATEWAY_STAGE_DURATION.labels("first_audio").observe(perf_counter() - started)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending

    async def announce(self, text):
        """Say something unprompted. Returns False when no Stick is connected."""
        if self.ws is None:
            return False
        async with self.speaking:
            ws = self.ws
            if ws is None:  # disconnected while we waited for the turn to end
                return False
            print(f"  (announce) {text}", flush=True)
            await ws.send_text(f"reply:{text}")
            await self._speak(ws, text)
            await ws.send_text("end")
        return True

    async def _ask(self, ws):
        """Stream the agent's reply, forwarding STATUS events to the Stick as
        they arrive. Unlike bridge_server.py's _ask(), this doesn't need
        iter_in_thread()/call_in_thread(): the agent call is already an async
        HTTP request, not an in-process blocking one, so it never holds the
        event loop and can't cause the keepalive-ping-timeout bug those exist
        to work around."""
        reply, tool_messages = "", []
        # Same mapping bridge_server.py's _ask() uses: --think means "let the
        # backend decide" (None), and its absence means "force it off" --
        # not the other way around, since a reasoning model defaults to on.
        think = None if self.args.think else False
        payload = {"messages": self.messages, "think": think}
        async with self.client.stream("POST", f"{self.urls['agent']}/ask_stream", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if event["kind"] == STATUS:
                    print(f"  ... {event['text']}")
                    await ws.send_text(f"status:{event['text']}")
                elif event["kind"] == TOOLS:
                    tool_messages = json.loads(event["text"])
                elif event["kind"] == FINAL:
                    reply = event["text"]
                # DELTA is ignored here, same as bridge_server.py: the Stick
                # gets whole sentences to speak, not a live-typing effect.
        return reply, tool_messages

    async def _turn(self, ws, pcm):
        turn_started = perf_counter()
        outcome = "error"
        try:
            await self._turn_inner(ws, pcm)
            outcome = "success"
        finally:
            GATEWAY_TURNS.labels(outcome).inc()
            GATEWAY_TURN_DURATION.observe(perf_counter() - turn_started)

    async def _turn_inner(self, ws, pcm):
        with wave.open(self.wav_in, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)

        # Before anything expensive, including the network round trip to
        # stt: is this the owner? Same ordering bridge_server.py uses and for
        # the same reason -- a stranger costs one embedding, not a whole turn.
        if self.gate is not None:
            verdict, score = await asyncio.to_thread(self.gate.check, self.wav_in)
            # One line per verdict, whatever it was, so acceptances are on the
            # record too (a scored acceptance used to be the only one that was).
            # An accepted verdict with no score was let through unchecked (gate
            # off, or a short clip allowed by --short-utterances allow).
            label = "unverified" if verdict == ACCEPTED and score is None else verdict
            seconds = len(pcm) / (SAMPLE_RATE * 2)
            streak = f" rejection_streak={self.rejection_streak + 1}" if verdict == REJECTED else ""
            print(f"  speaker verdict={label} score={'-' if score is None else f'{score:.3f}'} "
                  f"threshold={self.gate.threshold} audio={seconds:.2f}s{streak}", flush=True)
            record_speaker_check(label, score, self.gate)
            keep_dir = getattr(self.args, "keep_utterances", None)
            if keep_dir:
                await asyncio.to_thread(keep_utterance, self.wav_in, keep_dir, label, score,
                                        getattr(self.args, "keep_max", DEFAULT_MAX_KEEP))
            if verdict == TOO_SHORT:
                await self._say(ws, too_short_line())
                return
            if verdict == CHECK_FAILED:
                # The check couldn't run, which says nothing about who is
                # speaking: refuse, neutrally, and don't count it as a rejection.
                await self._say(ws, check_failed_line())
                return
            if verdict == REJECTED:
                self.rejection_streak += 1
                await self._say(ws, rejection_line(self.rejection_streak))
                return
            self.rejection_streak = 0

        with open(self.wav_in, "rb") as f:
            wav_bytes = f.read()
        stage_started = perf_counter()
        stt_resp = await self.client.post(
            f"{self.urls['stt']}/transcribe",
            files={"audio": ("turn.wav", wav_bytes, "audio/wav")},
            params={"lang": self.stt_lang} if self.stt_lang else None)
        stt_resp.raise_for_status()
        GATEWAY_STAGE_DURATION.labels("stt").observe(perf_counter() - stage_started)
        text = stt_resp.json()["text"]
        print(f"  you said: {text or '(nothing heard)'}")
        if not text:
            await ws.send_text("reply:(didn't catch that)")
            return

        await ws.send_text(f"heard:{text}")
        self.messages.append({"role": "user", "content": text})
        try:
            stage_started = perf_counter()
            reply, tool_messages = await self._ask(ws)
            GATEWAY_STAGE_DURATION.labels("agent").observe(perf_counter() - stage_started)
        except Exception as e:  # noqa: BLE001
            self.messages.pop()  # don't leave a dangling user turn in the history
            print(f"  ! llm error: {e}")
            await ws.send_text(f"reply:(llm error: {e})")
            return
        # The tool calls go into the history ahead of the reply (registry.TOOLS):
        # without them the model learns that just saying "done" is enough.
        self.messages.extend(tool_messages)
        self.messages.append({"role": "assistant", "content": reply})
        print(f"  Rina: {reply}")

        await ws.send_text(f"reply:{reply}")
        stage_started = perf_counter()
        await self._speak(ws, reply)
        GATEWAY_STAGE_DURATION.labels("tts_and_wire_audio").observe(perf_counter() - stage_started)


class _AsgiWebSocketAdapter:
    """Presents a Starlette WebSocket as the same str/bytes async iterator
    (plus send_text/send_bytes) that GatewaySession and handle_client()
    consume -- mirroring bridge_server.py's `websockets`-library connection
    closely enough that the exact same dispatch logic below works against
    either, and so tests can drive handle_client() with the plain FakeWebSocket
    already shared by every other wire-protocol test (see conftest.py)."""

    def __init__(self, ws):
        self._ws = ws

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self._ws.receive()
        if message["type"] == "websocket.disconnect":
            raise StopAsyncIteration
        if message.get("bytes") is not None:
            return message["bytes"]
        return message.get("text")

    async def send_text(self, text):
        await self._ws.send_text(text)

    async def send_bytes(self, data):
        await self._ws.send_bytes(data)


async def _client_loop(ws, session, turns):
    buf = bytearray()
    recording = False
    async for msg in ws:
        if isinstance(msg, (bytes, bytearray)):
            if recording:
                buf.extend(msg)
            continue
        if msg == "start":
            recording = True
            buf = bytearray()
            print("  [start] recording", flush=True)
        elif msg == "stop":
            recording = False
            print(f"  [stop] {len(buf) / (SAMPLE_RATE * 2):.2f}s of audio", flush=True)
            # In the background, so this loop keeps reading while the turn runs:
            # the agent can change the Stick's settings mid-turn, and the
            # Stick's confirmation arrives on this same socket. Turns still run
            # one at a time, in order, behind session.speaking.
            turns.append(asyncio.create_task(session.handle_utterance(ws, bytes(buf))))
        elif msg == "reset":
            print("  [reset] history cleared", flush=True)
            session.reset()
        elif msg.startswith("settings:"):
            await session.on_device_report(msg[len("settings:"):])
        else:
            print(f"  ? unexpected control message: {msg!r}")


async def handle_client(ws, session):
    print("  Stick connected", flush=True)
    session.ws = ws
    turns = []
    try:
        await _client_loop(ws, session, turns)
    finally:
        # Let turns already started finish (or fail on the closed socket).
        await asyncio.gather(*turns, return_exceptions=True)
        if session.ws is ws:
            session.ws = None
            session.device = None
        print("  Stick disconnected", flush=True)


@app.websocket("/")
@app.websocket("/stick")
async def stick_ws(ws: WebSocket):
    await ws.accept()
    try:
        await handle_client(_AsgiWebSocketAdapter(ws), _session)
    except WebSocketDisconnect:
        pass


async def announce_server(session, path):
    """A Unix socket that speaks whatever line is written to it -- identical
    in shape to bridge_server.py's, so tools/say.py works against either
    server unchanged.

        echo "the build finished" | python tools/say.py --port 8000
    """
    async def handle(reader, writer):
        try:
            line = (await reader.readline()).decode("utf-8", "replace").strip()
            if line:
                spoke = await session.announce(line)
                writer.write(b"ok\n" if spoke else b"no stick connected\n")
                await writer.drain()
        finally:
            writer.close()

    with suppress(FileNotFoundError):
        os.unlink(path)
    server = await asyncio.start_unix_server(handle, path)
    print(f"  announce socket at {path}", flush=True)
    return server


async def encourage_loop(session, low_minutes, high_minutes):
    """Say something encouraging every so often, unprompted -- ported
    unchanged from bridge_server.py's encourage_loop, see that one's
    docstring for the reasoning (random interval, silent when nobody's
    connected, never talks over an in-progress turn)."""
    while True:
        await asyncio.sleep(random.uniform(low_minutes, high_minutes) * 60)
        try:
            if not await session.announce(encouragement.line()):
                print("  (encouragement skipped -- no Stick connected)", flush=True)
        except Exception as e:  # noqa: BLE001 - this task must outlive any one failure
            print(f"  ! encouragement failed: {e}", flush=True)


def main():
    global _urls, _session
    args = cli.parse_args(build_parser())
    _urls = {"stt": args.stt_url, "agent": args.agent_url, "tts": args.tts_url}
    gate = cli.build_speaker_gate(args)
    client = httpx.AsyncClient(timeout=120)
    _session = GatewaySession(client, _urls, gate, args)

    @app.on_event("startup")
    async def _startup():
        # Load the speaker model now, so a missing model shows up at start-up
        # (and in /health) instead of on the first utterance.
        if gate is not None:
            await asyncio.to_thread(gate.warm)
            set_speaker_gate_state(gate)
        app.state.announcer = await announce_server(_session, args.announce_socket)
        app.state.cheerleader = None
        if args.encourage:
            low, high = args.encourage_interval
            app.state.cheerleader = asyncio.create_task(encourage_loop(_session, low, high))
            print(f"  encouragement on -- a line every {low:g}-{high:g} minutes", flush=True)

    @app.on_event("shutdown")
    async def _shutdown():
        if app.state.cheerleader is not None:
            app.state.cheerleader.cancel()
        app.state.announcer.close()
        with suppress(FileNotFoundError):
            os.unlink(args.announce_socket)
        await client.aclose()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
