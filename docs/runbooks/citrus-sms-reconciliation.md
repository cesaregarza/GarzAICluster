# Citrus SMS reconciliation scheduler

CES-848 prepares a bounded Kubernetes CronJob for
`sweep_manual_order_sms_attempts`. It does not authorize a merge, deployment,
live run, provider credential, or outbound SMS. Production and dev both render
without the CronJob until a later operator-controlled GitOps change passes
every source, secret, capacity, artifact, and activation gate below.

## Current state: dormant and not activation-ready

Both actual environment value sets are deliberately fail-closed:

```yaml
smsReconciliation:
  enabled: false
  suspend: true
  secretName: ""
  verifiedImageTag: ""
  commandCompatibleImageTags: []
  networkPolicy:
    enabled: false
    provider: ""
    revision: ""
    database:
      host: ""
```

There is no approved least-privilege runtime Secret and no compatible corrected
image. Empty values are intentional blockers, not placeholders to fill from an
existing broad Secret.

The historical dev image
`0e2258bf95c6170895c26780258eb42d5b5c557c` contains the command but is not an
activation artifact. Its disabled-mode behavior can consume stale scheduled
attempts, and its production startup validation requires Stripe, email, cache,
Celery, media, and AWS settings that this DB-only Job must not receive. The
current development release pin is authoritative only in
`helm/citrus/values-dev.yaml`. It is not an SMS activation artifact while the
applied `smsReconciliation.commandCompatibleImageTags` list remains empty.

The source-state correction merged as
`2528782045766c13d751f212a4caf36734d08583`. Workflow run `32932217731`
succeeded, published the immutable image with that tag, and opened GitOps image
PR #591 at `e12af0ad`. That image predates the CES-845 source/runtime contract
in Citrus PR #144, so it is not compatible with this dedicated six-key startup
profile; PR #591 also remains unmerged. Never treat that artifact receipt as
activation approval or pre-pin it in this scheduler contract.

CI uses that historical command-bearing tag only for an isolated, suspended
manifest schema render. The tag, release-bound synthetic Secret name, and
reserved `.example` database FQDN are command-line test inputs and do not appear
in either environment value file. The render is never applied or executed and
is not evidence that the image can run least-privileged.

## Fixed command and immutable execution envelope

The executable and option names are hardcoded in the Helm template:

```text
python manage.py sweep_manual_order_sms_attempts \
  --stale-minutes 15 \
  --limit 100
```

Values cannot replace `python`, `manage.py`, the management-command name,
either option name, or either integer. Schema constants, template validation,
and literal rendered argv all pin `staleMinutes=15` and `limit=100`; any drift
or attempt to restore a free-form `command` fails the render.

The chart also pins `suspend=true`, schedule `*/15 * * * *`, `Etc/UTC`,
`concurrencyPolicy: Forbid`, both deadlines to 240 seconds, one retry, two
successful histories, and three failed histories. The ConfigMap, migrations,
egress-policy, and scheduler sync waves are canonical strings `"0"`, `"1"`,
`"2"`, and `"3"` in strict order. The migration hook command is also fixed at
`python manage.py migrate --noinput`; an enabled scheduler cannot render behind
an arbitrary or successful no-op migration hook. Requests are exactly 50m CPU
and 128Mi memory; limits are exactly 250m CPU and 256Mi memory. A values-only
unsuspend, resource increase, faster schedule, wave reorder, or migration-command
change fails closed; unsuspending requires a separately reviewed chart change.

The SMS pod security context is literal rather than inherited from mutable
global values: UID, GID, and fsGroup are `10001`, `runAsNonRoot` is true, and
seccomp is `RuntimeDefault`. The container separately pins the same numeric
identity, disables privilege escalation, drops every Linux capability, and uses
a read-only root filesystem. Global security-context overrides therefore cannot
weaken this prepared workload.

## Least-privilege Secret contract

When a future activation render sets `enabled: true`, `secretName` is required
and is release-bound: `citrus-sms-reconciliation-runtime` for `citrus/default`
or `citrus-dev-sms-reconciliation-runtime` for
`citrus-dev/citrus-dev`. Mutable broad-Secret values cannot redefine this
contract. The chart does not create the Secret and never renders Secret data.

The container projects exactly six named keys from the dedicated Secret:

- `DJANGO_SECRET_KEY`;
- `DB_NAME`;
- `DB_USER`;
- `DB_PASSWORD`;
- `DB_HOST`; and
- `DB_PORT`.

`DB_SCHEMA` and other non-secret application settings come from
`django-config`. No Secret is imported through `envFrom`; explicit
`secretKeyRef` entries ensure extra keys accidentally present in the Secret are
not exposed to the Job. All six references are non-optional so missing runtime
authority fails startup rather than falling back to SQLite or a placeholder app
key.

The Job also hard-sets:

```text
TWILIO_SMS_ENABLED=False
TWILIO_SMS_RECIPIENT_ALLOWLIST=
```

Explicit `env` entries override same-named ConfigMap values. No Twilio account,
API key, auth token, messaging-service, Stripe, email, or object-storage
credential is projected.

This manifest contract alone is not enough for activation: current Citrus
production validation rejects intentional absence of those unrelated settings.
CES-845 must deliver an explicitly reviewed source/runtime profile that boots
this command with only app-key and DB authority while preserving fail-closed
payment checks. Do not bypass that blocker with `DJANGO_ENV`, debug mode,
placeholder settings, broad Secret projection, or synthetic provider values.

## Dedicated egress boundary

An enabled scheduler cannot render without a same-release
`CiliumNetworkPolicy`. The policy selects only Pods carrying the dedicated
`citrus.grace/sms-reconciliation-egress=restricted` label and enters default
deny before the CronJob wave. Its entire egress allowlist is:

- the exact `kube-system`/`k8s-app=kube-dns` endpoints on TCP and UDP 53, with
  DNS queries restricted to the reviewed database hostname; and
- one exact lower-case database FQDN, capped at 253 characters, on the fixed
  managed PostgreSQL port `25060`.

Wildcards, IP literals, Stripe/Twilio/payment/messaging/email provider domains,
additional destinations, `world`, `all`, and `toEntities` are not configurable.
The policy provider must be `cilium`, the receipt revision must be nonempty, and
its sync wave is fixed at `"2"`, after migrations and before the suspended
CronJob at `"3"`. Actual prod/dev values keep the policy disabled with empty
provider, revision, and database hostname; CI uses only
`db.sms-reconciliation.example`.

This policy covers the chart-owned labeled scheduler Pod. It is not authority to
create an unlabeled one-off Job, and it does not replace the CES-845 source
startup/fail-closed contract or namespace RBAC review. Activation remains
blocked until those controls and the Cilium policy receipt are jointly verified.

## Security boundaries

The preparation makes separate, testable claims:

- **Workload identity:** the Pod receives no Kubernetes ServiceAccount token.
- **Runtime authority:** only six named app/DB Secret keys are projected; no
  provider authority is present.
- **Release binding:** an enabled render requires `verifiedImageTag` to equal
  the immutable `image.tag` and that tag to be present in the reviewed
  `commandCompatibleImageTags` list. The repository is fixed to
  `registry.digitalocean.com/sendouq/citrus`, the release/namespace pair is
  exact, migrations remain enabled, and the ConfigMap is `django-config`.
- **Network authority:** the dedicated default-deny policy admits only exact DNS
  and database egress and is rendered in the wave between migrations and the
  scheduler.
- **Runtime behavior:** a future source artifact must prove disabled-mode
  replay semantics and least-privilege startup without provider access. No
  current image satisfies this claim.
- **Deployment:** only a reviewed GitOps change may select an artifact and
  Secret. This runbook performs no merge, Argo sync, or cluster mutation.

No Stripe operation belongs to this command or runbook. Never provide Stripe
or Twilio credentials to a render, source test, Job, log command, or ticket.

## Required cross-repository train

The dormant chart slice must remain draft until the prerequisite contracts are
reviewable. Activation follows this order:

1. **Correct source state semantics.** A reviewed Citrus PR must preserve stale
   `SCHEDULED` attempts while outbound SMS is disabled, make no Redis/Celery or
   provider call, and retain replay-safe fencing/reporting behavior.
2. **CES-844 secret isolation.** Dev must no longer possess live, restricted,
   shared-production, or unknown payment authority, with a sanitized operator
   receipt. A merged preparation template is not completion evidence.
3. **CES-845 intentional absence and zero egress.** Source startup must support
   the dedicated app/DB-only runtime contract and independently fail closed on
   payment-provider egress. The exact runtime-profile variable, if any, must
   come from that reviewed source contract; do not invent it in Helm. Verify the
   dedicated SMS policy against the installed Cilium control plane and retain
   the exact policy receipt.
4. **CES-849 capacity.** Measurement-backed headroom and scheduling gates must
   be complete for the planned CronJob request and overlap model.
5. **Immutable artifact.** Merge the reviewed source in its authorized train,
   publish an immutable Citrus image from that exact source SHA, and capture
   registry evidence. Do not use a branch name or unbuilt commit as an image.
6. **Dedicated Secret provisioning.** An authorized secret operator creates a
   namespace-scoped Secret with the exact release-bound name and only the six
   documented keys. The value is never printed, copied into Git, or attached to
   a ticket.
7. **Suspended GitOps activation PR.** A new reviewed change records the exact
   image in `image.tag`, `verifiedImageTag`, and
   `commandCompatibleImageTags`, records the exact dedicated `secretName`, and
   supplies the Cilium receipt plus exact DB FQDN/port. It may set `enabled:
   true` only while the chart hardcodes `suspend: true`.
8. **Operator-controlled validation and unsuspend.** After an authorized Argo
   sync is Synced/Healthy, a synthetic provider-free run must prove bounded
   counts, exact policy selection, denied non-DB egress, and replay safety.
   Unsuspending requires a separate explicit approval and reviewed chart change;
   a values-only change cannot do it.

No later step may begin from an inferred or missing immutable input. A dormant
template may eventually merge independently, but it does not shorten this
activation order.

## Safe pre-merge validation

These commands render manifests and run local contracts only. They must not be
supplied credentials and must not be followed by `kubectl apply`, Argo sync, or
a manually created Job.

```bash
helm lint helm/citrus
helm template citrus helm/citrus \
  --namespace default \
  -f helm/citrus/values.yaml
helm template citrus-dev helm/citrus \
  --namespace citrus-dev \
  -f helm/citrus/values.yaml \
  -f helm/citrus/values-dev.yaml
uv run python -m unittest tests.test_citrus_sms_reconciliation
```

The actual renders must contain neither the SMS reconciliation CronJob nor its
dedicated policy. CI separately renders the template with the release-bound
synthetic Secret name and reserved database FQDN, then verifies the literal
suspended execution envelope, exact argv, six named key projections, dedicated
policy selector, and DNS-plus-database-only egress. Adversarial tests run both
with schema validation and with a schema-less chart copy so template guards
cannot silently depend on schema. Strict kubeconform skips only the Cilium CRD
after semantically parsing its exact structure. This render is not a controlled
application run.

## Future controlled-run evidence

Only after every prerequisite above and separate mutation authority, use
reserved synthetic database rows. The source command must report bounded:

- candidates;
- requeued attempts;
- abandoned sends fenced as `UNKNOWN`;
- unknown attempts requiring manual review;
- skipped attempts; and
- failures.

Any non-zero failure count must exit unsuccessfully so Kubernetes retains a
failed Job. Repeat the same run to prove terminal states do not regress and no
duplicate enqueue/provider action occurs. Record counts and safe synthetic
identifiers only—never phone numbers, message bodies, environment variables, or
Secret contents.

## Read-only post-rollout evidence

After a separately approved rollout, collect only read-only evidence:

```bash
argocd app get citrus-dev --refresh
kubectl -n citrus-dev get cronjob citrus-dev-sms-reconciliation -o wide
kubectl -n citrus-dev get ciliumnetworkpolicy \
  citrus-dev-sms-reconciliation-egress -o wide
kubectl -n citrus-dev get jobs \
  -l app.kubernetes.io/component=sms-reconciliation
kubectl -n citrus-dev get events \
  --field-selector involvedObject.kind=Job
```

Argo must report `Synced` and `Healthy` at the reviewed GitOps revision. Failed
runs remain visible through the three retained failed Jobs. Do not read Secret
objects or print container environments while collecting evidence.

## Rollback

Rollback is a two-step reviewed GitOps change:

1. Revert any separately reviewed chart change that allowed unsuspension so the
   manifest again hardcodes `suspend: true`, then verify Argo reconciliation.
   This prevents a new schedule while preserving CronJob and delivery state.
2. Set `smsReconciliation.enabled: false` and verify both the CronJob and its
   dedicated `CiliumNetworkPolicy` are absent from the desired render.

Do not delete `SmsDeliveryAttempt` rows, retained Jobs, or other delivery state.
Do not delete an active Job merely to hide a failure: the 240-second deadline
bounds it, and its final state is reconciliation evidence. Re-enable only via a
new reviewed GitOps change after the cause is understood.
