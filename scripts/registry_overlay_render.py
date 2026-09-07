"""Render and extract the registry-overlay Helm chart."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


REGISTRY_OVERLAY_CONFIGMAP_NAME = "agent-control-plane-registry-overlay"
HELM_RENDER_TIMEOUT_SECONDS = 30
YAML_PARSER = YAML(typ="safe")


class RegistryOverlayRenderError(RuntimeError):
    """Raised when the registry-overlay chart cannot be rendered safely."""


def render_registry_overlay_documents(
    overlay_dir: Path,
    *,
    helm: str = "helm",
) -> list[dict[str, Any]]:
    chart_dir = overlay_dir.resolve()
    if not chart_dir.is_dir() or not (chart_dir / "Chart.yaml").is_file():
        raise RegistryOverlayRenderError(f"registry overlay Helm chart not found: {overlay_dir}")
    command = [
        helm,
        "template",
        REGISTRY_OVERLAY_CONFIGMAP_NAME,
        str(chart_dir),
        "--namespace",
        "agent-control-plane",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=chart_dir.parent,
            capture_output=True,
            text=True,
            check=False,
            timeout=HELM_RENDER_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RegistryOverlayRenderError(f"Helm binary not found: {helm}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RegistryOverlayRenderError(
            f"Helm render exceeded {HELM_RENDER_TIMEOUT_SECONDS}s"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise RegistryOverlayRenderError(f"Helm render failed: {detail}")
    try:
        documents = [
            document
            for document in YAML_PARSER.load_all(result.stdout)
            if isinstance(document, dict) and document
        ]
    except YAMLError as exc:
        raise RegistryOverlayRenderError(
            "Helm render produced malformed YAML"
        ) from exc
    if not documents:
        raise RegistryOverlayRenderError("Helm render produced no YAML objects")
    return documents


def render_registry_overlay_data(
    overlay_dir: Path,
    *,
    helm: str = "helm",
) -> dict[str, str]:
    matches: list[dict[str, Any]] = []
    for document in render_registry_overlay_documents(overlay_dir, helm=helm):
        if document.get("kind") != "ConfigMap":
            continue
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if metadata.get("name") != REGISTRY_OVERLAY_CONFIGMAP_NAME:
            continue
        matches.append(document)

    if len(matches) != 1:
        raise RegistryOverlayRenderError(
            f"rendered {REGISTRY_OVERLAY_CONFIGMAP_NAME} ConfigMap expected "
            f"exactly one match, got {len(matches)}"
        )

    data = matches[0].get("data")
    if not isinstance(data, dict) or not data:
        raise RegistryOverlayRenderError("rendered registry ConfigMap has no data")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in data.items()
    ):
        raise RegistryOverlayRenderError("rendered registry ConfigMap data must be strings")
    return dict(data)
