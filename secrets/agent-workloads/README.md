# Agent Workloads Secrets

Encrypted secrets consumed by the `agent-workloads-secrets` Argo CD app.

- `runtime-secret.enc.yaml`: the dedicated Agent Workloads Postgres workspace
  URL, shared worker token, optional model auth, and readonly source database
  URL. It must not contain workload-identity token keys. The XScraper/X Power
  readonly query profile also requires `XSCRAPER_READONLY_DATABASE_URL`, but
  `agent_workloads.readonly_query` is not granted in the first live `db_probe`
  rollout.
- `workload-identity-tokens.enc.yaml`: the three `mwit_v1` workload-identity
  tokens for `data.workspace_probe`, `opencode.proposer`, and
  `opencode.apply_executor`. The first live `data.workspace_probe` deployment
  mounts `MANDATE_WORKLOAD_IDENTITY_TOKEN` as a file at
  `MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE`; do not expose the shared
  `MANDATE_WORKER_TOKEN` to that long-running prod worker. The OpenCode
  proposer receives only `OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN` as
  `MANDATE_WORKLOAD_IDENTITY_TOKEN`. The OpenCode apply executor receives only
  `OPENCODE_APPLY_EXECUTOR_WORKLOAD_IDENTITY_TOKEN` as
  `MANDATE_WORKLOAD_IDENTITY_TOKEN`.
- `workload-identity-tokens.metadata.yaml`: non-secret ledger for the encrypted
  token file. It records the expected token key, agent id, `code_digest`,
  manifest digest, `iat`/`exp`, issuer/subject/audience/scope, digest spec
  version, source commit, and ciphertext hash for each token.
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
authenticates to Mandate with the proposer token from
`workload-identity-tokens.enc.yaml`, then Mandate returns a job-scoped
model-gateway token in the claim response.

The OpenCode apply executor must not receive model provider credentials, Git
credentials, database URLs, or the shared `MANDATE_WORKER_TOKEN`. It
authenticates to Mandate with the apply-executor token from
`workload-identity-tokens.enc.yaml` and consumes only claim-projected
approval/designated-action state.

When `apps/agent-workloads/values.yaml` contains `mandateReleasePins`, CI
decrypts `workload-identity-tokens.enc.yaml` and checks that each workload
identity token's `code_digest` claim matches the release pin and the embedded
registry overlay manifest. It also rejects token keys left in
`runtime-secret.enc.yaml` and verifies the metadata ledger against the token
Secret ciphertext. The gate does not mint tokens; rotate them with the
operator-held HMAC signing seed whenever a release changes a workload
`code_digest`.

Token rotations should regenerate `workload-identity-tokens.enc.yaml` as a whole
file, then regenerate `workload-identity-tokens.metadata.yaml` from the encrypted
file. Do not patch individual ciphertext values by hand; the metadata
`ciphertext_sha256` is the reviewable link between the ledger and the encrypted
token Secret.

The drift-gate automation receives only the scoped Age key that can decrypt
`workload-identity-tokens.enc.yaml`. It must not decrypt
`runtime-secret.enc.yaml`, database URLs, Codex auth JSON, registry credentials,
or any control-plane Secret.

The `data.workspace_probe` worker should receive only
`AGENT_WORKLOADS_DATABASE_URL` plus its mounted workload-identity token for the
first live path. Provider credentials and readonly source-database credentials
belong in later, separately granted worker or broker rollouts.
