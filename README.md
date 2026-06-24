# GarzAICluster

Kubernetes + Argo CD source of truth for SplatTop. Charts, AppSets, secrets workflow, and runbooks live here (not in the app repo).

## Quick links

- Bootstrap/runbooks: `docs/bootstrap.md`, `docs/argo-operations.md`, `docs/release-workflow.md`, `docs/cluster-identity.md`, `docs/secrets-strategy.md`, `docs/runbooks/postgres-restore.md`, `docs/developer-cheat-sheet.md`
- KSOPS deep dive and CMP recipe: `docs/ksops-llm-response.md`
- Argo objects: `argocd/` (AppProjects, Applications, AppSets)
- Charts/values: `helm/` and `apps/`
- Secrets layout: `secrets/` (bots) and `k8s/secrets.*`

## Repo map

- `argocd/` – production AppProject + Applications/AppSets; apply with `kubectl apply -f argocd/`.
- `apps/` – per-bot values/defs consumed by AppSets (e.g., `argocd/appsets/bots-*.yaml`).
- `helm/` – service charts and the umbrella chart; values files cover dev/default/prod overlays.
- `k8s/` – legacy/standalone manifests (ingress, cert, repo-server patches, secrets templates).
- `secrets/` – encrypted bot secrets (`secrets/bots/**`) with `kustomization.yaml` + `ksops.yaml` per bot.
- `docs/` – runbooks and design notes; start with `docs/README.md` for the reading order.
- `scripts/` – helpers like `scripts/validate_prometheus_config.py` (renders Helm, then promtool).

## KSOPS + secrets (Argo CD 3.2 quick recipe)

- Age key: create `argocd/sops-age-key` (`age.agekey` data). CI key is in GitHub Actions secret `SOPS_AGE_KEY`.
- Build flags: apply `k8s/argocd/argocd-cm-ksops-patch.yaml` so `argocd-cm.data.kustomize.buildOptions` includes `--enable-alpha-plugins --enable-exec` (Argo CD 3.2 ignores kustomize flags in `argocd-cmd-params-cm`).
- Repo-server: apply `k8s/argocd/repo-server-ksops-patch.yaml` to install ksops/sops, set `KUSTOMIZE_PLUGIN_HOME`, and mount the Age key.
- Bot secrets: `argocd/appsets/bots-secrets.yaml` renders `secrets/bots/<bot>/kustomization.yaml` + `ksops.yaml`; with the patches above Argo runs `kustomize build --enable-alpha-plugins --enable-exec` and decrypts `*.enc.yaml`.
- Want CMP/plugin-server instead of plain kustomize+KSOPS? See `docs/ksops-llm-response.md`.

## Working in this repo

- Make changes in a branch and run:
  - `helm lint helm/splattop`
  - `uv run python scripts/validate_prometheus_config.py`
- Apply changes to the cluster via Argo CD (prefer GitOps over UI edits).
- Keep secrets encrypted (`*.enc.yaml`); use `sops` with the Age key from CI or the cluster secret.

## Request a bump

Want your bot deployed? Use our one-click form:  
👉 **[Request a bump](https://github.com/cesaregarza/GarzAICluster/issues/new?template=bump-bot.yml)**
