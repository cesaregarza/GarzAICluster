# ExternalDNS transition gate

This Argo-managed application has automated sync and prune enabled (`argocd/applications/external-dns.yaml`). Merging resumes provider writes, so complete the pre-merge gates first.

The Deployment removes `--dry-run` and `--ingress-class=nginx`, retaining `traefik-nginx`. It changes the TXT prefix to `_externaldns%{record_type}.` and preserves the image, sources, provider, sync policy, domains, owner, interval, credentials, probes, resources and scheduling.

## Apex ownership correction

The old prefix generates `_externaldns.a-cegarza.com` for the `cegarza.com` apex. That name is outside the zone; Cloudflare can append the zone and create an unrelated name. Do not attempt that single-record repair.

The new prefix generates `_externaldnsa.cegarza.com`, inside the zone. ExternalDNS v0.14.2 removes the template when reading older records, preserving the `_externaldns.` fallback and recognition of all 23 existing ownership records. Its planner requires newly generated names to exist, so prepare 12 replacement ownership TXT records for the 12 currently owned A records. Preserve all 23 existing TXT identities and contents. The four currently unowned A records remain unowned. The Hermes callback A record keeps its address; only matching ownership metadata gains a replacement record.

Reference: [pinned ExternalDNS TXT registry implementation](https://github.com/kubernetes-sigs/external-dns/blob/v0.14.2/registry/txt.go).

## Required pre-merge evidence

1. Confirm all 13 canonical Ingresses have the Traefik class, no temporary canary Ingress remains, and every canonical status address is `129.212.154.58`.
2. Confirm the approved 15-record A-content move is complete and reviewed before merging. The move is from `152.42.155.167` to `129.212.154.58`; retain the Citrus origin `143.244.222.41`. Merging this change resumes provider writes, so the move must already be complete.
3. Verify all 12 replacement ownership TXT records have exact names inside their zones and matching source owner/resource contents and TTLs. Verify all 23 existing ownership records remain intact.
4. Review the rendered Deployment and Kubernetes admission server dry-run. Its only argument changes are the two removals and TXT prefix replacement described above.
5. Observe the actual controller plan using the new prefix in dry-run mode. Require two fresh observations at least 60 seconds apart with zero proposed DNS changes and no controller errors. Kubernetes admission dry-run validates the Deployment; it does not validate the DNS reconciliation plan.
6. Require all hosted checks to pass on the reviewed source revision before merge.

Because Argo has automated sync and prune enabled, do not merge while any preceding gate is pending.

After reconciliation, verify one Ready ExternalDNS pod, the expected arguments, all 35 ownership records, the 15 moved A records and the unchanged Hermes callback A record. If unexpected changes begin, pause the running controller immediately; a Git revert alone does not stop an already-running pod. Existing TXT records support restoring the old prefix with writes paused.
