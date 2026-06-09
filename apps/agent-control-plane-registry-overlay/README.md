# Agent Control Plane Registry Overlay

This app installs the prod deployment overlay for live `agent-workloads`
worker-service paths.

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
artifact (`sha-505f7689c65a`). That release includes the CES-92 OpenCode
proposer reliability fixes, the OpenCode proposer image, and the OpenCode apply
executor image, and this overlay uses the machine-generated
manifest/image/code digests from the release artifact.

The Agent Control Plane is pinned to agent-platform
`77925be310da0753175c8b5024e66c913df81930` / image tag
`sha-77925be310da`, which includes the OpenCode proposer governance,
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
