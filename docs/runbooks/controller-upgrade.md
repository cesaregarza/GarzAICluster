# cert-manager and Argo CD controller upgrades

This runbook is for the GarzAICluster DOKS cluster (`do-nyc3-k8s-nyc3-garz-ai`, Kubernetes `v1.33.12-do.0`). It is an offline, staged plan. The current installations are static upstream manifests: neither controller has a Helm release or an Argo Application owner. Do not let a later Argo reconciliation become the owner until the corresponding GitOps manifest is deliberately adopted.

The pinned source and digest contract is [ops/controller-upgrade-manifests.json](../../ops/controller-upgrade-manifests.json). It was checked against the official cert-manager GitHub release assets and Argo CD release manifests on 2026-09-07. Download into a private temporary directory and verify with `scripts/plan_controller_upgrade.py`; do not commit the downloaded payloads.

## Pinned route

cert-manager must advance one minor at a time, using the latest patch in each minor. Apply the **full** `cert-manager.yaml` at each stage; it already contains the six CRDs. The standalone `cert-manager.crds.yaml` is retained in the lock as an independently verified reference and must not be applied a second time.

`v1.7.1 -> v1.7.3 -> v1.8.2 -> v1.9.2 -> v1.10.2 -> v1.11.5 -> v1.12.17 -> v1.13.6 -> v1.14.7 -> v1.15.5 -> v1.16.5 -> v1.17.4 -> v1.18.6 -> v1.19.6 -> v1.20.3 -> v1.21.1`.

The exact cert-manager URLs are `https://github.com/cert-manager/cert-manager/releases/download/{tag}/cert-manager.yaml` and `.../cert-manager.crds.yaml`; the corresponding SHA256 values are in the lock file. The exact Argo CD route is `v3.2.0 -> v3.2.12 -> v3.3.14 -> v3.4.8 -> v3.5.2`, with each payload at `https://raw.githubusercontent.com/argoproj/argo-cd/{tag}/manifests/install.yaml` and its SHA256 in the same lock.

The first v1.7.3 server dry-run is recorded in [ops/v1.7.3-cert-manager-dryrun-report.md](../../ops/v1.7.3-cert-manager-dryrun-report.md). It found existing `kubectl-client-side-apply` ownership on version labels, RoleBinding subjects, controller image/POD_NAMESPACE fields, and webhook rules. The dry-run used no force and caused no mutation; those fields require explicit review before the first real apply.

Argo `v3.5.2` is the selected endpoint because it is the latest patched release verified from the official release API and its tested Kubernetes range includes Kubernetes 1.33. Each Argo stage is composed with the repository KSOPS patches before it is considered applyable.

## Prepare and validate payloads

Use `/root/dev/.tools/kubectl-v1.33.12` for cluster commands. Downloading is an operator action; the validator itself never accesses the network or the cluster:

```sh
set -eu
payload_dir=/secure/path/controller-payloads
out_dir=/secure/path/controller-plan
python3 scripts/plan_controller_upgrade.py \
  --manifest-dir "$payload_dir" --output-dir "$out_dir" \
  --baseline ops/live-controller-baseline.json --mode all
```

The command fails closed on a missing or changed SHA, a missing cert-manager CRD, a non-pinned controller image, or a missing KSOPS invariant. It writes a JSON plan and one composed Argo manifest per stage. Review the generated manifests and the field-level diff before every apply.

Before cert-manager stage 1.18.6, preserve the old private-key behavior explicitly. The target manifests do not expose a supported `--default-private-key-rotation-policy` controller flag, so use `privateKey.rotationPolicy: Never` in every repository-owned `Certificate` template and in a reviewed patch for each existing live `Certificate` whose spec omits it. The external Mandate chart's Certificate is now owned by `apps/agent-control-plane-runtime-controls/certificate.yaml`; `apps/agent-control-plane/values.yaml` disables the duplicate Helm Certificate while keeping the same resource identity and existing spec fields. The repository templates to inspect are the eight `helm/*/templates/certificate.yaml` files and the runtime-controls Certificate; Argo's `argo.splat.top` Certificate is ingress-shim-owned by `k8s/argocd/ingress.yaml`, and the former duplicate `k8s/argocd/certificate.yaml` is retired. Render and review the resulting Certificate-only diff. Do not change key algorithms, delete serving Secrets, or mass reissue. Keep the explicit setting through v1.21.1, then consider `Always` only as a separate, approved migration.

Capture a resource-only preflight (names, versions, readiness, image, replicas, scheduling and resource fields) and preserve the current one-replica/scheduling/resource shape. Upstream defaults have no permission to consume the remaining cluster CPU budget. Do not include Secret data in terminal output or ordinary backups.

The reviewed non-secret baseline is `ops/live-controller-baseline.json`; passing it to the validator rejects a removed live argument, security context, replica, resource, or scheduling setting and reports changed fields for review. The encrypted backup set captured for this operation is `/root/dev/gaic-optimization-2026-09-08/secure-controller-backups-20260908T002416Z`; keep it access-controlled and do not decrypt it into logs.

## Apply cert-manager, one stage at a time

For each stage in the route, after the validator and field-level diff pass:

```sh
/root/dev/.tools/kubectl-v1.33.12 --context do-nyc3-k8s-nyc3-garz-ai \
  apply -f "$payload_dir/<tag>-cert-manager.yaml"
/root/dev/.tools/kubectl-v1.33.12 --context do-nyc3-k8s-nyc3-garz-ai \
  -n cert-manager rollout status deployment/cert-manager --timeout=5m
/root/dev/.tools/kubectl-v1.33.12 --context do-nyc3-k8s-nyc3-garz-ai \
  -n cert-manager rollout status deployment/cert-manager-webhook --timeout=5m
```

After each stage, check all three deployments, six CRDs (`v1` served and storage), the `letsencrypt-prod` ClusterIssuer, and all existing Certificates for Ready conditions. Pause and roll back that stage if webhook admission, ACME orders/challenges, or certificate readiness degrades. Re-run the checksum validator before resuming.

The cert-manager backup must cover the cert-manager custom resources and the TLS/ACME account Secrets separately in an approved protected store. The cert-manager resource backup helper intentionally omits X.509 Secrets; never treat it as a complete restore. The discovery contract does not export or print any Secret value.

The source-owned rotation changes are in `helm/cegarza-blog/templates/certificate.yaml`, `helm/garz-ai/templates/certificate.yaml`, `helm/skyquiet-server/templates/certificate.yaml`, `helm/splattop-blog/templates/certificate.yaml`, `helm/splattop-teams/templates/certificate.yaml`, `helm/splattop/templates/certificate.yaml`, `helm/splatvote/templates/certificate.yaml`, `helm/spotify-hot-100/templates/certificate.yaml`, and `apps/agent-control-plane-runtime-controls/certificate.yaml`. Ingress-shim ownership is covered by `helm/citrus/values.yaml`, `helm/splattop/values.yaml`, `helm/splatvote/values.yaml`, `helm/splatvote/values-prod.yaml`, `helm/splattop-teams/values-prod.yaml`, `helm/poetry/templates/ingress.yaml`, `helm/garz-observability/values-prod.yaml`, `apps/vanity-hosts/values.yaml`, and `k8s/argocd/ingress.yaml`; `argo.splat.top` is owned solely by that ingress-shim Certificate. The former explicit `argo-splat-top` Certificate and historical `blog-tls-secret` duplicate are retired only after the guarded live deletion plan confirms the serving Secret and winner Certificate. The live owner inventory found the remaining explicit Certificates without ownerReferences and shim-created Certificates owned by their Ingress. All remaining source paths request `cert-manager.io/private-key-rotation-policy: Never` in source.

The duplicate retirement helper is `/root/dev/gaic-optimization-2026-09-08/cert_retire.py`. Run it with the checked-in rotation inventory selected via `GAIC_CERT_ROTATION_PATCH=.../ops/certificate-rotation-never-patch.yaml`; its default mode uses Kubernetes API `dryRun=All` with UID and resourceVersion preconditions and `propagationPolicy: Orphan`. It reads only Certificate metadata/status and public `tls.crt` hashes. Apply mode requires the merged cleanup commit SHA and must be reviewed immediately before execution; it never targets Secrets, winner Certificates, Orders, or Challenges.

The external `agent-control-plane` chart remains pinned to `e08ece2f754801b6364b760fb94aae990ee99eac` and no longer renders its Certificate. The same Argo Application now owns the explicit Certificate from `apps/agent-control-plane-runtime-controls/certificate.yaml`, preserving the namespace/name/spec and adding `privateKey.rotationPolicy: Never`. The SplatTop values overlay retains the ingress-shim annotation for any generated Certificate path, but the explicit runtime-controls Certificate is the durable owner. Before syncing, render both Application sources together and require one Certificate identity, no delete/create operation, the unchanged TLS Secret reference, and the pinned external chart/image values.

## Apply Argo CD, preserving KSOPS in the same payload

For each Argo stage, inspect the generated `<tag>-argocd-composed.yaml`. It must retain `argocd-cm` build options `--enable-alpha-plugins --enable-exec`, the `install-ksops` init container pinned to `viaductoss/ksops:v4.3.2`, `KUSTOMIZE_PLUGIN_HOME`, `SOPS_AGE_KEY_FILE`, and the `ksops-tools`/`sops-age` mounts. There must be no transient upstream-only apply and no follow-up patch that briefly removes the plugin.

Export Argo data to an access-controlled file before the first stage if the operator has approved that backup location. The export includes repository credentials; keep it out of logs and never paste it into a terminal transcript. Confirm that Applications, AppProjects, repositories, RBAC settings, sync windows, and the current resource requests/limits are represented in the reviewed diff.

Use server-side diff first:

```sh
/root/dev/.tools/kubectl-v1.33.12 --context do-nyc3-k8s-nyc3-garz-ai \
  diff -n argocd --server-side --field-manager=gaic-controller-upgrade \
  -f "$out_dir/<tag>-argocd-composed.yaml"
```

The v3.2-to-v3.3 ApplicationSet CRD annotation is already near the client-side annotation limit, so the upstream guidance requires SSA for this transition. A reviewer must inspect ownership changes at field level. The generated plan has separate diff and apply commands and does not force conflicts. Apply the reviewed payload with the ordinary SSA command first; only if the reviewed CRD transition reports the documented conflict may an operator rerun that same command with `--force-conflicts`. Reject the apply if it claims repository Secret data, `argocd-cm`, KSOPS fields, replicas, scheduling, or resource fields unexpectedly. Never use force-conflicts indiscriminately on an ad-hoc ConfigMap or Secret patch.

```sh
/root/dev/.tools/kubectl-v1.33.12 --context do-nyc3-k8s-nyc3-garz-ai \
  apply -n argocd --server-side --field-manager=gaic-controller-upgrade \
  -f "$out_dir/<tag>-argocd-composed.yaml"
```

After each stage, wait for all Argo deployments and verify the repo-server KSOPS init, environment, mounts, and secret reference by metadata only. Check Applications and AppProjects for the prior sync/health state, repositories and RBAC settings, sync windows, and absence of unexpected operations. Confirm no Application is terminating before advancing.

## Rollback

Rollback is staged and version-pinned: stop the route, restore the last known-good full manifest for that controller, and repeat the same reviewed diff/apply procedure. For cert-manager, restore the backed-up custom resources and Secrets only after the controller/webhook is healthy; never delete CRDs as a rollback shortcut. For Argo, restore the prior composed manifest so KSOPS remains present, then use the protected Argo export only if resource restoration is required. Recheck issuer, Certificate, Application, repository, RBAC, and sync-window health before reopening reconciliation.

Official references: [cert-manager upgrade guide](https://cert-manager.io/docs/installation/upgrade/), [cert-manager backup guidance](https://cert-manager.io/docs/devops-tips/backup/), [cert-manager 1.17→1.18 notes](https://cert-manager.io/docs/releases/upgrading/upgrading-1.17-1.18/), [cert-manager 1.20→1.21 notes](https://cert-manager.io/docs/releases/upgrading/upgrading-1.20-1.21/), [Argo upgrade overview](https://argo-cd.readthedocs.io/en/stable/operator-manual/upgrading/overview/), [Argo 3.2→3.3 notes](https://argo-cd.readthedocs.io/en/release-3.5/operator-manual/upgrading/3.2-3.3/), and [Argo tested Kubernetes versions](https://argo-cd.readthedocs.io/en/stable/operator-manual/tested-kubernetes-versions/).
