#!/usr/bin/env python3
"""One-shot Qwen3-TTS 0.6B GPU probe; does not touch the deployed TTS service.

Run this on the GTX 1650 node after installing requirements/qwen3-tts.txt.
The first run downloads the tokenizer and model from Hugging Face.
"""
from __future__ import annotations

import argparse
import time


SAMPLE = (
    "こんにちは、リナです。今日はジョージア工科大学の天気を確認します。\n"
    "雨が降りそうなら、いつ始まっていつ止むかもお知らせします。\n"
    "準備ができたら、いつでも話しかけてください。"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="qwen3-tts-ono-anna-1650.wav")
    parser.add_argument("--text", default=SAMPLE)
    args = parser.parse_args()

    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is unavailable; run this on the GTX 1650 node")
    device = torch.cuda.current_device()
    print(f"GPU: {torch.cuda.get_device_name(device)}")
    print(f"VRAM before load: {torch.cuda.memory_reserved(device) / 2**20:.0f} MiB reserved")
    started = time.perf_counter()
    model = Qwen3TTSModel.from_pretrained(
        "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
        device_map=f"cuda:{device}",
        dtype=torch.float16,
        attn_implementation="eager",
    )
    load_seconds = time.perf_counter() - started
    print(f"Loaded in {load_seconds:.1f}s")
    print(f"VRAM after load: {torch.cuda.memory_reserved(device) / 2**20:.0f} MiB reserved")
    started = time.perf_counter()
    wavs, sample_rate = model.generate_custom_voice(
        text=args.text,
        language="Japanese",
        speaker="Ono_Anna",
    )
    synth_seconds = time.perf_counter() - started
    sf.write(args.output, wavs[0], sample_rate)
    print(f"Wrote {args.output} ({sample_rate} Hz)")
    print(f"Synthesis: {synth_seconds:.1f}s")
    print(f"Peak VRAM: {torch.cuda.max_memory_allocated(device) / 2**20:.0f} MiB allocated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
