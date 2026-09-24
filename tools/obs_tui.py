#!/usr/bin/env python3
"""Terminal dashboard for the aicompanion cluster: a few MB of RAM, not a browser.

    python tools/obs_tui.py                    # everything, refreshing every 5s
    python tools/obs_tui.py --no-latency       # turn any panel off
    python tools/obs_tui.py --only gpu,memory  # or list exactly the ones you want
    python tools/obs_tui.py --once             # print one frame and exit
    python tools/obs_tui.py --history 2h       # graphs look back 2h (default 15m)
    python tools/obs_tui.py --no-graphs        # bars only

Panels (each has --NAME / --no-NAME, all on by default):
    memory   this machine's RAM, swap and kernel memory pressure (/proc, no
             Prometheus needed)
    gpu      per-GPU utilisation, VRAM, temperature and power (DCGM)
    pods     per-pod readiness, restarts, OOM kills, and memory vs its limit
    latency  gateway turn and per-stage latency, and HTTP latency by route
    traces   the last few real Stick turns from Tempo, split into stt / agent
             (LLM) / tts. Off by default, because Tempo is normally paused:
             run `tools/lean-mode.sh traces` first, then `--traces`.

Graphs are one-line sparklines next to the bars (▁▂▃▄▅▆▇█): GPU util and VRAM,
and the Python services' memory against their limits, come from Prometheus
history; the host RAM/swap/stall graphs are built from live samples, so they
start empty and fill as it runs. Latency has no graph, because turns are too
sparse for a trend to mean anything (use --window instead).

Everything but `memory` and `traces` reads Prometheus. tools/port-forwards.sh exposes it on
localhost:9090; point --prometheus (or $PROMETHEUS_URL) elsewhere if needed.
It needs `tools/lean-mode.sh` at `on` or lighter, since `deep` pauses Prometheus.
Standard library only, on purpose.
"""
import argparse
import json
import os
import re
import socket
import sys
import time
from collections import deque
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

PANELS = ("memory", "gpu", "pods", "latency", "traces")
OPT_IN = ("traces",)  # off unless asked for; needs Tempo, which lean-mode pauses
# process_resident_memory_bytes is only scraped from these Python services.
RSS_SERVICES = ("gateway", "agent", "stt", "tts")
NOISE_ROUTES = "/health|/metrics"
# On a Prometheus failure, keep showing a panel's last good data (marked stale)
# for this long before giving up and showing the error: a tunnel restart or a
# pod rollout takes a few seconds and shouldn't wipe the screen.
STALE_SECONDS = 60
WINDOW_RE = re.compile(r"^\d+[smhd]$")


class PromError(RuntimeError):
    source = "prometheus"
    hint = "is tools/port-forwards.sh running, and lean-mode below `deep`?"


class TempoError(PromError):
    source = "tempo"
    hint = "start it with tools/lean-mode.sh traces (tools/port-forwards.sh tunnels it)"


def fetch_json(url, timeout=5):
    try:
        with urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except (URLError, OSError, ValueError) as exc:
        raise PromError(f"{exc}") from exc


def prom(base, expr, get_json=fetch_json):
    """Instant query -> list of (labels, float). NaN results are dropped."""
    payload = get_json(f"{base.rstrip('/')}/api/v1/query?{urlencode({'query': expr})}")
    if payload.get("status") != "success":
        raise PromError(str(payload.get("error", "query failed")))
    out = []
    for item in payload["data"]["result"]:
        value = float(item["value"][1])
        if value == value:  # drops NaN, which histogram_quantile returns for no data
            out.append((item["metric"], value))
    return out


def prom_range(base, expr, seconds, get_json=fetch_json, now=time.time):
    """Range query -> list of (labels, [floats]) oldest first, NaN samples dropped."""
    end = now()
    query = urlencode({"query": expr, "start": end - seconds, "end": end, "step": max(15, seconds // 60)})
    payload = get_json(f"{base.rstrip('/')}/api/v1/query_range?{query}")
    if payload.get("status") != "success":
        raise PromError(str(payload.get("error", "query failed")))
    out = []
    for item in payload["data"]["result"]:
        values = [float(v) for _, v in item["values"]]
        out.append((item["metric"], [v for v in values if v == v]))
    return out


def graph_data(fn):
    """A graph is a nicety: if its history query fails or comes back in an
    unexpected shape, the panel keeps its live numbers and just drops the graph."""
    try:
        return fn()
    except (PromError, KeyError, ValueError, TypeError):
        return {}


SPARK = "▁▂▃▄▅▆▇█"


def resample(values, width):
    """Average `values` down to at most `width` points, keeping the trend."""
    if len(values) <= width:
        return list(values)
    n = len(values)
    out = []
    for i in range(width):
        chunk = values[i * n // width: max(i * n // width + 1, (i + 1) * n // width)]
        out.append(sum(chunk) / len(chunk))
    return out


def spark(style, values, width=24, lo=0.0, hi=None, code="c"):
    """One-line graph of `values`; '' when there is too little history to draw.

    Percentages pass lo=0, hi=100 so a quiet GPU looks quiet instead of being
    stretched to fill the row; other series autoscale (hi=None)."""
    points = resample(values, width)
    if len(points) < 2:
        return ""
    top = max(points) if hi is None else hi
    if top <= lo:
        return style(code, SPARK[0] * len(points))
    return style(code, "".join(SPARK[min(7, max(0, int((v - lo) / (top - lo) * 7.999)))] for v in points))


def peak(values, unit="%"):
    return f"peak {max(values):.0f}{unit}" if len(values) >= 2 else ""


# ------------------------------------------------------------------ styling
class Style:
    CODES = {"g": "32", "y": "33", "r": "31", "c": "36", "b": "1", "d": "2"}

    def __init__(self, color):
        self.color = color

    def __call__(self, code, text):
        return f"\033[{self.CODES[code]}m{text}\033[0m" if self.color else text

    def level(self, pct, text):
        return self("g" if pct < 70 else "y" if pct < 90 else "r", text)


def bar(style, pct, width=20):
    pct = max(0.0, min(100.0, pct))
    filled = round(width * pct / 100)
    return style.level(pct, "█" * filled) + style("d", "░" * (width - filled))


def mib(n):
    return f"{n / 1048576:,.0f}Mi"


def heading(style, title, note=""):
    return [style("b", f"── {title} ") + style("d", note)]


# ------------------------------------------------------------------- panels
def read_meminfo(path="/proc/meminfo"):
    info = {}
    with open(path) as f:
        for line in f:
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0]) * 1024  # kB -> bytes
    return info


def read_pressure(path="/proc/pressure/memory"):
    """'some avg10' from PSI: % of the last 10s any task stalled waiting on memory."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("some"):
                    return float(dict(p.split("=") for p in line.split()[1:])["avg10"])
    except (OSError, KeyError, ValueError):
        pass
    return None


def make_trail(args):
    """Rolling live samples for the memory panel. Prometheus has no host-RAM
    series here, so this graph is built from what the dashboard itself sees and
    starts empty each run."""
    n = max(12, min(720, int(window_seconds(args.history) / args.interval)))
    return {k: deque(maxlen=n) for k in ("ram", "swap", "stall")}


def panel_memory(style, meminfo=read_meminfo, pressure=read_pressure, trail=None):
    lines = heading(style, "memory", f"({socket.gethostname()}, from /proc)")
    try:
        m = meminfo()
    except OSError as exc:
        return lines + [style("r", f"  cannot read /proc/meminfo: {exc}")]
    used = m["MemTotal"] - m["MemAvailable"]
    ram = 100 * used / m["MemTotal"]
    swap = 100 * (m["SwapTotal"] - m["SwapFree"]) / m["SwapTotal"] if m.get("SwapTotal") else None
    psi = pressure()
    if trail is not None:
        trail["ram"].append(ram)
        if swap is not None:
            trail["swap"].append(swap)
        if psi is not None:
            trail["stall"].append(min(psi * 5, 100))

    def graph(key, hi=100.0):
        values = list(trail[key]) if trail is not None else []
        return ("  " + spark(style, values, hi=hi) + style("d", f" {peak(values)}")) if len(values) >= 2 else ""

    lines.append(f"  RAM   {bar(style, ram)} {style.level(ram, f'{ram:3.0f}%')}  "
                 f"{mib(used)} / {mib(m['MemTotal'])}  ({mib(m['MemAvailable'])} available){graph('ram')}")
    if swap is not None:
        swap_used = m["SwapTotal"] - m["SwapFree"]
        lines.append(f"  swap  {bar(style, swap)} {style.level(swap, f'{swap:3.0f}%')}  "
                     f"{mib(swap_used)} / {mib(m['SwapTotal'])}{graph('swap')}")
    if psi is not None:
        # >10% means processes are regularly stalled on memory, i.e. thrashing.
        lines.append(f"  stall {bar(style, min(psi * 5, 100))} "
                     f"{style.level(psi * 5, f'{psi:4.1f}%')}  time spent waiting on memory (10s){graph('stall')}")
    return lines


def gpu_history(base, seconds, get_json):
    by = "by (hostname, modelName, gpu)"
    key = lambda m: (m.get("hostname"), m.get("modelName"), m.get("gpu"))  # noqa: E731
    used, free, reserved = (f"max {by} (DCGM_FI_DEV_FB_{n})" for n in ("USED", "FREE", "RESERVED"))
    util = {key(m): v for m, v in prom_range(base, f"max {by} (DCGM_FI_DEV_GPU_UTIL)", seconds, get_json)}
    vram = {key(m): v for m, v in prom_range(base, f"100 * {used} / ({used} + {free} + {reserved})", seconds, get_json)}
    return util, vram


def panel_gpu(style, base, get_json=fetch_json, history=None):
    lines = heading(style, "gpu", "(DCGM)")
    # The 1650 is exported once per time-sliced slot, so collapse duplicates.
    by = "by (hostname, modelName, gpu)"
    q = lambda name: {  # noqa: E731
        (m.get("hostname"), m.get("modelName"), m.get("gpu")): v
        for m, v in prom(base, f"max {by} ({name})", get_json)}
    util, used, free = q("DCGM_FI_DEV_GPU_UTIL"), q("DCGM_FI_DEV_FB_USED"), q("DCGM_FI_DEV_FB_FREE")
    reserved, temp, power = q("DCGM_FI_DEV_FB_RESERVED"), q("DCGM_FI_DEV_GPU_TEMP"), q("DCGM_FI_DEV_POWER_USAGE")
    if not util:
        return lines + [style("y", "  no DCGM samples (dcgm-exporter paused by lean-mode deep?)")]
    util_hist, vram_hist = graph_data(lambda: gpu_history(base, history, get_json)) or ({}, {}) if history else ({}, {})

    def graph(values):
        values = values or []
        return ("  " + spark(style, values, hi=100.0) + style("d", f" {peak(values)}")) if len(values) >= 2 else ""
    for key in sorted(util, key=lambda k: str(k[0])):
        host, model, _ = key
        total = used.get(key, 0) + free.get(key, 0) + reserved.get(key, 0)
        vram = 100 * used.get(key, 0) / total if total else 0.0
        lines.append(f"  {style('c', host or '?')}  {model}")
        lines.append(f"    util  {bar(style, util[key])} {style.level(util[key], f'{util[key]:3.0f}%')}"
                     f"{graph(util_hist.get(key))}")
        lines.append(f"    vram  {bar(style, vram)} {style.level(vram, f'{vram:3.0f}%')}{graph(vram_hist.get(key))}  "
                     f"{used.get(key, 0):,.0f} / {total:,.0f} MiB"
                     f"   {temp.get(key, 0):.0f}°C  {power.get(key, 0):.0f}W")
    return lines


def panel_pods(style, base, get_json=fetch_json, history=None):
    lines = heading(style, "pods", "(kube-state-metrics; RSS only exists for the Python services)")
    ready = {m["pod"]: v for m, v in prom(base, 'kube_pod_status_ready{condition="true"}', get_json)}
    restarts = {m["pod"]: v for m, v in prom(
        base, "sum by (pod) (kube_pod_container_status_restarts_total)", get_json)}
    oom = {m["pod"] for m, v in prom(
        base, 'kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1', get_json)}
    limits = {m["pod"]: v for m, v in prom(
        base, 'sum by (pod) (kube_pod_container_resource_limits{resource="memory"})', get_json)}
    rss = {m["service"]: v for m, v in prom(base, "process_resident_memory_bytes", get_json)}
    rss_hist = {}
    if history:
        rss_hist = graph_data(lambda: {m["service"]: v for m, v in prom_range(
            base, "process_resident_memory_bytes", history, get_json)})
    if not ready:
        return lines + [style("y", "  no pod data from kube-state-metrics")]
    for pod in sorted(ready):
        service = next((s for s in RSS_SERVICES if pod.startswith(s + "-")), None)
        used, limit = rss.get(service), limits.get(pod)
        state = style("g", "ready ") if ready[pod] >= 1 else style("r", "NOT   ")
        mem = ""
        if limit:
            if used is not None:
                pct = 100 * used / limit
                mem = f"{bar(style, pct, 10)} {mib(used):>7} / {mib(limit)}"
            else:
                mem = style("d", f"{'':11}  limit {mib(limit)}")
        n = int(restarts.get(pod, 0))
        flag = style("r", f"  restarts={n} OOMKilled") if pod in oom else (
            style("y", f"  restarts={n}") if n else "")
        trend = ""
        if limit and rss_hist.get(service):
            # Scaled 0..limit, so a line near the top means "close to being killed".
            trend = "  " + spark(style, rss_hist[service], hi=limit) if len(rss_hist[service]) >= 2 else ""
        lines.append(f"  {state} {pod[:36]:36} {mem}{trend}{flag}")
    return lines


def _q(base, get_json, quantile, by, hist, window, matcher=""):
    expr = (f"histogram_quantile({quantile}, sum by (le{by}) "
            f"(increase({hist}_bucket{matcher}[{window}])))")
    return prom(base, expr, get_json)


def _secs(v):
    return f"{v * 1000:,.0f}ms" if v < 1 else f"{v:.2f}s"


def panel_latency(style, base, window, get_json=fetch_json):
    lines = heading(style, "latency", f"(last {window}; percentiles are histogram-bucket estimates, coarse at low traffic)")
    turn = "aicompanion_gateway_turn_duration_seconds"
    turns = prom(base, f"sum(increase({turn}_count[{window}]))", get_json)
    n = turns[0][1] if turns else 0
    p50, p95 = (_q(base, get_json, q, "", turn, window) for q in (0.5, 0.95))
    if n and p50:
        lines.append(f"  turns  {n:,.0f}   p50 {_secs(p50[0][1])}   p95 {_secs(p95[0][1]) if p95 else '-'}")
    else:
        lines.append(style("d", "  turns  none yet"))
    stage = "aicompanion_gateway_stage_duration_seconds"
    s50 = {m["stage"]: v for m, v in _q(base, get_json, 0.5, ", stage", stage, window)}
    s95 = {m["stage"]: v for m, v in _q(base, get_json, 0.95, ", stage", stage, window)}
    width = max((len(n) for n in s50), default=0)
    for name in sorted(s50):
        lines.append(f"    {name:{width}} p50 {_secs(s50[name]):>8}   p95 {_secs(s95.get(name, 0)):>8}")
    http = "aicompanion_http_request_duration_seconds"
    matcher = f'{{route!~"{NOISE_ROUTES}"}}'
    counts = prom(base, f"sum by (exported_service, route) (increase({http}_count{matcher}[{window}]))", get_json)
    h95 = {(m["exported_service"], m["route"]): v for m, v in _q(
        base, get_json, 0.95, ", exported_service, route", http, window, matcher)}
    for m, count in sorted(counts, key=lambda x: (x[0]["exported_service"], x[0]["route"])):
        if count > 0:
            key = (m["exported_service"], m["route"])
            lines.append(f"  {m['exported_service']:8} {m['route']:18} {count:5,.0f} req   "
                         f"p95 {_secs(h95[key]) if key in h95 else '-'}")
    return lines


# ------------------------------------------------------------------ traces
# Server-side span names, one per pipeline stage. The gateway's own client span
# for a streaming call closes when the response *starts* (5ms for the agent),
# so the server-side spans are the ones that hold the real duration.
STAGES = (("stt", "POST /transcribe"), ("agent", "POST /ask_stream"), ("tts", "POST /synth"))
STAGE_STYLE = {"stt": ("c", "▓"), "agent": ("y", "█"), "tts": ("g", "▒")}
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def window_seconds(window):
    return int(window[:-1]) * UNIT_SECONDS[window[-1]]


def tempo_search(base, name, start, end, get_json):
    query = urlencode({"q": '{ name = "%s" }' % name, "limit": 50, "spss": 100,
                       "start": int(start), "end": int(end)})
    try:
        return get_json(f"{base.rstrip('/')}/api/search?{query}").get("traces", [])
    except PromError as exc:
        raise TempoError(str(exc)) from exc


def collect_turns(base, window, get_json=fetch_json, now=time.time):
    """Stick turns, newest first, each {start_ns, end_ns, stt, agent, tts} in ms.

    Queried one stage at a time: a single regex over all three names returned
    only some of a turn's spans from Tempo. A trace is skipped when it was
    started directly at a service (root span "POST /..."), since that is someone
    calling an endpoint rather than the gateway running a turn. A live Stick
    connection is one long-lived WebSocket whose root span has not finished, so
    its trace holds many turns; a new turn begins at each transcribe span.
    """
    end = now()
    events = {}  # trace id -> [(start_ns, stage, dur_ns)]
    for stage, name in STAGES:
        for trace in tempo_search(base, name, end - window_seconds(window), end, get_json):
            if (trace.get("rootTraceName") or "").startswith("POST /"):
                continue
            for span_set in trace.get("spanSets") or [trace.get("spanSet") or {}]:
                for span in span_set.get("spans", []):
                    events.setdefault(trace["traceID"], []).append(
                        (int(span["startTimeUnixNano"]), stage, int(span["durationNanos"])))
    turns = []
    for trace_events in events.values():
        current = None
        for start, stage, dur in sorted(trace_events):
            if stage == "stt" or current is None:
                current = {"start_ns": start, "end_ns": start, "stt": 0.0, "agent": 0.0, "tts": 0.0}
                turns.append(current)
            current[stage] += dur / 1e6
            current["end_ns"] = max(current["end_ns"], start + dur)
    return sorted(turns, key=lambda t: t["start_ns"], reverse=True)


def stage_bar(style, turn, width=30):
    total = sum(turn[s] for s, _ in STAGES) or 1.0
    out = ""
    for stage, _ in STAGES:
        code, char = STAGE_STYLE[stage]
        out += style(code, char * max(1 if turn[stage] else 0, round(width * turn[stage] / total)))
    return out


def panel_traces(style, base, window, count, get_json=fetch_json, now=time.time):
    lines = heading(style, "traces", f"(Tempo, last {window}, newest first)")
    turns = collect_turns(base, window, get_json, now)[:count]
    if not turns:
        return lines + [style("d", "  no Stick turns in this window (direct calls to a service are excluded)")]
    lines.append("  " + "  ".join(style(STAGE_STYLE[s][0], f"{STAGE_STYLE[s][1]} {s}") for s, _ in STAGES)
                 + style("d", "   (agent is mostly the LLM)"))
    for turn in turns:
        clock = time.strftime("%H:%M:%S", time.localtime(turn["start_ns"] / 1e9))
        slowest = max((s for s, _ in STAGES), key=lambda s: turn[s])
        lines.append(f"  {clock}  {_secs((turn['end_ns'] - turn['start_ns']) / 1e9):>7}  {stage_bar(style, turn)}  "
                     + "  ".join(f"{s} {_secs(turn[s] / 1000)}" for s, _ in STAGES)
                     + style("d", f"  slowest: {slowest}"))
    return lines


# ---------------------------------------------------------------- assembly
def build_frame(args, style, get_json=fetch_json, cache=None, trail=None, **overrides):
    """Every panel is isolated: one failing (Prometheus down) can't blank the rest.

    `cache` (a dict the caller keeps between frames) holds each panel's last good
    lines so a brief outage shows them, marked stale, instead of an error."""
    base = args.prometheus
    history = window_seconds(args.history) if args.graphs else None
    makers = {
        "memory": lambda: panel_memory(style, trail=trail if args.graphs else None, **overrides.get("memory", {})),
        "gpu": lambda: panel_gpu(style, base, get_json, history=history),
        "pods": lambda: panel_pods(style, base, get_json, history=history),
        "latency": lambda: panel_latency(style, base, args.window, get_json),
        "traces": lambda: panel_traces(style, args.tempo, args.window, args.turns, get_json),
    }
    frame = [style("b", "aicompanion") + style("d", f"  {time.strftime('%H:%M:%S')}  "
                                                    f"prometheus={base}  refresh={args.interval:g}s  Ctrl-C quits")]
    for name in PANELS:
        if not getattr(args, name):
            continue
        try:
            lines = makers[name]()
            if cache is not None:
                cache[name] = (time.time(), lines)
            frame += [""] + lines
        except PromError as exc:
            age = time.time() - cache[name][0] if cache and name in cache else None
            if age is not None and age <= STALE_SECONDS:
                old = cache[name][1]
                frame += ["", old[0], style("y", f"  stale: {exc.source} unreachable for {age:.0f}s, showing last data")] + old[1:]
            else:
                frame += [""] + heading(style, name) + [
                    style("r", f"  {exc.source} unreachable: {exc}"),
                    style("d", f"  {exc.hint}")]
    return frame


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in PANELS:
        ap.add_argument(f"--{name}", action=argparse.BooleanOptionalAction, default=name not in OPT_IN,
                        help=f"show the {name} panel (default: {'off' if name in OPT_IN else 'on'})")
    ap.add_argument("--only", metavar="LIST", help="comma list of panels to show, "
                    f"turning all others off (from: {', '.join(PANELS)})")
    ap.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL", "http://localhost:9090"))
    ap.add_argument("--tempo", default=os.environ.get("TEMPO_URL", "http://localhost:3200"),
                    help="Tempo URL for the traces panel (env TEMPO_URL)")
    ap.add_argument("--turns", type=int, default=5, help="how many recent turns the traces panel shows")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between refreshes")
    ap.add_argument("--graphs", action=argparse.BooleanOptionalAction, default=True,
                    help="trend graphs next to the bars (default: on)")
    ap.add_argument("--history", default="15m", help="how far back the graphs go, e.g. 5m, 15m, 2h (default: 15m)")
    ap.add_argument("--window", default="1h", help="latency window, e.g. 15m, 1h, 1d (default: 1h)")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)
    if args.only:
        wanted = {p.strip() for p in args.only.split(",") if p.strip()}
        unknown = wanted - set(PANELS)
        if unknown:
            ap.error(f"--only: unknown panel(s) {', '.join(sorted(unknown))}; choose from {', '.join(PANELS)}")
        for name in PANELS:
            setattr(args, name, name in wanted)
    for flag in ("window", "history"):
        if not WINDOW_RE.match(getattr(args, flag)):
            ap.error(f"--{flag} must look like 30s, 15m, 1h or 1d")
    if args.interval <= 0:
        ap.error("--interval must be positive")
    if not any(getattr(args, n) for n in PANELS):
        ap.error("every panel is turned off")
    return args


def main(argv=None):
    args = parse_args(argv)
    interactive = sys.stdout.isatty() and not args.once
    style = Style(sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ)
    if not interactive:
        print("\n".join(build_frame(args, style)))
        return 0
    cache, trail = {}, make_trail(args)
    try:
        sys.stdout.write("\033[?25l\033[2J")  # hide cursor, clear once
        while True:
            frame = build_frame(args, style, cache=cache, trail=trail)
            sys.stdout.write("\033[H" + "\033[K\n".join(frame) + "\033[K\n\033[J")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write("\033[?25h\n")


if __name__ == "__main__":
    sys.exit(main())
