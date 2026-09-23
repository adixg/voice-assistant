#!/usr/bin/env python3
"""Terminal dashboard for the aicompanion cluster: a few MB of RAM, not a browser.

    python tools/obs_tui.py                    # everything, refreshing every 5s
    python tools/obs_tui.py --no-latency       # turn any panel off
    python tools/obs_tui.py --only gpu,memory  # or list exactly the ones you want
    python tools/obs_tui.py --once             # print one frame and exit

Panels (each has --NAME / --no-NAME, all on by default):
    memory   this machine's RAM, swap and kernel memory pressure (/proc, no
             Prometheus needed)
    gpu      per-GPU utilisation, VRAM, temperature and power (DCGM)
    pods     per-pod readiness, restarts, OOM kills, and memory vs its limit
    latency  gateway turn and per-stage latency, and HTTP latency by route

Everything but `memory` reads Prometheus. tools/port-forwards.sh exposes it on
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
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

PANELS = ("memory", "gpu", "pods", "latency")
# process_resident_memory_bytes is only scraped from these Python services.
RSS_SERVICES = ("gateway", "agent", "stt", "tts")
NOISE_ROUTES = "/health|/metrics"
WINDOW_RE = re.compile(r"^\d+[smhd]$")


class PromError(RuntimeError):
    pass


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


def panel_memory(style, meminfo=read_meminfo, pressure=read_pressure):
    lines = heading(style, "memory", f"({socket.gethostname()}, from /proc)")
    try:
        m = meminfo()
    except OSError as exc:
        return lines + [style("r", f"  cannot read /proc/meminfo: {exc}")]
    used = m["MemTotal"] - m["MemAvailable"]
    ram = 100 * used / m["MemTotal"]
    lines.append(f"  RAM   {bar(style, ram)} {style.level(ram, f'{ram:3.0f}%')}  "
                 f"{mib(used)} / {mib(m['MemTotal'])}  ({mib(m['MemAvailable'])} available)")
    if m.get("SwapTotal"):
        swap_used = m["SwapTotal"] - m["SwapFree"]
        swap = 100 * swap_used / m["SwapTotal"]
        lines.append(f"  swap  {bar(style, swap)} {style.level(swap, f'{swap:3.0f}%')}  "
                     f"{mib(swap_used)} / {mib(m['SwapTotal'])}")
    psi = pressure()
    if psi is not None:
        # >10% means processes are regularly stalled on memory, i.e. thrashing.
        lines.append(f"  stall {bar(style, min(psi * 5, 100))} "
                     f"{style.level(psi * 5, f'{psi:4.1f}%')}  time spent waiting on memory (10s)")
    return lines


def panel_gpu(style, base, get_json=fetch_json):
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
    for key in sorted(util, key=lambda k: str(k[0])):
        host, model, _ = key
        total = used.get(key, 0) + free.get(key, 0) + reserved.get(key, 0)
        vram = 100 * used.get(key, 0) / total if total else 0.0
        lines.append(f"  {style('c', host or '?')}  {model}")
        lines.append(f"    util  {bar(style, util[key])} {style.level(util[key], f'{util[key]:3.0f}%')}")
        lines.append(f"    vram  {bar(style, vram)} {style.level(vram, f'{vram:3.0f}%')}  "
                     f"{used.get(key, 0):,.0f} / {total:,.0f} MiB"
                     f"   {temp.get(key, 0):.0f}°C  {power.get(key, 0):.0f}W")
    return lines


def panel_pods(style, base, get_json=fetch_json):
    lines = heading(style, "pods", "(kube-state-metrics; RSS only exists for the Python services)")
    ready = {m["pod"]: v for m, v in prom(base, 'kube_pod_status_ready{condition="true"}', get_json)}
    restarts = {m["pod"]: v for m, v in prom(
        base, "sum by (pod) (kube_pod_container_status_restarts_total)", get_json)}
    oom = {m["pod"] for m, v in prom(
        base, 'kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1', get_json)}
    limits = {m["pod"]: v for m, v in prom(
        base, 'sum by (pod) (kube_pod_container_resource_limits{resource="memory"})', get_json)}
    rss = {m["service"]: v for m, v in prom(base, "process_resident_memory_bytes", get_json)}
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
        lines.append(f"  {state} {pod[:36]:36} {mem}{flag}")
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
    for name in sorted(s50):
        lines.append(f"    {name:8} p50 {_secs(s50[name]):>8}   p95 {_secs(s95.get(name, 0)):>8}")
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


# ---------------------------------------------------------------- assembly
def build_frame(args, style, get_json=fetch_json, **overrides):
    """Every panel is isolated: one failing (Prometheus down) can't blank the rest."""
    base = args.prometheus
    makers = {
        "memory": lambda: panel_memory(style, **overrides.get("memory", {})),
        "gpu": lambda: panel_gpu(style, base, get_json),
        "pods": lambda: panel_pods(style, base, get_json),
        "latency": lambda: panel_latency(style, base, args.window, get_json),
    }
    frame = [style("b", "aicompanion") + style("d", f"  {time.strftime('%H:%M:%S')}  "
                                                    f"prometheus={base}  refresh={args.interval:g}s  Ctrl-C quits")]
    for name in PANELS:
        if not getattr(args, name):
            continue
        try:
            frame += [""] + makers[name]()
        except PromError as exc:
            frame += [""] + heading(style, name) + [
                style("r", f"  prometheus unreachable: {exc}"),
                style("d", "  is tools/port-forwards.sh running, and lean-mode below `deep`?")]
    return frame


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in PANELS:
        ap.add_argument(f"--{name}", action=argparse.BooleanOptionalAction, default=True,
                        help=f"show the {name} panel (default: on)")
    ap.add_argument("--only", metavar="LIST", help="comma list of panels to show, "
                    f"turning all others off (from: {', '.join(PANELS)})")
    ap.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL", "http://localhost:9090"))
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between refreshes")
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
    if not WINDOW_RE.match(args.window):
        ap.error("--window must look like 30s, 15m, 1h or 1d")
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
    try:
        sys.stdout.write("\033[?25l\033[2J")  # hide cursor, clear once
        while True:
            frame = build_frame(args, style)
            sys.stdout.write("\033[H" + "\033[K\n".join(frame) + "\033[K\n\033[J")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write("\033[?25h\n")


if __name__ == "__main__":
    sys.exit(main())
