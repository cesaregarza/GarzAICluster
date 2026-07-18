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

- `provision_bot_db.py` – connects to Postgres via `psql`, creates a schema + login limited to that schema, grants a read-only role (default: `readonly`) usage/select on the schema with default privileges, and (by default) gives both roles SELECT/USAGE on the shared `common` schema. Optionally write the secret manifest:

  ```bash
  BOT_DB_ADMIN_URL=postgresql://admin:***@private-db:25060/xscraper?sslmode=require \
    uv run python scripts/provision_bot_db.py my-cool-bot \
      --secret-file secrets/bots/my-cool-bot/db-secret.enc.yaml
  sops --encrypt --in-place secrets/bots/my-cool-bot/db-secret.enc.yaml
  ```

- `provision_agent_control_plane_secrets.py` – provisions the Agent Control
  Plane Postgres schema/role, generates service tokens, and writes encrypted
  runtime and registry secrets under `secrets/agent-control-plane/`. It loads
  `.env` and expects `BOT_DB_ADMIN_URL` plus `DO_REGISTRY_READ_TOKEN` unless
  explicit flags are supplied:

  ```bash
  SOPS_AGE_KEY_FILE=keys/age-private.txt \
    uv run python scripts/provision_agent_control_plane_secrets.py
  ```

- `provision_agent_control_plane_readonly_sql.py` – creates or rotates only the
  Agent Control Plane read-only SQL broker role and stores the selected database
  URL in the encrypted runtime secret without rotating service tokens. The
  default `bots` target writes `AGENT_PLATFORM_READONLY_SQL_DATABASE_URL`; the
  `xscraper_analytical` target writes
  `AGENT_PLATFORM_READONLY_SQL_ANALYTICAL_DATABASE_URL`. It grants `CONNECT`,
  schema `USAGE`, and `SELECT` on the configured relations only:

  ```bash
  SOPS_AGE_KEY_FILE=keys/age-private.txt \
    uv run python scripts/provision_agent_control_plane_readonly_sql.py
  ```

  ```bash
  SOPS_AGE_KEY_FILE=keys/age-private.txt \
    AGENT_CONTROL_PLANE_READONLY_SQL_TARGET=xscraper_analytical \
    XSCRAPER_DB_ADMIN_URL=postgresql://admin:***@private-db:25060/xscraper?sslmode=require \
    uv run python scripts/provision_agent_control_plane_readonly_sql.py
  ```

- `provision_agent_workloads_secrets.py` – provisions the Agent Workloads
  workspace schema/role, writes the worker-service token into both the
  Agent Workloads runtime secret and the Agent Control Plane runtime secret,
  and encrypts the namespace registry pull secret:

  ```bash
  SOPS_AGE_KEY_FILE=keys/age-private.txt \
    uv run python scripts/provision_agent_workloads_secrets.py
  ```

  Use `--read-schema <schema>` only when a workload needs explicit read-only
  source-schema access in addition to owning the `agent_workloads` schema.

- `validate_prometheus_config.py` – renders the Prometheus ConfigMaps from the Helm chart (`helm template --show-only …`) and runs `promtool check config/rules` inside a Docker container. Example (prod values):

  ```bash
  uv run python scripts/validate_prometheus_config.py --values helm/garz-observability/values-prod.yaml
  ```

  Add `--allow-missing` if you want the script to exit successfully when monitoring is disabled for a given values file.

- `check_agent_control_plane_registry_compat.py` – materializes the live
  Agent Control Plane registry overlay into a checked-out `agent-platform`
  source tree and builds the pinned revision's
  `RegistrySnapshot.from_repo(environment="prod")`. Use this when a registry
  overlay or policy change must be proven compatible with the deployed Mandate
  binary:

  ```bash
  uv run python scripts/check_agent_control_plane_registry_compat.py \
    --agent-platform-repo ../agent-platform
  ```

  The `agent-platform` checkout must be at the exact `targetRevision` declared
  in `argocd/applications/agent-control-plane.yaml`. Use
  `--print-target-revision` to retrieve that SHA for automation.

- `mandate_apply.py` – plans or writes a local CES-123
  `MandateWorkloadEnablement` document into deployment-owned files only. Dry-run
  is the default; `--write` edits files for a normal PR. It never reads or
  writes secret values, never mutates live Kubernetes objects, and reports SOPS
  or NetworkPolicy work as operator gaps:

  ```bash
  uv run python scripts/mandate_apply.py enablement.yaml --repo-root .
  uv run python scripts/mandate_apply.py enablement.yaml \
    --repo-root . \
    --write \
    --output-pr-body .git/mandate-apply-pr-body.md
  ```

  See `docs/mandate-apply.md` for the document schema and boundaries.

- `bootstrap_bot.py` – scaffolds a bot entry (apps/bots YAML), secrets folder (README/kustomization/ksops), and copies the DB CA into the shared chart. Examples:

  ```bash
  uv run python scripts/bootstrap_bot.py my-cool-bot \
    --chart-path apps/agent-8s \
    --values-file apps/agent-8s/values.dev.yaml
  ```

  Follow up with `onboard_bot_secret.py` and `provision_bot_db.py` to generate encrypted secrets.

## Adding New Scripts

1. Place them in this directory (subfolders allowed).
2. Prefer Python with no external dependencies beyond the standard library (or document the requirements in the script header).
3. If the script is referenced by CI, ensure `.github/workflows/validate.yaml` installs the prerequisites.
4. Document usage examples in this README so other contributors know how to run them.
