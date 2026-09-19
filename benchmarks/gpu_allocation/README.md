# GPU allocation and failover benchmarks

Measure an intentional 4060 departure and return at least five times each.
Do this only between voice turns; it deliberately causes an agent rollout.

For every run, timestamp: trigger (laptop sleep/k3s-agent stop), Kubernetes
Node `NotReady`, scheduler log showing `LLM_HOST` change, new agent pod
Ready, and the first successful `POST /ask`. Report trigger → first successful
reply as the primary recovery metric, plus each intermediate interval.

Capture resource state before and during load on each node: `nvidia-smi`
(VRAM used, utilization, power, temperature), pod restarts, and error rate.
Use the same benchmark prompt across the 1650 and 4060 so model-size changes
are labelled rather than mistaken for routing overhead.
