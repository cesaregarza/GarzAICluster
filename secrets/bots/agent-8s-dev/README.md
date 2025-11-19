# Agent-8s Dev Secrets

Encrypt the Discord token and optional database URL before merging:

1. Discord token:

   ```bash
   uv run python scripts/onboard_bot_secret.py agent-8s-dev "DISCORD_TOKEN"
   # or set BOT_TOKEN in .env and omit the argument:
   uv run python scripts/onboard_bot_secret.py agent-8s-dev
   ```

2. Database credentials (requires `BOT_DB_ADMIN_URL` or `--admin-url`):

   ```bash
   BOT_DB_ADMIN_URL=postgresql://admin:***@private-db:25060/xscraper?sslmode=require \
     uv run python scripts/provision_bot_db.py agent-8s-dev \
       --secret-file secrets/bots/agent-8s-dev/db-secret.enc.yaml \
       --secret-name bot-db-readonly

   sops --encrypt --in-place secrets/bots/agent-8s-dev/db-secret.enc.yaml
   ```

Commit only the encrypted `.enc.yaml` files. The Agent-8s chart mounts them as
`bot-token` (`BOT_TOKEN`) and `bot-db-readonly` (`DATABASE_URL`) inside the
`splattop-bot-agent-8s-dev` namespace.
