"""Text-to-speech via the VITS-Umamusume synthesizer, shelled out to
tts_cli.py in the separate `uma-tts` conda env (see requirements-uma-tts.txt).

Standalone debug use (writes wavs to /tmp, doesn't play them):
    conda run -n chat python -m voicepipe.tts "hello there" -s 10 -l en
"""
import os
import re
import subprocess
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
UMA_PY = os.path.expanduser("~/anaconda3/envs/uma-tts/bin/python")
TTS_CLI = os.path.join(HERE, "tts_cli.py")
TMP = tempfile.gettempdir()


def chunks(text, limit=200):
    # sentence split, then hard-wrap any sentence that is still too long
    pieces = []
    for sent in re.split(r"(?<=[.!?。．！？])\s+", text.strip()):
        sent = sent.strip()
        if not sent:
            continue
        while len(sent) > limit:
            cut = sent.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if sent:
            pieces.append(sent)

    parts, buf = [], ""
    for piece in pieces:
        if len(buf) + len(piece) + 1 > limit and buf:
            parts.append(buf)
            buf = piece
        else:
            buf = f"{buf} {piece}".strip()
    if buf:
        parts.append(buf)
    return parts or [text.strip()]


class Voice:
    """Long-lived `tts_cli.py --serve` subprocess so the VITS model loads once.

    synth(text) -> list of wav paths, one per chunk; doesn't play anything, so
    it's the same code path for chat_loop.py (plays locally) and
    bridge_server.py (streams to the M5Stick) — see say() vs. bridge_server.py.
    """

    def __init__(self, args, hooks=None):
        self.args = args
        self.hooks = hooks
        self.p = None
        self.logpath = os.path.join(TMP, "tts_worker.log")

    def start(self):
        a = self.args
        cmd = [UMA_PY, TTS_CLI, "--serve", "-m", a.tts_model,
               "-s", str(a.speaker), "--device", a.tts_device]
        if a.tts_model == "trilingual":
            cmd += ["-l", a.tts_lang]
        print(f"  starting VITS worker on {a.tts_device} ...", flush=True)
        self.p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(self.logpath, "w"), text=True, bufsize=1,
        )
        line = self.p.stdout.readline().strip()
        if line != "ready":
            tail = ""
            try:
                tail = open(self.logpath).read()[-800:]
            except OSError:
                pass
            raise RuntimeError(f"VITS worker failed to start ({line!r})\n{tail}")

    def synth(self, text):
        """Synthesize `text` (chunked), returning the wav paths in order."""
        if self.p is None or self.p.poll() is not None:
            self.start()
        paths = []
        for i, chunk in enumerate(chunks(text)):
            wav = os.path.join(TMP, f"voice_reply_{i}.wav")
            self.p.stdin.write(f"{wav}\t{chunk.replace(chr(10), ' ')}\n")
            self.p.stdin.flush()
            resp = self.p.stdout.readline().strip()
            if resp != wav:
                print(f"  (tts: {resp or 'worker died'})")
                break
            paths.append(wav)
        return paths

    def say(self, text):
        """synth() + play each chunk locally (terminal/orb use)."""
        from .audio import play
        for wav in self.synth(text):
            play(wav, self.hooks)

    def close(self):
        if self.p and self.p.poll() is None:
            try:
                self.p.stdin.close()
            except OSError:
                pass
            self.p.terminate()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text")
    ap.add_argument("-m", "--tts-model", default="trilingual", choices=["trilingual", "japanese"])
    ap.add_argument("-l", "--tts-lang", default="en", choices=["ja", "zh", "en", "mix", "none"])
    ap.add_argument("-s", "--speaker", type=int, default=10)
    ap.add_argument("--tts-device", default="cpu")
    args = ap.parse_args()
    voice = Voice(args)
    for wav in voice.synth(args.text):
        print(wav)
    voice.close()
