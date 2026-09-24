# GPU allocation and failover benchmarks

Measure an intentional 4060 departure and return at least five times each.
Do this only between voice turns; it deliberately causes an agent rollout.

For every run, timestamp: trigger (laptop sleep/k3s-agent stop), Kubernetes
Node `NotReady`, scheduler log scaling `llama-cpp-gtx1650` to 1,
`llama-cpp-gtx1650` Ready (about 61 s from zero, measured 2026-09-24),
scheduler log showing the `LLM_HOST` change, new agent pod Ready, and the
first successful `POST /ask`. Run it both from zero (the default) and with the
standby pinned by `tools/standby.sh warm`; on return, also record the 60 s
debounce and the standby being scaled back to 0. Report trigger → first successful
reply as the primary recovery metric, plus each intermediate interval.

Capture resource state before and during load on each node: `nvidia-smi`
(VRAM used, utilization, power, temperature), pod restarts, and error rate.
Use the same benchmark prompt across the 1650 and 4060 so model-size changes
are labelled rather than mistaken for routing overhead.
