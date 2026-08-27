# CES-849 Agent-8s CPU right-sizing evidence — 2026-08-26

## Decision

Stage one production-only CPU-request reduction for the Agent-8s Discord bot:

| Workload/container | Replicas | Current | Proposed | 14-day p99 | 14-day max | Desired reduction |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `agent-8s/bot` | 1 | 100m | 50m | 6.016m | 6.580m | 50m |

The proposed request is 8.3 times observed p99 and 7.6 times the observed
maximum. The 300m CPU limit, 128Mi memory request, 256Mi memory limit, one
replica, Recreate rollout strategy, disabled HPA, and disabled PDB remain
unchanged. The disabled development overlay remains at zero replicas and a
100m CPU request.

At the measured two-node total of 3,800m allocatable CPU, the declarative 50m
reduction adds 1.32% of cluster allocatable CPU as scheduler headroom. It does
not change provider cost or actual application CPU consumption.

## Evidence

- Window: `2026-08-12T01:40:00Z` through `2026-08-26T01:40:00Z`, inclusive,
  sampled on a five-minute grid.
- Source: the same immutable offline Prometheus TSDB copy and bounded,
  sequential, join-free analysis documented in
  `ces-849-all-app-rightsizing-2026-08-26.md`.
- Grain: one `splattop-bot-agent-8s` / `Deployment` / `agent-8s` / `bot` row,
  represented as a max-across-active-pods envelope at each timestamp.
- Coverage: 4,033 CPU observations and 4,033 memory observations across 14
  days; 100% time-span and CPU-to-memory active-slot coverage; current at the
  window end.
- CPU p50/p95/p99/max: 4.142m / 5.774m / 6.016m / 6.580m.
- CPU throttle p95/p99/max: 0% / 0.157% / 0.555%.
- Container restart increase: zero.
- Memory p99/max: 60.7Mi / 61.5Mi, against the unchanged 128Mi request and
  256Mi limit.
- Candidate rule: at most a 50% reduction, no restart increase, p99 throttling
  below 1%, and a new request above twice observed p99 with a 50m floor.

No live Prometheus query was used for this slice. Absence of an OOM-reason
series in the archive remains a data caveat, not proof that no OOM occurred.

## Deployment boundary

This slice is deliberately separate from the four manual-sync applications in
the first CES-849 PR. `apps/bots/agent-8s.yaml` selects the production values,
and the generated `splattop-bot-agent-8s` Application has automated prune and
self-heal. Therefore, merging the values change is a production deployment and
requires explicit authorization; opening and reviewing the PR is inert.

The Deployment uses `Recreate`, so an authorized rollout can briefly take the
single bot replica offline. Before merge, record the current Argo revision,
rendered resources, Ready state, restarts, bot health, and node requested CPU.
After the automated sync, require the new pod to become Ready without
`FailedScheduling`, restart growth, elevated throttling, or functional bot
regression. Observe a representative bot activity cycle before advancing to
another automated application.

Rollback is the inverse values change, `50m` back to `100m`, followed by the
same automated reconciliation and health checks. Do not combine this rollout
with a Citrus rollout, another right-sizing merge, or a node-pool change.
