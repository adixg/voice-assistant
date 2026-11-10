"""benchmarks/plot_tts_tradeoff.py and plot_stt.py read their points from kept
run summaries and split CPU (RAM) from GPU (VRAM) charts."""
import importlib.util
import json
from pathlib import Path

_BENCH = Path(__file__).resolve().parents[1] / "benchmarks"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _BENCH / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pt = _load("plot_tts_tradeoff")
ps = _load("plot_stt")


def _run(path, device, entries):
    path.write_text(json.dumps({"metadata": {"device": device}, "summary": entries}))


def test_newest_run_wins_per_label_and_renamed_engines_merge(tmp_path):
    _run(tmp_path / "20260901T000000Z-old.json", "cpu",
         [{"label": "Kitten mini", "x_realtime": 1.0, "rss_peak_mib": 500, "n": 5},
          {"label": "Kitten nano 1.6x (live)", "x_realtime": 3.0, "rss_peak_mib": 260, "n": 50}])
    _run(tmp_path / "20260924T000000Z-new.json", "cpu",
         [{"label": "Kitten mini", "x_realtime": 1.5, "rss_peak_mib": 520, "n": 50},
          {"label": "Kitten nano 1.6x (deployed)", "x_realtime": 3.4, "rss_peak_mib": 250, "n": 50}])
    speed = pt.load_speed(tmp_path)
    assert speed[("Kitten mini", "cpu")]["x_realtime"] == 1.5
    assert speed[("Kitten nano 1.6x (live)", "cpu")]["x_realtime"] == 3.4
    assert ("Kitten nano 1.6x (deployed)", "cpu") not in speed


def test_cpu_chart_has_only_cpu_engines_and_skips_hidden_ones(tmp_path):
    _run(tmp_path / "20260924T000000Z-cpu.json", "cpu",
         [{"label": "Kitten mini", "x_realtime": 1.5, "rss_peak_mib": 520, "n": 50},
          {"label": "Kitten nano", "x_realtime": 3.0, "rss_peak_mib": 300, "n": 50}])
    _run(tmp_path / "20260924T000001Z-gpu.json", "gpu",
         [{"label": "VITS", "x_realtime": 25.0, "rss_peak_mib": 1657, "n": 50}])
    svg = pt.cpu_chart("light", pt.load_speed(tmp_path), {})
    assert "Kitten mini: 6.7 s · 520 MiB · n=50" in svg
    assert "Kitten nano:" not in svg and "VITS" not in svg


def test_gpu_chart_uses_vram_and_marks_a_lower_bound(tmp_path):
    _run(tmp_path / "20260924T000001Z-vits-gtx1650.json", "gpu",
         [{"label": "VITS", "x_realtime": 25.0, "rss_peak_mib": 1657, "n": 50}])
    _run(tmp_path / "20260905T000000Z-rtx4060-gpu.json", "gpu",
         [{"label": "Chatterbox Nano", "x_realtime": 2.5, "rss_peak_mib": 1857, "n": 3}])
    _run(tmp_path / "20261110T000000Z-cb-gtx1650.json", "gpu",
         [{"label": "Chatterbox Nano", "x_realtime": 2.0, "rss_peak_mib": 3000, "n": 50}])
    (tmp_path / "20261110T000000Z-tts-vram.json").write_text(json.dumps({"engines": [
        {"label": "VITS", "card": "gtx1650", "vram_peak_mib": 585, "lower_bound": True},
        {"label": "Chatterbox Nano", "card": "rtx4060", "vram_peak_mib": 1857},
        {"label": "Chatterbox Nano", "card": "gtx1650", "vram_peak_mib": 2222}]}))
    svg = pt.gpu_chart("dark", pt.load_speed(tmp_path), pt.load_vram(tmp_path))
    assert "VITS: 0.4 s · ≥585 MiB · n=50" in svg and "1657" not in svg
    # the same engine on two cards is two points, each with its own run
    assert "Chatterbox Nano (RTX 4060): 4.0 s · 1.8 GB · n=3" in svg
    assert "Chatterbox Nano (GTX 1650): 5.0 s · 2.2 GB · n=50" in svg


def test_stt_skips_partial_runs_and_splits_cpu_from_gpu(tmp_path):
    def run(name, p50, ok=50, n=50):
        (tmp_path / name).write_text(json.dumps({"summary": [
            {"ok": ok, "n": n, "stt_ms_p50": p50, "stt_ms_p95": p50 * 2}]}))
    run("20261109T000000Z-stt-moonshine-arch-ssd-cpu.json", 400)
    run("20261109T000000Z-stt-whisper-small-gtx1650.json", 3240)
    run("20261110T000000Z-stt-whisper-small-gtx1650.json", 9999, ok=46, n=47)  # crashed rerun
    (tmp_path / "20261110T000001Z-stt-memory.json").write_text(json.dumps({"pods": [
        {"model": "moonshine", "hw": "cpu", "ram_peak_mib": 274},
        {"model": "whisper-small", "hw": "gtx1650", "ram_peak_mib": 468, "vram_peak_mib": 831}]}))
    speed, mem = ps.load_speed(tmp_path), ps.load_memory(tmp_path)
    assert speed[("whisper-small", "gtx1650")]["stt_ms_p50"] == 3240
    cpu, gpu = ps.cpu_chart("light", speed, mem), ps.gpu_chart("light", speed, mem)
    assert "Moonshine base (live)" in cpu and "Whisper small" not in cpu
    assert "3.24 s · 831 MiB" in gpu and "Moonshine" not in gpu
