# CES-849 Citrus CPU headroom baseline

This is the before-change receipt for CES-849. It records the read-only
measurements gathered on 2026-08-26 and the remaining operator decision. It is
not evidence that capacity has been changed or that rollout acceptance passed.

## Live tuple

- Argo Application: `citrus-dev`
- Argo state: `Synced` and `Healthy`
- GitOps revision: `a7fc0226026e54558d66e7f08d815ac32fbe273d`
- Dev image: `registry.digitalocean.com/sendouq/citrus:0e2258bf95c6170895c26780258eb42d5b5c557c`
- Nodes: two `g-2vcpu-8gb` workers, each with 1,900m allocatable CPU
- Prometheus history: 14 days at five-minute resolution within a 15-day
  retention window

The original PR #575 tuple (`bc91feff...` / `a728346e...`) is historical and
is not the current live baseline.

## Scheduler reservations versus use

| Node | Requested CPU | Scheduler-free CPU | Current CPU use |
| --- | ---: | ---: | ---: |
| `pool-splattop-38ntsj` | 1,752m (92%) | 148m | 401m (21%) |
| `pool-splattop-38ntso` | 1,887m (99%) | 13m | 288m (15%) |
| Cluster | 3,639m of 3,800m (95.8%) | 161m | 689m |

Fourteen-day running-pod requests were 3,689m at p95 and p99, with a 3,714m
maximum. That left 111m at p95/p99 and 86m at the observed maximum. Cluster
container CPU was 1,017m at p95, 1,106m at p99, and 1,249m at maximum.

The failure mode is scheduler reservation and bin-packing pressure, not actual
CPU saturation. Current node descriptions are authoritative for exact
placement. Historical request queries must filter to Running pods because
retained completed Job pods otherwise inflate kube-state-metrics sums.

## Citrus fourteen-day CPU use

All values are mCPU. Web, worker, and Redis have 4,032 five-minute samples.
Short media-requeue Jobs have conditional samples only.

| Component | Environment | p95 | p99 | Maximum |
| --- | --- | ---: | ---: | ---: |
| Web | dev | 0.93 | 5.46 | 63.08 |
| Web | production | 1.02 | 2.64 | 50.93 |
| Media worker | dev | 1.26 | 1.30 | 21.45 |
| Media worker | production | 1.25 | 1.27 | 20.70 |
| Redis | dev | 5.64 | 5.87 | 6.05 |
| Redis | production | 5.66 | 5.87 | 6.03 |
| Media requeue | dev | 24.28 | 24.95 | 25.57 |
| Media requeue | production | 24.58 | 24.97 | 25.90 |

Media-GC and migration hooks did not yield enough rate samples for a reliable
quantile. Planned SMS, direct-payment, billing, and recurring workloads have no
representative live usage history. Production traffic was light during this
window, so these measurements do not justify an unobserved production request
reduction.

## Scheduling evidence

Web and media-worker Deployments use `maxSurge: 0` and
`maxUnavailable: 1`. Redis retains the one-replica Deployment default surge,
which can reserve another 50m. The migration Job has no CPU request, so the
scheduler currently treats its CPU reservation as zero and rollout accounting
is incomplete.

Production and dev media-requeue Jobs both run every ten minutes and each
request 50m. Media-GC runs in both environments at the same daily time and also
requests 50m. `concurrencyPolicy: Forbid` prevents overlap within one CronJob;
it does not prevent different CronJobs or production and dev from overlapping.

At 2026-08-26T01:10:00Z, a dev media-requeue pod was created and received two
`FailedScheduling` events stating that both nodes had insufficient CPU. It
scheduled at 01:10:15Z and ran from 01:10:16Z through 01:10:34Z. A production
requeue and agent-control-plane synthetic Jobs had the same retained event
reason. Across the lighter 14-day scheduling history, all-pod Pending duration
was 19 seconds at p95 and 25 seconds at p99; Citrus p95 was 25 seconds.

## Headroom contract

Before enabling the planned workloads, maintain:

- at least 800m unrequested CPU cluster-wide;
- at least 300m unrequested CPU on each node; and
- an alert when a node remains above 85% requested CPU for ten minutes.

The 800m envelope covers a conservative 100m migration request, a possible 50m
Redis surge, synchronized current Jobs, the preserved production and dev
direct-payment sweeps, two billing workers and metrics sidecars, overlapping
recurring tick/health Jobs, one disabled-by-default dev SMS sweep, and a small
residual buffer.

Current cluster-wide headroom is 161m, leaving a 639m shortfall. Measurement
supports a dev-only experiment reducing web, worker, and Redis requests, but
that would recover only about 125m and cannot close the gap. Do not lower
production requests from this low-traffic sample.

The capacity-first proposal is a third identical node, adding 1,900m
allocatable CPU without reducing existing reservations. Before preparing or
applying that change, record the authoritative node-pool/IaC owner, current
provider price, approved spend, and rollback window. Scale-down is not an
instant rollback because it can evict workloads.

## Alerting slice

The repository change accompanying this receipt adds:

- `KubernetesNodeCPURequestHeadroomLow`: per-node Running-pod CPU requests over
  85% of allocatable CPU for ten minutes;
- `CitrusPodPending`: a production or dev Citrus pod Pending for one minute.

Production Citrus runs in `default`; dev runs in `citrus-dev`. The Pending rule
therefore scopes both namespace and pod name instead of copying the existing
`namespace=~"citrus(-dev)?"` recurring-rule selector, which misses production.
Correcting those existing recurring rules is separate follow-up work.

The monitoring Application is manual-sync. Merging alert definitions does not
authorize or perform a live monitoring sync.

## Diagnostic safety incident

A broad joined 14-day Prometheus range query exceeded the single Prometheus
replica's memory budget. The container was OOM-killed once at
2026-08-26T01:40:44Z and recovered `Ready` with restart count 1. No further
Prometheus queries were run. Do not repeat broad historical joins interactively;
use bounded windows, recording rules, or an offline snapshot. The current
five-million-sample limit and 60-second timeout did not prevent this memory
failure mode.

## Remaining acceptance gates

CES-849 remains incomplete until all of the following are recorded:

1. An approved capacity or measurement-backed request/schedule change is merged
   and applied through the authoritative GitOps/IaC path.
2. The migration Job has an explicit measured CPU request.
3. Alert rules are reviewed, synced separately, and their delivery is proven.
4. One rollout and at least two scheduled-job cycles complete without
   `FailedScheduling`.
5. The before/after node request, Pending duration, cost, and rollback receipt
   is attached to CES-849.
