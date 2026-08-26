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
```

There is no approved least-privilege runtime Secret and no compatible corrected
image. Empty values are intentional blockers, not placeholders to fill from an
existing broad Secret.

The existing dev image
`0e2258bf95c6170895c26780258eb42d5b5c557c` contains the command but is not an
activation artifact. Its disabled-mode behavior can consume stale scheduled
attempts, and its production startup validation requires Stripe, email, cache,
Celery, media, and AWS settings that this DB-only Job must not receive. Current
source `4353f11595094bc4893b5799233cfd56c52aed89` still has that startup contract.
No downstream corrective image exists yet; never invent or pre-pin one.

CI uses the existing dev tag only for an isolated, suspended manifest schema
render. The tag and synthetic Secret name are command-line test inputs and do
not appear in either environment value file. The render is never applied or
executed and is not evidence that the image can run least-privileged.

## Fixed command and bounded knobs

The executable and option names are hardcoded in the Helm template:

```text
python manage.py sweep_manual_order_sms_attempts \
  --stale-minutes <staleMinutes> \
  --limit <limit>
```

Values cannot replace `python`, `manage.py`, the management-command name, or
either option name. Only two integers are configurable:

| Value | Default | Schema range |
| --- | ---: | ---: |
| `staleMinutes` | 15 | 5–1440 |
| `limit` | 100 | 1–100 |

Helm schema validation rejects strings, values outside these ranges, and any
attempt to restore a free-form `command` value.

The schedule wakes every 15 minutes in `Etc/UTC`, skips overlap with
`concurrencyPolicy: Forbid`, starts no more than 240 seconds late, and runs for
at most 240 seconds. One Job retry is allowed. Two successful and three failed
Jobs are retained.

## Least-privilege Secret contract

When a future activation render sets `enabled: true`, `secretName` is required
and must name a dedicated provider-free Secret. It must not reuse the broad
application, email, Spaces, generated-key, payment, Stripe, or Twilio Secret.
The chart does not create this Secret and never renders Secret data.

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

## Security boundaries

The preparation makes separate, testable claims:

- **Workload identity:** the Pod receives no Kubernetes ServiceAccount token.
- **Runtime authority:** only six named app/DB Secret keys are projected; no
  provider authority is present.
- **Release binding:** an enabled render requires `verifiedImageTag` to equal
  the immutable `image.tag` and that tag to be present in the reviewed
  `commandCompatibleImageTags` list.
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
   come from that reviewed source contract; do not invent it in Helm.
4. **CES-849 capacity.** Measurement-backed headroom and scheduling gates must
   be complete for the planned CronJob request and overlap model.
5. **Immutable artifact.** Merge the reviewed source in its authorized train,
   publish an immutable Citrus image from that exact source SHA, and capture
   registry evidence. Do not use a branch name or unbuilt commit as an image.
6. **Dedicated Secret provisioning.** An authorized secret operator creates a
   namespace-scoped Secret containing only the six documented keys. The value
   is never printed, copied into Git, or attached to a ticket.
7. **Suspended GitOps activation PR.** A new reviewed change records the exact
   image in `image.tag`, `verifiedImageTag`, and
   `commandCompatibleImageTags`, records the dedicated `secretName`, and may
   set `enabled: true` only while `suspend: true`.
8. **Operator-controlled validation and unsuspend.** After an authorized Argo
   sync is Synced/Healthy, a synthetic provider-free run must prove bounded
   counts and replay safety. Unsuspending requires a separate explicit approval
   and GitOps review.

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

The actual renders must not contain an SMS reconciliation CronJob. CI separately
renders only the CronJob template with a synthetic Secret name, verifies the
hardcoded argv and six named key projections, rejects broad/provider Secret
references, and runs strict kubeconform. That suspended schema render is not a
controlled application run.

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
kubectl -n citrus-dev get jobs \
  -l app.kubernetes.io/component=sms-reconciliation
kubectl -n citrus-dev get events \
  --field-selector involvedObject.kind=Job
```

Argo must report `Synced` and `Healthy` at the reviewed GitOps revision. Failed
runs remain visible through the three retained failed Jobs. Do not read Secret
objects or print container environments while collecting evidence.

## Rollback

Rollback is a two-step GitOps value change:

1. Set `smsReconciliation.suspend: true` and verify Argo reconciliation. This
   prevents a new schedule while preserving CronJob and delivery state.
2. Set `smsReconciliation.enabled: false` and verify the CronJob is absent from
   the desired render.

Do not delete `SmsDeliveryAttempt` rows, retained Jobs, or other delivery state.
Do not delete an active Job merely to hide a failure: the 240-second deadline
bounds it, and its final state is reconciliation evidence. Re-enable only via a
new reviewed GitOps change after the cause is understood.
