"""benchmarks/pipeline/bench_turn.py -- the pure parts (target parsing, URL
rewriting, request building, WAV/multipart helpers, summaries). The live
stages are HTTP calls against the cluster and are exercised by running the
tool itself, not here."""
import importlib.util
import io
import json
import sys
import wave
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "pipeline" / "bench_turn.py"
_spec = importlib.util.spec_from_file_location("bench_turn", _PATH)
bt = importlib.util.module_from_spec(_spec)
sys.modules["bench_turn"] = bt  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(bt)


def _wav(seconds: float, rate: int = 16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return out.getvalue()


class TestParseLLM:
    def test_defaults_to_openai_with_thinking_off(self):
        t = bt.parse_llm("x=http://h:8080/v1")
        assert (t.name, t.url, t.kind, t.think) == ("x", "http://h:8080/v1", "openai", "off")

    def test_reads_every_option(self):
        t = bt.parse_llm("a=http://agent:8002,kind=agent,model=m,think=on")
        assert (t.kind, t.model, t.think) == ("agent", "m", "on")

    @pytest.mark.parametrize("spec", ["no-equals", "x=http://h,think=maybe",
                                      "x=http://h,kind=grpc", "x=http://h,colour=red"])
    def test_rejects_bad_specs(self, spec):
        with pytest.raises(ValueError):
            bt.parse_llm(spec)

    def test_default_targets_all_parse(self):
        names = [bt.parse_llm(s).name for s in bt.DEFAULT_LLMS]
        assert names == ["agent", "gtx1650", "rtx4060"]


class TestRewriteHost:
    IPS = {"agent": "10.43.0.5", "llama-cpp-rtx4060": "10.43.0.9"}

    def test_replaces_known_service_keeping_port_and_path(self):
        assert bt.rewrite_host("http://llama-cpp-rtx4060:8080/v1", self.IPS) == "http://10.43.0.9:8080/v1"

    def test_leaves_unknown_hosts_alone(self):
        assert bt.rewrite_host("http://example.com:1/x", self.IPS) == "http://example.com:1/x"


class TestLLMRequest:
    def test_openai_think_off_sends_llama_cpp_switch(self):
        url, payload = bt.llm_request(bt.parse_llm("x=http://h:8080/v1,model=m"), [{"role": "user", "content": "hi"}])
        assert url == "http://h:8080/v1/chat/completions"
        assert payload["stream"] is True and payload["model"] == "m"
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}

    def test_openai_think_default_sends_no_switch(self):
        _, payload = bt.llm_request(bt.parse_llm("x=http://h/v1,think=default"), [])
        assert "chat_template_kwargs" not in payload

    def test_agent_uses_ask_stream_with_think_false(self):
        url, payload = bt.llm_request(bt.parse_llm("a=http://agent:8002/,kind=agent"), [])
        assert url == "http://agent:8002/ask_stream"
        assert payload["think"] is False


class TestAudioHelpers:
    def test_wav_seconds_sums_chunks(self):
        assert bt.wav_seconds([_wav(0.5), _wav(1.25)]) == pytest.approx(1.75)

    def test_join_wavs_concatenates(self):
        joined = bt.join_wavs([_wav(0.5), _wav(0.25)])
        assert bt.wav_seconds([joined]) == pytest.approx(0.75)

    def test_multipart_contains_field_and_payload(self):
        body, ctype = bt.multipart("audio", "turn.wav", b"RIFFDATA", "audio/wav")
        boundary = ctype.split("boundary=")[1]
        assert ctype.startswith("multipart/form-data; boundary=")
        assert b'name="audio"; filename="turn.wav"' in body
        assert b"RIFFDATA" in body and body.endswith(f"--{boundary}--\r\n".encode())


class TestSummary:
    def test_percentiles_and_failures_per_combination(self):
        rows = [
            {"llm": "a", "tts": "k", "ok": True, "stt_ms": 100, "llm_ms": 1000, "tts_ms": 300, "turn_ms": 1400},
            {"llm": "a", "tts": "k", "ok": True, "stt_ms": 200, "llm_ms": 3000, "tts_ms": 500, "turn_ms": 3700},
            {"llm": "a", "tts": "k", "ok": False, "error": "boom"},
            {"llm": "b", "tts": "k", "ok": True, "stt_ms": 0, "llm_ms": 50, "tts_ms": 0, "turn_ms": 50},
        ]
        by = {s["llm"]: s for s in bt.summarize(rows)}
        assert (by["a"]["ok"], by["a"]["n"]) == (2, 3)
        assert by["a"]["turn_ms_p50"] == pytest.approx(2550)
        assert by["b"]["llm_ms_p95"] == 50

    def test_save_writes_json_and_csv(self, tmp_path):
        rows = [{"llm": "a", "tts": "k", "ok": True, "turn_ms": 1.0}]
        stem = bt.save(rows, bt.summarize(rows), {"note": "t"}, tmp_path)
        data = json.loads(stem.with_suffix(".json").read_text())
        assert data["records"] == rows and data["metadata"] == {"note": "t"}
        assert stem.with_suffix(".csv").read_text().startswith("llm,ok,tts,turn_ms")

    def test_save_skips_empty_runs(self, tmp_path):
        assert bt.save([], [], {}, tmp_path) is None


def test_unreachable_input_tts_records_failures_instead_of_crashing():
    args = bt.build_parser().parse_args(["--timeout", "2", "hi"])
    args.input_tts = "http://127.0.0.1:9/"  # discard port: connection refused
    bench = bt.Bench(args, [bt.parse_llm(s) for s in bt.DEFAULT_LLMS], [("k", "http://127.0.0.1:9")])
    rows = bench.turn("hi", rep=0)
    assert len(rows) == 3 and not any(r["ok"] for r in rows)
    assert all(r["error"].startswith("input tts:") for r in rows)


def test_bad_repetitions_is_a_usage_error():
    assert bt.main(["--no-stt", "--repetitions", "0", "hi"]) == 2
