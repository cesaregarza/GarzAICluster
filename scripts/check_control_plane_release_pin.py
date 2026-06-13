#!/usr/bin/env python3
"""Fail closed when the control-plane chart pin and runtime image tag diverge."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_PATH = Path("argocd/applications/agent-control-plane.yaml")
VALUES_PATH = Path("apps/agent-control-plane/values.yaml")
AGENT_PLATFORM_REPO_URL = "git@github.com:cesaregarza/agent-platform.git"
REVISION_RE = re.compile(r"^[0-9a-fA-F]{12,40}$")

YAML_PARSER = YAML(typ="safe")


class ControlPlanePinError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the agent-platform chart targetRevision with the "
            "agent-control-plane runtime image tag."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--application-path", type=Path, default=APPLICATION_PATH)
    parser.add_argument("--values-path", type=Path, default=VALUES_PATH)
    args = parser.parse_args()

    try:
        result = check_control_plane_release_pin(
            repo_root=args.repo_root,
            application_path=args.application_path,
            values_path=args.values_path,
        )
    except ControlPlanePinError as exc:
        print(f"control-plane release pin gate failed: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


def check_control_plane_release_pin(
    *,
    repo_root: Path,
    application_path: Path,
    values_path: Path,
) -> str:
    application = _load_yaml(repo_root / application_path)
    values = _load_yaml(repo_root / values_path)

    target_revision = _agent_platform_target_revision(application)
    image = _required_mapping(values, "image", "agent-control-plane values")
    image_tag = _required_str(image, "tag", "agent-control-plane image")

    if REVISION_RE.fullmatch(target_revision) is None:
        raise ControlPlanePinError(
            "agent-platform targetRevision must be a 12-40 character hex commit pin"
        )
    expected_tag = f"sha-{target_revision[:12]}"
    if image_tag != expected_tag:
        raise ControlPlanePinError(
            "agent-control-plane image.tag must match agent-platform targetRevision: "
            f"expected {expected_tag}, got {image_tag}"
        )

    return "agent-control-plane chart targetRevision and image tag match."


def _agent_platform_target_revision(application: dict[str, Any]) -> str:
    spec = _required_mapping(application, "spec", "agent-control-plane application")
    sources = spec.get("sources")
    if not isinstance(sources, list):
        raise ControlPlanePinError("agent-control-plane application spec.sources must be a list")

    matches = [
        source
        for source in sources
        if isinstance(source, dict) and source.get("repoURL") == AGENT_PLATFORM_REPO_URL
    ]
    if len(matches) != 1:
        raise ControlPlanePinError(
            "agent-control-plane application must have exactly one "
            f"{AGENT_PLATFORM_REPO_URL} source"
        )
    return _required_str(matches[0], "targetRevision", "agent-platform source")


def _required_mapping(mapping: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ControlPlanePinError(f"{label} missing mapping {key}")
    return value


def _required_str(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ControlPlanePinError(f"{label} missing non-empty {key}")
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ControlPlanePinError(f"YAML mapping expected: {path}")
    return loaded


if __name__ == "__main__":
    sys.exit(main())
