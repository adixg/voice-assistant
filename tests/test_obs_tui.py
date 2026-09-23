"""tools/obs_tui.py: panel toggling, Prometheus failure isolation, and the
data-shaping quirks of this cluster (duplicated time-sliced GPU series, NaN
percentiles, RSS existing only for the Python services)."""
from urllib.parse import parse_qs, urlparse

import pytest

from tools import obs_tui

MEMINFO = {"MemTotal": 8 * 2**30, "MemAvailable": 2 * 2**30, "SwapTotal": 4 * 2**30, "SwapFree": 3 * 2**30}


def fake_prom(routes):
    """get_json stand-in: the first route whose key appears in the PromQL wins."""
    def get_json(url):
        expr = parse_qs(urlparse(url).query)["query"][0]
        for needle, result in routes.items():
            if needle in expr:
                return {"status": "success", "data": {"resultType": "vector", "result": [
                    {"metric": labels, "value": [0, str(value)]} for labels, value in result]}}
        return {"status": "success", "data": {"resultType": "vector", "result": []}}
    return get_json


def down(_url):
    raise obs_tui.PromError("Connection refused")


def parse(*argv):
    return obs_tui.parse_args(list(argv))


PLAIN = obs_tui.Style(color=False)
MEM = {"memory": {"meminfo": lambda: MEMINFO, "pressure": lambda: 0.0}}


# --------------------------------------------------------------- arguments
def test_every_panel_is_on_by_default_except_the_opt_in_ones():
    args = parse()
    assert all(getattr(args, p) for p in obs_tui.PANELS if p not in obs_tui.OPT_IN)
    assert not any(getattr(args, p) for p in obs_tui.OPT_IN)


def test_each_panel_can_be_switched_off_on_its_own():
    args = parse("--no-gpu", "--no-latency")
    assert (args.memory, args.gpu, args.pods, args.latency) == (True, False, True, False)


def test_only_turns_everything_else_off():
    args = parse("--only", "gpu, memory")
    assert (args.memory, args.gpu, args.pods, args.latency) == (True, True, False, False)


@pytest.mark.parametrize("argv", [
    ["--only", "bogus"],
    ["--window", "5x"],
    ["--interval", "0"],
    ["--no-memory", "--no-gpu", "--no-pods", "--no-latency"],
])
def test_bad_arguments_are_rejected(argv):
    with pytest.raises(SystemExit):
        parse(*argv)


# -------------------------------------------------------- frame assembly
def test_disabled_panels_do_not_appear_and_are_not_queried():
    queried = []

    def spy(url):
        queried.append(url)
        return fake_prom({})(url)
    frame = "\n".join(obs_tui.build_frame(parse("--only", "memory"), PLAIN, spy, **MEM))
    assert "── memory" in frame and "── gpu" not in frame and "── pods" not in frame
    assert queried == []


def test_prometheus_down_does_not_blank_the_memory_panel():
    frame = "\n".join(obs_tui.build_frame(parse(), PLAIN, down, **MEM))
    assert "RAM" in frame and "swap" in frame                      # host panel survived
    assert frame.count("prometheus unreachable") == 3              # gpu, pods, latency
    assert "port-forwards.sh" in frame                             # says how to fix it


def test_no_color_output_contains_no_escape_codes():
    frame = "\n".join(obs_tui.build_frame(parse(), PLAIN, down, **MEM))
    assert "\033" not in frame


# ------------------------------------------------------------------ memory
def test_memory_panel_reports_used_available_swap_and_pressure():
    text = "\n".join(obs_tui.panel_memory(PLAIN, lambda: MEMINFO, lambda: 2.0))
    assert "75%" in text and "6,144Mi / 8,192Mi" in text and "2,048Mi available" in text
    assert "25%" in text and "1,024Mi / 4,096Mi" in text
    assert "2.0%" in text


def test_meminfo_and_pressure_parsers(tmp_path):
    (tmp_path / "meminfo").write_text("MemTotal:        1024 kB\nMemAvailable:     512 kB\n")
    assert obs_tui.read_meminfo(str(tmp_path / "meminfo")) == {"MemTotal": 1024 * 1024, "MemAvailable": 512 * 1024}
    (tmp_path / "psi").write_text("some avg10=3.25 avg60=1.00 avg300=0.50 total=1\nfull avg10=0.00 avg60=0 avg300=0 total=0\n")
    assert obs_tui.read_pressure(str(tmp_path / "psi")) == 3.25
    assert obs_tui.read_pressure(str(tmp_path / "missing")) is None


# --------------------------------------------------------------------- gpu
def gpu_routes(duplicate_1650):
    gtx = {"hostname": "arch-ssd", "modelName": "GTX 1650", "gpu": "0"}
    rtx = {"hostname": "laptop", "modelName": "RTX 4060", "gpu": "0"}
    # `max by (...)` is in the query, so a real Prometheus already collapses the
    # time-sliced duplicates; the fake returns them raw to prove the panel also
    # keys by GPU and cannot double-print one.
    rows = lambda a, b: [(gtx, a)] * (2 if duplicate_1650 else 1) + [(rtx, b)]  # noqa: E731
    return {"GPU_UTIL": rows(10, 50), "FB_USED": rows(2000, 4000), "FB_FREE": rows(1500, 3000),
            "FB_RESERVED": rows(500, 1000), "GPU_TEMP": rows(44, 55), "POWER": rows(7, 40)}


def test_gpu_panel_prints_each_physical_gpu_once_with_vram_percent():
    text = "\n".join(obs_tui.panel_gpu(PLAIN, "http://p", fake_prom(gpu_routes(duplicate_1650=True))))
    assert text.count("GTX 1650") == 1 and text.count("RTX 4060") == 1
    assert " 50%" in text                    # 2000 / (2000+1500+500)
    assert "2,000 / 4,000 MiB" in text and "44°C" in text and "7W" in text


def test_gpu_panel_explains_missing_data():
    text = "\n".join(obs_tui.panel_gpu(PLAIN, "http://p", fake_prom({})))
    assert "no DCGM samples" in text and "lean-mode" in text


# -------------------------------------------------------------------- pods
def pod_routes():
    return {
        "kube_pod_status_ready": [({"pod": "stt-abc"}, 1), ({"pod": "tts-def"}, 0), ({"pod": "llama-cpp-x"}, 1)],
        "restarts_total": [({"pod": "stt-abc"}, 0), ({"pod": "tts-def"}, 5), ({"pod": "llama-cpp-x"}, 1)],
        "last_terminated_reason": [({"pod": "llama-cpp-x"}, 1)],
        "resource_limits": [({"pod": "stt-abc"}, 1024 * 2**20), ({"pod": "tts-def"}, 1792 * 2**20),
                            ({"pod": "llama-cpp-x"}, 2048 * 2**20)],
        "process_resident_memory_bytes": [({"service": "stt"}, 512 * 2**20), ({"service": "tts"}, 1024 * 2**20)],
    }


def test_pods_panel_shows_rss_against_limit_only_where_rss_exists():
    lines = obs_tui.panel_pods(PLAIN, "http://p", fake_prom(pod_routes()))
    by_pod = {next(w for w in l.split() if "-" in w): l for l in lines[1:]}
    assert "512Mi / 1,024Mi" in by_pod["stt-abc"]
    assert "limit 2,048Mi" in by_pod["llama-cpp-x"] and "/" not in by_pod["llama-cpp-x"].split("limit")[0].split("llama-cpp-x")[1]


def test_pods_panel_flags_restarts_oom_kills_and_unready_pods():
    text = "\n".join(obs_tui.panel_pods(PLAIN, "http://p", fake_prom(pod_routes())))
    tts = next(l for l in text.splitlines() if "tts-def" in l)
    llama = next(l for l in text.splitlines() if "llama-cpp-x" in l)
    stt = next(l for l in text.splitlines() if "stt-abc" in l)
    assert "NOT" in tts and "restarts=5" in tts and "OOMKilled" not in tts
    assert "restarts=1 OOMKilled" in llama
    assert "restarts" not in stt


# ----------------------------------------------------------------- latency
def test_latency_panel_says_so_when_there_is_no_traffic():
    text = "\n".join(obs_tui.panel_latency(PLAIN, "http://p", "1h", fake_prom({})))
    assert "none yet" in text


def test_latency_panel_reports_turns_stages_and_routes_and_ignores_nan():
    routes = {
        "sum(increase(aicompanion_gateway_turn_duration_seconds_count": [({}, 4)],
        "histogram_quantile(0.5, sum by (le) (increase(aicompanion_gateway_turn": [({}, 2.5)],
        "histogram_quantile(0.95, sum by (le) (increase(aicompanion_gateway_turn": [({}, float("nan"))],
        "histogram_quantile(0.5, sum by (le, stage)": [({"stage": "stt"}, 0.4)],
        "histogram_quantile(0.95, sum by (le, stage)": [({"stage": "stt"}, 0.9)],
        "_count{route": [({"exported_service": "tts", "route": "/synth"}, 7)],
        "histogram_quantile(0.95, sum by (le, exported_service, route)": [
            ({"exported_service": "tts", "route": "/synth"}, 3.0)],
    }
    text = "\n".join(obs_tui.panel_latency(PLAIN, "http://p", "1h", fake_prom(routes)))
    assert "turns  4" in text and "p50 2.50s" in text and "p95 -" in text     # NaN p95 -> '-'
    assert "stt" in text and "400ms" in text and "900ms" in text
    assert "tts" in text and "/synth" in text and "7 req" in text and "p95 3.00s" in text


def test_latency_queries_exclude_health_and_metrics_noise():
    seen = []

    def spy(url):
        seen.append(parse_qs(urlparse(url).query)["query"][0])
        return fake_prom({})(url)
    obs_tui.panel_latency(PLAIN, "http://p", "15m", spy)
    http = [q for q in seen if "http_request_duration" in q]
    assert http and all("/health|/metrics" in q and "[15m]" in q for q in http)


def test_prom_errors_are_reported_not_swallowed():
    with pytest.raises(obs_tui.PromError):
        obs_tui.prom("http://p", "up", lambda _u: {"status": "error", "error": "bad query"})


# ------------------------------------------------- brief outages stay quiet
def test_a_brief_outage_shows_last_data_marked_stale_not_an_error(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(obs_tui.time, "time", lambda: clock[0])
    cache = {}
    args = parse("--only", "pods")
    good = obs_tui.build_frame(args, PLAIN, fake_prom(pod_routes()), cache=cache)
    assert any("stt-abc" in line for line in good)

    clock[0] += 5                        # Prometheus goes away for 5 seconds
    during = "\n".join(obs_tui.build_frame(args, PLAIN, down, cache=cache))
    assert "stt-abc" in during           # last good rows are still on screen
    assert "stale" in during and "5s" in during
    assert "port-forwards.sh" not in during     # the full error block is not shown


def test_an_outage_longer_than_the_grace_period_shows_the_real_error(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(obs_tui.time, "time", lambda: clock[0])
    cache = {}
    args = parse("--only", "pods")
    obs_tui.build_frame(args, PLAIN, fake_prom(pod_routes()), cache=cache)

    clock[0] += obs_tui.STALE_SECONDS + 1
    later = "\n".join(obs_tui.build_frame(args, PLAIN, down, cache=cache))
    assert "prometheus unreachable" in later and "stt-abc" not in later   # old data must not linger


def test_no_cache_means_no_stale_data_on_first_failure():
    text = "\n".join(obs_tui.build_frame(parse("--only", "pods"), PLAIN, down, cache={}))
    assert "prometheus unreachable" in text and "stale" not in text


def test_a_recovered_panel_replaces_the_stale_one(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(obs_tui.time, "time", lambda: clock[0])
    cache = {}
    args = parse("--only", "pods")
    obs_tui.build_frame(args, PLAIN, fake_prom(pod_routes()), cache=cache)
    clock[0] += 3
    obs_tui.build_frame(args, PLAIN, down, cache=cache)
    clock[0] += 3
    back = "\n".join(obs_tui.build_frame(args, PLAIN, fake_prom(pod_routes()), cache=cache))
    assert "stale" not in back and "stt-abc" in back


# ------------------------------------------------------------------ traces
NS = 1_000_000_000


def span(start_s, dur_ms):
    return {"spanID": "x", "startTimeUnixNano": str(int(start_s * NS)), "durationNanos": str(int(dur_ms * 1e6))}


def tempo(traces_by_stage):
    """A get_json for Tempo's search: answers by the span name in the TraceQL."""
    def get_json(url):
        q = parse_qs(urlparse(url).query)["q"][0]
        for name, traces in traces_by_stage.items():
            if f'"{name}"' in q:
                return {"traces": traces}
        return {"traces": []}
    return get_json


def trace(tid, root, spans):
    return {"traceID": tid, "rootTraceName": root, "spanSets": [{"spans": spans}]}


NOW = 10_000.0


def turn_traces():
    return {
        "POST /transcribe": [trace("aaa", "HTTP /stick", [span(9000, 2000)])],
        "POST /ask_stream": [trace("aaa", "HTTP /stick", [span(9002, 8000)])],
        "POST /synth": [trace("aaa", "HTTP /stick", [span(9010, 6000)])],
    }


def test_traces_are_off_by_default_and_on_with_the_flag_or_only():
    assert parse().traces is False
    assert parse("--traces").traces is True
    assert parse("--only", "traces").traces is True and parse("--only", "traces").gpu is False


def test_a_turn_is_split_into_stt_agent_and_tts_from_the_server_side_spans():
    (turn,) = obs_tui.collect_turns("http://t", "1h", tempo(turn_traces()), now=lambda: NOW)
    assert (turn["stt"], turn["agent"], turn["tts"]) == (2000, 8000, 6000)
    # from the stt span starting at t=9000s to the tts span ending at 9010+6 = 9016s
    assert turn["end_ns"] - turn["start_ns"] == 16 * NS


def test_calls_made_directly_to_a_service_are_not_stick_turns():
    traces = turn_traces()
    traces["POST /synth"].append(trace("direct", "POST /synth", [span(9500, 3000)]))
    turns = obs_tui.collect_turns("http://t", "1h", tempo(traces), now=lambda: NOW)
    assert len(turns) == 1


def test_a_long_lived_websocket_trace_yields_one_turn_per_transcribe():
    """A Stick connection is one WebSocket whose root span has not finished (no
    root name yet), so a single trace holds many turns."""
    traces = {
        "POST /transcribe": [trace("ws", None, [span(100, 1000), span(200, 1500)])],
        "POST /ask_stream": [trace("ws", None, [span(101, 4000), span(202, 5000)])],
        "POST /synth": [trace("ws", None, [span(106, 2000), span(208, 3000)])],
    }
    turns = obs_tui.collect_turns("http://t", "1h", tempo(traces), now=lambda: NOW)
    assert [(t["stt"], t["agent"], t["tts"]) for t in turns] == [(1500, 5000, 3000), (1000, 4000, 2000)]  # newest first


def test_a_failed_turn_with_no_tts_span_is_still_shown():
    traces = turn_traces()
    traces["POST /synth"] = []
    (turn,) = obs_tui.collect_turns("http://t", "1h", tempo(traces), now=lambda: NOW)
    assert turn["tts"] == 0 and turn["agent"] == 8000


def test_traces_panel_shows_total_stages_and_the_slowest():
    text = "\n".join(obs_tui.panel_traces(PLAIN, "http://t", "1h", 5, tempo(turn_traces()), now=lambda: NOW))
    assert "stt 2.00s" in text and "agent 8.00s" in text and "tts 6.00s" in text
    assert "slowest: agent" in text and "16.00s" in text
    assert "▓" in text and "█" in text and "▒" in text


def test_traces_panel_limits_the_number_of_turns():
    traces = {
        "POST /transcribe": [trace("ws", None, [span(100 + i * 10, 500) for i in range(8)])],
        "POST /ask_stream": [], "POST /synth": []}
    lines = obs_tui.panel_traces(PLAIN, "http://t", "1h", 3, tempo(traces), now=lambda: NOW)
    assert len([l for l in lines if "slowest:" in l]) == 3


def test_traces_panel_says_when_there_are_no_turns():
    text = "\n".join(obs_tui.panel_traces(PLAIN, "http://t", "1h", 5, tempo({}), now=lambda: NOW))
    assert "no Stick turns" in text


def test_tempo_being_off_is_reported_as_tempo_with_the_lean_mode_hint():
    frame = "\n".join(obs_tui.build_frame(parse("--only", "traces"), PLAIN, down))
    assert "tempo unreachable" in frame and "lean-mode.sh traces" in frame
    assert "prometheus" not in frame.split("──", 1)[1].split("\n", 1)[1]


def test_the_search_is_bounded_to_the_requested_window():
    seen = []

    def spy(url):
        seen.append(parse_qs(urlparse(url).query))
        return {"traces": []}
    obs_tui.collect_turns("http://t", "15m", spy, now=lambda: NOW)
    assert all(int(q["end"][0]) - int(q["start"][0]) == 900 for q in seen) and len(seen) == 3


def test_window_seconds():
    assert [obs_tui.window_seconds(w) for w in ("30s", "15m", "2h", "1d")] == [30, 900, 7200, 86400]
