# SplatVote Prod Secrets

Provision the database role and generate encrypted secrets for SplatVote.

1. Create/update the database role + secrets (uses .env by default):

   ```bash
   SOPS_AGE_KEY_FILE=keys/age-private.txt \
     uv run python scripts/provision_splatvote_secrets.py
   ```

   This writes:
   - `secrets/splatvote-prod/vote-db-secrets.enc.yaml`
   - `secrets/splatvote-prod/regcred.enc.yaml`

2. Optional flags:

   ```bash
   # Skip DB provisioning (just regenerate secrets)
   uv run python scripts/provision_splatvote_secrets.py --skip-db

   # Skip regcred generation
   uv run python scripts/provision_splatvote_secrets.py --skip-regcred

   # Persist generated values into .env
   uv run python scripts/provision_splatvote_secrets.py --write-env .env --print-admin-token
   ```

The script reads:
- `BOT_DB_ADMIN_URL` (required for DB provisioning; defaults to .env)
- `DO_REGISTRY_READ_TOKEN` (for regcred)
- `SPLATVOTE_DB_USER`, `SPLATVOTE_DB_PASSWORD`, `SPLATVOTE_DB_NAME`, `SPLATVOTE_DB_HOST`, `SPLATVOTE_DB_PORT`
- `SPLATVOTE_VOTE_IP_PEPPER`, `SPLATVOTE_ADMIN_TOKEN_PEPPER`, `SPLATVOTE_ADMIN_TOKEN`

If peppers/admin token are missing, the script generates them and prints the admin token once.
Commit only the encrypted `.enc.yaml` files.
