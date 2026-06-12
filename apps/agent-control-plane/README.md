# Mandate Agent Control Plane

This directory holds the SplatTopConfig-owned values overlay for the reusable
Mandate chart from `agent-platform/helm/mandate`. The live Argo Application is
`argocd/applications/agent-control-plane.yaml`.

The Kubernetes namespace, Helm release, public hostname, and secret names still
use `agent-control-plane` for continuity. The deployed chart and runtime are
Mandate.

Required before activation:

- `registry.digitalocean.com/sendouq/agent-platform:<tag>` exists. The current
  pin is a published main image that includes workload-import support and the
  CES-34 platform auth hardening, so missing prod issuer or subject allowlist
  configuration fails closed in the live API.
- `agent-control-plane-secrets` has created `regcred` in the
  `agent-control-plane` namespace for DOCR image pulls.
- `agent-control-plane-secrets` has created `agent-control-plane-secrets` in the
  `agent-control-plane` namespace.
  It must include `AGENT_PLATFORM_DATABASE_URL` so run state and audit history
  survive pod restarts.
- `AGENT_PLATFORM_ENVIRONMENT=prod` so the live visible capability set is
  limited to the production-safe private-admin `task.echo`, `approval.probe`,
  `readonly_sql`, `audit.digest`, `mandate.ops.inspect`, and
  `mandate.deploy.smoke` bindings plus any capability explicitly granted by the
  deployment registry overlay.
- `apps/agent-control-plane-registry-overlay/` is synced before the control
  plane. It mounts the deployment-pinned `workload_imports.yaml`, generated
  workload manifest, and prod policy grant overlay into `/app/registries`.
  Importing the manifest only makes the worker-service agent visible to the
  registry; dispatch still requires the prod policy grant and Mandate admission.
- `AGENT_PLATFORM_READONLY_SQL_DATABASE_URL` is present when `readonly_sql` is
  enabled. It must point at a separate weak read-only role, not the platform
  state writer role.
- `AGENT_PLATFORM_WORKLOAD_IDENTITY_ISSUER` and
  `AGENT_PLATFORM_WORKLOAD_IDENTITY_ALLOWED_SUBJECTS_JSON` are explicitly set in
  prod. Missing issuer or subject allowlist fails closed for HMAC workload
  identity claims.
- The OpenClaw droplet has an MCP server entry pointing at the public control
  API URL with the matching OpenClaw service token.
- The callback adapter deployment uses the same Postgres state as the API and
  worker, claims delivery by event id, and posts safe terminal output to the
  OpenClaw droplet hook. The callback-only Discord token remains mounted so
  approval cards can still be rendered by deterministic service code.
- The approval interaction path is service-only:
  `POST /v1/openclaw/discord/approval-interactions` maps trusted Discord
  component payloads to the internal resolver and is not exposed through MCP.
- Model-gateway kill switch and per-job/lease revocation files are mounted from
  `agent-control-plane-model-gateway-controls` as a directory so operator edits
  project without pod restarts. See
  [model-gateway controls](../../docs/model-gateway-controls.md).
