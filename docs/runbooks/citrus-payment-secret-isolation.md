# Citrus payment credential isolation and rotation

This runbook is the value-free operator contract for CES-844. It separates the
preparation that can be reviewed in Git from the provider, secret-management,
GitOps, Argo CD, and rollout mutations that each require explicit authorization.
It does not implement the application startup or network-egress controls owned by
CES-845.

## Absolute safety rules

- Make zero Stripe network contact during inventory, preparation, rendering, or
  classifier review. Provider work begins only at the labeled provider gate.
- Never print, paste, hash, fingerprint, suffix, screenshot, attach, or commit a
  credential or identifying fragment. Do not decrypt a Secret to stdout, inspect
  a container environment, or enable shell tracing around secret work.
- Receipts contain only semantic classifications, Kubernetes object/key names,
  workload identities and revisions, source/config revisions, timestamps, and
  reviewers.
- Do not create a charge, refund, PaymentIntent, SetupIntent, Checkout Session,
  subscription, invoice, transfer, file, webhook test event, or other financial
  object as a smoke test.
- Never revoke an exposed production credential until its replacement is
  deployed in production and independently verified.
- Rotation/revocation, provider changes, encrypted-Secret edits, production
  Secret changes, GitOps merge, Argo reconciliation, rollout/restart, and
  deployment are separate authorization gates. Stop at every ungranted gate.
- A merge to this repository can trigger automated Argo reconciliation. Under
  the current policy, do not authorize or perform the merge unless the matching
  reconciliation and deployment authorization is already recorded, or a
  separately authorized mechanism has first made reconciliation inert.

## Incident baseline

The value-free 2026-08-23 receipt ties the finding to Citrus source
`a728346e4364d88b8ec35d9fe10f4fe2e76dc2a9`, GarzAICluster revision
`bc91feff51e54bf19cf8b775074d56bbf407f783`, and Argo Application/namespace
`citrus-dev`. The classifier reported the projected API credential and
publishable credential as live-mode. Webhook variables were present but were
not authoritatively classified. Feature flags were disabled and the emergency
stop was enabled; those controls did not prevent credential projection.

At that revision, the config source has these properties:

- `secrets/citrus-dev/django-secrets.enc.yaml` produces
  `citrus-dev/django-secrets`.
- `secrets/citrus/django-secrets.enc.yaml` produces
  `default/django-secrets`.
- Both objects expose the same payment key names:
  `STRIPE_PUBLIC_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`,
  `STRIPE_WEBHOOK_SECRET`, `STRIPE_WEBHOOK_SECRET_DEV`, and
  `STRIPE_WEBHOOK_SECRET_PROD`.
- Payment and database settings share `django-secrets`; the chart imports that
  entire Secret with `envFrom` rather than selecting payment keys explicitly.
- Dev and production already have separate namespaces and Argo Applications,
  but both broad Secret objects are named `django-secrets` and the dev object
  contains production payment authority. The shared Git repository, Argo CD,
  and SOPS runtime are trusted control-plane components and are not the
  isolation boundary for this incident.
- Secret-only reconciliation does not change the pod-template checksum for the
  long-running Deployments. Rotation therefore requires an explicit restart or
  another reviewed rollout trigger.

The chart projects `django-secrets` into the web and media-worker Deployments,
the migration Job, the media requeue and garbage-collection CronJobs, the
billing-worker Deployment and metrics sidecar, and recurring tick/health
CronJobs. Disabled workloads still count as projection paths because enabling
them creates a pod with the same Secret import. CI may write image values in this
repository but does not receive the runtime payment Secret.

A metadata-only live authorization review on 2026-08-23 confirmed the deployed
dev workloads use `citrus-dev/default`. That ServiceAccount cannot get or list
Secrets and cannot create Pods in either `citrus-dev` or `default`. The incident
is therefore caused by production payment material stored in and projected from
the dev namespace, not by dev workload RBAC reaching across namespaces.

## Required owners

Record a named person and timestamp for every role before execution. An empty or
ambiguous owner is a stop condition.

| Role | Responsibility in this runbook |
| --- | --- |
| Incident/operator owner | Owns the stop/go decision and may perform provider, secret-management, GitOps, Argo, and rollout actions when each action is explicitly authorized. |
| Independent verifier/reviewer | Reviews the encrypted/config diff and verifies production replacement and dev isolation before revocation. |

CES-844 does not require separate people for provider, secret-management,
GitOps, or Argo operation. One named incident/operator owner may perform those
actions. Independence is required only for the final evidence used to authorize
revocation and close the incident.

## Target isolation contract

The implementation must establish all of the following before the incident can
close:

1. Use distinct, environment-explicit payment Secret names, for example
   `citrus-dev-payment-credentials` and `citrus-prod-payment-credentials`.
2. Keep database, application, email, media, registry, and payment credentials
   in separate objects. Do not preserve the current all-or-nothing
   `django-secrets` payment projection.
3. Treat the Git repository, Argo CD, KSOPS/SOPS runtime, and cluster
   administrators as the trusted deployment control plane. A shared Argo CD
   instance and shared SOPS recipient are acceptable. Isolation is enforced at
   the workload and namespace boundary: the dev source must produce only the dev
   payment Secret, Secret references are namespace-local, and dev workload
   identities must have no production Secret-read or production pod-create
   authority.
4. Project only the payment keys each approved workload needs. A web, worker,
   Job, or CronJob with no payment responsibility must not receive the payment
   Secret.
5. Dev may receive dedicated non-live credentials, or no payment credential when
   payment behavior is disabled. Live, restricted-live, mixed, unknown, and
   shared-production classifications are forbidden in dev.
6. Dev must not receive a production webhook signing Secret. An unclassified
   webhook variable is unsafe until an authorized classifier records its role and
   environment semantically.
7. Maintain a value-free rotation ledger per environment with credential role,
   classification, owner, creation/rotation timestamp, reviewer, Secret object,
   and supersession status. Never record a value or identifying derivative.
8. Keep CES-845 separate: fail-closed application classification and independent
   zero-egress enforcement are required release gates, but are not implemented by
   this runbook or by a CES-844 Secret-only change.
9. Keep production and dev changes in separate deployable heads. For every head,
   record a source-revision impact matrix that lists every Argo Application and
   Secret Application following the changed revision, even when its rendered
   content is unchanged. A shared-chart change must be staged so the environment
   outside the current gate renders identically; otherwise split it again.
10. Bind every mutation authorization to the exact reviewed head SHA, reviewed
    Git tree object, and immutable impact-matrix reference. A changed head, tree,
    target, or matrix invalidates the authorization rather than inheriting it.

## Prepared repository controls

The review-only CES-844 preparation adds a disabled chart contract and two
environment-explicit activation overlays:

- `helm/citrus/values-payment-prod.yaml` selects
  `citrus-prod-payment-credentials` and the production webhook setting.
- `helm/citrus/values-payment-dev.yaml` selects
  `citrus-dev-payment-credentials` and the dev webhook setting.
- Neither overlay is referenced by an Argo Application. The current production
  and dev renders therefore remain unchanged until a separately reviewed
  Application change activates one overlay.
- Activation projects explicit `secretKeyRef` entries instead of importing the
  dedicated Secret with `envFrom`. Web receives API, publishable, and exactly one
  environment webhook setting. Billing and recurring consumers receive API
  authority only. Media, migrations, metrics, Redis, and media CronJobs receive
  no dedicated payment reference.
- Every dedicated reference is non-optional. A semantic rollout annotation is
  supplied by values; it must never be derived from credential material.

This preparation creates no Secret, private key, ciphertext, provider
credential, live controller, Application reference, sync, or rollout. It also
does not remove the legacy payment keys from `django-secrets`; that removal must
be atomic with activation of the dedicated source so nonpayment consumers cannot
retain the old projection.

At Citrus source `a728346e4364d88b8ec35d9fe10f4fe2e76dc2a9`, dev uses the
production-runtime validation path and will not start with payment credentials
intentionally absent. Until CES-845 supplies an approved absence mode, Gate 5
therefore requires dedicated non-live API and webhook credentials supplied by
the operator through the approved secret-management path. This repository
cannot manufacture those credentials.

The existing shared Argo CD and KSOPS/SOPS runtime may reconcile both sources.
No separate Argo architecture, CMP sidecar, or encryption recipient is required
for CES-844. A cluster-administrator or Argo-control-plane compromise is outside
this incident's threat model; a compromised dev workload or namespace identity
must still be unable to obtain a production payment credential.

## Ordered execution gates

### Gate 0: freeze and authorize

1. Freeze payment-related promotion from dev.
2. Confirm the exact Citrus source SHA, GarzAICluster SHA, container image, Argo
   revisions, namespaces, and current rollout revisions.
3. Record the incident/operator owner and independent verifier/reviewer.
4. Record a separately scoped authorization reference for every applicable
   action: provider credential creation; provider-to-secret-manager handoff;
   encrypted-source write; access-policy mutation; production or dev Secret
   mutation; GitOps merge; Argo reconciliation; rollout/restart; deployment;
   provider verification; rollback; provider revocation; and ledger/receipt
   mutation. One authorization never implies another. Missing authority stops
   the procedure.
5. Bind each repository, GitOps, Argo, rollout, deployment, verification, and
   rollback authorization to its exact reviewed source revision, reviewed Git
   tree object, immutable impact-matrix reference, target environment/system/
   resource, authorizer, and validity interval.
6. Confirm the rollback target is immutable and still available.

### Gate 1: value-free projection inventory

1. From Git, enumerate only the key names under `stringData` in the dev and
   production encrypted manifests. Do not output encrypted fields or ciphertext.
2. Render the dev and production charts locally and inventory every
   `envFrom.secretRef`, `secretKeyRef`, and `imagePullSecrets` reference by
   workload kind, workload name, container, and enabled/disabled state. Rendered
   chart output must not contain Secret data.
3. From Kubernetes, query workload specifications and controller/status metadata
   only. Record workload kinds/names, Secret reference names, images,
   generations, desired/ready replicas, selected revisions, and health. Do not
   retrieve or serialize Secret data.
4. Inventory Kubernetes authority from `ServiceAccount`, `Role`, `RoleBinding`,
   `ClusterRole`, and `ClusterRoleBinding` metadata plus semantic authorization
   review results. Record subject names, verbs, resources, and resourceNames only
   for Secret read/update and pod create/exec authority; never exercise that
   authority or retrieve Secret data.
5. Inventory Argo `AppProject` roles, Application destinations, controller
   service accounts, and repo-server/CMP mounted Secret names. Record identity,
   resource, namespace, and policy names only. Do not inspect mounted Secret
   contents, private keys, or repository credential payloads.
6. Inventory CI workflow permissions, environment protection metadata, broker
   capability names, GitHub App/repository access metadata, and operator/ad hoc
   Job creation paths. Record policy and identity names only; do not invoke a
   broker capability or secret-bearing workflow.
7. Obtain value-free ownership and access-policy attestations for the provider
   and secret-management systems from existing approved records. Gate 1 makes
   zero Stripe network contact and does not request provider credential metadata.
8. Include web, media worker, billing worker, migration hook, media CronJobs,
   recurring CronJobs, ad hoc operator Jobs, CI identities, controllers, and
   users with pod create/exec or Secret-read authority.
9. Compare the result to the incident baseline. Any unowned or unknown path is a
   stop condition.

### Gate 2: prepare isolated sources

This gate requires separately scoped encrypted-source write and PR-publication
authorization. Neither implies the other. It makes no
provider, live Secret, GitOps merge, Argo, rollout, or deployment change.

1. Prepare separate dev and production payment Secret manifest definitions and
   value-free metadata ledgers under environment-explicit paths and object names;
   do not create or update a live Secret in this gate.
2. Retain the trusted shared Argo CD and SOPS machinery. Prove that the dev
   Secret Application renders only the dev payment object into `citrus-dev`, the
   production Secret Application renders only the production payment object into
   `default`, and the dev workload ServiceAccount cannot read or create paths to
   production Secrets.
3. Review the prepared explicit chart projection, then atomically remove payment
   keys from `django-secrets` when the dedicated payment source is activated.
   Keep nonpayment credentials in their existing objects.
4. Prepare dev for dedicated non-live credentials or intentional absence. If the
   application cannot start with intentional absence, route the startup-control
   change to CES-845 rather than weakening validation in CES-844.
5. Render and test both environments. The diff and test logs may contain names and
   classifications only.
6. Open separate production-content-only and dev-content-only deployable heads and
   reviewable PRs; do not merge. A backward-compatible shared-chart preparation
   may appear in a head only when the other environment renders identically.
7. For each head, record the exact SHA, reviewed Git tree object, rollback commit,
   rendered diff, and an immutable source-revision impact matrix naming every
   Argo Application and Secret Application that follows the changed revision,
   including Applications with an unchanged render. An unknown Application
   impact is a stop condition.

### Gate 3: create the production replacement

Stop here without explicit provider credential-creation authorization. That
authorization permits provider creation only; it permits no secret-management,
repository, cluster, GitOps, Argo, rollout, deployment, verification, rollback,
or revocation mutation.

1. The provider operator creates a least-privilege production replacement for
   each production authority proven or conservatively treated as exposed.
2. Keep the replacement in provider-approved custody. Do not transfer it to
   secret management until Gate 4 has a separate, recorded handoff authorization.
   It must not pass through chat, Linear, GitHub, logs, screenshots, shell
   history, or a plaintext file in the repo.
3. Record only credential role, semantic mode, owner, timestamp, and planned
   supersession. Do not record any identifier or derivative.
4. Do not revoke or disable the old production authority.

### Gate 4: deploy and verify the production replacement

Stop here unless separate authorization references exist for every action that
will occur: provider-to-secret-manager handoff, encrypted-source write,
production Secret mutation, GitOps merge, Argo reconciliation, workload
restart/rollout, deployment, independent provider verification, and rollback.
No authorization in this gate implies another. Because Argo tracks `main` with
automated self-heal, treat merge as causally sufficient to begin deployment; all
downstream authorizations must therefore be recorded before merge. Reconciliation
authorization is required for every Application in the source-revision impact
matrix, including a dev Application whose revision advances with an unchanged
render. Every authorization must bind the exact reviewed head SHA, Git tree
object, and immutable impact-matrix reference. If rollback is not separately
authorized, a failure freezes the rollout for escalation.

1. Under the handoff authorization, transfer the replacement directly from
   provider-approved custody into the authorized secret-management intake path.
   No GitHub, chat, ticket, log, screenshot, shell-history, or plaintext-repo
   intermediary is permitted.
2. Under the encrypted-source authorization, update only the isolated production
   payment Secret source and its value-free ledger. Review object names,
   object names, namespaces, and key names without decrypting in review output.
3. Confirm the head is production-content-only: its impact matrix may advance
   other Applications to the source revision, but the rendered dev Secret and
   workload content must be identical. Any dev rendered-content change is a stop
   condition and must move to the separate Gate 5 head.
4. Merge the exact reviewed GitOps head only after the production Secret,
   restart/rollout, deployment, verification, applicable rollback, and separate
   reconciliation authorization for every Application in the impact matrix are
   recorded. The allowed merge method must produce the reviewed Git tree object;
   any extra content invalidates every downstream authorization.
5. Under the production Secret-mutation and per-Application Argo-reconciliation
   authorizations, reconcile only the reviewed merge result. Prove the post-merge
   `main` tree is identical to the reviewed Git tree object, Argo selected that
   exact merge-result revision, and the Secret Application reconciled
   successfully. A different tree or selected revision is a P0 stop.
6. Under the separately recorded restart/rollout and deployment authorizations,
   restart long-running consumers or apply the reviewed rollout trigger; a
   Secret-only sync is insufficient. Prove the workload rollout used the
   intended immutable image digest and Secret object, then verify desired/ready
   replicas, Service endpoints, migrations, and application health.
7. The independent verifier proves the replacement is active using the
   separately authorized, non-financial provider verification path. No
   transaction or test event is permitted. Zero provider contact remains the
   rule unless this exact verification authorization is active.
8. If verification fails and rollback was separately authorized, restore the
   recorded still-valid prior Secret source, reconcile and roll out only within
   that rollback scope, then stop. Without rollback authorization, freeze and
   escalate. Do not revoke the old credential.

### Gate 5: isolate and roll dev

Stop here unless separate authorization references exist for every action that
will occur: dev encrypted-source write, dev Secret mutation, GitOps merge, Argo
reconciliation, workload restart/rollout, deployment, classifier execution, and
rollback. No authorization in this gate implies another. If rollback is not
separately authorized, a failure freezes the rollout for escalation.
Reconciliation authorization is required for every Application in the
source-revision impact matrix, including a production Application whose revision
advances with an unchanged render. Every authorization must bind the exact
reviewed head SHA, Git tree object, and immutable impact-matrix reference.

1. Remove every live, restricted-live, shared-production, mixed, unknown, and
   unclassified payment authority from the dev source.
2. Add only dedicated non-live credentials or record the setting as
   `intentionally-absent`. A disabled feature flag is not isolation evidence.
3. Remove `STRIPE_WEBHOOK_SECRET_PROD` from dev. Keep only the environment-specific
   webhook material that an authorized classifier proves belongs to dev, or leave
   webhook settings intentionally absent.
4. Confirm the head is dev-content-only: its impact matrix may advance other
   Applications to the source revision, but the rendered production Secret and
   workload content must be identical. Any production rendered-content change is
   a stop condition and must be split into a separately authorized head.
5. Under the encrypted-source authorization, prepare only the reviewed isolated
   dev source. Merge only after the dev Secret, restart/rollout, deployment,
   classifier, applicable rollback, and separate reconciliation authorization
   for every Application in the impact matrix are recorded. The allowed merge
   method must produce the reviewed Git tree object; any extra content invalidates
   every downstream authorization.
6. Under the dev Secret-mutation and per-Application Argo-reconciliation
   authorizations, prove the post-merge `main` tree is identical to the reviewed
   Git tree object and reconcile only the exact merge-result revision selected by
   Argo. Under the separately recorded restart/rollout and deployment
   authorizations, restart web and media Deployments explicitly; confirm future
   migration, media, billing, recurring, and ad hoc Job/CronJob templates cannot
   import the production payment Secret. A different tree or selected revision
   is a P0 stop.
7. Verify Argo is `Synced` and `Healthy`, every desired replica is ready, and the
   running workload revisions and Secret object names match the reviewed receipt.
8. Under the classifier-execution authorization, run the approved local/in-pod
   classifier with networking disabled. Its output is restricted to setting
   name, credential role, semantic classification, and Secret/workload metadata.
   It must emit no values or identifying derivatives.
9. Any live, restricted-live, mixed, unknown, or unclassified result is a P0 stop.
   Revocation remains forbidden. If rollout verification fails, use only a
   separately authorized rollback that preserves dev isolation; otherwise freeze
   and escalate.

### Gate 6: revoke the superseded production authority

Stop here unless separate authorization references exist for provider revocation,
post-revocation provider verification, and ledger/receipt update. No
authorization in this gate implies another. Revocation is allowed only after
Gate 4 proves production uses the replacement and Gate 5 proves dev no longer
possesses the superseded authority.

1. Reconfirm the production replacement, production health, dev isolation, exact
   revisions, and reviewer signatures from fresh evidence.
2. Under the provider-revocation authorization, the provider operator revokes
   only the explicitly superseded authority.
3. Under the separate post-revocation verification authorization, the independent
   verifier confirms production remains healthy without creating a financial
   object or test event.
4. Under the ledger/receipt authorization, update only the incident-system
   value-free records with `revoked`/`superseded` status and timestamps. A GitOps
   ledger change would require its own merge, reconciliation, and deployment
   authorizations and is not implied here.

### Gate 7: close and re-gate

1. Re-run the projection inventory across source, live workloads, Jobs/CronJobs,
   CI, and operator paths.
2. Confirm all dev classifications are `non-live` or `intentionally-absent` and
   that no production payment principal or webhook Secret is available to dev.
3. Confirm dev and production Secret names, source paths, namespaces, payment
   principals, classifications, and rotation records are distinct.
4. Confirm production health on the replacement, old-authority revocation, dev
   `Synced`/`Healthy`, expected ready replicas, and zero financial test objects.
5. Obtain the independent verifier/reviewer signature. Only then may CES-844 be
   considered complete and the dependent release gates be re-run.

## Sanitized receipt schema

The final receipt is value-free. Create one authorization record per granted
scope, one access/isolation record per credential role and authority path, and
one rollout/verification record per workload projection. An authorization
reference names a Linear or change-management record that states scope, owner,
time, exact reviewed source revision, reviewed Git tree object, and immutable
impact-matrix reference; it must not contain a provider credential identifier.

### Authorization record

```text
gate
mutation_scope
authorization_reference
target_environment
target_system
target_resource
reviewed_source_revision
reviewed_tree_digest
impact_matrix_reference
authorized_by
authorized_at
expires_at
```

### Access and isolation record

```text
observed_at
environment
secret_source
secret_object
secret_key_name
credential_role
classification
source_owner
rotation_owner
created_or_rotated_at
supersession_status
access_policy_reference
isolation_result
reviewer
```

### Rollout and verification record

```text
observed_at
environment
source_sha
gitops_revision
reviewed_source_revision
reviewed_tree_digest
impact_matrix_reference
merge_result_revision
tree_equivalence_result
argo_application
argo_revision
argo_sync_status
argo_health_status
namespace
workload_kind
workload_name
workload_revision
container_name
container_image
container_image_digest
projection_path
desired_replicas
ready_replicas
classifier_version
verification_method_class
verification_result
supersession_status
rollback_revision
reviewer
```

Allowed final dev classifications are `non-live` and `intentionally-absent`.
`live`, `restricted-live`, `mixed`, `unknown`, and `unclassified` are blocking.
Every field is semantic metadata only. The receipt must also state that no
credential value, fragment, hash, fingerprint, screenshot, provider identifier,
provider network smoke, financial object, or test event was produced.

## Stop and rollback rules

- Stop on a moving or unreviewed GitOps head, unexpected Argo revision, missing
  owner, ambiguous Secret source, a production principal projected into dev,
  incomplete workload inventory, classifier error, unknown classification,
  unready replica, or any request to expose credential material.
- Before revocation, production may roll back only to the still-valid prior
  production Secret source recorded at Gate 0.
- After revocation, never restore the revoked authority. Mint a new replacement
  under a new provider authorization if the current replacement fails.
- A dev rollback must preserve isolation. Never reintroduce a live or shared
  production credential to restore service.
- Do not patch Argo state, force a sync, restart a workload, merge a PR, or change
  a Secret as an improvised recovery action without its matching authorization.
