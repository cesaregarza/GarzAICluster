# Citrus payment egress safety

## Current status

The chart defaults and production Application remain disabled. The development
overlay now activates CES-845 deny mode for the exact `citrus-dev` release and
namespace at policy revision `ces-845-dev-v1`. Its checked-in Argo render keeps
the healthy rollback image pinned while adding the runtime attestation and two
same-release `CiliumNetworkPolicy` resources at sync wave `-1`.

The `citrus-dev` Application tracks `main` with automated prune and self-heal.
Merging this activation therefore authorizes and can immediately trigger Argo
reconciliation; review and merge are the deployment gate. A later source-image
pin remains a separate change. Never use a Stripe request to validate this
policy.

## Boundary

When `paymentSafety.enabled=true`, the chart injects the same attestation into
every Citrus image container in the web Deployment, media worker, billing
worker and metrics sidecar, migration Job, and all media and recurring
CronJobs:

- `DJANGO_ENV`
- `CITRUS_ENVIRONMENT_OWNER`
- `PAYMENT_NETWORK_MODE`
- `PAYMENT_EGRESS_POLICY_REQUIRED`
- `PAYMENT_EGRESS_POLICY_PROVIDER`
- `PAYMENT_EGRESS_POLICY_REVISION`

The chart accepts only these named runtime tuples:

| Environment | Release | Namespace | Owner | Network mode |
| --- | --- | --- | --- | --- |
| `development` | `citrus-dev` | `citrus-dev` | `citrus-dev` | `deny` |
| `production` | `citrus` | `default` | `citrus` | `allow` |

Both require `PAYMENT_EGRESS_POLICY_REQUIRED=true`, provider `cilium`, a safe
nonempty policy revision, and same-release `CiliumNetworkPolicy` resources. The
policies are applied at sync wave `-1`, before the ConfigMap, migration Job, and
serving workloads.

The boundary deliberately uses two selectors over labels that already exist
before activation. One selects the web, media-worker, and billing-worker
Deployment Pods by their stable `app` values. The other selects migration,
media, and recurring batch Pods by release and an explicit component list.
This closes the activation window before old Pods or old CronJob templates are
replaced. Render tests require every current and enabled Citrus image workload
to match at least one selector and require Redis to match neither.

The dedicated CES-844 development credential overlay is already active and
renders independently of this policy. Activating payment safety does not change
its Secret names, key references, rollout revision, or credential contents.
The web process receives `STRIPE_WEBHOOK_SECRET_OWNER`, and the chart binds the
dev webhook variable to `citrus-dev-payment-credentials` owned by `citrus-dev`.
The separately prepared production overlay remains disabled. Generic webhook
projection is not an allowed managed-environment contract. This is value-free
provenance metadata; it does not classify credential contents, so the CES-844
operator classification receipt remains a separate gate.

Environment-variable attestation alone does not prove that the live Cilium
policy exists or is enforcing. It proves only the image/chart contract. Live
acceptance must separately verify the applied policy object, selected Cilium
endpoints, policy revision, and a provider-free negative network probe.

The `splattop` AppProject already permits namespaced
`cilium.io/CiliumNetworkPolicy` resources. No cluster-scoped CRD or AppProject
permission change is required. A read-only check on 2026-08-25 found the live
`ciliumnetworkpolicies.cilium.io/v2` CRD and Cilium agent `v1.17.15`.

### Development deny mode

Selected Citrus Pods enter Cilium's default-deny egress posture and receive
only these allow rules:

1. TCP and UDP DNS to the configured kube-dns Pod selector.
2. TCP Redis to the same release and namespace.
3. TCP to one exact, separately supplied database hostname and port.
4. HTTPS to the chart-derived Spaces endpoint, virtual-hosted bucket endpoint,
   and CDN hostname.
5. SMTP only when the configured Django backend is SMTP.
6. Separately reviewed exact FQDN/port entries for enabled optional features.

Wildcards, raw IP addresses, malformed names, and every exact or subdomain
destination under `stripe.com` or `stripe.network` fail Helm rendering. The
policy has no ingress section, so synthetic inbound webhook requests remain
possible through the existing ingress path.

### Production allow mode

Production must explicitly select owner `citrus` and network mode `allow`. Its
same-release Cilium policy renders `toEntities: [all]`, preserving existing
production connectivity while making the production exception visible and
revision-bound. This preparation does not activate that mode.

## Development activation inventory

Cilium FQDN rules are allow rules. Cilium deny rules cannot selectively deny
`toFQDNs`, so it is not possible to add a narrow `stripe.com` deny while safely
leaving every other dynamic destination open. The independent boundary must be
an allowlist that deliberately omits Stripe.

The chart cannot read the secret-backed `DB_HOST`, so this activation records
the exact database FQDN from operator-authorized environment evidence in the
reviewed development values. The remaining allowlist is derived from checked-in
configuration: same-release Redis and the three exact Spaces hostnames. Dev uses
the dummy email backend, while SMS, recurring runtime, Cloudflare Access, and
other optional provider integrations remain disabled. Accordingly,
`additionalExternalEgress` is empty.

Any later feature that needs another destination must add one exact reviewed
FQDN and port before it is enabled. A failing optional workflow is not
permission to weaken the deny boundary.

The `additionalExternalEgress` list is intentionally explicit. Each item needs
an audit name, one exact lowercase hostname, and one or more TCP ports. Do not
add a broad wildcard, IP range, generic proxy, or Stripe destination to make a
failing workflow pass.

## Required release order

Never deploy a source image that requires this contract before the environment
has been prepared. PR #609 demonstrated that reversing the order makes the new
image fail during settings import; PR #610 restored the healthy rollback image.

1. With the rollback image still selected, review and merge the development
   deny-mode attestation and both wave `-1` Cilium policies. Because Argo is
   automated, that merge is the deployment authorization and can reconcile
   immediately. This step changes neither the image nor the CES-844 credential
   projection.
2. Wait for Argo to report the exact merged GitOps revision as Synced and
   Healthy. Prove both policies select the existing Citrus endpoints, exclude
   Redis, report the reviewed policy revision, and reject a local fake
   destination omitted from the allowlist. Stop on any mismatch. Do not
   manually sync or probe Stripe.
3. Only after the activation receipt, pin the exact reviewed CES-845 source
   image in a separate GitOps change and verify startup checks across web,
   workers, Jobs, and CronJobs.
4. Rerun the CES-846 zero-network acceptance campaign on the final source and
   GitOps pair before continuing the release train.

Production is a separate train. Its explicit allow-mode attestation and policy
must reconcile before the CES-845 image is promoted there, and current health
must be verified at each step. Development activation is not a production
activation receipt.

## Prepared render checks

These commands render the exact development activation and the synthetic
production contract. Helm rendering does not resolve or contact any named
destination:

```bash
helm lint helm/citrus

helm template citrus-dev helm/citrus \
  --namespace citrus-dev \
  -f helm/citrus/values.yaml \
  -f helm/citrus/values-dev.yaml \
  -f helm/citrus/values-payment-dev.yaml

helm template citrus helm/citrus \
  --namespace default \
  -f helm/citrus/values.yaml \
  --set paymentSafety.enabled=true \
  --set-string paymentSafety.environment=production \
  --set-string paymentSafety.owner=citrus \
  --set-string paymentSafety.networkMode=allow \
  --set paymentSafety.policy.required=true \
  --set-string paymentSafety.policy.provider=cilium \
  --set-string paymentSafety.policy.revision=ces-845-review \
  --set paymentSafety.networkPolicy.enabled=true
```

Before merging the activation, render the exact proposed environment values and
verify all of the following:

- The two Cilium policy selectors cover both pre-activation and proposed Citrus
  Pod templates and neither selector matches the Redis Pod.
- The database, storage, email, and optional exact destinations are complete.
- No rendered `toFQDNs` entry ends in `.stripe.com` or `.stripe.network`.
- The source image implements the matching runtime guard contract.
- The rollback image remains frozen for activation; the guarded source image is
  pinned only after the live policy receipt.

After separately authorized activation, prove enforcement without contacting
Stripe: inspect Cilium policy status and use a local fake endpoint/FQDN omitted
from the allowlist as the negative network probe. Keep inbound webhook tests
synthetic. A missing or unverifiable Cilium policy is a failed deployment gate,
not a reason to switch development to `allow`.

## Rollback

Do not remove the Cilium boundary while payment credentials remain available to
development. Prefer correcting an omitted non-Stripe dependency through a
reviewed exact allow rule. If the activation must be abandoned, remove the dev
payment credential projection or scale the affected payment-capable workloads
down before reverting the source and policy activation together. Record the
source image, GitOps revision, policy revision, and credential-absence evidence.
