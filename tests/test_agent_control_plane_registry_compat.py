from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_agent_control_plane_registry_compat.py"
YAML_PARSER = YAML()


class AgentControlPlaneRegistryCompatTests(unittest.TestCase):
    def test_missing_per_user_daily_tokens_fails_against_old_pin_and_passes_after_bump(
        self,
    ) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, old_sha, new_sha = _fake_agent_platform_repo(tmp)
            config_repo = _config_repo(
                tmp,
                target_revision=old_sha,
                include_per_user_daily_tokens=False,
            )

            _git(platform_repo, "checkout", "--quiet", old_sha)
            old_result = _run_gate(config_repo, platform_repo)
            self.assertNotEqual(old_result.returncode, 0)
            self.assertIn(
                "policy aggregate_budget per_user_daily_tokens is invalid",
                old_result.stderr,
            )

            _write_control_plane_application(config_repo, new_sha)
            _git(platform_repo, "checkout", "--quiet", new_sha)
            new_result = _run_gate(config_repo, platform_repo)
            self.assertEqual(new_result.returncode, 0, new_result.stderr)
            self.assertIn(
                f"compatible with agent-platform {new_sha}",
                new_result.stdout,
            )

    def test_two_allowed_profiles_fails_with_named_registry_error(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, _old_sha, new_sha = _fake_agent_platform_repo(tmp)
            _git(platform_repo, "checkout", "--quiet", new_sha)
            config_repo = _config_repo(
                tmp,
                target_revision=new_sha,
                include_per_user_daily_tokens=True,
                allowed_profiles=["openai.first", "openai.second"],
            )

            result = _run_gate(config_repo, platform_repo)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "worker_service model call lease must declare exactly one allowed profile",
                result.stderr,
            )

    def test_agent_platform_checkout_must_match_argocd_target_revision(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, old_sha, new_sha = _fake_agent_platform_repo(tmp)
            _git(platform_repo, "checkout", "--quiet", old_sha)
            config_repo = _config_repo(
                tmp,
                target_revision=new_sha,
                include_per_user_daily_tokens=True,
            )

            result = _run_gate(config_repo, platform_repo)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("agent-platform checkout revision mismatch", result.stderr)

    def test_print_target_revision_reads_pr_tree_without_platform_checkout(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            _platform_repo, _old_sha, new_sha = _fake_agent_platform_repo(tmp)
            config_repo = _config_repo(
                tmp,
                target_revision=new_sha,
                include_per_user_daily_tokens=True,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--repo-root",
                    str(config_repo),
                    "--print-target-revision",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), new_sha)


def _run_gate(config_repo: Path, platform_repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(config_repo),
            "--agent-platform-repo",
            str(platform_repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _fake_agent_platform_repo(tmp: Path) -> tuple[Path, str, str]:
    repo = tmp / "agent-platform"
    (repo / "mandate" / "core").mkdir(parents=True)
    (repo / "mandate" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "mandate" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "mandate" / "core" / "registry.py").write_text(
        textwrap.dedent(
            """
            from pathlib import Path

            from ruamel.yaml import YAML


            YAML_PARSER = YAML(typ="safe")


            class RegistryError(ValueError):
                pass


            class RegistrySnapshot:
                @classmethod
                def from_repo(cls, repo_root, *, environment="dev"):
                    root = Path(repo_root)
                    rules = YAML_PARSER.load((root / "compat_rules.yaml").read_text())
                    policy = YAML_PARSER.load(
                        (root / "registries" / "policy.prod.yaml").read_text()
                    )
                    aggregate = (
                        ((policy or {}).get("defaults") or {}).get("aggregate_budget")
                        or {}
                    )
                    if rules.get("require_per_user_daily_tokens") and (
                        "per_user_daily_tokens" not in aggregate
                    ):
                        raise RegistryError(
                            "policy aggregate_budget per_user_daily_tokens is invalid"
                        )

                    workload_imports = YAML_PARSER.load(
                        (root / "registries" / "workload_imports.yaml").read_text()
                    )
                    for import_entry in workload_imports.get("imports") or []:
                        for capability_id, capability in (
                            import_entry.get("capabilities") or {}
                        ).items():
                            profiles = (
                                (capability.get("model_lease") or {}).get(
                                    "allowed_profiles"
                                )
                                or []
                            )
                            if len(profiles) != 1:
                                raise RegistryError(
                                    "worker_service model call lease must declare "
                                    f"exactly one allowed profile: {capability_id}"
                                )
                    return cls()
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (repo / "registries").mkdir()
    (repo / "registries" / "policy.base.yaml").write_text("defaults: {}\n", encoding="utf-8")
    (repo / "registries" / "capabilities.yaml").write_text("capabilities: []\n", encoding="utf-8")
    (repo / "registries" / "agents.yaml").write_text("agents: []\n", encoding="utf-8")
    (repo / "registries" / "models.yaml").write_text("model_tiers: []\nmodel_profiles: []\n", encoding="utf-8")
    (repo / "registries" / "evals.yaml").write_text("eval_suites: []\n", encoding="utf-8")

    _write_yaml(repo / "compat_rules.yaml", {"require_per_user_daily_tokens": True})
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "old validator")
    old_sha = _git(repo, "rev-parse", "HEAD")

    _write_yaml(repo / "compat_rules.yaml", {"require_per_user_daily_tokens": False})
    _git(repo, "add", "compat_rules.yaml")
    _git(repo, "commit", "--quiet", "-m", "new validator")
    new_sha = _git(repo, "rev-parse", "HEAD")
    return repo, old_sha, new_sha


def _config_repo(
    tmp: Path,
    *,
    target_revision: str,
    include_per_user_daily_tokens: bool,
    allowed_profiles: list[str] | None = None,
) -> Path:
    repo = tmp / f"config-{target_revision[:8]}"
    (repo / "argocd" / "applications").mkdir(parents=True)
    (repo / "apps" / "agent-control-plane-registry-overlay").mkdir(parents=True)
    _write_control_plane_application(repo, target_revision)
    _write_overlay_configmap(
        repo,
        include_per_user_daily_tokens=include_per_user_daily_tokens,
        allowed_profiles=allowed_profiles or ["openai.gpt-5.3-codex-spark"],
    )
    return repo


def _write_control_plane_application(repo: Path, target_revision: str) -> None:
    _write_yaml(
        repo / "argocd" / "applications" / "agent-control-plane.yaml",
        {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Application",
            "spec": {
                "sources": [
                    {
                        "repoURL": "git@github.com:cesaregarza/agent-platform.git",
                        "targetRevision": target_revision,
                        "path": "helm/mandate",
                    },
                    {
                        "repoURL": "https://github.com/cesaregarza/SplatTopConfig",
                        "targetRevision": "main",
                        "ref": "values",
                    },
                ]
            },
        },
    )


def _write_overlay_configmap(
    repo: Path,
    *,
    include_per_user_daily_tokens: bool,
    allowed_profiles: list[str],
) -> None:
    aggregate_budget: dict[str, Any] = {
        "platform_daily_usd": 25.0,
        "per_capability_daily_usd_default": 5.0,
        "per_capability_daily_usd": {"agent_workloads.opencode_propose": 1.0},
    }
    if include_per_user_daily_tokens:
        aggregate_budget["per_user_daily_tokens"] = 50_000
    policy = {
        "defaults": {
            "max_cost_usd_per_job": 0.25,
            "max_runtime_seconds_per_job": 60,
            "aggregate_budget": aggregate_budget,
        },
        "bindings": [],
    }
    workload_imports = {
        "schema_version": "workload-imports.v1",
        "imports": [
            {
                "id": "opencode.proposer",
                "manifest_path": "registries/imports/agent-opencode.proposer.json",
                "manifest_digest": "sha256:" + "a" * 64,
                "image_digest": "sha256:" + "b" * 64,
                "capabilities": {
                    "agent_workloads.opencode_propose": {
                        "model_lease": {"allowed_profiles": allowed_profiles}
                    }
                },
            }
        ],
    }
    data = {
        "workload_imports.yaml": _yaml_text(workload_imports),
        "policy.prod.yaml": _yaml_text(policy),
        "evals.yaml": _yaml_text({"eval_suites": []}),
        "agent-opencode.proposer.json": json.dumps(
            {
                "id": "opencode.proposer",
                "digest": "sha256:" + "a" * 64,
                "image": {"digest": "sha256:" + "b" * 64},
                "code_digest": "sha256:" + "c" * 64,
            }
        ),
    }
    _write_yaml(
        repo
        / "apps"
        / "agent-control-plane-registry-overlay"
        / "configmap.yaml",
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "agent-control-plane-registry-overlay"},
            "data": data,
        },
    )


def _yaml_text(payload: dict[str, Any]) -> str:
    from io import StringIO

    stream = StringIO()
    YAML_PARSER.dump(payload, stream)
    return stream.getvalue()


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_yaml_text(payload), encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
