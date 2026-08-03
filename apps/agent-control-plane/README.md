# Mandate Agent Control Plane

This directory holds the GarzAICluster-owned values overlay for the reusable
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
  state writer role. The role must satisfy the CES-263 ceiling: explicit
  `SELECT` grants only on approved schema-qualified relations, no database
  `CREATE`/`TEMPORARY`, no writable schema in `search_path`, no broad default
  table/sequence grants, and only `security_invoker=true` views unless a future
  reviewed owner-privilege view allowlist is added.
- `AGENT_PLATFORM_WORKLOAD_IDENTITY_ISSUER` and
  `AGENT_PLATFORM_WORKLOAD_IDENTITY_ALLOWED_SUBJECTS_JSON` are explicitly set in
  prod. Missing issuer or subject allowlist fails closed for HMAC workload
  identity claims.
- The OpenClaw droplet has the native `mandate-edge-openclaw` plugin enabled
  with the public control API URL, the matching trusted-edge/OpenClaw service
  token, and the shared `AGENT_PLATFORM_MCP_TRUSTED_CONTEXT_HMAC_SECRET` used
  to sign per-turn `mctx_v2` assertions.
- Hermes uses a separate `agent-control-plane-hermes-mctx` Secret mounted only
  into the API pod through `apiExtraSecretRefs`, so Discord ingress does not
  share OpenClaw's trusted-context HMAC key or expose the Hermes key to worker,
  callback, or model-gateway pods.
- The callback adapter deployment uses the same Postgres state as the API and
  worker, claims delivery by event id, and posts safe terminal output to the
  OpenClaw droplet's `/mandate-edge/openclaw-callback` plugin route. The
  callback-only Discord token remains mounted so approval cards can still be
  rendered by deterministic service code.
- The approval interaction path is service-only:
  `POST /v1/openclaw/discord/approval-interactions` maps trusted Discord
  component payloads to the internal resolver and is not exposed through MCP.
- Model-gateway kill switch and per-job/lease revocation files are mounted from
  `agent-control-plane-model-gateway-controls` as a directory so operator edits
  project without pod restarts. See
  [model-gateway controls](../../docs/model-gateway-controls.md).
- The `agent-control-plane-model-gateway-codex-auth` PVC is mounted by both the
  API and standalone model-gateway. The API therefore reads the same rotated
  `auth.json` as the gateway instead of restarting from the static bootstrap
  Secret. The live DigitalOcean block claim is `ReadWriteOnce`, so the chart
  co-locates the API with the gateway on one node; use ReadWriteMany storage
  before scaling this pair across nodes.
- The registry-overlay app's rollout-strategy Sync hook owns
  `maxSurge: 0`/`maxUnavailable: 1` for the API, callback adapter, git
  deliverer, and local worker. The control-plane Application ignores only those
  numeric fields so Argo does not replace the hardening with Kubernetes'
  singleton 1/0 defaults. The gateway remains chart-owned `Recreate`, and the
  CES-352 required affinity is unchanged.
- `apps/agent-control-plane-runtime-controls/postgres-sweep-cronjob.yaml` owns
  the production maintenance schedule. It runs `mandate-postgres-sweep` every
  ten minutes with `concurrencyPolicy: Forbid`, a five-minute missed-start
  deadline, and a zero-minimum/one-maximum Postgres pool. That single transient
  connection ceiling supersedes the July 5 connection-pressure suspension
  without restoring a long-lived maintenance pool. Keep the reusable chart's
  `postgresSweep.enabled=false` to prevent a second CronJob from running.
- The production terminal-row retention decision is 30 days. The sweep archives
  eligible terminal control-job rows and their dependent rows transactionally
  before deleting the live copies, skips jobs whose callbacks are not terminal,
  and leaves permanent `agent_runs` and `run_events` audit history in place.
  Queued-lease and approval expiry sweeps run on every invocation regardless of
  whether any terminal rows are old enough to archive.
- `syntheticLiveVerify.enabled=true` runs the scheduled deployment smoke and
  readonly-query probes every five minutes through the trusted-edge `/v1/tasks`
  path with signed `mctx_v2` assertions. The dedicated
  `mandate-live-probe` service principal is policy-granted only for
  `mandate.deploy.smoke` and `agent_workloads.readonly_query`, and the delivery
  target is internal so successful probes do not post to the operator Discord
  channel. The readonly-query journey requires the `model_call.finished` audit
  event as explicit proof that the external worker completed a Mandate-brokered
  MODEL round-trip; a green deployment-smoke journey alone is not a deploy
  gate. Failed Jobs alert through the production Prometheus/Alertmanager route.
