# Agent Control Plane Registry Overlay

This app installs the production registry overlay for live `agent-workloads` worker-service paths.

The overlay is authored as ordinary source files under `registry/` and assembled into the `agent-control-plane-registry-overlay` ConfigMap by Kustomize. Do not hand-edit a rendered ConfigMap literal; keep changes in these files instead:

- `registry/workload_imports.yaml` imports deployment-pinned workload manifests and image digests for `data.workspace_probe`, `opencode.proposer`, and `opencode.apply_executor`.
- `registry/policy.prod.yaml` carries the production binding/budget overlay for the imported capabilities and synthetic smoke actor.
- `registry/evals.yaml` mirrors the pinned `agent-platform` eval registry and adds deployment smoke suites for the OpenCode proposer/apply imports.
- `registry/imports/*.json` are immutable `WorkloadManifestV1` payloads captured from the `agent-workloads` release pins in `apps/agent-workloads/values.yaml`.
- `registry/imports/*.jsonl` are the overlay-owned smoke datasets referenced by the imported manifests.

## Runtime mount contract

The generated ConfigMap keeps the existing runtime shape:

- `workload_imports.yaml`, `policy.prod.yaml`, and `evals.yaml` are mounted into `/app/registries`.
- Every other ConfigMap key is mounted under `/app/registries/imports`.
- `scripts/check_agent_control_plane_registry_compat.py` materializes the same shape before asking the pinned `agent-platform` checkout to build `RegistrySnapshot.from_repo(environment="prod")`.
- In pull-request CI, the same script compares the rendered ConfigMap data against
  the base branch's last-known-good ConfigMap, semantically for YAML/JSON/JSONL
  values, so readability-only edits cannot silently drift production authority.

## Sync behavior

The control plane builds its `RegistrySnapshot` once at boot and never re-reads the mounted overlay, so every overlay change requires a restart of all four control-plane Deployments to take effect. The `PostSync` hook Job in `restart-hook.yaml`, using the narrowly scoped ServiceAccount/Role in `restart-rbac.yaml`, performs the rollout restart and waits on rollout status for each target deployment.

The Argo Application intentionally does not set `ApplyOutOfSyncOnly=true`; selective syncs skip hooks, which would skip the restart job and leave the control plane serving the previous boot-cached registry snapshot.

## Authority notes

The imported manifests are data, not dispatch authority. Mandate still loads the overlay through registry validators, and dispatch still requires a policy grant, admission, a matching workload identity claim, lease projection, output-gate processing, and audit.

`agent_workloads.opencode_propose` is proposal-only reversible-staging authority. It receives only a per-job model-gateway leased token through the worker claim response, and its diff is released as metadata-only `opencode_proposal` artifact metadata.

`agent_workloads.opencode_apply` is consequential authority and remains behind `admin_confirm`. The apply worker is a separate `executor: true` `capability_worker`, not a hosted harness. It receives no model gateway URL, provider credentials, Git credentials, or database credentials.
