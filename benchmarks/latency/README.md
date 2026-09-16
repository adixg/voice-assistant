# benchmarks/latency

Not built yet. Phase 4 of `docs/deployment-architecture.md`. Intent: measure
end-to-end turn latency (mic stop -> reply audio starts playing) for the
service-split architecture (`services/` over HTTP, `deploy/kubernetes/`) and
compare directly against the monolithic `bridge_server.py`'s numbers, since
splitting into network-separated services necessarily adds hops on a path
where latency is what users actually feel — see the caution about this in
`docs/deployment-architecture.md`'s design section. This directory should
hold the actual measured numbers once that comparison is run, same
convention as `docs/hardware-budget.md`.
