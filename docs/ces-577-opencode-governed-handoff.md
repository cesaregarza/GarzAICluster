# CES-577 OpenCode governed handoff

The OpenCode proposer and apply executor currently remain in
`legacySharedVolume` in production. That mode is an explicit rollback boundary:
one pod, the existing per-container HMAC environment values, and the shared
proposal `emptyDir`.

`governedCore` is the migration target. It renders separate proposer and apply
Deployments, separate release-scoped ServiceAccounts, separate NetworkPolicies,
and no shared proposal volume. The proposer retains a pod-local bounded
artifact directory only for diagnostic and rollback compatibility. The apply
pod receives approved exact bytes from Mandate Core in reserved, platform-owned
job claim metadata backed by Core's persisted resume state.
It has no storage credential, artifact list permission, producer workspace, or
model-gateway access.
Both worker NetworkPolicies are mandatory in `governedCore`; chart rendering
fails if either policy is disabled.

## Preconditions

Do not switch production values to `governedCore` until immutable releases
containing all of the following have been published and pinned:

1. the workload release that emits explicit handoff metadata, supports
   `governed_core`, validates Core grants, and retains legacy fallback;
2. the Core release that derives the approval binding from custodied bytes,
   rechecks it at resolution, and issues the single-consumer grant;
3. release tuples for both `opencode.proposer` and
   `opencode.apply_executor` whose image digests equal the runtime images.

Runtime-token attestation is not claimed here; that remains CES-576.

## Canary order

1. Deploy the compatible workload images while production remains
   `legacySharedVolume`.
2. Deploy the compatible Core image.
3. Render `governedCore` and verify that the two Deployments have different
   release-scoped ServiceAccount names, different worker ids, different HMAC
   secret keys, and no common proposal volume.
4. Canary the split pods with `identity.mode: hmac`. Exercise success, missing
   artifact, tamper, wrong producer, stale artifact, retry, and Core-backend
   failure paths.
5. Move proposer and apply independently to `identity.mode: projected`.
   Verify each token subject maps to that worker's immutable
   code/manifest/image tuple and retain the previous tuple during drain. Use a
   worker-scoped release artifact so its image, release pin, registry import,
   ServiceAccount subject, and `previousRelease` overlap move as one phase
   without re-pinning the other worker. The retained HMAC token and metadata
   stay bound to that explicit previous tuple; the identity drift gate checks
   the projected subject against current and rollback HMAC against previous.
6. Keep the HMAC keys available until the canary and CES-576 decision are
   complete. CES-577 does not delete or rotate them.

## Failure and rollback

Artifact selection, approval binding, and grant minting fail closed. Exact
content is bounded to 512,000 bytes, proposals age out after 24 hours, and each
grant expires with its designated action. Existing job retention owns physical
deletion; the terminal sweep redacts exact proposal and grant content before
archival while retaining digests and binding metadata. Workers receive no
deletion or listing capability.

For an identity-only rollback, leave the split Deployments and governed Core
handoff in place and change the affected worker from `projected` back to
`hmac`. In the same reviewed GitOps change, remove that worker's projected
`service_account_subject` and `previous_release` registry bindings while
leaving its bounded HMAC secret unchanged. This restores only that worker's
own HMAC.

Before the governed Core canary is accepted, a full rollback may restore
`legacySharedVolume` and the compatible co-located workload image. Never roll
Core back while leaving `governedCore` apply pods active: those pods correctly
reject a resume that has no Core artifact grant.
