# splattop-teams Helm Chart

Deploys the SplatTopTeams API, frontend, and daily embedding refresh CronJob.

## Production host

- `teams-int.splat.top`

## Required secret

`global.databaseSecretName` (default `db-secrets`) must provide either:

- `RANKINGS_DATABASE_URL` or `DATABASE_URL`, or
- DB parts (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`).

If `refresh.targetDatabaseSecretName` is set, that secret is also mounted into the
refresh CronJob and can provide `TARGET_DATABASE_URL` for snapshot writes while the
shared DB secret continues to supply source-table read credentials.
