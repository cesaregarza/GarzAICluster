# SplatTop Redis persistence

SplatTop production uses one Redis `7.2.4` replica and a retained `1Gi`
`ReadWriteOnce` PVC named `splattop-prod-redis-data`. The PVC uses
`do-block-storage-retain`, has Helm keep protection, and has Argo CD
`Prune=false,Delete=false` protection. The persistent Deployment uses
`Recreate`, mounts `/data`, and runs the Redis image as UID/GID 999 with a
non-root RuntimeDefault security context. Base chart values keep Redis
ephemeral so development and generic renders do not acquire storage.

The persistence values must be activated only after the source snapshot has
been seeded and its receipt reviewed. The helper does not change Helm values,
scale workloads, or perform cutover. Never enable the persistent Deployment
while the destination PVC is empty.

## Preflight

Record the exact live source Deployment UID and destination PVC UID before
running the helper. For the current production source, the expected source is
the `splattop-prod-redis` Deployment in `default`; the UID must be read from
the live object and supplied exactly. Do not use a name or a new UID from a
replacement Deployment as a substitute.

Render and review the production candidate first:

```bash
helm template splattop-prod helm/splattop --namespace default \
  -f helm/splattop/values-prod.yaml
```

Confirm that the render contains one `Deployment`, one retained PVC, one
replica, `strategy.type: Recreate`, the `/data` mount, and a matching claim
name. Confirm the Service name, selectors, Redis image, app image pins, and
credentials are unchanged. Create or reconcile the retained PVC before the
seed phase, while keeping the source Deployment ephemeral. The PVC must be
`Bound` and must retain its reviewed UID.

The read-only helper preflight checks the source Deployment UID, one replica,
absence of a source PVC, exactly one live source pod, its assigned node and
`kubernetes.io/hostname` node label, Redis `PING`, `appendonly=no`, and the
destination PVC's UID, `Bound` phase, retained StorageClass, and exact RWO
mode. It prints identities and status only:

```bash
uv run python scripts/splattop_redis_snapshot_move.py preflight \
  --context do-nyc3-k8s-nyc3-garz-ai \
  --namespace default \
  --kubectl /root/dev/.tools/kubectl-v1.33.12 \
  --deployment splattop-prod-redis \
  --pvc splattop-prod-redis-data \
  --expected-source-uid '<recorded-deployment-uid>' \
  --expected-source-pod-uid '<recorded-source-pod-uid>' \
  --expected-pvc-uid '<recorded-pvc-uid>'
```

Use the same arguments with `seed --dry-run --receipt /run/user/1000/splattop-redis-seed.json`
to review the bounded operation without creating a pod or writing the PVC.

## Seed and cutover

Before `seed`, stop or quiesce every SplatTop Redis writer: Celery worker and
beat, FastAPI request paths that enqueue work, and any scheduled warmup or
operator jobs. Confirm that no other Redis client is expected to write during
the operation. Use an owner-only directory for the receipt; the helper stores
no Redis values and never prints RDB bytes or Redis command output.

Run `seed` only after the preflight and dry run pass:

```bash
umask 077
mkdir -p /tmp/splattop-redis-migration-<run-id>
chmod 700 /tmp/splattop-redis-migration-<run-id>
uv run python scripts/splattop_redis_snapshot_move.py seed \
  --context do-nyc3-k8s-nyc3-garz-ai \
  --namespace default \
  --kubectl /root/dev/.tools/kubectl-v1.33.12 \
  --deployment splattop-prod-redis \
  --pvc splattop-prod-redis-data \
  --expected-source-uid '<recorded-deployment-uid>' \
  --expected-source-pod-uid '<recorded-source-pod-uid>' \
  --expected-pvc-uid '<recorded-pvc-uid>' \
  --receipt /tmp/splattop-redis-migration-<run-id>/receipt.json
```

The helper creates a non-root temporary pod on the source Redis node with the
exact destination PVC,
refuses to adopt an existing helper, refuses to overwrite any existing
`/data/dump.rdb`, pauses source writes, runs `SAVE`, bounds the RDB at 512 MiB,
copies it through stdin, verifies byte count and SHA-256 on both sides, and
runs `redis-check-rdb`. A successful run deletes the helper and writes a
`phase: awaiting-cutover` receipt with the pause expiry timestamp. The source
remains write-paused deliberately so it cannot race the new writer. The helper
fails closed if the configured pause will expire before the reviewed cutover
grace period; it then releases the source pause and emits no success receipt.

Review the receipt's Deployment UID, source pod UID, source node, PVC UID, byte count, and
checksum. Immediately before syncing, require at least 180 seconds before
`pause_expires_at`, unchanged source/PVC identities and snapshot checksum,
all writer Deployments at zero replicas, no remaining writer Pods (including
terminating Pods), and no unexpected Redis clients. An expired or uncertain
receipt invalidates the seed; stop and prepare a fresh verified seed before
cutover. Sync only the Redis Deployment and PVC while writers remain stopped.
Then perform the authorized Helm/Argo cutover with the production persistence
values. Wait for the old source pod to terminate and
the new pod to become Ready. Check Redis `PING`, the application Service
endpoints, worker/beat health, queue health, and the expected restored data
shape without printing values. Only after those checks pass may the source
pause be released and normal writers resumed.

## Failure and rollback

If any precondition or integrity check fails, the helper fails closed before
cutover. It deletes its temporary pod and attempts `CLIENT UNPAUSE`; if that
unpause cannot be confirmed, stop and repair the source operator-side before
resuming writers. A partial destination file remains protected by the helper's
no-overwrite check. Do not rerun `seed` against it and do not delete the PVC;
inspect the failure and provision a fresh reviewed destination claim if a new
attempt is required.

If cutover readiness or application checks fail, keep the retained PVC and
stop writers. Do not revert to an empty ephemeral Redis instance. Inspect the
new pod and RDB receipt, and repair the persistent candidate while preserving
the seeded PVC. Any rollback must retain one writer and the same Service
selectors. Resume source writes only after the active Redis endpoint and queue
consumers have been verified.
