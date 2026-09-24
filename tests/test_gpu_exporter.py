"""services/gpu_exporter/exporter.py against a fake NVML, so no GPU is needed."""
import importlib
import sys
import types
from types import SimpleNamespace as NS

import pytest
from prometheus_client import CollectorRegistry, generate_latest


class NVMLError(Exception):
    pass


def fake_pynvml(procs=(), reasons=0x1, unsupported=()):
    """A pynvml stand-in: one GTX 1650. Names in `unsupported` raise NVMLError."""
    m = types.ModuleType("pynvml")
    m.NVMLError = NVMLError
    m.NVMLError_Timeout = type("NVMLError_Timeout", (NVMLError,), {})
    m.nvmlMemory_v2 = 2
    m.NVML_TEMPERATURE_GPU, m.NVML_CLOCK_SM, m.NVML_CLOCK_MEM = 0, 1, 2
    values = {
        "nvmlDeviceGetCount": lambda: 1,
        "nvmlDeviceGetHandleByIndex": lambda i: "h0",
        "nvmlDeviceGetUUID": lambda h: b"GPU-abc",
        "nvmlDeviceGetName": lambda h: "NVIDIA GeForce GTX 1650",
        "nvmlDeviceGetUtilizationRates": lambda h: NS(gpu=37),
        "nvmlDeviceGetMemoryInfo": lambda h, v: NS(used=1024 * 2**20, free=2048 * 2**20, reserved=256 * 2**20),
        "nvmlDeviceGetTemperature": lambda h, kind: 55,
        "nvmlDeviceGetPowerUsage": lambda h: 12_500,
        "nvmlDeviceGetClockInfo": lambda h, kind: {1: 1590, 2: 6001}[kind],
        "nvmlDeviceGetCurrentClocksEventReasons": lambda h: reasons,
        "nvmlDeviceGetTotalEnergyConsumption": lambda h: 3_600_000,
        "nvmlDeviceGetComputeRunningProcesses": lambda h: list(procs),
    }
    for name, fn in values.items():
        if name in unsupported:
            def raiser(*a, _n=name):
                raise NVMLError(_n)
            fn = raiser
        setattr(m, name, fn)
    return m


@pytest.fixture
def exporter(monkeypatch):
    def load(**kw):
        monkeypatch.setitem(sys.modules, "pynvml", fake_pynvml(**kw))
        sys.modules.pop("services.gpu_exporter.exporter", None)
        return importlib.import_module("services.gpu_exporter.exporter")
    return load


def scrape(mod, resolver=None, xid=None):
    registry = CollectorRegistry()
    resolver = resolver or NS(resolve=lambda pids: {p: ("", "", "?") for p in pids})
    registry.register(mod.GpuCollector("arch-ssd", resolver, xid or {}))
    return generate_latest(registry).decode()


def value(text, prefix):
    lines = [line for line in text.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, (prefix, lines)
    return float(lines[0].rsplit(" ", 1)[1])


LABELS = 'UUID="GPU-abc",gpu="0",hostname="arch-ssd",modelName="NVIDIA GeForce GTX 1650"'


def test_keeps_the_dcgm_names_units_and_labels(exporter):
    text = scrape(exporter())
    assert value(text, "DCGM_FI_DEV_GPU_UTIL{" + LABELS) == 37
    assert value(text, "DCGM_FI_DEV_FB_USED{" + LABELS) == 1024      # MiB, like DCGM
    assert value(text, "DCGM_FI_DEV_FB_FREE{" + LABELS) == 2048
    assert value(text, "DCGM_FI_DEV_FB_RESERVED{" + LABELS) == 256
    assert value(text, "DCGM_FI_DEV_GPU_TEMP{" + LABELS) == 55
    assert value(text, "DCGM_FI_DEV_POWER_USAGE{" + LABELS) == 12.5  # W, not mW


def test_clocks_energy_and_throttle_reasons(exporter):
    text = scrape(exporter(reasons=0x1 | 0x40))
    assert value(text, 'aicompanion_gpu_clock_mhz{' + LABELS.replace('gpu="0"', 'clock="sm",gpu="0"')) == 1590
    assert value(text, "aicompanion_gpu_energy_joules_total{" + LABELS) == 3600  # from mJ
    throttle = {line.split('reason="')[1].split('"')[0]: float(line.rsplit(" ", 1)[1])
                for line in text.splitlines() if line.startswith("aicompanion_gpu_throttle{")}
    assert throttle["gpu_idle"] == 1 and throttle["hw_thermal_slowdown"] == 1
    assert throttle["sw_power_cap"] == 0


def test_vram_per_process_is_summed_per_pod(exporter):
    procs = [NS(pid=10, usedGpuMemory=3 * 2**30), NS(pid=11, usedGpuMemory=2**30),
             NS(pid=12, usedGpuMemory=None)]  # NVML reports None when it can't tell
    owners = {10: ("aicompanion", "llama-cpp-gtx1650-x", "llama-server"),
              11: ("aicompanion", "llama-cpp-gtx1650-x", "llama-server"),
              12: ("aicompanion", "tts-y", "python3")}
    text = scrape(exporter(procs=procs), resolver=NS(resolve=lambda pids: {p: owners[p] for p in pids}))
    rows = [line for line in text.splitlines() if line.startswith("aicompanion_gpu_process_memory_bytes{")]
    assert len(rows) == 1 and 'pod="llama-cpp-gtx1650-x"' in rows[0]
    assert float(rows[0].rsplit(" ", 1)[1]) == 4 * 2**30


def test_an_unsupported_call_drops_that_metric_not_the_scrape(exporter):
    text = scrape(exporter(unsupported={"nvmlDeviceGetTotalEnergyConsumption",
                                        "nvmlDeviceGetComputeRunningProcesses"}))  # e.g. WSL2
    assert "aicompanion_gpu_energy_joules_total{" not in text
    assert value(text, "DCGM_FI_DEV_GPU_UTIL{" + LABELS) == 37


def test_xid_counts_are_exported_per_code(exporter):
    text = scrape(exporter(), xid={"GPU-abc": {79: 2}})
    assert value(text, 'aicompanion_gpu_xid_errors_total{' + LABELS + ',xid="79"}') == 2


def test_pod_resolver_maps_a_host_pid_to_its_pod(exporter, tmp_path):
    mod = exporter()
    uid = "5c162618-4844-4114-817d-2369463f1698"
    (tmp_path / "pods" / f"aicompanion_tts-abc_{uid}").mkdir(parents=True)
    proc = tmp_path / "proc" / "42"
    proc.mkdir(parents=True)
    (proc / "cgroup").write_text(
        "0::/kubepods.slice/kubepods-burstable.slice/"
        f"kubepods-burstable-pod{uid.replace('-', '_')}.slice/cri-containerd-1.scope\n")
    (proc / "comm").write_text("python3\n")
    resolver = mod.PodResolver(str(tmp_path / "proc"), str(tmp_path / "pods"))
    assert resolver.resolve([42, 999]) == {42: ("aicompanion", "tts-abc", "python3"), 999: ("", "", "?")}
