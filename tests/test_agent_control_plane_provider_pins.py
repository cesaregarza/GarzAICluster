from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.check_agent_control_plane_provider_pins import (
    MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV,
    PROVIDER_PINS_ENV,
    ProviderPinGateError,
    check_agent_control_plane_provider_pins,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")
SHARED_ACTION_REF = "a1d2fb4a6b288066574b1ac53074ac62e920a07f"
FETCH_BROKER_ACTION = (
    f"cesaregarza/.github/actions/fetch-broker-credentials@{SHARED_ACTION_REF}"
)
API_PINS = {
    "model_gateway": {
        "digest": "sha256:" + "1" * 64,
        "protocol": "model_gateway",
    },
    "readonly-sql-broker": {
        "digest": "sha256:" + "2" * 64,
        "protocol": "readonly_sql",
    },
}
GATEWAY_PINS = {
    "model_gateway": {
        "digest": "sha256:" + "3" * 64,
        "protocol": "model_gateway",
    },
}


class AgentControlPlaneProviderPinTests(unittest.TestCase):
    def test_provider_pin_gate_accepts_current_digest_only_bump_without_repin(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, target_revision = _fake_agent_platform_repo(tmp)
            config_repo = _config_repo(tmp, target_revision=target_revision)
            seen_env: dict[str, dict[str, str]] = {}
            seen_images: list[str] = []

            result = check_agent_control_plane_provider_pins(
                repo_root=config_repo,
                agent_platform_repo=platform_repo,
                check_image_exists=True,
                fingerprint_runner=_fingerprint_runner(seen_env),
                image_checker=seen_images.append,
            )

            self.assertIn(f"agent-platform {target_revision}", result)
            self.assertEqual(
                seen_images,
                [
                    "registry.digitalocean.com/sendouq/agent-platform:"
                    f"sha-{target_revision[:12]}"
                ],
            )
            self.assertNotIn(PROVIDER_PINS_ENV, seen_env["control-api"])
            self.assertEqual(
                seen_env["control-api"]["AGENT_PLATFORM_READONLY_SQL_DATABASE_URL"],
                "postgresql://provider-pin-check@localhost/provider_pin_check",
            )
            self.assertEqual(
                seen_env["control-api"][MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV],
                "/var/lib/mandate/codex-auth/auth.json",
            )
            self.assertEqual(
                seen_env["model-gateway"][MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV],
                "/var/lib/mandate/codex-auth/auth.json",
            )

    def test_stale_control_api_pin_reports_expected_and_values_location(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, target_revision = _fake_agent_platform_repo(tmp)
            stale_api_pins = dict(API_PINS)
            stale_api_pins["model_gateway"] = {
                "digest": "sha256:" + "9" * 64,
                "protocol": "model_gateway",
            }
            config_repo = _config_repo(
                tmp,
                target_revision=target_revision,
                api_pins=stale_api_pins,
            )

            with self.assertRaises(ProviderPinGateError) as raised:
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=_fingerprint_runner({}),
                )

            message = str(raised.exception)
            self.assertIn(
                f"apps/agent-control-plane/values.yaml env.{PROVIDER_PINS_ENV}",
                message,
            )
            self.assertIn("sha256:" + "1" * 64, message)
            self.assertIn("sha256:" + "9" * 64, message)

    def test_stale_gateway_pin_reports_expected_and_values_location(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, target_revision = _fake_agent_platform_repo(tmp)
            stale_gateway_pins = {
                "model_gateway": {
                    "digest": "sha256:" + "8" * 64,
                    "protocol": "model_gateway",
                }
            }
            config_repo = _config_repo(
                tmp,
                target_revision=target_revision,
                gateway_pins=stale_gateway_pins,
            )

            with self.assertRaises(ProviderPinGateError) as raised:
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=_fingerprint_runner({}),
                )

            message = str(raised.exception)
            self.assertIn(
                "apps/agent-control-plane/values.yaml "
                f"modelGateway.env.{PROVIDER_PINS_ENV}",
                message,
            )
            self.assertIn("sha256:" + "3" * 64, message)
            self.assertIn("sha256:" + "8" * 64, message)

    def test_target_revision_must_be_agent_platform_main_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, target_revision = _fake_agent_platform_repo(
                tmp,
                target_on_side_branch=True,
            )
            config_repo = _config_repo(tmp, target_revision=target_revision)

            with self.assertRaisesRegex(
                ProviderPinGateError,
                "not an ancestor of origin/main",
            ):
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=_fingerprint_runner({}),
                )

    def test_image_tag_must_match_target_revision_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, target_revision = _fake_agent_platform_repo(tmp)
            config_repo = _config_repo(
                tmp,
                target_revision=target_revision,
                image_tag="sha-deadbeef0000",
            )

            with self.assertRaisesRegex(
                ProviderPinGateError,
                "image.tag must match agent-platform targetRevision",
            ):
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=_fingerprint_runner({}),
                )

    def test_missing_docr_image_tag_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, target_revision = _fake_agent_platform_repo(tmp)
            config_repo = _config_repo(tmp, target_revision=target_revision)

            def missing_image(image_ref: str) -> None:
                raise ProviderPinGateError(f"image tag absent from DOCR: {image_ref}")

            with self.assertRaisesRegex(
                ProviderPinGateError,
                "image tag absent from DOCR",
            ):
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    check_image_exists=True,
                    fingerprint_runner=_fingerprint_runner({}),
                    image_checker=missing_image,
                )

    def test_ci_runs_provider_pin_gate_with_source_and_registry_credentials(
        self,
    ) -> None:
        workflow = YAML_PARSER.load(
            (REPO_ROOT / ".github" / "workflows" / "ci.yaml").read_text()
        )
        job = workflow["jobs"]["agent-control-plane-provider-digest-pins"]
        steps = job["steps"]

        self.assertEqual(job["permissions"]["contents"], "read")
        self.assertEqual(job["permissions"]["id-token"], "write")
        broker_step = next(step for step in steps if step.get("id") == "broker")
        self.assertEqual(broker_step["uses"], FETCH_BROKER_ACTION)
        self.assertIn(
            '"mandate-contracts-read"',
            broker_step["with"]["capabilities"],
        )
        self.assertIn(
            '"digitalocean-registry-read"',
            broker_step["with"]["capabilities"],
        )

        checkout_step = next(
            step
            for step in steps
            if step.get("name") == "Check out pinned agent-platform source"
        )
        self.assertEqual(checkout_step["with"]["fetch-depth"], 0)
        check_step = next(
            step for step in steps if step.get("name") == "Check provider digest pins"
        )
        self.assertIn(
            "scripts/check_agent_control_plane_provider_pins.py",
            check_step["run"],
        )
        self.assertIn("--check-image-exists", check_step["run"])


def _fingerprint_runner(
    seen_env: dict[str, dict[str, str]],
) -> Any:
    def run(process: str, env: dict[str, str], _agent_platform_repo: Path) -> str:
        seen_env[process] = dict(env)
        if process == "control-api":
            return _pins_json(API_PINS)
        if process == "model-gateway":
            return _pins_json(GATEWAY_PINS)
        raise AssertionError(f"unexpected process: {process}")

    return run


def _fake_agent_platform_repo(
    tmp: Path,
    *,
    target_on_side_branch: bool = False,
) -> tuple[Path, str]:
    repo = tmp / ("agent-platform-side" if target_on_side_branch else "agent-platform")
    (repo / "helm" / "mandate").mkdir(parents=True)
    _write_yaml(repo / "helm" / "mandate" / "values.yaml", _chart_values())
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "CI")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "initial chart")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    if target_on_side_branch:
        _git(repo, "checkout", "--quiet", "-b", "candidate")
        (repo / "side.txt").write_text("side\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "--quiet", "-m", "side target")
    else:
        (repo / "digest-only.txt").write_text("no provider change\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "--quiet", "-m", "digest-only bump")
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo, _git(repo, "rev-parse", "HEAD")


def _config_repo(
    tmp: Path,
    *,
    target_revision: str,
    image_tag: str | None = None,
    api_pins: dict[str, Any] | None = None,
    gateway_pins: dict[str, Any] | None = None,
) -> Path:
    repo = tmp / f"config-{target_revision[:8]}"
    application_path = repo / "argocd" / "applications" / "agent-control-plane.yaml"
    values_path = repo / "apps" / "agent-control-plane" / "values.yaml"
    application_path.parent.mkdir(parents=True)
    values_path.parent.mkdir(parents=True)
    _write_yaml(
        application_path,
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
                        "repoURL": "https://github.com/cesaregarza/GarzAICluster",
                        "targetRevision": "main",
                        "ref": "values",
                    },
                ]
            },
        },
    )
    _write_yaml(
        values_path,
        {
            "image": {
                "repository": "registry.digitalocean.com/sendouq/agent-platform",
                "tag": image_tag or f"sha-{target_revision[:12]}",
            },
            "secretKeys": [
                "AGENT_PLATFORM_DATABASE_URL",
                "AGENT_PLATFORM_WORKER_SERVICE_TOKEN",
                "AGENT_PLATFORM_READONLY_SQL_DATABASE_URL",
                "AGENT_PLATFORM_READONLY_SQL_ANALYTICAL_DATABASE_URL",
                "AGENT_PLATFORM_WORKLOAD_IDENTITY_HMAC_SECRET",
                "AGENT_PLATFORM_MODEL_GATEWAY_CODEX_AUTH_JSON",
            ],
            "env": {
                "AGENT_PLATFORM_ENVIRONMENT": "prod",
                PROVIDER_PINS_ENV: _pins_json(api_pins or API_PINS),
                "AGENT_PLATFORM_READONLY_SQL_POOL_MIN_SIZE": "0",
                "AGENT_PLATFORM_READONLY_SQL_POOL_MAX_SIZE": "1",
                "AGENT_PLATFORM_MODEL_GATEWAY_BACKEND": "codex_chatgpt_responses",
                "AGENT_PLATFORM_MODEL_GATEWAY_TIMEOUT_SECONDS": "90",
                "AGENT_PLATFORM_WORKLOAD_IDENTITY_MODE": "hmac",
                "AGENT_PLATFORM_WORKLOAD_IDENTITY_REQUIRED_SCOPES": "worker_service",
                "AGENT_PLATFORM_WORKLOAD_IDENTITY_ALLOWED_SUBJECTS_JSON": (
                    '{"worker_service":["opencode.proposer"]}'
                ),
            },
            "migrations": {
                "enabled": True,
                "disableStartupSchemaMigration": True,
            },
            "skills": {
                "enabled": True,
                "mountPath": "/var/lib/mandate/skills",
            },
            "metrics": {
                "enabled": True,
                "port": 9090,
            },
            "service": {
                "targetPort": 8000,
            },
            "modelGateway": {
                "enabled": True,
                "env": {
                    PROVIDER_PINS_ENV: _pins_json(gateway_pins or GATEWAY_PINS),
                },
                "codexAuthPersistence": {
                    "enabled": True,
                },
            },
        },
    )
    return repo


def _chart_values() -> dict[str, Any]:
    return {
        "env": {},
        "secretKeys": [],
        "migrations": {
            "enabled": False,
            "disableStartupSchemaMigration": False,
        },
        "skills": {
            "enabled": False,
            "mountPath": "/var/lib/mandate/skills",
        },
        "metrics": {
            "enabled": False,
            "port": 9090,
        },
        "service": {
            "targetPort": 8000,
        },
        "modelGateway": {
            "env": {},
            "codexAuthPersistence": {
                "enabled": False,
                "mountPath": "/var/lib/mandate/codex-auth",
                "fileName": "auth.json",
            },
        },
    }


def _pins_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    from io import StringIO

    stream = StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.dump(payload, stream)
    path.write_text(stream.getvalue(), encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            textwrap.dedent(
                f"""
                git {' '.join(args)} failed
                stdout: {result.stdout}
                stderr: {result.stderr}
                """
            ).strip()
        )
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
