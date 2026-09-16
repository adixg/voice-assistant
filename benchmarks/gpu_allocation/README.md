# benchmarks/gpu_allocation

Not built yet. Phase 4 of `docs/deployment-architecture.md`. Intent: measure
how quickly `controller/gpu_scheduler/` actually retargets the `agent`
service after the RTX 4060 node transitions Ready/NotReady (laptop
wake/sleep, network drop, `kubectl label` change) — the controller's whole
value is that this happens automatically and promptly instead of a human
running `kubectl set env` by hand, so that gap needs a real measured number,
not just "it should work."
