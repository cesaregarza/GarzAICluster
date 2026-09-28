# API and data-worker replica availability

Production enables `fastapi.spreadAcrossNodes` and
`workers.data.workspace_probe.spreadAcrossNodes`. Each Deployment keeps its two
replicas, existing resources, image, Service selectors, and worker identity pins.

Hostname topology spreading uses `maxSkew: 1`, `DoNotSchedule`, and
`matchLabelKeys: [pod-template-hash]`. The revision key prevents old ReplicaSet
pods from masking an imbalance in the new revision. `nodeTaintsPolicy: Honor`
excludes nodes with untolerated taints from the eligible domains; omitting
`minDomains` allows replacement replicas on the remaining eligible node after a
failure, subject to resource capacity. Node recovery alone does not rebalance
existing pods. Verify placement after recovery and use a reviewed rollout if
replicas are still colocated.

These fields require Kubernetes 1.27+ with its default topology-spread feature
gates; the reviewed cluster runs 1.33.12. See the upstream
[topology spread documentation](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/).

When enabled for more than one configured replica, each workload gets a
PodDisruptionBudget with `maxUnavailable: 1` and
`unhealthyPodEvictionPolicy: AlwaysAllow`. These protect voluntary evictions, not
node crashes or Deployment updates. Single-replica configurations omit the
budget. See [disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/).

FastAPI separately uses a rolling update with one surge and zero unavailable
replicas, plus ten seconds of stable readiness. Its HTTP readiness check uses
`/api` on the named `http` port. This lightweight route index was verified to
return HTTP 200 in the deployed image; it checks application serving, not database
or external-provider availability. No restart-triggering liveness probe is added.

## Rollout

1. Refresh live node readiness, taints, pod requests, actual memory, and pending
   pods. Keep enough space on the destination for one surge: 150m CPU/256Mi for
   FastAPI and 25m CPU/96Mi for the data worker. The September 28 snapshot had
   approximately 308m and 173m CPU unreserved on the two nodes; these are evidence
   from that inspection, not a substitute for a new capacity check.
2. Review the GitOps revision and rendered diff. There must be no image, replica
   count, credential, identity pin, or resource-request changes.
3. Sync `splattop-prod` first. Wait for the rollout to complete with two Ready API
   replicas, one on each healthy eligible node, matching image digests and ready
   Service endpoints. Verify `/api` returns 200 through the normal service path.
   If the replacement cannot schedule or become Ready, retain the old replicas
   and investigate; do not relax the availability settings to force completion.
4. Sync `agent-workloads` only after the API gate passes. Verify the two
   `data.workspace_probe` pods are Ready on distinct healthy eligible nodes and
   retain their expected projected ServiceAccounts and image digest. Other
   workers and the broker should not roll out from this change.
5. Check the two PDBs select only their intended workloads and allow one healthy
   replica's voluntary eviction. Do not drain a node merely to test the budget.

This improves placement and planned-maintenance behavior; it does not establish
capacity to run the entire cluster on one node. A failed node, concurrent rollout,
or resource pressure can still leave replacements Pending.

## Rollback

Revert the PR and sync the same two applications in order. This restores the
previous placement/readiness settings and prunes the two PDBs when pruning is
included in the reviewed sync. No volume, schema, image, or identity rollback is
required. Disabling only `spreadAcrossNodes` also removes the corresponding PDB;
FastAPI readiness and rolling-update settings remain independently configurable.
