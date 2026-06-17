# Agent Control Plane Registry Overlay

This app installs the prod deployment overlay for live `agent-workloads`
worker-service paths.

## Sync rollout path (CES-108)

The control plane builds its `RegistrySnapshot` once at boot and never
re-reads the mounted overlay, so every overlay change requires a restart of
all four control-plane Deployments to take effect. A `PostSync` hook Job
(`restart-hook.yaml`, ServiceAccount/Role scoped to exactly those four
Deployments in `restart-rbac.yaml`) is intended to perform the `rollout
restart` and then wait on `rollout status` for each — **a config the deployed
image cannot boot fails the sync loudly** instead of crash-looping in silence.
Until CES-108 live verification confirms that hook fires in prod, operators
must confirm the Deployments rolled after overlay sync and run a manual
`kubectl rollout restart` if they did not.

Note: the hook fires on every sync of this app, including no-op re-syncs;
restarts are rolling and the app is manual-sync, so syncs are deliberate.
The Argo Application intentionally does not set `ApplyOutOfSyncOnly=true`:
selective syncs do not execute hooks, which skips the restart Job and leaves
the control plane serving the previous boot-cached registry snapshot.
Workload-identity token re-mints update the `agent-workloads-secrets` app and
roll the workload Deployments, not the control-plane Deployments. That worker
rollout path stays separate from this overlay hook. The control-plane-read HMAC
verify seed lives under `agent-control-plane-secrets`; rotating it is a rare
operator action and remains outside the registry-overlay restart hook.

## Deployed-version compatibility gate (CES-126)

Config repo CI runs `agent-control-plane-deployed-registry-compat` before an
overlay or policy PR can merge. The job reads the `agent-platform`
`targetRevision` from `argocd/applications/agent-control-plane.yaml`, checks out
that exact source revision, materializes this ConfigMap into its `registries/`
directory, and builds `RegistrySnapshot.from_repo(environment="prod")` using the
pinned code. A PR that the currently selected control-plane binary cannot boot
therefore fails in CI with the Mandate `RegistryError` instead of crash-looping
after Argo sync.

The job is only a compatibility gate. It does not grant dispatch authority and
does not treat this overlay as authority by itself; Mandate still enforces
registry validation, policy grants, admission, leases, brokers, output gates,
and audit at runtime. Keep the status check required in GitHub branch
protection, next to `agent-workloads-identity-digest-drift`.

The ConfigMap mounts registry overlay files into the Mandate pod:

- `workload_imports.yaml` imports deployment-pinned workload manifests and image
  digests for `data.workspace_probe`, `opencode.proposer`, and
  `opencode.apply_executor`.
- `agent-data.workspace_probe.json` is the generated `WorkloadManifestV1`
  captured from the release artifact.
- `agent-opencode.proposer.json` is the generated immutable manifest for the
  OpenCode proposer image.
- `agent-opencode.apply_executor.json` is the generated immutable manifest for
  the OpenCode apply executor image.
- `evals.yaml` keeps the standard Mandate eval registry plus the deployment
  `eval.opencode_proposer_smoke` and `eval.opencode_apply_smoke` suites
  required by the imported manifests.
- `policy.prod.yaml` grants `agent_workloads.db_probe`, the non-consequential
  `agent_workloads.opencode_propose` proposal capability, and the
  admin-confirmed consequential `agent_workloads.opencode_apply` executor
  capability to the private admin Discord actor/channel binding.

The current pins come from the latest successful `agent-workloads` main release
artifact (`sha-ef87ef840a88`). That release includes the OpenCode
proposer reliability fixes, the OpenCode proposer image, and the OpenCode apply
executor image, and this overlay uses the machine-generated
manifest/image/code digests from the release artifact.

The Agent Control Plane is pinned to agent-platform
`2ac9e64aed2efb30e531be9e0dc5aec0459dfff6` / image tag
`sha-2ac9e64aed2e`, which includes the OpenCode proposer governance,
DesignatedAction minting, executor-action lease projections, and workload-import
`executor` field support required by this overlay. Do not sync the apply
executor import against older control-plane images; workload imports validate
atomically.

The imported manifest is data, not dispatch authority. Mandate still loads the
overlay through the registry validators, and dispatch still requires the policy
grant, admission, a matching workload identity claim, lease projection, output
gate processing, and audit.

`agent_workloads.readonly_query` remains declared in the manifest so the image
and code digest match the generated release artifact, but it is not granted in
prod in this first live path. That capability needs a separate rollout after
the readonly database and model-call gates are explicitly reviewed.

`agent_workloads.opencode_propose` is granted as proposal-only
`reversible_staging` authority. It receives only a per-job model-gateway leased
token through the worker claim response, and its diff is released as a
metadata-only `opencode_proposal` artifact.

`agent_workloads.opencode_apply` is consequential authority and remains behind
`admin_confirm`. The apply worker is a separate `executor: true`
`capability_worker`, not a hosted harness. It receives no model gateway URL,
provider credentials, Git credentials, or database credentials. It consumes only
Mandate-projected approval resume state, `DesignatedAction`, and
`ExecutorActionLease`, re-hashes the proposal patch bytes, and prepares a local
non-default branch/commit. Remote push and PR creation remain deferred to a
future Git broker.
