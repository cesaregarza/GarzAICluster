# Citrus purchasing broker: deployment support and activation prerequisites

`helm/agent-workloads` can render an isolated Citrus purchasing broker through
`citrusPurchasingBroker`. It is **disabled by default** and absent from the
production overlay. Merging this support creates no runtime resources. The
fixture in `tests/fixtures/citrus-purchasing-broker-enabled.yaml` uses synthetic
TLS names, an example domain and a TEST-NET address; it is not deployable config.

When enabled, it adds one Deployment, ClusterIP Service, ServiceAccount and
NetworkPolicy. Its name is the release name (truncated to 49 characters) plus
`-citrus-broker`. An enabled worker cannot use that name. It is a provider server,
not a polling worker: no projected identity, API token, Role, RoleBinding,
Ingress, worker environment or worker runtime Secret is inherited. Registry
pull-secret names are the only shared chart input.

## Required broker inputs

All input belongs to `citrusPurchasingBroker`; unknown keys are rejected.

| Input | Contract |
| --- | --- |
| `enabled` | Defaults to false; incomplete configuration fails when true. |
| `image.repository` | Fixed to `registry.digitalocean.com/sendouq/agent-workloads-citrus-purchasing-broker`. |
| `image.digest` | Required immutable `sha256:` digest, never a mutable tag. |
| `serverTlsSecretName` | Existing Secret with `tls.crt` and `tls.key`; server certificate must cover the internal Service hostname. |
| `clientCaSecretName` | Existing Secret with `ca.crt`, the trust root for Core's client certificate. |
| `purchasingTokenSecretName` | Existing Secret with `token`, containing only the narrow Citrus purchasing-read credential. |
| `apiCaSecretName` | Optional existing Secret with `ca.crt` for Citrus API trust; otherwise image system roots apply. |
| `clientUriSan` | Exact URI identity from Core's client certificate; broker requires that sole URI SAN. |
| `apiOrigin` | Fixed HTTPS origin, no credentials/path/query/fragment, port 443 only. |
| `rolloutRevision` | Required non-secret revision label; change when rotating secrets or reviewing changed provider addresses. |
| `network.core` | Explicit `namespace`, `appName`, `releaseName`; peer also requires component `api`. |
| `network.dns` | Explicit `namespace` and nonempty `podLabels`. |
| `network.citrusIpv4Addresses` | 1–16 unique IPv4 `/32` host CIDRs, selected by the operator for the fixed API origin. |

The pod runs as UID/GID 65532, with a read-only root, no capabilities or privilege
escalation, RuntimeDefault seccomp and no automatic ServiceAccount token. Only
named Secret keys are mounted read-only at mode 0440; fsGroup 65532 makes them
readable. `/tmp` is a 16 MiB emptyDir. Fixed resources request 50m CPU/64Mi memory
and limit 500m/256Mi. No arbitrary environment, command, volume, ServiceAccount
or security override is exposed. Secrets must not be included in chart values.

Mounts are `/var/run/citrus/server-tls`, `client-ca`, `purchasing-token`, and
optional `api-ca`. The templates supply the exact `CITRUS_PURCHASING_*` file,
origin and client-SAN environment contract from the published broker. It listens
on TLS port 8443. TCP startup/readiness probes test the listener only; they do
not prove authorization or provider health. There is no plaintext health bypass.

The mandatory NetworkPolicy selects only this broker and combines namespace
and pod selectors within each peer. Ingress permits Core's API pods on TCP8443;
egress permits selected DNS pods on UDP/TCP53 and explicit Citrus host addresses
on TCP443. There is no policy disable switch or broad CIDR fallback. IPv6,
non-443 providers and service-selector egress require a reviewed chart extension.
NetworkPolicy is additive: activation must check other namespace/CNI policies do
not broaden access. Confirm CNI enforcement and actual service/NAT routing;
a Kubernetes render cannot prove these properties. DNS resolution is pinned by
the broker process at startup. Review DNS/IP changes, update the host allowlist
and bump `rolloutRevision`; restart after TLS/CA/token rotation as appropriate.

mTLS authenticates Core; Core's lease and capability policy authorize broker
operations; the Citrus token authorizes provider reads. Network selectors are
an additional boundary, not authentication. A compromised broker can read its
mounted narrow token. Namespace administrators and a compromised authorized
Core remain trusted by this deployment design. Image/source receipts bind a
published artifact, not a claim about live runtime state.

## Published inputs for the first activation

These source artifacts exist; none of these pins is activated by this change.

| Component | Source | Published image digest |
| --- | --- | --- |
| Core | `69bb119a22d5df71912f92fbd4261ff6a930abbd` | `sha256:99f707b1206adc4d0c86b8aac39ccfb3112ede51386e86a78dec641b6367f267` |
| Shopping worker | `1f77449d0e9310db1b74b06e17be507336793487` | `sha256:cdced6a87554fa32e284d9b5e7f2a2bbf30938b2d378bdd22a3594eb2c3c918e` |
| Purchasing broker | `1f77449d0e9310db1b74b06e17be507336793487` | `sha256:216e824e52a2e4e3c8ea09f6ddf6d24633947d3dc143dcdabe61e9b116f3ef59` |

Publication evidence: [Core](https://github.com/cesaregarza/agent-platform/actions/runs/36369933531),
[worker](https://github.com/cesaregarza/agent-workloads/actions/runs/36369937184),
[broker](https://github.com/cesaregarza/agent-workloads/actions/runs/36369940096).
The broker uses the separate `citrus-purchasing-broker.json` receipt, not worker
registry artifacts. The fixture records its published digest.

## Core runtime prerequisite (CES-1048)

The Core Application now selects the published `69bb119a22d5df71912f92fbd4261ff6a930abbd`
chart and image tuple above. This installs the broker-operation implementation
when the operator explicitly syncs Core; it does not configure a provider or
activate a Citrus broker, worker, credential or grant. Existing worker release
tuples and policy remain unchanged. All five Core Deployments share this image.

Before proceeding with activation, sync the reviewed Core revision and verify
migrations, all five deployed image digests/readiness, existing worker claims,
callback/output delivery and zero identity/provider digest mismatches. The old
source was `cdf4a4388aa28ac0a8595efa5ed243765c1deca5`, tag `sha-cdf4a4388aa2`, digest
`sha256:f3b947b5f9b29bc5349c9f852cfff19891651334d91d44345d5e1f7cd53da065`.
Keep that complete tuple as the rollback reference and verify database/schema
compatibility before rollback. A merged pin is not evidence of a live rollout.

## Later activation sequence

1. Select the actual Citrus API environment and verify the purchasing API is
   available there. Dev support does not prove production promotion. Issue the
   narrow purchasing-read token, server TLS and a dedicated Core client
   certificate with the exact sole URI SAN. Install Secrets in the appropriate
   namespaces, never in worker mounts or plaintext Git values. Review actual
   Core labels, DNS labels and Citrus IPs against the enforcing CNI.
2. Prepare a reviewed enabled broker overlay with the receipt digest. Prepare
   Core's compatible chart revision and image digest together: its current
   external Argo chart pin must not be assumed to contain the provider support.
   Core's chart supports `env`, `extraVolumes` and `extraVolumeMounts`. Mount
   broker server-CA trust and the dedicated client cert/key read-only into Core;
   do not mount the Citrus token there. Set
   `AGENT_PLATFORM_BROKER_OPERATION_PROVIDERS_JSON` to a descriptor array with
   `broker_id: citrus_purchasing`, `endpoint`, `ca_file`, `client_cert_file`,
   `client_key_file`. For release/namespace `agent-workloads`, endpoint is
   `https://agent-workloads-citrus-broker.agent-workloads.svc:8443/v1/execute`.
   Ensure server certificate DNS SAN and trust match this exact endpoint.
   Recompute any provider digest pin from this complete reviewed configuration;
   do not reuse an old digest after changing endpoint or certificate inputs.
3. Prepare the initial shopping worker registry/manifest import, release tuple
   and projected ServiceAccount from the published worker artifact. This is a
   new identity, not an OpenCode identity rotation; do not use the existing
   OpenCode-only activation helper. The proposed subject is
   `system:serviceaccount:agent-workloads:agent-workloads-citrus-shopping-list-17e988b0dcbf95d47f6a`.
   Verify the artifact's manifest, code, image and bundle digests together.
4. Establish the chosen Hermes principal/channel and only its shopping grant.
   Add the named policy prerequisite `citrus-shopping-job-runtime-120`:
   `defaults.max_runtime_seconds_per_capability.agent_workloads.citrus_shopping_list=120`.
   Keep the broker operation bound and timeout at 20 seconds, one operation,
   zero provider cost, and requester-channel output release. Worker declaration
   alone does not provide a 120-second lease. Preserve unrelated grants.
5. Review the complete rollout and rollback tuple before requesting deployment.
   The Core and workload Argo apps require explicit sync. Deploy broker and
   compatible Core substrate first, verify mTLS/provider readiness, then enable
   the coherent worker registry/identity/grant tuple. Confirm wrong client SAN,
   absent credentials, ungranted principal, and direct worker-to-Citrus access
   remain denied. On failure, disable the new capability/worker and restore the
   previously reviewed pins before retrying; do not leave partial authority.
6. Run one real ingredient-shopping request with an explicit date window.
   Verify stored job lease 120 seconds, broker bound 20 seconds, successful
   purchasing response and output release only to the intended principal/channel.
   TCP readiness is insufficient. Only then expose the workflow for daily use.

Receipt processing remains a subsequent capability. This change neither issues
credentials nor grants authority, syncs Argo, runs a live canary or deploys images.
While disabled, reverting this chart addition has no runtime rollback work.

## Verification

Python chart tests exercise valid/invalid configurations, mandatory credentials
and network inputs, exact peer selectors, resources, mounts, image pinning,
name collisions and unchanged existing workers. CI renders the enabled fixture
alongside production values for strict kubeconform validation. Default and
production renders are also compared to the parent revision during delivery.
No local Hermes tests or resource-intensive Hermes checks are part of this gate.
