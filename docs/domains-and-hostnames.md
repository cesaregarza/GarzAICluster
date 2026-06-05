# Domains and Hostnames

Use this checklist when adding a new public hostname or rolling out another DNS zone/TLD.

## external-dns scope

- `external-dns` only manages zones that are explicitly listed in `infra/external-dns/deployment.yaml`.
- Add one `--domain-filter=<zone>` entry per public zone/TLD that this cluster should manage.
- Keep the filter list tight. Do not remove the filters entirely unless you intentionally want this cluster to manage every zone available to the Cloudflare token.
- Make sure the Cloudflare token in `infra/external-dns/cloudflare-api-token.enc.yaml` can edit every added zone.
- Current managed zones are `splat.top` and `garz.ai`.

## App ingress and TLS

- Add the hostname to the app's `ingress.hosts` list.
- For charts with explicit `Certificate` resources, `ingress.tls.certificate.dnsNames` now falls back to the ingress hostnames when left empty. Set `dnsNames` only when you need a custom override.
- For redirect-only hostnames, prefer `apps/vanity-hosts/values.yaml` instead of making the target app serve multiple canonical domains.

## App-specific follow-up

- `helm/splattop-blog/values-prod.yaml`: update `blog.health.hostHeader`, `blog.env.ALLOWED_HOSTS`, and `blog.env.CSRF_TRUSTED_ORIGINS` when the new hostname is canonical.
- `helm/splattop/values-prod.yaml`: update `monitoring.grafana.serverDomain` and `monitoring.grafana.serverRootUrl` only if the new Grafana host becomes canonical. If it should just redirect, use vanity hosts.
- `helm/garz-ai`: serves FastAPI-backed garz.ai pages, including JustFlip/Habit Taper legal pages at `justflip.garz.ai` and `habit-taper.garz.ai`.
- Citrus (new): DNS is outside Cloudflare for this cluster, so update app ingress and application settings only (do not change `external-dns` filters) and provision the following records manually:
  - `citrus-grace.com` A 143.244.222.41
  - `www.citrus-grace.com` A 143.244.222.41
  - `dev.citrus-grace.com` A 143.244.222.41

## Rollout checks

- Sync the `external-dns` Argo app after changing domain filters.
- Confirm the new hostname resolves to the nginx ingress load balancer before expecting ACME issuance to succeed.
- Run chart lint for every chart you touched before opening a PR.
