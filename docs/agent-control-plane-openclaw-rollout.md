# Mandate OpenClaw Rollout

The current OpenClaw deployment should integrate through Mandate, the Agent
Control Plane, not by calling workload containers directly.

## Target Shape

- `agent-platform` owns the reusable Mandate control-plane API chart at
  `helm/mandate`.
- `GarzAICluster` owns the live values overlay, Argo Application, namespace,
  image tag, runtime secret, DNS, and TLS.
- The live Kubernetes release, namespace, and hostname remain
  `agent-control-plane` for continuity, even though the deployed runtime is
  Mandate.
- The OpenClaw droplet mounts the Mandate MCP shim and sees only the
  `platform_*` tools.
- `agent-workloads` supplies external worker and broker implementations only
  after Mandate has an external-worker dispatch contract.

## Activation Order

1. Publish an immutable Mandate API image:
   `registry.digitalocean.com/sendouq/agent-platform:sha-5c94123d1cc4`.
2. Commit and sync `argocd/applications/agent-control-plane-secrets.yaml` so
   the `agent-control-plane-secrets` Argo app creates `regcred` and
   `agent-control-plane-secrets` in the `agent-control-plane` namespace.
3. Confirm the encrypted runtime secret includes the required keys for this
   overlay:
   `AGENT_PLATFORM_DATABASE_URL`,
   `AGENT_PLATFORM_OPENCLAW_TOKEN`,
   `AGENT_PLATFORM_INTERNAL_WORKER_TOKEN`,
   `AGENT_PLATFORM_APPROVAL_TOKEN`,
   `AGENT_PLATFORM_AUDIT_READ_TOKEN`, and
   `AGENT_PLATFORM_AUDIT_WRITE_TOKEN`.
   When `readonly_sql` is enabled, it must also include
   `AGENT_PLATFORM_READONLY_SQL_DATABASE_URL` for a separate weak read-only
   database role.
   When the callback adapter uses the OpenClaw hook sink, it must also include
   `AGENT_PLATFORM_OPENCLAW_CALLBACK_URL` and
   `AGENT_PLATFORM_OPENCLAW_CALLBACK_TOKEN`. Keep
   `AGENT_PLATFORM_DISCORD_BOT_TOKEN` mounted only for deterministic approval
   cards.
4. Render the chart locally with
   `helm template agent-control-plane ../agent-platform/helm/mandate -f apps/agent-control-plane/values.yaml`.
5. Confirm the rendered NetworkPolicy allows DNS plus managed Postgres egress
   to `10.108.0.0/20:25060`.
6. Run the live deployment with `AGENT_PLATFORM_ENVIRONMENT=prod`; production
   policy exposes only private-admin no-key ops capabilities plus bounded
   `readonly_sql`.
7. Sync `argocd-repositories` so Argo has the read-only deploy key for the
   private `agent-platform` chart source.
8. Apply the AppProject update from `argocd/projects/splattop-project.yaml` so
   Argo may read the `agent-platform` chart source and deploy into the
   `agent-control-plane` namespace.
9. Sync `splattop-root`, then sync `agent-control-plane`.
10. Configure the OpenClaw droplet MCP server to run
   `AGENT_PLATFORM_MCP_BACKEND=http uv run python -m mandate.adapters.mcp.server`
   with `AGENT_PLATFORM_CONTROL_API_BASE_URL=https://agent-control-plane.garz.ai`
   and the matching OpenClaw service token from `agent-control-plane-secrets`.
   The current OpenClaw droplet IP is `143.198.149.87`.

## Current MVP Limits

The live values now deploy the local deterministic worker for `task.echo`,
`approval.probe`, bounded `readonly_sql`, `audit.digest`,
`mandate.ops.inspect`, and `mandate.deploy.smoke`, plus a callback adapter with
Postgres-backed event-id dedupe. Terminal callbacks are handed back to OpenClaw
through the droplet hook so OpenClaw owns the user-facing follow-up turn.
Approval cards still use deterministic Discord service code. Routine accepted
and progress callbacks are accepted by the hook but do not trigger an OpenClaw
turn. The safe smoke targets are the full governed paths:

```text
OpenClaw submit -> API accepts -> worker drains -> output gate releases ->
OpenClaw hook accepts terminal callback once -> OpenClaw replies in Discord ->
status shows released result only

OpenClaw submit approval.probe -> worker pauses -> approval.requested card posts ->
trusted Discord interaction resolves approval -> output gate releases ->
final callback posts once

OpenClaw submit readonly_sql -> API accepts -> worker receives only the broker
handle -> read-only broker executes through a weak role -> output gate releases
summary envelope -> status/callback show no raw rows

OpenClaw submit mandate.ops.inspect -> API accepts -> worker reads only the
Mandate audit summary query -> output gate releases operational health summary

OpenClaw submit mandate.deploy.smoke -> API accepts -> worker verifies the
admission/dispatch/lease/release path -> final callback posts once
```

Visible production capabilities must remain limited to `task.echo`,
`approval.probe`, private-admin `readonly_sql`, `audit.digest`,
`mandate.ops.inspect`, and `mandate.deploy.smoke`. Do not advertise external
`agent-workloads` capabilities, `db_export`, `db.workspace.*`, or write-capable
database capabilities to users yet.

OpenClaw status wording should treat `queued`, `running`, and
`waiting_for_approval` as non-terminal even when the progress text sounds final.
Before reporting completion, perform a final status read and require
`succeeded`, `failed`, or `cancelled`.
