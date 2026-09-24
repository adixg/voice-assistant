"""A small NVML exporter for Prometheus, replacing NVIDIA's dcgm-exporter.

dcgm-exporter embeds the whole DCGM host engine (built for datacenter fleets)
to export a handful of numbers, and held ~405 MiB of anonymous memory per node
doing it (measured 2026-09-24, arch-ssd). This reads the same numbers from NVML,
the library nvidia-smi uses, at scrape time.

Drop-in: the six DCGM_FI_DEV_* series the dashboard, tools/obs_tui.py and the
agent's GPU tool read keep their names, units and labels (hostname, gpu, UUID,
modelName). Unlike DCGM there is one series per GPU, not one per pod sharing a
time-sliced GPU (those duplicates carried the whole card's numbers anyway).

Added, under aicompanion_gpu_*:
  clock_mhz{clock=sm|mem}            what the card is actually running at
  throttle{reason=...}               1 while that reason is holding clocks down
                                     (thermal, power cap, ...): arch-ssd is a
                                     laptop, so thermal throttling is plausible
  energy_joules_total                counter since the driver loaded
  xid_errors_total{xid=...}          driver-reported GPU faults (XID codes)
  process_memory_bytes{namespace,pod,command}
                                     VRAM per process, attributed to its pod via
                                     the host's /proc (needs hostPID: NVML only
                                     lists processes in its own PID namespace)
                                     and /var/log/pods; NVML can't report it
                                     under WSL2, so the laptop node exports none
"""
import argparse
import os
import re
import threading
import time

import pynvml
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

MIB = 1024 * 1024

# NVML clocks-event (throttle) reason bits, from nvml.h. Named here rather than
# imported, since nvidia-ml-py has renamed these constants between releases.
THROTTLE_REASONS = {
    "gpu_idle": 0x1,
    "applications_clocks_setting": 0x2,
    "sw_power_cap": 0x4,
    "hw_slowdown": 0x8,
    "sync_boost": 0x10,
    "sw_thermal_slowdown": 0x20,
    "hw_thermal_slowdown": 0x40,
    "hw_power_brake_slowdown": 0x80,
    "display_clock_setting": 0x100,
}

# A pod's cgroup path carries its UID: "...-pod5c162618_4844_4114_817d_2369463f1698.slice"
# with the systemd cgroup driver, dashes instead of underscores with cgroupfs.
_POD_UID = re.compile(r"pod([0-9a-f]{8}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{12})")


def _call(fn, *args, default=None):
    """NVML calls a given card or driver doesn't support raise; export what works."""
    try:
        return fn(*args)
    except pynvml.NVMLError:
        return default


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


class PodResolver:
    """host PID -> (namespace, pod, command), from the host's /proc (mounted at
    proc_root) and the kubelet's /var/log/pods/<namespace>_<pod>_<uid> dirs."""

    def __init__(self, proc_root, pod_logs):
        self.proc_root, self.pod_logs = proc_root, pod_logs

    def _pods_by_uid(self):
        pods = {}
        try:
            for entry in os.listdir(self.pod_logs):
                parts = entry.split("_")
                if len(parts) == 3:
                    pods[parts[2].replace("-", "_")] = (parts[0], parts[1])
        except OSError:
            pass
        return pods

    def resolve(self, pids):
        pods, out = self._pods_by_uid(), {}
        for pid in pids:
            namespace = pod = ""
            command = "?"
            try:
                with open(os.path.join(self.proc_root, str(pid), "cgroup")) as f:
                    match = _POD_UID.search(f.read())
                if match:
                    namespace, pod = pods.get(match.group(1).replace("-", "_"), ("", ""))
                with open(os.path.join(self.proc_root, str(pid), "comm")) as f:
                    command = f.read().strip()
            except OSError:
                pass
            out[pid] = (namespace, pod, command)
        return out


class GpuCollector:
    """Reads NVML on every scrape; nothing runs between scrapes."""

    def __init__(self, hostname, resolver, xid_counts):
        self.hostname, self.resolver, self.xid_counts = hostname, resolver, xid_counts

    def describe(self):
        return []  # don't touch NVML at registration time

    def collect(self):
        base = ["gpu", "UUID", "modelName", "hostname"]

        def gauge(name, doc, extra=()):
            return GaugeMetricFamily(name, doc, labels=base + list(extra))

        util = gauge("DCGM_FI_DEV_GPU_UTIL", "GPU utilization (in %).")
        fb_used = gauge("DCGM_FI_DEV_FB_USED", "Framebuffer memory used (in MiB).")
        fb_free = gauge("DCGM_FI_DEV_FB_FREE", "Framebuffer memory free (in MiB).")
        fb_reserved = gauge("DCGM_FI_DEV_FB_RESERVED", "Framebuffer memory reserved (in MiB).")
        temp = gauge("DCGM_FI_DEV_GPU_TEMP", "GPU temperature (in C).")
        power = gauge("DCGM_FI_DEV_POWER_USAGE", "Power draw (in W).")
        clock = gauge("aicompanion_gpu_clock_mhz", "Current clock (MHz).", ["clock"])
        throttle = gauge("aicompanion_gpu_throttle", "1 while this reason holds the clocks down.", ["reason"])
        energy = CounterMetricFamily("aicompanion_gpu_energy_joules",
                                     "Energy used since the driver loaded (J).", labels=base)
        xid = CounterMetricFamily("aicompanion_gpu_xid_errors", "Driver-reported GPU faults, by XID code.",
                                  labels=base + ["xid"])
        proc_mem = gauge("aicompanion_gpu_process_memory_bytes", "VRAM used per process.",
                         ["namespace", "pod", "command"])
        reasons_fn = (getattr(pynvml, "nvmlDeviceGetCurrentClocksEventReasons", None)
                      or pynvml.nvmlDeviceGetCurrentClocksThrottleReasons)

        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            labels = [str(i), _text(pynvml.nvmlDeviceGetUUID(h)), _text(pynvml.nvmlDeviceGetName(h)),
                      self.hostname]
            rates = _call(pynvml.nvmlDeviceGetUtilizationRates, h)
            if rates is not None:
                util.add_metric(labels, rates.gpu)
            mem = _call(pynvml.nvmlDeviceGetMemoryInfo, h, pynvml.nvmlMemory_v2)
            if mem is not None:
                fb_used.add_metric(labels, mem.used / MIB)
                fb_free.add_metric(labels, mem.free / MIB)
                fb_reserved.add_metric(labels, mem.reserved / MIB)
            t = _call(pynvml.nvmlDeviceGetTemperature, h, pynvml.NVML_TEMPERATURE_GPU)
            if t is not None:
                temp.add_metric(labels, t)
            mw = _call(pynvml.nvmlDeviceGetPowerUsage, h)
            if mw is not None:
                power.add_metric(labels, mw / 1000)
            for name, kind in (("sm", pynvml.NVML_CLOCK_SM), ("mem", pynvml.NVML_CLOCK_MEM)):
                mhz = _call(pynvml.nvmlDeviceGetClockInfo, h, kind)
                if mhz is not None:
                    clock.add_metric(labels + [name], mhz)
            reasons = _call(reasons_fn, h)
            if reasons is not None:
                for name, bit in THROTTLE_REASONS.items():
                    throttle.add_metric(labels + [name], 1 if reasons & bit else 0)
            mj = _call(pynvml.nvmlDeviceGetTotalEnergyConsumption, h)
            if mj is not None:
                energy.add_metric(labels, mj / 1000)
            for code, count in sorted(self.xid_counts.get(labels[1], {}).items()):
                xid.add_metric(labels + [str(code)], count)
            procs = _call(pynvml.nvmlDeviceGetComputeRunningProcesses, h, default=[]) or []
            owners = self.resolver.resolve([p.pid for p in procs])
            totals = {}
            for p in procs:
                if p.usedGpuMemory is not None:
                    totals[owners[p.pid]] = totals.get(owners[p.pid], 0) + p.usedGpuMemory
            for (namespace, pod, command), used in sorted(totals.items()):
                proc_mem.add_metric(labels + [namespace, pod, command], used)

        yield from (util, fb_used, fb_free, fb_reserved, temp, power, clock, throttle, energy, xid, proc_mem)


def watch_xid(counts, stop):
    """Count XID events: NVML only delivers them as events, not as a value to
    read. Best effort, since some platforms (WSL2) don't support events."""
    try:
        events = pynvml.nvmlEventSetCreate()
        for i in range(pynvml.nvmlDeviceGetCount()):
            pynvml.nvmlDeviceRegisterEvents(pynvml.nvmlDeviceGetHandleByIndex(i),
                                            pynvml.nvmlEventTypeXidCriticalError, events)
    except pynvml.NVMLError as e:
        print(f"XID events unavailable: {e}", flush=True)
        return
    while not stop.is_set():
        try:
            data = pynvml.nvmlEventSetWait_v2(events, 5000)
        except pynvml.NVMLError_Timeout:
            continue
        except pynvml.NVMLError as e:
            print(f"XID watch stopped: {e}", flush=True)
            return
        uuid = _text(pynvml.nvmlDeviceGetUUID(data.device))
        per_gpu = counts.setdefault(uuid, {})
        per_gpu[data.eventData] = per_gpu.get(data.eventData, 0) + 1
        print(f"XID {data.eventData} on {uuid}", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9400)
    ap.add_argument("--hostname", default=os.environ.get("NODE_NAME") or os.uname().nodename)
    ap.add_argument("--proc-root", default="/host/proc")
    ap.add_argument("--pod-logs", default="/var/log/pods")
    args = ap.parse_args(argv)

    pynvml.nvmlInit()
    print(f"NVML driver {_text(pynvml.nvmlSystemGetDriverVersion())}, {pynvml.nvmlDeviceGetCount()} GPU(s), "
          f"serving :{args.port} as {args.hostname}", flush=True)
    xid_counts = {}
    REGISTRY.register(GpuCollector(args.hostname, PodResolver(args.proc_root, args.pod_logs), xid_counts))
    start_http_server(args.port)
    stop = threading.Event()
    threading.Thread(target=watch_xid, args=(xid_counts, stop), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
