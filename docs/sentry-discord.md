# Sentry issue notifications in Discord

The `sentry-discord-alerts` CronJob polls Sentry's issue API every three minutes
and sends new issues to the existing private Discord alerts webhook. It uses a
read-only personal token (`event:read`); no paid Sentry notification integration
or application instrumentation change is required.

The production organization is `cesar-eduardo-garza`. An empty projects list means
all projects accessible to that token. This detects **new issues**, not every
event, issue regression, metric alert, or resolution.

## Credentials and deployment

The operator-managed Kubernetes Secret `monitoring/sentry-discord-alerts` must
exist before Argo sync, with keys `sentry-token` and `discord-webhook-url`. No
credential is rendered by Helm or committed to Git. Rotate the Secret to update
credentials; the next Job mounts the new values. The Discord webhook is the
existing private `#alerts` destination, also used by Alertmanager.

The source script is packaged in a chart ConfigMap and runs on a digest-pinned
official Python image. A 1 GiB PVC stores the SQLite delivery ledger. The pod
runs without root or a Kubernetes API token. Its Cilium policy permits DNS plus
HTTPS to `sentry.io` and `discord.com`. There is no inbound service.

## Delivery behavior

- The first successful run establishes a baseline and sends no historical issues.
- Later runs page through new issues from the checkpoint with a one-hour overlap
  for indexing delay. Confirmed deliveries are committed individually to SQLite.
- At most 20 notifications are sent per run; any remaining backlog keeps the
  previous checkpoint. Pagination/network failures also preserve the checkpoint.
- A bounded HTTP 429 retry respects Retry-After. Failed jobs retry once and the
  following scheduled run can resume from durable state.
- Delivery is at-least-once: a process crash after Discord accepts a message but
  before SQLite commits it can cause one duplicate. Indexing delayed longer than
  the overlap, or an outage beyond Sentry's retained/searchable data, can lose alerts.
- Notifications include issue title, project, severity, first-seen time and a
  Sentry link. They disable Discord mentions and omit event payloads and stack traces.

The PVC is retained on Argo prune/delete to avoid an accidental reset. Do not
delete its state to clear an error: a new empty state starts a new baseline.

## Operations

Inspect `kubectl -n monitoring get cronjob,jobs` and the poller Job logs. Normal
logs contain only counts/status, never tokens, webhook URLs, or issue bodies.
`--check` reads Sentry without changing state or sending messages;
`--test-notification` sends one explicitly labeled Discord setup test without
changing the delivery ledger. Use a one-off Job based on the CronJob for either.

To stop polling, suspend `sentry-discord-alerts`; persist the intended schedule or
disable flag in GitOps. Preserve the PVC and Secret for a later resume. The
existing Alertmanager notifications continue independently.

Validation: `python3 -m unittest discover -s tests -p test_sentry_discord.py` and
`helm lint helm/garz-observability -f helm/garz-observability/values-prod.yaml`.
