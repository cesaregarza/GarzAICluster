# Cluster Identity

This document records the GarzAICluster production cluster identity and the
current decision on legacy `splattop` names. The goal is to make the user-facing
operator identity read `garz.ai` without causing a flag-day Argo or monitoring
history reset.

## Live Identity

Observed on 2026-06-23:

| Surface | Current value | Decision |
| --- | --- | --- |
| DigitalOcean Kubernetes cluster name | `k8s-nyc3-garz-ai` | Moved to garz.ai identity |
| DigitalOcean Kubernetes cluster ID | `4124737a-6816-4ee9-af15-cf1c0f6f2f65` | Immutable provider ID |
| Kubernetes context | `do-nyc3-k8s-nyc3-garz-ai` | Moved to garz.ai identity |
| Region | `nyc3` | Stays |
| GitOps repository | `cesaregarza/GarzAICluster` | Moved to GarzAICluster |

Source-controlled manifests do not pin the old kubectl context name. Operators
should refresh local kubeconfig with:

```bash
doctl kubernetes cluster kubeconfig save 4124737a-6816-4ee9-af15-cf1c0f6f2f65
kubectl config use-context do-nyc3-k8s-nyc3-garz-ai
```

## Names That Stay For Now

These names still contain `splattop` by design. They are internal resource,
product, or history-bearing identifiers and must not be renamed casually.

| Name family | Current examples | Reason to keep |
| --- | --- | --- |
| Argo AppProject | `argocd/projects/splattop-project.yaml`, live project `splattop` | AppProject names are effectively delete-and-recreate; current RBAC policies are scoped to `proj:splattop:*` |
| Root and umbrella apps | `splattop-root`, `splattop-prod` | App names are immutable; renaming means delete-and-recreate and must be sequenced so children are not orphaned |
| Monitoring Helm release resources | `splattop-prod-prometheus`, `splattop-prod-grafana`, `splattop-prod-alertmanager` | Renaming a Helm release would recreate resources and risks Prometheus TSDB and Grafana state continuity |
| SplatTop product workloads | `helm/splattop`, `splattop-teams`, `splattop-blog`, `splatvote` | These are product names, not cluster identity names |
| Bot namespace/application prefix | `splattop-bot-*`, `splattop-bots` | Existing AppSets and namespace policies depend on the prefix; migrate separately if the product prefix changes |
| Historical docs | `docs/historical/**` | Kept as dated evidence, not current operator instructions |

## Future Rename Sequence

If an operator later chooses to rename internal Argo or Helm release resources,
do it as a staged maintenance event:

1. Decide the target names and update this matrix before changing manifests.
2. Preserve monitoring state first. For Prometheus and Grafana, identify the
   PVCs, ConfigMaps, and Secrets that must be retained before any release rename.
3. Create replacement AppProject and Application manifests in Git with
   destination, source repo, and RBAC parity.
4. Sync replacement apps while old apps are still present, then verify
   `Synced` and `Healthy`.
5. Delete the old Application resources only after the replacement owns the same
   rendered Kubernetes resources or after an explicit cutover window.
6. Keep redirects, DNS, and operator docs compatible until all live Argo and
   monitoring evidence is green.

The lower-risk current state is intentional: user-facing provider and kubectl
identity are garz.ai, while high-risk internal names remain stable until a
separate operator-approved cutover preserves state.
