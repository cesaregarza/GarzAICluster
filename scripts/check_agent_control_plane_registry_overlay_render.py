#!/usr/bin/env python3
"""Validate the complete registry-overlay Application render."""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_OVERLAY_DIR = Path("apps/agent-control-plane-registry-overlay")
REGISTRY_OVERLAY_APPLICATION_PATH = Path(
    "argocd/applications/agent-control-plane-registry-overlay.yaml"
)
REGISTRY_OVERLAY_CONFIGMAP_NAME = "agent-control-plane-registry-overlay"
REGISTRY_OVERLAY_RESTART_ORDER = [
    "agent-control-plane",
    "agent-control-plane-model-gateway",
    "agent-control-plane-callback-adapter",
    "agent-control-plane-git-deliverer",
    "agent-control-plane-local-worker",
]
GOLDEN_CONFIGMAP_PATH = Path(
    "tests/fixtures/agent-control-plane-registry-overlay/configmap.golden.yaml"
)

YAML_PARSER = YAML(typ="safe")


class RegistryOverlayRenderError(RuntimeError):
    """Raised when the registry overlay cannot render or drifts from its contract."""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Render the registry-overlay Application's real Helm source, require "
            "the generated PostSync Job and scoped RBAC, and compare its ConfigMap "
            "with both the local Kustomize source render and committed golden."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--kustomize", default="kustomize")
    parser.add_argument(
        "--golden-configmap",
        type=Path,
        default=None,
        help="Optional path to the pre-refactor registry overlay ConfigMap golden.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    golden_path = (
        args.golden_configmap.resolve()
        if args.golden_configmap is not None
        else repo_root / GOLDEN_CONFIGMAP_PATH
    )
    try:
        check_registry_overlay_render(
            repo_root=repo_root,
            helm=args.helm,
            kustomize=args.kustomize,
            golden_configmap_path=golden_path,
        )
    except RegistryOverlayRenderError as exc:
        print(f"registry overlay render gate failed: {exc}", file=sys.stderr)
        return 1
    print(
        "registry overlay Helm Application render contains ConfigMap, scoped RBAC, "
        "and generated PostSync Job; ConfigMap matches Kustomize and golden."
    )
    return 0


def check_registry_overlay_render(
    *,
    repo_root: Path,
    helm: str,
    kustomize: str,
    golden_configmap_path: Path,
) -> None:
    _assert_single_helm_application_source(repo_root)
    helm_documents = render_registry_overlay_application(
        repo_root=repo_root,
        helm=helm,
    )
    helm_configmap = _extract_registry_configmap(helm_documents)

    kustomize_rendered = _kustomize_build(
        repo_root=repo_root,
        overlay_dir=repo_root / REGISTRY_OVERLAY_DIR,
        kustomize=kustomize,
    )
    kustomize_configmap = _extract_registry_configmap(
        _load_rendered_documents(kustomize_rendered, label="kustomize")
    )
    golden_configmap = _load_yaml(golden_configmap_path)
    for rendered_configmap in (helm_configmap, kustomize_configmap):
        _assert_configmap_identity_matches(rendered_configmap, golden_configmap)
        _assert_configmap_data_matches(rendered_configmap, golden_configmap)


def render_registry_overlay_application(
    *,
    repo_root: Path,
    helm: str,
) -> list[dict[str, Any]]:
    """Render and validate the one source Argo will use for this Application."""

    rendered = _helm_template(
        repo_root=repo_root,
        chart_dir=repo_root / REGISTRY_OVERLAY_DIR,
        helm=helm,
    )
    documents = _load_rendered_documents(rendered, label="helm")
    _assert_complete_application_resources(documents)
    return documents


def _assert_single_helm_application_source(repo_root: Path) -> None:
    application = _load_yaml(repo_root / REGISTRY_OVERLAY_APPLICATION_PATH)
    spec = application.get("spec")
    if not isinstance(spec, dict):
        raise RegistryOverlayRenderError("registry overlay Application spec is missing")
    if "sources" in spec:
        raise RegistryOverlayRenderError(
            "registry overlay Application must not use multi-source rendering"
        )
    expected = {
        "repoURL": "https://github.com/cesaregarza/GarzAICluster",
        "targetRevision": "main",
        "path": str(REGISTRY_OVERLAY_DIR),
        "helm": {},
    }
    if spec.get("source") != expected:
        raise RegistryOverlayRenderError(
            "registry overlay Application source drifted:\n"
            + _json_diff(expected, spec.get("source"))
        )


def _helm_template(*, repo_root: Path, chart_dir: Path, helm: str) -> str:
    command = [
        helm,
        "template",
        "agent-control-plane-registry-overlay",
        str(chart_dir.relative_to(repo_root)),
        "--namespace",
        "agent-control-plane",
    ]
    return _run_render_command(command, cwd=repo_root, label="helm template")


def _kustomize_build(*, repo_root: Path, overlay_dir: Path, kustomize: str) -> str:
    subcommand = "kustomize" if Path(kustomize).name == "kubectl" else "build"
    command = [kustomize, subcommand, str(overlay_dir.relative_to(repo_root))]
    return _run_render_command(command, cwd=repo_root, label="kustomize build")


def _run_render_command(command: list[str], *, cwd: Path, label: str) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RegistryOverlayRenderError(f"binary not found: {command[0]}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RegistryOverlayRenderError(f"{label} failed: {stderr}")
    if not result.stdout.strip():
        raise RegistryOverlayRenderError(f"{label} produced no output")
    return result.stdout


def _load_rendered_documents(rendered: str, *, label: str) -> list[dict[str, Any]]:
    documents = [
        document
        for document in YAML_PARSER.load_all(rendered)
        if isinstance(document, dict) and document
    ]
    if not documents:
        raise RegistryOverlayRenderError(f"{label} render contained no YAML objects")
    return documents


def _extract_registry_configmap(
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    for document in documents:
        if document.get("kind") != "ConfigMap":
            continue
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if metadata.get("name") == REGISTRY_OVERLAY_CONFIGMAP_NAME:
            return document
    raise RegistryOverlayRenderError(
        f"rendered {REGISTRY_OVERLAY_CONFIGMAP_NAME} ConfigMap not found"
    )


def _assert_complete_application_resources(documents: list[dict[str, Any]]) -> None:
    if len(documents) != 5:
        raise RegistryOverlayRenderError(
            f"complete Application must contain exactly five objects, got {len(documents)}"
        )
    expected_named = {
        ("v1", "ServiceAccount", "registry-overlay-restart"),
        ("v1", "ConfigMap", REGISTRY_OVERLAY_CONFIGMAP_NAME),
        (
            "rbac.authorization.k8s.io/v1",
            "Role",
            "registry-overlay-restart",
        ),
        (
            "rbac.authorization.k8s.io/v1",
            "RoleBinding",
            "registry-overlay-restart",
        ),
    }
    actual_named: set[tuple[Any, Any, Any]] = set()
    generated_hooks: list[dict[str, Any]] = []
    for document in documents:
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            raise RegistryOverlayRenderError("rendered object metadata must be a mapping")
        if metadata.get("namespace") != "agent-control-plane":
            raise RegistryOverlayRenderError(
                f"rendered {document.get('kind')} has wrong namespace: "
                f"{metadata.get('namespace')}"
            )
        name = metadata.get("name")
        if isinstance(name, str):
            actual_named.add((document.get("apiVersion"), document.get("kind"), name))
        if document.get("kind") == "Job" and metadata.get("generateName"):
            generated_hooks.append(document)

    if actual_named != expected_named:
        raise RegistryOverlayRenderError(
            "complete Application named-resource shape drifted:\n"
            + _json_diff(sorted(expected_named), sorted(actual_named))
        )
    if len(generated_hooks) != 1:
        raise RegistryOverlayRenderError(
            f"complete Application must contain one generated Job, got {len(generated_hooks)}"
        )
    hook = generated_hooks[0]
    metadata = hook["metadata"]
    if metadata.get("name") is not None:
        raise RegistryOverlayRenderError("generated PostSync Job must not set metadata.name")
    if metadata.get("generateName") != "registry-overlay-restart-":
        raise RegistryOverlayRenderError("generated PostSync Job prefix drifted")
    annotations = metadata.get("annotations")
    expected_annotations = {
        "argocd.argoproj.io/hook": "PostSync",
        "argocd.argoproj.io/hook-delete-policy": "HookSucceeded",
    }
    if annotations != expected_annotations:
        raise RegistryOverlayRenderError(
            "generated Job hook annotations drifted:\n"
            + _json_diff(expected_annotations, annotations)
        )
    hook_spec = hook.get("spec")
    if not isinstance(hook_spec, dict) or hook_spec.get("backoffLimit") != 0:
        raise RegistryOverlayRenderError("generated PostSync Job must fail without retry")
    if hook_spec.get("ttlSecondsAfterFinished") != 86400:
        raise RegistryOverlayRenderError(
            "generated PostSync Job must retain failed logs for one bounded day"
        )

    role = next(document for document in documents if document.get("kind") == "Role")
    expected_rules = [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "resourceNames": REGISTRY_OVERLAY_RESTART_ORDER,
            "verbs": ["get", "patch"],
        },
        {
            "apiGroups": [""],
            "resources": ["pods"],
            "verbs": ["list"],
        },
    ]
    if role.get("rules") != expected_rules:
        raise RegistryOverlayRenderError(
            "rendered restart Role is not restricted to the five targets:\n"
            + _json_diff(expected_rules, role.get("rules"))
        )


def _assert_configmap_identity_matches(
    rendered_configmap: dict[str, Any],
    golden_configmap: dict[str, Any],
) -> None:
    rendered_identity = _configmap_identity(rendered_configmap)
    golden_identity = _configmap_identity(golden_configmap)
    if rendered_identity == golden_identity:
        return
    raise RegistryOverlayRenderError(
        "rendered ConfigMap identity drifted from golden:\n"
        + _json_diff(golden_identity, rendered_identity)
    )


def _configmap_identity(configmap: dict[str, Any]) -> dict[str, Any]:
    metadata = configmap.get("metadata")
    if not isinstance(metadata, dict):
        raise RegistryOverlayRenderError("ConfigMap metadata must be a mapping")
    return {
        "apiVersion": configmap.get("apiVersion"),
        "kind": configmap.get("kind"),
        "metadata": {
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace"),
            "labels": metadata.get("labels") or {},
        },
    }


def _assert_configmap_data_matches(
    rendered_configmap: dict[str, Any],
    golden_configmap: dict[str, Any],
) -> None:
    rendered_data = _configmap_data(rendered_configmap, label="rendered")
    golden_data = _configmap_data(golden_configmap, label="golden")
    rendered_keys = set(rendered_data)
    golden_keys = set(golden_data)
    if rendered_keys != golden_keys:
        raise RegistryOverlayRenderError(
            "rendered ConfigMap data keys drifted from golden: "
            f"expected {sorted(golden_keys)}, got {sorted(rendered_keys)}"
        )
    for key in sorted(golden_keys):
        rendered_value = rendered_data[key]
        golden_value = golden_data[key]
        if rendered_value == golden_value:
            continue
        raise RegistryOverlayRenderError(
            f"rendered ConfigMap data drifted from golden: {key}\n"
            + _text_diff(
                golden_value,
                rendered_value,
                fromfile=f"golden:{key}",
                tofile=f"rendered:{key}",
            )
        )


def _configmap_data(configmap: dict[str, Any], *, label: str) -> dict[str, str]:
    data = configmap.get("data")
    if not isinstance(data, dict):
        raise RegistryOverlayRenderError(f"{label} ConfigMap data must be a mapping")
    strings: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RegistryOverlayRenderError(
                f"{label} ConfigMap data entries must be strings"
            )
        strings[key] = value
    return strings


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RegistryOverlayRenderError(f"YAML mapping expected: {path}")
    return loaded


def _json_diff(expected: Any, rendered: Any) -> str:
    expected_text = json.dumps(expected, indent=2, sort_keys=True) + "\n"
    rendered_text = json.dumps(rendered, indent=2, sort_keys=True) + "\n"
    return _text_diff(
        expected_text,
        rendered_text,
        fromfile="golden",
        tofile="rendered",
    )


def _text_diff(expected: str, rendered: str, *, fromfile: str, tofile: str) -> str:
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
