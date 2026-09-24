# Hardware budget — GPU, models, TTS backends

Detail behind the current-state summary in `CLAUDE.md`. Every number below is
measured or fetched, with the date and the command that produced it, so it
can be re-checked rather than trusted.

## Every STT and TTS backend at a glance

One table for choosing a backend; the sections below have the detail behind
each number. Two machines: the **laptop** (20 CPU threads, RTX 4060) and
**arch-ssd** (i5-10300H, 8 threads, AVX2 without VNNI, GTX 1650). "—" means
never measured. Switch the live one with `tools/switch-backend.sh`.

STT, seconds to transcribe one utterance. The arch-ssd column is **n=50**
(p50 / p95, the live `stt` pod, `benchmarks/sentences.txt` spoken by Kitten,
3 warm-ups), kept in `benchmarks/runs/pipeline/20260924T20*.json`; the other
columns are small samples (2-4 clips):

| backend | CPU, laptop | arch-ssd, n=50 | GPU | measured |
| --- | ---: | ---: | --- | --- |
| `faster-whisper` small | 1.30 s | 3.13 / 3.58 s on the GTX 1650 (time-sliced with the LLM) | 0.33 s on the 4060 (685 MiB) | 09-05, 09-24 |
| `parakeet` 0.6B int8 | 0.19-0.37 s | **0.35 / 0.69 s** CPU | — (needs sherpa-onnx's CUDA build) | 09-24 |
| `moonshine` base (deployed) | 0.12-0.17 s | **0.19 / 0.36 s** CPU | — (same) | 09-24 |

TTS, as a multiple of realtime (2x = 10 s of speech made in 5 s). The arch-ssd
column is **n=50** per engine (`benchmarks/tts_engines/bench_tts_engines.py`,
3 warm-ups + 10 x 5 sentences, each engine alone in its own process in the tts
image), kept in `benchmarks/runs/tts_engines/20260924T193819Z-arch-ssd-cpu.json`;
memory there is the engine process's peak RSS. The running service adds
overhead on top (pod peaks: kitten 559 MiB, kokoro-onnx fp32 ~1.35 GiB):

| backend | CPU, laptop | arch-ssd, n=50 (slowest call) | GPU (4060) | memory | measured |
| --- | ---: | ---: | ---: | --- | --- |
| `kitten` nano int8, speed 1.6 (deployed) | — | **3.35x** (2.97x) | — | 251 MiB RSS | 09-24 |
| `kitten` nano int8, speed 1.0 | — | 3.20x (2.92x) | — | 348 MiB RSS | 09-24 |
| `kokoro-onnx` fp32 | — | **2.11x** (1.36x) | — | 650 MiB RSS | 09-24 |
| `kitten` mini | ~2.2x | 1.49x (1.25x) | — | 539 MiB RSS | 09-24 |
| `kokoro-onnx` int8 | ~1.0x | 0.80x (0.44x) | — | 462 MiB RSS | 09-24 |
| `kokoro` (PyTorch) | — | OOM-killed at 3 GiB | — | 2.8 GiB+ | 09-23/24 |
| `vits` (Umamusume) | 1.84x | — | — | 0 MiB VRAM | 09-05 |
| `chatterbox` Nano | 0.79x | — | **2.54x** (n=3) | 1857 MiB VRAM | 09-05 |
| `chatterbox` Turbo | 0.43x | — | 1.75x (n=3) | 2805 MiB VRAM | 09-05 |
| `elevenlabs` | cloud | | | | not measured |

A whole turn on the live cluster (agent route on the 4060, STT + LLM + TTS of
the full reply, n=50 each, `benchmarks/runs/pipeline/`):

| stt + tts | turn p50 | turn p95 |
| --- | ---: | ---: |
| parakeet + kitten 1.6 | 6.57 s | 32.1 s |
| **moonshine + kitten 1.6 (deployed)** | **6.83 s** | 30.8 s |
| moonshine + kokoro-onnx | 7.34 s | 41.5 s |
| faster-whisper (1650) + kitten 1.6 | 9.36 s | 35.7 s |

The p95 is the long replies (the LLM talks for 30+ s of audio), and the TTS
stage here synthesizes the whole reply; the gateway speaks sentence by
sentence, so the first sound on the Stick comes much sooner than these totals.
The moonshine-vs-parakeet turn difference is within the LLM's own variation
(2.23 vs 2.56 s p50); STT alone, moonshine is faster.

Missing GPU numbers need the 4060 free of `llama-cpp-rtx4060` (it holds
~7.9 of 8 GiB), and the sherpa-onnx rows need its CUDA wheel.

## Hardware budget

**NVIDIA RTX 4060 Laptop GPU, 8188 MiB total, driver 560.94** (WSL2).
Note that `nvidia-smi --query-compute-apps` cannot report per-process memory
under WSL2 (returns `[N/A]`), so components are measured by difference —
start/stop one and re-read the total.

Measured 2026-09-05 with `nvidia-smi --query-gpu=memory.used --format=csv`:

| Component | VRAM | How |
| --- | --- | --- |
| `rina` (qwen3 8.2B Q4_K_M) | **3659 MiB** | 4344 → 685 on `keep_alive: 0` |

STT measured 2026-09-05 from a 0 MiB card, on 1.9s / 3.3s / 12.2s clips:

| faster-whisper `small` | VRAM | load | avg latency | speed |
| --- | ---: | ---: | ---: | ---: |
| cuda, float16 | **685 MiB** | 1.4s | **0.33s** | 17.5x realtime |
| cpu, int8 | **0 MiB** | 2.7s | **1.30s** | 4.5x realtime |

So whisper on CPU is entirely usable — it costs about **+1.0s per turn** and frees
685 MiB. (Any script measuring this must call `voicepipe.cuda.ensure_cuda_libs()`
before importing the backend, or the CUDA path dies with
`Library libcublas.so.12 is not found`.)

TTS engines measured directly against a **0 MiB idle card** (bridge stopped,
Ollama unloaded), each started alone — see the table under "TTS backends"
below. Trust those over any by-difference figure: an earlier reading put
Turbo at 3250 MiB by subtraction, but measured alone from a clean baseline it
is **2805 MiB**.

**Chatterbox is the largest single consumer** — Turbo costs ~4x what whisper
does. It, not the LLM and not STT, is what makes the GPU tight.

### The LLM is already spilling to CPU

`GET /api/ps` while `rina` was loaded reported `size: 5971 MB` but
`size_vram: 3711 MB` — only ~62% of the model is on the GPU, the rest runs on
the CPU. `context_length` was 4096. So today's setup is already past the
card, and the LLM is the thing paying for it.

Moving TTS off the GPU frees 3250 MiB and leaves **~7503 MiB** for the LLM,
which is enough for an 8B Q4_K_M (5971 MB) to sit fully resident with ~1.5 GB
left for KV cache.

## Installed models

`GET /api/tags`, 2026-09-05: `rina:latest` (5.2 GB) and `qwen3:8b` (5.2 GB).
`rina` is qwen3 family, 8.2B, Q4_K_M — i.e. the Modelfile persona on qwen3:8b.

## TTS backends — measured

Measured 2026-09-05 from a 0 MiB idle card, 20 CPU cores, three reply-length
sentences per config, voice cloned from `voice_audition/refs/hinata_ref.wav`
(script: each backend started alone via its own class, VRAM read 3s after
load). RTF = audio seconds per wall-clock second; **above 1.0 is faster than
realtime**.

| config | VRAM | load | avg latency | avg RTF | clones a voice |
| --- | ---: | ---: | ---: | ---: | :---: |
| Chatterbox **Nano, cuda** | **1857 MiB** | 9.0s | **1.18s** | **2.54x** | yes |
| Chatterbox Turbo, cuda | 2805 MiB | 10.7s | 1.53s | 1.75x | yes |
| VITS, cpu | **0 MiB** | 5.1s | 1.35s | 1.84x | **no** |
| Chatterbox Nano, cpu | 93 MiB | 8.4s | 3.70s | 0.79x | yes |
| Chatterbox Turbo, cpu | 93 MiB | 12.1s | 5.65s | 0.43x | yes |

(The 93 MiB on the CPU rows is torch's CUDA context, not model weights.)

Conclusions from this table:

- **Nano-on-GPU beats Turbo-on-GPU outright** — 948 MiB less VRAM *and*
  faster (1.18s vs 1.53s). There is no reason to run Turbo over Nano when
  Chatterbox is the chosen backend at all.
- **Nano on CPU does not reach Resemble's claimed 3x realtime.** Measured
  **0.79x on 20 cores**, i.e. slower than realtime and a 3.7s wait per reply.
  Treat the vendor's "3x realtime on 8 CPU cores" as not reproducible here.
- **VITS is the only genuinely free-on-VRAM option**, and it does not clone
  a voice the way Chatterbox does — it picks from the VITS-Umamusume model's
  built-in speaker set instead (default speaker id 10, Grass Wonder).

**2026-09-16: the default TTS backend switched from Chatterbox to VITS**
(Umamusume voice, requested outright rather than following from a VRAM
argument — the Chatterbox-Nano-on-CUDA numbers above are unchanged and it
remains available via `--tts-backend chatterbox --chatterbox-nano` for
anyone who wants voice cloning back). Since VITS runs at 0 MiB on the CPU,
the GPU is now free for STT + LLM only by default, which loosens the "LLM
already spilling to CPU" finding below — worth re-measuring `GET /api/ps`
against a clean idle card if that matters for a future change.

## sherpa-onnx backends — measured on arch-ssd

Four CPU backends run through one runtime, sherpa-onnx
(`voicepipe/backends/sherpa_stt.py`, `sherpa_tts.py`): STT `parakeet` and
`moonshine`, TTS `kokoro-onnx` and `kitten`. Switch the live one with
`tools/switch-backend.sh`, which also sets the matching memory limit.

Live, in the cluster, 2026-09-24. Three sentences x two reps, warm, via
`benchmarks/pipeline/bench_turn.py` (4060 LLM direct); pod memory is the
cgroup's `memory.peak`:

| STT backend | p50 | pod peak | transcripts |
| --- | ---: | ---: | --- |
| faster-whisper small, GTX 1650 | 3.31 s | 1023 MiB (1 GiB limit) | all correct |
| `parakeet` (0.6B int8, CPU) | **0.23 s** | 962 MiB | all correct |
| `moonshine` (base, CPU, English) | **0.18 s** | 571 MiB | all correct |

Why whisper takes 3.3 s here against 0.33 s standalone on the 4060 was not
investigated. It shares the 1650 with the LLM by time-slicing, and it also
has `vad_filter` on. Either CPU backend removes the question.

| TTS backend | speed | pod peak | notes |
| --- | ---: | ---: | --- |
| `kokoro` (PyTorch) | — | OOM at 3 GiB | killed about 1 min after start under the benchmark |
| `kokoro-onnx` fp32 | 2.2x realtime | ~1.35 GiB, flat | same af_bella voice; the preset limit is 2 GiB |
| `kokoro-onnx` int8 | 0.9x realtime | OOM at 1 GiB | slower than fp32 on this CPU, see below |
| `kitten` nano int8 | 3.1x realtime | 559 MiB | lower quality, speaks more slowly |

Turn p50 (STT + LLM + TTS), before and after: **10.6 s** with whisper and
kokoro-onnx, **7.4 s** with moonshine and kokoro-onnx. PyTorch Kokoro couldn't
finish a run.

How the TTS rows above were split (a one-off pod on arch-ssd, 12 identical
reply-sized sentences in a row, 4 threads, RSS of a bare process without the
HTTP service):

| model | x realtime | RSS |
| --- | ---: | ---: |
| kokoro fp32 (`kokoro-multi-lang-v1_0`) | 2.21 | 700 MB |
| kokoro int8 (`kokoro-int8-multi-lang-v1_0`) | 0.87 | 581 MB |
| kitten nano int8 (`kitten-nano-en-v0_8-int8`) | 3.09 | 330 MB |
| kitten mini (`kitten-mini-en-v0_8`) | 1.43 | 535 MB |

- **int8 is slower on arch-ssd.** Its i5-10300H has AVX2 but no VNNI, so
  quantized matmuls get no hardware help. The same int8 model ran about 1x
  realtime on this laptop. Hence the defaults are fp32 Kokoro and nano Kitten.
- **8 threads is no faster than 4** (kokoro fp32: 2.04x vs 2.21x).
- **Memory stops growing.** RSS is flat after the second call. In the service,
  `kokoro-onnx` settled at 1346 MiB and stayed there across ten more requests
  of 50-1907 characters. It's a high-water mark (onnxruntime's arena), not a
  leak, but it's about double the bare process's, hence the 2 GiB limit.

### Chatterbox Nano — needs a git install, not a PyPI version

- `ResembleAI/chatterbox-nano` on Hugging Face (updated 2026-07-21) holds
  `t3_nano_v1.safetensors` beside the same `s3gen`/`ve` weights Turbo uses.
- 110M params vs Turbo's 350M. Loaded through the *same* class:
  `ChatterboxTurboTTS.from_pretrained(device, nano=True)`, which switches the
  checkpoint and uses `GPT2_small` instead of `GPT2_medium`.
- **PyPI's latest (0.1.7) does not have it** — its `from_pretrained(device)`
  takes no `nano` argument. Git main declares the same version `0.1.7` but
  its source differs, so a version bump will never pick this up.
- Installed here 2026-09-05 with
  `pip install --no-deps --force-reinstall git+https://github.com/resemble-ai/chatterbox.git`.
  `--no-deps` is safe and deliberate: every dependency git pins (torch 2.6.0,
  transformers 5.2.0, diffusers 0.29.0, gradio 6.8.0, …) already matched
  exactly, so only the package source changed. Turbo was regression-tested
  after the swap and still synthesizes.
- A PyPI backup of the pre-swap package is *not* kept in the repo; to revert,
  `pip install --force-reinstall chatterbox-tts==0.1.7`.
