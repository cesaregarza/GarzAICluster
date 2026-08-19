#!/usr/bin/env python3
"""Run the idempotent CES-395 Mandate deployment choreography."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from ruamel.yaml import YAML

import argocd_core as argo


REPO_ROOT = Path(__file__).resolve().parents[1]
GARZAICLUSTER_REPO = "https://github.com/cesaregarza/GarzAICluster"
GARZAICLUSTER_ORIGIN_IDENTITY = "github.com/cesaregarza/garzaicluster"
ROOT_APPLICATION = "splattop-root"
SKILLS_APPLICATION = "agent-control-plane-skills"
OVERLAY_APPLICATION = "agent-control-plane-registry-overlay"
SKILLS_IMAGE = "registry.digitalocean.com/sendouq/agent-workloads-skills:main"
SKILLS_NAMESPACE = "agent-control-plane"
SKILLS_CONFIGMAP = "mandate-skill-packs"
VERIFY_NAMESPACE = "agent-control-plane"
VERIFY_CRONJOB = "agent-control-plane-synthetic-live-verify"
VERIFY_CONTAINER = "synthetic-live-verify"
VERIFY_DEADLINE_SECONDS = 480
VERIFY_SETTLEMENT_GRACE_SECONDS = 30
RUN_INFO_NAME = "ces-395-run-id"
PRODUCTION_CONTEXT = "do-nyc3-k8s-nyc3-garz-ai"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
ERROR_CONDITIONS = {"ComparisonError", "InvalidSpecError"}


@dataclass(frozen=True)
class Stage:
    name: str
    applications: tuple[str, ...]


STAGES = (
    Stage("secrets", ("agent-control-plane-secrets", "agent-workloads-secrets")),
    Stage("skills", (SKILLS_APPLICATION,)),
    Stage("registry-overlay", (OVERLAY_APPLICATION,)),
    Stage("control-plane", ("agent-control-plane",)),
    Stage("workers", ("agent-workloads",)),
)
MANAGED_APPLICATIONS = tuple(
    application for stage in STAGES for application in stage.applications
)
DEPENDENCIES = (
    ("agent-control-plane-secrets", SKILLS_APPLICATION),
    ("agent-workloads-secrets", SKILLS_APPLICATION),
    (SKILLS_APPLICATION, OVERLAY_APPLICATION),
    (OVERLAY_APPLICATION, "agent-control-plane"),
    (OVERLAY_APPLICATION, "agent-workloads"),
    ("agent-control-plane", "agent-workloads"),
)
APPLICATION_MANIFESTS = {
    ROOT_APPLICATION: Path("argocd/applications/root.yaml"),
    **{
        application: Path("argocd/applications") / f"{application}.yaml"
        for application in MANAGED_APPLICATIONS
    },
}


Runner = argo.Runner
YAML_PARSER = YAML(typ="safe")


class LateDriftDetected(argo.ArgoCoreError):
    """A hard refresh invalidated a preflight no-op plan before manual sync."""


@dataclass(frozen=True)
class ApplicationContract:
    name: str
    identity: dict[str, Any]
    resolved_revisions: tuple[str, ...]
    automated: bool


@dataclass(frozen=True)
class SkillBundle:
    data: dict[str, str]
    digest: str
    skill_count: int
    source_commit: str
    image_digest: str | None = None
    image_reference: str | None = None


@dataclass(frozen=True)
class JourneyContract:
    journey_id: str
    capability_id: str
    expected_status: str
    required_events: tuple[str, ...]


def emit_receipt(stage: str, result: str, **fields: object) -> None:
    print(
        json.dumps(
            {"stage": stage, "result": result, **fields},
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def validate_stage_plan(stages: Sequence[Stage] = STAGES) -> tuple[str, ...]:
    applications = tuple(
        application for stage in stages for application in stage.applications
    )
    if len(applications) != len(set(applications)):
        raise argo.ArgoCoreError(
            "canonical deploy order contains a duplicate Application"
        )
    if set(applications) != set(MANAGED_APPLICATIONS):
        raise argo.ArgoCoreError("canonical deploy Application set drifted")
    position = {application: index for index, application in enumerate(applications)}
    for predecessor, successor in DEPENDENCIES:
        if position[predecessor] >= position[successor]:
            raise argo.ArgoCoreError(
                f"canonical dependency drift: {predecessor} must precede {successor}"
            )
    return applications


def _required_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise argo.ArgoCoreError(f"{label} must be a mapping")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise argo.ArgoCoreError(f"{label} must be a non-empty string")
    return value


def _plain_value(value: object, label: str) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _plain_value(item, f"{label}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_plain_value(item, label) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise argo.ArgoCoreError(f"{label} contains an unsupported value")


def _differing_paths(expected: object, actual: object, prefix: str = "") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        paths: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in expected or key not in actual:
                paths.append(path)
            else:
                paths.extend(_differing_paths(expected[key], actual[key], path))
        return paths
    if expected != actual:
        return [prefix or "root"]
    return []


def _application_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _required_mapping(payload.get("metadata"), "Application metadata")
    spec = _required_mapping(payload.get("spec"), "Application spec")
    return {
        "name": metadata.get("name"),
        # The complete hook-critical spec is an immutable execution contract.
        # This deliberately includes source options, sync options, prune/selfHeal,
        # ignoreDifferences, and revisionHistoryLimit instead of selecting only a
        # few convenient fields.
        "spec": _plain_value(dict(spec), "Application spec"),
    }


def _identity_sources(identity: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    spec = _required_mapping(identity.get("spec"), "Application identity spec")
    raw_sources = spec.get("sources")
    if isinstance(raw_sources, list) and raw_sources:
        return [
            _required_mapping(source, "Application identity source")
            for source in raw_sources
        ]
    return [_required_mapping(spec.get("source"), "Application identity source")]


def _identity_automated(identity: Mapping[str, Any]) -> bool:
    spec = _required_mapping(identity.get("spec"), "Application identity spec")
    sync_policy = spec.get("syncPolicy")
    return isinstance(sync_policy, dict) and isinstance(
        sync_policy.get("automated"), dict
    )


def _identity_sync_options(identity: Mapping[str, Any]) -> tuple[str, ...]:
    spec = _required_mapping(identity.get("spec"), "Application identity spec")
    sync_policy = _required_mapping(
        spec.get("syncPolicy") or {}, "Application identity syncPolicy"
    )
    raw_options = sync_policy.get("syncOptions") or []
    if not isinstance(raw_options, list) or not all(
        isinstance(option, str) for option in raw_options
    ):
        raise argo.ArgoCoreError("Application syncOptions must be strings")
    return tuple(raw_options)


def load_application_contracts(
    repo_root: Path,
    release_sha: str,
) -> dict[str, ApplicationContract]:
    contracts: dict[str, ApplicationContract] = {}
    for application, relative_path in APPLICATION_MANIFESTS.items():
        path = repo_root / relative_path
        try:
            payload = YAML_PARSER.load(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise argo.ArgoCoreError(
                f"could not read Application manifest {path}: {error}"
            ) from error
        manifest = _required_mapping(payload, f"Application manifest {relative_path}")
        identity = _application_identity(manifest)
        if identity["name"] != application:
            raise argo.ArgoCoreError(
                f"Application manifest name drift: expected {application}, got {identity['name']}"
            )
        resolved: list[str] = []
        if application in {SKILLS_APPLICATION, OVERLAY_APPLICATION} and (
            "ApplyOutOfSyncOnly=true" in _identity_sync_options(identity)
        ):
            raise argo.ArgoCoreError(
                f"{application} cannot use ApplyOutOfSyncOnly=true because hooks are required"
            )
        for source in _identity_sources(identity):
            revision = _required_string(
                source.get("targetRevision"),
                f"{application} targetRevision",
            )
            if source.get("repoURL") == GARZAICLUSTER_REPO and revision == "main":
                revision = release_sha
            resolved.append(revision)
        contracts[application] = ApplicationContract(
            name=application,
            identity=identity,
            resolved_revisions=tuple(resolved),
            automated=_identity_automated(identity),
        )
    return contracts


def load_journey_contracts(repo_root: Path) -> tuple[JourneyContract, ...]:
    path = repo_root / "apps/agent-control-plane/values.yaml"
    try:
        payload = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise argo.ArgoCoreError(
            f"could not read verification contract {path}: {error}"
        ) from error
    values = _required_mapping(payload, "agent-control-plane values")
    verify = _required_mapping(values.get("syntheticLiveVerify"), "syntheticLiveVerify")
    if verify.get("enabled") is not True:
        raise argo.ArgoCoreError("syntheticLiveVerify must remain enabled")
    raw_journeys = verify.get("journeys")
    if not isinstance(raw_journeys, list) or not raw_journeys:
        raise argo.ArgoCoreError("syntheticLiveVerify journeys must be non-empty")
    journeys: list[JourneyContract] = []
    for raw_journey in raw_journeys:
        journey = _required_mapping(raw_journey, "syntheticLiveVerify journey")
        events = journey.get("required_event_types") or []
        if not isinstance(events, list) or not all(
            isinstance(event, str) for event in events
        ):
            raise argo.ArgoCoreError("journey required_event_types must be strings")
        journeys.append(
            JourneyContract(
                journey_id=_required_string(journey.get("id"), "journey id"),
                capability_id=_required_string(
                    journey.get("capability_id"), "journey capability_id"
                ),
                expected_status=_required_string(
                    journey.get("expected_status"), "journey expected_status"
                ),
                required_events=tuple(events),
            )
        )
    ids = [journey.journey_id for journey in journeys]
    if len(ids) != len(set(ids)):
        raise argo.ArgoCoreError("syntheticLiveVerify journey ids must be unique")
    expected_ids = {"deployment-smoke", "readonly-query-skill-digests"}
    if set(ids) != expected_ids:
        raise argo.ArgoCoreError(
            f"CES-395 requires exact verification journeys {sorted(expected_ids)}; got {sorted(ids)}"
        )
    readonly = next(
        journey
        for journey in journeys
        if journey.journey_id == "readonly-query-skill-digests"
    )
    if readonly.capability_id != "agent_workloads.readonly_query":
        raise argo.ArgoCoreError("readonly verification capability contract drifted")
    if "model_call.finished" not in readonly.required_events:
        raise argo.ArgoCoreError(
            "readonly verification must require model_call.finished"
        )
    return tuple(journeys)


def validate_release_checkout(
    repo_root: Path,
    release_sha: str,
    *,
    git: str,
    runner: Runner = subprocess.run,
) -> None:
    if not SHA_PATTERN.fullmatch(release_sha):
        raise argo.ArgoCoreError(
            "--confirm-sha must be a full lowercase 40-character SHA"
        )
    checks = (
        ("checkout root", [git, "-C", str(repo_root), "rev-parse", "--show-toplevel"]),
        ("checkout HEAD", [git, "-C", str(repo_root), "rev-parse", "HEAD"]),
        (
            "checkout cleanliness",
            [
                git,
                "-C",
                str(repo_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
            ],
        ),
    )
    results: list[subprocess.CompletedProcess[str]] = []
    for label, command in checks:
        result = argo.run_command(command, runner=runner)
        if result.returncode != 0:
            raise argo.command_failure(f"stage=release-preflight check={label}", result)
        results.append(result)
    actual_root = Path(results[0].stdout.strip()).resolve()
    if actual_root != repo_root.resolve():
        raise argo.ArgoCoreError(
            f"release checkout root mismatch: expected={repo_root.resolve()} actual={actual_root}"
        )
    if results[1].stdout.strip() != release_sha:
        raise argo.ArgoCoreError(
            f"release checkout HEAD mismatch: expected={release_sha} actual={results[1].stdout.strip()}"
        )
    if results[2].stdout.strip():
        raise argo.ArgoCoreError(
            "release checkout is dirty; use a clean exact-main checkout"
        )
    validate_remote_main(
        repo_root,
        release_sha,
        git=git,
        runner=runner,
        stage="checkout",
    )
    emit_receipt("release-preflight", "succeeded", release_sha=release_sha)


def validate_remote_main(
    repo_root: Path,
    release_sha: str,
    *,
    git: str,
    runner: Runner = subprocess.run,
    stage: str,
) -> None:
    origin = argo.run_command(
        [git, "-C", str(repo_root), "remote", "get-url", "origin"],
        runner=runner,
    )
    if origin.returncode != 0:
        raise argo.command_failure(
            f"stage=remote-main-guard deploy_stage={stage} check=origin", origin
        )
    actual_origin = normalize_github_origin(origin.stdout.strip())
    if actual_origin != GARZAICLUSTER_ORIGIN_IDENTITY:
        raise argo.ArgoCoreError(
            "canonical origin mismatch: "
            f"expected={GARZAICLUSTER_ORIGIN_IDENTITY} "
            f"actual={actual_origin or 'invalid'} stage={stage}"
        )
    result = argo.run_command(
        [
            git,
            "-C",
            str(repo_root),
            "ls-remote",
            "--exit-code",
            "origin",
            "refs/heads/main",
        ],
        runner=runner,
    )
    if result.returncode != 0:
        raise argo.command_failure(
            f"stage=remote-main-guard deploy_stage={stage}", result
        )
    remote_fields = result.stdout.strip().split()
    remote_sha = remote_fields[0] if remote_fields else "missing"
    if remote_sha != release_sha:
        raise argo.ArgoCoreError(
            "remote main moved: "
            f"confirmed={release_sha} remote={remote_sha} stage={stage}; "
            "restart with a fresh checkout"
        )
    emit_receipt(
        "remote-main-guard", "succeeded", deploy_stage=stage, release_sha=release_sha
    )


def normalize_github_origin(value: str) -> str | None:
    raw = value.strip()
    if raw.startswith("git@github.com:"):
        path = raw.removeprefix("git@github.com:")
    else:
        parsed = urlparse(raw)
        if parsed.scheme not in {"https", "ssh"} or parsed.hostname != "github.com":
            return None
        if parsed.password is not None or parsed.query or parsed.fragment:
            return None
        if parsed.scheme == "https" and parsed.username is not None:
            return None
        if parsed.scheme == "ssh" and parsed.username not in {None, "git"}:
            return None
        path = parsed.path.lstrip("/")
    normalized_path = path.rstrip("/").removesuffix(".git").lower()
    if normalized_path.count("/") != 1:
        return None
    return f"github.com/{normalized_path}"


def run_drift_gates(
    repo_root: Path,
    *,
    uv: str,
    runner: Runner = subprocess.run,
) -> None:
    gates = (
        (
            "control-plane-release-pin",
            [
                uv,
                "run",
                "--frozen",
                "python",
                "scripts/check_control_plane_release_pin.py",
            ],
        ),
        (
            "workload-identity-digests",
            [
                uv,
                "run",
                "--frozen",
                "python",
                "scripts/check_agent_workloads_identity_digests.py",
                "--repo-root",
                str(repo_root),
                "--check",
            ],
        ),
    )
    for gate, command in gates:
        result = argo.run_command(
            command,
            runner=runner,
            working_directory=repo_root,
        )
        if result.returncode != 0:
            mismatch = (
                result.stderr or result.stdout or "no diagnostic output"
            ).strip()
            if len(mismatch) > 2000:
                mismatch = f"{mismatch[:1997]}..."
            emit_receipt(
                "pre-mutation-drift",
                "failed",
                gate=gate,
                mismatch=mismatch,
                remediation="authorized remint/release-pin workflow, then rerun",
            )
            raise argo.ArgoCoreError(
                f"stage=pre-mutation-drift gate={gate} mismatch={mismatch}; "
                "use the authorized remint/release-pin workflow before rerunning"
            )
        emit_receipt("pre-mutation-drift", "succeeded", gate=gate)


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def validate_skill_bundle_data(data: Mapping[str, Any], label: str) -> SkillBundle:
    if not data or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise argo.ArgoCoreError(
            f"{label} ConfigMap data must contain only string entries"
        )
    manifest_raw = data.get("manifest.json")
    if not isinstance(manifest_raw, str):
        raise argo.ArgoCoreError(f"{label} is missing manifest.json")
    try:
        manifest = json.loads(manifest_raw)
    except json.JSONDecodeError as error:
        raise argo.ArgoCoreError(
            f"{label} manifest.json is invalid: {error}"
        ) from error
    manifest = _required_mapping(manifest, f"{label} manifest")
    expected_manifest_keys = {
        "schema_version",
        "bundle_digest",
        "source",
        "skill_digests",
        "skills",
    }
    if set(manifest) != expected_manifest_keys:
        raise argo.ArgoCoreError(f"{label} manifest keys drifted")
    if manifest.get("schema_version") != "agent-control-plane-skill-bundle.v1":
        raise argo.ArgoCoreError(f"{label} manifest schema_version drifted")
    source = _required_mapping(manifest.get("source"), f"{label} source")
    if set(source) != {"repo", "commit"}:
        raise argo.ArgoCoreError(f"{label} source keys drifted")
    source_repo = _required_string(source.get("repo"), f"{label} source repo")
    if source_repo != "cesaregarza/agent-workloads":
        raise argo.ArgoCoreError(f"{label} source repository identity drifted")
    source_commit = _required_string(source.get("commit"), f"{label} source commit")
    if not SHA_PATTERN.fullmatch(source_commit):
        raise argo.ArgoCoreError(f"{label} source commit must be a full SHA")
    raw_skills = manifest.get("skills")
    if not isinstance(raw_skills, list) or not raw_skills:
        raise argo.ArgoCoreError(f"{label} skills must be a non-empty list")
    skills: list[dict[str, Any]] = []
    ids: list[str] = []
    expected_data_keys = {"manifest.json"}
    for raw_skill in raw_skills:
        skill = _required_mapping(raw_skill, f"{label} skill")
        if set(skill) != {"id", "version", "path", "digest", "requires"}:
            raise argo.ArgoCoreError(f"{label} skill descriptor keys drifted")
        skill_id = _required_string(skill.get("id"), f"{label} skill id")
        version = _required_string(skill.get("version"), f"{label} skill version")
        path = _required_string(skill.get("path"), f"{label} skill path")
        digest = _required_string(skill.get("digest"), f"{label} skill digest")
        requires = skill.get("requires")
        if path != f"{skill_id}.md" or "/" in path:
            raise argo.ArgoCoreError(f"{label} skill path does not match id {skill_id}")
        if not DIGEST_PATTERN.fullmatch(digest):
            raise argo.ArgoCoreError(
                f"{label} skill digest is not sha256 for {skill_id}"
            )
        if not isinstance(requires, list) or not all(
            isinstance(item, str) for item in requires
        ):
            raise argo.ArgoCoreError(
                f"{label} skill requires must be strings for {skill_id}"
            )
        content = data.get(path)
        if not isinstance(content, str):
            raise argo.ArgoCoreError(f"{label} is missing skill content {path}")
        if _sha256_text(content) != digest:
            raise argo.ArgoCoreError(
                f"{label} skill content digest mismatch for {skill_id}"
            )
        expected_data_keys.add(path)
        ids.append(skill_id)
        skills.append(
            {
                "id": skill_id,
                "version": version,
                "path": path,
                "digest": digest,
                "requires": list(requires),
            }
        )
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise argo.ArgoCoreError(f"{label} skill ids must be sorted and unique")
    if set(data) != expected_data_keys:
        raise argo.ArgoCoreError(f"{label} ConfigMap data key set drifted")
    expected_skill_digests = {skill["id"]: skill["digest"] for skill in skills}
    if manifest.get("skill_digests") != expected_skill_digests:
        raise argo.ArgoCoreError(f"{label} redundant skill_digests drifted")
    descriptor = {
        "schema_version": "agent-control-plane-skill-bundle-content.v1",
        "skills": skills,
    }
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    if manifest.get("bundle_digest") != digest:
        raise argo.ArgoCoreError(
            f"{label} bundle_digest does not match canonical content"
        )
    return SkillBundle(dict(data), digest, len(skills), source_commit)


def _bundle_data_from_tar(
    archive_path: Path,
) -> tuple[dict[str, str], dict[str, bytes]]:
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                normalized = member.name.lstrip("./")
                if not normalized.startswith("skill-bundle/"):
                    continue
                relative = normalized.removeprefix("skill-bundle/")
                if (
                    relative == "manifest.json"
                    or relative == "SHA256SUMS"
                    or (relative.startswith("skills/") and relative.endswith(".md"))
                ):
                    if relative in files:
                        raise argo.ArgoCoreError(
                            f"desired skill bundle has duplicate member {relative}"
                        )
                    file_object = archive.extractfile(member)
                    if file_object is None:
                        raise argo.ArgoCoreError(
                            f"could not read desired bundle member {member.name}"
                        )
                    files[relative] = file_object.read()
    except (tarfile.TarError, OSError) as error:
        raise argo.ArgoCoreError(
            f"could not read desired skill bundle archive: {error}"
        ) from error
    if "manifest.json" not in files or "SHA256SUMS" not in files:
        raise argo.ArgoCoreError("desired skill bundle archive is incomplete")
    try:
        checksum_rows = files["SHA256SUMS"].decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise argo.ArgoCoreError(
            "desired skill bundle SHA256SUMS is not UTF-8"
        ) from error
    expected_checksum_paths = set(files) - {"SHA256SUMS"}
    observed_checksum_paths: set[str] = set()
    for row in checksum_rows:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", row)
        if match is None:
            raise argo.ArgoCoreError("desired skill bundle SHA256SUMS is malformed")
        expected_hex, relative = match.groups()
        content = files.get(relative)
        if content is None or hashlib.sha256(content).hexdigest() != expected_hex:
            raise argo.ArgoCoreError(
                f"desired skill bundle checksum mismatch for {relative}"
            )
        observed_checksum_paths.add(relative)
    if observed_checksum_paths != expected_checksum_paths:
        raise argo.ArgoCoreError("desired skill bundle checksum path set drifted")
    data: dict[str, str] = {}
    for relative, content in files.items():
        if relative == "SHA256SUMS":
            continue
        key = "manifest.json" if relative == "manifest.json" else Path(relative).name
        if key in data:
            raise argo.ArgoCoreError(f"desired skill bundle key collision: {key}")
        try:
            data[key] = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise argo.ArgoCoreError(
                f"desired skill bundle member is not UTF-8: {relative}"
            ) from error
    return data, files


def read_desired_skill_bundle(
    *,
    crane: str,
    runner: Runner = subprocess.run,
) -> SkillBundle:
    image_digest = resolve_skill_image_digest(crane=crane, runner=runner)
    image_repository = SKILLS_IMAGE.rsplit(":", 1)[0]
    image_reference = f"{image_repository}@{image_digest}"
    with tempfile.TemporaryDirectory(prefix="ces-395-skills.") as directory:
        archive_path = Path(directory) / "bundle.tar"
        result = argo.run_command(
            [crane, "export", image_reference, str(archive_path)],
            runner=runner,
        )
        if result.returncode != 0:
            raise argo.command_failure("export desired skill bundle", result)
        data, _ = _bundle_data_from_tar(archive_path)
    validated = validate_skill_bundle_data(data, "desired skill bundle")
    bundle = SkillBundle(
        data=validated.data,
        digest=validated.digest,
        skill_count=validated.skill_count,
        source_commit=validated.source_commit,
        image_digest=image_digest,
        image_reference=image_reference,
    )
    emit_receipt(
        "skills-preflight",
        "succeeded",
        bundle_digest=bundle.digest,
        skill_count=bundle.skill_count,
        source_commit=bundle.source_commit,
        image_digest=image_digest,
    )
    return bundle


def resolve_skill_image_digest(*, crane: str, runner: Runner = subprocess.run) -> str:
    result = argo.run_command([crane, "digest", SKILLS_IMAGE], runner=runner)
    if result.returncode != 0:
        raise argo.command_failure("resolve desired skill image digest", result)
    digest = result.stdout.strip()
    if not DIGEST_PATTERN.fullmatch(digest):
        raise argo.ArgoCoreError(
            f"desired skill image returned an invalid digest: {digest or 'missing'}"
        )
    return digest


def recheck_skill_image_digest(
    desired: SkillBundle,
    *,
    crane: str,
    runner: Runner = subprocess.run,
) -> None:
    if desired.image_digest is None or desired.image_reference is None:
        raise argo.ArgoCoreError("desired skill bundle lacks immutable image identity")
    current = resolve_skill_image_digest(crane=crane, runner=runner)
    if current != desired.image_digest:
        raise argo.ArgoCoreError(
            "stage=skills-image-guard reason=tag-moved "
            f"expected={desired.image_digest} actual={current}"
        )
    emit_receipt(
        "skills-image-guard",
        "succeeded",
        image_digest=current,
        bundle_digest=desired.digest,
        source_commit=desired.source_commit,
    )


def read_live_skill_bundle(
    *,
    kubeconfig: Path,
    kubectl: str,
    runner: Runner = subprocess.run,
) -> SkillBundle:
    result = argo.run_command(
        [
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            SKILLS_NAMESPACE,
            "get",
            "configmap",
            SKILLS_CONFIGMAP,
            "--output",
            "json",
        ],
        runner=runner,
    )
    if result.returncode != 0:
        raise argo.command_failure("read live mandate skill bundle", result)
    payload = argo.parse_application_json(result.stdout, "live mandate skill bundle")
    data = _required_mapping(payload.get("data"), "live skill ConfigMap data")
    return validate_skill_bundle_data(data, "live skill bundle")


def validate_live_application(
    contract: ApplicationContract,
    payload: Mapping[str, Any],
    *,
    allow_operation: bool = False,
) -> argo.ApplicationSnapshot:
    actual_identity = _application_identity(payload)
    if actual_identity != contract.identity:
        differing = _differing_paths(contract.identity, actual_identity)
        named = differing[:20]
        if len(differing) > len(named):
            named.append(f"...+{len(differing) - len(named)}")
        raise argo.ArgoCoreError(
            f"live Application identity drift for {contract.name}: "
            f"fields={','.join(named)}"
        )
    metadata = _required_mapping(payload.get("metadata"), f"{contract.name} metadata")
    if metadata.get("deletionTimestamp") is not None:
        raise argo.ArgoCoreError(f"live Application {contract.name} is terminating")
    snapshot = argo.application_snapshot(payload)
    if not allow_operation and (
        snapshot.operation_present or snapshot.operation.phase in argo.ACTIVE_PHASES
    ):
        raise argo.ArgoCoreError(
            f"stage=application-preflight application={contract.name} reason=operation-overlap"
        )
    conditions = sorted(set(snapshot.condition_types) & ERROR_CONDITIONS)
    if conditions:
        raise argo.ArgoCoreError(
            f"live Application {contract.name} has blocking conditions: {','.join(conditions)}"
        )
    return snapshot


def preflight_live_applications(
    contracts: Mapping[str, ApplicationContract],
    *,
    kubeconfig: Path,
    kubectl: str,
    namespace: str,
    runner: Runner = subprocess.run,
) -> dict[str, argo.ApplicationSnapshot]:
    snapshots: dict[str, argo.ApplicationSnapshot] = {}
    for application in (ROOT_APPLICATION, *MANAGED_APPLICATIONS):
        payload = argo.read_application_payload(
            application,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            runner=runner,
        )
        snapshots[application] = validate_live_application(
            contracts[application], payload
        )
    emit_receipt(
        "application-preflight",
        "succeeded",
        applications=1 + len(MANAGED_APPLICATIONS),
    )
    return snapshots


def _status_fingerprint(snapshot: argo.ApplicationSnapshot) -> tuple[object, ...]:
    return (
        snapshot.sync_status,
        snapshot.health_status,
        snapshot.revisions,
        tuple(sorted(snapshot.condition_types)),
        snapshot.operation_present,
        snapshot.operation.fingerprint(),
    )


def assert_preflight_state_unchanged(
    application: str,
    baseline: argo.ApplicationSnapshot,
    current: argo.ApplicationSnapshot,
    *,
    force_sync: bool,
    expected_revisions: Sequence[str],
) -> bool:
    if _status_fingerprint(current) == _status_fingerprint(baseline):
        return False
    operation = current.operation
    safe_passive_settlement = (
        force_sync
        and operation.fingerprint() == baseline.operation.fingerprint()
        and current.revisions == tuple(expected_revisions)
        and not current.operation_present
        and current.sync_status == "Synced"
        and current.health_status == "Healthy"
        and not (set(current.condition_types) & ERROR_CONDITIONS)
    )
    if safe_passive_settlement:
        return False
    allowed_automated_movement = (
        force_sync
        and argo.is_new_operation(baseline.operation, operation)
        and operation.automated
        and operation.revisions in {(), tuple(expected_revisions)}
        and operation.phase not in argo.FAILURE_PHASES
    )
    if not allowed_automated_movement:
        raise argo.ArgoCoreError(
            f"stage=stage-entry application={application} "
            "reason=preflight-state-moved; restart the complete train"
        )
    return True


def train_requires_reconcile(
    contracts: Mapping[str, ApplicationContract],
    snapshots: Mapping[str, argo.ApplicationSnapshot],
    desired_skills: SkillBundle,
    live_skills: SkillBundle,
) -> bool:
    reasons = [
        f"application:{application}"
        for application in (ROOT_APPLICATION, *MANAGED_APPLICATIONS)
        if application not in snapshots
        or not _is_ready(
            snapshots[application], contracts[application].resolved_revisions
        )
    ]
    if live_skills.digest != desired_skills.digest:
        reasons.append("skills:semantic-bundle")
    required = bool(reasons)
    emit_receipt(
        "train-plan",
        "reconcile" if required else "no-op-candidate",
        force_canonical_replay=required,
        reasons=reasons,
    )
    return required


def validate_hard_refresh(
    application: str,
    before: argo.ApplicationSnapshot,
    refreshed: argo.ApplicationSnapshot,
    expected_revisions: Sequence[str],
) -> None:
    if not argo.resource_version_advanced(
        before.resource_version, refreshed.resource_version
    ):
        raise argo.ArgoCoreError(
            f"stage=hard-refresh application={application} reason=resource-version-not-advanced"
        )
    if (
        refreshed.reconciled_at is None
        or refreshed.reconciled_at == before.reconciled_at
    ):
        raise argo.ArgoCoreError(
            f"stage=hard-refresh application={application} reason=reconciled-at-not-advanced"
        )
    if refreshed.refresh_annotation is not None:
        raise argo.ArgoCoreError(
            f"stage=hard-refresh application={application} reason=refresh-not-consumed"
        )
    if refreshed.revisions != tuple(expected_revisions):
        raise argo.ArgoCoreError(
            f"stage=hard-refresh application={application} reason=revision-drift "
            f"expected={tuple(expected_revisions)} actual={refreshed.revisions or 'missing'}"
        )
    conditions = sorted(set(refreshed.condition_types) & ERROR_CONDITIONS)
    if conditions:
        raise argo.ArgoCoreError(
            f"stage=hard-refresh application={application} conditions={','.join(conditions)}"
        )


def assert_full_hook_operation(
    application: str, operation: argo.OperationSnapshot
) -> None:
    if operation.selected_resources:
        raise argo.ArgoCoreError(
            f"stage=sync application={application} reason=selective-sync-refused"
        )
    if application in {SKILLS_APPLICATION, OVERLAY_APPLICATION} and any(
        option == "ApplyOutOfSyncOnly=true" for option in operation.sync_options
    ):
        raise argo.ArgoCoreError(
            f"stage=sync application={application} reason=apply-out-of-sync-only-refused"
        )
    # Argo's controller persists nil for the default Hook strategy on automated
    # syncs. Manual CES-395 submissions pass --strategy hook and persist it.
    if operation.sync_strategy not in {None, "hook"}:
        raise argo.ArgoCoreError(
            f"stage=sync application={application} reason=non-hook-strategy "
            f"actual={operation.sync_strategy or 'missing'}"
        )


def assert_overlay_hooks(operation: argo.OperationSnapshot) -> None:
    expected = (
        ("registry-overlay-rollout-strategy-", "Sync", "Sync"),
        ("registry-overlay-restart-", "PostSync", "PostSync"),
    )
    for prefix, hook_type, sync_phase in expected:
        matches = [
            resource
            for resource in operation.resources
            if resource.kind == "Job"
            and resource.namespace == "agent-control-plane"
            and resource.name.startswith(prefix)
            and resource.hook_type == hook_type
            and resource.sync_phase == sync_phase
            and resource.hook_phase == "Succeeded"
        ]
        if len(matches) != 1:
            raise argo.ArgoCoreError(
                f"stage=registry-overlay reason=hook-evidence-missing prefix={prefix} "
                f"matches={len(matches)}"
            )


def _is_ready(snapshot: argo.ApplicationSnapshot, expected: Sequence[str]) -> bool:
    return (
        not snapshot.operation_present
        and snapshot.operation.phase not in argo.ACTIVE_PHASES
        and snapshot.sync_status == "Synced"
        and snapshot.health_status == "Healthy"
        and snapshot.revisions == tuple(expected)
        and not (set(snapshot.condition_types) & ERROR_CONDITIONS)
    )


def _adopt_automated_operation(
    application: str,
    *,
    before: argo.OperationSnapshot,
    initial: argo.ApplicationSnapshot,
    expected_revisions: Sequence[str],
    kubeconfig: Path,
    kubectl: str,
    namespace: str,
    operation_timeout: float,
    adoption_timeout: float,
    interval: float,
    runner: Runner,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> argo.OperationSnapshot | None:
    deadline = monotonic() + adoption_timeout
    current = initial
    while True:
        if argo.is_new_operation(before, current.operation):
            return argo.poll_operation(
                application,
                before=before,
                expected_revisions=expected_revisions,
                expected_automated=True,
                run_id=None,
                kubeconfig=kubeconfig,
                kubectl=kubectl,
                namespace=namespace,
                timeout=operation_timeout,
                interval=interval,
                runner=runner,
                initial=current,
            )
        if _is_ready(current, expected_revisions):
            return None
        if (
            current.operation.phase in argo.ACTIVE_PHASES
            and not current.operation.automated
        ):
            raise argo.ArgoCoreError(
                f"stage=sync application={application} reason=unexpected-manual-overlap"
            )
        if monotonic() >= deadline:
            return None
        sleeper(interval)
        current = argo.read_application_snapshot(
            application,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            runner=runner,
        )


def reconcile_application(
    contract: ApplicationContract,
    *,
    stage: str,
    invocation_id: str,
    desired_skills: SkillBundle,
    preflight_snapshot: argo.ApplicationSnapshot,
    preflight_skill_digest: str | None,
    force_sync: bool,
    kubeconfig: Path,
    argocd: str,
    kubectl: str,
    namespace: str,
    refresh_timeout: float,
    operation_timeout: float,
    adoption_timeout: float,
    interval: float,
    runner: Runner = subprocess.run,
) -> str:
    payload = argo.read_application_payload(
        contract.name,
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        namespace=namespace,
        runner=runner,
    )
    before = validate_live_application(contract, payload, allow_operation=force_sync)
    early_automated = assert_preflight_state_unchanged(
        contract.name,
        preflight_snapshot,
        before,
        force_sync=force_sync,
        expected_revisions=contract.resolved_revisions,
    )
    if contract.name == SKILLS_APPLICATION:
        live_at_entry = read_live_skill_bundle(
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            runner=runner,
        )
        stable_bundle = (
            preflight_skill_digest is not None
            and live_at_entry.digest == preflight_skill_digest
        )
        expected_early_bundle = (
            early_automated and live_at_entry.digest == desired_skills.digest
        )
        if not stable_bundle and not expected_early_bundle:
            raise argo.ArgoCoreError(
                "stage=skills reason=preflight-bundle-moved; restart the complete train"
            )
    if early_automated:
        completed_early = argo.poll_operation(
            contract.name,
            before=preflight_snapshot.operation,
            expected_revisions=contract.resolved_revisions,
            expected_automated=True,
            run_id=None,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            timeout=operation_timeout,
            interval=interval,
            runner=runner,
            initial=before,
        )
        assert_full_hook_operation(contract.name, completed_early)
        before = argo.poll_application_ready(
            contract.name,
            expected_revisions=contract.resolved_revisions,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            timeout=operation_timeout,
            interval=interval,
            runner=runner,
        )
        emit_receipt(
            stage,
            "settled",
            application=contract.name,
            reason="exact-automated-movement-before-canonical-stage",
        )
    emit_receipt(
        "hard-refresh", "started", application=contract.name, deploy_stage=stage
    )
    refreshed_payload, refreshed = argo.hard_refresh_application(
        contract.name,
        kubeconfig=kubeconfig,
        argocd=argocd,
        timeout=refresh_timeout,
        runner=runner,
    )
    validate_live_application(contract, refreshed_payload, allow_operation=True)
    validate_hard_refresh(
        contract.name,
        before,
        refreshed,
        contract.resolved_revisions,
    )
    emit_receipt(
        "hard-refresh",
        "succeeded",
        application=contract.name,
        deploy_stage=stage,
        revisions=list(refreshed.revisions),
    )

    skills_drift = False
    if contract.name == SKILLS_APPLICATION:
        live_skills = read_live_skill_bundle(
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            runner=runner,
        )
        skills_drift = live_skills.digest != desired_skills.digest
        emit_receipt(
            "skills-semantic-drift",
            "changed" if skills_drift else "unchanged",
            desired_digest=desired_skills.digest,
            live_digest=live_skills.digest,
            skill_count=desired_skills.skill_count,
        )

    late_drift = not force_sync and (
        skills_drift
        or refreshed.sync_status != "Synced"
        or refreshed.revisions != contract.resolved_revisions
        or argo.is_new_operation(before.operation, refreshed.operation)
    )

    automated_completed = _adopt_automated_operation(
        contract.name,
        before=before.operation,
        initial=refreshed,
        expected_revisions=contract.resolved_revisions,
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        namespace=namespace,
        operation_timeout=operation_timeout,
        adoption_timeout=adoption_timeout if contract.automated else 0,
        interval=interval,
        runner=runner,
    )
    completed = automated_completed
    outcome = "automated" if automated_completed is not None else None

    current = argo.read_application_snapshot(
        contract.name,
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        namespace=namespace,
        runner=runner,
    )
    if automated_completed is not None and not force_sync:
        current = argo.poll_application_ready(
            contract.name,
            expected_revisions=contract.resolved_revisions,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            timeout=operation_timeout,
            interval=interval,
            runner=runner,
        )
        emit_receipt(
            stage,
            "settled",
            application=contract.name,
            reason="late-automated-operation-before-train-restart",
        )
    late_drift = late_drift or (
        not force_sync
        and (
            completed is not None
            or skills_drift
            or current.sync_status != "Synced"
            or current.revisions != contract.resolved_revisions
        )
    )
    if automated_completed is not None and force_sync:
        # The hard refresh may start auto-sync. Observe its exact terminal state
        # and health before the canonical correlated replay for this stage.
        assert_full_hook_operation(contract.name, automated_completed)
        current = argo.poll_application_ready(
            contract.name,
            expected_revisions=contract.resolved_revisions,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            timeout=operation_timeout,
            interval=interval,
            runner=runner,
        )
        emit_receipt(
            stage,
            "settled",
            application=contract.name,
            reason="automated-operation-before-canonical-replay",
        )
        completed = None

    needs_sync = skills_drift or not _is_ready(current, contract.resolved_revisions)
    if late_drift:
        if current.operation_present or current.operation.phase in argo.ACTIVE_PHASES:
            raise argo.ArgoCoreError(
                f"stage={stage} application={contract.name} "
                "reason=late-drift-operation-still-active"
            )
        raise LateDriftDetected(
            f"stage={stage} application={contract.name} "
            "reason=late-hard-refresh-drift; restarting the full canonical train"
        )
    if not force_sync and completed is None and not needs_sync:
        emit_receipt(
            stage,
            "skipped",
            application=contract.name,
            reason="already-reconciled",
            revisions=list(current.revisions),
        )
        return "skipped"

    if (
        not force_sync
        and completed is None
        and not skills_drift
        and current.sync_status == "Synced"
    ):
        argo.poll_application_ready(
            contract.name,
            expected_revisions=contract.resolved_revisions,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            timeout=operation_timeout,
            interval=interval,
            runner=runner,
        )
        emit_receipt(
            stage,
            "waited",
            application=contract.name,
            reason="health-settled",
        )
        return "waited"

    if force_sync or completed is None:
        if current.operation_present or current.operation.phase in argo.ACTIVE_PHASES:
            raise argo.ArgoCoreError(
                f"stage={stage} application={contract.name} reason=operation-overlap"
            )
        run_id = f"{invocation_id}-{contract.name}"[:80]
        emit_receipt(stage, "started", application=contract.name, run_id=run_id)
        argo.submit_sync(
            contract.name,
            revisions=contract.resolved_revisions,
            run_id=run_id,
            kubeconfig=kubeconfig,
            argocd=argocd,
            runner=runner,
        )
        completed = argo.poll_operation(
            contract.name,
            before=current.operation,
            expected_revisions=contract.resolved_revisions,
            expected_automated=False,
            run_id=run_id,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            timeout=operation_timeout,
            interval=interval,
            runner=runner,
        )
        outcome = "manual"

    assert_full_hook_operation(contract.name, completed)
    if contract.name == OVERLAY_APPLICATION:
        assert_overlay_hooks(completed)
    ready = argo.poll_application_ready(
        contract.name,
        expected_revisions=contract.resolved_revisions,
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        namespace=namespace,
        timeout=operation_timeout,
        interval=interval,
        runner=runner,
    )
    receipt_fields: dict[str, object] = {
        "application": contract.name,
        "outcome": outcome or "unknown",
        "revisions": list(ready.revisions),
    }
    if contract.name == OVERLAY_APPLICATION:
        receipt_fields.update(
            {
                "rollout_strategy_hook": "succeeded",
                "boot_cache_restart": "completed",
            }
        )
    if contract.name == SKILLS_APPLICATION:
        live_after = read_live_skill_bundle(
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            runner=runner,
        )
        if live_after.digest != desired_skills.digest:
            raise argo.ArgoCoreError(
                "stage=skills result=failed reason=bundle-still-drifted "
                f"expected={desired_skills.digest} actual={live_after.digest}"
            )
        receipt_fields["bundle_digest"] = live_after.digest
        receipt_fields["skill_count"] = live_after.skill_count
    emit_receipt(stage, "succeeded", **receipt_fields)
    return outcome or "synced"


def ensure_no_active_verify_job(
    *,
    kubeconfig: Path,
    kubectl: str,
    runner: Runner = subprocess.run,
) -> None:
    result = argo.run_command(
        [
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            VERIFY_NAMESPACE,
            "get",
            "jobs",
            "--output",
            "json",
        ],
        runner=runner,
    )
    if result.returncode != 0:
        raise argo.command_failure("list synthetic live-verify Jobs", result)
    payload = argo.parse_application_json(
        result.stdout, "synthetic live-verify Job list"
    )
    items = payload.get("items")
    if not isinstance(items, list):
        raise argo.ArgoCoreError("synthetic live-verify Job list items must be a list")
    nonterminal: list[str] = []
    for raw_item in items:
        item = _required_mapping(raw_item, "synthetic live-verify Job")
        status = _required_mapping(
            item.get("status") or {}, "synthetic live-verify Job status"
        )
        metadata = _required_mapping(
            item.get("metadata"), "synthetic live-verify Job metadata"
        )
        name = str(metadata.get("name") or "unknown")
        owner_references = metadata.get("ownerReferences") or []
        if not isinstance(owner_references, list):
            raise argo.ArgoCoreError("verification Job ownerReferences must be a list")
        owned_by_cronjob = any(
            isinstance(reference, dict)
            and reference.get("kind") == "CronJob"
            and reference.get("name") == VERIFY_CRONJOB
            for reference in owner_references
        )
        labels = _required_mapping(
            metadata.get("labels") or {}, "synthetic live-verify Job labels"
        )
        matches_verify = (
            owned_by_cronjob
            or name.startswith(f"{VERIFY_CRONJOB}-")
            or name.startswith("agent-control-plane-postdeploy-")
            or RUN_INFO_NAME in labels
        )
        conditions = status.get("conditions") or []
        if not isinstance(conditions, list):
            raise argo.ArgoCoreError("verification Job conditions must be a list")
        terminal = any(
            isinstance(condition, dict)
            and condition.get("type") in {"Complete", "Failed"}
            and condition.get("status") == "True"
            for condition in conditions
        )
        if matches_verify and not terminal:
            nonterminal.append(name)
    if nonterminal:
        raise argo.ArgoCoreError(
            "stage=mandate-verify reason=nonterminal-job-overlap jobs="
            + ",".join(sorted(nonterminal))
        )


def build_verify_job_payload(
    template: Mapping[str, Any], job_name: str, run_id: str
) -> dict[str, Any]:
    job = copy.deepcopy(dict(template))
    if job.get("kind") != "Job":
        raise argo.ArgoCoreError("CronJob-derived verification template is not a Job")
    metadata = _required_mapping(job.get("metadata"), "verification Job metadata")
    metadata["name"] = job_name
    labels = metadata.get("labels") or {}
    if not isinstance(labels, dict):
        raise argo.ArgoCoreError("verification Job labels must be a mapping")
    labels[RUN_INFO_NAME] = run_id
    metadata["labels"] = labels
    spec = _required_mapping(job.get("spec"), "verification Job spec")
    if spec.get("activeDeadlineSeconds") != VERIFY_DEADLINE_SECONDS:
        raise argo.ArgoCoreError(
            "verification Job activeDeadlineSeconds drifted: "
            f"expected={VERIFY_DEADLINE_SECONDS} "
            f"actual={spec.get('activeDeadlineSeconds') or 'missing'}"
        )
    template_spec = _required_mapping(
        _required_mapping(spec.get("template"), "verification pod template").get(
            "spec"
        ),
        "verification pod spec",
    )
    containers = template_spec.get("containers")
    if not isinstance(containers, list):
        raise argo.ArgoCoreError("verification Job containers must be a list")
    matches = [
        container
        for container in containers
        if isinstance(container, dict) and container.get("name") == VERIFY_CONTAINER
    ]
    if len(matches) != 1:
        raise argo.ArgoCoreError(
            "verification Job must contain exactly one named verifier container"
        )
    container = matches[0]
    existing_args = container.get("args") or []
    if not isinstance(existing_args, list) or not all(
        isinstance(arg, str) for arg in existing_args
    ):
        raise argo.ArgoCoreError("verification Job args must be strings")
    if "--format" in existing_args:
        raise argo.ArgoCoreError(
            "verification Job already declares --format; refusing ambiguity"
        )
    container["command"] = ["mandate", "verify"]
    container["args"] = [*existing_args, "--format", "json"]
    return job


def render_verify_job(
    job_name: str,
    run_id: str,
    *,
    kubeconfig: Path,
    kubectl: str,
    runner: Runner,
) -> dict[str, Any]:
    dry_run = argo.run_command(
        [
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            VERIFY_NAMESPACE,
            "create",
            "job",
            f"--from=cronjob/{VERIFY_CRONJOB}",
            job_name,
            "--dry-run=client",
            "--output",
            "json",
        ],
        runner=runner,
    )
    if dry_run.returncode != 0:
        raise argo.command_failure("render mandate verify Job", dry_run)
    template = argo.parse_application_json(dry_run.stdout, "CronJob-derived verify Job")
    return build_verify_job_payload(template, job_name, run_id)


def preflight_mandate_verify(
    *,
    invocation_id: str,
    kubeconfig: Path,
    kubectl: str,
    runner: Runner = subprocess.run,
) -> None:
    ensure_no_active_verify_job(
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        runner=runner,
    )
    render_verify_job(
        "agent-control-plane-postdeploy-preflight",
        invocation_id,
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        runner=runner,
    )
    emit_receipt(
        "mandate-verify-preflight",
        "succeeded",
        cronjob=VERIFY_CRONJOB,
        command="mandate verify --format json",
    )


def validate_verify_timeout(timeout: float) -> float:
    if timeout != float(VERIFY_DEADLINE_SECONDS):
        raise argo.ArgoCoreError(
            "verify timeout must exactly match the bounded CronJob deadline: "
            f"expected={VERIFY_DEADLINE_SECONDS}s actual={timeout:g}s"
        )
    return timeout


def parse_verify_report(
    raw: str,
    journeys: Sequence[JourneyContract],
) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise argo.ArgoCoreError(
            f"stage=mandate-verify reason=invalid-json: {error}"
        ) from error
    report = _required_mapping(payload, "mandate verify report")
    if report.get("ok") is not True:
        stage = report.get("stage") or "unknown"
        raise argo.ArgoCoreError(
            f"stage=mandate-verify result=failed verify_stage={stage}"
        )
    raw_results = report.get("results")
    if not isinstance(raw_results, list):
        raise argo.ArgoCoreError("mandate verify results must be a list")
    results: dict[str, Mapping[str, Any]] = {}
    for raw_result in raw_results:
        result = _required_mapping(raw_result, "mandate verify journey result")
        journey_id = _required_string(result.get("journey_id"), "verify journey_id")
        if journey_id in results:
            raise argo.ArgoCoreError(f"duplicate mandate verify journey: {journey_id}")
        results[journey_id] = result
    expected_ids = {journey.journey_id for journey in journeys}
    if set(results) != expected_ids:
        raise argo.ArgoCoreError(
            f"mandate verify journey set mismatch: expected={sorted(expected_ids)} "
            f"actual={sorted(results)}"
        )
    for journey in journeys:
        result = results[journey.journey_id]
        if result.get("capability_id") != journey.capability_id:
            raise argo.ArgoCoreError(
                f"journey={journey.journey_id} capability mismatch"
            )
        if result.get("ok") is not True or result.get("stage") != "complete":
            raise argo.ArgoCoreError(
                f"journey={journey.journey_id} did not PASS stage=complete"
            )
        if result.get("status") != journey.expected_status:
            raise argo.ArgoCoreError(
                f"journey={journey.journey_id} status mismatch: "
                f"expected={journey.expected_status} actual={result.get('status')}"
            )
        events = result.get("events")
        if not isinstance(events, list) or not all(
            isinstance(event, str) for event in events
        ):
            raise argo.ArgoCoreError(
                f"journey={journey.journey_id} events must be strings"
            )
        missing = sorted(set(journey.required_events) - set(events))
        if missing:
            raise argo.ArgoCoreError(
                f"journey={journey.journey_id} missing events={','.join(missing)}"
            )
    return dict(report)


def _successful_verify_pod(
    job_name: str,
    *,
    kubeconfig: Path,
    kubectl: str,
    runner: Runner,
) -> str:
    result = argo.run_command(
        [
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            VERIFY_NAMESPACE,
            "get",
            "pods",
            "--selector",
            f"job-name={job_name}",
            "--output",
            "json",
        ],
        runner=runner,
    )
    if result.returncode != 0:
        raise argo.command_failure("list mandate verify Pods", result)
    payload = argo.parse_application_json(result.stdout, "mandate verify Pod list")
    items = payload.get("items")
    if not isinstance(items, list):
        raise argo.ArgoCoreError("mandate verify Pod list items must be a list")
    successful: list[tuple[str, str]] = []
    for raw_item in items:
        item = _required_mapping(raw_item, "mandate verify Pod")
        status = _required_mapping(
            item.get("status") or {}, "mandate verify Pod status"
        )
        metadata = _required_mapping(
            item.get("metadata"), "mandate verify Pod metadata"
        )
        if status.get("phase") == "Succeeded":
            successful.append(
                (
                    str(metadata.get("creationTimestamp") or ""),
                    _required_string(metadata.get("name"), "mandate verify Pod name"),
                )
            )
    if not successful:
        raise argo.ArgoCoreError("stage=mandate-verify reason=no-successful-pod")
    return sorted(successful)[-1][1]


def run_mandate_verify(
    journeys: Sequence[JourneyContract],
    *,
    kubeconfig: Path,
    kubectl: str,
    timeout: float,
    runner: Runner = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    run_id: str | None = None,
) -> str:
    validate_verify_timeout(timeout)
    ensure_no_active_verify_job(
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        runner=runner,
    )
    verify_run_id = run_id or f"ces395-{uuid.uuid4().hex[:12]}"
    suffix = verify_run_id[-12:]
    job_name = (
        f"agent-control-plane-postdeploy-{now().strftime('%Y%m%d%H%M%S')}-{suffix}"
    )
    job = render_verify_job(
        job_name,
        verify_run_id,
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        runner=runner,
    )
    emit_receipt("mandate-verify", "started", job=job_name, command="mandate verify")
    create = argo.run_command(
        [
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            VERIFY_NAMESPACE,
            "create",
            "--filename",
            "-",
        ],
        runner=runner,
        input_text=json.dumps(job, sort_keys=True, separators=(",", ":")),
    )
    if create.returncode != 0:
        raise argo.command_failure("create mandate verify Job", create)
    wait = argo.run_command(
        [
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            VERIFY_NAMESPACE,
            "wait",
            "--for=condition=complete",
            f"--timeout={VERIFY_DEADLINE_SECONDS + VERIFY_SETTLEMENT_GRACE_SECONDS}s",
            f"job/{job_name}",
        ],
        runner=runner,
    )
    if wait.returncode != 0:
        raise argo.command_failure("stage=mandate-verify phase=wait", wait)
    pod_name = _successful_verify_pod(
        job_name,
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        runner=runner,
    )
    logs = argo.run_command(
        [
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "--namespace",
            VERIFY_NAMESPACE,
            "logs",
            pod_name,
            "--container",
            VERIFY_CONTAINER,
        ],
        runner=runner,
    )
    if logs.returncode != 0:
        raise argo.command_failure("read mandate verify result", logs)
    parse_verify_report(logs.stdout, journeys)
    emit_receipt(
        "mandate-verify",
        "succeeded",
        job=job_name,
        journeys=[journey.journey_id for journey in journeys],
        readonly_event="model_call.finished",
    )
    return job_name


def run_deploy_train(
    *,
    repo_root: Path,
    release_sha: str,
    kubeconfig: Path,
    argocd: str,
    kubectl: str,
    uv: str,
    git: str,
    crane: str,
    namespace: str,
    refresh_timeout: float,
    operation_timeout: float,
    adoption_timeout: float,
    verify_timeout: float,
    interval: float,
    runner: Runner = subprocess.run,
    invocation_id: str | None = None,
) -> str:
    validate_stage_plan()
    validate_verify_timeout(verify_timeout)
    validate_release_checkout(repo_root, release_sha, git=git, runner=runner)
    contracts = load_application_contracts(repo_root, release_sha)
    journeys = load_journey_contracts(repo_root)
    run_drift_gates(repo_root, uv=uv, runner=runner)
    desired_skills = read_desired_skill_bundle(crane=crane, runner=runner)
    snapshots = preflight_live_applications(
        contracts,
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        namespace=namespace,
        runner=runner,
    )
    live_skills = read_live_skill_bundle(
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        runner=runner,
    )
    force_sync = train_requires_reconcile(
        contracts, snapshots, desired_skills, live_skills
    )

    invocation = invocation_id or f"ces395-{uuid.uuid4().hex[:12]}"
    preflight_mandate_verify(
        invocation_id=invocation,
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        runner=runner,
    )
    validate_remote_main(
        repo_root,
        release_sha,
        git=git,
        runner=runner,
        stage="final-pre-mutation",
    )

    def execute_application(application: str, stage: str) -> str:
        validate_remote_main(
            repo_root,
            release_sha,
            git=git,
            runner=runner,
            stage=f"{stage}:{application}",
        )
        if application == SKILLS_APPLICATION:
            recheck_skill_image_digest(
                desired_skills,
                crane=crane,
                runner=runner,
            )
        return reconcile_application(
            contracts[application],
            stage=stage,
            invocation_id=invocation,
            desired_skills=desired_skills,
            preflight_snapshot=snapshots[application],
            preflight_skill_digest=(
                live_skills.digest if application == SKILLS_APPLICATION else None
            ),
            force_sync=force_sync,
            kubeconfig=kubeconfig,
            argocd=argocd,
            kubectl=kubectl,
            namespace=namespace,
            refresh_timeout=refresh_timeout,
            operation_timeout=operation_timeout,
            adoption_timeout=adoption_timeout,
            interval=interval,
            runner=runner,
        )

    def execute_pass() -> list[str]:
        pass_outcomes = [execute_application(ROOT_APPLICATION, "application-specs")]
        for stage in STAGES:
            for application in stage.applications:
                pass_outcomes.append(execute_application(application, stage.name))
        return pass_outcomes

    try:
        outcomes = execute_pass()
    except LateDriftDetected as error:
        emit_receipt(
            "train-restart",
            "required",
            reason="late-hard-refresh-drift",
            detail=str(error),
        )
        snapshots = preflight_live_applications(
            contracts,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            namespace=namespace,
            runner=runner,
        )
        live_skills = read_live_skill_bundle(
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            runner=runner,
        )
        force_sync = True
        preflight_mandate_verify(
            invocation_id=invocation,
            kubeconfig=kubeconfig,
            kubectl=kubectl,
            runner=runner,
        )
        validate_remote_main(
            repo_root,
            release_sha,
            git=git,
            runner=runner,
            stage="late-drift-restart",
        )
        outcomes = execute_pass()
    changed = any(outcome in {"manual", "automated", "synced"} for outcome in outcomes)
    if not changed:
        emit_receipt("mandate-verify", "skipped", reason="no-changes")
        emit_receipt(
            "deploy-train",
            "no-op",
            release_sha=release_sha,
            applications=len(outcomes),
            synced=0,
        )
        return "no-op"
    run_mandate_verify(
        journeys,
        kubeconfig=kubeconfig,
        kubectl=kubectl,
        timeout=verify_timeout,
        runner=runner,
        run_id=invocation,
    )
    emit_receipt(
        "deploy-train",
        "succeeded",
        release_sha=release_sha,
        applications=len(outcomes),
        manual=outcomes.count("manual"),
        automated=outcomes.count("automated"),
    )
    return "succeeded"


def validate_authorization(apply: bool, release_sha: str | None) -> str:
    if not apply:
        raise argo.ArgoCoreError("deploy train requires --apply")
    if release_sha is None or not SHA_PATTERN.fullmatch(release_sha):
        raise argo.ArgoCoreError(
            "deploy train requires --confirm-sha with the exact full lowercase main SHA"
        )
    return release_sha


def validate_production_context(context: str | None) -> str:
    if context != PRODUCTION_CONTEXT:
        raise argo.ArgoCoreError(
            "deploy train requires the exact production kube context "
            f"{PRODUCTION_CONTEXT}; got {context or 'missing'}"
        )
    return context


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Hard-refresh and settle the Mandate Argo applications in canonical "
            "order, then run a stage-named on-demand verification."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-sha")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--kubeconfig", type=Path, default=Path.home() / ".kube" / "config"
    )
    parser.add_argument(
        "--context",
        required=True,
        choices=(PRODUCTION_CONTEXT,),
        help="Exact production kube context (required; no current-context fallback).",
    )
    parser.add_argument("--namespace", default="argocd")
    parser.add_argument("--argocd", default=argo.default_argocd_executable())
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--git", default="git")
    parser.add_argument("--crane", default="crane")
    parser.add_argument("--refresh-timeout", type=float, default=180.0)
    parser.add_argument("--operation-timeout", type=float, default=1800.0)
    parser.add_argument("--adoption-timeout", type=float, default=10.0)
    parser.add_argument(
        "--verify-timeout",
        type=float,
        choices=(float(VERIFY_DEADLINE_SECONDS),),
        default=float(VERIFY_DEADLINE_SECONDS),
        help="Must match the immutable verification Job active deadline.",
    )
    parser.add_argument("--poll-interval", type=float, default=2.0)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        release_sha = validate_authorization(args.apply, args.confirm_sha)
        context = validate_production_context(args.context)
        timeouts = (
            args.refresh_timeout,
            args.operation_timeout,
            args.verify_timeout,
            args.poll_interval,
        )
        if any(value <= 0 for value in timeouts) or args.adoption_timeout < 0:
            raise argo.ArgoCoreError(
                "timeouts must be positive and adoption timeout non-negative"
            )
        argocd = argo.resolve_executable(args.argocd)
        kubectl = argo.resolve_executable(args.kubectl)
        uv = argo.resolve_executable(args.uv)
        git = argo.resolve_executable(args.git)
        crane = argo.resolve_executable(args.crane)
        argo.validate_argocd_version(argocd, argo.pinned_version())
        with argo.core_kubeconfig(
            args.kubeconfig,
            kubectl=kubectl,
            namespace=args.namespace,
            context=context,
        ) as kubeconfig:
            result = run_deploy_train(
                repo_root=args.repo_root.resolve(),
                release_sha=release_sha,
                kubeconfig=kubeconfig,
                argocd=argocd,
                kubectl=kubectl,
                uv=uv,
                git=git,
                crane=crane,
                namespace=args.namespace,
                refresh_timeout=args.refresh_timeout,
                operation_timeout=args.operation_timeout,
                adoption_timeout=args.adoption_timeout,
                verify_timeout=args.verify_timeout,
                interval=args.poll_interval,
            )
        return 0 if result in {"succeeded", "no-op"} else 1
    except argo.ArgoCoreError as error:
        print(f"mandate_deploy_train: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
