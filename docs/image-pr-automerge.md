# Automatic Citrus development image PRs

Citrus development follows: source tests → image publication → GAIC PR →
GAIC checks → automatic merge → existing Argo auto-sync and PostSync checks.
Production image PRs require a human merge decision. Production's existing
Argo auto-sync remains unchanged: manually merging production values can deploy.

## Eligibility

The source workflow opts in only for a successful new image build on a push
to `cesaregarza/Citrus` `dev`. Manual workflow dispatch, skipped builds,
production and no-change updates do not opt in. Source tests, release
capabilities and the updater's exact commit/path receipts remain mandatory.

`automation/image-automerge.json` initially enables only `citrus-dev`.
The PR must target GAIC main from the same repository and contain only these
release references, all pointing to the same 40-character built source SHA:

- `helm/citrus/values-dev.yaml`: `image.tag`,
  `recurringRuntime.expectedSourceRevision`.
- `helm/citrus/values-payment-dev.yaml`:
  `directOrderPaymentSweep.verifiedImageTag`.

Image repository, resources, replicas, storage, permissions, networking,
production values and workflow changes cannot join an automatically merged
image PR. Existing source-SHA image tags are retained; this change does not
introduce registry digest pinning or attest that a tagged image is immutable.

## Merge checks

Enable repository auto-merge and require these GitHub Actions checks on main,
while preserving existing protection and administrator enforcement:

- `agent-workloads-identity-digest-drift` (already required).
- `config-ci-required`: every Config Repo CI validation job must succeed.
- `plaintext-secrets`.
- `immutable-image-tags`.
- `image-pr-policy`.

The security scans run on every main PR, so their required statuses cannot
remain absent because of path filters. The aggregate treats skipped, cancelled
and failed validation as a failure.

The source calls `scripts/image_pr_automerge.py` on the exact PR/head it just
created and verified. The helper reads repository protection, validates the
complete file/value delta, then uses native GitHub auto-merge with
`--match-head-commit`. It defaults to read-only without `--apply`. The existing
config writer needs contents/PR write and permission to read branch protection
(`Administration: read` for fine-grained or App tokens). It never submits an
approval or bypasses branch rules.

A separate `pull_request_target` workflow runs the policy from the trusted base
revision and parses candidate files as data; it never executes candidate code.
It rechecks the current image PR on every push, reopening, ready transition or
base edit. This matters because GitHub can keep auto-merge enabled after a
write-authorized user pushes another commit. Mixed changes fail the required
scope check even when auto-merge remains queued. Source publishers and users
who can change protected policy remain trusted release operators.

The repository currently uses native auto-merge without a merge queue. Preserve
the existing up-to-date requirement setting. Add and verify merge-group policy
semantics before enabling a merge queue; the image scope workflow currently
reports only on PR events.

## Installation and acceptance

1. Merge the reviewed GAIC implementation after its local and hosted checks.
   The new trusted-base workflow cannot validate its own installation PR.
2. Save current repository settings and branch protection. Enable auto-merge
   and add all five required Actions checks; read back to verify. Do not remove
   other owners' requirements.
3. Merge the reviewed source workflow change to Citrus dev after source CI.
   Its subsequent successful build creates and opts in the first real image PR.
4. Verify every required check reports on that PR's current head, native
   auto-merge completes, and Citrus dev Argo sync/health and existing PostSync
   acceptance finish. This is the live end-to-end proof; offline mocks alone
   are not deployment acceptance.

A failed check leaves the PR unmerged. Inspect and fix the owning source/config
problem; do not use an admin merge to make the automated release appear green.
A failure reading protection also leaves a manual PR; do not silently switch
to weaker checks or a more privileged token.

## Stop or roll back

Disable the `citrus-dev` policy or remove the source opt-in step to stop future
automatic requests. Also disable auto-merge on any already queued image PR;
a previously passed status does not retroactively change when policy changes.
Keep the scope guard installed and do not delete its entry while a PR is queued.

For a queued PR:

```bash
gh pr merge --disable-auto <PR_URL> --repo cesaregarza/GarzAICluster
```

Roll back an application through an explicit reviewed release PR using the
previous source/image references. Disabling merge automation does not revert a
deployed application. Adding another application's policy is a separate
reviewed opt-in, including its source publisher and rollout behavior.
