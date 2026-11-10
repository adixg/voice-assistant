#!/usr/bin/env python3
"""LLM benchmark for one GPU node: speed, load time, VRAM, tool calls.

    python3 benchmarks/llm/bench_llm.py benchmarks/llm/models-gtx1650.json [label ...]

Run it on the GPU node (stdlib only; it reads nvidia-smi there), or anywhere
with KUBECTL_PREFIX="ssh <server>" when the node has no kubeconfig. Settings
come from the environment: NODE_SELECTOR, CACHE_DIR (a hostPath for GGUFs),
CTX, MEM_LIMIT, POD, STANDBY_DEPLOY ("none" when the card has no standby to
yield to), USE_HF_CACHE=1 to reuse a llama-server's existing HF-layout cache.
The RTX 4060 run used CTX=40960 and the live server's flash-attention and
q8_0 KV flags (models-rtx4060.json); results go to benchmarks/runs/llm/.

It drives kubectl. For each model it
starts a one-off llama.cpp pod (same image and flags as the live server),
lets it download, restarts it to time a cached load, then measures:
  - speed: TTFT, generation tok/s, prompt tok/s, think off (and on for models
    whose template supports enable_thinking), with Rina's 20 real tools sent
    on every request like the live agent does;
  - tool calls: 13 fixed requests, first-turn tool choice and arguments;
  - VRAM: nvidia-smi before, after load, peak; GPU layers offloaded.
Pauses (and kills its pod) whenever the live standby LLM on this GPU is
scaled up, so failover keeps working.
"""
import json, os, re, subprocess, sys, threading, time, urllib.request

NS = "aicompanion"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
OUT = os.environ.get("OUT_DIR", os.path.join(REPO, "benchmarks", "runs", "llm"))
os.makedirs(OUT, exist_ok=True)
KENV = dict(os.environ, KUBECONFIG=os.path.expanduser("~/.kube/config"))
IMAGE = "ghcr.io/ggml-org/llama.cpp:server-cuda"
POD = os.environ.get("POD", "llmb")
STANDBY = os.environ.get("STANDBY_DEPLOY", "llama-cpp-gtx1650")
NODE_SELECTOR = json.loads(os.environ.get("NODE_SELECTOR", '{"gpu-tier":"gtx1650"}'))
CACHE = os.environ.get("CACHE_DIR", "/var/lib/aicompanion/bench-llm")
MEM = os.environ.get("MEM_LIMIT", "4Gi")
CTX = os.environ.get("CTX", "4096")

# The agent's real tool definitions, as the live agent sends them.
sys.path.insert(0, os.path.join(REPO, "tools"))
import companion_control_mcp  # noqa: E402

TOOLS = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                           "parameters": t["inputSchema"]}} for t in companion_control_mcp.TOOLS]
SENTENCES = [l.strip() for l in open(os.path.join(REPO, "benchmarks", "sentences.txt")) if l.strip()]
SYSTEM = ("You are Rina, a friendly voice assistant on a small desk device. "
          "Your replies are spoken aloud, so keep them short, natural and free of markdown. "
          "Use a tool when the request needs one; otherwise just answer.")

def has(s, *words):
    s = (s or "").lower()
    return all(w in s for w in words)

TOOL_CASES = [
    ("Remind me to call the dentist at 5 pm.", "set_reminder",
     lambda a: has(a.get("text"), "dentist") and "17" in str(a.get("at", ""))),
    ("Turn the volume down a bit.", "set_stick_volume", lambda a: (a.get("change") or 0) < 0 or "percent" in a),
    ("Set the volume to 40 percent.", "set_stick_volume", lambda a: a.get("percent") == 40),
    ("Make the screen brighter.", "set_stick_brightness", lambda a: (a.get("change") or 0) > 0 or "percent" in a),
    ("What's the weather like in Atlanta?", "get_weather", lambda a: has(a.get("location"), "atlanta")),
    ("What time is it?", "get_time", lambda a: True),
    ("Write down that I need to buy milk.", "add_note", lambda a: has(a.get("text"), "milk")),
    ("What reminders do I have?", "list_reminders", lambda a: True),
    ("Switch to the Kokoro Bella voice.", "set_voice",
     lambda a: has(a.get("voice"), "bella") and a.get("engine", "kokoro") == "kokoro"),
    ("Search the web for the latest llama.cpp release.", "search_web", lambda a: has(a.get("query"), "llama")),
    ("Is the GPU running hot right now?", "get_gpu_status", lambda a: True),
    ("Tell me a joke.", None, None),
    ("How are you doing today?", None, None),
]

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def kubectl(*args, inp=None, check=False):
    pre = os.environ.get("KUBECTL_PREFIX", "").split()
    if pre:  # e.g. "ssh -o BatchMode=yes rina": run kubectl on the server node
        import shlex
        cmd = pre + ["KUBECONFIG=$HOME/.kube/config kubectl -n " + NS + " " + " ".join(shlex.quote(a) for a in args)]
    else:
        cmd = ["kubectl", "-n", NS, *args]
    r = subprocess.run(cmd, input=inp, capture_output=True, text=True, env=KENV)
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()

def gpu_used():
    try:
        return int(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                  capture_output=True, text=True).stdout.split()[0])
    except Exception:
        return None

def standby_up():
    v = kubectl("get", "deploy", STANDBY, "-o", "jsonpath={.spec.replicas}")
    return v not in ("", "0")

def wait_standby_down():
    if standby_up():
        log("standby LLM is up (laptop away?) -- pausing")
        kubectl("delete", "pod", POD, "--ignore-not-found", "--wait=true")
        while standby_up():
            time.sleep(30)
        # let the standby pod actually leave the GPU
        while kubectl("get", "pods", "-l", f"app={STANDBY}", "--no-headers"):
            time.sleep(10)
        log("standby down again -- resuming")

def pod_spec(m):
    args = ["--hf-repo", m["repo"], "--hf-file", m["file"], "--alias", "bench", "--host", "0.0.0.0",
            "--port", "8080", "--jinja", "--ctx-size", CTX, "--n-gpu-layers", "99"] + m.get("extra", [])
    return json.dumps({
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": POD, "namespace": NS, "labels": {"app": "llm-bench"}},
        "spec": {"runtimeClassName": "nvidia", "restartPolicy": "Never", "nodeSelector": NODE_SELECTOR,
                 "containers": [{"name": "s", "image": IMAGE, "imagePullPolicy": "IfNotPresent", "args": args,
                                 "env": [{"name": "HF_HOME", "value": "/models/huggingface"}]
                                        + ([] if os.environ.get("USE_HF_CACHE") else [{"name": "LLAMA_CACHE", "value": "/models/llama"}]),
                                 "resources": {"requests": {"memory": "1Gi"}, "limits": {"memory": MEM, "nvidia.com/gpu": "1"}},
                                 "volumeMounts": [{"name": "c", "mountPath": "/models"}]}],
                 "volumes": [{"name": "c", "hostPath": {"path": CACHE, "type": "DirectoryOrCreate"}}]}})

def start_pod(m, timeout):
    kubectl("delete", "pod", POD, "--ignore-not-found", "--wait=true")
    t0 = time.time()
    kubectl("apply", "-f", "-", inp=pod_spec(m), check=True)
    ip = ""
    while time.time() - t0 < timeout:
        if standby_up():
            return None, None, "standby"
        ph = kubectl("get", "pod", POD, "-o", "jsonpath={.status.phase}")
        if ph in ("Failed", "Succeeded"):
            return None, time.time() - t0, "pod " + ph + ": " + kubectl("logs", POD, "--tail=15")[-1500:]
        ip = kubectl("get", "pod", POD, "-o", "jsonpath={.status.podIP}")
        if ip:
            try:
                if json.load(urllib.request.urlopen(f"http://{ip}:8080/health", timeout=3)).get("status") == "ok":
                    return ip, time.time() - t0, None
            except Exception:
                pass
        time.sleep(2)
    return None, time.time() - t0, "timeout; log: " + kubectl("logs", POD, "--tail=15")[-1500:]

def stream_chat(ip, messages, think, max_tokens, tools=True):
    body = {"model": "bench", "messages": messages, "stream": True, "max_tokens": max_tokens,
            "temperature": 0.7, "chat_template_kwargs": {"enable_thinking": think}}
    if tools:
        body["tools"] = TOOLS
    req = urllib.request.Request(f"http://{ip}:8080/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    t0 = time.time(); first = first_content = None; timings = {}; content = ""; reasoning = ""; ncalls = 0
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            d = json.loads(line[5:])
            timings = d.get("timings") or timings
            for ch in d.get("choices", []):
                de = ch.get("delta", {})
                if de.get("reasoning_content"):
                    reasoning += de["reasoning_content"]; first = first or time.time()
                if de.get("content"):
                    content += de["content"]; first = first or time.time(); first_content = first_content or time.time()
                if de.get("tool_calls"):
                    ncalls += 1; first = first or time.time(); first_content = first_content or time.time()
    end = time.time()
    return {"ttft_s": (first - t0) if first else None,
            "t_first_answer_s": (first_content - t0) if first_content else None,
            "total_s": end - t0,
            "gen_tok": timings.get("predicted_n"), "gen_tps": timings.get("predicted_per_second"),
            "prompt_tok": timings.get("prompt_n"), "prompt_tps": timings.get("prompt_per_second"),
            "reasoning_chars": len(reasoning), "content_chars": len(content), "tool_call_chunks": ncalls}

def tool_case(ip, prompt, want, check, think):
    body = {"model": "bench", "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "tools": TOOLS, "max_tokens": 1024 if think else 300, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": think}}
    req = urllib.request.Request(f"http://{ip}:8080/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    try:
        msg = json.load(urllib.request.urlopen(req, timeout=300))["choices"][0]["message"]
    except Exception as e:
        return {"prompt": prompt, "want": want, "ok": False, "error": str(e)[:200]}
    calls = msg.get("tool_calls") or []
    got = calls[0]["function"]["name"] if calls else None
    try:
        args = json.loads(calls[0]["function"].get("arguments") or "{}") if calls else {}
    except Exception:
        args = {"_unparseable": calls[0]["function"].get("arguments")}
    if want is None:
        ok = not calls
    else:
        ok = got == want and bool(check(args))
    return {"prompt": prompt, "want": want, "got": got, "args": args, "ok": ok,
            "reply": (msg.get("content") or "")[:160]}

def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100
    f = int(k); c = min(f + 1, len(xs) - 1)
    return round(xs[f] + (xs[c] - xs[f]) * (k - f), 3)

def summarize(rows):
    out = {"n": len(rows)}
    for k in ("ttft_s", "t_first_answer_s", "total_s", "gen_tps", "prompt_tps", "gen_tok"):
        out[k + "_p50"] = pct([r.get(k) for r in rows], 50)
        out[k + "_p95"] = pct([r.get(k) for r in rows], 95)
    return out

def bench_model(m):
    res = {"model": m, "image": IMAGE, "ctx": CTX, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "node_selector": NODE_SELECTOR, "system_prompt": SYSTEM, "tools_sent": len(TOOLS)}
    wait_standby_down()
    res["vram_idle_mib"] = gpu_used()
    log(m["label"], "first start (download + load)")
    ip, dt, err = start_pod(m, 3600)
    res["first_start_s"] = dt
    if err == "standby":
        return None
    if err:
        res["error"] = err; log(m["label"], "FAILED:", err[:300]); return res
    log(m["label"], f"first start {dt:.0f}s; restarting for a cached load")
    ip, dt, err = start_pod(m, 1800)
    if err == "standby":
        return None
    res["cached_load_s"] = dt
    if err:
        res["error"] = err; log(m["label"], "FAILED on cached load:", err[:300]); return res
    logs = kubectl("logs", POD)
    mm = re.search(r"offloaded (\d+)/(\d+) layers to GPU", logs)
    res["gpu_layers"] = f"{mm.group(1)}/{mm.group(2)}" if mm else None
    for pat, key in ((r"CUDA0 model buffer size\s*=\s*([\d.]+) MiB", "model_buf_mib"),
                     (r"CUDA0 KV buffer size\s*=\s*([\d.]+) MiB", "kv_buf_mib"),
                     (r"CUDA0 compute buffer size\s*=\s*([\d.]+) MiB", "compute_buf_mib")):
        mm = re.search(pat, logs)
        res[key] = float(mm.group(1)) if mm else None
    res["vram_loaded_mib"] = gpu_used()
    log(m["label"], f"loaded in {dt:.1f}s, layers {res['gpu_layers']}, VRAM {res['vram_loaded_mib']} MiB")

    peak = [res["vram_loaded_mib"] or 0]; stop = threading.Event()
    def poll():
        while not stop.is_set():
            v = gpu_used()
            if v: peak[0] = max(peak[0], v)
            time.sleep(1)
    th = threading.Thread(target=poll, daemon=True); th.start()
    try:
        modes = [False] + ([True] if m.get("think") else [])
        for think in modes:
            key = "think_on" if think else "think_off"
            reps = 10 if not think else 2
            rows = []
            for i in range(3 + reps):
                for s in SENTENCES:
                    if standby_up():
                        return None
                    try:
                        r = stream_chat(ip, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": s}],
                                        think, 1024 if think else 256)
                    except Exception as e:
                        r = {"error": str(e)[:200]}
                    if i >= 3:
                        r["sentence"] = s; rows.append(r)
            res[key] = {"summary": summarize([r for r in rows if "error" not in r]),
                        "errors": sum("error" in r for r in rows), "rows": rows}
            log(m["label"], key, json.dumps(res[key]["summary"]))
            tc = [tool_case(ip, p, w, c, think) for p, w, c in TOOL_CASES]
            res[key]["tools"] = {"passed": sum(t["ok"] for t in tc), "of": len(tc), "cases": tc}
            log(m["label"], key, f"tool calls {res[key]['tools']['passed']}/{len(tc)}",
                [t["prompt"][:25] + "->" + str(t.get("got")) for t in tc if not t["ok"]])
    finally:
        stop.set(); th.join()
    res["vram_peak_mib"] = peak[0]
    return res

def main():
    models = json.load(open(sys.argv[1]))
    only = set(sys.argv[2:])
    for m in models:
        if only and m["label"] not in only:
            continue
        path = os.path.join(OUT, f"{time.strftime('%Y%m%dT%H%M%S')}-{m['label']}.json")
        while True:
            res = bench_model(m)
            if res is not None:
                break
            wait_standby_down()  # interrupted by failover; start the model over
        json.dump(res, open(path, "w"), indent=1)
        log(m["label"], "saved", path)
        kubectl("delete", "pod", POD, "--ignore-not-found", "--wait=true")
    log("ALL DONE")

if __name__ == "__main__":
    main()
