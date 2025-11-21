# Argo Operations Handbook

This guide covers day-to-day management of Argo CD once it tracks the config repo.

## AppProjects & RBAC

- Production is the only environment managed from this repo today. The manifest lives at `argocd/projects/splattop-project.yaml`.
- The project pins `sourceRepos` to `https://github.com/cesaregarza/SplatTopConfig` and limits destinations to the `default` (app) and `monitoring` namespaces on the in-cluster API server.
- Resource whitelists mirror the previous settings so Helm can continue to manage monitoring/cluster objects required by prod.
- A weekday sync window (Mon–Fri, cron `0 15 * * 1-5`, `duration: 11h`) blocks off-hours deploys.
- Only the `splattop-admins` group is bound (role `proj:splattop:admin`). Set `policy.default: role:readonly` in `argocd-rbac-cm` so casual logins stay read-only.
- Apply project changes via GitOps (`kubectl apply -f argocd/projects`) rather than editing the object in the UI.

## Sync Policies

| Environment | Sync Policy | Notes |
| ----------- | ----------- | ----- |
| prod        | manual sync only | `ApplyOutOfSyncOnly=true`, `RespectIgnoreDifferences=true`, namespace autocreation for monitoring objects, and the weekday sync window above. |

All of the details are codified inside `argocd/applications/splattop-prod.yaml`; update that manifest rather than flipping settings in the UI.

## Repository & Registry Credentials

1. **Config repo**
   - Create a read-only deploy key dedicated to Argo (`argocd-repo-splattopconfig` secret in the `argocd` namespace).
   - Reference it from `argocd-cm.repositories` so no developer PATs are needed inside the control plane.
2. **Container registry**
   - Mirror the existing DOCR `regcred` into the `argocd` namespace for metadata lookups, and keep per-namespace pull secrets for workloads.
   - Document the `kubectl create secret docker-registry ...` command used plus the rotation owner/date.
3. **Helm repos / OCI charts (if used)**
   - Capture auth + mirror strategy in this repo before onboarding any external chart.

Keep renewal dates in `developer-cheat-sheet.md` or a shared calendar.

## Argo UI Exposure

- `argo.splat.top` is the public entry point for the Argo CD UI/API. The DNS record already maps to the nginx ingress load balancer; keep it updated if the LB IP changes.
- TLS is provisioned by cert-manager via `k8s/argocd/certificate.yaml` (secret `argo-splat-top-tls`, issuer `letsencrypt-prod`). Reapply it after issuer/cluster moves.
- The ingress at `k8s/argocd/ingress.yaml` fronts `svc/argocd-server` with HTTPS pass-through. Apply this manifest whenever the controller name or annotations need to change.
- Once the ingress is reachable, patch `argocd-cm` with `data.url: https://argo.splat.top` so CLI logins and links point at the new hostname.

## Policy Enforcement

- Kyverno/Gatekeeper policies (post-cutover):
  - Deny mutable image tags.
  - Require CPU/memory requests & limits.
  - Optionally require cosign signatures once signing is enabled.
- Config repo CI runs `conftest test` mirroring these policies; Argo admission enforces live state.

## Monitoring & Alerts

- Enable Argo metrics service (`argocd-metrics`).
- Alert on:
  - Sync failures > 10 minutes.
  - Applications OutOfSync for prod namespaces.
  - Failed auto-syncs (expose via Prometheus rule).
- Capture alert runbooks (who responds, expected actions) in this file.

## Game Day / Drills

- Quarterly exercise:
  1. Deploy change via normal workflow.
  2. Introduce controlled failure (e.g., bad config).
  3. Detect via alerts.
  4. Roll back using config repo revert.
  5. Document findings + update docs/tests.

Record outcomes (date, scenario, owner) at the bottom of this file for traceability.

## In-Cluster Secret Decryption (SOPS/ksops)

- Ensure the Age private key is present as `sops-age-key` in the `argocd` namespace (`age.agekey` key).
- Argo CD 3.2 only honors kustomize flags from `argocd-cm.data.kustomize.buildOptions` (or per-app build options), so apply `k8s/argocd/argocd-cm-ksops-patch.yaml` to inject `--enable-alpha-plugins --enable-exec`; `argocd-cmd-params-cm` does nothing for kustomize flags.
- Patch repo-server to install ksops + sops and mount the key:  
  `kubectl patch deploy argocd-repo-server -n argocd --type strategic --patch-file k8s/argocd/repo-server-ksops-patch.yaml`
- The `bots-secrets` ApplicationSet simply renders `kustomization.yaml` + `ksops.yaml`; it relies on the global build options above rather than setting `enableAlphaPlugins` per app.
- If you need the CMP/plugin-server variant instead of plain kustomize+KSOPS, the full recipe lives in `docs/ksops-llm-response.md`.
- Rotate keys by updating the `sops-age-key` secret and reapplying the patch (or rolling repo-server) to ensure the new key is mounted.

## Bot Read-Only DB Access

When a Discord bot maintainer needs database reads without touching the FastAPI service:

1. **Encrypt their Discord token** via the helper script:

   ```bash
   uv run python scripts/onboard_bot_secret.py <bot-name> "<discord-token>"
   ```

   This writes `secrets/bots/<bot-name>/token.enc.yaml`, which the `bots-secrets` ApplicationSet syncs automatically.
   Store the token in `.env` as `BOT_TOKEN` to avoid passing it on the command line.

2. **Provision their schema + secret** with the helper script (it connects via `psql`, creates the schema/role, and optionally writes the Kubernetes Secret manifest):

   ```bash
   BOT_DB_ADMIN_URL="postgresql://admin:***@private-db:25060/xscraper?sslmode=require" \
     uv run python scripts/provision_bot_db.py <bot-name>
   ```

   - `BOT_DB_ADMIN_URL` (or `--admin-url`) must point at a superuser/owner account inside the cluster’s Postgres instance.
   - The script constrains the new role to its own schema (`bot_<bot-name>`) and prints the generated connection string for auditing.
   - Secrets auto-encrypt via SOPS when available, and Argo decrypts them in-cluster via ksops (Age key mounted in repo-server).
   - Scripts automatically load secrets from `.env` (or the path in `SPLATTOPCONFIG_ENV_FILE`) before falling back to interactive prompts, so keeping `BOT_DB_ADMIN_URL` there works without exporting it every time.

3. **Flip the network permission** inside `apps/bots/<bot>.yaml` so the `bot-netpol` chart opens only the needed egress:

   ```yaml
   permissions:
     postgres: false
     prometheus: false
     dbReadOnlyVPC: true
   ```

After the PR merges, Argo CD renders the new encrypted secrets (via the `splattop-bot-*-secret` ApplicationSet) and deploys the updated sandbox NetworkPolicy (via `splattop-bot-*-netpol`). Developers simply mount `bot-token` / `bot-db-readonly` inside their chart and can read from the DigitalOcean VPC-scoped Postgres instance.
