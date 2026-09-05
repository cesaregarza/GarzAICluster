#!/usr/bin/env python3
"""Validate synthetic live-verify config against its deployed source contracts."""

from __future__ import annotations

import argparse
import ast
import json
import math
import subprocess
import sys
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

try:
    from scripts.agent_platform_layout import (
        AgentPlatformLayout,
        AgentPlatformLayoutError,
        resolve_agent_platform_layout,
    )
except ModuleNotFoundError:  # Direct ``python scripts/check_*.py`` execution.
    from agent_platform_layout import (  # type: ignore[no-redef]
        AgentPlatformLayout,
        AgentPlatformLayoutError,
        resolve_agent_platform_layout,
    )


REPO_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_PATH = Path("argocd/applications/agent-control-plane.yaml")
VALUES_PATH = Path("apps/agent-control-plane/values.yaml")
POLICY_OVERLAY_PATH = Path(
    "apps/agent-control-plane-registry-overlay/registry/policy.prod.yaml"
)
WORKLOAD_IMPORTS_PATH = Path(
    "apps/agent-control-plane-registry-overlay/registry/workload_imports.yaml"
)
PLATFORM_POLICY_BASE_PATH = Path("registries/policy.base.yaml")
PLATFORM_CAPABILITIES_PATH = Path("registries/capabilities.yaml")
PLATFORM_AGENTS_PATH = Path("registries/agents.yaml")
PLATFORM_OUTPUT_PROJECTION_PATH = Path("mandate/core/output_projection.py")
PLATFORM_EVENTS_PATH = Path("mandate/contracts/events.py")
PLATFORM_CALLBACK_EVENTS_PATH = Path("mandate/contracts/callback_event_types.py")
AGENT_PLATFORM_REPO_URLS = {
    "git@github.com:cesaregarza/agent-platform.git",
    "https://github.com/cesaregarza/agent-platform",
    "https://github.com/cesaregarza/agent-platform.git",
}

YAML_PARSER = YAML(typ="safe")


class ConfigCoherenceError(RuntimeError):
    """Raised when deployment config contradicts a pinned source contract."""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate synthetic live-verify journeys against the effective "
            "production policy, pinned agent-platform source, registry overlay, "
            "and deployed skill-bundle manifest."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--agent-platform-repo",
        type=Path,
        required=True,
        help="Checked-out agent-platform repository at the Argo targetRevision.",
    )
    parser.add_argument(
        "--skills-manifest",
        type=Path,
        required=True,
        help="Extracted /skill-bundle/manifest.json from the deployed bundle image.",
    )
    parser.add_argument(
        "--skills-manifest-source",
        default="/skill-bundle/manifest.json",
        help="Source label named in skill-reference failures.",
    )
    args = parser.parse_args()

    try:
        summary = check_agent_control_plane_config_coherence(
            repo_root=args.repo_root.resolve(),
            agent_platform_repo=args.agent_platform_repo.resolve(),
            skills_manifest_path=args.skills_manifest.resolve(),
            skills_manifest_source=args.skills_manifest_source,
        )
    except ConfigCoherenceError as exc:
        print(f"agent-control-plane config coherence gate failed: {exc}", file=sys.stderr)
        return 1
    print(summary)
    return 0


def check_agent_control_plane_config_coherence(
    *,
    repo_root: Path,
    agent_platform_repo: Path,
    skills_manifest_path: Path,
    skills_manifest_source: str = "/skill-bundle/manifest.json",
    values_path: Path = VALUES_PATH,
    policy_overlay_path: Path = POLICY_OVERLAY_PATH,
    workload_imports_path: Path = WORKLOAD_IMPORTS_PATH,
    validate_checkout: bool = True,
) -> str:
    try:
        source_layout = resolve_agent_platform_layout(
            agent_platform_repo,
            required_paths=(
                Path("core/output_projection.py"),
                Path("contracts/events.py"),
                Path("contracts/callback_event_types.py"),
            ),
        )
    except AgentPlatformLayoutError as exc:
        raise ConfigCoherenceError(str(exc)) from exc

    if validate_checkout:
        expected_revision = _agent_platform_target_revision(
            _load_yaml(repo_root / APPLICATION_PATH, APPLICATION_PATH.as_posix())
        )
        _validate_agent_platform_checkout(
            agent_platform_repo,
            expected_revision,
            source_layout=source_layout,
        )
    else:
        expected_revision = "fixture"

    deployment_values = _load_yaml(repo_root / values_path, values_path.as_posix())
    journeys = _journeys(deployment_values, values_path)
    base_policy = _load_yaml(
        agent_platform_repo / PLATFORM_POLICY_BASE_PATH,
        _platform_label(PLATFORM_POLICY_BASE_PATH),
    )
    overlay_policy = _load_yaml(
        repo_root / policy_overlay_path,
        policy_overlay_path.as_posix(),
    )
    effective_policy = _deep_merge(base_policy, overlay_policy)
    capabilities, capability_sources = _effective_capabilities(
        agent_platform_repo=agent_platform_repo,
        repo_root=repo_root,
        workload_imports_path=workload_imports_path,
    )
    agents = _agent_records(agent_platform_repo / PLATFORM_AGENTS_PATH)
    projection_source = _platform_label(
        PLATFORM_OUTPUT_PROJECTION_PATH,
        source_layout,
    )
    projection_fields = _projection_result_fields(
        _source_path(agent_platform_repo, PLATFORM_OUTPUT_PROJECTION_PATH, source_layout),
        projection_source,
    )
    skill_ids = _skill_ids(skills_manifest_path, skills_manifest_source)
    events_source = _platform_label(PLATFORM_EVENTS_PATH, source_layout)
    event_members = _event_type_members(
        _source_path(agent_platform_repo, PLATFORM_EVENTS_PATH, source_layout),
        events_source,
    )
    callback_types = _callback_event_types(
        _source_path(
            agent_platform_repo,
            PLATFORM_CALLBACK_EVENTS_PATH,
            source_layout,
        ),
        event_members,
        _platform_label(PLATFORM_CALLBACK_EVENTS_PATH, source_layout),
    )

    contradictions: list[str] = []
    for index, journey in enumerate(journeys):
        journey_path = f"{values_path.as_posix()} syntheticLiveVerify.journeys[{index}]"
        capability_id = _required_string_value(
            journey,
            "capability_id",
            journey_path,
            contradictions,
        )
        _check_budget(
            journey=journey,
            journey_path=journey_path,
            capability_id=capability_id,
            effective_policy=effective_policy,
            contradictions=contradictions,
            policy_overlay_path=policy_overlay_path,
        )

        if capability_id is not None and capability_id not in capabilities:
            contradictions.append(
                f"{journey_path}.capability_id={capability_id!r} is absent from the "
                "effective registry built from "
                f"{_platform_label(PLATFORM_CAPABILITIES_PATH)} and "
                f"{workload_imports_path.as_posix()}"
            )

        if capability_id is not None and capability_id in capabilities:
            result_fields, result_source = _result_contract(
                capability_id=capability_id,
                capability=capabilities[capability_id],
                capability_source=capability_sources[capability_id],
                agents=agents,
                projection_fields=projection_fields,
                agent_platform_repo=agent_platform_repo,
                source_layout=source_layout,
                projection_source=projection_source,
            )
            for field_index, field in _string_list(
                journey,
                "required_result_fields",
                journey_path,
                contradictions,
            ):
                if result_fields is None:
                    contradictions.append(
                        f"{journey_path}.required_result_fields[{field_index}]={field!r} "
                        f"cannot be validated: {result_source} exposes no static "
                        "result-field contract"
                    )
                elif field not in result_fields:
                    contradictions.append(
                        f"{journey_path}.required_result_fields[{field_index}]={field!r} "
                        f"is not emitted by {result_source}; declared fields are "
                        f"{sorted(result_fields)}"
                    )

        for skill_index, skill_id in _string_list(
            journey,
            "required_skill_ids",
            journey_path,
            contradictions,
        ):
            if skill_id not in skill_ids:
                contradictions.append(
                    f"{journey_path}.required_skill_ids[{skill_index}]={skill_id!r} "
                    f"is absent from {skills_manifest_source}"
                )

        for event_index, event_type in _string_list(
            journey,
            "required_event_types",
            journey_path,
            contradictions,
        ):
            if event_type not in event_members.values():
                contradictions.append(
                    f"{journey_path}.required_event_types[{event_index}]={event_type!r} "
                    f"is not a member of {events_source} EventType"
                )

        for callback_index, callback_type in _string_list(
            journey,
            "required_callback_types",
            journey_path,
            contradictions,
        ):
            if callback_type not in callback_types:
                contradictions.append(
                    f"{journey_path}.required_callback_types[{callback_index}]="
                    f"{callback_type!r} is not declared by "
                    f"{_platform_label(PLATFORM_CALLBACK_EVENTS_PATH, source_layout)}"
                )

    if contradictions:
        rendered = "\n".join(f"- {item}" for item in contradictions)
        raise ConfigCoherenceError(f"configuration contradictions:\n{rendered}")
    return (
        f"{values_path.as_posix()} syntheticLiveVerify has {len(journeys)} coherent "
        f"journey(s) against agent-platform {expected_revision}, "
        f"{policy_overlay_path.as_posix()}, and {skills_manifest_source}."
    )


def _journeys(values: Mapping[str, Any], values_path: Path) -> list[Mapping[str, Any]]:
    verify = values.get("syntheticLiveVerify")
    if not isinstance(verify, Mapping):
        raise ConfigCoherenceError(
            f"{values_path.as_posix()} syntheticLiveVerify must be a mapping"
        )
    journeys = verify.get("journeys")
    if not isinstance(journeys, list) or not journeys:
        raise ConfigCoherenceError(
            f"{values_path.as_posix()} syntheticLiveVerify.journeys must be a non-empty list"
        )
    if not all(isinstance(item, Mapping) for item in journeys):
        raise ConfigCoherenceError(
            f"{values_path.as_posix()} syntheticLiveVerify.journeys entries must be mappings"
        )
    return journeys


def _check_budget(
    *,
    journey: Mapping[str, Any],
    journey_path: str,
    capability_id: str | None,
    effective_policy: Mapping[str, Any],
    contradictions: list[str],
    policy_overlay_path: Path,
) -> None:
    defaults = effective_policy.get("defaults")
    if not isinstance(defaults, Mapping):
        contradictions.append(
            f"{journey_path} cannot resolve policy caps because "
            f"{_platform_label(PLATFORM_POLICY_BASE_PATH)} merged with "
            f"{policy_overlay_path.as_posix()} has no defaults mapping"
        )
        return
    cap_sources = (
        f"{_platform_label(PLATFORM_POLICY_BASE_PATH)} merged with "
        f"{policy_overlay_path.as_posix()}"
    )
    budget_specs = (
        (
            "max_runtime_seconds",
            "max_runtime_seconds_per_job",
            "max_runtime_seconds_per_capability",
        ),
        ("max_cost_usd", "max_cost_usd_per_job", "max_cost_usd_per_capability"),
    )
    for journey_key, default_key, overrides_key in budget_specs:
        requested = _number(journey.get(journey_key))
        if requested is None:
            contradictions.append(
                f"{journey_path}.{journey_key} must be a finite non-negative number; "
                f"the effective cap comes from {cap_sources}"
            )
            continue
        cap = defaults.get(default_key)
        overrides = defaults.get(overrides_key, {})
        if capability_id is not None and isinstance(overrides, Mapping):
            cap = overrides.get(capability_id, cap)
        numeric_cap = _number(cap)
        if numeric_cap is None:
            contradictions.append(
                f"{journey_path}.{journey_key}={requested:g} has no finite non-negative "
                f"{default_key} cap in {cap_sources}"
            )
            continue
        if requested > numeric_cap:
            contradictions.append(
                f"{journey_path}.{journey_key}={requested:g} exceeds the effective "
                f"{capability_id or 'journey'} cap {numeric_cap:g} from {cap_sources}"
            )


def _effective_capabilities(
    *,
    agent_platform_repo: Path,
    repo_root: Path,
    workload_imports_path: Path,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    capabilities_doc = _load_yaml(
        agent_platform_repo / PLATFORM_CAPABILITIES_PATH,
        _platform_label(PLATFORM_CAPABILITIES_PATH),
    )
    raw_capabilities = capabilities_doc.get("capabilities")
    if not isinstance(raw_capabilities, list):
        raise ConfigCoherenceError(
            f"{_platform_label(PLATFORM_CAPABILITIES_PATH)} capabilities must be a list"
        )
    capabilities: dict[str, Mapping[str, Any]] = {}
    sources: dict[str, str] = {}
    for item in raw_capabilities:
        if not isinstance(item, Mapping):
            continue
        capability_id = item.get("id")
        if isinstance(capability_id, str) and capability_id:
            capabilities[capability_id] = item
            sources[capability_id] = _platform_label(PLATFORM_CAPABILITIES_PATH)

    imports_doc = _load_yaml(
        repo_root / workload_imports_path,
        workload_imports_path.as_posix(),
    )
    imports = imports_doc.get("imports")
    if not isinstance(imports, list):
        raise ConfigCoherenceError(
            f"{workload_imports_path.as_posix()} imports must be a list"
        )
    for item in imports:
        if not isinstance(item, Mapping):
            continue
        imported_capabilities = item.get("capabilities")
        if not isinstance(imported_capabilities, Mapping):
            continue
        for capability_id, capability in imported_capabilities.items():
            if isinstance(capability_id, str) and isinstance(capability, Mapping):
                capabilities[capability_id] = capability
                sources[capability_id] = workload_imports_path.as_posix()
    return capabilities, sources


def _agent_records(path: Path) -> list[Mapping[str, Any]]:
    doc = _load_yaml(path, _platform_label(PLATFORM_AGENTS_PATH))
    agents = doc.get("agents")
    if not isinstance(agents, list):
        raise ConfigCoherenceError(
            f"{_platform_label(PLATFORM_AGENTS_PATH)} agents must be a list"
        )
    return [agent for agent in agents if isinstance(agent, Mapping)]


def _result_contract(
    *,
    capability_id: str,
    capability: Mapping[str, Any],
    capability_source: str,
    agents: list[Mapping[str, Any]],
    projection_fields: Mapping[str, frozenset[str]],
    agent_platform_repo: Path,
    source_layout: AgentPlatformLayout,
    projection_source: str,
) -> tuple[frozenset[str] | None, str]:
    if capability_source == _platform_label(PLATFORM_CAPABILITIES_PATH):
        for agent in agents:
            agent_capabilities = agent.get("capabilities")
            if not isinstance(agent_capabilities, list) or capability_id not in agent_capabilities:
                continue
            loop = agent.get("loop")
            module = loop.get("module") if isinstance(loop, Mapping) else None
            if not isinstance(module, str) or not module:
                continue
            try:
                source_path = source_layout.path_for_module(module)
            except AgentPlatformLayoutError as exc:
                raise ConfigCoherenceError(str(exc)) from exc
            source_label = _platform_label(
                Path("mandate") / Path(*module.split(".")[1:]).with_suffix(".py"),
                source_layout,
            )
            fields = _worker_result_fields(source_path, source_label)
            return fields, source_label

    output_gate = capability.get("output_gate")
    projection_id = (
        output_gate.get("projection_id") if isinstance(output_gate, Mapping) else None
    )
    if not isinstance(projection_id, str) or not projection_id:
        projection_id = capability_id
    return projection_fields.get(projection_id), projection_source


def _worker_result_fields(
    path: Path,
    source_label: str | None = None,
) -> frozenset[str] | None:
    tree = _parse_python(path, source_label)
    fields: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "WorkerResult":
            continue
        result_keyword = next(
            (keyword for keyword in node.keywords if keyword.arg == "result"),
            None,
        )
        if result_keyword is None or not isinstance(result_keyword.value, ast.Dict):
            continue
        for key in result_keyword.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                fields.add(key.value)
    return frozenset(fields) if fields else None


def _projection_result_fields(
    path: Path,
    source_label: str,
) -> dict[str, frozenset[str]]:
    values = _module_assignments(
        _parse_python(path, source_label),
        event_members={},
    )
    raw = values.get("PUBLIC_RESULT_FIELDS_BY_PROJECTION_ID")
    if not isinstance(raw, Mapping):
        raise ConfigCoherenceError(
            f"{source_label} does not declare "
            "PUBLIC_RESULT_FIELDS_BY_PROJECTION_ID"
        )
    result: dict[str, frozenset[str]] = {}
    for projection_id, fields in raw.items():
        if not isinstance(projection_id, str) or not isinstance(
            fields, (set, frozenset, tuple, list)
        ):
            continue
        if all(isinstance(field, str) for field in fields):
            result[projection_id] = frozenset(fields)
    return result


def _skill_ids(path: Path, source: str) -> frozenset[str]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigCoherenceError(f"could not read {source}: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise ConfigCoherenceError(f"{source} must contain a JSON object")
    skills = manifest.get("skills")
    if not isinstance(skills, list):
        raise ConfigCoherenceError(f"{source} skills must be a list")
    skill_ids: set[str] = set()
    for index, skill in enumerate(skills):
        skill_id = skill.get("id") if isinstance(skill, Mapping) else None
        if not isinstance(skill_id, str) or not skill_id:
            raise ConfigCoherenceError(f"{source} skills[{index}].id is invalid")
        skill_ids.add(skill_id)
    return frozenset(skill_ids)


def _event_type_members(path: Path, source_label: str) -> dict[str, str]:
    tree = _parse_python(path, source_label)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "EventType":
            continue
        members: dict[str, str] = {}
        for statement in node.body:
            name, expression = _assignment(statement)
            if name is None or not isinstance(expression, ast.Constant):
                continue
            if isinstance(expression.value, str):
                members[name] = expression.value
        if members:
            return members
    raise ConfigCoherenceError(
        f"{source_label} does not declare a string EventType enum"
    )


def _callback_event_types(
    path: Path,
    event_members: Mapping[str, str],
    source_label: str,
) -> frozenset[str]:
    values = _module_assignments(
        _parse_python(path, source_label),
        event_members=event_members,
    )
    callback_types = {
        value
        for name, value in values.items()
        if name.isupper() and isinstance(value, str)
    }
    if not callback_types:
        raise ConfigCoherenceError(
            f"{source_label} declares no callback types"
        )
    return frozenset(callback_types)


def _module_assignments(
    tree: ast.Module,
    *,
    event_members: Mapping[str, str],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for statement in tree.body:
        name, expression = _assignment(statement)
        if name is None or expression is None:
            continue
        try:
            values[name] = _literal_value(
                expression,
                values=values,
                event_members=event_members,
            )
        except ValueError:
            continue
    return values


def _assignment(statement: ast.stmt) -> tuple[str | None, ast.expr | None]:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
        if isinstance(target, ast.Name):
            return target.id, statement.value
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id, statement.value
    return None, None


def _literal_value(
    node: ast.expr,
    *,
    values: Mapping[str, Any],
    event_members: Mapping[str, str],
) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in values:
            return values[node.id]
        raise ValueError(node.id)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        members = [
            _literal_value(item, values=values, event_members=event_members)
            for item in node.elts
        ]
        if isinstance(node, ast.Tuple):
            return tuple(members)
        if isinstance(node, ast.Set):
            return set(members)
        return members
    if isinstance(node, ast.Dict):
        return {
            _literal_value(key, values=values, event_members=event_members): _literal_value(
                value,
                values=values,
                event_members=event_members,
            )
            for key, value in zip(node.keys, node.values, strict=True)
            if key is not None
        }
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
    ):
        return frozenset(
            _literal_value(node.args[0], values=values, event_members=event_members)
        )
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "value"
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "EventType"
    ):
        member = event_members.get(node.value.attr)
        if member is not None:
            return member
    raise ValueError(ast.dump(node))


def _string_list(
    journey: Mapping[str, Any],
    key: str,
    journey_path: str,
    contradictions: list[str],
) -> list[tuple[int, str]]:
    raw = journey.get(key, [])
    if not isinstance(raw, list):
        contradictions.append(f"{journey_path}.{key} must be a list of strings")
        return []
    result: list[tuple[int, str]] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value:
            contradictions.append(
                f"{journey_path}.{key}[{index}] must be a non-empty string"
            )
            continue
        result.append((index, value))
    return result


def _required_string_value(
    mapping: Mapping[str, Any],
    key: str,
    path: str,
    contradictions: list[str],
) -> str | None:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        contradictions.append(f"{path}.{key} must be a non-empty string")
        return None
    return value


def _agent_platform_target_revision(application: Mapping[str, Any]) -> str:
    spec = application.get("spec")
    sources = spec.get("sources") if isinstance(spec, Mapping) else None
    if not isinstance(sources, list):
        raise ConfigCoherenceError(
            f"{APPLICATION_PATH.as_posix()} spec.sources must be a list"
        )
    matches = [
        source
        for source in sources
        if isinstance(source, Mapping)
        and source.get("repoURL") in AGENT_PLATFORM_REPO_URLS
    ]
    if len(matches) != 1:
        raise ConfigCoherenceError(
            f"{APPLICATION_PATH.as_posix()} must contain exactly one agent-platform source"
        )
    revision = matches[0].get("targetRevision")
    if not isinstance(revision, str) or len(revision) != 40:
        raise ConfigCoherenceError(
            f"{APPLICATION_PATH.as_posix()} agent-platform targetRevision must be a full git SHA"
        )
    return revision


def _validate_agent_platform_checkout(
    path: Path,
    expected_revision: str,
    *,
    source_layout: AgentPlatformLayout | None = None,
) -> None:
    required = (
        PLATFORM_POLICY_BASE_PATH,
        PLATFORM_CAPABILITIES_PATH,
        PLATFORM_AGENTS_PATH,
    )
    if source_layout is None:
        try:
            source_layout = resolve_agent_platform_layout(
                path,
                required_paths=(
                    Path("core/output_projection.py"),
                    Path("contracts/events.py"),
                    Path("contracts/callback_event_types.py"),
                ),
            )
        except AgentPlatformLayoutError as exc:
            raise ConfigCoherenceError(str(exc)) from exc
    missing = next(
        (relative for relative in required if not (path / relative).is_file()),
        None,
    )
    if missing is not None:
        raise ConfigCoherenceError(
            f"agent-platform checkout is missing {_platform_label(missing)}: {path}"
        )
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConfigCoherenceError("git is required to verify agent-platform revision") from exc
    actual_revision = result.stdout.strip()
    if result.returncode != 0 or actual_revision != expected_revision:
        raise ConfigCoherenceError(
            "agent-platform checkout revision contradicts "
            f"{APPLICATION_PATH.as_posix()}: expected {expected_revision}, "
            f"got {actual_revision or 'unreadable'}"
        )


def _load_yaml(path: Path, source: str) -> dict[str, Any]:
    try:
        loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigCoherenceError(f"could not read {source}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigCoherenceError(f"{source} must contain a YAML mapping")
    return loaded


def _parse_python(path: Path, source_label: str | None = None) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        label = source_label or path.as_posix()
        raise ConfigCoherenceError(f"could not parse {label}: {exc}") from exc


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return numeric


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _source_path(
    repo_root: Path,
    legacy_path: Path,
    source_layout: AgentPlatformLayout,
) -> Path:
    if legacy_path.parts[:1] == ("mandate",):
        return source_layout.path_for(Path(*legacy_path.parts[1:]))
    return repo_root / legacy_path


def _platform_label(
    path: Path,
    source_layout: AgentPlatformLayout | None = None,
) -> str:
    if source_layout is not None and path.parts[:1] == ("mandate",):
        path = Path(source_layout.label_for(Path(*path.parts[1:])))
    return f"agent-platform/{path.as_posix()}"


if __name__ == "__main__":
    sys.exit(main())
