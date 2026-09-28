from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.check_agent_control_plane_provider_pins import (
    MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV,
    BROKER_OPERATION_PROVIDERS_ENV,
    BROKER_PUBLIC_CERTIFICATES_PATH,
    PROVIDER_PINS_ENV,
    ProviderPinGateError,
    check_agent_control_plane_provider_pins,
    _operation_provider_inputs,
    _run_operation_fingerprint,
    _run_provider_fingerprints,
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
WORKER_PINS = {
    "model_gateway": {
        "digest": "sha256:" + "4" * 64,
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
            seen_env: dict[str, list[dict[str, str]]] = {}
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
            self.assertEqual(len(seen_env["control-api"]), 2)
            api_env = next(
                env
                for env in seen_env["control-api"]
                if MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV in env
            )
            worker_env = next(
                env
                for env in seen_env["control-api"]
                if MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV not in env
            )
            self.assertNotIn(PROVIDER_PINS_ENV, api_env)
            self.assertNotIn(PROVIDER_PINS_ENV, worker_env)
            self.assertEqual(
                api_env["AGENT_PLATFORM_READONLY_SQL_DATABASE_URL"],
                "postgresql://provider-pin-check@localhost/provider_pin_check",
            )
            self.assertEqual(
                worker_env["AGENT_PLATFORM_READONLY_SQL_DATABASE_URL"],
                "postgresql://provider-pin-check@localhost/provider_pin_check",
            )
            self.assertEqual(
                api_env[MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV],
                "/var/lib/mandate/codex-auth/auth.json",
            )
            self.assertEqual(len(seen_env["model-gateway"]), 1)
            self.assertEqual(
                seen_env["model-gateway"][0][
                    MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV
                ],
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

    def test_stale_local_worker_pin_reports_expected_and_values_location(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, target_revision = _fake_agent_platform_repo(tmp)
            stale_worker_pins = dict(WORKER_PINS)
            stale_worker_pins["model_gateway"] = {
                "digest": "sha256:" + "8" * 64,
                "protocol": "model_gateway",
            }
            config_repo = _config_repo(
                tmp,
                target_revision=target_revision,
                worker_pins=stale_worker_pins,
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
                f"localWorker.env.{PROVIDER_PINS_ENV}",
                message,
            )
            self.assertIn("sha256:" + "4" * 64, message)
            self.assertIn("sha256:" + "8" * 64, message)

    def test_api_env_pin_override_is_checked_at_override_and_siblings_ignore_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, target_revision = _fake_agent_platform_repo(tmp)
            operation = _operation_descriptor()
            pins = dict(API_PINS)
            pins["citrus_purchasing"] = {
                "digest": "sha256:" + "5" * 64,
                "protocol": "broker-operation-mtls.v1",
            }
            config_repo = _config_repo(
                tmp,
                target_revision=target_revision,
                api_env={
                    BROKER_OPERATION_PROVIDERS_ENV: json.dumps([operation]),
                    PROVIDER_PINS_ENV: _pins_json(pins),
                },
            )
            _write_public_manifest(config_repo)
            seen: dict[str, list[dict[str, str]]] = {}

            check_agent_control_plane_provider_pins(
                repo_root=config_repo,
                agent_platform_repo=platform_repo,
                fingerprint_runner=_fingerprint_runner(seen),
                operation_fingerprint_runner=lambda *_: "sha256:" + "5" * 64,
            )
            api_env = seen["control-api"][0]
            worker_env = seen["control-api"][1]
            self.assertIn(BROKER_OPERATION_PROVIDERS_ENV, api_env)
            self.assertNotIn(BROKER_OPERATION_PROVIDERS_ENV, worker_env)
            self.assertNotIn(
                BROKER_OPERATION_PROVIDERS_ENV, seen["model-gateway"][0]
            )

            stale = dict(pins)
            stale["citrus_purchasing"] = {
                "digest": "sha256:" + "6" * 64,
                "protocol": "broker-operation-mtls.v1",
            }
            values_path = config_repo / "apps/agent-control-plane/values.yaml"
            values = YAML_PARSER.load(values_path.read_text())
            values["apiEnv"][PROVIDER_PINS_ENV] = _pins_json(stale)
            _write_yaml(values_path, values)
            with self.assertRaises(ProviderPinGateError) as raised:
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=_fingerprint_runner({}),
                    operation_fingerprint_runner=lambda *_: "sha256:" + "5" * 64,
                )
            self.assertIn(f"apiEnv.{PROVIDER_PINS_ENV}", str(raised.exception))

            collision_operation = dict(operation, broker_id="model_gateway")
            values["apiEnv"][BROKER_OPERATION_PROVIDERS_ENV] = json.dumps([collision_operation])
            _write_yaml(values_path, values)
            _write_public_manifest(config_repo, provider_id="model_gateway")
            with self.assertRaisesRegex(ProviderPinGateError, "conflicts with base provider pin"):
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=_fingerprint_runner({}),
                    operation_fingerprint_runner=lambda *_: "sha256:" + "5" * 64,
                )

    def test_operation_manifest_requires_exact_ids_and_safe_public_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            platform_repo, target_revision = _fake_agent_platform_repo(tmp)
            config_repo = _config_repo(
                tmp,
                target_revision=target_revision,
                api_env={BROKER_OPERATION_PROVIDERS_ENV: json.dumps([_operation_descriptor()])},
            )
            fake = _fingerprint_runner({})
            with self.assertRaisesRegex(ProviderPinGateError, "mapping is required"):
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=fake,
                    operation_fingerprint_runner=lambda *_: "sha256:" + "5" * 64,
                )

            _write_public_manifest(config_repo, extra_provider=True)
            with self.assertRaisesRegex(ProviderPinGateError, "exactly match"):
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=fake,
                    operation_fingerprint_runner=lambda *_: "sha256:" + "5" * 64,
                )

            _write_public_manifest(config_repo, ca_file="apps/agent-control-plane/public/missing.pem")
            with self.assertRaisesRegex(ProviderPinGateError, "file missing or unsafe"):
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=fake,
                    operation_fingerprint_runner=lambda *_: "sha256:" + "5" * 64,
                )

            _write_public_manifest(config_repo, ca_file="../outside.pem")
            with self.assertRaisesRegex(ProviderPinGateError, "path unsafe"):
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=fake,
                    operation_fingerprint_runner=lambda *_: "sha256:" + "5" * 64,
                )

            _write_public_manifest(config_repo, private_key=True)
            with self.assertRaisesRegex(ProviderPinGateError, "unsafe PEM material"):
                check_agent_control_plane_provider_pins(
                    repo_root=config_repo,
                    agent_platform_repo=platform_repo,
                    fingerprint_runner=fake,
                    operation_fingerprint_runner=lambda *_: "sha256:" + "5" * 64,
                )

    def test_operation_fingerprint_subprocess_contract_is_offline_and_sanitized(self) -> None:
        core_repo = Path("/tmp/selected core checkout")
        config = _operation_descriptor()
        ca_pem = b"-----BEGIN CERTIFICATE-----\nY2E=\n-----END CERTIFICATE-----\n"
        cert_pem = b"-----BEGIN CERTIFICATE-----\nY2xpZW50\n-----END CERTIFICATE-----\n"
        expected_digest = "sha256:" + "a" * 64
        with patch.dict(os.environ, {"AGENT_PLATFORM_PRIVATE_SENTINEL": "do-not-forward"}):
            with patch("scripts.check_agent_control_plane_provider_pins.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = json.dumps({
                    "digest": expected_digest,
                    "protocol": "broker-operation-mtls.v1",
                })
                run.return_value.stderr = ""
                digest = _run_operation_fingerprint(config, ca_pem, cert_pem, core_repo)
        self.assertEqual(digest, expected_digest)
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ["uv", "--directory", str(core_repo), "run"])
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["config"], config)
        self.assertEqual(base64.b64decode(payload["ca_pem_base64"]), ca_pem)
        self.assertEqual(base64.b64decode(payload["client_cert_pem_base64"]), cert_pem)
        self.assertNotIn("AGENT_PLATFORM_PRIVATE_SENTINEL", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.kwargs["check"], False)
        first_payload = payload

        changed_digest = "sha256:" + "b" * 64
        with patch("scripts.check_agent_control_plane_provider_pins.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps({
                "digest": changed_digest,
                "protocol": "broker-operation-mtls.v1",
            })
            changed_ca = b"-----BEGIN CERTIFICATE-----\nY2E=\n-----END CERTIFICATE-----\n\n"
            digest = _run_operation_fingerprint(config, changed_ca, cert_pem, core_repo)
        self.assertNotEqual(digest, expected_digest)
        changed_payload = json.loads(run.call_args.kwargs["input"])
        self.assertNotEqual(
            first_payload["ca_pem_base64"], changed_payload["ca_pem_base64"]
        )

        with patch("scripts.check_agent_control_plane_provider_pins.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = "private environment output"
            run.return_value.stderr = "private environment detail"
            with self.assertRaises(ProviderPinGateError) as raised:
                _run_operation_fingerprint(config, ca_pem, cert_pem, core_repo)
        self.assertNotIn("private environment", str(raised.exception))

        with patch("scripts.check_agent_control_plane_provider_pins.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps({
                "digest": "bad-digest",
                "protocol": "broker-operation-mtls.v1",
            })
            with self.assertRaisesRegex(ProviderPinGateError, "invalid digest"):
                _run_operation_fingerprint(config, ca_pem, cert_pem, core_repo)

    def test_operation_provider_json_and_manifest_reject_duplicates_and_stale_entries(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo = Path(raw_tmp)
            duplicate_field = (
                '[{"broker_id":"one","broker_id":"two","endpoint":"https://x",'
                '"ca_file":"/ca","client_cert_file":"/cert","client_key_file":"/key"}]'
            )
            with self.assertRaisesRegex(ProviderPinGateError, "unique-key JSON"):
                _operation_provider_inputs(
                    repo_root=repo,
                    api_env={BROKER_OPERATION_PROVIDERS_ENV: duplicate_field},
                )

            duplicate_ids = [_operation_descriptor(), _operation_descriptor()]
            with self.assertRaisesRegex(ProviderPinGateError, "duplicate broker ID"):
                _operation_provider_inputs(
                    repo_root=repo,
                    api_env={BROKER_OPERATION_PROVIDERS_ENV: json.dumps(duplicate_ids)},
                )

            _write_public_manifest(repo)
            with self.assertRaisesRegex(ProviderPinGateError, "exactly match"):
                _operation_provider_inputs(
                    repo_root=repo,
                    api_env={BROKER_OPERATION_PROVIDERS_ENV: "[]"},
                )

            manifest = repo / BROKER_PUBLIC_CERTIFICATES_PATH
            manifest.write_text(
                "schema_version: broker-provider-public-certificates.v1\n"
                "schema_version: broker-provider-public-certificates.v1\n"
                "providers: {}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProviderPinGateError, "YAML is invalid"):
                _operation_provider_inputs(
                    repo_root=repo,
                    api_env={BROKER_OPERATION_PROVIDERS_ENV: "[]"},
                )

    def test_public_certificate_paths_and_mapping_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            repo = Path(raw_tmp)
            _write_public_manifest(repo, ca_file="/etc/passwd")
            env = {BROKER_OPERATION_PROVIDERS_ENV: json.dumps([_operation_descriptor()])}
            with self.assertRaisesRegex(ProviderPinGateError, "path unsafe"):
                _operation_provider_inputs(repo_root=repo, api_env=env)

            _write_public_manifest(repo, ca_file="apps/agent-control-plane/public/ca-link.pem")
            public_dir = repo / "apps/agent-control-plane/public"
            (public_dir / "ca-link.pem").symlink_to(public_dir / "ca.pem")
            with self.assertRaisesRegex(ProviderPinGateError, "path unsafe"):
                _operation_provider_inputs(repo_root=repo, api_env=env)

            _write_public_manifest(repo, ca_file="apps/agent-control-plane/public-link/ca.pem")
            (repo / "apps/agent-control-plane/public-link").symlink_to(public_dir)
            with self.assertRaisesRegex(ProviderPinGateError, "path unsafe"):
                _operation_provider_inputs(repo_root=repo, api_env=env)

            _write_public_manifest(repo, extra_private_field=True)
            with self.assertRaisesRegex(ProviderPinGateError, "entry invalid"):
                _operation_provider_inputs(repo_root=repo, api_env=env)

            _write_public_manifest(repo)
            ca_path = public_dir / "ca.pem"
            ca_path.write_bytes(b"preamble\n-----BEGIN CERTIFICATE-----\nY2E=\n-----END CERTIFICATE-----\n")
            with self.assertRaisesRegex(ProviderPinGateError, "unsafe PEM material"):
                _operation_provider_inputs(repo_root=repo, api_env=env)

            ca_path.write_bytes(b"-----BEGIN CERTIFICATE-----\nnot-base64!\n-----END CERTIFICATE-----\n")
            with self.assertRaisesRegex(ProviderPinGateError, "unsafe PEM material"):
                _operation_provider_inputs(repo_root=repo, api_env=env)

    def test_base_api_fingerprint_subprocess_drops_operation_provider_env(self) -> None:
        env = {BROKER_OPERATION_PROVIDERS_ENV: "[]"}
        with patch("scripts.check_agent_control_plane_provider_pins.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "{}"
            run.return_value.stderr = ""
            _run_provider_fingerprints("control-api", env, Path("/unused"))
        self.assertNotIn(BROKER_OPERATION_PROVIDERS_ENV, run.call_args.kwargs["env"])

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
    seen_env: dict[str, list[dict[str, str]]],
) -> Any:
    def run(process: str, env: dict[str, str], _agent_platform_repo: Path) -> str:
        seen_env.setdefault(process, []).append(dict(env))
        if process == "control-api":
            pins = (
                API_PINS
                if MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV in env
                else WORKER_PINS
            )
            return _pins_json(pins)
        if process == "model-gateway":
            return _pins_json(GATEWAY_PINS)
        raise AssertionError(f"unexpected process: {process}")

    return run


def _operation_descriptor() -> dict[str, str]:
    return {
        "broker_id": "citrus_purchasing",
        "endpoint": "https://broker.example/v1/execute",
        "ca_file": "/var/run/broker/ca.crt",
        "client_cert_file": "/var/run/broker/client.crt",
        "client_key_file": "/var/run/broker/client.key",
    }


def _write_public_manifest(
    repo: Path,
    *,
    ca_file: str = "apps/agent-control-plane/public/ca.pem",
    extra_provider: bool = False,
    private_key: bool = False,
    extra_private_field: bool = False,
    provider_id: str = "citrus_purchasing",
) -> None:
    public_dir = repo / "apps/agent-control-plane/public"
    public_dir.mkdir(parents=True, exist_ok=True)
    ca_bytes = (
        b"-----BEGIN PRIVATE KEY-----\nY2E=\n-----END PRIVATE KEY-----\n"
        if private_key
        else b"-----BEGIN CERTIFICATE-----\nY2E=\n-----END CERTIFICATE-----\n"
    )
    (public_dir / "ca.pem").write_bytes(ca_bytes)
    (public_dir / "client.pem").write_bytes(
        b"-----BEGIN CERTIFICATE-----\nY2xpZW50\n-----END CERTIFICATE-----\n"
    )
    providers: dict[str, Any] = {
        provider_id: {
            "ca_file": ca_file,
            "client_cert_file": "apps/agent-control-plane/public/client.pem",
        }
    }
    if extra_private_field:
        providers[provider_id]["client_key_file"] = "apps/agent-control-plane/public/client.pem"
    if extra_provider:
        providers["stale"] = {
            "ca_file": "apps/agent-control-plane/public/ca.pem",
            "client_cert_file": "apps/agent-control-plane/public/client.pem",
        }
    _write_yaml(
        repo / BROKER_PUBLIC_CERTIFICATES_PATH,
        {"schema_version": "broker-provider-public-certificates.v1", "providers": providers},
    )


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
    worker_pins: dict[str, Any] | None = None,
    api_env: dict[str, str] | None = None,
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
            **({"apiEnv": api_env} if api_env is not None else {}),
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
            "localWorker": {
                "env": {
                    PROVIDER_PINS_ENV: _pins_json(worker_pins or WORKER_PINS),
                },
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
        "localWorker": {
            "env": {},
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
