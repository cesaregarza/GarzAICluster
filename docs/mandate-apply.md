# Mandate Apply

`scripts/mandate_apply.py` is the first local CES-123 enablement planner for
Mandate workload capabilities. It consumes one declarative
`MandateWorkloadEnablement` YAML document and reconciles only the
deployment-owned edges that SplatTopConfig is allowed to own.

Dry-run is the default. Use `--write` only on a branch intended for a normal PR.
The tool never mutates the live cluster, never edits SOPS secret values, never
mints workload identity tokens, and never changes workload-release-owned facts.

## Example

```yaml
schema_version: mandate-workload-enablement.v1
kind: MandateWorkloadEnablement
workload: data.workspace_probe
capability: agent_workloads.readonly_query
grant:
  binding: private-admin-controlled-capabilities
model_lease:
  allowed_profile: openai.gpt-5.3-codex-spark
worker:
  claims: true
secrets:
  - key: XSCRAPER_READONLY_DATABASE_URL
network:
  - to: private-db-postgresql-nyc3-xscraper-do-user-15543770-0.c.db.ondigitalocean.com:25060
```

Plan without writing:

```bash
uv run python scripts/mandate_apply.py enablement.yaml --repo-root .
```

Write automatic deployment-owned edits and emit a PR body:

```bash
uv run python scripts/mandate_apply.py enablement.yaml \
  --repo-root . \
  --write \
  --output-pr-body .git/mandate-apply-pr-body.md
```

## What It Can Edit

- `policy.prod.yaml` embedded in the agent-control-plane registry overlay:
  append the requested capability to an existing binding.
- `workload_imports.yaml` embedded in the registry overlay: set exactly one
  allowed model profile on a deployment-owned `model_lease`.
- `apps/agent-workloads/values.yaml`: add the capability to a known worker's
  `AGENT_WORKLOADS_WORKER_CAPABILITIES` claim list.

All of these changes still require the normal PR, CI, merge, Argo sync, and
control-plane restart path. The enablement document is not dispatch authority.

## What It Refuses Or Names

- `grant` may only name an existing binding. It cannot add users, actors,
  channels, new bindings, wildcard grants, consequence classes, or policy
  authority.
- `model_lease` may only name one `allowed_profile`. Multiple profiles and other
  model lease fields are refused in this v0.
- `secrets` may only name keys. Missing key references or missing encrypted SOPS
  keys are reported as operator gaps; values are never read or written.
- `network` requests are reported as operator-review gaps because the deployed
  NetworkPolicy uses CIDR and selector rules, not hostname authority.
- Workload-release-owned fields stay in `agent-workloads` and require the normal
  publish, re-pin, and re-mint flow described by `docs/grant-ownership.md`.
