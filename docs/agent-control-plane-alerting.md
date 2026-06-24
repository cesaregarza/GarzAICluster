# Agent Control Plane Alerting

The cluster-level `helm/garz-observability` chart owns Mandate alerting.
Prometheus discovers the control-plane API pod through the shared
`kubernetes-pods` annotation scrape job; CES-113 adds the API pod annotations
and makes `/metrics` scrapeable. This chart adds Prometheus egress to the
`agent-control-plane` namespace, records stable target labels, renders the
Mandate alert rules, and routes production alerts to Discord through
Alertmanager.

## Required Secrets

Create the Discord webhook Secret for Alertmanager:

```bash
kubectl -n monitoring create secret generic alertmanager-discord-webhook \
  --from-literal=webhook-url="$DISCORD_WEBHOOK_URL"
```

## Scrape Path

`helm/garz-observability/values-prod.yaml` enables
`monitoring.prometheus.agentControlPlane`. The rendered Prometheus config:

- discovers annotated pods through the existing `kubernetes-pods` job;
- scrapes the control-plane API pod at `/metrics` after CES-113's
  `prometheus.io/scrape`, `prometheus.io/port`, and `prometheus.io/path`
  annotations land;
- relabels `namespace`, `pod`, `app_kubernetes_io_name`,
  `app_kubernetes_io_instance`, and `app_kubernetes_io_component` so Mandate
  alerts can target the API pod without a static Prometheus job;
- sends alerts to Alertmanager, which reads the Discord webhook from
  `/etc/alertmanager/secrets/discord-webhook/webhook-url`.

The `agent-control-plane` app values also allow ingress from Prometheus through
the namespace NetworkPolicy and set the same 10 MiB ingress request body limit
used by sibling apps.

## Alert Thresholds

Thresholds are documented next to the rendered rules in
`helm/garz-observability/templates/monitoring-prometheus-rules-configmap.yaml`.

Named SLOs:

- `metrics-observability`: the control-plane API must continuously expose one
  healthy scrape target for live registry-digest and runtime verification.
- `time-to-claim`: ready governed jobs should be claimed within 5 minutes at
  this scale.
- `callback-delivery-latency`: callback outbox events should drain before the
  backlog exceeds 100 pending rows.
- `journey-success-rate`: terminal job state and migration health must not
  degrade host-visible completion.

| Alert | SLO | Source | Threshold |
| --- | --- | --- | --- |
| `MandateAgentControlPlaneMetricsDown` | `metrics-observability` | Prometheus scrape health | annotated CP API target absent/down for 5m |
| `MandateCallbackAdapterDown` | `callback-delivery-latency` | kube-state-metrics deployment availability | fewer than 1 available callback-adapter replica for 5m |
| `MandateCallbackOutboxBacklog` | `callback-delivery-latency` | Mandate metrics | outbox rows > 100 for 10m |
| `MandateOldestReadyJobAgeHigh` | `time-to-claim` | Mandate metrics | oldest ready job > 300 seconds for 10m |
| `MandateDeadLetteredCallbackDeliveries` | `journey-success-rate` | Mandate metrics | dead-lettered delivery count increases in 15m |
| `MandateMigrationJobFailed` | `journey-success-rate` | kube-state-metrics job status | failed migration job reported for 5m |

The callback-adapter and migration-job alerts require kube-state-metrics. The
callback daemon heartbeat remains enforced by the Mandate chart liveness probe;
Prometheus alerts on the resulting Kubernetes deployment availability.

## Test Window

During a maintenance window, verify Discord delivery with a reversible scale test:

```bash
kubectl -n agent-control-plane scale deployment agent-control-plane-callback-adapter --replicas=0
```

Wait for `MandateCallbackAdapterDown` to fire and confirm the Discord alert, then
restore the daemon:

```bash
kubectl -n agent-control-plane scale deployment agent-control-plane-callback-adapter --replicas=1
```

The alert should resolve after kube-state-metrics and Alertmanager observe the
restored replica.
