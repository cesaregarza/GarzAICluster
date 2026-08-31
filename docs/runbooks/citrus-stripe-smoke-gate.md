# Citrus development Stripe smoke gate

This runbook covers only the `citrus-dev` Stripe test-mode smoke runner. It does
not authorize a production read, write, deployment, provider configuration
change, or payment.

## Safety boundary

The chart ships both runner paths disabled. `stripeSmokeRunner.enabled=true`
materializes the suspended CES-883 operator CronJob. Automation additionally
requires `stripeSmokeRunner.automation.enabled=true`, which replaces that
CronJob with one exact-image Argo CD `PostSync` Job.

Do not enable either path until CES-854 has verified the intended Stripe test
account, webhook registration, API version, and signing-secret ownership. The
activation PR must bind the exact non-live `acct_...` identifier in the
dev-only reviewed values field; Stripe account IDs are identifiers, not secret
credentials. That narrow activation value is the only Git exception. Never put
the identifier in this runbook, logs, alerts, screenshots, or tickets, and
never put credentials, webhook signing material, provider object IDs, or
payment details in Git or any of those channels.

Normal web, worker, migration, scheduler, and recurring workloads remain in
`PAYMENT_NETWORK_MODE=deny`. Only the runner carries the smoke marker and the
dedicated Stripe Cilium policy. The automated runner's only entity-based
egress is `kube-apiserver` on TCP/443 for its exact Job and receipt updates;
broad `world`, `cluster`, host, and node entities remain forbidden.

## Before activation

1. Confirm the source image is an immutable lowercase 40-hex SHA and includes
   the stage-2 runner-state contract. The automated render must project
   `PAYMENT_EGRESS_POLICY_REVISION=ces-881-stripe-smoke-gate-v1`; the manual
   CES-883 path retains `ces-883-stripe-smoke-v1`. This mismatch is deliberate:
   a stage-1 image must fail before provider contact if automation is enabled.
2. Render the dev overlays with an account-shaped sentinel. Confirm the runner
   is absent while disabled, the manual path is suspended, and the automated
   path contains one `PostSync` Job, one ServiceAccount/Role/RoleBinding, and
   one Stripe-bearing Cilium policy.
3. Confirm `citrus-dev` has no `ApplyOutOfSyncOnly=true` sync option and no Argo
   retry block. A failed financial smoke must never be blindly retried.
4. After CES-854, submit a separate acknowledged dev-only activation PR with
   the exact test account ID. Do not merge or sync it as part of an unrelated
   image update.
5. Run the suspended stage-1 path once in the projected environment before
   enabling automation. The provider campaign must be small, test-mode-only,
   and leave zero active runner-owned objects.

## Per-image lifecycle

The automated Job is named `citrus-smoke-<full-source-sha>`. Argo starts it only
in `PostSync`, after normal resources and migrations are Healthy. It uses
`restartPolicy: Never`, `backoffLimit: 0`, and a bounded active deadline.

Before any provider request, the runner atomically claims the full SHA in the
runner-created `citrus-dev-stripe-smoke-receipts` ConfigMap. These outcomes are
sticky:

- `passed`: a later sync exits successfully with zero provider contact.
- `failed`: a later sync fails closed with zero provider contact.
- `running`: treat this as an interrupted or timed-out run; a later sync fails
  closed with zero provider contact.

The ConfigMap is intentionally not chart-managed. Argo self-heal must not erase
its durable state. The runner can create ConfigMaps but can read or mutate only
that exact ConfigMap; it can patch progress labels only on its own exact-SHA
Job.

## Success

The runner persists a sanitized receipt for the exact source SHA, reports zero
active runner-owned objects, and exits zero. `HookSucceeded` removes the
successful Job and Pod while the ConfigMap receipt remains. Success sends no
operator notification.

Inspect only the named dev ConfigMap and verify the receipt contains semantic
outcomes, timestamps, test-mode classification, and the exact SHA. Stop if it
contains an account ID, provider object identifier, payment detail, credential,
secret fragment, or unbounded error text.

## Production promotion attestation

For every production promotion PR, complete the CES-806 merge-day checklist
before merge:

1. Copy only the exact lowercase 40-hex SHA from the durable `passed` receipt
   for that image. Do not copy receipt metadata, logs, or provider identifiers.
2. Set both `image.tag` and `stripeSmokePromotion.verifiedImageTag` to that exact
   SHA in `helm/citrus/values.yaml` in the same reviewed promotion commit.
3. Render the production values and require exact equality. A receipt for an
   ancestor, a failed or interrupted receipt, a mutable tag, or any mismatch
   blocks promotion.

The release updater deliberately fails closed while the manual-attestation
binding is enabled; it cannot confer promotion authority or copy a cluster
receipt automatically. Never disable the gate merely to make a promotion pass.

## Failure or timeout

A failed or deadline-exceeded Job remains for diagnosis and fails the Argo
operation. The alert identifies `citrus-dev`, the exact SHA, Job name, and the
last fixed allowlisted smoke step, and states that release promotion is
blocked. Do not assume alert delivery works until the separately authorized
garz-observability sync and controlled receiver verification have completed.

Inspect only the retained dev Job/Pod events, sanitized logs, and the named
receipt ConfigMap. Do not rerun merely because the failure looks transient.
First confirm cleanup reports zero active runner-owned objects and resolve the
failure or timeout cause.

## Explicit rerun

Rerunning provider mutations requires an operator decision. After the cause is
fixed and cleanup is proven, remove only the exact SHA entry from
`citrus-dev-stripe-smoke-receipts`, then remove only the failed exact-SHA Job.
The next separately acknowledged sync can create one new attempt. Removing the
Job without clearing its record performs no provider contact; clearing the
record without removing a retained failed Job does not create a new hook.

Never delete the whole ConfigMap to rerun one SHA. Never add an Argo retry
policy or Kubernetes Job retry.

## Emergency disable

Submit a dev-only GitOps change setting
`stripeSmokeRunner.automation.enabled=false` and
`stripeSmokeRunner.enabled=false`. Review the render to confirm the Job, RBAC,
Secret projection, and Stripe policy disappear. A disable does not erase
receipts, cancel provider objects, roll back an image, or mutate production;
handle any cleanup as a separate, explicit dev-only operation.
