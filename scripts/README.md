# Scripts

Utilities that were previously bundled with the app repo move here when they are infrastructure-focused or referenced by config-repo CI. Use [uv](https://docs.astral.sh/uv/) (`uv run python ...`) so the dependencies defined in `pyproject.toml` are installed automatically. Scripts automatically load variables from `.env` (override with `SPLATTOPCONFIG_ENV_FILE`) before reading other environment values.

## Available

- `onboard_bot_secret.py` – scaffolds/ encrypts a Discord bot token under `secrets/bots/<bot>/token.enc.yaml`. Example:

  ```bash
  uv run python scripts/onboard_bot_secret.py my-cool-bot "DISCORD_TOKEN"
  # or set BOT_TOKEN in .env and omit the argument:
  uv run python scripts/onboard_bot_secret.py my-cool-bot
  ```

  Commit the resulting `.enc.yaml` and let the `splattop-bot-*-secret` ApplicationSet sync it.

- `provision_bot_db.py` – connects to Postgres via `psql`, creates a schema + login limited to that schema, and prints the bot’s connection string. Optionally write the secret manifest:

  ```bash
  BOT_DB_ADMIN_URL=postgresql://admin:***@private-db:25060/xscraper?sslmode=require \
    uv run python scripts/provision_bot_db.py my-cool-bot \
      --secret-file secrets/bots/my-cool-bot/db-secret.enc.yaml
  sops --encrypt --in-place secrets/bots/my-cool-bot/db-secret.enc.yaml
  ```

- `validate_prometheus_config.py` – renders the Prometheus ConfigMaps from the Helm chart (`helm template --show-only …`) and runs `promtool check config/rules` inside a Docker container. Example (prod values):

  ```bash
  uv run python scripts/validate_prometheus_config.py --values helm/splattop/values-prod.yaml
  ```

  Add `--allow-missing` if you want the script to exit successfully when monitoring is disabled for a given values file.

## Adding New Scripts

1. Place them in this directory (subfolders allowed).
2. Prefer Python with no external dependencies beyond the standard library (or document the requirements in the script header).
3. If the script is referenced by CI, ensure `.github/workflows/validate.yaml` installs the prerequisites.
4. Document usage examples in this README so other contributors know how to run them.
