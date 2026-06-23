> **Relocated from `agent-platform` per CES-50 (2026-06-06).** Original deployment-specific MVP notes. The generic deployment *pattern* stays in `agent-platform/docs/runbooks/openclaw-mvp-deployment.md`; the cluster-specific historical detail lives here.

# OpenClaw MVP Deployment

This is the deployment path for making the current OpenClaw instance talk to
Mandate without exposing raw broker or worker tools to the model.

## What Can Be Deployed First

Deploy the Mandate API and local deterministic worker from
`mandate-agent-control-plane` into Kubernetes through `GarzAICluster`. The
chart lives in `helm/mandate`; the live values, runtime secret, DNS, TLS, and
Argo Application belong in `GarzAICluster`.

The initial OpenClaw-facing capability surface should be limited to:

- `platform_list_capabilities`
- `platform_submit_task`
- `platform_get_job_status`
- `platform_cancel_job`

This proves trusted context injection, service-token auth, policy admission,
safe status reads, deterministic queue draining, output-gated release, and
idempotent callback rendering for `task.echo`.

## Not Ready Yet

The no-key local worker runner exists, but it is intentionally narrow:

- default allowed capability: `task.echo`
- broker-backed capability routing remains explicitly allowlisted per
  deployment
- no external worker dispatcher for `agent-workloads` manifests
- no general hosted-harness worker runtime yet

The model-visible MCP status tool intentionally hides raw worker result and
artifact payloads. Public status and callbacks must continue to use only
released output.

## First Useful End-to-End Slice

1. Publish the Mandate API image. During the current compatibility period this
   is still pushed to `registry.digitalocean.com/sendouq/agent-platform`.
2. Deploy the control API and local worker with service tokens and shared
   Postgres state.
3. Mount the MCP shim into OpenClaw with HTTP backend config.
4. Keep `AGENT_PLATFORM_LOCAL_WORKER_CAPABILITIES=task.echo`.
5. Read callback outbox events and render accepted/progress/final posts through
   the OpenClaw callback adapter.
6. Verify: OpenClaw submits a `task.echo` request, the job runs, and the final
   deterministic callback is posted once.

`agent-workloads` should remain smoke-only until there is a general
external-worker dispatcher. Broker-backed capabilities should remain disabled
until the callback worker path and production policy rollout are complete.

## Live No-Key Verification

Use this checklist before advertising anything beyond `task.echo`:

1. Confirm the API pod and local worker pod are running from the same image tag.
2. Confirm the local worker has
   `AGENT_PLATFORM_LOCAL_WORKER_CAPABILITIES=task.echo`.
3. Confirm OpenClaw's MCP config exposes only:
   `platform_submit_task`, `platform_get_job_status`, `platform_cancel_job`,
   and `platform_list_capabilities`.
4. From OpenClaw, submit a `task.echo` request and capture the returned `job_id`.
5. Read public status through the MCP status tool. It should move from `queued`
   to `succeeded` and show only released output.
6. Inspect internal events for the job. Required events:
   `output_gate.started`, `output_gate.check_finished`, `output_gate.passed`,
   and `result.released`.
7. Inspect cost ledger entries for `worker_execute`, `output_gate`, and
   `result_release`; all should be deterministic zero-cost entries.
8. Replay callback delivery for the same callback event id. Discord/OpenClaw
   should not receive a duplicate final result.

If any check requires raw worker output to be shown to the model or Discord,
stop. Raw output is internal only until the output gate releases it.
