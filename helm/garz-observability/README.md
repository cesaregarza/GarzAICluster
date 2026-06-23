# Garz Observability Helm Chart

This chart deploys the cluster-level Prometheus, Grafana, and Alertmanager stack
for GarzAICluster.

## Cutover Notes

The chart currently preserves the live `splattop-prod` object name prefix and
`app.kubernetes.io/name=splattop` / `app.kubernetes.io/instance=splattop-prod`
selector labels. That lets Argo move ownership out of the SplatTop app chart
without renaming the Prometheus StatefulSet or Grafana PVC.

CES-257 records the state-preservation choice: do not re-bind storage during the
extraction. The expected stateful identities are:

- Prometheus StatefulSet: `splattop-prod-prometheus`
- Prometheus generated TSDB PVC: `prometheus-data-splattop-prod-prometheus-0`
- Grafana runtime-state PVC: `splattop-prod-grafana-storage`

If a later cleanup renames the chart, release, StatefulSet, selector labels, or
PVC names, perform an explicit PV retain/re-bind migration first. Do not let a
Helm rename create replacement volumes implicitly.

## Production

The `garz-observability` Argo application renders this chart with
`values-prod.yaml` into the `monitoring` namespace. It keeps the existing
annotation-based `kubernetes-pods` Prometheus scrape config, alert rules,
Grafana dashboards, Grafana ingress, Alertmanager, network policies, and PDBs.

The Prometheus rule ConfigMap is split by ownership:

- `cluster-mandate-alerts.yaml` owns cluster, Mandate, Prometheus,
  Alertmanager, and Grafana health alerts.
- `splattop-app-alerts.yaml` keeps the existing SplatTop application alerts in
  this chart for now, so the observability extraction has no alert gap. Those
  app alerts are deliberately named and grouped separately until SplatTop owns a
  rule-mount path.

The public Grafana host is `grafana.garz.ai`. The previous
`grafana.splat.top` hostname is kept as a redirect-only vanity host during the
rename window.

Required pre-existing secrets:

- `grafana-admin-credentials`
- `alertmanager-config` unless `monitoring.alertmanager.config.create` is true

Render locally:

```bash
helm template garz-observability ./helm/garz-observability -n monitoring -f ./helm/garz-observability/values-prod.yaml
```
