# Poetry chart rollout stages

`values.yaml` is intentionally inert: both `enabled` and `ingress.enabled` are
false. `values-ci.yaml` exists only to exercise every template in CI and must not
be added to the Argo CD Application.

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
3. Set `ingress.enabled: true` with `cloudflareProxied: "false"`. Complete nginx
   routing and the initial Let's Encrypt HTTP-01 issuance before proxying the
   hostname.
4. Configure and verify Cloudflare Access for `/admin` and `/admin/*`. Copy the
   application's exact team domain and audience tag into
   `application.cloudflareAccess`, enable the origin validator, and prove that
   a raw load-balancer request with `Host: poetry.cegarza.com` receives `403` for
   `/admin/`. The validator checks the Access-signed RS256 JWT, issuer, audience,
   and exact `cesar@cegarza.com` identity; the normal Wagtail login remains the
   second authentication step. The ConfigMap checksum in the pod template makes
   sure this same GitOps revision restarts the Deployment with validation active.
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
