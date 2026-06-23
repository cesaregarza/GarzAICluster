> **Relocated from `agent-platform` per CES-50 (2026-06-06).** This is deployment-owned historical operational evidence — a dated record of an actual rollout. The generic Mandate package keeps only a pointer to this file; the full trail lives here.

# OpenClaw MCP Rollout - 2026-05-27

This document records the rollout that connected the live OpenClaw droplet to
Agent Control Plane through the model-visible MCP shim.

## End State

OpenClaw can now see Agent Control Plane as an MCP server named
`agent-platform`. The model-visible surface is limited to:

- `platform_submit_task`
- `platform_get_job_status`
- `platform_cancel_job`
- `platform_list_capabilities`

The live control API is reachable at:

```text
https://agent-control-plane.garz.ai
```

The Kubernetes deployment is healthy and running:

```text
registry.digitalocean.com/sendouq/agent-platform:sha-c06fd9aa810e
```

The deployment still runs with `AGENT_PLATFORM_ENVIRONMENT=dev`. This was
intentional during the initial rollout because local test bindings and example
placeholders existed in the bundled file policy. A later source change split
policy into shared `policy.base.yaml` defaults plus `policy.local.yaml`,
`policy.dev.yaml`, and `policy.prod.yaml`; the live deployment still needs an
explicit rollout to switch
`AGENT_PLATFORM_ENVIRONMENT=prod`.

## Repository Changes

### agent-platform

Commit:

```text
c06fd9a Authorize OpenClaw private admin binding
```

Changed the dev policy so the initial private admin binding matches the live
OpenClaw Discord scope:

- guild: `614277943706910722`
- channel: `1480483954694819940`
- admin user: `94265880216612864`

This binding authorizes the no-key development and smoke-test capabilities for
the current private OpenClaw channel.

Validated before publishing:

```text
uv run --extra dev pytest
159 passed

uv run --extra dev ruff check .
All checks passed
```

GitHub Actions publish workflow:

```text
run: 26497472807
status: success
image: registry.digitalocean.com/sendouq/agent-platform:sha-c06fd9aa810e
```

### GarzAICluster

Commits:

```text
b43278e Deploy OpenClaw policy agent control plane image
893ae04 Fix agent control plane chart revision
```

Changed:

- `apps/agent-control-plane/values.yaml`
  - updated image tag to `sha-c06fd9aa810e`
- `argocd/applications/agent-control-plane.yaml`
  - updated chart source revision to
    `c06fd9aa810e37522cdab3675294f272bb789e89`

Validation:

```text
helm lint /root/dev/agent-platform/helm/agent-control-plane \
  -f apps/agent-control-plane/values.yaml
```

Result:

```text
1 chart(s) linted, 0 chart(s) failed
```

## Cluster Rollout

Synced the Argo app-of-apps root so child Application changes were applied:

```text
splattop-root
sync: Synced
health: Healthy
revision: 893ae04f53040f31819d94d289a8826d6ae8927b
```

Synced the Agent Control Plane app:

```text
agent-control-plane
sync: Synced
health: Healthy
chart revision: c06fd9aa810e37522cdab3675294f272bb789e89
values revision: 893ae04f53040f31819d94d289a8826d6ae8927b
```

Waited for deployment rollout:

```text
deployment "agent-control-plane" successfully rolled out
```

Final Kubernetes state:

```text
deployment/agent-control-plane ready: 1/1
image: registry.digitalocean.com/sendouq/agent-platform:sha-c06fd9aa810e
service: agent-control-plane ClusterIP
ingress: agent-control-plane.garz.ai
certificate: agent-control-plane-cert Ready=True
```

Public health check:

```json
{"ok": true, "environment": "dev"}
```

Authenticated OpenClaw actor capability-list smoke returned these visible
capabilities:

- `task.echo`
- `task.inspect`
- `db.schema.inspect`
- `artifact.probe`
- `approval.probe`
- `broker.probe`
- `task.fail`
- `audit.digest`

## OpenClaw Droplet Wiring

Target host:

```text
root@143.198.149.87
```

OpenClaw runtime facts:

- service: `openclaw-gateway.service`
- user: `openclaw`
- config root: `/home/openclaw/.openclaw`
- workspace: `/home/openclaw/.openclaw/workspace`

Installed Agent Platform checkout:

```text
/home/openclaw/agent-platform
```

Installed `uv` for the OpenClaw user:

```text
/home/openclaw/.local/bin/uv
uv 0.8.17
```

Created the Python virtual environment:

```text
/home/openclaw/agent-platform/.venv
```

Installed the MCP wrapper:

```text
/usr/local/bin/agent-platform-mcp
```

Wrapper behavior:

1. `cd /home/openclaw/agent-platform`
2. source `/home/openclaw/.openclaw/agent-platform-mcp.env`
3. run `python -m mandate.adapters.mcp.server`

Installed runtime environment file:

```text
/home/openclaw/.openclaw/agent-platform-mcp.env
mode: 0600
owner: openclaw
```

The env file contains:

- `AGENT_PLATFORM_MCP_BACKEND=http`
- `AGENT_PLATFORM_CONTROL_API_BASE_URL=https://agent-control-plane.garz.ai`
- `AGENT_PLATFORM_CONTROL_API_TOKEN`
- `AGENT_PLATFORM_MCP_ACTOR_JSON`
- `AGENT_PLATFORM_MCP_REPLY_TARGET_JSON`

The service token was copied without printing it to logs.

Installed the OpenClaw workspace skill:

```text
/home/openclaw/.openclaw/workspace/skills/agent-platform/SKILL.md
```

The skill instructs OpenClaw to route eligible long-running, data/export,
compute, or broker-backed tasks through Agent Control Plane and to avoid raw
broker, credential, policy, approval-resolution, and artifact-delivery tools
when a platform capability applies.

Updated OpenClaw config:

```text
/home/openclaw/.openclaw/openclaw.json
```

Backup:

```text
/home/openclaw/.openclaw/openclaw.json.bak-agent-platform-20260527074045
```

MCP servers after update:

```text
agent-platform
provider-broker
```

`agent-platform` entry:

```json
{
  "command": "/usr/local/bin/agent-platform-mcp",
  "args": []
}
```

Restarted OpenClaw:

```text
systemctl --user -M openclaw@ restart openclaw-gateway.service
```

Final OpenClaw health:

```text
openclaw-gateway.service: active
gateway health: {"ok": true, "status": "live"}
```

## MCP Verification

Used the official Python MCP client from the droplet virtual environment to
exercise the wrapper.

Tool list returned exactly:

```text
platform_submit_task
platform_get_job_status
platform_cancel_job
platform_list_capabilities
```

Calling `platform_list_capabilities` through the MCP wrapper hit the live
control API successfully:

```text
HTTP Request: POST https://agent-control-plane.garz.ai/v1/capabilities/list
HTTP/1.1 200 OK
```

The call returned 8 visible capabilities:

```text
task.echo
task.inspect
db.schema.inspect
artifact.probe
approval.probe
broker.probe
task.fail
audit.digest
```

Submitted a harmless smoke job through the MCP wrapper:

```text
job_id: job_66900e9b2f5d4be6a2bb484ec0eff8f3
submit status: queued
safe status read: queued
progress_summary: Task accepted and queued.
```

This proves that OpenClaw's model-visible MCP path can submit and read safe job
status using trusted actor context and the scoped OpenClaw service token.

## Security Notes

- The LLM-visible MCP surface does not include approval resolution, broker
  invocation, credential minting, policy edits, budget overrides, or direct
  artifact delivery.
- Actor and reply-target context are injected by the droplet-side MCP runtime,
  not supplied by model-visible tool arguments.
- The OpenClaw service token is stored in a mode `0600` env file owned by
  `openclaw`.
- The Kubernetes runtime secret was not printed during this rollout.
- A prior accidental secret-data display during the earlier deployment work was
  corrected by rotating the generated runtime credentials before this MCP wiring
  was completed.

## Current Limitations

The live production rollout now proves the full no-key loop for `task.echo`.
OpenClaw-facing submission accepts the job, the local deterministic worker
claims the queued job from shared Postgres state, worker output is stored as raw
output first, the mandatory output gate emits `output_gate.*`, `result.released`
is emitted, public status exposes only released output, and the callback adapter
posts the final response exactly once from platform delivery state.

The source repo also includes a production callback adapter worker. It reads
platform callback events from Postgres, posts safe accepted/progress/final and
approval content to the configured reply target, and dedupes by event id through
`agent_platform.callback_deliveries`. It must run with the same
`AGENT_PLATFORM_DATABASE_URL` as the API and local worker.

The live deployment runs with `AGENT_PLATFORM_ENVIRONMENT=prod`; the production
policy exposes `task.echo`, `approval.probe`, and the bounded private-admin
`readonly_sql` broker path. Write-capable database capabilities, `db_export`,
and `db.workspace.*` remain intentionally disabled until the broker/runtime
production boundary and approval gates are promoted for that class of work.

## Next Work

1. Wire the OpenClaw production interaction receiver to convert Discord
   approve/deny component events into `POST /v1/internal/approvals/resolve`
   calls using the approval-scoped service token.
2. Prove a live `approval.probe` path with approval card, deterministic user
   event, service-only resolution, replay rejection, and unauthorized reaction
   rejection.
3. Only after approval replay rejection and the broker/runtime production
   boundary are complete, start advertising or routing real broker-backed and
   external-worker capabilities.
