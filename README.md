# GarzAICluster

Kubernetes + Argo CD source of truth for SplatTop. Charts, AppSets, secrets workflow, and runbooks live here (not in the app repo).

`cesaregarza/GarzAICluster` is the canonical GitOps repository. Do not recreate `cesaregarza/SplatTopConfig` as a separate repository; legacy links should resolve here.

## Quick links

- Bootstrap/runbooks: `docs/bootstrap.md`, `docs/argo-operations.md`, `docs/release-workflow.md`, `docs/cluster-identity.md`, `docs/secrets-strategy.md`, `docs/runbooks/prometheus-historical-query-safety.md`, `docs/runbooks/postgres-restore.md`, `docs/runbooks/citrus-spaces-cors.md`, `docs/runbooks/citrus-payment-secret-isolation.md`, `docs/runbooks/citrus-recurring-runtime-preflight.md`, `infra/ingress-nginx/README.md`, `docs/developer-cheat-sheet.md`
- KSOPS deep dive and CMP recipe: `docs/ksops-llm-response.md`
- Argo objects: `argocd/` (AppProjects, Applications, AppSets)
- Charts/values: `helm/` and `apps/`
- Secrets layout: `secrets/` (bots) and `k8s/secrets.*`

## Repo map

- `argocd/` – production AppProject + Applications/AppSets; apply with `kubectl apply -f argocd/`.
- `apps/` – per-bot values/defs consumed by AppSets (e.g., `argocd/appsets/bots-*.yaml`).
- `helm/` – service charts and the umbrella chart; values files cover dev/default/prod overlays.
- `k8s/` – legacy/standalone manifests (ingress, cert, repo-server patches, secrets templates).
- `infra/spaces/` – checked-in Spaces bucket configuration payloads that must be applied by operators.
- `infra/ingress-nginx/` – operator-only Citrus source-IP load-balancer payloads that are not reconciled by Argo CD.
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

The grant ownership generator reads literal constants from the explicit
agent-workloads checkout. It supports the original release-applier file and the
split `release_applier_common.py` layout without importing workload code. Missing,
ambiguous, or nonliteral contracts fail instead of using the committed snapshot.

## Add a Mandate worker

Declare the worker under `workers.<worker-id>` in
`apps/agent-workloads/values.yaml`, with its image, projected identity, runtime
environment, resources and network rules. Add its immutable release tuple under
`mandateReleasePins.<worker-id>`. The chart renders every entry through the same
templates; the identity gate and enablement planner read these same keys.
Adding a worker does not require editing a Python worker-name map.

The worker's `identity.workerId` must equal its map key. Its current and optional
previous projected ServiceAccounts are derived from the corresponding immutable
release tuples. Retained HMAC rollback material is separate: declare
`identity.hmacRollbackTokenKey` and `identity.hmacRollbackRelease` together only
when retaining an existing credential. A new projected worker needs neither.
These fields never mount a credential or enable HMAC authentication.

This is the deployment declaration, not authorization to run a new capability.
The descriptor, handler, image and reviewed registry import still establish the
workload contract; existing grants govern dispatch. Use the normal reviewed
release artifacts and scoped operator reconcile for deployment.

The CES-965 release-tooling and GitOps PRs must both be merged before a new
worker publication is dispatched. This chart migration preserves deployed image
pins and rollback tuples and requires no new image publication.

To review a chart migration against an immutable baseline, run:

```bash
uv run python scripts/check_worker_chart_migration.py --baseline-ref <reviewed-commit>
```

The proof renders each revision's own chart and production values, rejects missing
or changed resources, and permits only removal of the three unused broker env
variables. Its JSON receipt includes the resolved baseline commit and resource
hashes; it preserves field types and list order.

## Request a bump

Want your bot deployed? Use our one-click form:  
👉 **[Request a bump](https://github.com/cesaregarza/GarzAICluster/issues/new?template=bump-bot.yml)**
