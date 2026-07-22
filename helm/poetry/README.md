# Poetry chart rollout stages

`values.yaml` enables the public DNS-only TLS stage: the runtime, signed
Cloudflare Access origin validation, and Ingress are enabled while Cloudflare
proxying remains disabled. `values-ci.yaml` exercises the same public Ingress
with isolated Access identifiers in CI and must not be added to the Argo CD
Application.

During this stage, unauthenticated `/admin/` requests are rejected by the Django
origin with `403` even when sent directly to the public load balancer. Do not
create staff or superuser accounts until both this raw-origin denial and the
later Cloudflare edge challenge have been proven.

Activate in reviewed stages:

1. Add the SOPS-encrypted database, Django, media, and registry Secrets. A root
   sync wave orders the `poetry-secrets` Application at `-10` and `poetry` at
   `+10`, but those waves order Application reconciliation only. They do not
   prove the child secrets Application has finished. Verify `poetry-secrets` is
   `Synced` and `Healthy` before enabling the chart.
2. Pin a published immutable image, set `enabled: true`, keep ingress disabled,
   and verify the Service plus `/healthz` and `/readyz` from inside the cluster.
   While Access is disabled the chart explicitly sets
   `CLOUDFLARE_ACCESS_REQUIRED=false` for this ingress-free staging phase.
3. Configure Cloudflare Access for `/admin` and `/admin/*`, then enable
   `application.cloudflareAccess` with the application's exact team domain and
   audience tag while keeping `ingress.enabled: false`. The validator checks the
   Access-signed RS256 JWT, issuer, audience, and exact `cesar@cegarza.com`
   identity; the normal Wagtail login remains the second authentication step.
   The ConfigMap checksum makes sure this GitOps revision restarts the Deployment
   with validation active. Prove the new pod is Ready and an in-cluster request
   to `/admin/` without a signed assertion receives `403` before continuing.
4. In a separate reviewed revision, set `ingress.enabled: true` with
   `cloudflareProxied: "false"`. Prove that a raw load-balancer request with
   `Host: poetry.cegarza.com` still receives `403` for `/admin/`, then complete
   nginx routing and the initial Let's Encrypt HTTP-01 issuance.
5. Switch Cloudflare proxying on. Confirm public routes remain unauthenticated,
   `/admin/` challenges at the edge, an authenticated request reaches Wagtail,
   and a direct origin request is still denied.

The chart refuses to render a proxied Ingress unless origin JWT validation is
enabled in that same revision. Enabling validation also derives
`CLOUDFLARE_ACCESS_REQUIRED=true`; the pod will then fail startup rather than
run a production admin origin without the issuer and audience.

Do not use `nginx.ingress.kubernetes.io/whitelist-source-range` for this control
unless the shared ingress controller is separately changed and verified to
preserve Cloudflare source IPs. The current controller does not provide that
guarantee. Signed Access JWT validation is independent of source-IP handling and
does not interfere with HTTP-01 certificate renewal.
