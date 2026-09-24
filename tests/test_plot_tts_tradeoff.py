"""benchmarks/plot_tts_tradeoff.py reads points from kept run summaries."""
import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "plot_tts_tradeoff.py"
_spec = importlib.util.spec_from_file_location("plot_tts_tradeoff", _PATH)
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)


def _run(path, device, entries):
    path.write_text(json.dumps({"metadata": {"device": device}, "summary": entries}))


def test_newest_run_wins_per_label_and_hidden_labels_are_skipped(tmp_path):
    _run(tmp_path / "20260901T000000Z-old.json", "cpu",
         [{"label": "Kitten mini", "x_realtime": 1.0, "rss_peak_mib": 500, "n": 5}])
    _run(tmp_path / "20260924T000000Z-new.json", "cpu",
         [{"label": "Kitten mini", "x_realtime": 1.5, "rss_peak_mib": 520, "n": 50},
          {"label": "Kitten nano", "x_realtime": 3.0, "rss_peak_mib": 300, "n": 50},
          {"label": "No memory", "x_realtime": 2.0, "rss_peak_mib": None}])
    _run(tmp_path / "20260905T000000Z-gpu.json", "gpu",
         [{"label": "Chatterbox Nano", "x_realtime": 2.54, "rss_peak_mib": 1857, "n": 3}])
    points = pt.load_points(tmp_path)
    assert points == [("Chatterbox Nano", 1857, 2.54, "gpu", "above", 3),
                      ("Kitten mini", 520, 1.5, "cpu", "right", 50)]


def test_svg_lists_every_point_in_its_description():
    svg = pt.svg("light", [("Kitten mini", 520, 1.5, "cpu", "right", 50)])
    assert "Kitten mini: 6.7 s, 520 MiB, CPU" in svg and "n=50" in svg
