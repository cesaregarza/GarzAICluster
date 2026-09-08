# ExternalDNS transition gate

This Argo-managed application remains automated (`argocd/applications/external-dns.yaml`), so a merge can reconcile the Deployment without a separate manual sync. Review the rendered diff and complete every gate below before merging this change.

The checked-in Deployment keeps the existing image `registry.k8s.io/external-dns/external-dns:v0.14.2`, sources, Cloudflare provider, `sync` policy, TXT registry, owner ID, TXT prefix, domains, interval, probes, resources, and scheduling. It removes the Stage A `--dry-run` pause and stops watching the legacy `nginx` class; the only ingress class retained is `traefik-nginx`.

## Required pre-merge evidence

1. Confirm all 13 canonical Ingresses have the Traefik class, no temporary canary Ingress remains, and every canonical status address is `129.212.154.58`.
2. Confirm the approved 15-record A-content move is complete and reviewed. The move is from `152.42.155.167` to `129.212.154.58`; retain the Citrus origin `143.244.222.41`. This repository change does not modify DNS records.
3. Repair and verify the missing `cegarza.com` TXT ownership record before enabling provider writes. Preserve the existing `_externaldns.` prefix, TXT owner ID `splattop-prod`, TTL, proxy state, and any unrelated TXT records.
4. Render `infra/external-dns` and review the exact Deployment argument delta. The only argument changes are removal of `--dry-run` and `--ingress-class=nginx`, with `--ingress-class=traefik-nginx` present. Do not approve a resource, image, credential reference, owner, domain, policy, or scheduling change.
5. Run the server dry-run plan against the intended cluster and record the result before merge. Because Argo has automated sync and prune enabled, do not merge while any preceding gate is pending.

After merge, verify one Ready ExternalDNS pod, the expected `external-dns` Deployment arguments, Cloudflare TXT ownership records, and the 15 approved A records. Stop and roll back the source revision if an unmanaged legacy-class record is changed or any unrelated DNS record appears in the plan.
