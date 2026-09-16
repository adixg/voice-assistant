# Hermes / Hermes Agent / OpenClaw — full investigation log

Detail behind the current-state summary in `CLAUDE.md`. This is the full,
chronological investigation into whether a local agent runtime (Hermes Agent
or OpenClaw) could replace the plain-chat voice pipeline — kept in full
because re-deriving any of these findings from scratch would cost real time
again, including the theories that turned out to be wrong.

**Bottom line, reached at the end of this log**: local `hermes3:8b` tool
calling is unreliable enough (best case ~87%, with dangerous confabulation
rather than refusal in the failures) that it isn't used for the voice
pipeline. `--llm-backend hermes-agent` exists and works as a plain
OpenAI-compatible client for anyone who wants to point it at a *hosted*
model instead, which sidesteps both the reliability problem and the 64K
context floor.

## "Hermes" means two different things — don't conflate them

Checked 2026-09-05 (I got this wrong once; the distinction is the whole point):

- **Hermes 3 / Hermes 4** — a *model* family (weights) from Nous Research,
  pullable in Ollama. Covered below.
- **Hermes Agent** — a *separate product*: an MIT-licensed, self-hosted
  autonomous agent runtime, also by Nous Research
  (`hermes-agent.nousresearch.com`, `github.com/NousResearch/hermes-agent`).
  It is an application, not a model and not a library.

Hermes Agent ships 60+ built-in tools (web search, browsing, image gen, TTS),
a three-layer memory system (working / episodic / semantic-skill) with FTS5
cross-session recall and LLM summarization, **skills** the agent writes for
itself as procedural memory (`skill_manage`, agentskills.io standard),
built-in cron scheduling, a bot mode for collaborating specialist bots, and
MCP client support. It runs on Linux/macOS/Windows/WSL2/Docker/serverless.

Crucially it is **model-agnostic** — Nous Portal, OpenRouter, OpenAI, or any
OpenAI-compatible custom endpoint, which includes local Ollama at
`http://localhost:11434/v1`. So "Hermes Agent" and "a Hermes model" are
independent choices, and it is a direct competitor to OpenClaw rather than to
the Claude Agent SDK.

### SUPERSEDED: "it needs ≥64K context, which this card cannot give a local 8B"

**The conclusion in this section is wrong and was corrected 2026-09-07** — see
"hermes3:8b RUNS AT 64K ENTIRELY ON THE GPU" below. The arithmetic here assumes
an **f16 KV cache** and a card shared with TTS. With `OLLAMA_KV_CACHE_TYPE=q4_0`
and TTS/STT on the CPU, `hermes3:8b` holds 64000 tokens with all 33 layers on
the GPU at 7137 MiB. The section is kept because the per-token KV reasoning is
still the right way to think about it; only the precision assumption changed.

### The original (f16-only) reasoning

Hermes Agent's own Ollama guide states a **minimum of 64,000 tokens** of
context for agent use with tools. Against the measured numbers above, for
Llama-3.1-8B (32 layers, 8 KV heads, 128 head-dim → ~128 KiB/token at fp16):

| KV precision | KV at 64K | + 4813 MiB weights | fits in 8188 MiB? |
| --- | ---: | ---: | :---: |
| fp16 | ~8192 MiB | ~13005 MiB | no |
| q8_0 | ~4096 MiB | ~8909 MiB | no |
| q4 | ~2048 MiB | ~6861 MiB | only with the GPU otherwise empty, and q4 KV degrades quality |

**So Hermes Agent driving a local Hermes 8B does not fit here** — not even
with whisper and TTS both pushed to the CPU. Running Hermes Agent against a
*hosted* model sidesteps this entirely and leaves the whole card to
whisper + TTS.

Measured directly 2026-09-05, asking Ollama for `qwen3:8b` at `num_ctx:
64000` (any 8B behaves the same; this is not Hermes-specific):

- Ollama capped context at its own max, **40960** — below the 64K asked for.
- Even so: **total 11064 MiB, only 4738 MiB on the GPU, 6326 MiB on the CPU —
  57% spilled**, with the card at 7398 MiB.
- 11064 MiB exceeds the whole 8188 MiB card, so it would spill even with the
  GPU completely empty.

The runtime itself needs no GPU — Hermes Agent's docs advertise running on a
$5 VPS. **The GPU question is entirely about where the *model* runs.** A
spilled model still works, just several times slower, which on a voice loop
means tens of seconds per reply.

### It CAN be driven programmatically — no fork, no custom channel

Resolved 2026-09-05 by reading the repo (`github.com/NousResearch/hermes-agent`,
HEAD 245e480). It is **Python**, not Node, and ships **three** protocols for
external programs, all driving the same `AIAgent` core
(`website/docs/developer-guide/programmatic-integration.md`):

| Protocol | Transport | Defined by |
| --- | --- | --- |
| ACP (Agent Client Protocol) | JSON-RPC over stdio | `acp_adapter/` |
| TUI gateway | JSON-RPC over stdio or WebSocket | `tui_gateway/server.py` |
| **API server** | **HTTP + SSE, OpenAI-compatible** | `gateway/platforms/api_server.py` |

The API server is the fit for this bridge. Enable in `~/.hermes/.env` with
`API_SERVER_ENABLED=true` and `API_SERVER_KEY=...`, run `hermes gateway`, and
it listens on `http://127.0.0.1:8642`.

- `POST /v1/chat/completions` — OpenAI-compatible and **stateless** (full
  conversation in `messages`), and per the docs the agent handles it "with its
  full toolset (terminal, file operations, web search, memory, skills)". That
  maps directly onto this project's `LLMBackend.ask(messages, think) -> str`.
- Streaming is SSE: standard `chat.completion.chunk` plus a Hermes-specific
  `hermes.tool.progress` event for tool-start visibility.
- `POST /v1/runs` + `GET /v1/runs/{id}/events` (SSE) for attach/detach without
  losing state, plus `/steer`, `/approval`, `/stop`.
- `POST /api/sessions/{id}/chat/stream` emits `assistant.delta`,
  `tool.started`, `tool.completed`, `run.completed`.

**Consequence: `voicepipe/backends/hermes_agent.py` would be a small
OpenAI-compatible client (~40 lines), and the Stick would be talking to a full
agent with no firmware change and no change to the wire protocol.** The
tool-progress events are also the missing progress seam for the Stick's
THINKING state and caption line.

One caveat from `website/docs/user-guide/security.md`: `api_server` counts as
an *unattended* surface (no human to answer an approval prompt), so
`unattended_mode` defaults to `deny` — dangerous commands are blocked
instantly rather than waiting on a timeout. The Stick is exactly such a
surface; decide this deliberately rather than setting `approve`.

## Hermes *model* availability

Checked against ollama.com 2026-09-05:

- Official Ollama library has **Hermes 3 in 3B / 8B / 70B / 405B**, plus older
  OpenHermes 7B, Nous Hermes 7B/13B, Nous Hermes 2 10.7B/34B.
- **No official Hermes 4 at 14B or smaller on Ollama.** Hermes 4 is 14B (Qwen3
  based) / 70B / 405B upstream, and the only 4.x on Ollama is a community
  `hermes-4.3-36B` (~21.8 GB Q4_K_M) — far past this card.
- Realistic local option here is **Hermes 3 8B**. Sizes from ollama.com:
  `hermes3:8b` 4.7 GB, `8b-llama3.1-q4_K_M` 4.9 GB, `8b-llama3.1-q3_K_M`
  4.0 GB, `hermes3:3b` 2.0 GB.
- Hermes *agent* workflows are documented as wanting **≥64K context**, well
  past the 4096 currently in use, and the KV cache is the binding constraint,
  not the weights.

### Does Hermes fit? (worked from the measured numbers)

Card is 8188 MiB. With the Nano bridge resident (whisper 685 + Nano 1857 =
**2542 MiB**, confirmed by reading the card after startup), **5646 MiB is
free**:

- `hermes3:8b` (4.7 GB ≈ 4813 MiB) fits fully resident, leaving ~830 MiB for
  KV cache. So **yes — Hermes runs entirely on the GPU, and the Hinata voice
  is kept.**
- But Llama-3.1-8B's KV cache is ~128 KiB/token at fp16 (32 layers × 8 KV
  heads × 128 head-dim × 2 for K+V × 2 bytes), so ~830 MiB buys only **~6K
  tokens** of context; roughly 12K with a q8_0 KV cache. **The 64K the Hermes
  agent docs ask for is not reachable on this card** while TTS is also on it.
- For comparison, today's `rina` (5971 MB) does **not** fit alongside TTS,
  which is exactly why `/api/ps` showed it spilling 38% to CPU.

Pushing STT and/or TTS to the CPU buys context, at ~1s of added latency each.
All three rows keep Hermes fully GPU-resident; only the context differs:

| layout | GPU used | free for LLM | KV after 4813 MiB of weights | added latency | keeps Hinata |
| --- | ---: | ---: | --- | ---: | :---: |
| whisper cuda + Nano cuda | 2542 MiB | 5646 MiB | ~6.5K fp16 / ~13K q8 | — | yes |
| whisper **cpu** + Nano cuda | 1857 MiB | 6331 MiB | ~12K fp16 / ~24K q8 | +1.0s | yes |
| whisper **cpu** + VITS **cpu** | ~0 MiB | 8188 MiB | ~26K fp16 / ~52K q8 | +1.1s | **no** |

Only the all-CPU row approaches the 64K the Hermes agent docs want, and it is
the row that gives up voice cloning. KV figures assume Llama-3.1-8B's
~128 KiB/token at fp16; halve the cost with a q8_0 KV cache.

## Hermes Agent vs OpenClaw (both checked 2026-09-05)

Both are self-hosted agent runtimes, both model-agnostic, both speak MCP,
both need a custom integration to be driven by this project's M5Stick
front-end. They differ in what they are *best* at:

| | Hermes Agent | OpenClaw |
| --- | --- | --- |
| Author | Nous Research | Peter Steinberger (ex-Moltbot) |
| Runtime | standalone app, "$5 VPS" class | Node.js (26 rec.; 22.22.3+/24.15+/25.9+) |
| Models | Nous Portal, OpenRouter, OpenAI, any OpenAI-compatible endpoint (incl. Ollama) | Anthropic, OpenAI, Gemini, Mistral, Cohere, Groq + Ollama, llama.cpp, LM Studio, vLLM, SGLang, LiteLLM |
| Built-in tools | 60+ (search, browse, image gen, TTS) | shell, browser, files |
| Memory | 3-layer working/episodic/semantic, FTS5 recall + LLM summarization, 8 external providers | sessions, context, memory, multi-agent routing |
| Skills | self-written procedural memory (`skill_manage`, agentskills.io) | not documented as a distinct feature |
| Scheduling | built-in cron | cron + webhooks |
| Channels | terminal, Telegram, Discord (`hermes gateway setup`) | Discord, Google Chat, iMessage, Matrix, Teams, Signal, Slack, Telegram, WhatsApp, Zalo |
| Stated context floor | **≥64K for tool use** | none stated (absence of docs, not evidence of tolerance) |

For this project: OpenClaw's strength is messaging-channel breadth, which is
irrelevant here — the Stick is the front-end. Hermes Agent's strength is
memory + skills + cron, which maps almost exactly onto the wishlist
(persistent memory, reminders, notes, search). Lean Hermes Agent on features;
the deciding risk for either is the missing custom-front-end entry point.

Relevance of OpenClaw specifically: it's a self-hosted **agent runtime and
message router** (Node.js gateway) by Peter Steinberger, renamed from Moltbot
in January 2026. It bridges chat apps (WhatsApp, Discord, Telegram, Signal,
iMessage, Slack, …) to AI coding agents, and can run shell commands, drive a
browser and manage files. It is **not a model** — it routes to one. This
project already has its own front-end (the Stick, the bridge, the wire
protocol, the pixel-art UI), which is the part OpenClaw would otherwise
supply, so its capabilities are better borrowed as tools than adopted as a
runtime.

## Speaker verification — see `docs/voice-pipeline.md`

(Not a Hermes topic — moved there.)

## An 8B agent DOES fit — measured with the card empty and a quantized KV cache

Measured 2026-09-07, superseding the "does Hermes fit?" arithmetic above, which
assumed TTS was resident and an f16 KV cache. Two things changed:

1. **The card is now genuinely empty.** Running `--tts-backend vits
   --whisper-device cpu` leaves `nvidia-smi` reporting **0 MiB used of 8188**,
   confirmed directly. The whole card is available to the model.
2. **Ollama can quantize the KV cache**, which the earlier tables never applied.
   `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0` (or `q4_0`), set as
   **server** environment variables — it is a global server setting, not a
   per-request option. Ollama 0.17.0 here.

`qwen3:8b` (Q4_K_M, 36 layers, 8 KV heads, 128 head-dim — so ~144 KiB/token at
f16), loaded alone on the empty card via `/api/generate` with `num_ctx` set,
then read back from `/api/ps`:

| KV type | num_ctx | total | on GPU | on CPU | fully resident? |
| --- | ---: | ---: | ---: | ---: | :---: |
| f16 | 8192 | 6255 MiB | 6255 | 0 | yes |
| f16 | **16384** | 7431 MiB | 7431 | 0 | **yes — f16 ceiling** |
| f16 | 32768 | 9855 MiB | 7304 | 2551 | no (74%) |
| f16 | 40960 | 11063 MiB | 7467 | 3596 | no (67%) |
| q8_0 | 16384 | 6415 MiB | 6415 | 0 | yes |
| q8_0 | 24576 | 7059 MiB | 7059 | 0 | yes |
| q8_0 | **28672** | 7393 MiB | 7393 | 0 | **yes — q8_0 ceiling** |
| q8_0 | 32768 | 7763 MiB | 6555 | 1208 | no (84%) |
| **q4_0** | **40960** | **6931 MiB** | **6931** | **0** | **yes** |

Read off that table:

- **f16 caps you at 16K.** This is the number every earlier estimate here was
  implicitly using, and it is the reason "Hermes doesn't fit" was the old
  conclusion.
- **q8_0 buys 28K**, at a perplexity cost benchmarked in the 0.002-0.05 range.
- **q4_0 fits qwen3's entire native 40960 window on the GPU** with ~1.25 GB of
  card to spare. 40960 is `qwen3.context_length`, i.e. the architectural
  maximum — there is nothing above it to reach without rope scaling.
- Measured KV cost per token: **147 KiB f16, ~80 KiB q8_0** (from the slope
  between two context sizes at fixed weights). q8_0 is ~55% of f16, not 50%,
  because of per-block scale factors.

So the binding constraint is no longer VRAM — it is **qwen3:8b's own 40960
ceiling** versus the 64K Hermes Agent's docs ask for. Reaching 64K needs a
model whose *native* window is larger; `hermes3:8b` is Llama-3.1-based
(131072 native) and is the obvious candidate.

**`qwen3:8b` declares tool calling.** `/api/show` reports `capabilities:
['completion', 'tools', 'thinking']`. The Ollama guide's claim that "only
`gemma4:31b` has reliable tool calling" is about the four models *that guide
lists*, not a general statement — do not read it as excluding qwen3.

### How to set the KV cache type without touching the system service

The measurements above were taken from a **second Ollama instance** on another
port, which leaves the systemd unit and the running bridge untouched:

    OLLAMA_HOST=127.0.0.1:11435 OLLAMA_FLASH_ATTENTION=1 \
        OLLAMA_KV_CACHE_TYPE=q4_0 ollama serve

Confirm it took effect by grepping the server log for `FLASH_ATTENTION` and
`q8_0`/`q4_0` — quantized KV **silently falls back to f16** on architectures
that don't support it, so setting the variable is not proof it applied.

To make it permanent for the system service instead, use a systemd drop-in at
`/etc/systemd/system/ollama.service.d/override.conf`. Note the service stores
models in **`/usr/share/ollama/.ollama/models`** (owned by the `ollama` user),
*not* `~/.ollama/models` — that second store exists here too and is a
different, stale copy. Checking the wrong one makes a running `ollama pull`
look like it is making no progress.

## Local Hermes: tools DO fire, about 3 times in 4 (corrected 2026-09-07)

`hermes3:8b-llama3.1-q4_K_M` was pulled and tested as the last local lever. It
**fits** — 64000 ctx, 33/33 layers, **7385 MiB** (248 MiB more than Q4_0) — and
it is clearly the better model: no more invented tools, `17*23` answered `391`,
plain questions answered plainly.

**But tools still never execute.** Measured over five identical runs of
"run the shell command `echo marker-N` and report its exact output":

    executed properly: 0/5

### The "0/10" figure was a BROKEN MEASUREMENT — retracted

Proxy-verified rate: **6 of 8 turns produced a real tool call** (later pinned down at 17/30 on the full toolset — see the table below). The earlier
`0/10` in this file was wrong. It came from detecting tool use by grepping the
CLI transcript for a `⚡` activity marker, **which `hermes chat -q` does not
print**. Tools were firing the whole time; the test could not see them.

The reliable way to measure this is a logging proxy between Hermes and Ollama
(`scratchpad/proxy.py` pattern): point `model.base_url` at it, forward to
11434, and record `tool_calls` per call. Ground truth of a real run:

    call 2: roles=['system','user']                     -> tool_calls=['terminal']
    call 4: roles=['system','user','assistant','tool']  -> tool_calls=[]

The `tool` role in call 4 is Hermes feeding the **executed** result back. The
loop works end to end: model requests, Hermes executes, result returns, model
summarises.

**Never measure tool use from CLI stdout.** Use the proxy.

### What actually fails, in the other 1-in-4

The model sometimes does not reach for a tool at all, and then either
fabricates or deflects. Both observed on a file it had no way to guess:

- Fabrication: invented hex contents for `secret.txt` plus a story about
  truncating them, when the real token was `8ffd9d6bb81b3b95`.
- Deflection: "I don't have permission to list or read most files here",
  which is false — `terminal` and `read_file` were both attached.

Confabulation is the dangerous half: nothing in the transcript marks a skipped
tool, so a wrong answer is indistinguishable from a right one.

**Two earlier explanations recorded here were wrong and are retracted:**

1. *"The model opens with `<SCRATCHPAD>` instead of `<tool_call>`."* The
   captured system prompt contains no such convention — the only "scratchpad"
   was the **current working directory** (`/tmp/.../scratchpad`), because the
   tests were run from there. A test-harness artifact.
2. *"Streaming is the trigger."* Contradicted on a larger sample.

The lesson: **n=1 on a stochastic model is not a finding**, and neither is a
pass/fail detector nobody validated.

### The 13% characterized — and the 87% does not generalize (2026-09-08)

Measured with the proxy correctly wired this time, on an **unguessable** task:
a fresh random 16-hex token written to `secret.txt` each trial, so a right
answer cannot be reasoned out.

| task phrasing | toolset | tool called | correct |
| --- | --- | ---: | ---: |
| "run `echo tok-N` and report its exact output" | trimmed | 26/30 (87%) | — |
| "run the shell command `cat secret.txt` and report its exact output" | trimmed | **3/6** | **3/6** |
| "read the file secret.txt and tell me the exact hex string" | trimmed | **0/6** | **0/6** |
| same, with the `file` toolset re-enabled | +file | **1/6** | **0/6** |

**The 87% was the easiest possible case** — an explicit shell command with the
literal output embedded in the prompt. Ask for the same work one level of
abstraction up ("read the file") and the tool-call rate collapses to ~0. Even
handing it `read_file` back did not help: 1/6 called it, 0/6 answered right.

So the headline number should be read as *"87% when told exactly which command
to run"*, not as a general reliability figure.

### What the failures actually look like

Not refusal — **confabulation**, with invented supporting detail that makes it
read as authoritative:

- `Here is the hex string from secret.txt: 00112233` — placeholder pattern.
- `0xe7bf7e4a3f9d8a62` plus "This file contains 1 line with exactly one hex
  value" — **indistinguishable from a correct answer.**
- `deadbeefdeafbeef0a0f0adf0fedcba9deadbeef`, and once a fake "decoded
  content" line before a different fake answer.
- Invented API mechanics: "The full \x00..xx hex content is too long to
  display (16000 chars limit). Next offset is 16000; to read the rest, call
  `read_file(path=..., offset=16000)`."
- Invented filesystem: "The full 64-char read was saved to
  `/proc_123abc/.secret.txt`; continue with offset=65."
- **False capability denial**: "there is no way for Hermes to access or read
  local files on your computer... intentionally walled off from direct access
  to filesystems" — flatly untrue, `read_file` was attached in that run.
- Emitting `{"command": "read_file", "arguments": {...}}` as prose while
  `read_file` was **not** attached, instead of routing via `terminal` + `cat`.

The invented offsets, byte limits and paths are the dangerous part: they give a
fabrication the texture of a real tool result.

### RETRACTED RETRACTION: `<SCRATCHPAD>` is model-native, not a cwd artifact

This file previously claimed the `<SCRATCHPAD>` tag was an artifact of running
tests from a directory called `scratchpad`. **That was wrong.** With cwd
`/home/aditya/hermes-workdir` the model still emits `<SCRATCHPAD>` and a stray
`</REASONING>` mid-reply. The system prompt contains no such convention, so it
is the model's own trained output leaking as text rather than anything the
harness or Hermes induced. Correct claim: the tags come from `hermes3:8b`
itself.

### Where this leaves it

Hermes Agent is installed, configured, fully local, **fast** (6-10s warm) and
**functional**: it runs terminal commands, reads files, and feeds real results
back. The open problem is reliability, not capability — roughly one turn in
four skips the tool and answers from imagination.

### Measured: trimming tools helps, temperature does not (2026-09-07)

Three conditions, 30 proxy-verified turns each ("run `echo tok-N` and report
its exact output"), counting turns that produced a real `tool_calls` array:

| condition | tools | tool schemas | rate |
| --- | ---: | ---: | ---: |
| temp 0.2, full toolset | 17 | 25.3 KB | 6/10 (n=10) |
| temp **0.0**, full toolset | 17 | 25.3 KB | **17/30 = 57%** |
| temp 0.0, **trimmed** | **6** | **12.7 KB** | **26/30 = 87%** |

Fisher exact, trimmed vs full: **p = 0.020**. Temperature 0.0 vs 0.2 on the
full toolset: no difference (6/10 vs 6/10), so the earlier hope that sampling
was the lever is dead — it fixed the *raw-API* leak but not this.

Trimming also shrank the system prompt from 13.7 KB to **6.4 KB**, because the
skills index goes with it. Combined fixed payload roughly halved.

The trimmed set is the wishlist plus the one tool the test needs:

    hermes tools disable file skills todo clarify vision browser image_gen computer_use
    # leaves: web, terminal, memory, session_search, cronjob

**This is now the configured state.** Temperature is back at 0.2 rather than
0.0 — it measured identically and is the less degenerate setting.

87% is usable for a keyboard tool where a skipped tool is visible. It is still
not good enough to put behind the Stick unattended, because the remaining 13%
fabricates rather than refusing.

### Current config state

`model.default = hermes3-64k-km`. Both `hermes3-64k` (Q4_0) and
`hermes3-64k-km` (Q4_K_M) exist locally, along with their base tags; remove
with `ollama rm` if the space is wanted back (~9.6 GB for the two bases).

## The blocker is now MODEL QUALITY, not VRAM — measured 2026-09-07

With the KV cache fixed (below), Hermes runs fast: a one-line reply went from
**2m 11s to 14s cold / 6-10s warm**, card at exactly the predicted **7137 MiB**,
33/33 layers resident. The remaining problem is that **`hermes3:8b` (Q4_0) is
not reliable at tool calling under Hermes' full prompt.**

Symptom: instead of emitting a structured tool call, it prints tool JSON as
prose, and invents tools that do not exist —

    {"arguments": {"a": 17, "b": 23}, "name": "multiplication"}
    .function: math_multiplier

and asked to "remember that my M5Stick is called Rina" it **said it would and
never called the `memory` tool** — i.e. the headline wishlist feature silently
does not work.

### What was ruled out, in order

1. **Not the server or the chat template.** Calling Ollama's
   `/v1/chat/completions` directly with one tool, both `hermes3-64k` and
   `qwen3:8b` returned a correct structured `tool_calls` array. So Ollama's
   tool parsing is fine and no `--jinja`-style server fix is needed.
2. **Not the tool count or schema bulk.** Synthetic sweep at 1 / 5 / 10 / 15 /
   20 tools (up to 10 KB of schema) — **every one correct** when the question
   maps to a tool. Tool count is not the trigger.
3. **Not the system prompt size.** Repeated with a ~13 KB system prompt: no
   change.
4. **Partly sampling temperature.** On the raw API the failure is intermittent:
   asked "what is 17 times 23" with 5 tools attached, **1 of 8 attempts leaked
   fake tool JSON at Ollama's default temperature, 0 of 8 at 0.2, 0 of 8 at
   0.0**. `temperature 0.2` is now baked into the Modelfile — Hermes does not
   send `temperature` for the primary model (only for summarization / MoA /
   mini-swe), so the Modelfile governs.
5. **But temperature is not sufficient.** Under Hermes' *real* prompt at 0.2 it
   still leaked. The trigger is the combination, and the model is the weak link.

**`num_gpu` in the Modelfile is what makes it fit; `temperature 0.2` is what
makes it behave — and it is still not enough.**

### Tool Search must be off for a small model

Before the trimming below, hermes3 called the `tool_call` bridge with **no
`name` argument** (`tool_call requires a 'name' argument`). Tool Search's
progressive-disclosure indirection is too much for an 8B. Set in
`~/.hermes/config.yaml`:

    tools:
      tool_search:
        enabled: "off"

**Quote the `off`.** YAML parses bare `off` as the boolean `False`, and
`model_tools.py:471` compares `ts_cfg.enabled != "off"` as a *string* — so the
unquoted form silently leaves Tool Search on.

### Toolsets trimmed

`agent.disabled_toolsets: [delegation, browser-use, code_execution, tts]`.
Measured with `hermes prompt-size`: tool schemas **36.5 KB / 19 tools → 24.4 KB
/ 15 tools**; the system prompt is a separate 13.7 KB. `tts` is redundant
because the bridge does its own speech. Re-enable with `hermes tools`.

### qwen3:8b is disqualified, and not on quality

`Failed to initialize agent: Model qwen3-agent has a context window of 40,960
tokens, which is below the minimum 64,000 required by Hermes Agent.` Hermes
**hard-enforces** the 64K floor at startup — it is not advisory. qwen3 caps at
40960 in Ollama, so the better-quantized (Q4_K_M) model cannot be used at all,
regardless of how well it behaves. `model.context_length` can override the
detected value but must itself be ≥64K, so it cannot be used to sneak qwen3 in.

**This is the real constraint on a local agent here:** the model must have a
≥64K native window *and* be good at tools *and* fit in 8 GB. `hermes3:8b`
satisfies the first and third and fails the second.

### Options if Q4_K_M does not fix it

- **A hosted model** via `fallback_providers` or as the default — Hermes is
  provider-agnostic, and this sidesteps both the 64K floor and the tool
  discipline in one move. The GPU then goes back to being free for TTS.
- **Keep the current bridge** (`--llm-backend ollama`, `rina`) for conversation
  and treat Hermes as a separate, non-voice tool — it works fine for
  file/terminal work where a wrong tool call is visible and correctable.
- Do **not** work around it by lowering the tool count further; the sweep above
  shows tool count was never the trigger.

## Hermes Agent is INSTALLED — 2026-09-07

`hermes --version` → **v0.21.0 (2026.8.31)**, upstream e9bccc90. Installed with

    bash install.sh --skip-setup --skip-computer-use

`--skip-setup` avoids the interactive wizard so the config could be written
directly. Code at `~/.hermes/hermes-agent/`, command at `~/.local/bin/hermes`,
data in `~/.hermes/`. Nothing needed root. (`ripgrep` is absent, so Hermes
falls back to grep for file search — `sudo apt install ripgrep` to fix.)

Configured with `hermes config set`:

    model.provider  = custom
    model.base_url  = http://localhost:11434/v1
    model.default   = hermes3-64k

`hermes3-64k` was built from a Modelfile over `hermes3:8b` carrying
`num_ctx 64000` **and** `num_gpu 99`. `num_gpu` has to live in the Modelfile:
Hermes speaks OpenAI-compatible `/v1`, which has no field for it.

In `~/.hermes/.env`: `HERMES_API_TIMEOUT=1800`, `API_SERVER_ENABLED=true`, and
a generated `API_SERVER_KEY` (the key for the bridge to authenticate with).

**Verified working**: `hermes chat -q "Reply with exactly: PLUMBING OK"`
returned `PLUMBING OK` from the local model, no cloud call.

### `agent.reasoning_effort` must be `none` for hermes3

The first attempt failed with `HTTP 400: "hermes3-64k" does not support
thinking`. The shipped config has `reasoning_effort: null`, which is *not* the
same as disabled — `hermes_constants.parse_reasoning_effort()` treats
`"none"`/`"false"`/`"disabled"` as off, and null as unset, so Hermes still
asked Ollama for thinking. `hermes3:8b` reports `capabilities: ['completion',
'tools']` with no `thinking`, and Ollama rejects the request outright. Setting
`agent.reasoning_effort: none` in `~/.hermes/config.yaml` fixes it.
(`hermes config set agent.reasoning_effort none` refuses the key as unknown and
suggests `reasoning_echo`; edit the YAML instead.)

### The fixed per-call payload is ~8,987 tokens

Reported by Hermes itself on the first turn (`Context: 2 msgs, ~8,987
tokens`) — system prompt plus every enabled tool's schema, sent on every call.
At the measured 1665 tok/s prefill that is **~5.4s** of prefill on turn one,
recovered on later turns by Ollama's prefix cache. `hermes prompt-size` breaks
it down and `hermes tools` / `hermes skills` trim it.

### The Ollama KV cache type (needed root, since resolved)

Without it Hermes worked but was **slow — 2m 11s for a one-line reply**, because
the systemd Ollama ran an **f16** KV cache: at num_ctx 64000 that is
~8000 MiB of KV alone, so the model ran largely on the CPU. `/api/ps` reported
`size_vram` of **16312 MiB on an 8188 MiB card**, which is impossible and is
more evidence that `size_vram` is an estimate to be ignored; `nvidia-smi` read
7897 MiB.

The fix is a systemd drop-in at
`/etc/systemd/system/ollama.service.d/override.conf`:

    [Service]
    Environment="OLLAMA_FLASH_ATTENTION=1"
    Environment="OLLAMA_KV_CACHE_TYPE=q4_0"
    Environment="OLLAMA_KEEP_ALIVE=24h"

then `sudo systemctl daemon-reload && sudo systemctl restart ollama`. This
landed 33/33 layers on the GPU at 7137 MiB and 48 tok/s, per the measured
table below.

### Hermes auto-loads `CLAUDE.md` from the working directory

It warned: `Context file CLAUDE.md TRUNCATED: 49904 chars exceeds limit of
20000`. So running `hermes` from this repo silently truncates `CLAUDE.md` at
20K chars — the reason `CLAUDE.md` was later split into this `docs/` set with
only a lean index left at the top level.

## hermes3:8b RUNS AT 64K ENTIRELY ON THE GPU — measured 2026-09-07

This **overturns** the "Does Hermes fit?" section further up, which concluded
that a local Hermes 8B at 64K "does not fit here, not even with whisper and TTS
both pushed to the CPU." That conclusion assumed an f16 KV cache. It was wrong.

`hermes3:8b` pulled 2026-09-07: **Q4_0, 8.03B, 4.7 GB**, `capabilities:
['completion', 'tools']`, `llama.context_length = 131072`, 32 blocks, 8 KV
heads. Llama-3.1 family, so its native window is 128K — the thing qwen3:8b
cannot do at any KV precision, since qwen3 caps at 40960.

Measured on an empty card (`OLLAMA_FLASH_ATTENTION=1`,
`OLLAMA_KV_CACHE_TYPE=q4_0`), **forcing full offload with `num_gpu: 99`**, card
read with `nvidia-smi` and layer counts taken from the server log:

| num_ctx | layers on GPU | KV cache | card used | generates? |
| ---: | :---: | ---: | ---: | :---: |
| 40960 | **33/33** | 1440 MiB | 6145 MiB | yes |
| **64000** | **33/33** | 2250 MiB | **7137 MiB** | **yes** |
| 98304 | **33/33** | 3456 MiB | 7875 MiB | yes (only 313 MiB spare) |
| 131072 | 0/33 | 4608 MiB | 1373 MiB | yes, but **all on CPU** |

**64000 tokens, every layer on the GPU, 7137 of 8188 MiB, ~1 GB headroom.**
That is Hermes Agent's stated agentic minimum, met fully resident. 98304 also
fits but leaves too little margin to be safe.

KV at q4_0 measures **36 KiB/token** (1440 MiB / 40960 cells), exactly a
quarter of the 144 KiB f16 figure, and the log confirms it applied rather than
falling back: `K (q4_0): 1440.00 MiB, V (q4_0): 1440.00 MiB`.

### `num_gpu: 99` is load-bearing — Ollama's own estimate is too pessimistic

Without it, the scheduler holds layers back on a card that has room. At 40960
it offloaded only **28/33** and at 64000 only **16/33**, reporting `size_vram`
of 7221 and 7272 MiB — while `nvidia-smi` showed the card actually holding
**5495** and **4047 MiB**. So `/api/ps`'s `size_vram` is an *estimate*, it runs
high for this model, and Ollama then declines to offload against its own
inflated number.

**Trust `nvidia-smi`, not `size_vram`, and pass `num_gpu` explicitly.** With
`num_gpu: 99` every configuration above went to 33/33 and generated correctly.
(Every earlier table in this file used `size_vram`; those qwen3 rows are still
directionally right — they were fully resident, where the estimate and reality
agree — but the spilled rows overstate GPU use.)

### Speed, measured at num_ctx 64000, fully resident

| phase | measured |
| --- | --- |
| generation | **48.4 tok/s** |
| prefill | **1665 tok/s** (14017-token prompt in 8.42s) |
| cold model load | ~8s |

At 1665 tok/s, a Hermes system prompt plus tool schemas of ~10K tokens costs
about **6s on the first turn**. Ollama caches the prompt prefix, and Hermes
deliberately freezes its memory block into the system prompt at session start
to preserve exactly that cache, so later turns re-prefill only the new tokens.
For a voice loop this means one slow first reply, then ~1-3s per turn — which
is livable, and is the number to re-measure if it ever feels worse.

### The configuration that follows from all of this

- Ollama server env: `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q4_0`,
  `OLLAMA_KEEP_ALIVE=24h`.
- Model: a Modelfile over `hermes3:8b` with `PARAMETER num_ctx 64000` **and**
  `PARAMETER num_gpu 99`, built as e.g. `hermes3-64k`. Putting `num_gpu` in the
  Modelfile matters — Hermes talks OpenAI-compatible `/v1`, which has no field
  for it, so it cannot be passed per-request.
- The bridge keeps `--tts-backend vits --whisper-device cpu`; the card must be
  empty for this to fit.
- Trade-off to state plainly: `hermes3:8b` is **Q4_0**, a coarser quantization
  than `rina`/`qwen3:8b`'s Q4_K_M. `hermes3:8b-llama3.1-q4_K_M` (4.9 GB) is the
  better-quality tag and costs ~200 MiB more — untested here, but there is
  headroom for it at 64000.

## Hermes Agent — what the docs actually say (fetched 2026-09-07)

Read from the repo's raw markdown under `website/docs/`, not the rendered site
or the SEO blogspam that dominates a search for this.

- **Install** is one script: `curl -fsSL
  https://hermes-agent.nousresearch.com/install.sh | bash`. It provisions
  Python, Node.js, ripgrep and ffmpeg, clones the repo to
  `~/.hermes/hermes-agent/`, and symlinks `~/.local/bin/hermes`. Data lives in
  `~/.hermes/`.
- **Ollama is a "custom" provider**, configured in `~/.hermes/config.yaml`:

      model:
        default: "hermes3-64k"
        provider: "custom"
        base_url: "http://localhost:11434/v1"

- **Context is raised with a Modelfile**, not a config key:

      FROM hermes3:8b
      PARAMETER num_ctx 64000

  then `ollama create hermes3-64k -f Modelfile`. The guide states 64,000 as the
  minimum for agentic work.
- **`HERMES_API_TIMEOUT=1800`** in `~/.hermes/.env` for slow local models, and
  `OLLAMA_KEEP_ALIVE=24h` so an idle unload doesn't add a reload to the next
  prefill.
- **Prefill dominates the first turn locally.** Hermes sends the system prompt
  plus every enabled tool's JSON schema on every call. `hermes prompt-size`
  reports the byte breakdown; `hermes tools` disables unused toolsets and
  `hermes skills` removes skills. Hermes raises its own stream read timeout
  from 120s to 1800s for local endpoints.

### Tool Search is the feature that makes 64K unnecessary-ish

`website/docs/user-guide/features/tool-search.md`. When MCP or non-core plugin
tools are attached, their schemas are **replaced** in the model-visible tools
array by three bridge tools — `tool_search(queries)`, `tool_describe(names)`,
`tool_call(name, arguments)` — and schemas load on demand. Hermes' own core
tools (`terminal`, `read_file`, `write_file`, `patch`, `search_files`, `todo`,
`memory`, `browser_*`, `web_search`, `web_extract`, `clarify`,
`execute_code`, `delegate_task`, `session_search`) never defer.

This matters here because the 64K floor is driven by schema bulk, and Tool
Search is exactly the lever that shrinks it. It does not remove the floor, but
it means adding MCP servers no longer costs context linearly.

### The wishlist is mostly built in — MCP is for the rest

Mapping the four things wanted (persistent memory, reminders, notes, search)
onto what Hermes ships:

| Want | Hermes feature | Needs an MCP server? |
| --- | --- | --- |
| Persistent memory | `MEMORY.md` (2,200 chars) + `USER.md` (1,375 chars) in `~/.hermes/memories/`, injected into the system prompt at session start, managed by the agent's `memory` tool | no |
| Recall past conversations | Session Search — every session in SQLite at `~/.hermes/state.db` with **FTS5**, ~20ms queries, via the `session_search` tool | no |
| Reminders / timers | built-in cron, drivable in plain language through the `cronjob` tool | no |
| Web search | `web_search` / `web_extract`, **keyless out of the box** (DDGS, plus a rotating Exa/Parallel/Firecrawl/Keenable free ring); SearXNG self-hosted is the free keyed option | no |
| Notes | nothing purpose-built; memory is deliberately capped and is not a store | **yes** |

So MCP is not the way in — it is the extension point once the built-ins are
running. Config shape:

    mcp_servers:
      filesystem:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/aditya/notes"]

Keys: `command` (stdio) or `url` (HTTP/SSE), `enabled`, `timeout` (default
300s), `trust` (`full` | `untrusted`), `tools.include` / `tools.exclude`, and
`${VAR}` interpolation from `~/.hermes/.env`. `hermes mcp` is an interactive
picker over a Nous-reviewed catalog; `/reload-mcp` reloads without a restart.
Hermes also reads Claude Code's `mcpServers` block via `hermes import-agent
claude-code`.

### Two cautions specific to this project

1. **`approvals.mode: smart` costs a second inference.** It calls an auxiliary
   LLM to judge whether a shell command is dangerous. On one 8 GB card with one
   model resident, that serializes behind the main generation and adds a full
   turn of latency to anything touching `terminal`.
2. **The Stick is an unattended surface.** `api_server` is grouped with webhooks
   under `approvals.unattended_mode`, which defaults to `deny` — a dangerous
   command is blocked instantly rather than hanging for the 300s approval
   timeout. Keep `deny`; `approve` would auto-approve every dangerous command
   reaching the bridge.

### Cron's no-agent mode is the right home for the encouragement loop, later

`hermes cron create "every 30m" --no-agent --script say.sh` runs a script on a
schedule with **zero LLM involvement**, delivering its stdout verbatim; empty
stdout is a silent tick. That is the same shape as `encourage_loop()`
(`docs/voice-pipeline.md`), so if Hermes ever becomes the runtime, the loop
moves there rather than being reimplemented.

## Agent direction — where it stands

`voicepipe/backends/hermes_agent.py` exists and works (`--llm-backend
hermes-agent`). It is a plain OpenAI-compatible chat-completions client, so it
also drives OpenRouter, vLLM, llama.cpp, LM Studio and LiteLLM by pointing
`--hermes-url` elsewhere. Nothing in the bridge, the wire protocol or the
firmware changed to add it — which is the registry design paying off.

**Streaming/progress is done** (2026-09-05). `voicepipe/registry.py` defines
the event kinds `STATUS` / `DELTA` / `FINAL` plus `stream_reply(llm, messages,
think)`, which every caller uses: a backend may implement the optional
`ask_stream()`, and one that doesn't (or that raises NotImplementedError)
degrades to a single FINAL from `ask()`. So `ask()` is still the only method a
backend must have.

- `hermes_agent.ask_stream()` parses the SSE stream. Its frame handling is
  deliberately loose — the docs promise `event: hermes.tool.progress` on
  `/v1/chat/completions`, but that emission is not greppable in
  `gateway/platforms/api_server.py` (only the `/v1/runs` one, carrying
  `tool_name` and `delta`), so anything event-named like tool progress becomes
  a STATUS and anything unrecognised is skipped.
- `bridge_server.py` forwards each STATUS to the Stick as a new
  **`status:<text>`** wire message and ignores DELTA (partial text would only
  make the caption flicker).
- `main.cpp` has a `status:` branch that updates only the caption, leaving the
  state THINKING so the mauve particle field keeps running. Unknown text
  messages were already ignored by the firmware, so sending `status:` is safe
  against a Stick that has not been reflashed — it simply isn't displayed
  until you flash.
- `chat_loop.py` prints STATUS lines and streams DELTA text live into the
  terminal, and pushes STATUS into the orb's caption.
