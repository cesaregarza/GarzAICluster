# Agent-8s Secrets

Encrypt the Discord token and read-only database URL before merging:

1. Discord token:

   ```bash
   uv run python scripts/onboard_bot_secret.py agent-8s "DISCORD_TOKEN"
   ```

2. Database credentials (requires `BOT_DB_ADMIN_URL` or `--admin-url`):

   ```bash
   BOT_DB_ADMIN_URL=postgresql://admin:***@private-db:25060/xscraper?sslmode=require \
     uv run python scripts/provision_bot_db.py agent-8s \
       --secret-file secrets/bots/agent-8s/db-secret.enc.yaml \
       --secret-name bot-db-readonly

   sops --encrypt --in-place secrets/bots/agent-8s/db-secret.enc.yaml
   ```

Commit the encrypted `token.enc.yaml` and `db-secret.enc.yaml` files only. The `apps/agent-8s` chart mounts these as `bot-token` and `bot-db-readonly` to provide `BOT_TOKEN` and `DATABASE_URL` to the pod.
