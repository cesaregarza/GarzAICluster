#!/usr/bin/env python3
"""Validate the registry overlay kustomize render against the pre-refactor golden."""

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
REGISTRY_OVERLAY_CONFIGMAP_NAME = "agent-control-plane-registry-overlay"
GOLDEN_CONFIGMAP_PATH = Path(
    "tests/fixtures/agent-control-plane-registry-overlay/configmap.golden.yaml"
)

YAML_PARSER = YAML(typ="safe")


class RegistryOverlayRenderError(RuntimeError):
    """Raised when the registry overlay cannot render or drifts from the golden."""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run real kustomize build for the control-plane registry overlay "
            "and compare the generated ConfigMap data to the committed golden."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
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
            kustomize=args.kustomize,
            golden_configmap_path=golden_path,
        )
    except RegistryOverlayRenderError as exc:
        print(f"registry overlay render gate failed: {exc}", file=sys.stderr)
        return 1
    print("registry overlay kustomize render matches the committed ConfigMap golden.")
    return 0


def check_registry_overlay_render(
    *,
    repo_root: Path,
    kustomize: str,
    golden_configmap_path: Path,
) -> None:
    rendered = _kustomize_build(
        repo_root=repo_root,
        overlay_dir=repo_root / REGISTRY_OVERLAY_DIR,
        kustomize=kustomize,
    )
    rendered_configmap = _extract_registry_configmap(rendered)
    golden_configmap = _load_yaml(golden_configmap_path)
    _assert_configmap_identity_matches(rendered_configmap, golden_configmap)
    _assert_configmap_data_matches(rendered_configmap, golden_configmap)


def _kustomize_build(*, repo_root: Path, overlay_dir: Path, kustomize: str) -> str:
    try:
        result = subprocess.run(
            [kustomize, "build", str(overlay_dir.relative_to(repo_root))],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RegistryOverlayRenderError(
            f"kustomize binary not found: {kustomize}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RegistryOverlayRenderError(f"kustomize build failed: {stderr}")
    if not result.stdout.strip():
        raise RegistryOverlayRenderError("kustomize build produced no output")
    return result.stdout


def _extract_registry_configmap(rendered: str) -> dict[str, Any]:
    for document in YAML_PARSER.load_all(rendered):
        if not isinstance(document, dict):
            continue
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
