#!/usr/bin/env python3
"""Validate the control-plane registry overlay against the deployed Mandate pin."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_APPLICATION_PATH = Path("argocd/applications/agent-control-plane.yaml")
REGISTRY_OVERLAY_CONFIGMAP_PATH = Path(
    "apps/agent-control-plane-registry-overlay/configmap.yaml"
)
AGENT_PLATFORM_REPO_URLS = {
    "git@github.com:cesaregarza/agent-platform.git",
    "https://github.com/cesaregarza/agent-platform",
    "https://github.com/cesaregarza/agent-platform.git",
}
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REGISTRY_ROOT_KEYS = {
    "workload_imports.yaml",
    "policy.prod.yaml",
    "evals.yaml",
}

YAML_PARSER = YAML(typ="safe")


class RegistryCompatError(RuntimeError):
    """Raised when the deployed-version compatibility gate fails closed."""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the production Mandate RegistrySnapshot that the pinned "
            "agent-control-plane revision would build from this config repo."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--agent-platform-repo",
        type=Path,
        help=(
            "Checked-out agent-platform repository at the targetRevision from "
            "argocd/applications/agent-control-plane.yaml."
        ),
    )
    parser.add_argument(
        "--environment",
        default="prod",
        help="Registry policy environment to validate. Defaults to prod.",
    )
    parser.add_argument(
        "--print-target-revision",
        action="store_true",
        help="Print the pinned agent-platform targetRevision and exit.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    try:
        target_revision = agent_platform_target_revision(
            repo_root / CONTROL_PLANE_APPLICATION_PATH
        )
        if args.print_target_revision:
            print(target_revision)
            return 0
        if args.agent_platform_repo is None:
            raise RegistryCompatError("--agent-platform-repo is required")
        summary = validate_deployed_registry_compat(
            repo_root=repo_root,
            agent_platform_repo=args.agent_platform_repo.resolve(),
            expected_revision=target_revision,
            environment=args.environment,
        )
    except RegistryCompatError as exc:
        print(f"agent-control-plane registry compat gate failed: {exc}", file=sys.stderr)
        return 1
    print(summary)
    return 0


def validate_deployed_registry_compat(
    *,
    repo_root: Path,
    agent_platform_repo: Path,
    expected_revision: str,
    environment: str = "prod",
) -> str:
    _validate_agent_platform_checkout(
        agent_platform_repo=agent_platform_repo,
        expected_revision=expected_revision,
    )
    data = registry_overlay_data(repo_root / REGISTRY_OVERLAY_CONFIGMAP_PATH)

    with tempfile.TemporaryDirectory(prefix="mandate-registry-compat-") as raw_tmp:
        temp_repo = Path(raw_tmp) / "agent-platform"
        ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".mypy_cache")
        shutil.copytree(agent_platform_repo, temp_repo, ignore=ignore)
        materialize_registry_overlay(temp_repo, data)
        _import_registry_snapshot_from(temp_repo, environment=environment)

    return (
        "agent-control-plane registry overlay is compatible with "
        f"agent-platform {expected_revision} for {environment}."
    )


def agent_platform_target_revision(application_path: Path) -> str:
    app = _load_yaml(application_path)
    sources = (((app.get("spec") or {}).get("sources")) or [])
    if not isinstance(sources, list):
        raise RegistryCompatError("agent-control-plane Argo Application sources invalid")
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("repoURL") not in AGENT_PLATFORM_REPO_URLS:
            continue
        target_revision = source.get("targetRevision")
        if not isinstance(target_revision, str) or not target_revision:
            raise RegistryCompatError(
                "agent-platform Argo source missing targetRevision"
            )
        if GIT_SHA_RE.fullmatch(target_revision) is None:
            raise RegistryCompatError(
                "agent-platform targetRevision must be a full 40-character git SHA"
            )
        return target_revision
    raise RegistryCompatError("agent-control-plane Argo Application missing agent-platform source")


def registry_overlay_data(configmap_path: Path) -> dict[str, str]:
    configmap = _load_yaml(configmap_path)
    data = configmap.get("data")
    if not isinstance(data, dict) or not data:
        raise RegistryCompatError("registry overlay ConfigMap must contain data")
    strings: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or "/" in key or key in {"", ".", ".."}:
            raise RegistryCompatError(f"registry overlay ConfigMap key is invalid: {key}")
        if not isinstance(value, str) or not value.strip():
            raise RegistryCompatError(f"registry overlay ConfigMap value is empty: {key}")
        strings[key] = value
    return strings


def materialize_registry_overlay(agent_platform_repo: Path, data: dict[str, str]) -> None:
    registry_dir = agent_platform_repo / "registries"
    imports_dir = registry_dir / "imports"
    registry_dir.mkdir(parents=True, exist_ok=True)
    imports_dir.mkdir(parents=True, exist_ok=True)
    for key, value in data.items():
        target_dir = registry_dir if key in REGISTRY_ROOT_KEYS else imports_dir
        target = target_dir / key
        target.write_text(value, encoding="utf-8")


def _validate_agent_platform_checkout(
    *,
    agent_platform_repo: Path,
    expected_revision: str,
) -> None:
    if not agent_platform_repo.is_dir():
        raise RegistryCompatError(f"agent-platform repo not found: {agent_platform_repo}")
    if not (agent_platform_repo / "mandate" / "core" / "registry.py").is_file():
        raise RegistryCompatError(
            f"agent-platform checkout is missing mandate/core/registry.py: {agent_platform_repo}"
        )
    try:
        result = subprocess.run(
            ["git", "-C", str(agent_platform_repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RegistryCompatError("git is required to verify agent-platform revision") from exc
    if result.returncode != 0:
        raise RegistryCompatError("could not read agent-platform git revision")
    actual = result.stdout.strip()
    if actual != expected_revision:
        raise RegistryCompatError(
            "agent-platform checkout revision mismatch: "
            f"expected {expected_revision}, got {actual}"
        )


def _import_registry_snapshot_from(repo_root: Path, *, environment: str) -> None:
    with _temporary_sys_path(repo_root):
        for module_name in (
            "mandate.core.registry",
            "mandate.loaders.registry",
            "mandate.paths",
            "mandate",
        ):
            sys.modules.pop(module_name, None)
        try:
            registry_module = importlib.import_module("mandate.core.registry")
        except Exception as exc:  # noqa: BLE001 - surfacing import failures is the gate.
            raise RegistryCompatError(
                f"could not import pinned agent-platform registry code: {exc}"
            ) from exc

        registry_snapshot = getattr(registry_module, "RegistrySnapshot", None)
        registry_error = getattr(registry_module, "RegistryError", ValueError)
        if registry_snapshot is None:
            raise RegistryCompatError(
                "pinned agent-platform registry code does not expose RegistrySnapshot"
            )
        try:
            registry_snapshot.from_repo(repo_root, environment=environment)
        except registry_error as exc:
            raise RegistryCompatError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - fail closed on any boot-time break.
            raise RegistryCompatError(
                f"pinned agent-platform registry snapshot failed: {exc}"
            ) from exc


@contextlib.contextmanager
def _temporary_sys_path(path: Path) -> Any:
    raw_path = str(path)
    sys.path.insert(0, raw_path)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(raw_path)


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RegistryCompatError(f"YAML mapping expected: {path}")
    return loaded


if __name__ == "__main__":
    sys.exit(main())
