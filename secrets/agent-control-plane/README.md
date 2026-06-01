# Agent Control Plane Secrets

Encrypted secrets consumed by the `agent-control-plane-secrets` Argo CD app.

- `runtime-secret.enc.yaml`: API database URL and service tokens for OpenClaw,
  internal workers, external worker services, approval handlers, audit
  readers/writers, the OpenClaw callback hook URL/token, and the
  callback-adapter-only Discord token used for deterministic approval cards.
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
approval-card Discord token.

Add or rotate only the read-only SQL broker credential without rotating service
tokens:

```bash
SOPS_AGE_KEY_FILE=keys/age-private.txt \
  uv run python scripts/provision_agent_control_plane_readonly_sql.py
```

That helper creates a separate weak login role and stores
`AGENT_PLATFORM_READONLY_SQL_DATABASE_URL` in `runtime-secret.enc.yaml`.
