# Citrus payment credential isolation

This runbook is the value-free operator contract for CES-844. It removes
production payment configuration from `citrus-dev` without treating trusted dev
or the shared GitOps control plane as an attacker. It does not rotate production
credentials and does not implement the startup classification or zero-egress
controls owned by CES-845.

## Safety rules

- Make zero Stripe network contact during repository work, rendering,
  classification review, or rollout inspection.
- Never print, paste, hash, fingerprint, suffix, screenshot, attach, or commit a
  credential or identifying fragment. Never inspect a container environment or
  retrieve Kubernetes Secret data.
- Receipts contain only semantic classifications, object/key names, workload and
  source revisions, timestamps, status, and reviewers.
- Do not create a charge, refund, PaymentIntent, SetupIntent, Checkout Session,
  subscription, invoice, transfer, file, webhook test event, or other financial
  object as verification.
- Encrypted-source edits, GitOps merge, Argo reconciliation, rollout/restart, and
  deployment remain separately authorized actions.
- A merge to this repository can cause automatic Argo reconciliation. Merge only
  when its exact head and downstream effect are authorized.

## Threat model and production decision

The Git repository, Argo CD, KSOPS/SOPS runtime, cluster administrators, and dev
namespace administration are trusted control-plane components. A shared Argo CD
instance and shared SOPS recipient are acceptable. Secret references remain
namespace-local, and workload identities must not have cross-namespace Secret
read or pod-create authority.

The production payment configuration found inside trusted dev infrastructure is
a configuration error, not a credential disclosure under this threat model. No
production replacement, rotation, or revocation is required by CES-844 absent
new evidence of disclosure outside the trusted boundary. Production Secret
sources, values, Applications, workloads, and provider state remain unchanged.

## Incident baseline

The value-free 2026-08-23 receipt ties the finding to Citrus source
`a728346e4364d88b8ec35d9fe10f4fe2e76dc2a9`, GarzAICluster revision
`bc91feff51e54bf19cf8b775074d56bbf407f783`, and Argo Application/namespace
`citrus-dev`. The classifier reported the projected API and publishable payment
settings as live-mode. Webhook settings were present but not authoritatively
classified. Feature flags were disabled and the emergency stop was enabled;
those controls did not prevent projection.

At that revision:

- `secrets/citrus-dev/django-secrets.enc.yaml` produces
  `citrus-dev/django-secrets`.
- `secrets/citrus/django-secrets.enc.yaml` produces
  `default/django-secrets`.
- Both broad objects expose the payment key names `STRIPE_PUBLIC_KEY`,
  `STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `STRIPE_WEBHOOK_SECRET_DEV`, and `STRIPE_WEBHOOK_SECRET_PROD`.
- Payment and database settings share `django-secrets`, which the chart imports
  with `envFrom`.
- The broad Secret is projected into web and media Deployments, the migration
  Job, media CronJobs, the billing Deployment and metrics sidecar, and recurring
  CronJobs. Disabled workloads remain future projection paths.
- Secret-only reconciliation does not change long-running pod templates, so the
  final dev change requires a reviewed rollout trigger or restart.

A metadata-only live authorization review on 2026-08-23 confirmed deployed dev
workloads use `citrus-dev/default`. That ServiceAccount cannot get or list
Secrets and cannot create Pods in either `citrus-dev` or `default`. The unsafe
state is the content intentionally projected from the dev namespace, not
cross-namespace RBAC.

## Ownership

| Role | Responsibility |
| --- | --- |
| Incident/operator owner | Owns the stop/go decision and may perform secret-management, GitOps, Argo, and rollout actions when each action is explicitly authorized. |
| Independent verifier/reviewer | Reviews the encrypted/config diff and verifies value-free dev isolation evidence. |

CES-844 does not require different people or controllers for dev and production.
One incident/operator owner may perform the operational actions. Independence is
required only for the evidence used to close the ticket.

## Target state

CES-844 is complete only when all of these are true:

1. Dev uses an environment-explicit payment Secret name such as
   `citrus-dev-payment-credentials`; production keeps its existing configuration
   until an independently authorized production change is requested.
2. `citrus-dev/django-secrets` remains encrypted and unchanged, but an activated
   dev render no longer imports it with `envFrom`. Workloads reference only its
   five database fields and the exact dev webhook field. No process receives its
   legacy payment API, generic webhook, or production webhook fields.
3. Every dev Django process receives the same dedicated test-mode API and
   publishable settings, exact dev webhook setting, and value-free owner
   `citrus-dev`. This intentionally includes web, migrations, workers, Jobs, and
   CronJobs: the trusted dev threat model separates dev from production, not one
   dev process from another.
4. The API and publishable settings are authoritatively classified as test-mode
   `non-live`. The signing setting is selected only from the environment-explicit
   dev webhook field and has no API or financial-object authority. Live API and
   production webhook settings are forbidden in dev.
5. The dev workload identity remains unable to read production Secrets or create
   production Pods. Cluster administrators and Argo remain trusted.
6. `citrus-dev` is `Synced` and `Healthy`, expected replicas are ready, and a
   value-free receipt identifies the source, Secret, key names, workload
   revisions, classifications, timestamp, and reviewer.
7. No provider call or financial object is used for verification.

CES-845 remains separate. It owns automatic startup classification,
intentional-absence behavior, payment-adapter denial, and independent zero-egress
enforcement. CES-844 must not weaken or duplicate those controls.

## Repository preparation in PR #576

The preparation adds a disabled chart contract and environment-explicit
activation overlays:

- `helm/citrus/values-payment-prod.yaml` names
  `citrus-prod-payment-credentials` for possible future production work. It is
  owned by `citrus`, is not referenced by the production Application, and makes
  no active change.
- `helm/citrus/values-payment-dev.yaml` names
  `citrus-dev-payment-credentials`, is owned by `citrus-dev`, and selects only
  the dev webhook setting.
- PR #576 initially coupled credential projection to the CES-845 payment-safety
  contract. The dev activation PR removes that coupling because test-mode
  credential isolation is independently complete; CES-845 remains responsible
  for startup classification and zero-egress behavior.
- The production overlay remains unreferenced. The dev overlay is referenced
  only by the separately reviewed activation PR.
- Activation uses explicit, non-optional `secretKeyRef` entries rather than
  importing the dedicated Secret with `envFrom`.
- Every dev Django runtime receives test API and publishable settings from the
  dedicated Secret, exactly one existing dev webhook field, and
  `STRIPE_WEBHOOK_SECRET_OWNER`. Redis remains provider-free. No runtime imports
  the broad application Secret.
- A semantic rollout revision changes pod templates without deriving anything
  from credential material.

PR #606 initially projected the full set to web only. Its migration hook then
failed the current all-process production configuration check before any new
generation became ready. PR #607 restored the previous render. The corrected
activation projects the same test-mode set to every trusted dev Django process,
which satisfies both the current check and CES-845 without weakening the
dev/production boundary.

PR #576's inert preparation created no Secret, credential, ciphertext,
Application reference, live mutation, sync, or rollout.

## Remaining dev credential decision

The operator supplied one test-mode API setting and its matching publishable
setting through the owner-only ignored handoff. The handoff was validated by
key role and semantic mode only. The existing `STRIPE_WEBHOOK_SECRET_DEV` field
is retained by exact `secretKeyRef`; its value is never read or copied. No
generic or production webhook field is projected. No placeholder, provider
request, or production replacement is involved.

## Ordered execution

### Gate A: merge inert preparation

1. Review the exact PR head and confirm hosted checks pass.
2. Prove active prod and dev renders are byte-identical to the pre-PR baseline.
3. Merge only with explicit authorization for the merge and its automatic Argo
   revision reconciliation.
4. Observe; do not manually sync. Confirm the Citrus Applications select the
   merge revision while Deployment generations, ReplicaSet revisions, images,
   and ready replica counts remain unchanged.

### Gate B: prepare the dev-only encrypted source

This gate requires an approved result from the dev credential decision plus
explicit encrypted-source write and PR-publication authorization. It makes no
live Secret, merge, Argo, rollout, or deployment mutation.

1. Create an encrypted dev-only Secret source named
   `citrus-dev-payment-credentials` under `secrets/citrus-dev/`.
2. Include only the canonical API and publishable key roles, both classified
   test-mode `non-live`. Do not add any webhook or production setting.
3. Leave the encrypted dev `django-secrets` source unchanged. When the dedicated
   projection is enabled, replace its broad `envFrom` import with exact database
   references and one exact dev webhook reference for every dev Django runtime.
4. Add the dedicated encrypted file to the dev KSOPS generator.
5. Add `values-payment-dev.yaml` to the dev Application without enabling or
   modifying the CES-845 contract. Do not change the production Application or
   any production encrypted source.
6. Render and review object names, namespaces, key names, and workload
   projection paths only. No plaintext or identifying derivative may appear in
   the diff or logs.
7. Open a dev-only PR and record its exact head, tree, rollback commit, and
   affected Applications. Do not merge.

### Gate C: merge, reconcile, and roll dev

This gate requires separate authorization for GitOps merge, automatic or manual
Argo reconciliation, rollout/restart, deployment, verification, and rollback.

1. Merge only the exact reviewed dev-only head.
2. Reconcile only the selected merge revision. Stop if another revision is
   selected or any production render changes.
3. Roll the dev workloads so the broad Secret projection is removed from every
   new pod. Future dev Jobs and CronJobs must render the same dedicated
   test-mode settings and exact dev webhook field, never legacy or production
   payment fields.
4. Confirm `citrus-dev` and `citrus-dev-secrets` are `Synced` and `Healthy`, all
   expected replicas are ready, and workload revisions match the receipt.
5. Run only an approved value-free, zero-network classifier. A live API
   classification or any projected production webhook field is a stop condition.
6. Obtain the independent verifier/reviewer approval and close CES-844. Leave
   production unchanged.

## Sanitized receipt

Record one row per setting/workload path with these fields only:

```text
observed_at
environment
source_revision
gitops_revision
argo_application
argo_sync_status
argo_health_status
namespace
secret_source
secret_object
secret_key_name
credential_role
classification
workload_kind
workload_name
workload_revision
desired_replicas
ready_replicas
verification_result
reviewer
```

Allowed final API/publishable classifications are `non-live`; the selected
webhook classification is `dev-endpoint-only`.
The receipt must state that no credential value, fragment, hash, fingerprint,
provider identifier, provider network smoke, financial object, or test event was
produced.

## Stop and rollback rules

- Stop on an unreviewed GitOps head, unexpected Argo revision, ambiguous Secret
  source, production payment setting in dev, incomplete projection inventory,
  classifier error, unknown classification, unready replica, or any request to
  expose credential material.
- A dev rollback must preserve isolation. Never restore the production payment
  configuration to dev to recover service.
- Do not patch Argo state, force a sync, restart workloads, merge another PR, or
  change a live Secret without its matching explicit authorization.
