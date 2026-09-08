"""Headless Chatterbox Turbo TTS (Resemble AI, MIT licensed): text in -> wav
file out. 350M params, ~75ms latency, 6x real-time on GPU — see
https://www.resemble.ai/learn/models/chatterbox-turbo.

Needs the `chatterbox-tts` conda env (requirements-chatterbox.txt). Downloads
its weights from Hugging Face (ResembleAI/chatterbox-turbo) on first run.

No reference clip is passed by default, so it speaks in the model's own
built-in voice rather than cloning anyone's — pass --prompt a_clip.wav (5s+
of clean speech) to clone a voice instead; see model.generate()'s
audio_prompt_path in resemble-ai/chatterbox's example_tts_turbo.py.

One-shot:
    python chatterbox_cli.py "Hello there." -o hi.wav
    python chatterbox_cli.py "Hi! [chuckle] good to hear from you." -o hi.wav
    python chatterbox_cli.py "In her voice now." --prompt ref_clip.wav -o hi.wav

Persistent server (model stays resident; loading it is the slow part):
    python chatterbox_cli.py --serve --device cuda
    # then write one line per request on stdin:  <outpath>\t<text>
    # a line with just <outpath> is echoed back when that wav is ready
"""
import argparse
import contextlib
import sys
import time

import torch
import torchaudio as ta
from chatterbox.tts_turbo import ChatterboxTurboTTS

# chatterbox-tts prints its own progress ("loaded PerthNet (Implicit) at step
# 250,000", tqdm bars for some steps) straight to stdout, not stderr — which
# collides with --serve's stdin/stdout handshake protocol below (the first
# stdout line has to be exactly "ready", not whatever the library felt like
# printing first). Redirecting stdout to stderr for the actual model calls
# keeps that chatter out of the protocol stream without silencing it.
_quiet = lambda: contextlib.redirect_stdout(sys.stderr)  # noqa: E731


def load_model(device, nano=False):
    # Nano (110M) and Turbo (350M) share this class and differ only by which
    # checkpoint and GPT2 config get loaded — see from_pretrained's nano flag.
    with _quiet():
        return ChatterboxTurboTTS.from_pretrained(device=device, nano=nano)


def synth(model, text, prompt, out_path):
    kwargs = {"audio_prompt_path": prompt} if prompt else {}
    with _quiet():
        wav = model.generate(text, **kwargs)
    ta.save(out_path, wav, model.sr)
    return wav.shape[-1] / model.sr


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", help="text to synthesize")
    ap.add_argument("-o", "--out", default="out.wav", help="output wav path (default: out.wav)")
    ap.add_argument("--prompt", default=None,
                    help="reference clip (5s+) to clone a voice from; omit for the model's own default voice")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu",
                    help="torch device: cuda, cuda:0, cpu (default: cuda if available)")
    ap.add_argument("--nano", action="store_true",
                    help="Chatterbox Nano (110M) instead of Turbo (350M) — same voice cloning, "
                         "small enough to run on CPU and leave the GPU to whisper/the LLM")
    ap.add_argument("--serve", action="store_true", help="persistent stdin loop (see module docstring)")
    args = ap.parse_args()

    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        ap.error("--device cuda requested but torch.cuda.is_available() is False")

    model = load_model(device, nano=args.nano)

    if args.serve:
        # stdin protocol, one request per line, tab-separated:
        #   <outpath>\t<text>              synth with the launch --prompt (or none)
        #   <outpath>\t<prompt-or-->\t<text>   synth with that reference clip ("-" = none)
        #   <outpath>                      no-op ping, echoed back
        # the outpath is echoed on success, or "ERR\t<msg>" on failure
        sys.stderr.write(f"chatterbox ready ({'nano' if args.nano else 'turbo'}, "
                         f"prompt={args.prompt or 'default voice'}, {device})\n")
        sys.stderr.flush()
        print("ready", flush=True)
        for line in sys.stdin:
            parts = line.rstrip("\n").split("\t")
            if not parts or not parts[0]:
                continue
            out_path = parts[0]
            if len(parts) == 1:  # ping
                print(out_path, flush=True)
                continue
            if len(parts) >= 3:
                prompt = None if parts[1] == "-" else parts[1]
                text = "\t".join(parts[2:])
            else:
                prompt, text = args.prompt, parts[1]
            try:
                synth(model, text, prompt, out_path)
                print(out_path, flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"ERR\t{e}", flush=True)
        return

    if not args.text:
        ap.error("text is required (or pass --serve)")

    if device.type == "cuda":
        torch.cuda.synchronize()
    t0 = time.time()
    dur = synth(model, args.text, args.prompt, args.out)
    if device.type == "cuda":
        torch.cuda.synchronize()
    dt = time.time() - t0
    print(f"wrote {args.out}  ({dur:.2f}s audio @ {model.sr} Hz) "
          f"on {device} in {dt:.2f}s  ({dur / dt:.1f}x realtime)")


if __name__ == "__main__":
    main()
