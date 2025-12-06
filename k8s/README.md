# k8s Directory

This folder is reserved for Argo CD bootstrap artifacts only (e.g., repo-server patches, ksops enablement, Argo ingress/cert). Runtime workloads and infra apps (external-dns, vanity-hosts, charts, etc.) live elsewhere:

- Argo applications/config: `argocd/`
- Helm charts: `helm/`
- Infra kustomizations: `infra/`
- Encrypted secrets: `secrets/`

Avoid adding new workload manifests here; use Helm or Argo-managed paths instead.
