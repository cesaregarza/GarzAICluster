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
    parser.add_argument(
        "--update-golden",
        action="store_true",
        help=(
            "Rewrite the committed ConfigMap golden from the real kustomize render "
            "instead of only checking for drift."
        ),
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
            update_golden=args.update_golden,
        )
    except RegistryOverlayRenderError as exc:
        print(f"registry overlay render gate failed: {exc}", file=sys.stderr)
        return 1
    if args.update_golden:
        print(f"updated registry overlay ConfigMap golden: {golden_path}")
        return 0
    print("registry overlay kustomize render matches the committed ConfigMap golden.")
    return 0


def check_registry_overlay_render(
    *,
    repo_root: Path,
    kustomize: str,
    golden_configmap_path: Path,
    update_golden: bool = False,
) -> None:
    rendered = _kustomize_build(
        repo_root=repo_root,
        overlay_dir=repo_root / REGISTRY_OVERLAY_DIR,
        kustomize=kustomize,
    )
    rendered_configmap = _extract_registry_configmap(rendered)
    if update_golden:
        _write_golden_configmap(rendered_configmap, golden_configmap_path)
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


def _write_golden_configmap(
    rendered_configmap: dict[str, Any],
    golden_configmap_path: Path,
) -> None:
    """Write a stable golden from the rendered ConfigMap.

    Kustomize emits valid YAML but may order top-level fields differently than the
    committed fixture. Preserve the existing fixture's data-key and label order
    where possible so regeneration diffs stay focused on actual registry data.
    """
    rendered_identity = _configmap_identity(rendered_configmap)
    rendered_data = _configmap_data(rendered_configmap, label="rendered")

    existing_identity: dict[str, Any] | None = None
    existing_data: dict[str, str] = {}
    if golden_configmap_path.exists():
        existing = _load_yaml(golden_configmap_path)
        existing_identity = _configmap_identity(existing)
        existing_data = _configmap_data(existing, label="golden")

    data_keys = _stable_order(
        existing_order=existing_data.keys(),
        rendered_keys=rendered_data.keys(),
    )
    label_keys = _stable_order(
        existing_order=(
            (existing_identity or {}).get("metadata", {}).get("labels", {}) or {}
        ).keys(),
        rendered_keys=rendered_identity["metadata"]["labels"].keys(),
    )

    golden_configmap_path.parent.mkdir(parents=True, exist_ok=True)
    golden_configmap_path.write_text(
        _format_golden_configmap(
            identity=rendered_identity,
            data=rendered_data,
            data_keys=data_keys,
            label_keys=label_keys,
        ),
        encoding="utf-8",
    )


def _stable_order(*, existing_order: Any, rendered_keys: Any) -> list[str]:
    rendered = set(rendered_keys)
    ordered = [key for key in existing_order if key in rendered]
    ordered.extend(sorted(key for key in rendered if key not in ordered))
    return ordered


def _format_golden_configmap(
    *,
    identity: dict[str, Any],
    data: dict[str, str],
    data_keys: list[str],
    label_keys: list[str],
) -> str:
    metadata = identity["metadata"]
    labels = metadata["labels"]
    lines = [
        f"apiVersion: {_yaml_scalar(identity['apiVersion'])}",
        f"kind: {_yaml_scalar(identity['kind'])}",
        "metadata:",
        f"  name: {_yaml_scalar(metadata['name'])}",
        f"  namespace: {_yaml_scalar(metadata['namespace'])}",
        "  labels:",
    ]
    for key in label_keys:
        lines.append(f"    {_yaml_key(key)}: {_yaml_scalar(labels[key])}")
    lines.append("data:")
    for key in data_keys:
        value = data[key]
        chomping = "|" if value.endswith("\n") else "|-"
        lines.append(f"  {_yaml_key(key)}: {chomping}")
        value_lines = value.splitlines()
        if value_lines:
            lines.extend(f"    {line}" if line else "" for line in value_lines)
        else:
            lines.append("")
    return "\n".join(lines) + "\n"


def _yaml_key(value: str) -> str:
    if not value or any(char in value for char in " \t\n\r:#{}[],&*?|<>=!%@\\\"'"):
        return json.dumps(value)
    return value


def _yaml_scalar(value: Any) -> str:
    if not isinstance(value, str):
        return json.dumps(value)
    if not value or any(char in value for char in " \t\n\r:#{}[],&*?|<>=!%@\\\"'"):
        return json.dumps(value)
    return value


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
