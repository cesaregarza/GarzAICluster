# Infra Docs Overview

These documents capture the canonical workflows for the SplatTop config repository. Read them in roughly this order when contributing here:

1. `bootstrap.md` – bring-up steps for a fresh cluster + Argo that points to this repo.
2. `release-workflow.md` – how images flow from the app repo into environment value bumps, including hotfixes/rollbacks.
3. `argo-operations.md` – AppProject guardrails, policy enforcement, and day-to-day Argo duties.
4. `domains-and-hostnames.md` – public hostname rollout checklist, including multi-zone external-dns changes.
5. `secrets-strategy.md` – SOPS + Age plan, rotation, and CI/Dev ergonomics.
6. `mandate-apply.md` – local declarative enablement planner for Mandate
   workload capability wiring.
7. `developer-cheat-sheet.md` – quick reference for common commands/tasks after the split.
8. `k8s/secrets.template.yaml` → `k8s/secrets.enc.yaml` – example flow for encrypted secrets.

Supplemental references:

- `docs/config_repo_split_plan.md` (in the app repo) – migration backlog until cutover completes.
- GitHub issues labeled `config-repo` – track outstanding automation (CI PR bump bot, policy enforcement, etc.).

Historical Mandate/OpenClaw rollout evidence relocated from the generic
`agent-platform` repository:

- `historical/agent-platform/openclaw-mcp-rollout-2026-05-27.md` – dated
  rollout record for the first OpenClaw MCP integration.
- `historical/agent-platform/openclaw-mvp-deployment.md` – original
  OpenClaw/Mandate MVP deployment notes before the generic platform copy was
  reduced to reusable guidance.
