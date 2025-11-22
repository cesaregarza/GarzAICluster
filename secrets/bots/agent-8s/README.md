# Agent-8s Secrets

Encrypt the Discord token and optional database URL before merging:

1. Discord token:

   ```bash
   uv run python scripts/onboard_bot_secret.py agent-8s "DISCORD_TOKEN"
   # or set BOT_TOKEN in .env and omit the argument:
   uv run python scripts/onboard_bot_secret.py agent-8s
   ```

2. Database credentials (requires `BOT_DB_ADMIN_URL` or `--admin-url`):

   ```bash
   BOT_DB_ADMIN_URL=postgresql://admin:***@private-db:25060/xscraper?sslmode=require \
     uv run python scripts/provision_bot_db.py agent-8s
   ```

The script now writes to `secrets/bots/agent-8s/db-secret.enc.yaml` by default
and will auto-encrypt with SOPS when available. Commit only the encrypted `.enc.yaml`
files. The Agent-8s chart mounts them as `bot-token` (`BOT_TOKEN`) and
`bot-db-readonly` (`DATABASE_URL`) inside the `splattop-bot-agent-8s` namespace.
