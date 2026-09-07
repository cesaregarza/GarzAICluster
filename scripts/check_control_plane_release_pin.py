#!/usr/bin/env python3
"""Fail closed when the control-plane chart pin and runtime image tag diverge."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
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
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the validated pin; default is preview",
    )
    args = parser.parse_args()

    try:
        if args.source_sha or args.image_digest:
            if not args.source_sha or not args.image_digest:
                raise ControlPlanePinError(
                    "--source-sha and --image-digest must be provided together"
                )
            result = update_control_plane_release_pin(
                repo_root=args.repo_root,
                source_sha=args.source_sha,
                image_digest=args.image_digest,
                apply=args.apply,
            )
        else:
            if args.apply:
                raise ControlPlanePinError(
                    "--apply requires --source-sha and --image-digest"
                )
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


PIN_FILES = {
    "values": VALUES_PATH,
    "application": APPLICATION_PATH,
    "sweep": Path(
        "apps/agent-control-plane-runtime-controls/postgres-sweep-cronjob.yaml"
    ),
    "runbook": Path("docs/runbooks/postgres-restore.md"),
    "train_test": Path("tests/test_mandate_deploy_train.py"),
}
IMAGE_PREFIX = "registry.digitalocean.com/sendouq/agent-platform:"
RELEASE_MARKER = "Current GitOps Core release pin:"
# Each pattern owns one literal, preserving surrounding formatting and comments.
PIN_SITES = {
    "tag": ("values", r"^  tag: (sha-[0-9a-f]{12})$"),
    "digest": ("values", r"^  digest: (sha256:[0-9a-f]{64})$"),
    "source": ("application", r"^      targetRevision: ([0-9a-f]{40})$"),
    "sweep": (
        "sweep",
        rf"^              image: {re.escape(IMAGE_PREFIX)}(sha-[0-9a-f]{{12}})$",
    ),
    "runnable": (
        "runbook",
        rf"^          image: {re.escape(IMAGE_PREFIX)}(sha-[0-9a-f]{{12}})$",
    ),
    "fixture": ("train_test", r'^                "([0-9a-f]{40})",$'),
}


def _pin_matches(originals: dict[str, str]) -> dict[str, re.Match[str]]:
    matches = {}
    for label, (name, pattern) in PIN_SITES.items():
        found = list(re.finditer(pattern, originals[name], re.MULTILINE))
        if len(found) != 1:
            raise ControlPlanePinError(
                f"{label} must have exactly one pin marker; found {len(found)}"
            )
        matches[label] = found[0]
    return matches


def _resolve_previous_pin(matches: dict[str, re.Match[str]], source_sha: str) -> str:
    # The deploy-train fixture retains the full previous source when an operator
    # has already changed values/Application. Never infer state from prose.
    sources = {matches[key].group(1) for key in ("source", "fixture")} - {source_sha}
    if len(sources) > 1:
        raise ControlPlanePinError("conflicting previous source pins")
    previous = next(iter(sources), source_sha)
    allowed_tags = {f"sha-{previous[:12]}", f"sha-{source_sha[:12]}"}
    for key in ("tag", "sweep", "runnable"):
        if matches[key].group(1) not in allowed_tags:
            raise ControlPlanePinError(
                f"{key} is inconsistent with previous/requested source pins"
            )
    return previous


def _update_release_reference(text: str, source_sha: str, image_digest: str) -> str:
    line = (
        f"{RELEASE_MARKER} `{IMAGE_PREFIX}sha-{source_sha[:12]}` "
        f"(`{image_digest}`), source `{source_sha}`."
    )
    if RELEASE_MARKER in text:
        pattern = (
            rf"^{re.escape(RELEASE_MARKER)} `{re.escape(IMAGE_PREFIX)}sha-[0-9a-f]{{12}}` "
            r"\(`sha256:[0-9a-f]{64}`\), source `[0-9a-f]{40}`\.$"
        )
        found = list(re.finditer(pattern, text, re.MULTILINE))
        if text.count(RELEASE_MARKER) != 1 or len(found) != 1:
            raise ControlPlanePinError(
                "runbook release reference is missing, malformed or ambiguous"
            )
        return text[: found[0].start()] + line + text[found[0].end() :]
    anchor = "\n## Accepted Recovery Targets\n"
    if text.count(anchor) != 1:
        raise ControlPlanePinError(
            "runbook release insertion anchor is missing or ambiguous"
        )
    return text.replace(anchor, f"\n{line}\n{anchor}", 1)


def _plan_replacements(
    originals: dict[str, str], source_sha: str, image_digest: str
) -> dict[str, str]:
    matches = _pin_matches(originals)
    _resolve_previous_pin(matches, source_sha)
    replacements = {
        "tag": f"sha-{source_sha[:12]}",
        "digest": image_digest,
        "source": source_sha,
        "sweep": f"sha-{source_sha[:12]}",
        "runnable": f"sha-{source_sha[:12]}",
        "fixture": source_sha,
    }
    updated = dict(originals)
    # Reverse offsets keep both values.yaml replacements bound to their matches.
    for key, match in sorted(
        matches.items(), key=lambda item: item[1].start(1), reverse=True
    ):
        name = PIN_SITES[key][0]
        text = updated[name]
        updated[name] = (
            text[: match.start(1)] + replacements[key] + text[match.end(1) :]
        )
    updated["runbook"] = _update_release_reference(
        updated["runbook"], source_sha, image_digest
    )
    return updated


def _write_pin_plan(
    files: dict[str, Path], originals: dict[str, str], updated: dict[str, str]
) -> None:
    """Stage every file before installing; restore originals on an IO failure."""
    staged = {}
    installed = []
    try:
        for name, path in files.items():
            if updated[name] == originals[name]:
                continue
            if path.read_text(encoding="utf-8") != originals[name]:
                raise ControlPlanePinError(f"pin input changed during planning: {path}")
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as tmp:
                staged[name] = Path(tmp.name)
                tmp.write(updated[name])
            staged[name].chmod(path.stat().st_mode & 0o777)
        for name, temporary in staged.items():
            os.replace(temporary, files[name])
            installed.append(name)
    except OSError as exc:
        for name in reversed(installed):
            files[name].write_text(originals[name], encoding="utf-8")
        raise ControlPlanePinError(
            "pin write failed; restored installed originals"
        ) from exc
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def update_control_plane_release_pin(
    *, repo_root: Path, source_sha: str, image_digest: str, apply: bool = False
) -> str:
    """Preview or apply the complete validated Core pin plan."""
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ControlPlanePinError(
            "--source-sha must be a full lowercase 40-character SHA"
        )
    if DIGEST_RE.fullmatch(image_digest) is None:
        raise ControlPlanePinError("--image-digest must be a full sha256 digest")
    files = {name: repo_root / path for name, path in PIN_FILES.items()}
    try:
        originals = {
            name: path.read_text(encoding="utf-8") for name, path in files.items()
        }
    except OSError as exc:
        raise ControlPlanePinError("cannot read all required Core pin files") from exc
    updated = _plan_replacements(originals, source_sha, image_digest)
    changed = sum(updated[name] != originals[name] for name in files)
    if not changed:
        return f"Core release pin already current: {source_sha} {image_digest}"
    if apply:
        _write_pin_plan(files, originals, updated)
    mode = "applied" if apply else "preview"
    return f"Core release pin {mode}: {source_sha} {image_digest}; files={changed}"


def _agent_platform_target_revision(application: dict[str, Any]) -> str:
    spec = _required_mapping(application, "spec", "agent-control-plane application")
    sources = spec.get("sources")
    if not isinstance(sources, list):
        raise ControlPlanePinError(
            "agent-control-plane application spec.sources must be a list"
        )

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
