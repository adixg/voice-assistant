# tts_engines — every TTS engine, alone, on the same sentences

`bench_tts_engines.py` measures each engine with no network, no LLM and no
other load: load time, then `--warmups` untimed calls, then `--repetitions`
passes over `benchmarks/sentences.txt`, each call timed through the backend's
real `synth()`. Each engine runs in its own child process, so the peak memory
reported (`rss_peak_mib`, the process's VmHWM plus any worker it started) is
that engine's alone.

It reports **seconds to make 10 s of speech** (total synth time over total
audio, the chart's y axis), x realtime, the slowest call's x realtime,
per-call p50/p95, and peak memory. The README's speed-vs-memory chart is drawn
from the `--keep` summaries in `benchmarks/runs/tts_engines/`.

## Running it

Locally, in the `chat` env:

```bash
python benchmarks/tts_engines/bench_tts_engines.py \
    --engine "Kitten nano=kitten" \
    --engine "Kokoro-ONNX fp32=kokoro-onnx" \
    --warmups 3 --repetitions 10 --keep "laptop CPU"
```

`--engine` is `LABEL=BACKEND` followed by that backend's own flags, e.g.
`"Kitten mini=kitten --kitten-model kitten-mini-en-v0_8"`.

On arch-ssd the numbers come from a one-off pod running the deployed tts
image, so the engine sees the same CPU and libraries the live service does:

```bash
# on arch-ssd, with this repo's voicepipe/ and benchmarks/ copied into the pod
kubectl -n aicompanion run tts-engine-bench --image=ghcr.io/adixg/aicompanion-tts-kokoro:latest \
    --overrides='{"spec":{"nodeSelector":{"gpu-tier":"gtx1650"}, ...}}' -- sleep 7200
kubectl -n aicompanion exec tts-engine-bench -- sh -c \
    'cd /tmp/aigf && python3 benchmarks/tts_engines/bench_tts_engines.py ... --host-label arch-ssd \
     --git-commit <sha> --keep "arch-ssd CPU"'
kubectl -n aicompanion cp tts-engine-bench:/tmp/aigf/benchmarks/runs/tts_engines/<file> \
    benchmarks/runs/tts_engines/<file>
kubectl -n aicompanion delete pod tts-engine-bench
```

Mount the `/var/lib/aicompanion/sherpa-onnx` hostPath at `/models/sherpa-onnx`
and set `SHERPA_MODELS_DIR` to it, so models aren't downloaded again.

## Not measured here

- **PyTorch `kokoro`** on arch-ssd: it grows past 2.8 GiB and was OOM-killed
  at a 3 GiB limit. Running it would put the live pods on that node under
  memory pressure.
- **`vits`, `chatterbox`**: their packages aren't in the kokoro image, and
  Chatterbox wants the GPU. The chart's Chatterbox points are the
  2026-09-05 laptop GPU numbers, in a file marked `-transcribed`.
