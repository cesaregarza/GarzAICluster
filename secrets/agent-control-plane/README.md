# Agent Control Plane Secrets

Encrypted secrets consumed by the `agent-control-plane-secrets` Argo CD app.

- `runtime-secret.enc.yaml`: API database URL and service tokens for OpenClaw,
  internal workers, external worker services, approval handlers, audit
  readers/writers, the OpenClaw callback hook URL/token, and the
  callback-adapter-only Discord token used for deterministic approval cards.
  It also holds the model-gateway signing secret, Codex ChatGPT `auth.json`,
  workload-identity HMAC secret used to lease model access to hosted harness
  workers such as `opencode.proposer`, and the trusted-edge HMAC secret shared
  with the OpenClaw droplet for `mctx_v2` assertions. The OpenCode git
  deliverer also expects `AGENT_PLATFORM_GIT_DELIVERY_GITHUB_TOKEN`, a
  fine-grained GitHub token restricted to `cesaregarza/mandate-sandbox` with
  Contents and Pull Requests read/write.
- `regcred.enc.yaml`: DOCR pull credentials for
  `registry.digitalocean.com/sendouq/agent-platform`.

Regenerate with:

```bash
SOPS_AGE_KEY_FILE=keys/age-private.txt \
  uv run python scripts/provision_agent_control_plane_secrets.py
```

The script loads `.env`, provisions the dedicated Postgres schema/role using
`BOT_DB_ADMIN_URL`, and encrypts both secret manifests before writing them. Set
`AGENT_PLATFORM_OPENCLAW_CALLBACK_URL`,
`AGENT_PLATFORM_OPENCLAW_CALLBACK_TOKEN`, and
`AGENT_PLATFORM_DISCORD_BOT_TOKEN` or `OPENCLAW_DISCORD_TOKEN` before running it
to include the callback adapter's OpenClaw hook credentials and deterministic
approval-card Discord token. Set `AGENT_PLATFORM_GIT_DELIVERY_GITHUB_TOKEN` to
include the deliverer-scoped GitHub credential.

Add or rotate only the read-only SQL broker credential without rotating service
tokens:

```bash
SOPS_AGE_KEY_FILE=keys/age-private.txt \
  AGENT_CONTROL_PLANE_READONLY_SQL_RELATIONS=public.accounts,common.orders \
  uv run python scripts/provision_agent_control_plane_readonly_sql.py
```

That helper creates a separate weak login role and stores
`AGENT_PLATFORM_READONLY_SQL_DATABASE_URL` in `runtime-secret.enc.yaml`. The
relation list is mandatory: CES-263 forbids blanket schema grants. The helper
grants `SELECT` only on the approved schema-qualified tables/views, pins the role
`search_path` to those schemas, removes database `CREATE`/`TEMPORARY`, revokes
broad/default table grants from the role, and rejects approved views unless they
are `security_invoker=true`.

Hosted harness model access currently needs these additional keys:

- `AGENT_PLATFORM_MODEL_GATEWAY_TOKEN_SECRET`: signs short-lived
  `mglt_v1` model-gateway leases.
- `AGENT_PLATFORM_MODEL_GATEWAY_CODEX_AUTH_JSON`: ChatGPT/Codex auth JSON used
  only by the Mandate model-gateway pod.
- `AGENT_PLATFORM_WORKLOAD_IDENTITY_HMAC_SECRET`: verifies scoped
  `mwit_v1` worker assertions from hosted harness pods.
- `AGENT_PLATFORM_MCP_TRUSTED_CONTEXT_HMAC_SECRET`: verifies per-turn Mandate
  Edge assertions from the OpenClaw plugin.
