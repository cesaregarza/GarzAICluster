# Citrus SMS reconciliation scheduler

CES-848 prepares a bounded Kubernetes CronJob for
`sweep_manual_order_sms_attempts`. It does not authorize a deployment, a live
run, provider credentials, or outbound SMS. Production and dev both render
without the CronJob until a later GitOps change explicitly passes every gate.

## Fail-closed contract

The chart requires all three conditions before a CronJob can run:

1. `smsReconciliation.enabled: true` renders the resource. It is `false` in
   both real environment value sets.
2. `smsReconciliation.suspend: false` permits schedules. It is `true` in both
   real environment value sets and in every CES-848 synthetic render.
3. `smsReconciliation.verifiedImageTag` must exactly equal `image.tag` when the
   resource is rendered, and that tag must appear in the reviewed
   `commandCompatibleImageTags` allowlist. A missing, mismatched, or unreviewed
   receipt fails `helm template`.

The container also hard-sets `TWILIO_SMS_ENABLED=False` and an empty
`TWILIO_SMS_RECIPIENT_ALLOWLIST`. Explicit `env` entries override any values
with the same names inherited through `envFrom`. Consequently, the prepared
job cannot contact Twilio even if a referenced Secret is misconfigured. This
override is intentionally not configurable by values; changing it requires a
separate reviewed source change and separate provider approval.

No Stripe operation belongs to this command or runbook. Never provide Stripe
or Twilio credentials to a render, source test, Job, log command, or ticket.

## Image receipt as of 2026-08-25

| Environment shape | Immutable image tag | Command evidence | Real values |
| --- | --- | --- | --- |
| Production | `3f68967f777b2665fccb4f0ab423f339b8ea1357` | Does not contain `Apps/Juices/management/commands/sweep_manual_order_sms_attempts.py`; candidate `a728346e4364d88b8ec35d9fe10f4fe2e76dc2a9` is not its ancestor | disabled, suspended, empty receipt |
| Dev | `0e2258bf95c6170895c26780258eb42d5b5c557c` | Descends from `a728346e4364d88b8ec35d9fe10f4fe2e76dc2a9` and contains the command | disabled, suspended, exact receipt recorded |

Production therefore cannot render an enabled scheduler against its current
older image. CI exercises the production-shaped enabled path only by replacing
the image and receipt with the command-compatible dev tag; the result remains
suspended and is never applied.

For any future image bump, verify the exact source object before updating the
receipt. From a native-Linux Citrus checkout with the image tag available as a
Git object, the checks are read-only:

```bash
git merge-base --is-ancestor \
  a728346e4364d88b8ec35d9fe10f4fe2e76dc2a9 \
  <exact-image-tag>
git cat-file -e \
  <exact-image-tag>:Apps/Juices/management/commands/sweep_manual_order_sms_attempts.py
```

A registry tag name alone is not evidence. Keep the full immutable source SHA
in `image.tag`; add it to `commandCompatibleImageTags` and copy it to the
environment's `verifiedImageTag` only after both checks pass. Retain previously
reviewed tags so a rollback remains renderable, but never add an image that
lacks the command.

## Bounded behavior

The wake-up runs every 15 minutes in `Etc/UTC`, skips overlap with
`concurrencyPolicy: Forbid`, starts no more than 240 seconds late, and may run
for at most 240 seconds. It inspects attempts stale for 15 minutes with a hard
limit of 100. One Job retry is allowed. Two successful and three failed Jobs
are retained.

The source command reports only bounded operational counts:

- candidates;
- requeued attempts;
- abandoned sends fenced as `UNKNOWN`;
- unknown attempts requiring manual review;
- skipped attempts; and
- failures.

Any non-zero failure count exits unsuccessfully, so Kubernetes retains a failed
Job. Replaying the source command is safe: stale scheduled attempts go through
the existing idempotent enqueue boundary, an abandoned `SENDING` attempt is
fenced as `UNKNOWN` before any provider handoff, and existing `UNKNOWN`
attempts are reported for manual review. Source tests mock enqueue and delivery
at `Apps/Juices/test_manual_order_sms_sweep.py`; no provider is needed.

## Safe pre-merge validation

These commands render manifests and run mocked/local contract tests only. They
must not be supplied credentials and must not be followed by `kubectl apply`,
Argo sync, or a manually created Job under CES-848.

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

In a Citrus checkout at the exact verified source SHA, the corresponding
synthetic SQLite test is:

```bash
uv run python manage.py test Apps.Juices.test_manual_order_sms_sweep
```

The test suite uses reserved synthetic data and mocks the delivery boundaries.
Do not replace those mocks with a provider client.

## Activation gates for a future ticket

Do not enable or unsuspend this CronJob until all of the following are recorded
in the approving GitOps pull request:

- CES-849 CPU headroom validation is complete;
- separate go-live approval explicitly authorizes the target environment;
- the final immutable image contains the command and has an exact matching
  `verifiedImageTag` receipt in `commandCompatibleImageTags`;
- Helm lint, the prod/dev disabled renders, both suspended enabled renders, and
  strict kubeconform validation pass;
- synthetic database attempts demonstrate the bounded summary and replay-safe
  second run while the provider is mocked or hard-disabled; and
- the reviewer confirms no credential, allowlist, provider, Stripe, or customer
  data change is included.

Changing `enabled` alone only creates a suspended CronJob. Changing `suspend`
alone renders nothing. Removing the hard provider-disable is outside CES-848.

## Read-only post-rollout evidence

Only after a separately approved rollout, collect these read-only checks. Do
not print environment variables or Secret objects.

```bash
argocd app get citrus-dev --refresh
kubectl -n citrus-dev get cronjob citrus-dev-sms-reconciliation -o wide
kubectl -n citrus-dev get jobs \
  -l app.kubernetes.io/component=sms-reconciliation
kubectl -n citrus-dev get events \
  --field-selector involvedObject.kind=Job
```

Argo must report `Synced` and `Healthy`. The CronJob must show the approved
schedule and suspension state. A failed run is visible as a retained failed Job;
inspect only its count summary and safe attempt identifiers. Never copy customer
phone numbers, message bodies, environment variables, or credentials into logs
or tickets.

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
