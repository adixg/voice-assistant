# deploy/helm

Not built yet. Phase 2 of `docs/deployment-architecture.md`: package the
manifests in `deploy/kubernetes/` (which are the source of truth for now)
into a chart once they've actually been run against the live cluster and
stopped changing shape — charting manifests that are still being debugged
just means editing templates instead of YAML for no benefit.
