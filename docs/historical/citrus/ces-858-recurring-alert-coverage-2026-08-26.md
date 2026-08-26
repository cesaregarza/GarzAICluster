# CES-858 recurring-alert coverage receipt

Date: 2026-08-26

This receipt records the source-only correction for Citrus recurring-runtime
alert selectors. It does not authorize or record a monitoring sync, recurring
runtime activation, enrollment, reminder, charge, provider request, or other
live mutation.

## Authoritative environment mapping

The implementation baseline is GarzAICluster `main` at
`a7fc0226026e54558d66e7f08d815ac32fbe273d`.

| Environment | Argo application | Helm release | Kubernetes namespace | Billing worker pod prefix |
| --- | --- | --- | --- | --- |
| Production | `citrus` | `citrus` | `default` | `citrus-billing-worker-` |
| Development | `citrus-dev` | `citrus-dev` | `citrus-dev` | `citrus-dev-billing-worker-` |

The mappings come from `argocd/applications/citrus.yaml` and
`argocd/applications/citrus-dev.yaml`. Prometheus's `kubernetes-pods` scrape
configuration projects the discovered namespace and pod name into the
`namespace` and `pod` target labels.

## Alert inventory and stable behavior

All recurring alerts retain `severity: critical`, `service: citrus`, their
existing names, and their existing pending durations.

| Alert | Signal owner | Pending |
| --- | --- | --- |
| `CitrusRecurringRuntimeHealthFailed` | exact recurring-health CronJob owner | 2m |
| `CitrusRecurringRuntimeTickFailed` | exact recurring-tick CronJob owner | 2m |
| `CitrusRecurringRuntimeExporterUnhealthy` | Citrus billing-worker metrics pod | 2m |
| `CitrusRecurringRuntimeMetricsMissing` | Citrus billing-worker Deployment/scrape | 2m |
| `CitrusRecurringRuntimeWorkOverdue` | Citrus billing-worker metrics pod | 5m |
| `CitrusRecurringRuntimeOutboxStale` | Citrus billing-worker metrics pod | 5m |
| `CitrusRecurringBillingQueueUnavailable` | Citrus billing-worker metrics pod | 5m |
| `CitrusRecurringNoticeFailures` | Citrus billing-worker metrics pod | 2m |
| `CitrusRecurringConfirmationNeedsReview` | Citrus billing-worker metrics pod | 2m |
| `CitrusRecurringPaymentReconciliationLag` | Citrus billing-worker metrics pod | 5m |
| `CitrusRecurringChargeOrOrderStalled` | Citrus billing-worker metrics pod | 5m |

Kube-state-metrics selectors use two complete environment branches: production
requires `namespace="default"` plus the exact `citrus-*` owner or Deployment,
while development requires `namespace="citrus-dev"` plus the exact
`citrus-dev-*` identity. Application metrics pair those same namespaces with
their respective billing-worker pod prefixes and `job="kubernetes-pods"`.
The missing-metrics rule keeps each Deployment and scrape-side `unless` in the
same fixed environment branch, so a cross-named pod cannot suppress the real
worker's alert. An unrelated or swapped-name pod cannot satisfy any recurring
alert merely by exposing the same metric name.

## Test matrix

`helm/garz-observability/tests/citrus-recurring-alerts.test.yaml` is executed by
the pinned Prometheus 2.52 `promtool test rules` path. The synthetic scenarios
provide every alert with:

- a production failure series in `default` that must fire;
- a development failure series in `citrus-dev` that must fire; and
- an unrelated `default` workload series with the same signal that must not
  appear in the expected alerts;
- both swapped namespace/name combinations at firing values that must not
  appear; and
- correct and swapped scrape-health controls proving only the correctly paired
  `up == 1` series clears a missing-metrics alert.

Both limbs of each multi-signal alert fire for the correct production and
development pod in the fixture. The rules aggregate by `job`, `namespace`, and
`pod` after thresholding so simultaneous signals yield one stable alert label
set instead of duplicate alerts distinguished only by metric name.

Rendered Python contracts independently inventory all eleven alert names,
durations, severities, source jobs, namespaces, owners, Deployments, and pod
selectors. Helm rendering and `promtool check config/rules` remain part of the
same CI contract.

## Rollout/preflight gate

Recurring runtime remains disabled by default in production and development.
CES-850 must remain disabled until its existing payment-isolation and capacity
blockers are complete and this source change has been reviewed. A later,
separately authorized monitoring rollout must record all of the following:

1. the merged GitOps commit selected by the `garz-observability` Argo
   application;
2. a successful manual sync at that exact revision and Synced/Healthy status;
3. the rendered production and development selector coverage from this test
   matrix; and
4. safe live alert/series verification for each environment before any
   separately governed recurring-runtime activation.

`garz-observability` has manual sync policy, so merging this source-only change
does not deploy it. Rollback before an authorized sync is simply to withhold
the sync; rollback afterward is a reviewed revert followed by another
separately authorized manual sync.
