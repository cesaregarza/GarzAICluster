# Mandate Agent Control Plane

This directory holds the SplatTopConfig-owned values overlay for the reusable
Mandate `agent-platform` chart. The live Argo Application is
`argocd/applications/agent-control-plane.yaml`.

Required before activation:

- `registry.digitalocean.com/sendouq/agent-platform:<tag>` exists.
- `agent-control-plane-secrets` has created `regcred` in the
  `agent-control-plane` namespace for DOCR image pulls.
- `agent-control-plane-secrets` has created `agent-control-plane-secrets` in the
  `agent-control-plane` namespace.
  It must include `AGENT_PLATFORM_DATABASE_URL` so run state and audit history
  survive pod restarts.
- `AGENT_PLATFORM_ENVIRONMENT=prod` so the live visible capability set is
  limited to the production-safe private-admin `task.echo`, `approval.probe`,
  `readonly_sql`, `audit.digest`, `mandate.ops.inspect`, and
  `mandate.deploy.smoke` bindings.
- `AGENT_PLATFORM_READONLY_SQL_DATABASE_URL` is present when `readonly_sql` is
  enabled. It must point at a separate weak read-only role, not the platform
  state writer role.
- The OpenClaw droplet has an MCP server entry pointing at the public control
  API URL with the matching OpenClaw service token.
- The callback adapter deployment uses the same Postgres state as the API and
  worker, claims delivery by event id, and posts safe released output through
  the configured Discord sink.
- The approval interaction path is service-only:
  `POST /v1/openclaw/discord/approval-interactions` maps trusted Discord
  component payloads to the internal resolver and is not exposed through MCP.
