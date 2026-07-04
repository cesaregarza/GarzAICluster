#!/usr/bin/env python3
"""Recompute deployed control-plane provider pins from the pinned source tree."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_PATH = Path("argocd/applications/agent-control-plane.yaml")
VALUES_PATH = Path("apps/agent-control-plane/values.yaml")
CHART_VALUES_PATH = Path("helm/mandate/values.yaml")
AGENT_PLATFORM_REPO_URLS = {
    "git@github.com:cesaregarza/agent-platform.git",
    "https://github.com/cesaregarza/agent-platform",
    "https://github.com/cesaregarza/agent-platform.git",
}
FULL_GIT_SHA_RE = r"^[0-9a-f]{40}$"
PROVIDER_PINS_ENV = "AGENT_PLATFORM_PROVIDER_DIGEST_PINS_JSON"
MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV = (
    "AGENT_PLATFORM_MODEL_GATEWAY_CODEX_AUTH_STORE_PATH"
)
MODEL_GATEWAY_CODEX_AUTH_JSON_ENV = "AGENT_PLATFORM_MODEL_GATEWAY_CODEX_AUTH_JSON"
DEFAULT_AGENT_PLATFORM_MAIN_REF = "origin/main"

YAML_PARSER = YAML(typ="safe")

ProviderProcess = Literal["control-api", "model-gateway"]
FingerprintRunner = Callable[[ProviderProcess, Mapping[str, str], Path], str]
ImageChecker = Callable[[str], None]


class ProviderPinGateError(RuntimeError):
    """Raised when the deployed provider-pin gate fails closed."""


@dataclass(frozen=True)
class PinLocation:
    process: ProviderProcess
    value_path: tuple[str, ...]
    env: Mapping[str, str]

    @property
    def label(self) -> str:
        return ".".join(self.value_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute Mandate runtime provider digest pins from the pinned "
            "agent-platform checkout and compare them with the GitOps values."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--application-path", type=Path, default=APPLICATION_PATH)
    parser.add_argument("--values-path", type=Path, default=VALUES_PATH)
    parser.add_argument(
        "--agent-platform-repo",
        type=Path,
        help="Checked-out agent-platform repository at the Argo targetRevision.",
    )
    parser.add_argument(
        "--agent-platform-main-ref",
        default=DEFAULT_AGENT_PLATFORM_MAIN_REF,
        help="Git ref that represents agent-platform main. Defaults to origin/main.",
    )
    parser.add_argument(
        "--check-image-exists",
        action="store_true",
        help="Verify image.repository:image.tag exists in DOCR with crane.",
    )
    parser.add_argument(
        "--image-check-command",
        default="crane",
        help="Registry CLI used for image existence checks. Defaults to crane.",
    )
    parser.add_argument(
        "--print-target-revision",
        action="store_true",
        help="Print the pinned agent-platform targetRevision and exit.",
    )
    parser.add_argument(
        "--print-expected-pins",
        action="store_true",
        help="Print recomputed pin JSON for each values.yaml location and exit.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    try:
        target_revision = agent_platform_target_revision(
            repo_root / args.application_path
        )
        if args.print_target_revision:
            print(target_revision)
            return 0
        if args.agent_platform_repo is None:
            raise ProviderPinGateError("--agent-platform-repo is required")
        if args.print_expected_pins:
            expected = expected_provider_pins(
                repo_root=repo_root,
                agent_platform_repo=args.agent_platform_repo.resolve(),
                application_path=args.application_path,
                values_path=args.values_path,
                agent_platform_main_ref=args.agent_platform_main_ref,
            )
            for location, pins_json in expected.items():
                print(f"{location}={pins_json}")
            return 0
        summary = check_agent_control_plane_provider_pins(
            repo_root=repo_root,
            agent_platform_repo=args.agent_platform_repo.resolve(),
            application_path=args.application_path,
            values_path=args.values_path,
            agent_platform_main_ref=args.agent_platform_main_ref,
            check_image_exists=args.check_image_exists,
            image_checker=_crane_image_checker(args.image_check_command),
        )
    except ProviderPinGateError as exc:
        print(f"agent-control-plane provider pin gate failed: {exc}", file=sys.stderr)
        return 1
    print(summary)
    return 0


def check_agent_control_plane_provider_pins(
    *,
    repo_root: Path,
    agent_platform_repo: Path,
    application_path: Path = APPLICATION_PATH,
    values_path: Path = VALUES_PATH,
    agent_platform_main_ref: str = DEFAULT_AGENT_PLATFORM_MAIN_REF,
    check_image_exists: bool = False,
    fingerprint_runner: FingerprintRunner | None = None,
    image_checker: ImageChecker | None = None,
) -> str:
    application = _load_yaml(repo_root / application_path)
    deployment_values = _load_yaml(repo_root / values_path)
    target_revision = _agent_platform_target_revision(application)
    _validate_target_revision(target_revision)
    _validate_agent_platform_checkout(
        agent_platform_repo=agent_platform_repo,
        expected_revision=target_revision,
        main_ref=agent_platform_main_ref,
    )

    image = _required_mapping(
        deployment_values,
        "image",
        f"{values_path.as_posix()} image",
    )
    image_repository = _required_str(
        image,
        "repository",
        f"{values_path.as_posix()} image",
    )
    image_tag = _required_str(image, "tag", f"{values_path.as_posix()} image")
    expected_tag = f"sha-{target_revision[:12]}"
    if image_tag != expected_tag:
        raise ProviderPinGateError(
            f"{values_path.as_posix()} image.tag must match agent-platform "
            f"targetRevision: expected {expected_tag}, got {image_tag}"
        )
    if check_image_exists:
        checker = image_checker or _crane_image_checker("crane")
        checker(f"{image_repository}:{image_tag}")

    expected = expected_provider_pins(
        repo_root=repo_root,
        agent_platform_repo=agent_platform_repo,
        application_path=application_path,
        values_path=values_path,
        agent_platform_main_ref=agent_platform_main_ref,
        fingerprint_runner=fingerprint_runner,
        validate_git=False,
    )
    _assert_declared_pins_match(
        values=deployment_values,
        values_path=values_path,
        expected=expected,
    )
    return (
        "agent-control-plane provider pins match agent-platform "
        f"{target_revision} and image {image_repository}:{image_tag}."
    )


def expected_provider_pins(
    *,
    repo_root: Path,
    agent_platform_repo: Path,
    application_path: Path = APPLICATION_PATH,
    values_path: Path = VALUES_PATH,
    agent_platform_main_ref: str = DEFAULT_AGENT_PLATFORM_MAIN_REF,
    fingerprint_runner: FingerprintRunner | None = None,
    validate_git: bool = True,
) -> dict[str, str]:
    application = _load_yaml(repo_root / application_path)
    target_revision = _agent_platform_target_revision(application)
    _validate_target_revision(target_revision)
    if validate_git:
        _validate_agent_platform_checkout(
            agent_platform_repo=agent_platform_repo,
            expected_revision=target_revision,
            main_ref=agent_platform_main_ref,
        )

    chart_values = _load_yaml(agent_platform_repo / CHART_VALUES_PATH)
    deployment_values = _load_yaml(repo_root / values_path)
    merged_values = _deep_merge(chart_values, deployment_values)
    locations = _provider_pin_locations(merged_values)
    runner = fingerprint_runner or _run_provider_fingerprints

    expected: dict[str, str] = {}
    for location in locations:
        raw = runner(location.process, location.env, agent_platform_repo)
        expected[location.label] = _canonical_pin_json(
            raw,
            label=f"recomputed {location.label}",
        )
    return expected


def agent_platform_target_revision(application_path: Path) -> str:
    return _agent_platform_target_revision(_load_yaml(application_path))


def _agent_platform_target_revision(application: dict[str, Any]) -> str:
    spec = _required_mapping(application, "spec", "agent-control-plane application")
    sources = spec.get("sources")
    if not isinstance(sources, list):
        raise ProviderPinGateError("agent-control-plane application spec.sources invalid")
    matches = [
        source
        for source in sources
        if isinstance(source, dict) and source.get("repoURL") in AGENT_PLATFORM_REPO_URLS
    ]
    if len(matches) != 1:
        raise ProviderPinGateError(
            "agent-control-plane application must have exactly one agent-platform source"
        )
    return _required_str(matches[0], "targetRevision", "agent-platform source")


def _validate_target_revision(target_revision: str) -> None:
    import re

    if re.fullmatch(FULL_GIT_SHA_RE, target_revision) is None:
        raise ProviderPinGateError(
            "agent-platform targetRevision must be a full 40-character git SHA"
        )


def _validate_agent_platform_checkout(
    *,
    agent_platform_repo: Path,
    expected_revision: str,
    main_ref: str,
) -> None:
    if not agent_platform_repo.is_dir():
        raise ProviderPinGateError(f"agent-platform repo not found: {agent_platform_repo}")
    if not (agent_platform_repo / CHART_VALUES_PATH).is_file():
        raise ProviderPinGateError(
            "agent-platform checkout is missing "
            f"{CHART_VALUES_PATH.as_posix()}: {agent_platform_repo}"
        )
    actual_revision = _git_stdout(agent_platform_repo, "rev-parse", "HEAD")
    if actual_revision != expected_revision:
        raise ProviderPinGateError(
            "agent-platform checkout revision mismatch: "
            f"expected {expected_revision}, got {actual_revision}"
        )
    _git_stdout(agent_platform_repo, "rev-parse", "--verify", f"{expected_revision}^{{commit}}")
    if not _git_ref_exists(agent_platform_repo, main_ref):
        raise ProviderPinGateError(
            f"agent-platform main ref is unavailable for ancestor check: {main_ref}"
        )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(agent_platform_repo),
            "merge-base",
            "--is-ancestor",
            expected_revision,
            main_ref,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        raise ProviderPinGateError(
            "agent-platform targetRevision is not an ancestor of "
            f"{main_ref}: {expected_revision}"
        )
    if result.returncode != 0:
        raise ProviderPinGateError(
            "could not verify agent-platform targetRevision ancestry: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _provider_pin_locations(values: Mapping[str, Any]) -> tuple[PinLocation, ...]:
    control_env = _control_api_process_env(values)
    gateway_env = _model_gateway_process_env(values)
    return (
        PinLocation(
            process="control-api",
            value_path=("env", PROVIDER_PINS_ENV),
            env=control_env,
        ),
        PinLocation(
            process="model-gateway",
            value_path=("modelGateway", "env", PROVIDER_PINS_ENV),
            env=gateway_env,
        ),
    )


def _control_api_process_env(values: Mapping[str, Any]) -> dict[str, str]:
    env = _secret_env(values)
    operator_env = _string_map(
        values.get("env"),
        label="agent-control-plane values env",
    )
    env.update(operator_env)
    _add_startup_migration_env(env, values, operator_env)
    _add_skills_env(env, values, operator_env)
    _add_metrics_env(env, values, operator_env)
    env.pop(PROVIDER_PINS_ENV, None)
    return env


def _model_gateway_process_env(values: Mapping[str, Any]) -> dict[str, str]:
    env = _secret_env(values)
    operator_env = _string_map(
        values.get("env"),
        label="agent-control-plane values env",
    )
    gateway = _optional_mapping(values.get("modelGateway"))
    gateway_env = _string_map(
        gateway.get("env") if gateway else None,
        label="agent-control-plane values modelGateway.env",
    )
    for key, value in operator_env.items():
        if key not in gateway_env:
            env[key] = value
    _add_startup_migration_env(env, values, operator_env)
    _add_skills_env(env, values, operator_env)
    env.update(gateway_env)
    _add_model_gateway_codex_auth_store_env(env, gateway)
    env.pop(PROVIDER_PINS_ENV, None)
    return env


def _secret_env(values: Mapping[str, Any]) -> dict[str, str]:
    raw_secret_keys = values.get("secretKeys")
    if raw_secret_keys is None:
        return {}
    if not isinstance(raw_secret_keys, list):
        raise ProviderPinGateError("agent-control-plane values secretKeys must be a list")
    env: dict[str, str] = {}
    for raw_key in raw_secret_keys:
        if not isinstance(raw_key, str) or not raw_key:
            raise ProviderPinGateError("agent-control-plane values secretKeys invalid")
        env[raw_key] = _placeholder_for_secret_key(raw_key)
    return env


def _placeholder_for_secret_key(key: str) -> str:
    if key.endswith("DATABASE_URL"):
        return "postgresql://provider-pin-check@localhost/provider_pin_check"
    if key == MODEL_GATEWAY_CODEX_AUTH_JSON_ENV:
        return json.dumps(
            {
                "tokens": {"access_token": "provider-pin-check"},
                "last_refresh": "2026-01-01T00:00:00Z",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    if key.endswith("_JSON"):
        return "{}"
    return "provider-pin-check"


def _add_startup_migration_env(
    env: dict[str, str],
    values: Mapping[str, Any],
    operator_env: Mapping[str, str],
) -> None:
    migrations = _optional_mapping(values.get("migrations"))
    if not migrations:
        return
    if (
        migrations.get("enabled") is True
        and migrations.get("disableStartupSchemaMigration") is True
        and "AGENT_PLATFORM_POSTGRES_MIGRATE_ON_STARTUP" not in operator_env
    ):
        env["AGENT_PLATFORM_POSTGRES_MIGRATE_ON_STARTUP"] = "false"


def _add_skills_env(
    env: dict[str, str],
    values: Mapping[str, Any],
    operator_env: Mapping[str, str],
) -> None:
    skills = _optional_mapping(values.get("skills"))
    if not skills:
        return
    mount_path = skills.get("mountPath")
    if (
        skills.get("enabled") is True
        and isinstance(mount_path, str)
        and mount_path
        and "AGENT_PLATFORM_SKILLS_DIR" not in operator_env
    ):
        env["AGENT_PLATFORM_SKILLS_DIR"] = mount_path


def _add_metrics_env(
    env: dict[str, str],
    values: Mapping[str, Any],
    operator_env: Mapping[str, str],
) -> None:
    metrics = _optional_mapping(values.get("metrics"))
    service = _optional_mapping(values.get("service"))
    if not metrics:
        return
    if "AGENT_PLATFORM_API_PORT" not in operator_env and service:
        target_port = service.get("targetPort")
        if target_port is not None:
            env["AGENT_PLATFORM_API_PORT"] = _helm_string(target_port)
    if "AGENT_PLATFORM_METRICS_ENABLED" not in operator_env:
        env["AGENT_PLATFORM_METRICS_ENABLED"] = _helm_string(metrics.get("enabled"))
    if "AGENT_PLATFORM_METRICS_PORT" not in operator_env:
        metrics_port = metrics.get("port")
        if metrics_port is not None:
            env["AGENT_PLATFORM_METRICS_PORT"] = _helm_string(metrics_port)


def _add_model_gateway_codex_auth_store_env(
    env: dict[str, str],
    gateway: Mapping[str, Any] | None,
) -> None:
    if not gateway:
        return
    persistence = _optional_mapping(gateway.get("codexAuthPersistence"))
    if not persistence or persistence.get("enabled") is not True:
        return
    mount_path = persistence.get("mountPath")
    file_name = persistence.get("fileName")
    if not isinstance(mount_path, str) or not mount_path:
        raise ProviderPinGateError("modelGateway.codexAuthPersistence.mountPath invalid")
    if not isinstance(file_name, str) or not file_name:
        raise ProviderPinGateError("modelGateway.codexAuthPersistence.fileName invalid")
    env[MODEL_GATEWAY_CODEX_AUTH_STORE_PATH_ENV] = (
        f"{mount_path.rstrip('/')}/{file_name}"
    )


def _run_provider_fingerprints(
    process: ProviderProcess,
    env: Mapping[str, str],
    agent_platform_repo: Path,
) -> str:
    process_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENT_PLATFORM_")
    }
    process_env.update(env)
    try:
        result = subprocess.run(
            [
                "uv",
                "--directory",
                str(agent_platform_repo),
                "run",
                "mandate-provider-fingerprints",
                "--process",
                process,
                "--format",
                "pins-json",
            ],
            capture_output=True,
            text=True,
            env=process_env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProviderPinGateError("uv is required to recompute provider pins") from exc
    if result.returncode != 0:
        raise ProviderPinGateError(
            f"provider fingerprint command failed for {process}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def _assert_declared_pins_match(
    *,
    values: Mapping[str, Any],
    values_path: Path,
    expected: Mapping[str, str],
) -> None:
    for label, expected_json in expected.items():
        raw_declared = _value_at_path(values, tuple(label.split(".")))
        if not isinstance(raw_declared, str) or not raw_declared.strip():
            raise ProviderPinGateError(
                f"{values_path.as_posix()} {label} missing non-empty provider pin JSON; "
                f"expected {expected_json}"
            )
        declared_json = _canonical_pin_json(
            raw_declared,
            label=f"{values_path.as_posix()} {label}",
        )
        if declared_json != expected_json:
            raise ProviderPinGateError(
                f"{values_path.as_posix()} {label} mismatch: "
                f"expected {expected_json}, declared {declared_json}"
            )


def _canonical_pin_json(raw: str, *, label: str) -> str:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderPinGateError(f"{label} must be JSON") from exc
    if not isinstance(decoded, dict):
        raise ProviderPinGateError(f"{label} must be a JSON object")
    return json.dumps(decoded, sort_keys=True, separators=(",", ":"))


def _crane_image_checker(command: str) -> ImageChecker:
    def check(image_ref: str) -> None:
        try:
            result = subprocess.run(
                [command, "digest", image_ref],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ProviderPinGateError(
                f"{command} is required to verify image tag existence"
            ) from exc
        if result.returncode != 0:
            raise ProviderPinGateError(
                "control-plane image tag is absent from DOCR or inaccessible: "
                f"{image_ref}. {result.stderr.strip() or result.stdout.strip()}"
            )

    return check


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _value_at_path(values: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = values
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _string_map(value: Any, *, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProviderPinGateError(f"{label} must be a mapping")
    env: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key:
            raise ProviderPinGateError(f"{label} key invalid")
        env[key] = _helm_string(raw)
    return env


def _helm_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _optional_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _required_mapping(mapping: Mapping[str, Any], key: str, label: str) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ProviderPinGateError(f"{label} missing mapping {key}")
    return value


def _required_str(mapping: Mapping[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderPinGateError(f"{label} missing non-empty {key}")
    return value


def _git_stdout(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProviderPinGateError("git is required for provider-pin checks") from exc
    if result.returncode != 0:
        raise ProviderPinGateError(
            f"git {' '.join(args)} failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def _git_ref_exists(repo: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ProviderPinGateError(f"YAML mapping expected: {path}")
    return loaded


if __name__ == "__main__":
    sys.exit(main())
