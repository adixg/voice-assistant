#!/usr/bin/env python3
"""Time a voice turn stage by stage, for example sentences, across backends.

A turn on the k3s path is strictly sequential (see services/gateway/app.py):
STT -> the full LLM reply -> one TTS call for the whole reply. So the stage
durations below add up to the delay the person actually waits, minus only the
gateway's resample and the BLE hop to the Stick.

For each sentence the harness:
  1. speaks it once with a TTS service to get realistic input audio (untimed,
     cached -- it stands in for the person talking),
  2. times STT on that audio,
  3. times every LLM target on the transcript,
  4. times every TTS target on each LLM's reply,
and reports per-stage and whole-turn latency for every LLM x TTS combination.

It calls the services directly rather than driving the gateway WebSocket: the
gateway's speaker gate would (correctly) reject a synthetic voice before the
LLM ever ran.

Examples:

    # interactive: type sentences, see each breakdown immediately
    python benchmarks/pipeline/bench_turn.py --cluster-ssh arch-ssd.<tailnet>.ts.net

    # batch, 3 repetitions each, default targets (agent route + both GPUs)
    python benchmarks/pipeline/bench_turn.py --cluster-ssh arch-ssd.<tailnet>.ts.net \\
        --repetitions 3 "Hey, how was your day?" "Remind me why the sky is blue."

    # compare thinking on vs off on the 4060, text only (skip STT)
    python benchmarks/pipeline/bench_turn.py --cluster-ssh arch-ssd.<tailnet>.ts.net --no-stt \\
        --llm 4060=http://llama-cpp-rtx4060:8080/v1,model=qwen3-8b,think=off \\
        --llm 4060-think=http://llama-cpp-rtx4060:8080/v1,model=qwen3-8b,think=on \\
        "Say hi in three words."

`--cluster-ssh HOST` resolves service names (agent, stt, tts, llama-cpp-*) to
their ClusterIPs via `kubectl` on HOST, which works from any k3s node; inside
a pod the names resolve through cluster DNS and the flag isn't needed.

Standard library only, Python 3.10+. Results go to benchmarks/results/pipeline/
(gitignored) as JSON + CSV.
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

NAMESPACE = "aicompanion"

DEFAULT_LLMS = [
    "agent=http://agent:8002,kind=agent",
    "gtx1650=http://llama-cpp-gtx1650:8080/v1,model=qwen3.5-4b,think=off",
    "rtx4060=http://llama-cpp-rtx4060:8080/v1,model=qwen3-8b,think=off",
]
DEFAULT_TTS = ["kokoro=http://tts:8003"]
DEFAULT_STT = "http://stt:8001"


# ---------------------------------------------------------------- targets

@dataclass
class LLMTarget:
    name: str
    url: str
    kind: str = "openai"          # "openai" (llama.cpp etc.) or "agent" (our agent service)
    model: str = "local-model"
    think: str = "off"            # "off" | "on" | "default" (send nothing, server decides)
    extra: dict = field(default_factory=dict)


def parse_llm(spec: str) -> LLMTarget:
    """NAME=URL[,model=M][,kind=openai|agent][,think=off|on|default]."""
    if "=" not in spec:
        raise ValueError(f"LLM target must look like NAME=URL[,key=value...]: {spec!r}")
    name, rest = spec.split("=", 1)
    url, *opts = rest.split(",")
    target = LLMTarget(name=name.strip(), url=url.strip())
    for opt in opts:
        key, _, value = opt.partition("=")
        key, value = key.strip(), value.strip()
        if key == "model":
            target.model = value
        elif key == "kind":
            if value not in ("openai", "agent"):
                raise ValueError(f"kind must be openai or agent, got {value!r}")
            target.kind = value
        elif key == "think":
            if value not in ("off", "on", "default"):
                raise ValueError(f"think must be off, on or default, got {value!r}")
            target.think = value
        else:
            raise ValueError(f"unknown LLM option {key!r} in {spec!r}")
    return target


def parse_named_url(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise ValueError(f"target must look like NAME=URL: {spec!r}")
    name, url = spec.split("=", 1)
    return name.strip(), url.strip()


def resolve_cluster_urls(urls: list[str], ssh_host: str) -> dict[str, str]:
    """Map in-cluster service hostnames in `urls` to ClusterIPs via kubectl over ssh."""
    out = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", ssh_host,
         f"KUBECONFIG=~/.kube/config kubectl get svc -n {NAMESPACE} -o json"],
        capture_output=True, text=True, timeout=60, check=True).stdout
    ips = {item["metadata"]["name"]: item["spec"]["clusterIP"]
           for item in json.loads(out)["items"]}
    return {u: rewrite_host(u, ips) for u in urls}


def rewrite_host(url: str, ips: dict[str, str]) -> str:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if host not in ips:
        return url
    netloc = ips[host] + (f":{parts.port}" if parts.port else "")
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


# ---------------------------------------------------------------- HTTP stages

def _post_json(url: str, payload: dict, timeout: float):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def tts_synth(base_url: str, text: str, timeout: float) -> list[bytes]:
    with _post_json(base_url.rstrip("/") + "/synth", {"text": text}, timeout) as resp:
        body = json.loads(resp.read())
    return [base64.b64decode(chunk) for chunk in body["chunks_b64"]]


def wav_seconds(chunks: list[bytes]) -> float:
    total = 0.0
    for chunk in chunks:
        with wave.open(io.BytesIO(chunk), "rb") as w:
            total += w.getnframes() / w.getframerate()
    return total


def multipart(field_name: str, filename: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def stt_transcribe(base_url: str, wav: bytes, timeout: float) -> str:
    body, ctype = multipart("audio", "turn.wav", wav, "audio/wav")
    req = urllib.request.Request(base_url.rstrip("/") + "/transcribe", data=body,
                                 headers={"content-type": ctype}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["text"]


def llm_request(target: LLMTarget, messages: list[dict]) -> tuple[str, dict]:
    if target.kind == "agent":
        # The agent service takes our own `think` flag; the gateway sends False.
        think = {"off": False, "on": None, "default": False}[target.think]
        return target.url.rstrip("/") + "/ask_stream", {"messages": messages, "think": think}
    payload = {"model": target.model, "messages": messages, "stream": True}
    if target.think in ("off", "on"):
        # llama.cpp's per-request switch for Qwen3-style chat templates.
        payload["chat_template_kwargs"] = {"enable_thinking": target.think == "on"}
    return target.url.rstrip("/") + "/chat/completions", payload


def llm_stream(target: LLMTarget, messages: list[dict], timeout: float) -> dict:
    """Stream one reply; return reply text, first-token times, and reasoning size."""
    url, payload = llm_request(target, messages)
    started = time.perf_counter()
    first_any = first_content = None
    reply: list[str] = []
    reasoning_chars = 0
    with _post_json(url, payload, timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            if target.kind == "agent":
                event = json.loads(line)
                kind, text = event.get("kind"), event.get("text", "")
                if kind == "delta" and text:
                    first_any = first_any or time.perf_counter()
                    first_content = first_content or time.perf_counter()
                    reply.append(text)
                elif kind == "final":
                    # MCP-enabled agents return only a final event (no deltas).
                    first_any = first_any or time.perf_counter()
                    first_content = first_content or time.perf_counter()
                    reply = [text]
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            delta = (json.loads(data).get("choices") or [{}])[0].get("delta", {})
            thought, content = delta.get("reasoning_content"), delta.get("content")
            if thought:
                first_any = first_any or time.perf_counter()
                reasoning_chars += len(thought)
            if content:
                first_any = first_any or time.perf_counter()
                first_content = first_content or time.perf_counter()
                reply.append(content)
    ended = time.perf_counter()
    ms = lambda t: round((t - started) * 1000, 1) if t else None  # noqa: E731
    return {"reply": "".join(reply).strip(), "llm_first_token_ms": ms(first_any),
            "llm_first_content_ms": ms(first_content), "llm_ms": ms(ended),
            "reasoning_chars": reasoning_chars}


# ---------------------------------------------------------------- the benchmark

def service_config(base_url: str, timeout: float = 10) -> dict:
    """Which backend a stt/tts service is running, from its /health: the
    registry name and every option it was started with (defaults included).
    Recorded with each result so a saved run says what it measured."""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (OSError, ValueError) as exc:
        return {"name": None, "error": str(exc)}
    return body.get("config") or {"name": body.get("backend"), "options": {}}


def describe(config: dict | None) -> str:
    """'kitten (kitten_speed=1.6, kitten_voice=Bella, ...)' for printing."""
    if not config:
        return "-"
    opts = ", ".join(f"{k}={v}" for k, v in sorted((config.get("options") or {}).items()))
    return f"{config.get('name')}" + (f" ({opts})" if opts else "")


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5, check=True).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p / 100
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


class Bench:
    def __init__(self, args, llms: list[LLMTarget], ttss: list[tuple[str, str]],
                 stt_backend: str | None = None, tts_backends: dict[str, str] | None = None):
        self.args, self.llms, self.ttss = args, llms, ttss
        self.stt_backend, self.tts_backends = stt_backend, tts_backends or {}
        self.input_audio: dict[str, bytes] = {}
        self.records: list[dict] = []

    def spoken(self, sentence: str) -> bytes:
        """The sentence as input audio -- untimed, cached per sentence."""
        if sentence not in self.input_audio:
            chunks = tts_synth(self.args.input_tts, sentence, self.args.timeout)
            self.input_audio[sentence] = chunks[0] if len(chunks) == 1 else join_wavs(chunks)
        return self.input_audio[sentence]

    def messages(self, text: str) -> list[dict]:
        msgs = [{"role": "system", "content": self.args.system}] if self.args.system else []
        return msgs + [{"role": "user", "content": text}]

    def turn(self, sentence: str, rep: int) -> list[dict]:
        rows, stt_ms, heard = [], 0.0, sentence
        if not self.args.no_stt:
            try:
                audio = self.spoken(sentence)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                return [{"sentence": sentence, "rep": rep, "heard": "", "llm": t.name,
                         "ok": False, "error": f"input tts: {exc}"} for t in self.llms]
            try:
                t0 = time.perf_counter()
                heard = stt_transcribe(self.args.stt, audio, self.args.timeout)
                stt_ms = round((time.perf_counter() - t0) * 1000, 1)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                return [{"sentence": sentence, "rep": rep, "heard": "", "llm": t.name,
                         "ok": False, "error": f"stt: {exc}"} for t in self.llms]
        for llm in self.llms:
            base = {"sentence": sentence, "rep": rep, "heard": heard, "stt_ms": stt_ms,
                    "stt_backend": self.stt_backend, "llm": llm.name, "llm_think": llm.think}
            try:
                result = llm_stream(llm, self.messages(heard), self.args.timeout)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                rows.append({**base, "ok": False, "error": f"llm: {exc}"})
                continue
            for tts_name, tts_url in self.ttss:
                row = {**base, **result, "tts": tts_name, "tts_backend": self.tts_backends.get(tts_name),
                       "reply_chars": len(result["reply"])}
                if self.args.no_tts or not result["reply"]:
                    row.update(tts_ms=0.0, audio_s=0.0)
                else:
                    try:
                        t0 = time.perf_counter()
                        chunks = tts_synth(tts_url, result["reply"], self.args.timeout)
                        row["tts_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                        row["audio_s"] = round(wav_seconds(chunks), 3)
                        if row["tts_ms"]:
                            row["tts_x_realtime"] = round(row["audio_s"] / (row["tts_ms"] / 1000), 3)
                    except (OSError, ValueError, urllib.error.URLError) as exc:
                        rows.append({**row, "ok": False, "error": f"tts: {exc}"})
                        continue
                row["turn_ms"] = round(row["stt_ms"] + row["llm_ms"] + row["tts_ms"], 1)
                row.update(ok=bool(result["reply"]), error=None if result["reply"] else "empty reply")
                rows.append(row)
        return rows

    def warm_up(self, sentence: str):
        for _ in range(self.args.warmups):
            for row in self.turn(sentence, rep=-1):
                if not row.get("ok"):
                    print(f"  warm-up {row['llm']}/{row.get('tts', '-')}: {row['error']}", file=sys.stderr)

    def run(self, sentence: str):
        rows = []
        for rep in range(self.args.repetitions):
            rows += self.turn(sentence, rep)
        self.records += rows
        print_rows(rows)


def join_wavs(chunks: list[bytes]) -> bytes:
    out, params, frames = io.BytesIO(), None, []
    for chunk in chunks:
        with wave.open(io.BytesIO(chunk), "rb") as w:
            params = params or w.getparams()
            frames.append(w.readframes(w.getnframes()))
    with wave.open(out, "wb") as w:
        w.setparams(params)
        for f in frames:
            w.writeframes(f)
    return out.getvalue()


def fmt_s(ms) -> str:
    return "     -" if ms is None else f"{ms / 1000:6.2f}"


def print_rows(rows: list[dict]):
    if not rows:
        return
    first = rows[0]
    print(f"\n> {first['sentence']}")
    if first["heard"] and first["heard"] != first["sentence"]:
        print(f"  heard: {first['heard']}")
    print(f"  {'llm':<14}{'tts':<10}{'stt s':>7}{'1st tok':>8}{'llm s':>7}{'tts s':>7}"
          f"{'TURN s':>8}{'audio s':>8}  reply")
    for r in rows:
        if not r.get("ok"):
            print(f"  {r['llm']:<14}{r.get('tts', '-'):<10}  FAILED: {r['error']}")
            continue
        think = f" ({r['reasoning_chars']} chars hidden reasoning)" if r.get("reasoning_chars") else ""
        print(f"  {r['llm']:<14}{r['tts']:<10}{fmt_s(r['stt_ms']):>7}{fmt_s(r['llm_first_token_ms']):>8}"
              f"{fmt_s(r['llm_ms']):>7}{fmt_s(r['tts_ms']):>7}{fmt_s(r['turn_ms']):>8}"
              f"{r['audio_s']:>8.2f}  {r['reply'][:60]!r}{think}")


def summarize(records: list[dict]) -> list[dict]:
    combos: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        combos.setdefault((r["llm"], r.get("tts", "-")), []).append(r)
    summary = []
    for (llm, tts), rows in combos.items():
        ok = [r for r in rows if r.get("ok")]
        entry = {"llm": llm, "tts": tts, "stt_backend": rows[0].get("stt_backend"),
                 "tts_backend": rows[0].get("tts_backend"), "ok": len(ok), "n": len(rows)}
        for key in ("stt_ms", "llm_ms", "tts_ms", "turn_ms", "tts_x_realtime", "audio_s"):
            vals = [r[key] for r in ok if r.get(key) is not None]
            entry[f"{key}_p50"] = percentile(vals, 50)
            entry[f"{key}_p95"] = percentile(vals, 95)
            entry[f"{key}_mean"] = round(sum(vals) / len(vals), 3) if vals else None
            entry[f"{key}_min"] = min(vals) if vals else None
            entry[f"{key}_max"] = max(vals) if vals else None
        summary.append(entry)
    return summary


def print_summary(summary: list[dict]):
    if not summary:
        return
    print(f"\n{'llm':<14}{'tts':<10}{'ok':>6}{'stt p50':>9}{'llm p50':>9}{'tts p50':>9}"
          f"{'TURN p50':>10}{'TURN p95':>10}")
    for s in sorted(summary, key=lambda s: s["turn_ms_p50"] or float("inf")):
        print(f"{s['llm']:<14}{s['tts']:<10}{s['ok']:>3}/{s['n']:<2}{fmt_s(s['stt_ms_p50']):>9}"
              f"{fmt_s(s['llm_ms_p50']):>9}{fmt_s(s['tts_ms_p50']):>9}"
              f"{fmt_s(s['turn_ms_p50']):>10}{fmt_s(s['turn_ms_p95']):>10}")


def save(records: list[dict], summary: list[dict], metadata: dict, output_dir: Path) -> Path | None:
    if not records:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem.with_suffix(".json").write_text(json.dumps(
        {"metadata": metadata, "summary": summary, "records": records}, indent=2) + "\n")
    fields = sorted({k for r in records for k in r})
    with stem.with_suffix(".csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    return stem


def keep(summary: list[dict], metadata: dict, label: str, runs_dir: Path) -> Path:
    """A small, committable record of one run: its metadata (backends, options,
    commit, sample counts) and summary statistics, without the raw rows. Raw
    JSON/CSV stay in the gitignored --output-dir."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:60] or "run"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{slug}.json"
    path.write_text(json.dumps({"label": label, "metadata": metadata, "summary": summary},
                               indent=2) + "\n")
    return path


def read_sentences(args) -> list[str]:
    sentences = list(args.sentences)
    if args.sentences_file:
        sentences += [line.strip() for line in Path(args.sentences_file).read_text().splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
    return sentences


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sentences", nargs="*", help="example sentences; none + a terminal = interactive")
    ap.add_argument("--sentences-file", help="one sentence per line (# comments allowed)")
    ap.add_argument("--llm", action="append", metavar="SPEC",
                    help="NAME=URL[,model=M][,kind=openai|agent][,think=off|on|default]; repeatable. "
                         "Default: the agent route plus both llama.cpp servers, thinking off")
    ap.add_argument("--tts", action="append", metavar="NAME=URL", help="TTS service to compare; repeatable")
    ap.add_argument("--stt", default=DEFAULT_STT, help=f"STT service (default {DEFAULT_STT})")
    ap.add_argument("--input-tts", help="TTS service used to speak the input sentence (default: first --tts)")
    ap.add_argument("--no-stt", action="store_true", help="feed the sentence straight to the LLM")
    ap.add_argument("--no-tts", action="store_true", help="skip speaking the replies")
    ap.add_argument("--system", help="optional system prompt sent to every LLM target")
    ap.add_argument("--repetitions", type=int, default=1, help="timed runs per sentence (default 1)")
    ap.add_argument("--warmups", type=int, default=1, help="untimed full passes before measuring (default 1)")
    ap.add_argument("--timeout", type=float, default=300, help="per-request timeout, seconds")
    ap.add_argument("--cluster-ssh", metavar="HOST",
                    help="resolve service names to ClusterIPs via kubectl on HOST")
    ap.add_argument("--output-dir", type=Path, default=Path("benchmarks/results/pipeline"),
                    help="raw JSON/CSV per run (gitignored)")
    ap.add_argument("--keep", metavar="LABEL",
                    help="also write a small summary to --runs-dir, meant to be committed")
    ap.add_argument("--runs-dir", type=Path, default=Path("benchmarks/runs/pipeline"))
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.repetitions < 1 or args.warmups < 0:
        print("--repetitions must be >= 1 and --warmups >= 0", file=sys.stderr)
        return 2
    try:
        llms = [parse_llm(s) for s in (args.llm or DEFAULT_LLMS)]
        ttss = [parse_named_url(s) for s in (args.tts or DEFAULT_TTS)]
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    args.input_tts = args.input_tts or ttss[0][1]

    if args.cluster_ssh:
        urls = [t.url for t in llms] + [u for _, u in ttss] + [args.stt, args.input_tts]
        mapping = resolve_cluster_urls(urls, args.cluster_ssh)
        for t in llms:
            t.url = mapping[t.url]
        ttss = [(n, mapping[u]) for n, u in ttss]
        args.stt, args.input_tts = mapping[args.stt], mapping[args.input_tts]

    stt_config = None if args.no_stt else service_config(args.stt)
    tts_configs = {} if args.no_tts else {name: service_config(url) for name, url in ttss}
    bench = Bench(args, llms, ttss, stt_backend=stt_config and stt_config.get("name"),
                  tts_backends={n: c.get("name") for n, c in tts_configs.items()})
    sentences = read_sentences(args)
    interactive = not sentences and sys.stdin.isatty()
    if not sentences and not interactive:
        sentences = [line.strip() for line in sys.stdin if line.strip()]

    print("targets:  LLM " + ", ".join(f"{t.name}(think={t.think})" for t in llms)
          + "  |  TTS " + ", ".join(n for n, _ in ttss)
          + ("" if args.no_stt else "  |  STT on"), flush=True)
    if stt_config:
        print(f"stt backend: {describe(stt_config)}", flush=True)
    for name, config in tts_configs.items():
        print(f"tts backend [{name}]: {describe(config)}", flush=True)
    try:
        first = sentences[0] if sentences else "Hello there, how are you today?"
        if args.warmups:
            print(f"warming up ({args.warmups} pass)...", flush=True)
            bench.warm_up(first)
        if interactive:
            print("type a sentence and press enter (empty line or Ctrl-D to finish)")
            while True:
                try:
                    sentence = input("\nsentence> ").strip()
                except EOFError:
                    break
                if not sentence:
                    break
                bench.run(sentence)
        else:
            for sentence in sentences:
                bench.run(sentence)
    except KeyboardInterrupt:
        print("\ninterrupted -- saving what was measured")

    summary = summarize(bench.records)
    print_summary(summary)
    metadata = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "llms": [vars(t) for t in llms], "tts": ttss, "stt": None if args.no_stt else args.stt,
                "repetitions": args.repetitions, "warmups": args.warmups, "system": args.system,
                "stt_backend": stt_config, "tts_backends": tts_configs, "git_commit": git_commit(),
                "sentences": sentences, "records": len(bench.records)}
    stem = save(bench.records, summary, metadata, args.output_dir)
    if stem:
        print(f"\nsaved {stem}.json and {stem}.csv")
    if args.keep and summary:
        print(f"kept {keep(summary, metadata, args.keep, args.runs_dir)}")
    failed = sum(1 for r in bench.records if not r.get("ok"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
