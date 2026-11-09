# pipeline benchmarks — time a voice turn, stage by stage

`bench_turn.py` takes example sentences and reports how long each stage of a
voice turn takes on the real deployed services, for every backend combination
you give it. Use it to answer "why does a conversation feel slow?" and "is
backend X faster than backend Y?" with numbers instead of impressions.

For each sentence it speaks the sentence with a TTS service to get realistic
input audio (untimed, it stands in for you talking), then times:

| column | what it is |
| --- | --- |
| `stt s` | speech-to-text on that audio (the deployed `stt` service; Moonshine since 2026-09-24) |
| `1st tok` | time until the LLM produced its first token (reasoning or answer) |
| `llm s` | time until the LLM's full reply finished |
| `tts s` | text-to-speech of the whole reply |
| `TURN s` | `stt + llm + tts` — the gateway runs these strictly in sequence, so this is the wait you actually hear, minus only the gateway's resample and the BLE hop |
| `audio s` | length of the spoken reply |

A reply line also shows how many characters of *hidden* reasoning the model
produced. llama.cpp returns Qwen3's thinking in a separate field, so it never
reaches the Stick; you only pay for it in time.

It calls `stt`, `agent`/`llama-cpp-*`, and `tts` directly rather than driving the
gateway WebSocket: the gateway's speaker gate would reject a synthetic voice
before the LLM ran. Standard library only; results go to
`benchmarks/results/pipeline/` (gitignored) as JSON + CSV.

## Running it

From this laptop (a k3s node, so it can reach every ClusterIP). `--cluster-ssh`
looks up the service addresses with `kubectl` on the home server:

```bash
# interactive: type a sentence, see its breakdown, repeat; empty line to finish
python benchmarks/pipeline/bench_turn.py --cluster-ssh arch-ssd.<tailnet>.ts.net

# batch: sentences on the command line or in a file
python benchmarks/pipeline/bench_turn.py --cluster-ssh arch-ssd.<tailnet>.ts.net \
    --repetitions 3 "Hey, how was your day?" "Can you remind me to drink water later?"
python benchmarks/pipeline/bench_turn.py --cluster-ssh arch-ssd.<tailnet>.ts.net \
    --sentences-file my_sentences.txt --repetitions 5
```

From inside a pod the service names resolve directly; drop `--cluster-ssh`.

Default targets: the live `agent` route (whatever the GPU scheduler currently
points it at), plus both llama.cpp servers called directly with thinking off,
each spoken by the deployed `tts` (Kitten nano since 2026-09-24; the target
is still labelled `kokoro`). While the 4060 serves, `llama-cpp-gtx1650` is at 0
replicas: run `tools/standby.sh warm` first (about 61 s to Ready) and
`tools/standby.sh auto` afterwards, or that row fails. Override with repeatable flags:

```bash
--llm NAME=URL[,model=M][,kind=openai|agent][,think=off|on|default]
--tts NAME=URL
--stt URL            --no-stt   (feed the sentence straight to the LLM)
--no-tts             --system "persona prompt for every LLM target"
```

For example, to see what thinking costs on the 4060, text only:

```bash
python benchmarks/pipeline/bench_turn.py --cluster-ssh arch-ssd.<tailnet>.ts.net \
    --no-stt --no-tts \
    --llm off=http://llama-cpp-rtx4060:8080/v1,model=qwen3-8b,think=off \
    --llm on=http://llama-cpp-rtx4060:8080/v1,model=qwen3-8b,think=on \
    "In one short sentence, why is the sky blue?"
```

A TTS target is any service speaking the `tts` HTTP contract (`POST /synth`
→ `{"chunks_b64": [...]}`), so a second deployed voice can be compared by
adding another `--tts`. To compare *local* TTS engines without deploying them,
use `tools/bench_tts.py`.

## What gets recorded

Before timing anything, `bench_turn` asks the `stt` and `tts` services'
`/health` which backend they run and with which options (defaults included),
prints it, and saves it with every row (`stt_backend`, `tts_backend`) and in
the run's metadata alongside the git commit and the sentences. A saved run
therefore says what it measured, whatever the `--tts NAME` label says.

For a result worth keeping, use the shared sentences and 50 samples, and
`--keep` to write a small committable summary to `benchmarks/runs/pipeline/`:

```bash
python benchmarks/pipeline/bench_turn.py --cluster-ssh arch-ssd.<tailnet>.ts.net \
    --llm agent=http://agent:8002,kind=agent \
    --sentences-file benchmarks/sentences.txt --warmups 3 --repetitions 10 \
    --keep "moonshine + kitten 1.6"
```

Summaries carry n, p50, p95, mean, min and max per stage, plus the reply
voice's x realtime. See `benchmarks/README.md` for where everything lives.

## Reading the numbers

- Run `--warmups 1` (the default) or more: the first request after a model
  loads or a pod restarts is a cold start and isn't representative.
- `agent` vs the direct llama.cpp rows isolates what the agent adds (its MCP
  tool loop, and whether it disables thinking).
- `think=on` rows show how much of a turn is hidden reasoning.
- Record the cold/warm state and which GPU the scheduler had selected with
  any result you keep; `benchmarks/latency/README.md` lists what else to hold
  fixed for a publishable comparison.

## First measured result (2026-09-24)

Same prompt, text only, one warm run each:

| route | thinking off | thinking on |
| --- | --- | --- |
| GTX 1650, `qwen3.5-4b` direct | 1.0 s | 20.5 s (3,704 chars hidden reasoning) |
| RTX 4060, `qwen3-8b` direct | 1.3 s | 13.3 s (1,259 chars hidden reasoning) |
| live `agent` route (4060) | 6.6 s | — |

The gateway asks for thinking off (`think=False`), but the agent's
`openai-compatible` backend was dropping that flag, so Qwen3 reasoned on every
turn. Fixed the same day (commit `2151781`); see the after numbers below.

Full turn minus reply TTS (`--no-tts`), two sentences x two repetitions,
warm, 4060 up (so `agent` was routed to the 4060):

| route | STT p50 | LLM p50 | turn p50 (no TTS) | turn p95 |
| --- | --- | --- | --- | --- |
| RTX 4060 direct, thinking off | 3.06 s | 1.76 s | 4.84 s | 5.13 s |
| GTX 1650 direct, thinking off | 3.06 s | 2.48 s | 5.56 s | 6.62 s |
| live `agent` route | 3.06 s | 14.77 s | 17.86 s | 19.62 s |

Reply TTS could not be measured that day: the deployed Kokoro `tts` pod was
OOM-killed (3 GiB limit) after three to four sequential `/synth` calls,
memory climbing per request. STT at ~3 s is also well above the 0.33 s this
card measured standalone (`docs/hardware-budget.md`).

After the fix (same command, agent redeployed, 4060 route, warm):

| route | STT p50 | LLM p50 | turn p50 (no TTS) | turn p95 |
| --- | --- | --- | --- | --- |
| live `agent` route, before | 3.06 s | 14.77 s | 17.86 s | 19.62 s |
| live `agent` route, after | 3.14 s | 2.24 s | 5.39 s | 5.61 s |
| RTX 4060 direct, thinking off | 3.14 s | 1.68 s | 4.79 s | 5.03 s |

The agent now costs about 0.5 s over a direct call (its MCP tool loop). STT
is now the largest stage. The GTX 1650 rows failed in this run because
`llama-cpp-gtx1650` was OOM-killed at its 2 GiB limit mid-request.

## Comparing STT and TTS backends

`bench_turn` talks to whichever backend the `stt` and `tts` services are
running, so compare backends by switching and re-running the same command:

```bash
tools/switch-backend.sh stt moonshine      # on arch-ssd, or anywhere with kubectl
python benchmarks/pipeline/bench_turn.py --cluster-ssh arch-ssd.<tailnet>.ts.net \
    --llm 4060=http://llama-cpp-rtx4060:8080/v1,model=qwen3-8b,think=off \
    --sentences-file my_sentences.txt --repetitions 2
```

The `tts` column is the target's label (`kokoro` by default), not the
backend, so note which backend each saved result was run against. Results of
doing this on 2026-09-24 are in `docs/hardware-budget.md` ("sherpa-onnx
backends"): STT p50 went from 3.31 s (whisper) to 0.18 s (moonshine) and turn
p50 from 10.6 s to 7.4 s. The PyTorch Kokoro pod was OOM-killed mid-run.
