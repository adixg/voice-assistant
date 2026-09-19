#!/usr/bin/env python3
"""Measure streaming latency through agent or an OpenAI-compatible server.

Examples (run from a pod/network namespace that can resolve ClusterIP names):

    python benchmarks/latency/benchmark_streaming.py --kind agent \
      --base-url http://agent:8002 --repetitions 10
    python benchmarks/latency/benchmark_streaming.py --kind openai \
      --base-url http://llama-cpp-rtx4060:8080/v1 --model qwen3-8b

The harness deliberately reports characters/sec, not fabricated tokens/sec:
the agent's NDJSON contract does not expose tokenizer usage.  Pair this with
llama-server's own metrics/logs if exact generated-token counts are required.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_PROMPTS = [
    "Reply with exactly: benchmark one",
    "In one short sentence, explain why the sky looks blue.",
    "Give three concise steps for making tea.",
    "What is 17 multiplied by 23? Reply with only the number.",
    "Write one friendly sentence greeting a user.",
]


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p / 100
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def read_prompts(path: str | None) -> list[str]:
    if not path:
        return DEFAULT_PROMPTS
    prompts = [line.strip() for line in Path(path).read_text().splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    if not prompts:
        raise ValueError(f"no prompts found in {path}")
    return prompts


def event_stream(kind: str, base_url: str, model: str, prompt: str, timeout: float):
    """Yield text chunks from the project's agent NDJSON or OpenAI SSE API."""
    if kind == "agent":
        url = base_url.rstrip("/") + "/ask_stream"
        payload = {"messages": [{"role": "user", "content": prompt}], "think": False}
    else:
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "stream": True}
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if kind == "agent":
                if not line:
                    continue
                event = json.loads(line)
                if event.get("kind") == "delta":
                    yield event.get("text", "")
            else:
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                event = json.loads(data)
                yield event.get("choices", [{}])[0].get("delta", {}).get("content") or ""


def measure(kind: str, base_url: str, model: str, prompt: str, timeout: float) -> dict:
    started = time.perf_counter()
    first_chunk_at = None
    chunks: list[str] = []
    error = None
    try:
        for chunk in event_stream(kind, base_url, model, prompt, timeout):
            if chunk:
                first_chunk_at = first_chunk_at or time.perf_counter()
                chunks.append(chunk)
    except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        error = str(exc)
    ended = time.perf_counter()
    total_s = ended - started
    ttft_s = first_chunk_at - started if first_chunk_at else None
    text = "".join(chunks)
    generation_s = ended - first_chunk_at if first_chunk_at else None
    return {
        "prompt": prompt,
        "ok": error is None and bool(text),
        "error": error,
        "ttft_ms": round(ttft_s * 1000, 3) if ttft_s is not None else None,
        "total_ms": round(total_s * 1000, 3),
        "generation_ms": round(generation_s * 1000, 3) if generation_s is not None else None,
        "output_chars": len(text),
        "output_chars_per_s": round(len(text) / generation_s, 3) if generation_s else None,
        "reply": text,
    }


def write_results(records: list[dict], output_dir: Path, metadata: dict) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path, csv_path = output_dir / f"{stem}.json", output_dir / f"{stem}.csv"
    json_path.write_text(json.dumps({"metadata": metadata, "records": records}, indent=2) + "\n")
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("agent", "openai"), default="agent")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", default="local-model", help="used only with --kind openai")
    parser.add_argument("--prompts-file", help="one prompt per line")
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--repetitions", type=int, default=10, help="measurements per prompt")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmarks/results/latency"))
    args = parser.parse_args()
    if args.warmups < 0 or args.repetitions < 1:
        parser.error("--warmups must be >= 0 and --repetitions must be >= 1")
    prompts = read_prompts(args.prompts_file)
    for prompt in prompts:
        for _ in range(args.warmups):
            result = measure(args.kind, args.base_url, args.model, prompt, args.timeout)
            if not result["ok"]:
                print(f"warmup failed: {result['error']}", file=sys.stderr)
                return 1
    records = [measure(args.kind, args.base_url, args.model, prompt, args.timeout)
               for prompt in prompts for _ in range(args.repetitions)]
    metadata = {"kind": args.kind, "base_url": args.base_url, "model": args.model,
                "warmups": args.warmups, "repetitions": args.repetitions,
                "timestamp_utc": datetime.now(UTC).isoformat()}
    json_path, csv_path = write_results(records, args.output_dir, metadata)
    successful = [row for row in records if row["ok"]]
    ttft = [row["ttft_ms"] for row in successful if row["ttft_ms"] is not None]
    total = [row["total_ms"] for row in successful]
    print(f"results: {json_path} and {csv_path}")
    print(f"success: {len(successful)}/{len(records)}")
    for label, values in (("TTFT ms", ttft), ("total ms", total)):
        if values:
            print(f"{label}: p50={percentile(values, 50):.1f} p95={percentile(values, 95):.1f} "
                  f"mean={statistics.mean(values):.1f} max={max(values):.1f}")
    return 0 if len(successful) == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
