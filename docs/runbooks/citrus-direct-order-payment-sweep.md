# Citrus direct-order payment sweep rollout

This runbook is the activation boundary for CES-807. The default chart values
leave the CronJob absent: `directOrderPaymentSweep.enabled=false`, the runtime
Secret name and image receipt are empty, and production and development render
exactly as they did before this chart support existed. In particular, the
shared ConfigMap does not gain `DIRECT_ORDER_OFF_SESSION_MODE`, so merging the
disabled slice does not trigger an application rollout.

## Materialization contract

An enabled CronJob is safe even if an operator or API client changes
`spec.suspend` outside Helm. Helm refuses to materialize the object unless the
render already contains all of these controls:

- a lowercase 40-hex immutable image tag and an exact matching
  `directOrderPaymentSweep.verifiedImageTag` receipt;
- the CES-844 environment-owned payment Secret projected as the single
  non-optional `STRIPE_SECRET_KEY` setting;
- the CES-845 Cilium payment boundary and its runtime attestation;
- a separate provider-free runtime Secret projected by exact, non-optional
  references to `DJANGO_SECRET_KEY`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`,
  `DB_HOST`, and `DB_PORT`;
- a job-scoped `DIRECT_ORDER_OFF_SESSION_MODE=legacy` entry that overrides any
  same-named ConfigMap value;
- the hardcoded management command, bounded execution policy, non-root
  security context, and disabled Kubernetes service-account token.

The job imports only the application ConfigMap with `envFrom`. It never imports
the broad application, email, Spaces, generated-key, or payment Secrets with
`envFrom`. The production baseline image
`3f68967f777b2665fccb4f0ab423f339b8ea1357` is explicitly rejected because it
predates `sweep_direct_order_payment_attempts`.

## Activation evidence

Do not materialize or unsuspend the CronJob until the target environment has a
reviewed receipt containing:

1. The exact source SHA, image tag, and registry evidence for an image whose
   source contains `sweep_direct_order_payment_attempts` and can boot the
   management command from only the projected ConfigMap and named Secret keys.
2. The dedicated runtime Secret's name and key-name inventory. Never record or
   print its values.
3. The classified CES-844 payment Secret owner and CES-845 policy revision.
   Development must remain unable to reach Stripe and must never receive live
   or restricted-live credentials.
4. The exact GitOps revision and target Argo Application.
5. `directOrderPaymentSweep.offSessionMode=legacy`. Shadow, allowlist, and on
   remain separate, later activation decisions.

## Ordered rollout

1. Pin and reconcile the verified immutable image while the sweep remains
   disabled.
2. Establish the dedicated runtime Secret and the reviewed payment
   credential/policy resources without exposing their values.
3. In one GitOps change, set `enabled=true`, keep `suspend=true`, and provide
   the runtime Secret name and exact verified image tag. Review the rendered
   CronJob, Cilium selector, annotations, environment projection, command,
   schedule, namespace, and immutable image together.
4. Verify Argo is Synced and Healthy at that exact GitOps revision and confirm
   no Job exists. Treat a direct API unsuspend as drift even though the
   materialized Pod template is already fail-closed.
5. In a separately reviewed GitOps change, set `suspend=false` while keeping
   every image, credential, policy, Secret, and legacy-mode receipt unchanged.
6. Verify one scheduled Job completes on the expected image. Record only a
   sanitized sweep summary; omit customer, order, provider, and credential
   data.
7. Only after live evidence exists may another change consider a non-legacy
   application mode. The sweep itself remains forced to legacy until that
   contract is deliberately revised.

## Phased rollback

1. Set `suspend=true` first. Preserve `enabled=true`, the immutable image and
   receipt, both Secret references, the Cilium policy, payment attestation, and
   job-scoped legacy mode.
2. Reconcile and verify the exact rollback revision. Suspending a CronJob does
   not terminate Jobs that already started, so enumerate active Jobs and let
   them finish or use the separately authorized supported termination path.
3. After the CronJob is suspended and no active Job remains, set
   `enabled=false` and reconcile the CronJob removal.
4. Remove the dedicated runtime Secret only after the CronJob and all owned
   Jobs are absent. Remove shared payment credentials or Cilium boundaries
   last, and only after proving no remaining Citrus workload consumes them.

Rollback does not reverse database state already reconciled by a completed
sweep. Keep application off-session mode at `legacy` throughout.
