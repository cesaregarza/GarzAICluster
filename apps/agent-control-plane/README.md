# Agent Control Plane

This directory holds the SplatTopConfig-owned values overlay for the reusable
`agent-platform` chart.

It is intentionally not active by itself. To deploy it, move or copy the
candidate Application from `argocd/candidates/agent-control-plane.yaml` into
`argocd/applications/` after the required image exists and
`agent-control-plane-secrets` has synced.

Required before activation:

- `registry.digitalocean.com/sendouq/agent-platform:<tag>` exists.
- `agent-control-plane-secrets` has created `regcred` in the
  `agent-control-plane` namespace for DOCR image pulls.
- `agent-control-plane-secrets` has created `agent-control-plane-secrets` in the
  `agent-control-plane` namespace.
  It must include `AGENT_PLATFORM_DATABASE_URL` so run state and audit history
  survive pod restarts.
- The OpenClaw droplet has an MCP server entry pointing at the public control
  API URL with the matching OpenClaw service token.
- The callback/approval adapter path is implemented before advertising
  long-running or approval-gated capabilities to users.
