# Agent Control Plane Registry Overlay

This app installs the prod deployment overlay for the first live
`agent-workloads` worker-service path.

The ConfigMap mounts three files into the Mandate pod:

- `workload_imports.yaml` imports the deployment-pinned
  `data.workspace_probe` workload manifest and image digest.
- `agent-data.workspace_probe.json` is the generated `WorkloadManifestV1`
  captured from the release artifact.
- `policy.prod.yaml` grants only `agent_workloads.db_probe` to the private admin
  Discord actor/channel binding.

The current pins come from the latest successful `agent-workloads` main release
artifact (`sha-5dd0c7e75e9e`). The CES-34 worker-loop hardening changes the
future workload `code_digest`; after that source PR merges and publishes from
main, refresh this overlay from the new release artifact before claiming the
hardened loop is deployed.

The imported manifest is data, not dispatch authority. Mandate still loads the
overlay through the registry validators, and dispatch still requires the policy
grant, admission, a matching workload identity claim, lease projection, output
gate processing, and audit.

`agent_workloads.readonly_query` remains declared in the manifest so the image
and code digest match the generated release artifact, but it is not granted in
prod in this first live path. That capability needs a separate rollout after
the readonly database and model-call gates are explicitly reviewed.
