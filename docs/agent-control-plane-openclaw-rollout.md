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
   `registry.digitalocean.com/sendouq/agent-platform:sha-fa8c357d9d4f`.
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
6. Apply the AppProject update from `argocd/projects/splattop-project.yaml` so
   Argo may read the `agent-platform` chart source and deploy into the
   `agent-control-plane` namespace.
7. Move `argocd/candidates/agent-control-plane.yaml` into
   `argocd/applications/agent-control-plane.yaml`.
8. Merge and sync `splattop-root`, then sync `agent-control-plane`.
9. Configure the OpenClaw droplet MCP server to run
   `AGENT_PLATFORM_MCP_BACKEND=http uv run python -m audit.api.app.mcp.server`
   with `AGENT_PLATFORM_CONTROL_API_BASE_URL=https://agent-control-plane.garz.ai`
   and the matching OpenClaw service token from `agent-control-plane-secrets`.
   The current OpenClaw droplet IP is `143.198.149.87`.

## Current MVP Limits

The control API can accept and authorize tasks today, but OpenClaw needs two
more pieces before this is useful for real user-visible work:

- A runner path for queued local deterministic workers, or a background worker
  service that drains queued jobs.
- A deterministic adapter that consumes callbacks and renders final result,
  progress, approval, and artifact events back into OpenClaw/Discord.

Until those exist, the safe smoke target is capability listing plus controlled
task submission/status checks. Do not advertise external `agent-workloads`
capabilities to users yet.
