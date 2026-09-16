# observability/prometheus

Not built yet. Phase 3 of `docs/deployment-architecture.md`. Intent: each
service in `services/` exposes latency and (for stt/tts) GPU-memory metrics
at `/metrics`, scraped by a Prometheus instance in the cluster. This turns
the measured-by-hand numbers this repo already tracks in
`docs/hardware-budget.md` into a live, queryable series instead of a
point-in-time note in a markdown file.
