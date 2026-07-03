#!/usr/bin/env python3
"""Validate the control-plane registry overlay against the deployed Mandate pin."""

from __future__ import annotations

import argparse
import contextlib
import inspect
import importlib
import json
import os
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
REGISTRY_OVERLAY_DIR = Path("apps/agent-control-plane-registry-overlay")
REGISTRY_OVERLAY_CONFIGMAP_PATH = REGISTRY_OVERLAY_DIR / "configmap.yaml"
REGISTRY_OVERLAY_KUSTOMIZATION_PATH = REGISTRY_OVERLAY_DIR / "kustomization.yaml"
REGISTRY_OVERLAY_CONFIGMAP_NAME = "agent-control-plane-registry-overlay"
SKILL_BUNDLE_DIR = Path("apps/agent-control-plane-skills")
SKILL_BUNDLE_CONFIGMAP_NAME = "mandate-skill-packs"
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
    data = registry_overlay_data(repo_root / REGISTRY_OVERLAY_DIR)
    _assert_render_equivalent_to_base_configmap(
        repo_root=repo_root,
        rendered_data=data,
    )

    with tempfile.TemporaryDirectory(prefix="mandate-registry-compat-") as raw_tmp:
        temp_repo = Path(raw_tmp) / "agent-platform"
        ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".mypy_cache")
        shutil.copytree(agent_platform_repo, temp_repo, ignore=ignore)
        materialize_registry_overlay(temp_repo, data)
        materialize_skill_bundle(temp_repo, skill_bundle_data(repo_root / SKILL_BUNDLE_DIR))
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


def registry_overlay_data(overlay_path: Path) -> dict[str, str]:
    """Read registry overlay data from Kustomize sources or the legacy ConfigMap."""
    if overlay_path.is_dir():
        kustomization_path = overlay_path / "kustomization.yaml"
        if kustomization_path.exists():
            return _registry_overlay_data_from_rendered_kustomization(
                overlay_dir=overlay_path,
                kustomization_path=kustomization_path,
            )
        configmap_path = overlay_path / "configmap.yaml"
        if configmap_path.exists():
            return _registry_overlay_data_from_configmap(configmap_path)
        raise RegistryCompatError(
            "registry overlay must contain kustomization.yaml or configmap.yaml"
        )
    return _registry_overlay_data_from_configmap(overlay_path)


def skill_bundle_data(bundle_path: Path) -> dict[str, str]:
    """Read the operator-reviewed skill bundle ConfigMap data."""
    if not bundle_path.exists():
        return {}
    kustomization_path = bundle_path / "kustomization.yaml"
    if not kustomization_path.exists():
        return {}
    rendered = _render_kustomization(bundle_path)
    if rendered is not None:
        with contextlib.suppress(RegistryCompatError):
            return _named_configmap_data_from_rendered_yaml(
                rendered,
                configmap_name=SKILL_BUNDLE_CONFIGMAP_NAME,
                label="skill bundle",
            )
    return _configmap_data_from_kustomization_sources(
        source_dir=bundle_path,
        kustomization_path=kustomization_path,
        configmap_name=SKILL_BUNDLE_CONFIGMAP_NAME,
        label="skill bundle",
    )


def _assert_render_equivalent_to_base_configmap(
    *,
    repo_root: Path,
    rendered_data: dict[str, str],
) -> None:
    base_ref = _registry_equivalence_base_ref()
    if base_ref is None:
        return
    if (
        os.environ.get("AGENT_CONTROL_PLANE_REGISTRY_BASE_REF") is None
        and not _is_git_worktree(repo_root)
    ):
        return
    expected_data = _base_registry_configmap_data(repo_root=repo_root, base_ref=base_ref)
    if expected_data is None:
        return
    _assert_registry_data_equivalent(rendered_data, expected_data)


def _registry_equivalence_base_ref() -> str | None:
    configured = os.environ.get("AGENT_CONTROL_PLANE_REGISTRY_BASE_REF")
    if configured:
        return configured
    github_base_ref = os.environ.get("GITHUB_BASE_REF")
    if github_base_ref:
        return f"origin/{github_base_ref}"
    return None


def _base_registry_configmap_data(
    *,
    repo_root: Path,
    base_ref: str,
) -> dict[str, str] | None:
    raw = _git_show(
        repo_root,
        f"{base_ref}:{REGISTRY_OVERLAY_CONFIGMAP_PATH.as_posix()}",
    )
    if raw is None and base_ref.startswith("origin/"):
        _git_fetch_base_ref(repo_root, base_ref.removeprefix("origin/"))
        raw = _git_show(
            repo_root,
            f"{base_ref}:{REGISTRY_OVERLAY_CONFIGMAP_PATH.as_posix()}",
        )
    if raw is None and not _git_ref_exists(repo_root, base_ref):
        raise RegistryCompatError(
            f"registry overlay render-equivalence base ref is unavailable: {base_ref}"
        )
    if raw is None:
        return None
    configmap = YAML_PARSER.load(raw)
    data = configmap.get("data") if isinstance(configmap, dict) else None
    if not isinstance(data, dict):
        raise RegistryCompatError("base registry overlay ConfigMap must contain data")
    return {
        key: value
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _git_show(repo_root: Path, revision: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", revision],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _git_fetch_base_ref(repo_root: Path, base_ref: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "fetch",
            "--depth=1",
            "origin",
            f"{base_ref}:refs/remotes/origin/{base_ref}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_git_worktree(repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_ref_exists(repo_root: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _assert_registry_data_equivalent(
    rendered_data: dict[str, str],
    expected_data: dict[str, str],
) -> None:
    rendered_keys = set(rendered_data)
    expected_keys = set(expected_data)
    if rendered_keys != expected_keys:
        raise RegistryCompatError(
            "registry overlay render key drift: "
            f"expected {sorted(expected_keys)}, got {sorted(rendered_keys)}"
        )
    for key in sorted(expected_keys):
        rendered = _registry_data_semantic_value(key, rendered_data[key])
        expected = _registry_data_semantic_value(key, expected_data[key])
        if rendered != expected:
            raise RegistryCompatError(f"registry overlay render drift: {key}")


def _registry_data_semantic_value(key: str, value: str) -> Any:
    if key.endswith(".yaml") or key.endswith(".yml"):
        return YAML_PARSER.load(value)
    if key.endswith(".json"):
        return json.loads(value)
    if key.endswith(".jsonl"):
        return [
            json.loads(line)
            for line in value.splitlines()
            if line.strip()
        ]
    return value


def _registry_overlay_data_from_configmap(configmap_path: Path) -> dict[str, str]:
    configmap = _load_yaml(configmap_path)
    data = configmap.get("data")
    if not isinstance(data, dict) or not data:
        raise RegistryCompatError("registry overlay ConfigMap must contain data")
    strings: dict[str, str] = {}
    for key, value in data.items():
        _validate_registry_overlay_key(key)
        if not isinstance(value, str) or not value.strip():
            raise RegistryCompatError(f"registry overlay ConfigMap value is empty: {key}")
        strings[key] = value
    return strings


def _registry_overlay_data_from_rendered_kustomization(
    *,
    overlay_dir: Path,
    kustomization_path: Path,
) -> dict[str, str]:
    rendered = _render_kustomization(overlay_dir)
    if rendered is not None:
        data = _registry_overlay_data_from_rendered_yaml(rendered)
        if data:
            return data
    return _registry_overlay_data_from_kustomization_sources(
        overlay_dir=overlay_dir,
        kustomization_path=kustomization_path,
    )


def _registry_overlay_data_from_rendered_yaml(rendered: str) -> dict[str, str]:
    return _named_configmap_data_from_rendered_yaml(
        rendered,
        configmap_name=REGISTRY_OVERLAY_CONFIGMAP_NAME,
        label="registry overlay",
    )


def _named_configmap_data_from_rendered_yaml(
    rendered: str,
    *,
    configmap_name: str,
    label: str,
) -> dict[str, str]:
    for document in YAML_PARSER.load_all(rendered):
        if not isinstance(document, dict):
            continue
        if document.get("kind") != "ConfigMap":
            continue
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if metadata.get("name") != configmap_name:
            continue
        data = document.get("data")
        if not isinstance(data, dict):
            raise RegistryCompatError(f"rendered {label} ConfigMap missing data")
        strings: dict[str, str] = {}
        for key, value in data.items():
            _validate_registry_overlay_key(key)
            if not isinstance(value, str) or not value.strip():
                raise RegistryCompatError(
                    f"rendered {label} ConfigMap value is empty: {key}"
                )
            strings[key] = value
        return strings
    raise RegistryCompatError(f"rendered {label} ConfigMap not found")


def _render_kustomization(overlay_dir: Path) -> str | None:
    commands = (
        ["kustomize", "build", str(overlay_dir)],
        ["kubectl", "kustomize", str(overlay_dir)],
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            continue
        if result.returncode == 0:
            return result.stdout
    return None


def _registry_overlay_data_from_kustomization_sources(
    *,
    overlay_dir: Path,
    kustomization_path: Path,
) -> dict[str, str]:
    return _configmap_data_from_kustomization_sources(
        source_dir=overlay_dir,
        kustomization_path=kustomization_path,
        configmap_name=REGISTRY_OVERLAY_CONFIGMAP_NAME,
        label="registry overlay",
    )


def _configmap_data_from_kustomization_sources(
    *,
    source_dir: Path,
    kustomization_path: Path,
    configmap_name: str,
    label: str,
) -> dict[str, str]:
    kustomization = _load_yaml(kustomization_path)
    generators = kustomization.get("configMapGenerator")
    if not isinstance(generators, list):
        raise RegistryCompatError(f"{label} kustomization missing configMapGenerator")

    generator = None
    for item in generators:
        if isinstance(item, dict) and item.get("name") == configmap_name:
            generator = item
            break
    if generator is None:
        raise RegistryCompatError(
            f"{label} kustomization missing {configmap_name} generator"
        )

    file_specs = generator.get("files")
    if not isinstance(file_specs, list) or not file_specs:
        raise RegistryCompatError(f"{label} ConfigMap generator must contain files")

    data: dict[str, str] = {}
    for raw_spec in file_specs:
        if not isinstance(raw_spec, str) or not raw_spec:
            raise RegistryCompatError(f"{label} ConfigMap file spec is invalid")
        key, relative_path = _parse_kustomize_file_spec(raw_spec)
        _validate_registry_overlay_key(key)
        if key in data:
            raise RegistryCompatError(f"{label} ConfigMap key is duplicated: {key}")
        source_path = _resolve_overlay_source_path(source_dir, relative_path)
        value = source_path.read_text(encoding="utf-8")
        if not value.strip():
            raise RegistryCompatError(f"{label} ConfigMap value is empty: {key}")
        data[key] = value

    return data


def _parse_kustomize_file_spec(file_spec: str) -> tuple[str, str]:
    if "=" in file_spec:
        key, relative_path = file_spec.split("=", 1)
    else:
        relative_path = file_spec
        key = Path(relative_path).name
    if not key or not relative_path:
        raise RegistryCompatError(f"registry overlay ConfigMap file spec is invalid: {file_spec}")
    return key, relative_path


def _resolve_overlay_source_path(overlay_dir: Path, relative_path: str) -> Path:
    source_path = (overlay_dir / relative_path).resolve()
    overlay_root = overlay_dir.resolve()
    try:
        source_path.relative_to(overlay_root)
    except ValueError as exc:
        raise RegistryCompatError(
            f"registry overlay ConfigMap file escapes overlay directory: {relative_path}"
        ) from exc
    if not source_path.is_file():
        raise RegistryCompatError(
            f"registry overlay ConfigMap source file not found: {relative_path}"
        )
    return source_path


def _validate_registry_overlay_key(key: Any) -> None:
    if not isinstance(key, str) or "/" in key or key in {"", ".", ".."}:
        raise RegistryCompatError(f"registry overlay ConfigMap key is invalid: {key}")


def materialize_registry_overlay(agent_platform_repo: Path, data: dict[str, str]) -> None:
    registry_dir = agent_platform_repo / "registries"
    imports_dir = registry_dir / "imports"
    registry_dir.mkdir(parents=True, exist_ok=True)
    imports_dir.mkdir(parents=True, exist_ok=True)
    for key, value in data.items():
        target_dir = registry_dir if key in REGISTRY_ROOT_KEYS else imports_dir
        target = target_dir / key
        target.write_text(value, encoding="utf-8")


def materialize_skill_bundle(agent_platform_repo: Path, data: dict[str, str]) -> None:
    if not data:
        return
    skills_dir = agent_platform_repo / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for key, value in data.items():
        _validate_registry_overlay_key(key)
        (skills_dir / key).write_text(value, encoding="utf-8")


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
        kwargs: dict[str, Any] = {"environment": environment}
        signature = inspect.signature(registry_snapshot.from_repo)
        skills_dir = repo_root / "skills"
        if skills_dir.exists() and "skill_store" in signature.parameters:
            skills_module = importlib.import_module("mandate.loaders.skills")
            kwargs["skill_store"] = skills_module.load_skill_store(skills_dir)
            if "skill_store_loader" in signature.parameters:
                kwargs["skill_store_loader"] = lambda: skills_module.load_skill_store(
                    skills_dir
                )
        try:
            registry_snapshot.from_repo(repo_root, **kwargs)
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
