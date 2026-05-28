# Agent Control Plane Secrets

Encrypted secrets consumed by the `agent-control-plane-secrets` Argo CD app.

- `runtime-secret.enc.yaml`: API database URL and service tokens for OpenClaw,
  internal workers, approval handlers, audit readers/writers, and the
  callback-adapter-only Discord sink token.
- `regcred.enc.yaml`: DOCR pull credentials for
  `registry.digitalocean.com/sendouq/agent-platform`.

Regenerate with:

```bash
SOPS_AGE_KEY_FILE=keys/age-private.txt \
  uv run python scripts/provision_agent_control_plane_secrets.py
```

The script loads `.env`, provisions the dedicated Postgres schema/role using
`BOT_DB_ADMIN_URL`, and encrypts both secret manifests before writing them. Set
`AGENT_PLATFORM_DISCORD_BOT_TOKEN` or `OPENCLAW_DISCORD_TOKEN` before running it
to include the callback adapter's Discord sink token.
