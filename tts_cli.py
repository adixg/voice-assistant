"""Headless VITS-Umamusume TTS: text in -> wav file out. No gradio.

Runs the full VITS model in torch, on GPU when one is available (override with
--device). This is the plain torch path, not the ONNX-hybrid one the Space uses
for CPU-only hosting; output is equivalent.

Needs the cloned Space next to this file, at
  ./VITS-Umamusume-voice-synthesizer/
(configs, model code, and pretrained_models/*.pth) plus the `uma-tts` conda env.

One-shot:
    python tts_cli.py "こんにちわ。" -o hello.wav
    python tts_cli.py "Hello there." -m trilingual -l en -s 96 -o hi.wav
    python tts_cli.py --list-speakers -m trilingual

Persistent server (model stays resident; ~1 request/sec instead of ~1/10sec):
    python tts_cli.py --serve -m trilingual -l en -s 10 --device cpu
    # then write one line per request on stdin:  <outpath>\t<text>
    # a line with just <outpath> is echoed back when that wav is ready
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


def load_model(model_name, device):
    cfg = MODELS[model_name]
    hps = utils.get_hparams_from_file(cfg["config"])
    net = models.SynthesizerTrn(
        len(hps.symbols),
        hps.data.filter_length // 2 + 1,
        hps.train.segment_size // hps.data.hop_length,
        n_speakers=hps.data.n_speakers,
        **hps.model,
    ).to(device)
    net.eval()
    utils.load_checkpoint(cfg["model"], net, None)
    return net, hps


def synth(net, hps, device, text, mark, speaker, speed, out_path):
    stn = get_text(f"{mark}{text}{mark}", hps)
    with no_grad():
        x = stn.unsqueeze(0).to(device)
        x_len = LongTensor([stn.size(0)]).to(device)
        sid = LongTensor([speaker]).to(device)
        audio = net.infer(x, x_len, sid=sid, noise_scale=.667, noise_scale_w=0.8,
                          length_scale=1.0 / speed)[0][0, 0].data.cpu().float().numpy()
    sf.write(out_path, audio, hps.data.sampling_rate)
    return len(audio) / hps.data.sampling_rate


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
    ap.add_argument("--serve", action="store_true", help="persistent stdin loop (see module docstring)")
    ap.add_argument("--list-speakers", action="store_true", help="print speaker ids and exit")
    args = ap.parse_args()

    hps = utils.get_hparams_from_file(MODELS[args.model]["config"])
    if args.list_speakers:
        for name, sid in hps.speakers.items():
            print(f"{sid:3d}  {name}")
        return

    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        ap.error("--device cuda requested but torch.cuda.is_available() is False")

    mark = LANG_MARK[args.lang] if args.model == "trilingual" else ""
    net, hps = load_model(args.model, device)

    if args.serve:
        sys.stderr.write(f"tts ready ({args.model}/{args.lang}/s{args.speaker} on {device})\n")
        sys.stderr.flush()
        print("ready", flush=True)
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line:
                continue
            out_path, tab, text = line.partition("\t")
            if not tab:  # just a path -> treat as a no-op ping
                print(out_path, flush=True)
                continue
            try:
                synth(net, hps, device, text, mark, args.speaker, args.speed, out_path)
                print(out_path, flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"ERR\t{e}", flush=True)
        return

    if not args.text:
        ap.error("text is required (or pass --list-speakers / --serve)")

    if device.type == "cuda":
        torch.cuda.synchronize()
    t0 = time.time()
    dur = synth(net, hps, device, args.text, mark, args.speaker, args.speed, args.out)
    if device.type == "cuda":
        torch.cuda.synchronize()
    dt = time.time() - t0
    print(f"wrote {args.out}  ({dur:.2f}s audio @ {hps.data.sampling_rate} Hz) "
          f"on {device} in {dt:.2f}s  ({dur / dt:.1f}x realtime)")


if __name__ == "__main__":
    main()
