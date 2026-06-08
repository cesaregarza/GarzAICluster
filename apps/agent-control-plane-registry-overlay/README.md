# Agent Control Plane Registry Overlay

This app installs the prod deployment overlay for live `agent-workloads`
worker-service paths.

The ConfigMap mounts registry overlay files into the Mandate pod:

- `workload_imports.yaml` imports deployment-pinned workload manifests and image
  digests for `data.workspace_probe` and `opencode.proposer`.
- `agent-data.workspace_probe.json` is the generated `WorkloadManifestV1`
  captured from the release artifact.
- `agent-opencode.proposer.json` is the generated immutable manifest for the
  OpenCode proposer image.
- `evals.yaml` keeps the standard Mandate eval registry plus the deployment
  `eval.opencode_proposer_smoke` suite required by the imported manifest.
- `policy.prod.yaml` grants `agent_workloads.db_probe` and the non-consequential
  `agent_workloads.opencode_propose` proposal capability to the private admin
  Discord actor/channel binding.

The current pins come from the latest successful `agent-workloads` main release
artifact (`sha-5a6571fc7cb3`). That release includes the CES-34 worker-loop
hardening and the OpenCode proposer image, and this overlay uses the
machine-generated manifest/image/code digests from the release artifact.

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
metadata-only `opencode_proposal` artifact. This overlay does not add an apply
path or consequential authority.
