# latency benchmarks

`benchmark_streaming.py` measures streaming text latency with only the Python
standard library. It writes per-request JSON/CSV records plus p50/p95/mean/max
summaries. Results stay local in `benchmarks/results/`.

Run it from a temporary pod that can resolve the cluster service names, or
from any machine with network access to the relevant endpoint. Warm up first,
then take ten samples of each fixed prompt:

```bash
python benchmarks/latency/benchmark_streaming.py --kind agent \
  --base-url http://agent:8002 --warmups 3 --repetitions 10

python benchmarks/latency/benchmark_streaming.py --kind openai \
  --base-url http://llama-cpp-gtx1650:8080/v1 --model qwen3.5-4b \
  --warmups 3 --repetitions 10
```

The first command measures the routed service path; the second isolates a
llama.cpp server. Run each against both GPU nodes, and record cold-start runs
separately from warm runs. The harness reports TTFT and total duration in
milliseconds, as well as output characters/sec. It intentionally does not
claim token/sec because the agent's NDJSON API does not report token usage.

## Full voice turns

For a quick per-stage breakdown of example sentences across backends, use
`benchmarks/pipeline/bench_turn.py` (see `benchmarks/pipeline/README.md`).
The protocol below is for a publishable, real-device comparison.

For 20 prerecorded utterances plus 20 real-device turns, record these
timestamps in one row per turn: mic/WAV end, STT complete, first agent delta,
agent final reply, TTS complete, and first audio frame delivered to Android.
Report p50/p95/max for mic-stop → first audio (the user-facing metric), each
component duration, success rate, speaker-gate accept/reject, transcription
accuracy, and BLE/audio-drop count. Keep fixed prompts, model/quantization,
context size, node, and cold/warm state with every result.
