#!/usr/bin/env python3
"""Time the GPU scheduler's failover from the RTX 4060 to the GTX 1650 and
back, end to end, as seen by a client asking the agent a question.

    python3 benchmarks/gpu_allocation/bench_failover.py --runs 5 [--warm] [--out DIR]

Run it on the k3s server (stdlib only; it calls kubectl and reaches the
agent's ClusterIP). Each run takes the 4060's llama.cpp server down by scaling
llama-cpp-rtx4060 to 0 -- the controller treats a 4060 whose server isn't
Available exactly like a lost node -- then brings it back, and timestamps
every step against the trigger:

  departure: standby scaled up -> standby Ready -> agent retargeted ->
             agent rollout done -> first good reply (and how many failed)
  return:    4060 server Ready -> agent retargeted -> rollout done -> first
             good reply on the 4060 -> standby scaled back to 0 (60 s debounce)

plus the TTS Service moving between CPU and GPU when tts-gpu is deployed. A
probe asks the agent a short question once a second the whole time. --warm
pins the standby (tools/standby.sh warm) for the run and unpins it after.

This does not cover the node itself disappearing (laptop sleep or k3s-agent
stop), which adds Kubernetes' node-NotReady grace period on top.
"""
import argparse
import json
import os
import subprocess
import threading
import time
import urllib.request

NS = "aicompanion"
KENV = dict(os.environ, KUBECONFIG=os.path.expanduser("~/.kube/config"))
RTX, STANDBY, AGENT = "llama-cpp-rtx4060", "llama-cpp-gtx1650", "agent"
GTX_HOST, RTX_HOST = "http://llama-cpp-gtx1650:8080/v1", "http://llama-cpp-rtx4060:8080/v1"
QUESTION = {"messages": [{"role": "user", "content": "Say hi in three words."}], "think": False}


def kubectl(*args):
    return subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True, env=KENV).stdout


def deploy(name):
    out = kubectl("get", "deployment", name, "-o", "json")
    return json.loads(out) if out else None


def agent_host(d):
    for c in d["spec"]["template"]["spec"]["containers"]:
        for e in c.get("env") or []:
            if e["name"] == "LLM_HOST":
                return e.get("value")


def rolled_out(d):
    s, want = d.get("status", {}), d["spec"].get("replicas", 0)
    return (s.get("observedGeneration") == d["metadata"]["generation"] and s.get("updatedReplicas", 0) == want
            and s.get("readyReplicas", 0) == want and not s.get("unavailableReplicas"))


def ready(d):
    return bool(d) and (d.get("status", {}).get("readyReplicas") or 0) >= 1


def tts_target():
    out = kubectl("get", "service", "tts", "-o", "jsonpath={.spec.selector.app}")
    return "gpu" if out == "tts-gpu" else "cpu"


def gpu_tts_deployed():
    return deploy("tts-gpu") is not None


class Probe(threading.Thread):
    """Asks the agent once a second while `active` is set (only around a
    switch, so the GPU isn't generating nonstop); records (t, ok, seconds)."""

    def __init__(self, url, timeout=30):
        super().__init__(daemon=True)
        self.url, self.timeout = url, timeout
        self.log, self.stop, self.active = [], threading.Event(), threading.Event()

    def run(self):
        while not self.stop.is_set():
            if not self.active.wait(0.5):
                continue
            t0 = time.time()
            try:
                req = urllib.request.Request(self.url + "/ask", data=json.dumps(QUESTION).encode(),
                                             headers={"content-type": "application/json"})
                ok = bool(json.load(urllib.request.urlopen(req, timeout=self.timeout)).get("reply"))
            except Exception:
                ok = False
            self.log.append((t0, ok, time.time() - t0))
            self.stop.wait(max(0.0, 1.0 - (time.time() - t0)))

    def first_ok_after(self, t, min_ts=0):
        for ts, ok, dur in self.log:
            if ts >= max(t, min_ts) and ok:
                return ts + dur
        return None

    def failures_between(self, a, b):
        return sum(1 for ts, ok, _ in self.log if a <= ts < b and not ok)


def wait_for(cond, timeout, step=1.0):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return time.time()
        time.sleep(step)
    return None


def steady(timeout=900):
    """4060 serving, agent on it and rolled out, standby down (unless pinned),
    GPU TTS back on the card if deployed."""
    def ok():
        a, s, r = deploy(AGENT), deploy(STANDBY), deploy(RTX)
        pinned = (s["metadata"].get("annotations") or {}).get("aicompanion/keep-warm") == "true"
        base = (ready(r) and agent_host(a) == RTX_HOST and rolled_out(a)
                and (pinned or s["spec"].get("replicas", 0) == 0))
        if base and gpu_tts_deployed() and not pinned:
            return tts_target() == "gpu"
        return base
    return wait_for(ok, timeout, step=3)


def run_once(probe, warm):
    rel = {}
    t0 = time.time()
    probe.active.set()
    kubectl("scale", "deployment", RTX, "--replicas=0")
    events = {
        "standby_scaled_up": lambda: deploy(STANDBY)["spec"].get("replicas", 0) >= 1,
        "standby_ready": lambda: ready(deploy(STANDBY)),
        "agent_retargeted": lambda: agent_host(deploy(AGENT)) == GTX_HOST,
        "tts_on_cpu": lambda: tts_target() == "cpu",
    }
    pending = dict(events)
    while pending and time.time() - t0 < 900:
        for name in list(pending):
            if pending[name]():
                rel[name] = round(time.time() - t0, 1)
                del pending[name]
        time.sleep(0.5)
    t_rt = t0 + rel.get("agent_retargeted", 0)
    t = wait_for(lambda: agent_host(deploy(AGENT)) == GTX_HOST and rolled_out(deploy(AGENT)), 600, 0.5)
    rel["agent_rolled_out"] = round(t - t0, 1) if t else None
    t = wait_for(lambda: probe.first_ok_after(t_rt) is not None, 300, 0.5)
    first = probe.first_ok_after(t_rt)
    rel["first_good_reply"] = round(first - t0, 1) if first else None
    rel["failed_probes"] = probe.failures_between(t0, first or time.time())
    probe.active.clear()

    # return
    back = {}
    t1 = time.time()
    probe.active.set()
    kubectl("scale", "deployment", RTX, "--replicas=1")
    t = wait_for(lambda: ready(deploy(RTX)), 600, 0.5)
    back["rtx_ready"] = round(t - t1, 1) if t else None
    t = wait_for(lambda: agent_host(deploy(AGENT)) == RTX_HOST, 300, 0.5)
    back["agent_retargeted"] = round(t - t1, 1) if t else None
    t_rt = t or time.time()
    t = wait_for(lambda: agent_host(deploy(AGENT)) == RTX_HOST and rolled_out(deploy(AGENT)), 600, 0.5)
    back["agent_rolled_out"] = round(t - t1, 1) if t else None
    t = wait_for(lambda: probe.first_ok_after(t_rt) is not None, 300, 0.5)
    first = probe.first_ok_after(t_rt)
    back["first_good_reply"] = round(first - t1, 1) if first else None
    back["failed_probes"] = probe.failures_between(t1, first or time.time())
    probe.active.clear()
    if not warm:
        t = wait_for(lambda: deploy(STANDBY)["spec"].get("replicas", 0) == 0, 600, 1)
        back["standby_scaled_down"] = round(t - t1, 1) if t else None
        if gpu_tts_deployed():
            t = wait_for(lambda: tts_target() == "gpu", 900, 2)
            back["tts_on_gpu"] = round(t - t1, 1) if t else None
    return {"departure": rel, "return": back}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--warm", action="store_true")
    ap.add_argument("--probe-timeout", type=float, default=30,
                    help="seconds a probe waits for a reply; a short one cuts off the first, slow request "
                         "to a fresh server and retries it")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runs",
                                                  "gpu_allocation"))
    args = ap.parse_args()
    url = "http://" + kubectl("get", "svc", AGENT, "-o", "jsonpath={.spec.clusterIP}:{.spec.ports[0].port}")
    if args.warm:
        kubectl("annotate", "deployment", STANDBY, "aicompanion/keep-warm=true", "--overwrite")
    probe = Probe(url, args.probe_timeout)
    probe.start()
    runs = []
    try:
        for i in range(args.runs):
            print(time.strftime("%H:%M:%S"), f"waiting for steady state before run {i + 1}", flush=True)
            if not steady():
                print("never reached steady state; stopping", flush=True)
                break
            time.sleep(10)
            r = run_once(probe, args.warm)
            print(time.strftime("%H:%M:%S"), json.dumps(r), flush=True)
            runs.append(r)
    finally:
        probe.stop.set()
        if args.warm:
            kubectl("annotate", "deployment", STANDBY, "aicompanion/keep-warm-")
        if (deploy(RTX) or {}).get("spec", {}).get("replicas", 1) == 0:
            kubectl("scale", "deployment", RTX, "--replicas=1")  # never leave the 4060 down
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                        + f"-failover-{'warm' if args.warm else 'cold'}.json")
    meta = {"trigger": "scale llama-cpp-rtx4060 to 0 and back", "warm_standby": args.warm,
            "gpu_tts_deployed": gpu_tts_deployed(), "probe": "POST /ask once a second around each switch",
            "probe_timeout_s": args.probe_timeout, "runs": len(runs)}
    json.dump({"metadata": meta, "runs": runs}, open(path, "w"), indent=1)
    print("saved", path, flush=True)


if __name__ == "__main__":
    main()
