# Citrus payment egress safety

## Current status

The CES-845 Helm contract is disabled by default. Neither
`argocd/applications/citrus.yaml` nor `argocd/applications/citrus-dev.yaml`
enables it. Merging the reusable chart slice therefore renders no
`CiliumNetworkPolicy`, adds no runtime environment variables or Pod labels, and
does not change either live environment.

Activation, Argo synchronization, credential changes, and provider testing are
separate operator actions. Never use a Stripe request to validate this policy.

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

The chart accepts only these named runtime pairs:

| Environment | Owner | Network mode |
| --- | --- | --- |
| `development` | `citrus-dev` | `deny` |
| `production` | `citrus` | `allow` |

Both require `PAYMENT_EGRESS_POLICY_REQUIRED=true`, provider `cilium`, a safe
nonempty policy revision, and a same-release `CiliumNetworkPolicy`. The policy
is applied at sync wave `-1`, before the ConfigMap, migration Job, and serving
workloads.

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

Wildcards, malformed names, and every exact or subdomain destination under
`stripe.com` or `stripe.network` fail Helm rendering. The policy has no ingress
section, so synthetic inbound webhook requests remain possible through the
existing ingress path.

### Production allow mode

Production must explicitly select owner `citrus` and network mode `allow`. Its
same-release Cilium policy renders `toEntities: [all]`, preserving existing
production connectivity while making the production exception visible and
revision-bound. This preparation does not activate that mode.

## Why development activation is still blocked

Cilium FQDN rules are allow rules. Cilium deny rules cannot selectively deny
`toFQDNs`, so it is not possible to add a narrow `stripe.com` deny while safely
leaving every other dynamic destination open. The independent boundary must be
an allowlist that deliberately omits Stripe.

The chart can derive Redis, object storage, and configured SMTP destinations,
but it cannot read the secret-backed `DB_HOST`, and it cannot infer whether
secret-backed or feature-gated CAPTCHA, Twilio, ntfy, or receipt-source URL
integrations are enabled. Enabling the policy without that inventory could
break database access, account registration, notifications, media processing,
or scheduled work. Do not activate it until an operator supplies the exact
database FQDN and accounts for every enabled non-Stripe dependency.

The `additionalExternalEgress` list is intentionally explicit. Each item needs
an audit name, one exact lowercase hostname, and one or more TCP ports. Do not
add a broad wildcard, IP range, generic proxy, or Stripe destination to make a
failing workflow pass.

## Prepared render checks

Use synthetic hostnames only during local review. These commands render YAML;
they do not contact any named destination:

```bash
helm lint helm/citrus

helm template citrus-dev helm/citrus \
  --namespace citrus-dev \
  -f helm/citrus/values.yaml \
  -f helm/citrus/values-dev.yaml \
  --set paymentSafety.enabled=true \
  --set-string paymentSafety.environment=development \
  --set-string paymentSafety.owner=citrus-dev \
  --set-string paymentSafety.networkMode=deny \
  --set paymentSafety.policy.required=true \
  --set-string paymentSafety.policy.provider=cilium \
  --set-string paymentSafety.policy.revision=ces-845-review \
  --set paymentSafety.networkPolicy.enabled=true \
  --set-string paymentSafety.networkPolicy.database.host=db.dev.example

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

Before any later activation, render the exact proposed environment values and
verify all of the following:

- The Cilium policy selects every Citrus Pod template and does not select the
  Redis Pod.
- The database, storage, email, and optional exact destinations are complete.
- No rendered `toFQDNs` entry ends in `.stripe.com` or `.stripe.network`.
- The source image implements the matching runtime guard contract.
- The source image and policy revision are immutable and reviewed together.

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
