# Garz Observability Helm Chart

This chart deploys the cluster-level Prometheus, Grafana, and Alertmanager stack
for GarzAICluster.

## Cutover Notes

The chart currently preserves the live `splattop-prod` object name prefix and
`app.kubernetes.io/name=splattop` / `app.kubernetes.io/instance=splattop-prod`
selector labels. That lets Argo move ownership out of the SplatTop app chart
without renaming the Prometheus StatefulSet or Grafana PVC. CES-257 owns the
explicit state-preserving PV/PVC migration and any later selector cleanup.

## Production

The `garz-observability` Argo application renders this chart with
`values-prod.yaml` into the `monitoring` namespace. It keeps the existing
annotation-based `kubernetes-pods` Prometheus scrape config, alert rules,
Grafana dashboards, Grafana ingress, Alertmanager, network policies, and PDBs.

Required pre-existing secrets:

- `grafana-admin-credentials`
- `alertmanager-config` unless `monitoring.alertmanager.config.create` is true

Render locally:

```bash
helm template garz-observability ./helm/garz-observability -n monitoring -f ./helm/garz-observability/values-prod.yaml
```
