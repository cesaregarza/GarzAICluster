# Agent Workloads Secrets

Encrypted secrets consumed by the `agent-workloads-secrets` Argo CD app.

- `runtime-secret.enc.yaml`: Mandate worker-service token and the dedicated
  Agent Workloads Postgres workspace URL.
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
