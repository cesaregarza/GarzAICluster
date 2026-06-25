# Grant Ownership Map

This file is generated from the agent-workloads release applier contract and
the deployment registry overlay. Do not hand-edit it; run
`scripts/generate_grant_ownership.py` instead.

Changing a deployment-owned key edits the GarzAICluster registry overlay and
requires a control-plane restart. It does not move workload image, manifest,
or code digests, and it does not require workload identity token re-minting.

Changing a workload-release-owned key belongs in `agent-workloads`
`agents/<id>/agent.yaml`; that moves the workload code digest and requires the
normal publish, re-pin, and re-mint flow.

## Source Contract

- Source: `cesaregarza/agent-workloads/scripts/apply_splattop_release_artifacts.py`
- Deployment-owned capability roots: `artifacts`, `disclosure`, `model_lease`
- Preserved existing capability roots: `approval_mode`
- Session authority preserved subkeys: `session_authority_budget.max_operations`, `session_authority_budget.session_taint`

## Consumers

- `scripts/set_grant.py`
- `mandate doctor deployment-layout checks`

## Policy Overlay

The `policy.prod.yaml` embedded in the registry overlay is deployment-owned.
Policy grants, approval overrides, and aggregate budget caps require a
control-plane restart and do not require re-minting.

## Capability Keys

| capability | key | owner | consequence |
| --- | --- | --- | --- |
| `agent_workloads.db_probe` | `description` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.db_probe` | `output_gate` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.db_probe` | `output_schema` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_apply` | `approval_mode` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_apply` | `artifacts` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_apply` | `description` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_apply` | `disclosure` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_apply` | `disclosure_summary` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_apply` | `negative_affordances` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_apply` | `output_gate` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_apply` | `output_schema` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_apply` | `result_contract` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_apply` | `session_authority_budget` | `mixed` | `control_plane_restart` |
| `agent_workloads.opencode_orchestrate` | `artifacts` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_orchestrate` | `description` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_orchestrate` | `disclosure` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_orchestrate` | `disclosure_summary` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_orchestrate` | `model_lease` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_orchestrate` | `negative_affordances` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_orchestrate` | `output_gate` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_orchestrate` | `output_schema` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_orchestrate` | `result_contract` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_orchestrate` | `session_authority_budget` | `mixed` | `control_plane_restart` |
| `agent_workloads.opencode_orchestrate` | `task_contract` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_propose` | `artifacts` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_propose` | `description` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_propose` | `disclosure` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_propose` | `disclosure_summary` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_propose` | `model_lease` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_propose` | `negative_affordances` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_propose` | `output_gate` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_propose` | `output_schema` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_propose` | `result_contract` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_propose` | `session_authority_budget` | `mixed` | `control_plane_restart` |
| `agent_workloads.opencode_task` | `artifacts` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_task` | `description` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_task` | `disclosure` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_task` | `disclosure_summary` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_task` | `model_lease` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.opencode_task` | `negative_affordances` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_task` | `output_gate` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_task` | `output_schema` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_task` | `result_contract` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.opencode_task` | `session_authority_budget` | `mixed` | `control_plane_restart` |
| `agent_workloads.opencode_task` | `task_contract` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.readonly_query` | `artifacts` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.readonly_query` | `broker` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.readonly_query` | `broker_lease` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.readonly_query` | `description` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.readonly_query` | `model_lease` | `deployment_overlay` | `control_plane_restart` |
| `agent_workloads.readonly_query` | `output_gate` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.readonly_query` | `output_schema` | `workload_release` | `digest_moves_repin_remint` |
| `agent_workloads.readonly_query` | `session_authority_budget` | `mixed` | `control_plane_restart` |

