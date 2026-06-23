# Observability State Preservation

CES-257 chooses the lowest-risk preservation path for the CES-256 observability
extraction: keep the live Prometheus and Grafana stateful object identities
unchanged while Argo ownership moves from `splattop-prod` to
`garz-observability`.

## Decision

Use stable Kubernetes object names and selector labels for the extraction. Do
not perform a PV retain/re-bind cutover during CES-256.

The production render must keep these identities:

- Prometheus StatefulSet: `splattop-prod-prometheus`
- Prometheus generated TSDB PVC: `prometheus-data-splattop-prod-prometheus-0`
- Grafana runtime-state PVC: `splattop-prod-grafana-storage`
- Stateful selector labels:
  - `app.kubernetes.io/name: splattop`
  - `app.kubernetes.io/instance: splattop-prod`

This preserves the existing DO block-storage PV bindings. It also avoids the
double-billing/orphan risk of accidentally creating fresh volumes during the
chart extraction.

## Operator Cutover Check

Before syncing `garz-observability`, capture the live bindings:

```bash
kubectl get pvc -n monitoring \
  prometheus-data-splattop-prod-prometheus-0 \
  splattop-prod-grafana-storage \
  -o wide

kubectl get pvc -n monitoring \
  prometheus-data-splattop-prod-prometheus-0 \
  splattop-prod-grafana-storage \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.volumeName}{"\n"}{end}'
```

Sync `garz-observability` before pruning the moved resources from
`splattop-prod`. After both Argo apps settle, re-run the same commands and
confirm the PVC names and `.spec.volumeName` values match the pre-cutover
capture.

Then confirm no replacement monitoring volumes were created:

```bash
kubectl get pvc -n monitoring | grep -E 'prometheus|grafana'
kubectl get pv | grep -E 'prometheus|grafana|splattop-prod'
```

Acceptance evidence is the before/after PVC-to-PV binding match plus Prometheus
and Grafana healthy pods using those claims.

## Future Rename Path

A later cleanup may choose to rename the release, chart prefix, selector labels,
StatefulSet, or PVCs away from `splattop-prod`. That is not part of CES-256 or
this CES-257 decision. If that rename happens, use a separate PV retain/re-bind
runbook:

1. Set the existing PVs' reclaim policy to `Retain`.
2. Scale down Prometheus and Grafana.
3. Delete the old PVC objects only after recording their bound PV names.
4. Remove `claimRef` from the retained PVs.
5. Create new PVCs with the intended names and explicit `volumeName` bindings.
6. Sync the renamed workloads.
7. Verify Prometheus history and Grafana runtime state before deleting any old
   storage artifacts.
