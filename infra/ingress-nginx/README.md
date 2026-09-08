# Citrus source-IP load balancer

This directory contains the operator-only CES-748 blue/green payload. The
spare DigitalOcean `REGIONAL_NETWORK` load balancer at
`129.212.154.58` is verified against the GAIC Traefik pod labels and named
ports. DigitalOcean network load balancers preserve the client source address
natively, so neither NGINX nor the existing `REGIONAL` load balancer needs a
risky one-sided PROXY-protocol transition.

The payload is deliberately not referenced by Argo CD, Helm, or Kustomize.
Merging it is inert. It does not modify the current Service/load balancer,
Ingresses, controller ConfigMap or Deployment, DNS, or the Citrus payment flag.
Applying it and changing DNS are separate production operations that require
explicit approval.

## GAIC Stage A source contract

Stage A records the already verified spare mapping and pauses external-dns
writes while both `nginx` and `traefik-nginx` sources are observed. It does not
change application Ingress classes, Traefik status publication, or the
production Services at `143.244.222.41` and `152.42.155.167`.

The 143 Citrus Service and IP remain the rollback and DNS floor. Its existing
Service may later move to the verified Traefik selector while retaining the
same IP, ports, annotations, and `externalTrafficPolicy: Local`; Google DNS
provider access is needed only to move Citrus A records. Keep the
`ingress-nginx` namespace and every PVC permanently. Cleanup of either old
controller is a separate reviewed step after public DNS and rollback
observation.

Stage order for a later operator activation:

1. Confirm the spare Service has endpoints on two Ready Traefik pods and pass
   client-IP, forged-`X-Forwarded-For`, TLS, redirect, and backend checks.
2. Keep external-dns in `--dry-run` with both ingress class filters until all
   15 non-Citrus hosts across 11 Ingresses have moved through the verified
   Service selector and class gate.
3. Move the 143 selector only in the guarded Citrus selector/class window;
   retain 143 throughout the Google DNS handoff and TTL-plus-observation
   period.
4. Enable status publication and remove external-dns `--dry-run` only after
   the status address and intentional DNS plan are reviewed.
5. Prune only named, verified controller resources after rollback proof;
   never prune `ingress-nginx`, application namespaces, PVCs, or serving TLS
   Secrets.

## Legacy admission webhook class isolation

The v1.0.0 `ingress-nginx-admission` webhook historically checks host/path
overlap before applying its own class filter. That makes a same-host Traefik
canary fail when it overlaps a legacy Ingress. The checked-in webhook source
adds one Kubernetes 1.30+ `matchConditions` CEL expression to
`validate.nginx.ingress.kubernetes.io`. The expression skips only an Ingress
whose `spec.ingressClassName` is `traefik-nginx` and whose deprecated
`kubernetes.io/ingress.class` annotation is absent or also `traefik-nginx`.
Legacy `legacy-nginx`, `nginx`, no-class, and conflicting-class inputs still
reach the webhook. `failurePolicy: Fail`, the existing rules, service client,
selectors, and any existing match conditions remain unchanged.

Kubernetes documents that all match conditions must evaluate true for a
webhook call, that `matchConditions` is stable since 1.30, and that an
evaluation error with `failurePolicy: Fail` rejects the request. The expression
therefore checks class-field and annotation-map presence before reading optional fields or using the CEL `in` operator.

The source patch is inert until explicitly applied. Review it with the helper's
default server dry-run, without printing webhook CA material:

```bash
python3 scripts/patch_legacy_ingress_webhook.py \
  --context do-nyc3-k8s-nyc3-garz-ai \
  --kubectl /root/dev/.tools/kubectl-v1.33.12
```

If that dry-run and the route review pass, an operator may create the private
pre-mutation receipt and apply the UID/resourceVersion-guarded patch:

```bash
python3 scripts/patch_legacy_ingress_webhook.py \
  --apply \
  --receipt /root/dev/gaic-optimization-2026-09-08/legacy-webhook-receipt.json
```

The helper changes only `matchConditions`; it never reads Secrets or DNS and
never prints `clientConfig.caBundle`. If a post-apply verification fails, use
the same receipt for a guarded rollback server dry-run, followed by explicit
`--apply` only after reviewing that rollback:

```bash
python3 scripts/patch_legacy_ingress_webhook.py \
  --rollback \
  --receipt /root/dev/gaic-optimization-2026-09-08/legacy-webhook-receipt.json
python3 scripts/patch_legacy_ingress_webhook.py \
  --rollback --apply \
  --receipt /root/dev/gaic-optimization-2026-09-08/legacy-webhook-receipt.json
```

After applying, the operator must server-dry-run all 13 existing canary
Ingresses with their original `spec.ingressClassName: traefik-nginx` and no
class annotation. Separately server-dry-run a private duplicate host/path
fixture with the legacy class; the legacy webhook must still reject that
fixture. Do not add a deprecated annotation to the canaries as an experiment:
the match condition intentionally protects the clean `spec`-class contract.

DigitalOcean currently labels `REGIONAL_NETWORK` as public preview and does not
support IPv6 on it. This is not a current Citrus regression: the old load
balancer has an IPv4-only network stack and all three Citrus hosts have A
records but no AAAA records. Re-check those facts immediately before applying.

## Activation gates

1. Confirm the cluster is still DOKS 1.33.1-do.0 or later and the old route is
   healthy at `143.244.222.41`.
2. Confirm only `citrus-grace.com`, `www.citrus-grace.com`, and
   `dev.citrus-grace.com` use ingress class `legacy-nginx`.
3. Confirm each hostname has an A record and no AAAA record. Stop if IPv6 has
   become part of the production contract.
4. Scale the existing legacy controller to two ready replicas on distinct
   nodes before using it as the sole backend for the new route:

   ```bash
   kubectl -n ingress-nginx scale deployment ingress-nginx-controller --replicas=2
   kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller
   kubectl -n ingress-nginx get pod \
     -l app.kubernetes.io/instance=ingress-nginx,app.kubernetes.io/component=controller \
     -o custom-columns=NAME:.metadata.name,NODE:.spec.nodeName,READY:.status.containerStatuses[0].ready
   ```

   Require two `READY=true` rows on different nodes. This legacy Deployment is
   not Argo-owned; record the live replica count before changing it.
5. Confirm both Citrus Certificates are Ready and at least seven days before
   renewal. Stop rather than overlapping this migration with ACME renewal.
6. Record the DOKS worker firewall. DigitalOcean documents that creating a
   network load balancer adds inbound `0.0.0.0/0` access to kube-proxy health
   port 10256. After creation, verify that this is the only new public worker
   rule and that no ingress NodePort became public.

## Create without cutting over

Apply the checked-in payload:

```bash
kubectl apply -f infra/ingress-nginx/citrus-source-ip-load-balancer.yaml
```

This adds a PDB and a second Service. It does not alter the old Service named
`ingress-nginx-controller`. Wait for the new Service to receive an external IP
and for `doctl` to report a healthy `REGIONAL_NETWORK` load balancer named
`citrus-source-ip`.

Re-read the DOKS worker firewall and require the provider-created public rule
to be limited to TCP port 10256. Stop if any workload or NodePort is exposed.

The Service exposes ports 80 and 443 and uses named target ports `http` and
`https`, which resolve to the Traefik container ports 8000 and 8443. Keep
`externalTrafficPolicy: Local`; its health check sends traffic only to nodes
with a ready Traefik pod.

## Prove the new route before DNS

For each Citrus hostname, use `curl --resolve` to send HTTPS directly to the
new external IP. Require a valid existing certificate and HTTP 200 from prod
and dev.

From a client with a known public IPv4 address:

1. Make a uniquely identifiable production request through the new IP.
2. Confirm the legacy ingress access log records that public address exactly.
3. Confirm the live application path using `get_client_ip` reports the same
   address.
4. Repeat with a forged `X-Forwarded-For` header and confirm the forged value
   changes neither result.
5. Confirm representative hosts on the unrelated `nginx` controller still
   return 200.

Do not change DNS unless every check passes. `DIRECT_ORDER_PAYMENT_SETUP_ENABLED`
remains disabled until CES-748's live production identity gates have passed.

## DNS cutover and observation

Change the A records for all three Citrus hosts from `143.244.222.41` to the new
network load balancer IP. Keep the old Service and load balancer intact. Verify
public resolvers, access logs, prod/dev HTTP 200, the known-IP check, and the
forged-header check throughout the observation window.

Do not delete or disown the old load balancer in CES-748. Its continued health
is the rollback control. Removal is a separately reviewed cleanup after the
new route has remained healthy.

## Rollback

Restore the three DNS A records to `143.244.222.41`. Verify public resolution,
prod/dev HTTP 200, and the old ingress access log. The old load balancer never
changed, so rollback does not have a PROXY-protocol convergence window.

Only after DNS is confirmed back on the old route may an operator remove the
new resources:

```bash
kubectl delete -f infra/ingress-nginx/citrus-source-ip-load-balancer.yaml
```

Deletion destroys the new DigitalOcean load balancer. Enumerate its Service,
public IP, and `doctl` load-balancer ID before deletion and obtain explicit
cleanup approval. Restore the controller's recorded pre-cutover replica count
only after the rollback route is stable. Confirm DigitalOcean also removed the
network load balancer's public port-10256 health-check firewall rule.

## Provider references

- [DigitalOcean DOKS load-balancer configuration](https://docs.digitalocean.com/products/kubernetes/how-to/configure-load-balancers/)
- [DigitalOcean load-balancer setting ownership](https://docs.digitalocean.com/support/why-do-my-doks-load-balancer-settings-keep-reverting/)
