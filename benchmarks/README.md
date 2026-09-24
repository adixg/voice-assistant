# benchmarks

What is measured, how, and where the results live.

| tool | measures | raw results (local, gitignored) | kept summaries (committed) |
| --- | --- | --- | --- |
| [`pipeline/bench_turn.py`](pipeline/README.md) | one voice turn on the live cluster, stage by stage: STT, LLM, TTS, whole turn | `results/pipeline/` | `runs/pipeline/` |
| [`tts_engines/bench_tts_engines.py`](tts_engines/README.md) | each TTS engine alone: seconds per 10 s of speech, x realtime, peak memory | `results/tts_engines/` | `runs/tts_engines/` |
| [`latency/benchmark_streaming.py`](latency/README.md) | LLM streaming latency (time to first token, total) | `results/` | none yet |
| [`gpu_allocation/`](gpu_allocation/README.md) | 4060 → 1650 failover and recovery | — | **plan only, never run** |
| `plot_tts_tradeoff.py` | draws the README's TTS chart from `runs/tts_engines/` | | `../assets/charts/` |

## Raw vs kept

Every run writes its raw per-sample rows to `results/` (gitignored: bulky,
and one laptop's scratch data). Pass `--keep "label"` to also write a small
summary to `runs/`, which is meant to be committed: the metadata (which
backends and options actually ran, reported by each service's `/health`,
the git commit, host, sentences, warm-ups, repetitions, failures) and the
statistics (n, p50, p95, mean, min, max). A number quoted in the docs should
point at a file in `runs/`.

Files in `runs/` start with a UTC timestamp, so the newest per engine is the
last by name. A file ending in `-transcribed.json` was typed in from older
docs because its raw data was never kept; its `metadata.source` says so.

## Sample sizes

Use the shared `sentences.txt` (five fixed sentences, short to long) with
`--warmups 3 --repetitions 10`: 50 timed samples per configuration. Enough
for a stable p50; treat p95 from 50 samples as indicative, not precise.
Record whether the model was cold or warm and which GPU the scheduler had
selected (`bench_turn` saves the backends automatically).

## Live data

The gateway also records every real turn's stage durations to Prometheus
(`aicompanion_gateway_stage_duration_seconds`, stages `stt`, `agent`,
`first_audio`, `tts_and_wire_audio`). That grows on its
own from real use; see it in Grafana or `tools/obs_tui.py`.
