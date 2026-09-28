# Governed Citrus-dev shopping canary

The candidate connects shared Mandate Core to **Citrus dev** and grants
`agent_workloads.citrus_shopping_list` only to the `citrus-shopping-private-assistant`
binding: user `94265880216612864` in **#assistant** (`1546428227411513488`),
guild `1523242748822425750`. The older `private-admin-controlled-capabilities`
binding points to **#general** (`1523242750043226234`) and does not grant shopping.

The result is a gross ingredient shopping list for an explicit,
inclusive date window of at most 31 days; stock is not subtracted. Missing
recipes and excluded demand remain visible as warnings. Only `output_text`
passes the existing `text_result_v1` release projection.

The application source remains on Citrus `dev`. Core, worker and GitOps
changes use their repositories' `main` branches. This does not promote Citrus
production, create wife-specific access, or enable receipt processing.

## Immutable inputs

| Component | Source | Published image digest |
| --- | --- | --- |
| Core/chart | `f98a5ca247344249f9b6e2461342eccc787c7041` | `sha256:f710ce7108867739e653c8f06f554117c20bff8c15bd3432f2de94b570a2d898` |
| Shopping worker | `1f77449d0e9310db1b74b06e17be507336793487` | `sha256:cdced6a87554fa32e284d9b5e7f2a2bbf30938b2d378bdd22a3594eb2c3c918e` |
| Purchasing broker | `1f77449d0e9310db1b74b06e17be507336793487` | `sha256:216e824e52a2e4e3c8ea09f6ddf6d24633947d3dc143dcdabe61e9b116f3ef59` |

Published evidence: [Core](https://github.com/cesaregarza/agent-platform/actions/runs/36394613770),
[worker](https://github.com/cesaregarza/agent-workloads/actions/runs/36369937184),
[broker](https://github.com/cesaregarza/agent-workloads/actions/runs/36369940096).
The broker has its own image receipt and is not a worker registry import.
The generated shopping manifest, values tuple and projected subject are applied
transactionally from the single-worker release artifact. There is no previous
shopping release, HMAC mint or overlap.

## Authority and credential boundaries

- The shopping worker has a Kubernetes projected identity, no provider secrets,
  and network access only to DNS and Core. It receives one leased
  `purchasing_requirements` operation, with no model or SQL ability.
- The capability's policy gives the whole job **120 seconds**. Its independent
  broker bound remains **20 seconds**, with one operation, zero provider cost,
  bounded request/response bytes and external influence on returned data.
- Only Core's API receives `apiEnv` provider configuration and the dedicated
  `apiExtraVolumes` client identity. The other Core processes retain their
  existing provider pins and cannot mount this client key through these values.
- The broker owns the purchasing-read token. Its fixed origin is
  `https://dev.citrus-grace.com`; there is no caller URL, redirect or proxy
  override. The credential has no receipt create/read/legacy scope.
- Broker ingress requires both the Core namespace and API pod labels on TCP8443.
  Egress allows selected DNS pods and `143.244.222.41/32` on TCP443. Verify the
  origin's IPv4 addresses again before activation. Cilium is enforcing policy;
  the current inventory has no additional policy selecting the new broker or
  worker. Still prove actual denied connections after deployment.
- mTLS validates the server chain and hostname and authenticates the Core client
  by its sole URI SAN, `spiffe://mandate.garz.ai/core/citrus-dev-purchasing`.
  Namespace administrators and a compromised authorized Core remain trusted.
  NetworkPolicy is additive and is not a replacement for authentication.

The worker/broker pods run as UID/GID65532 with read-only roots, no ambient
ServiceAccount token, no privilege escalation and no Linux capabilities.
The worker has only its explicit projected token. The broker has no Kubernetes
Role/RoleBinding. Broker secret mounts are read-only mode0440; its resources
are bounded and `/tmp` is a 16MiB emptyDir.

Dedicated SOPS files are referenced by the two namespaces' KSOPS generators.
No pre-existing secret is rotated. The Core secret holds server-CA trust and
client certificate/key; the broker's separate secrets hold server certificate/key,
client-CA trust and the token. The purchasing token is never mounted into Core
or the worker. The credential and TLS material were issued for 90 days on
2026-09-28; certificates expire on 2026-12-27. Rotate before expiry. Issuer keys
were discarded, so rotation creates a new complete trust bundle rather than
renewing from a retained CA key.

## Public identity verification

`apps/agent-control-plane/broker-public-certificates.yaml` maps the configured
provider IDs to committed public server-CA and Core-client certificate files.
The provider-pin gate rejects missing/extra/duplicate mappings, unknown fields,
absolute or traversing paths, symlinks, non-certificate PEM material and malformed
base64. It invokes `broker_operation_fingerprint` from the exact chart-pinned
Core checkout, preserving the original endpoint and public bytes. It needs no
private key, TLS context or cluster DNS. Public-file validation checks encoding;
it does not establish X.509 trust or live readiness.

The pin binds transport source, protocol, broker ID, endpoint, CA and client
certificate. It does not attest the remote broker image; that image is pinned
separately. The API override is checked at its actual `apiEnv` location, while
local-worker and model-gateway pins are computed from their own environments.
Runtime construction still performs all normal endpoint/TLS/key/DNS checks.

Issuance verified key pairs, signatures and public-input equality before
SOPS encryption. Local decryption was unavailable on the preparation host.
Before activating the provider, verify that the installed Core CA/client
certificate hashes match the committed public files without printing keys or
plaintext credential values.

## Reviewed rollout order

Merging configuration is not the shopping canary. Core, registry and worker
Applications use manual sync. The Core secrets Application auto-syncs; the
workload secrets Application requires explicit sync. Use the exact reviewed
GitOps merge SHA, recheck project sync windows and current operations, and
record dry-run and apply receipts. Never alter sync-window policy.

The existing train orders registry, Core, then workers. A new broker Service
must exist **before** Core constructs its DNS-pinned provider, so bootstrap
only the broker resources before running that train:

1. Confirm exact GitOps `main`, successful post-merge CI and all published image
   digests. Confirm Citrus dev is healthy at the purchasing API source revision,
   that its purchasing migration is applied, and that its HTTPS/DNS address
   still matches the broker allowlist. Keep the existing Core/worker baseline.
2. Wait for the Core secrets app to sync at that revision; dry-run and sync the
   workload secrets app at the same revision. Confirm all four new Secret names
   and required keys, and public certificate hashes, using bounded metadata/hash
   checks. Do not print private keys or tokens.
3. Dry-run a resource-selected Argo sync of only the new broker's Deployment,
   Service, ServiceAccount and NetworkPolicy in `agent-workloads`, all named
   `agent-workloads-citrus-broker`. Verify the selected diff contains exactly
   those four resources, then apply and wait for the broker listener and ready
   Service endpoints. This explicit bootstrap has no hooks; it must not sync the
   shopping worker or registry. The workload Application remains OutOfSync
   until the final full reconciliation.
4. Use `scripts/mandate_scoped_deploy.py` with the exact merge SHA and applications
   `agent-control-plane-registry-overlay`, `agent-control-plane`,
   `agent-workloads`, in that canonical order. Run its dry-run before `--apply`.
   This keeps overlay hooks and worker reconciliation in one invocation.
   The old Core has no Citrus provider until its image/config sync; readiness
   denies shopping while it is unavailable. The new projected subject and
   worker tuple must agree when the train finishes. Require all five Core
   Deployments at the new image, migrations successful, all selected apps
   Synced/Healthy, and the existing governed verification journeys passing.
5. Run one real shopping request with explicit dates from the selected private
   admin context. Inspect the stored 120-second job lease, 20-second broker
   bound, consumed operation budget, external influence, output-gate pass,
   released result and callback to that same requester channel. Do not expose
   raw provider data as a shortcut. Empty demand is a valid result, but must be
   identified as empty rather than a populated shopping plan.
6. Check an ungranted principal is denied; wrong client certificate identity is
   denied; the shopping worker cannot connect directly to Citrus or the broker;
   and existing worker claims show no new identity/provider mismatches.
   TCP readiness alone does not satisfy this acceptance.

The user's approval covers this scoped Citrus-dev canary after critic merge.
No broader principal, environment, receipt-write or production-promotion scope
is authorized by this rollout.

## Correcting the initial channel selection

The first authentic request reached Core on 2026-09-28 but received
`admission.forbidden` before job creation. The account and guild were correct;
the request came from #assistant while the initial shopping grant reused the
older #general binding. Channel names are explanatory; the exact IDs above
remain the authorization selectors. A private channel does not automatically
inherit another channel's grant.

For this policy-only correction, reconcile `agent-control-plane-registry-overlay`
and `agent-workloads` together with the scoped deploy helper at the reviewed
merge SHA, after its dry run. The overlay hooks refresh Core's loaded registry.
The broker, certificates, credential, image pins and runtime budgets are already
installed and do not need reissuance or another bootstrap. Repeat a **fresh**
authenticated request from #assistant; do not replay the denied event. The new
binding grants only shopping, has no admins or approval overrides, and leaves
other capabilities on their existing surfaces.

## Failure and rollback

Stop before the next phase when any gate fails. Before the main train, the
broker alone conveys no user-facing capability; do not add a grant to work
around a failure. During or after the train, disable the new shopping grant
and worker through a reviewed GitOps rollback and reconcile the overlay plus
workers together. Revoke the named Citrus-dev credential
`mandate-citrus-dev-purchasing-20260928` if abandoning this connection. Delete
bootstrap resources only after confirming no active shopping job depends on
them and removing the provider from Core.

The previous Core source/chart is
`69bb119a22d5df71912f92fbd4261ff6a930abbd`, image
`sha256:99f707b1206adc4d0c86b8aac39ccfb3112ede51386e86a78dec641b6367f267`,
at GitOps baseline `958c4a0c20ca26cf027d508f6ed31daeff5ba49e`.
If reverting Core, restore its complete source/chart/image/provider-env tuple;
never retain the new certificate-bound pin with old transport source. Existing
worker release tuples, retained overlaps and HMAC ciphertext are unchanged.

Receipt processing, a dedicated wife channel and production Citrus access
remain later work.
