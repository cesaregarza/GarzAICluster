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
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

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
    parser.add_argument("--source-sha", help="full 40-character Core source SHA")
    parser.add_argument("--image-digest", help="full sha256 image digest")
    parser.add_argument("--apply", action="store_true", help="write the validated pin; default is preview")
    args = parser.parse_args()

    try:
        if args.source_sha or args.image_digest:
            if not args.source_sha or not args.image_digest:
                raise ControlPlanePinError("--source-sha and --image-digest must be provided together")
            result = update_control_plane_release_pin(
                repo_root=args.repo_root,
                source_sha=args.source_sha,
                image_digest=args.image_digest,
                apply=args.apply,
            )
        else:
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


def update_control_plane_release_pin(
    *, repo_root: Path, source_sha: str, image_digest: str, apply: bool = False
) -> str:
    """Preview or atomically update every owned Core pin reference."""
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ControlPlanePinError("--source-sha must be a full lowercase 40-character SHA")
    if DIGEST_RE.fullmatch(image_digest) is None:
        raise ControlPlanePinError("--image-digest must be a full sha256 digest")
    tag = f"sha-{source_sha[:12]}"
    files = {
        "values": repo_root / VALUES_PATH,
        "application": repo_root / APPLICATION_PATH,
        "sweep": repo_root / "apps/agent-control-plane-runtime-controls/postgres-sweep-cronjob.yaml",
        "runbook": repo_root / "docs/runbooks/postgres-restore.md",
        "train_test": repo_root / "tests/test_mandate_deploy_train.py",
    }
    originals = {name: path.read_text(encoding="utf-8") for name, path in files.items()}
    updated = dict(originals)

    def replace_once(name: str, old: str, new: str, marker: str) -> None:
        count = updated[name].count(old)
        if count != 1:
            raise ControlPlanePinError(f"{marker} must have exactly one match; found {count}")
        updated[name] = updated[name].replace(old, new, 1)

    def replace_current_or_old(name: str, old: str, new: str, marker: str) -> None:
        if old == new:
            if updated[name].count(new) == 1:
                return
            raise ControlPlanePinError(f"{marker} must have exactly one current match")
        old_count = updated[name].count(old)
        new_count = updated[name].count(new)
        if old_count == 1 and new_count == 0:
            updated[name] = updated[name].replace(old, new, 1)
        elif old_count == 0 and new_count == 1:
            return
        else:
            raise ControlPlanePinError(
                f"{marker} must have exactly one old or new match; old={old_count} new={new_count}"
            )

    values = updated["values"]
    value_tag = re.search(r"(?m)^  tag: (sha-[0-9a-f]{12})$", values)
    value_digest = re.search(r"(?m)^  digest: (sha256:[0-9a-f]{64})$", values)
    if not value_tag or not value_digest:
        raise ControlPlanePinError("values image tag/digest markers are missing or ambiguous")
    old_tag, old_digest = value_tag.group(1), value_digest.group(1)
    application_source = re.search(r"(?m)^      targetRevision: ([0-9a-f]{40})$", updated["application"])
    if application_source is None:
        raise ControlPlanePinError("Application targetRevision marker is missing or ambiguous")
    old_source = application_source.group(1)
    if old_tag == tag:
        historical = re.search(
            r"(?m)^\| Current image \| `registry\.digitalocean\.com/sendouq/agent-platform:(sha-[0-9a-f]{12})` \|$",
            updated["runbook"],
        )
        if historical is None or historical.group(1) == tag:
            raise ControlPlanePinError("runbook historical Current image marker is missing or already current")
        old_tag = historical.group(1)
        old_digest = image_digest
    replace_current_or_old("values", f"  tag: {old_tag}", f"  tag: {tag}", "values image tag")
    replace_current_or_old("values", f"  digest: {old_digest}", f"  digest: {image_digest}", "values image digest")
    replace_current_or_old("application", f"      targetRevision: {old_source}", f"      targetRevision: {source_sha}", "Application targetRevision")
    replace_current_or_old("sweep", f"agent-platform:{old_tag}", f"agent-platform:{tag}", "postgres sweep image")
    replace_current_or_old("runbook", f"          image: registry.digitalocean.com/sendouq/agent-platform:{old_tag}", f"          image: registry.digitalocean.com/sendouq/agent-platform:{tag}", "runbook runnable image")
    replace_current_or_old("train_test", f'                "{old_source}"', f'                "{source_sha}"', "deploy-train expected source")

    release_marker = "Current GitOps Core release pin:"
    release_line = f"{release_marker} `registry.digitalocean.com/sendouq/agent-platform:{tag}` (`{image_digest}`), source `{source_sha}`."
    if release_marker in updated["runbook"]:
        updated["runbook"] = re.sub(rf"(?m)^{re.escape(release_marker)}.*$", release_line, updated["runbook"], count=1)
    else:
        anchor = "\n## Accepted Recovery Targets\n"
        if updated["runbook"].count(anchor) != 1:
            raise ControlPlanePinError("runbook release insertion anchor is missing or ambiguous")
        updated["runbook"] = updated["runbook"].replace(anchor, f"\n{release_line}\n{anchor}", 1)

    changed = [path for name, path in files.items() if updated[name] != originals[name]]
    if not changed:
        return f"Core release pin already current: {source_sha} {image_digest}"
    if apply:
        for name, path in files.items():
            if updated[name] != originals[name]:
                path.write_text(updated[name], encoding="utf-8")
    mode = "applied" if apply else "preview"
    return f"Core release pin {mode}: {source_sha} {image_digest}; files={len(changed)}"


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
