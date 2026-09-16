#!/usr/bin/env python3
"""Voice bridge for the M5StickS3: the same STT -> LLM -> TTS pipeline as
chat_loop.py (via voicepipe/), but the mic and speaker are the Stick's, over a
WebSocket, instead of this machine's. See firmware/m5stick_bridge/ for the
Stick side, and tools/echo_server.py for a version that skips STT/LLM/TTS
entirely (mic straight back out) — useful for telling a hardware or network
problem apart from a model one.

Wire protocol (one persistent WS connection, PCM16 mono @ 16kHz both ways):
  Stick -> server: text "start", then binary mic PCM chunks, then text "stop";
                   or text "reset" at any time, to clear conversation history
  server -> Stick: text "heard:<transcript>" as soon as STT finishes, then
                   zero or more "status:<what it's doing>" while an agent runs
                   tools, then text "reply:<text>", then binary reply PCM
                   chunks, then text "end"

Every turn ends with exactly one "end", including failures — the Stick stays
in its speaking state until it arrives.

"status:" only appears with a backend that streams progress (an agent that
runs tools, not a plain chat model). Older firmware ignores unknown text
messages, so sending it is safe against a Stick that hasn't been reflashed —
it just won't be displayed.

Run in the `chat` conda env:

    conda activate chat
    python bridge_server.py --host http://media:11434 --model rina

Needs ffmpeg on PATH (to resample TTS output to 16kHz for the Stick), plus
whichever conda env the chosen --tts-backend shells out to. Run with --help
to see every backend's own options.
"""
import asyncio
import os
import queue
import random
import sys
import tempfile
import wave
from contextlib import suppress

import websockets

from voicepipe import cli
from voicepipe import encouragement
from voicepipe.cuda import ensure_cuda_libs
from voicepipe.registry import FINAL, STATUS, stream_reply
from voicepipe.speaker import REJECTED, TOO_SHORT, rejection_line, too_short_line
from voicepipe.wire_audio import NORMALIZE_FILTER, SAMPLE_RATE, SEND_CHUNK, resample_to_pcm16  # noqa: F401

ensure_cuda_libs()

# This normally runs under nohup with stdout redirected to a file, which makes
# stdout FULLY buffered rather than line buffered — so progress lines sit
# invisible in the buffer for as long as it takes to fill. That turned a
# working speaker check into an apparent hang: the turn had completed, the
# score was computed, and none of it had reached the log yet. Line buffering
# here fixes every print at once, rather than relying on remembering
# flush=True at each call site.
sys.stdout.reconfigure(line_buffering=True)

MIN_UTTERANCE_BYTES = SAMPLE_RATE * 2 // 4  # ignore stray <0.25s blips
ENROLL_SAMPLES = 5  # how many the --enroll prompt asks for; more is better


async def iter_in_thread(make_iter):
    """Yield from a *blocking* generator without blocking the event loop.

    Every heavy stage of a turn — the speaker gate, whisper, the LLM, TTS — is
    synchronous, and calling one inline stops the loop for its whole duration.
    While the loop is stopped websockets cannot answer the Stick's keepalive
    ping, so `websockets.serve()`'s default 20s ping timeout tears the
    connection down mid-turn and the finished reply is thrown away with
    `received 1011 (internal error) keepalive ping timeout`. That was a latent
    bug for any slow turn and a guaranteed one for an agent backend running
    tools; a cold model load after a reboot is what finally exposed it.

    The generator runs in a worker thread and hands items over an ordinary
    thread-safe queue, so the loop stays free to answer pings and to send the
    `status:` updates that arrive between yields. The queue is unbounded on
    purpose: a bounded one deadlocks if the consumer stops early, because the
    producer would block forever in put() while nothing drains it.
    """
    done = object()
    items = queue.Queue()

    def pump():
        try:
            for item in make_iter():
                items.put(item)
        except BaseException as e:  # noqa: BLE001 - re-raised on the loop side
            items.put(e)
        else:
            items.put(done)

    pumping = asyncio.get_running_loop().run_in_executor(None, pump)
    try:
        while True:
            item = await asyncio.to_thread(items.get)
            if item is done:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        await pumping


async def call_in_thread(fn, *a, **kw):
    """A blocking call, off the event loop. See iter_in_thread for why."""
    return await asyncio.to_thread(fn, *a, **kw)


class Session:
    """One M5StickS3's conversation state (this bridge assumes a single Stick)."""

    def __init__(self, llm, stt, stt_lang, voice, args, gate=None):
        self.llm = llm
        self.stt = stt
        self.stt_lang = stt_lang
        self.voice = voice
        self.args = args
        self.gate = gate
        self.messages = []
        if args.system:
            self.messages.append({"role": "system", "content": args.system})
        self.wav_in = os.path.join(tempfile.mkdtemp(prefix="voicepipe-bridge-"), "utterance.wav")
        # The currently connected Stick, or None. Held so that something other
        # than a user turn — a reminder, a finished build — can speak.
        self.ws = None
        # Consecutive failed speaker checks. Drives how angry she gets about
        # it; reset by any accepted utterance, so one stray rejection doesn't
        # leave her shouting at the owner afterwards.
        self.rejection_streak = 0
        # One turn on the wire at a time. `reply:` / audio / `end` is a frame
        # sequence, not a message, so an announcement that lands mid-turn
        # doesn't arrive "during" the reply — it interleaves its PCM with the
        # reply's and both come out as noise. Every path that sends a turn
        # takes this first, so an unprompted line waits for the turn to finish
        # instead of corrupting it.
        self.speaking = asyncio.Lock()
        # getattr so a Session built from a minimal namespace (tests, other
        # callers) doesn't have to know about every CLI flag.
        self.normalize = not getattr(args, "no_normalize", False)

    def reset(self):
        """Drop the conversation, keeping the system prompt."""
        self.messages = [m for m in self.messages if m["role"] == "system"]

    async def handle_utterance(self, ws, pcm):
        """One full turn. Sends exactly one "end" unless the audio was too
        short to be speech, in which case the Stick never left idle and there
        is nothing to close off."""
        if len(pcm) < MIN_UTTERANCE_BYTES:
            # Logged, not silently dropped: "nothing happened at all" is the
            # hardest symptom to diagnose, and this was the blind spot when a
            # button press produced no output whatsoever.
            print(f"  (ignored {len(pcm) / (SAMPLE_RATE * 2):.2f}s — under the "
                  f"{MIN_UTTERANCE_BYTES / (SAMPLE_RATE * 2):.2f}s minimum)", flush=True)
            return
        async with self.speaking:
            try:
                # getattr, not args.enroll: Session is built from a minimal
                # namespace in tests and by other callers, and shouldn't require
                # every flag the CLI happens to define.
                if getattr(self.args, "enroll", False):
                    await self.enroll(ws, pcm)
                else:
                    await self._turn(ws, pcm)
            except Exception as e:  # noqa: BLE001 - a bad turn must not kill the connection
                print(f"  ! turn failed: {e}")
                await ws.send(f"reply:(error: {e})")
            finally:
                await ws.send("end")

    async def enroll(self, ws, pcm):
        """Record one enrollment sample from the Stick and save it.

        Enrolling through the Stick, rather than from a file, is deliberate:
        the voiceprint should be built from the same microphone, codec and
        room the gate will judge against, or the scores at use time won't
        resemble the scores at enrollment time.
        """
        with wave.open(self.wav_in, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)

        seconds = len(pcm) / (SAMPLE_RATE * 2)
        try:
            embedding = await call_in_thread(self.gate.backend.embed, self.wav_in)
        except Exception as e:  # noqa: BLE001
            print(f"  ! couldn't embed that sample: {e}")
            await self._say(ws, "That one didn't work, try again.")
            return

        # Warn about a clip too short to characterise a voice rather than
        # silently poisoning the print with it.
        if seconds < 2.0:
            print(f"  ! that was only {seconds:.1f}s — say a full sentence")
            await self._say(ws, "Too short. Say a whole sentence.")
            return

        print_ = self.gate.voiceprint
        if print_.samples:  # how well does this match what's already enrolled?
            print(f"  (matches existing samples at {print_.score(embedding):.3f})")
        print_.add(embedding, wav_path=self.wav_in)
        print_.save()
        n = len(print_)
        print(f"  enrolled sample {n} ({seconds:.1f}s) -> {print_.path}", flush=True)
        remaining = max(0, ENROLL_SAMPLES - n)
        await self._say(ws, f"Got sample {n}."
                        + (f" {remaining} to go." if remaining else " That's all of them."))

    async def _say(self, ws, text):
        """Speak a line to the Stick mid-enrollment.

        This deliberately plays real audio rather than just setting the
        caption: playback is what ends and restarts the microphone, so
        speaking between samples is what makes an enrolled sample resemble a
        sample taken during an actual conversation. Enrolling in silence
        captured a mic state that never occurs in use, and the same voice then
        scored 0.75 before the first reply and 0.17-0.60 after it.
        """
        await ws.send(f"reply:{text}")
        if self.voice:
            async for wav in iter_in_thread(lambda: self.voice.synth(text)):
                pcm_out = await resample_to_pcm16(wav, self.normalize)
                for off in range(0, len(pcm_out), SEND_CHUNK):
                    await ws.send(pcm_out[off:off + SEND_CHUNK])

    async def announce(self, text):
        """Say something unprompted.

        The wire protocol turns out not to care who started a turn: `reply:` /
        audio / `end` is just "display this and play it", and the Stick's
        handler never checks whether a question is outstanding. So a reminder
        or an alert is the ordinary speak path with no utterance in front of
        it — no firmware change needed.

        Returns False when no Stick is connected, rather than raising: an
        announcement nobody can hear is not an error.
        """
        if self.ws is None:
            return False
        async with self.speaking:
            ws = self.ws
            if ws is None:  # disconnected while we waited for the turn to end
                return False
            print(f"  (announce) {text}", flush=True)
            await ws.send(f"reply:{text}")
            if self.voice:
                async for wav in iter_in_thread(lambda: self.voice.synth(text)):
                    pcm_out = await resample_to_pcm16(wav, self.normalize)
                    for off in range(0, len(pcm_out), SEND_CHUNK):
                        await ws.send(pcm_out[off:off + SEND_CHUNK])
            await ws.send("end")
        return True

    async def _ask(self, ws):
        """The model's reply, forwarding progress to the Stick as it arrives.

        A chat model produces one FINAL event and this is just `ask()` with
        extra steps. An agent runs tools for tens of seconds, and each STATUS
        ("searching the web") goes straight to the Stick's caption so the
        screen shows work happening instead of freezing.

        The generator is synchronous, so it runs in a worker thread via
        iter_in_thread() rather than inline: a tool-running agent can hold it
        for tens of seconds, and blocking the event loop that long makes
        websockets drop the connection on a missed keepalive ping. Running it
        off-loop also means the STATUS sends below actually go out while the
        model is still working, instead of queueing behind it.
        """
        reply = ""
        think = None if self.args.think else False
        async for kind, payload in iter_in_thread(
                lambda: stream_reply(self.llm, self.messages, think)):
            if kind == STATUS:
                print(f"  ... {payload}")
                await ws.send(f"status:{payload}")
            elif kind == FINAL:
                reply = payload
            # DELTA is ignored here: the Stick gets whole sentences to speak,
            # and partial text would only make the caption flicker. chat_loop.py
            # prints them, where a live-typing effect is worth having.
        return reply

    async def _turn(self, ws, pcm):
        with wave.open(self.wav_in, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)

        # Before anything expensive: is this the owner? Checking here rather
        # than after STT means a stranger costs one embedding, not a whole
        # transcribe-and-generate turn.
        if self.gate is not None:
            verdict, score = await call_in_thread(self.gate.check, self.wav_in)
            if score is not None:
                streak = f", rejection #{self.rejection_streak + 1}" if verdict == REJECTED else ""
                print(f"  speaker score: {score:.3f} "
                      f"({verdict}, threshold {self.gate.threshold}{streak})")
            if verdict == TOO_SHORT:
                # Not a rejection: this is usually the owner asking something
                # brief, so ask for more rather than accusing them — and don't
                # count it toward the anger streak.
                await self._say(ws, too_short_line())
                return
            if verdict == REJECTED:
                self.rejection_streak += 1
                # Spoken, not just captioned: whoever picked it up should hear
                # why nothing is happening, and it's more fun in her voice.
                await self._say(ws, rejection_line(self.rejection_streak))
                return
            self.rejection_streak = 0

        text = await call_in_thread(self.stt.transcribe, self.wav_in, self.stt_lang)
        print(f"  you said: {text or '(nothing heard)'}")
        if not text:
            await ws.send("reply:(didn't catch that)")
            return

        # Not word-by-word live (faster-whisper transcribes the finished
        # utterance, not a stream) — sent as soon as it's ready, which lands
        # as the Stick's UI moves from listening to thinking.
        await ws.send(f"heard:{text}")

        self.messages.append({"role": "user", "content": text})
        try:
            reply = await self._ask(ws)
        except Exception as e:  # noqa: BLE001
            self.messages.pop()  # don't leave a dangling user turn in the history
            print(f"  ! llm error: {e}")
            await ws.send(f"reply:(llm error: {e})")
            return
        self.messages.append({"role": "assistant", "content": reply})
        print(f"  Rina: {reply}")

        await ws.send(f"reply:{reply}")
        if self.voice:
            async for wav in iter_in_thread(lambda: self.voice.synth(reply)):
                pcm_out = await resample_to_pcm16(wav, self.normalize)
                for off in range(0, len(pcm_out), SEND_CHUNK):
                    await ws.send(pcm_out[off:off + SEND_CHUNK])


async def handle_client(ws, session):
    print(f"  Stick connected: {ws.remote_address}", flush=True)
    session.ws = ws
    buf = bytearray()
    recording = False
    try:
        await _client_loop(ws, session, buf, recording)
    finally:
        if session.ws is ws:
            session.ws = None
        print("  Stick disconnected", flush=True)


async def _client_loop(ws, session, buf, recording):
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
            await session.handle_utterance(ws, bytes(buf))
        elif msg == "reset":
            print("  [reset] history cleared", flush=True)
            session.reset()
        else:
            print(f"  ? unexpected control message: {msg!r}")


async def announce_server(session, path):
    """A Unix socket that speaks whatever line is written to it.

    This is the trigger side of `announce()`: one line of text in, one spoken
    announcement out. A socket rather than an HTTP endpoint because it needs no
    dependency, no port, and filesystem permissions are the access control —
    anything that can reach it (a cron job, the agent, `tools/say.py`) can make
    the Stick talk.

        echo "the build finished" | python tools/say.py
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
    """Say something encouraging every so often, unprompted.

    Built on `announce()`, so it needs no firmware change and no button press.
    The interval is drawn fresh each time from [low, high] rather than being
    fixed: a line that arrives on a predictable schedule stops reading as her
    thinking of you and starts reading as a cron job.

    Nothing here fails loudly. If no Stick is connected the line is simply
    dropped — there is no queue, because encouragement that arrives an hour
    late is worse than none — and if a turn is in progress `announce()` waits
    for it rather than talking over it.
    """
    while True:
        await asyncio.sleep(random.uniform(low_minutes, high_minutes) * 60)
        try:
            if not await session.announce(encouragement.line()):
                print("  (encouragement skipped — no Stick connected)", flush=True)
        except Exception as e:  # noqa: BLE001 - this task must outlive any one failure
            print(f"  ! encouragement failed: {e}", flush=True)


async def serve(args):
    if args.enroll:
        return await enroll_mode(args)

    llm = cli.build_llm(args)
    stt, stt_lang = cli.build_stt(args)
    voice = cli.build_tts(args)
    gate = cli.build_speaker_gate(args)
    session = Session(llm, stt, stt_lang, voice, args, gate=gate)

    announcer = await announce_server(session, args.announce_socket)
    cheerleader = None
    if args.encourage:
        low, high = args.encourage_interval
        cheerleader = asyncio.create_task(encourage_loop(session, low, high))
        print(f"  encouragement on — a line every {low:g}-{high:g} minutes", flush=True)
    try:
        print(f"  listening on ws://{args.ws_host}:{args.ws_port} — waiting for the Stick...", flush=True)
        async with websockets.serve(lambda ws: handle_client(ws, session),
                                    args.ws_host, args.ws_port, max_size=None):
            await asyncio.Future()  # run forever
    finally:
        if cheerleader is not None:
            cheerleader.cancel()
        announcer.close()
        with suppress(FileNotFoundError):
            os.unlink(args.announce_socket)
        # Without this the TTS worker outlives the bridge, holding its model
        # (and a couple of GB of VRAM) until something kills it by hand.
        if voice:
            voice.close()


async def enroll_mode(args):
    """Record voiceprint samples and nothing else.

    Skips the LLM and STT entirely — enrollment needs the microphone and the
    speaker model, not a conversation — so it starts in seconds and can\'t
    accidentally answer anyone while the gate is still empty.
    """
    from voicepipe.registry import SV
    from voicepipe.speaker import SpeakerGate, Voiceprint

    voiceprint = Voiceprint.load()
    backend = SV.build(args.speaker_backend, args)
    # Tag the print with whatever produced it, so a later run with a different
    # backend can refuse it instead of comparing incomparable vectors.
    voiceprint.backend = getattr(backend, "name", args.speaker_backend)
    gate = SpeakerGate(backend, voiceprint, args.speaker_threshold, args.speaker_min_seconds)
    # TTS so she can speak the prompts and confirm each sample out loud, and
    # the announce socket so those prompts can be triggered from outside.
    voice = cli.build_tts(args)
    session = Session(None, None, None, voice, args, gate=gate)

    print(f"  ENROLLING — {len(voiceprint)} samples so far, aiming for {ENROLL_SAMPLES}.")
    print("  Hold the button and say a full sentence, in your normal voice.")
    print("  Vary it: different sentences, and the distance you'd normally hold it at.")
    print(f"  Saves to {voiceprint.path}; delete that file to start over.\n", flush=True)

    announcer = await announce_server(session, args.announce_socket)
    try:
        async with websockets.serve(lambda ws: handle_client(ws, session),
                                    args.ws_host, args.ws_port, max_size=None):
            print(f"  listening on ws://{args.ws_host}:{args.ws_port} — waiting for the Stick...",
                  flush=True)
            await asyncio.Future()
    finally:
        announcer.close()
        with suppress(FileNotFoundError):
            os.unlink(args.announce_socket)
        if voice:
            voice.close()


def main():
    ap = cli.build_parser(__doc__)
    ap.add_argument("--ws-host", default="0.0.0.0", help="bind address for the Stick's WebSocket")
    ap.add_argument("--ws-port", type=int, default=8765)
    ap.add_argument("--enroll", action="store_true",
                    help="record voiceprint samples from the Stick instead of holding a "
                         "conversation — hold the button and speak, once per sample")
    ap.add_argument("--announce-socket", default=os.path.join(tempfile.gettempdir(), "rina-announce.sock"),
                    help="Unix socket that speaks any line written to it, so a reminder or an "
                         "alert can reach the Stick without anyone pressing the button")
    ap.add_argument("--no-normalize", action="store_true",
                    help="send TTS audio to the Stick at its raw level. By default it is run "
                         "through ffmpeg's speech normalizer first, which is worth about "
                         "+3 dB — the speaker is at full volume but the synthesizer doesn't "
                         "use the whole range it's given")
    ap.add_argument("--encourage", action="store_true",
                    help="say something encouraging unprompted every so often, without "
                         "waiting to be asked (see --encourage-interval)")
    ap.add_argument("--encourage-interval", type=float, nargs=2, default=(15.0, 20.0),
                    metavar=("MIN", "MAX"),
                    help="minutes between encouragements, drawn fresh from this range each "
                         "time so it doesn't feel scheduled (default: 15 20)")
    args = cli.parse_args(ap)
    if args.encourage_interval[0] > args.encourage_interval[1]:
        ap.error("--encourage-interval MIN must not be greater than MAX")

    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
