# Release & Promotion Workflow

This doc describes the target-state flow once the config repo owns deployments. Every deploy should be reproducible, digest-pinned, and traceable via PR history.

## Overview

1. App repo merge triggers CI → build images per service → publish tags & digests → emit `component-tags.json`.
2. Automation (GitHub App or Actions workflow) consumes the artifact and opens a PR in this repo that bumps only the affected services’ digests/tags under `helm/splattop/values-*.yaml`.
3. Config repo CI validates manifests (helm lint/template, kubeconform, Prometheus rule checks, optional OPA/Kyverno tests).
4. Review + merge rules:
   - Citrus dev: successful new dev builds opt in to automatic image PR merge after all required checks. See [image PR automation](image-pr-automerge.md) for eligibility, installation and rollback. Other apps remain manual until explicitly opted in.
   - Staging/Prod: require human review (platform DRI) + green CI.
5. Post-merge, Argo syncs dev automatically; staging/prod either auto-sync with gates or require manual sync, depending on `argo-operations.md`.

## Artifacts & Signals

- `component-tags.json` structure:

```json
{
  "api":    { "tag": "1.7.3", "sha": "abc1234", "digest": "sha256:..." },
  "web":    { "tag": "2.4.0", "sha": "abc1234", "digest": "sha256:..." },
  "worker": { "tag": "0.9.1", "sha": "abc1234", "digest": "sha256:..." }
}
```

- Stored as a workflow artifact plus job summary for humans.
- Automation references this file to update `values-{env}.yaml`. Tags remain in the values files for readability, but Argo deploys by digest.

## Rollbacks

1. Identify the last-good config repo commit (or tag).
2. `git revert` the digest bump commit (never rebuild images).
3. Merge the revert PR (staging/prod still require review).
4. Trigger Argo sync (manual for prod if required). Verify:
   - `argocd app wait splattop-prod --health`.
   - `kubectl get pods -n prod` to ensure rollout completes.
5. Document the incident in `docs/argo-operations.md` (game day log) + retro issue.

Goal: rollback ≤ 5 minutes from revert merge to healthy status.

## Hotfix Path

1. Cherry-pick/apply fix in app repo; merge into main (or hotfix branch).
2. CI builds only impacted services and updates `component-tags.json`.
3. Automation opens “Hotfix” PR here bumping relevant digests.
4. Staging review is mandatory but can be expedited (pager/on-call).
5. After merge, manually trigger prod Argo sync (or rely on auto with confirmation).
6. Follow up with a postmortem + ensure tests cover the regression.

## Policy Guardrails

- No post-merge mutations in app repo (`main` must match built images; i18n copies happen pre-build or in Dockerfile).
- Config repo merges only via reviewed PRs (no direct push to `main`).
- CI enforces digest-only manifests (`conftest` / Kyverno tests output error on mutable tags).
- CI must require `agent-control-plane-deployed-registry-compat` for PRs that
  can affect Mandate registry overlay, policy, or control-plane values. The
  check validates the PR's config against the `agent-platform` `targetRevision`
  selected by the same PR, so a registry shape that the deployed binary cannot
  boot is unmergeable.
- The projection-contract reader accepts canonical-only Core after its temporary
  aliases are removed. A present but malformed alias declaration still fails
  closed; generic cards must name registered schemas and explicit released fields.
- CODEOWNERS require platform review for `envs/staging/**` and `envs/prod/**`.
- Bot PRs must label themselves (e.g., `automation:release-bump`) for auditability.

## Verification After Each Deploy

For a Mandate Core pin update, preview and then apply the owned references with
the repository helper. It updates the values tag/digest, Argo source revision,
postgres sweep image, current release reference and runnable restore snippet,
and deploy-train contract fixture as one validated operation:

```bash
uv run python scripts/check_control_plane_release_pin.py \
  --repo-root . \
  --source-sha d3d4d2f955805fd66da131f29cd3bec108a27f75 \
  --image-digest sha256:a62a0b6d3608d810dfb1bf0fe82b0a4bf35aaa668b7f097ae05f3e9106441008
uv run python scripts/check_control_plane_release_pin.py \
  --repo-root . \
  --source-sha d3d4d2f955805fd66da131f29cd3bec108a27f75 \
  --image-digest sha256:a62a0b6d3608d810dfb1bf0fe82b0a4bf35aaa668b7f097ae05f3e9106441008 \
  --apply
```

- [ ] Argo shows `Synced` & `Healthy`.
- [ ] `kubectl get deployment <svc>` shows new digest.
- [ ] For a Mandate control-plane, registry-overlay, or workload deploy, start
      a fresh Job from `cronjob/agent-control-plane-synthetic-live-verify`,
      wait for it to complete, and retain its logs. Completion must include the
      `readonly-query-skill-digests` journey with required
      `model_call.finished`, proving a real external worker MODEL-capability
      round-trip; `mandate.deploy.smoke` alone is insufficient.
      ```bash
      verify_job="agent-control-plane-postdeploy-$(date -u +%Y%m%d%H%M%S)"
      kubectl -n agent-control-plane create job \
        --from=cronjob/agent-control-plane-synthetic-live-verify "$verify_job"
      kubectl -n agent-control-plane wait \
        --for=condition=complete --timeout=8m "job/$verify_job"
      kubectl -n agent-control-plane logs "job/$verify_job"
      ```
- [ ] Update release log (GitHub release notes or `docs/release-log.md` TBD).

If any verification fails, run the rollback steps above and document findings.
