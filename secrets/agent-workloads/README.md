# Agent Workloads Secrets

Encrypted secrets consumed by the `agent-workloads-secrets` Argo CD app.

- `runtime-secret.enc.yaml`: Mandate worker-service token and the dedicated
  Agent Workloads Postgres workspace URL. It can also hold either
  `OPENAI_API_KEY` or `OPENAI_CODEX_AUTH_JSON` for broker workloads that call
  the OpenAI/Codex Responses API. The XScraper/X Power readonly query profile
  also requires `XSCRAPER_READONLY_DATABASE_URL`. The OpenCode proposer uses a
  separate `OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN`; expose it only to the
  `opencode.proposer` deployment as `MANDATE_WORKLOAD_IDENTITY_TOKEN`.
- `regcred.enc.yaml`: DOCR pull credentials for
  `registry.digitalocean.com/sendouq/agent-workloads-worker`.

Provision or rotate with:

```bash
SOPS_AGE_KEY_FILE=keys/age-private.txt \
  uv run python scripts/provision_agent_workloads_secrets.py
```

The helper creates the `agent_workloads` schema and `agent_workloads_user`
role, encrypts the worker runtime secret, and writes the matching
`AGENT_PLATFORM_WORKER_SERVICE_TOKEN` into the Agent Control Plane runtime
secret. Add `--read-schema <schema>` only when a workload needs explicit
read-only access to an existing source schema.

Add or rotate OpenAI auth without rerunning database provisioning:

```bash
SOPS_AGE_KEY_FILE=keys/age-private.txt \
  AGENT_WORKLOADS_OPENAI_API_KEY=sk-... \
  python scripts/set_agent_workloads_openai_auth.py
```

Or store Codex `auth.json` credentials:

```bash
SOPS_AGE_KEY_FILE=keys/age-private.txt \
  AGENT_WORKLOADS_CODEX_AUTH_JSON_FILE=/path/to/auth.json \
  python scripts/set_agent_workloads_openai_auth.py
```

Use only one auth source at a time. `OPENAI_API_KEY` is preferred for simple
platform API access; `OPENAI_CODEX_AUTH_JSON` is supported for the same
ChatGPT/Codex auth shape used by OpenClaw. For long-lived broker deployments,
prefer an API key unless a separate process is keeping the Codex `auth.json`
fresh; a SOPS/Kubernetes Secret value is static after sync.

The OpenCode proposer must not receive `OPENAI_CODEX_AUTH_JSON`,
`OPENAI_API_KEY`, database URLs, or the shared `MANDATE_WORKER_TOKEN`. It
authenticates to Mandate with `OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN`, then
Mandate returns a job-scoped model-gateway token in the claim response.
