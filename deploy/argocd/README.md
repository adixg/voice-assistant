# deploy/argocd

Not built yet. Phase 2 of `docs/deployment-architecture.md`, after
`deploy/helm/` exists: an ArgoCD `Application` pointing at this repo so a
push to `main` syncs the cluster, rather than manual `kubectl apply`.
