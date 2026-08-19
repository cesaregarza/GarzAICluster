# CES-573 projected workload identity canary

This change canaries Kubernetes-native workload identity for
`data.workspace_probe`. It removes the per-release HMAC/SOPS mint from that
worker's normal re-pin path while preserving the existing HMAC credential,
ciphertext, metadata, and rollout checksum as an unchanged rollback input.
The normal API and postgres-sweep HMAC allowlists no longer accept
`data.workspace_probe`; a retained `mwit_v1` credential is rollback-only.
OpenCode proposer and apply remain distinct, accepted HMAC identities.

## Security boundary

- Kubernetes TokenReview authenticates the exact projected ServiceAccount
  subject and the `mandate-api` audience.
- The reviewed registry maps that subject to one immutable
  code/manifest/image release tuple; worker-requested names or digests do not
  authorize a release.
- The reviewed GitOps merge, automated window-governed registry-overlay sync,
  and subsequent manual workload sync remain the operator authorization events.
- This phase does not recompute runtime workflow digests or attest
  source-to-image integrity. That remains CES-576.

The immutable inputs for this canary are:

- Core: `5efddce6841756aa8f5b4f770cc9a64c17bcd68e`
- agent-workloads: `51102759737c5e9a1c0d95adf1ae299a12e3cae1`
- `data.workspace_probe` code:
  `sha256:a1eff6143142995fc1ca59644533e258650674e0ba0e0058ca747ee980fadf02`
- manifest:
  `sha256:912222e0cf0adea2733166543213e6ff70aadff34f13815ca36038bdf30d123f`
- image:
  `sha256:9132f8e5d761f42947186041a9b28a6298533d22b0d286f55551dabc978531ed`
- ServiceAccount subject:
  `system:serviceaccount:agent-workloads:agent-workloads-data-workspace-probe-a3404f79b15b11b8b7f3`

## Operator sequence

The original canary used manual sync for every affected app. With CES-668, the
registry overlay now reconciles automatically inside the existing sync window;
the control-plane and workload applications remain manual. After review and
merge:

1. Sync `agent-control-plane` first. Confirm the dedicated
   `agent-control-plane-tokenreview` ServiceAccount has only
   `authentication.k8s.io/tokenreviews/create`, the API becomes healthy, and
   non-API control-plane pods do not receive the reviewer token.
2. Wait for `agent-control-plane-registry-overlay` to auto-sync. Confirm its
   rollout-strategy Sync hook and restart PostSync hook complete and Core has
   loaded the exact ServiceAccount subject and release tuple above. A successful
   generated hook self-deletes, so use the Application operation plus
   `SuccessfulCreate`/`Completed` events rather than expecting a surviving Job.
3. Sync `agent-workloads`. Confirm the workspace-probe Deployment uses the
   release-scoped ServiceAccount, has no legacy token Secret volume, and
   projects only the `mandate-api` token.
4. Start a fresh `agent-control-plane-synthetic-live-verify` Job and require the
   `readonly-query-skill-digests` journey to complete with a
   `model_call.finished` audit event. Confirm the workload-identity metric
   records `mode=kubernetes,outcome=accepted` and that no HMAC mint command or
   SOPS edit occurred.
5. Replace the projected token file atomically while the worker remains
   running, then prove a later request succeeds with the rotated token.
6. Exercise wrong subject, wrong worker, wrong audience, expired token,
   ambiguous subject, and TokenReview unavailable/error cases; each must fail
   closed without an HMAC retry.

Do not migrate another worker or remove the rollback credential until this
canary is accepted.

## Rollback

If the Core substrate is unhealthy, revert its values to `hmac`, disable
`workloadIdentity.kubernetesTokenReview`, and remove the dedicated RBAC in one
reviewed GitOps change.

Re-enabling workspace-probe HMAC is an explicit reviewed rollback, never a
normal re-pin side effect. Before activating a worker rollback, that reviewed
GitOps change must re-add `data.workspace_probe` to
`AGENT_PLATFORM_WORKLOAD_IDENTITY_ALLOWED_SUBJECTS_JSON` in both
`apps/agent-control-plane/values.yaml` and
`apps/agent-control-plane-runtime-controls/postgres-sweep-cronjob.yaml`.

If only the worker canary is unhealthy, leave Core in hybrid mode. In the same
reviewed rollback, restore the two HMAC allowlists, remove the workspace-probe
`service_account_subject` registry binding, disable
`projectedWorkloadIdentity`, and restore the existing read-only Secret volume
and `MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE`. The retained HMAC ciphertext,
metadata, checksum, and release tuple must remain unchanged. Never mount static
and projected credentials into the same worker, and never activate the retained
credential before the reviewed allowlist restoration is effective.

No merge, sync, production cutover, HMAC rotation, legacy-secret deletion, or
CES-576 runtime-attestation implementation is authorized by this document.
