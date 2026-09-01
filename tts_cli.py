"""Headless VITS-Umamusume TTS: text in -> wav file out. No gradio.

Runs the full VITS model in torch, on GPU when one is available (override with
--device). This is the plain torch path, not the ONNX-hybrid one the Space uses
for CPU-only hosting; output is equivalent.

Needs the cloned Space next to this file, at
  ./VITS-Umamusume-voice-synthesizer/
(configs, model code, and pretrained_models/*.pth) plus the `uma-tts` conda env.

Examples:
    python tts_cli.py "こんにちわ。" -o hello.wav
    python tts_cli.py "Hello there." -m trilingual -l en -s 96 -o hi.wav
    python tts_cli.py "Hello there." --device cpu -o hi.wav
    python tts_cli.py --list-speakers -m trilingual
"""
import argparse
import os
import sys
import time

# the cloned Space lives next to this script; run from inside it so the
# repo-relative paths in the config/model tables resolve, and import its modules
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VITS-Umamusume-voice-synthesizer")
if not os.path.isdir(REPO):
    sys.exit(f"can't find the VITS Space at {REPO}")
os.chdir(REPO)
sys.path.insert(0, REPO)

import soundfile as sf
import torch
from torch import no_grad, LongTensor

import commons
import utils
import models
from text import text_to_sequence

MODELS = {
    "trilingual": {
        "config": "./configs/uma_trilingual.json",
        "model": "./pretrained_models/G_trilingual.pth",
    },
    "japanese": {
        "config": "./configs/uma87.json",
        "model": "./pretrained_models/G_jp.pth",
    },
}

# wrap text in a language token (trilingual model only; harmless empty for the JP model)
LANG_MARK = {"ja": "[JA]", "zh": "[ZH]", "en": "[EN]", "mix": "", "none": ""}


def get_text(text, hps):
    seq = text_to_sequence(text, hps.symbols, hps.data.text_cleaners)
    if hps.data.add_blank:
        seq = commons.intersperse(seq, 0)
    return LongTensor(seq)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", help="text to synthesize")
    ap.add_argument("-o", "--out", default="out.wav", help="output wav path (default: out.wav)")
    ap.add_argument("-m", "--model", choices=list(MODELS), default="japanese",
                    help="'japanese' (87 chars, JP only) or 'trilingual' (JP/ZH/EN)")
    ap.add_argument("-s", "--speaker", type=int, default=0, help="speaker id, see --list-speakers")
    ap.add_argument("-l", "--lang", choices=list(LANG_MARK), default="ja",
                    help="language token for the trilingual model (ja/zh/en/mix/none)")
    ap.add_argument("--speed", type=float, default=1.0, help="speech speed multiplier")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu",
                    help="torch device: cuda, cuda:0, cpu (default: cuda if available)")
    ap.add_argument("--list-speakers", action="store_true", help="print speaker ids and exit")
    args = ap.parse_args()

    cfg = MODELS[args.model]
    hps = utils.get_hparams_from_file(cfg["config"])

    if args.list_speakers:
        for name, sid in hps.speakers.items():
            print(f"{sid:3d}  {name}")
        return

    if not args.text:
        ap.error("text is required (or pass --list-speakers)")

    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        ap.error("--device cuda requested but torch.cuda.is_available() is False")

    net = models.SynthesizerTrn(
        len(hps.symbols),
        hps.data.filter_length // 2 + 1,
        hps.train.segment_size // hps.data.hop_length,
        n_speakers=hps.data.n_speakers,
        **hps.model,
    ).to(device)
    net.eval()
    utils.load_checkpoint(cfg["model"], net, None)

    mark = LANG_MARK[args.lang] if args.model == "trilingual" else ""
    stn = get_text(f"{mark}{args.text}{mark}", hps)
    with no_grad():
        x = stn.unsqueeze(0).to(device)
        x_len = LongTensor([stn.size(0)]).to(device)
        sid = LongTensor([args.speaker]).to(device)
        if device.type == "cuda":
            torch.cuda.synchronize()
        t0 = time.time()
        audio = net.infer(x, x_len, sid=sid, noise_scale=.667, noise_scale_w=0.8,
                          length_scale=1.0 / args.speed)[0][0, 0].data.cpu().float().numpy()
        if device.type == "cuda":
            torch.cuda.synchronize()
        dt = time.time() - t0

    sf.write(args.out, audio, hps.data.sampling_rate)
    dur = len(audio) / hps.data.sampling_rate
    print(f"wrote {args.out}  ({dur:.2f}s audio @ {hps.data.sampling_rate} Hz) "
          f"on {device} in {dt:.2f}s  ({dur / dt:.1f}x realtime)")


if __name__ == "__main__":
    main()
