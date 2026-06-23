# Model Gateway Kill Switch And Revocations

This runbook covers the deployment-owned controls mounted into the Mandate
model-gateway and API pods from the
`agent-control-plane-model-gateway-controls` ConfigMap.

Mandate owns the enforcement code. GarzAICluster owns the live files that make
the controls operable in this cluster.

## Files

The agent-control-plane values set:

- `AGENT_PLATFORM_MODEL_GATEWAY_KILL_SWITCH_FILE=/app/model-gateway-controls/kill-switch`
- `AGENT_PLATFORM_MODEL_GATEWAY_REVOCATION_FILE=/app/model-gateway-controls/revocations.txt`

The ConfigMap is mounted as a directory, not by `subPath`, so kubelet projects
key changes into running pods without a pod restart.

Normal state:

- `kill-switch` key absent
- `revocations.txt` present and readable, with comments or one revoked id per line

## Halt All Model-Gateway Traffic

Use this when model calls must stop immediately but the control plane should
remain up for status, audit, cancellation, and operator actions.

Recommended emergency path:

```bash
kubectl -n agent-control-plane patch configmap agent-control-plane-model-gateway-controls \
  --type merge \
  -p '{"data":{"kill-switch":"active\n"}}'
```

Expected result: after the kubelet ConfigMap projection window, model-gateway
requests fail before token validation with `model_gateway_kill_switch_active`
and HTTP 503.

Deactivation:

```bash
kubectl -n agent-control-plane patch configmap agent-control-plane-model-gateway-controls \
  --type json \
  -p '[{"op":"remove","path":"/data/kill-switch"}]'
```

Argo decision: this control is intentionally operator-editable out of band.
The `agent-control-plane` Argo Application is manual-sync; Argo will show drift
after an emergency edit but will not auto-revert it. A later manual sync can
remove an active kill switch because Git keeps the safe default with no
`kill-switch` key. Before syncing while the switch is active, either confirm the
incident is over or commit the active state to Git intentionally.

## Revoke One Job Or Lease

Edit `revocations.txt` with one id per line. Blank lines and `#` comments are
allowed. Accepted formats:

```text
job_abc123
job:job_abc123
job_id:job_abc123
lease_abc123
lease:lease_abc123
lease_id:lease_abc123
```

Patch example:

```bash
kubectl -n agent-control-plane patch configmap agent-control-plane-model-gateway-controls \
  --type merge \
  -p '{"data":{"revocations.txt":"# emergency revocations\njob:job_abc123\n"}}'
```

Expected result: matching model-gateway requests fail before the provider
boundary with `model_gateway_lease_revoked` and HTTP 403.

To clear revocations, replace the file with only comments:

```bash
kubectl -n agent-control-plane patch configmap agent-control-plane-model-gateway-controls \
  --type merge \
  -p '{"data":{"revocations.txt":"# no active revocations\n"}}'
```

## Propagation Window

Kubernetes updates mounted ConfigMap directories asynchronously. Treat the
worst-case propagation target as 60 seconds unless the live test records a
smaller cluster-specific bound. Current model-gateway tokens are still bounded
by their lease TTL and session-authority budget; the kill switch and revocation
file are containment controls, not replacements for lease expiry.

During live verification, record:

- timestamp of ConfigMap edit
- first observed `model_gateway_kill_switch_active` or `model_gateway_lease_revoked`
- whether any pod restarted
- timestamp of restoration

## Fail-Closed Checks

If the configured kill-switch or revocation source becomes unreadable, the
model-gateway route must fail closed:

- kill-switch source unreadable: `model_gateway_kill_switch_unavailable`, HTTP 503
- revocation source unreadable: `model_gateway_revocation_source_unavailable`, HTTP 503

Do not make these files writable by application containers. Operators mutate the
ConfigMap through Kubernetes or Git; pods only read the projected files.
