# Safe Prometheus historical queries and right-sizing evidence

CES-856 defines the only supported path for multi-day right-sizing queries on
the cluster Prometheus. On 2026-08-26 at 01:40:44 UTC, concurrent broad joined
14-day range queries caused the single Prometheus pod to be OOM-killed with
exit code 137. It restarted once and recovered Ready. The existing 1,200 MiB
`GOMEMLIMIT`, 5,000,000-sample limit, concurrency of 5, and 60-second timeout
did not prevent that failure. Do not run raw historical joins against the live
server to reproduce it.

This change does not increase resources, alter retention or storage, deploy the
chart, or backfill data. Recording rules are forward-only. Wait for 14 days of
recorded history before treating a receipt as a complete 14-day window; they do
not backfill the incident-era raw history. If older evidence is essential, stop
and obtain a separately reviewed offline-export plan. A raw-query fallback is
not part of this runbook.

## Safety contract

All of these controls apply together:

- Prometheus rejects an individual query above 500,000 samples, admits at most
  two engine queries concurrently, and times each query out after 30 seconds.
  The sample cap is per query, not a byte or aggregate-memory guarantee.
- The supported recording group evaluates every 15 seconds. Values used by the
  helper are five-minute rates or trailing five-minute extrema, so querying at
  five-minute steps does not leave gaps between covered windows.
- Each recording rule may emit at most 20 series. That limit caps output
  cardinality after evaluation; it does not bound raw expression inputs. Fixed
  selectors and the 500,000-sample engine limit are the input guards.
- `scripts/query_prometheus_history.py` accepts only the recording metrics in
  this document, exact labels, one query at a time, a fixed step of 5m, and a
  maximum range of 14 days. It requests query statistics and rejects anything
  other than exactly one returned series or a reviewed peak above 10,000
  samples. It also requires every expected timestamp; an incomplete warm-up,
  stale series, or sustained recording gap whose newest sample is older than
  30 seconds fails closed instead of using Prometheus's default five-minute
  stale-value reuse. One missed 15-second evaluation can still reuse a fresh
  preceding sample; `PrometheusRuleGroupIterationsMissed` detects that case.
- The helper performs p95, p99, minimum, and maximum calculations offline. It
  never submits a range function, join, subquery, wildcard, or aggregation to
  Prometheus. Each receipt retains at most 4,033 validated points for reviewed
  offline comparisons.

A 14-day query at a 5-minute step has 4,033 points. The isolated Prometheus
2.52 proof run on 2026-08-26 used an 80-pod synthetic fixture with five-minute
raw input cadence. It measured a 972,034-sample raw joined peak versus 4,033 for
the recorded path (645,280 versus 4,033 total queryable samples). It then
launched two guarded broad queries concurrently. Both were rejected while the
2 GiB container with `GOMEMLIMIT=1200MiB` remained Ready, running, not
OOM-killed, and at zero restarts. The regression test requires the recorded
peak to remain below 10,000 samples and the raw peak to remain above the
500,000-sample guard. A synthetic 60-second recording gap must also fail the
30-second lookback/completeness contract instead of reusing a stale value.

## Supported evidence

Every query must select exactly one row from this table.

| Recording metric | Required exact labels | Meaning |
| --- | --- | --- |
| `cluster:container_cpu_usage_seconds_total:sum_rate5m` | none | Cluster container CPU cores used, summed from per-series five-minute rates. |
| `cluster:kube_pod_container_resource_requests_cpu_cores:max_sum_active_bound5m` | none | Highest cluster sum in the trailing five minutes of app-container CPU requests on bound Pending, Running, or Unknown pods. |
| `node:kube_pod_container_resource_requests_cpu_cores:max_sum_active_bound5m` | `node` | Highest equivalent request sum for one node in the trailing five minutes. |
| `cluster:kube_node_status_allocatable_cpu_cores:sum` | none | Current cluster allocatable CPU cores. |
| `node:kube_node_status_allocatable_cpu_cores:max` | `node` | Current allocatable CPU cores for one exact node. |
| `namespace_container:citrus_container_cpu_usage_seconds_total:max_rate5m` | `namespace`, `container` | Highest per-pod five-minute CPU rate for a Citrus container. |
| `namespace_container:citrus_container_memory_working_set_bytes:max_over_time5m` | `namespace`, `container` | Highest per-pod working set observed in the trailing five minutes. |
| `namespace_container:citrus_kube_pod_container_resource_requests_cpu_cores:max_over_time5m` | `namespace`, `container` | Highest configured app-container CPU request observed in the trailing five minutes. |
| `namespace_container:citrus_kube_pod_container_resource_requests_memory_bytes:max_over_time5m` | `namespace`, `container` | Highest configured app-container memory request observed in the trailing five minutes. |
| `namespace:citrus_kube_pod_scheduling_latency_seconds:p95_over_pods5m` | `namespace` | Snapshot p95 across Citrus pods observed during the trailing five minutes. |
| `namespace:citrus_kube_pod_scheduling_latency_seconds:max_over_pods5m` | `namespace` | Snapshot maximum across Citrus pods observed during the trailing five minutes. |
| `namespace:citrus_kube_pod_pending_age_seconds:max_over_time5m` | `namespace` | Highest Pending age observed during the trailing five minutes; zero means none was Pending. |

The only allowed Citrus namespaces are `default` and `citrus-dev`. CPU and
memory usage/request records are separate per-pod envelopes. They are useful as
absolute evidence, but must not be divided: the maximum usage and maximum
request can belong to different pods or rollout revisions, so their quotient is
not a worst-pod utilization ratio. Run cluster usage, request, and allocatable
queries sequentially and combine their receipts with
`scripts/calculate_prometheus_history_ratio.py` when calculating aggregate
utilization or reservation ratios. Never divide independent receipt summaries;
p95(A) divided by p95(B) is not p95(A/B).

The request records use kube-state-metrics app-container requests. The cluster
and node records include only pods with a node and an active nonterminal phase;
they do not include scheduler-effective init-container maximums or pod
overhead. The scheduling p95 is a five-minute snapshot across observable pod
objects, not an event-weighted 14-day scheduling-latency percentile. Offline
receipt percentiles describe the distribution of those snapshot rollups over
time. Keep these limitations in any recommendation.

## Query procedure

1. Confirm the recording rules have accumulated enough forward history for the
   requested window. Do not backfill or substitute a raw historical query.
2. Start a local port-forward in a dedicated terminal only after the relevant
   deployment and read access have separate authorization:

   ```bash
   kubectl -n monitoring port-forward service/prometheus 9090:9090
   ```

3. Validate the exact request without network access:

   ```bash
   python scripts/query_prometheus_history.py \
     --metric namespace_container:citrus_container_memory_working_set_bytes:max_over_time5m \
     --label namespace=default \
     --label container=web \
     --start 2026-08-27T00:00:00Z \
     --end 2026-09-10T00:00:00Z \
     --step 5m \
     --dry-run
   ```

4. Run that one query and save its receipt:

   ```bash
   python scripts/query_prometheus_history.py \
     --metric namespace_container:citrus_container_memory_working_set_bytes:max_over_time5m \
     --label namespace=default \
     --label container=web \
     --start 2026-08-27T00:00:00Z \
     --end 2026-09-10T00:00:00Z \
     --step 5m \
     --output /tmp/citrus-web-memory-history.json
   ```

5. Let the command finish before starting another. Record the exact time range,
   labels, sample count, peak sample statistics, and offline summary from the
   receipt. Do not launch helpers in parallel.

6. After both source receipts are complete, calculate only a reviewed ratio
   from their timestamp-aligned points. This command has no network access:

   ```bash
   python scripts/calculate_prometheus_history_ratio.py \
     --numerator /tmp/cluster-cpu-usage.json \
     --denominator /tmp/cluster-cpu-allocatable.json \
     --output /tmp/cluster-cpu-usage-to-allocatable.json
   ```

   Supported pairs are cluster CPU usage/allocatable, cluster
   request/allocatable, and node request/allocatable. The calculator rejects
   usage/request envelope division, Citrus envelope division, differing labels,
   ranges, steps, timestamps, gaps, non-finite values, zero denominators, and
   all other metric pairs.

Stop immediately on a timeout, sample rejection, unexpected cardinality,
missing statistics, a peak above 10,000, a memory-pressure/restart alert, or a
non-Ready Prometheus. Do not retry automatically and do not broaden a selector.

## Prohibited query shapes

Do not submit any of the following to live Prometheus:

- raw multi-day joins or raw multi-day aggregations;
- a `query_range` whose expression contains a range function or subquery;
- `*_over_time((raw expression)[14d:...])` or any equivalent 14-day subquery;
- wildcard or regex selectors in an operator query;
- Grafana panels that fan out historical requests;
- concurrent helpers, shell background jobs, or automatic retries;
- a live replay of the CES-856 incident query.

The helper's allowlist and exact-label contract are intentional. Do not bypass
them with `curl`, Grafana Explore, or a hand-written PromQL approximation.

## Alerts and diagnosis

The rules add these testable signals:

- `PrometheusContainerMemoryPressureHigh`: cgroup working set remains above
  1,610,612,736 bytes (75% of the existing 2 GiB limit) for 10 minutes. This is
  a sustained-pressure warning, not protection from a millisecond-scale spike;
  the query guards provide prevention.
- `PrometheusContainerOOMKilled`: a restart occurred in 15 minutes and the last
  termination reason is OOMKilled.
- `PrometheusContainerRestarted`: a non-OOM restart occurred in 15 minutes.
- `PrometheusRuleEvaluationFailures`: a rule evaluation failed in 10 minutes.
- `PrometheusRuleGroupIterationsMissed`: a rule group missed an evaluation in
  10 minutes, which can create a recorded-history gap without a failed query.

Preserve availability first: stop interactive queries, confirm Alertmanager is
reachable, inspect only bounded status/flag/rule health endpoints, and retain
the exact alert and restart evidence. Do not delete TSDB data, reduce retention,
resize storage, restart the pod, or change resources as an ad hoc diagnostic.

## Offline verification

These checks use a disposable local Prometheus 2.52 container and synthetic
data. They do not contact Kubernetes or live Prometheus:

```bash
python scripts/validate_prometheus_config.py \
  --chart-dir helm/garz-observability \
  --release-name garz-observability \
  --namespace monitoring \
  --values helm/garz-observability/values-prod.yaml \
  --label prod

python scripts/verify_prometheus_query_safety.py
```

The first command renders the chart, validates config and every rule with the
pinned `prom/prometheus:v2.52.0` promtool, and runs the synthetic rule tests.
The second creates a disposable 14-day TSDB, calls the helper's request and
receipt core against that exact version, and removes its containers and
temporary data. The Python unit suite separately verifies the CLI allowlist,
exact labels, local-only URL, completeness gate, and offline ratio contracts.

## Rollout, observation, and rollback

The Argo application uses manual sync. Merging this change does not deploy it.
Any Argo sync, Kubernetes apply, pod restart, or capacity purchase requires
separate authorization. An authorized sync will change Prometheus query flags
and the rules checksum, restarting the single StatefulSet pod while preserving
its PVC.

Before an authorized sync, require a green pull request, a reachable
Alertmanager, a rollback commit, and an agreed observation window. After sync,
observe readiness, restart/OOM state, working-set pressure, rule failures,
missed iterations, and recording-series presence for 24–72 hours. Use only a
short-window helper query during that observation. Wait the full 14-day warm-up
before making a 14-day right-sizing recommendation. Resource changes, if any,
belong in a separate measured and authorized change.

If the rules impair observability, stop history queries and revert the reviewed
Git change through a new pull request plus a separately authorized sync. Do not
casually restore the former 5,000,000-sample, concurrency-5, 60-second query
budget; that reopens the incident condition. No rollback should delete the PVC,
truncate retention, or mutate TSDB data.

Prometheus references: [recording rules](https://prometheus.io/docs/prometheus/2.52/configuration/recording_rules/),
[2.52 command-line query controls](https://prometheus.io/docs/prometheus/2.52/command-line/prometheus/),
and the [2.52 HTTP range-query API](https://github.com/prometheus/prometheus/blob/v2.52.0/docs/querying/api.md#range-queries).
