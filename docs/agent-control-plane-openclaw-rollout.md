# Agent Control Plane OpenClaw Rollout

The current OpenClaw deployment should integrate through Agent Control Plane,
not by calling workload containers directly.

## Target Shape

- `agent-platform` owns the reusable control-plane API chart.
- `SplatTopConfig` owns the live values overlay, Argo Application, namespace,
  image tag, runtime secret, DNS, and TLS.
- The OpenClaw droplet mounts the Agent Control Plane MCP shim and sees only the
  `platform_*` tools.
- `agent-workloads` supplies external worker and broker implementations only
  after Agent Control Plane has an external-worker dispatch contract.

## Activation Order

1. Publish an immutable Agent Control Plane API image:
   `registry.digitalocean.com/sendouq/agent-platform:sha-0a0ef55c0d1b`.
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
4. Render the chart locally with
   `helm template agent-control-plane ../agent-platform/helm/agent-control-plane -f apps/agent-control-plane/values.yaml`.
5. Confirm the rendered NetworkPolicy allows DNS plus managed Postgres egress
   to `10.108.0.0/20:25060`.
6. Run the live deployment with `AGENT_PLATFORM_ENVIRONMENT=prod`; production
   policy exposes only the safe no-key `task.echo` capability.
7. Sync `argocd-repositories` so Argo has the read-only deploy key for the
   private `agent-platform` chart source.
8. Apply the AppProject update from `argocd/projects/splattop-project.yaml` so
   Argo may read the `agent-platform` chart source and deploy into the
   `agent-control-plane` namespace.
9. Sync `splattop-root`, then sync `agent-control-plane`.
10. Configure the OpenClaw droplet MCP server to run
   `AGENT_PLATFORM_MCP_BACKEND=http uv run python -m audit.api.app.mcp.server`
   with `AGENT_PLATFORM_CONTROL_API_BASE_URL=https://agent-control-plane.garz.ai`
   and the matching OpenClaw service token from `agent-control-plane-secrets`.
   The current OpenClaw droplet IP is `143.198.149.87`.

## Current MVP Limits

The live values now deploy the local deterministic worker for `task.echo` and a
callback adapter with Postgres-backed event-id dedupe. The safe smoke target is
the full no-key path:

```text
OpenClaw submit -> API accepts -> worker drains -> output gate releases ->
callback posts once -> status shows released result only
```

Do not advertise external `agent-workloads` capabilities, approval-gated
capabilities, or broker-backed capabilities to users yet.
