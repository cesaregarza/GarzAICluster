from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGMAP_PATH = Path("apps/agent-control-plane-registry-overlay/configmap.yaml")
OWNERSHIP_SOURCE_PATH = Path("docs/grant-ownership-source.yaml")
OWNERSHIP_MAP_PATH = Path("docs/grant-ownership.yaml")
OWNERSHIP_DOC_PATH = Path("docs/grant-ownership.md")
APPLIER_PATH = Path("scripts/apply_splattop_release_artifacts.py")

YAML_SAFE = YAML(typ="safe")
YAML_RT = YAML()
YAML_RT.preserve_quotes = True
YAML_RT.width = 4096

DEPLOYMENT_OWNER = "deployment_overlay"
RELEASE_OWNER = "workload_release"
MIXED_OWNER = "mixed"
CONTROL_PLANE_RESTART = "control_plane_restart"
DIGEST_MOVES = "digest_moves_repin_remint"


class GrantOwnershipError(RuntimeError):
    pass


class GrantEditError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApplierContract:
    deployment_owned_capability_keys: tuple[str, ...]
    preserve_existing_capability_keys: tuple[str, ...]
    session_taint_key: str
    session_authority_budget_preserved_keys: tuple[str, ...]


@dataclass(frozen=True)
class GrantEditResult:
    capability_id: str
    key_path: str
    action: str
    old_value: Any
    new_value: Any
    deploy_consequence: str
    config_path: str
    owner: str


def find_agent_workloads_repo(
    *,
    repo_root: Path = REPO_ROOT,
    explicit: Path | None = None,
) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    env_path = os.environ.get("AGENT_WORKLOADS_REPO")
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            repo_root / ".ci" / "agent-workloads",
            repo_root.parent / "agent-workloads",
        ]
    )

    for candidate in candidates:
        applier = candidate / APPLIER_PATH
        if applier.exists():
            return candidate

    searched = ", ".join(str(path) for path in candidates)
    raise GrantOwnershipError(
        "agent-workloads checkout not found; set AGENT_WORKLOADS_REPO or checkout "
        f"cesaregarza/agent-workloads to .ci/agent-workloads. searched: {searched}"
    )


def load_applier_contract(
    *,
    repo_root: Path,
    agent_workloads_repo: Path | None,
) -> ApplierContract:
    if agent_workloads_repo is not None or os.environ.get("AGENT_WORKLOADS_REPO"):
        return extract_applier_contract(
            find_agent_workloads_repo(
                repo_root=repo_root,
                explicit=agent_workloads_repo,
            )
        )

    source_path = repo_root / OWNERSHIP_SOURCE_PATH
    if source_path.exists():
        source = _load_yaml(source_path)
        preserved_keys = tuple(
            sorted(
                str(key)
                for key in source.get("session_authority_budget_preserved_keys")
                or [_required_str(source.get("session_taint_key"), "session_taint_key")]
            )
        )
        session_taint_key = _required_str(
            source.get("session_taint_key") or "session_taint",
            "session_taint_key",
        )
        if session_taint_key not in preserved_keys:
            preserved_keys = tuple(sorted((*preserved_keys, session_taint_key)))

        return ApplierContract(
            deployment_owned_capability_keys=tuple(
                sorted(
                    _required_list(
                        source.get("deployment_owned_capability_keys"),
                        "deployment_owned_capability_keys",
                    )
                )
            ),
            preserve_existing_capability_keys=tuple(
                sorted(
                    _required_list(
                        source.get("preserve_existing_capability_keys"),
                        "preserve_existing_capability_keys",
                    )
                )
            ),
            session_taint_key=session_taint_key,
            session_authority_budget_preserved_keys=preserved_keys,
        )

    return extract_applier_contract(find_agent_workloads_repo(repo_root=repo_root))


def extract_applier_contract(agent_workloads_repo: Path) -> ApplierContract:
    source_path = agent_workloads_repo / APPLIER_PATH
    module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    wanted = {
        "DEPLOYMENT_OWNED_CAPABILITY_KEYS",
        "PRESERVE_EXISTING_CAPABILITY_KEYS",
        "SESSION_AUTHORITY_BUDGET_PRESERVED_KEYS",
        "SESSION_TAINT_KEY",
    }
    values: dict[str, Any] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                values[target.id] = ast.literal_eval(node.value)

    try:
        deployment_owned = tuple(sorted(values["DEPLOYMENT_OWNED_CAPABILITY_KEYS"]))
        preserve_existing = tuple(sorted(values["PRESERVE_EXISTING_CAPABILITY_KEYS"]))
        session_taint_key = str(values["SESSION_TAINT_KEY"])
        preserved_keys = tuple(
            sorted(
                str(key)
                for key in values.get(
                    "SESSION_AUTHORITY_BUDGET_PRESERVED_KEYS",
                    {session_taint_key},
                )
            )
        )
    except KeyError as exc:
        raise GrantOwnershipError(
            f"{source_path} is missing expected applier contract constant {exc.args[0]}"
        ) from exc

    return ApplierContract(
        deployment_owned_capability_keys=deployment_owned,
        preserve_existing_capability_keys=preserve_existing,
        session_taint_key=session_taint_key,
        session_authority_budget_preserved_keys=preserved_keys,
    )


def build_ownership_map(
    *,
    repo_root: Path = REPO_ROOT,
    agent_workloads_repo: Path | None = None,
) -> dict[str, Any]:
    contract = load_applier_contract(
        repo_root=repo_root,
        agent_workloads_repo=agent_workloads_repo,
    )
    configmap = _load_yaml(repo_root / CONFIGMAP_PATH)
    data = _required_mapping(configmap.get("data"), "registry overlay ConfigMap data")
    workload_imports = _load_yaml_text(
        _required_str(data.get("workload_imports.yaml"), "data.workload_imports.yaml")
    )
    imports = _required_list(workload_imports.get("imports"), "workload imports")

    capabilities: dict[str, Any] = {}
    for entry in imports:
        import_entry = _required_mapping(entry, "workload import")
        agent_id = _required_str(import_entry.get("id"), "workload import id")
        expected_source = _required_mapping(
            import_entry.get("expected_source"),
            f"{agent_id}.expected_source",
        )
        import_capabilities = _required_mapping(
            import_entry.get("capabilities"),
            f"{agent_id}.capabilities",
        )
        for capability_id in sorted(import_capabilities):
            capability = _required_mapping(
                import_capabilities[capability_id],
                f"{agent_id}.capabilities.{capability_id}",
            )
            keys = {
                key: _ownership_for_key(key, contract)
                for key in sorted(capability)
            }
            capabilities[capability_id] = {
                "agent_id": agent_id,
                "source": {
                    "repo": expected_source.get("repo"),
                    "repo_url": expected_source.get("repo_url"),
                    "path": expected_source.get("path"),
                },
                "config_path": (
                    "apps/agent-control-plane-registry-overlay/configmap.yaml:"
                    f"data.workload_imports.yaml.imports[id={agent_id}]"
                    f".capabilities.{capability_id}"
                ),
                "keys": keys,
            }

    return {
        "schema_version": "grant-ownership.v1",
        "generated_from": {
            "repo": "cesaregarza/agent-workloads",
            "path": str(APPLIER_PATH),
            "deployment_owned_capability_keys": list(
                contract.deployment_owned_capability_keys
            ),
            "preserve_existing_capability_keys": list(
                contract.preserve_existing_capability_keys
            ),
            "session_authority_budget_preserved_keys": list(
                contract.session_authority_budget_preserved_keys
            ),
        },
        "consumers": [
            "scripts/set_grant.py",
            "mandate doctor deployment-layout checks",
        ],
        "policy_overlay": {
            "owner": DEPLOYMENT_OWNER,
            "file": str(CONFIGMAP_PATH),
            "embedded_file": "policy.prod.yaml",
            "deploy_consequence": CONTROL_PLANE_RESTART,
            "remint_required": False,
            "digest_moves": False,
            "editable_paths": [
                "defaults.aggregate_budget",
                "bindings[].capabilities.allow",
                "bindings[].capabilities.approval_overrides",
            ],
        },
        "capabilities": capabilities,
    }


def render_ownership_markdown(ownership: dict[str, Any]) -> str:
    generated_from = ownership["generated_from"]
    lines = [
        "# Grant Ownership Map",
        "",
        "This file is generated from the agent-workloads release applier contract and",
        "the deployment registry overlay. Do not hand-edit it; run",
        "`scripts/generate_grant_ownership.py` instead.",
        "",
        "Changing a deployment-owned key edits the GarzAICluster registry overlay.",
        "The CES-108 PostSync hook is intended to roll the control-plane Deployments",
        "after registry-overlay sync. Until CES-108 live verification closes, confirm",
        "the rollout after sync and run a manual restart if it did not fire. It does",
        "not move workload image, manifest, or code digests, and it does not require",
        "workload identity token re-minting.",
        "",
        "Changing a workload-release-owned key belongs in `agent-workloads`",
        "`agents/<id>/agent.yaml`; that moves the workload code digest and requires the",
        "normal publish, re-pin, and re-mint flow.",
        "",
        "## Source Contract",
        "",
        f"- Source: `{generated_from['repo']}/{generated_from['path']}`",
        "- Deployment-owned capability roots: "
        + ", ".join(f"`{key}`" for key in generated_from["deployment_owned_capability_keys"]),
        "- Preserved existing capability roots: "
        + ", ".join(f"`{key}`" for key in generated_from["preserve_existing_capability_keys"]),
        "- Session authority preserved subkeys: "
        + ", ".join(
            f"`session_authority_budget.{key}`"
            for key in generated_from["session_authority_budget_preserved_keys"]
        ),
        "",
        "## Consumers",
        "",
    ]
    lines.extend(f"- `{consumer}`" for consumer in ownership["consumers"])
    lines.extend(
        [
            "",
            "## Policy Overlay",
            "",
            "The `policy.prod.yaml` embedded in the registry overlay is deployment-owned.",
            "Policy grants, approval overrides, and aggregate budget caps follow the",
            "same registry-overlay rollout-verification path and do not require",
            "re-minting.",
            "",
            "## Capability Keys",
            "",
            "| capability | key | owner | consequence |",
            "| --- | --- | --- | --- |",
        ]
    )

    for capability_id, capability in sorted(ownership["capabilities"].items()):
        for key, info in sorted(capability["keys"].items()):
            lines.append(
                "| `{capability}` | `{key}` | `{owner}` | `{consequence}` |".format(
                    capability=capability_id,
                    key=key,
                    owner=info["owner"],
                    consequence=info["deploy_consequence"],
                )
            )

    lines.extend(["", ""])
    return "\n".join(lines)


def write_ownership_outputs(
    *,
    repo_root: Path = REPO_ROOT,
    agent_workloads_repo: Path | None = None,
) -> None:
    contract = load_applier_contract(
        repo_root=repo_root,
        agent_workloads_repo=agent_workloads_repo,
    )
    _write_yaml(
        repo_root / OWNERSHIP_SOURCE_PATH,
        {
            "schema_version": "grant-ownership-source.v1",
            "generated_from": {
                "repo": "cesaregarza/agent-workloads",
                "path": str(APPLIER_PATH),
            },
            "deployment_owned_capability_keys": list(
                contract.deployment_owned_capability_keys
            ),
            "preserve_existing_capability_keys": list(
                contract.preserve_existing_capability_keys
            ),
            "session_taint_key": contract.session_taint_key,
            "session_authority_budget_preserved_keys": list(
                contract.session_authority_budget_preserved_keys
            ),
        },
    )
    ownership = build_ownership_map(
        repo_root=repo_root,
        agent_workloads_repo=None,
    )
    _write_yaml(repo_root / OWNERSHIP_MAP_PATH, ownership)
    (repo_root / OWNERSHIP_DOC_PATH).write_text(
        render_ownership_markdown(ownership),
        encoding="utf-8",
    )


def check_ownership_outputs(
    *,
    repo_root: Path = REPO_ROOT,
    agent_workloads_repo: Path | None = None,
) -> None:
    ownership = build_ownership_map(
        repo_root=repo_root,
        agent_workloads_repo=agent_workloads_repo,
    )
    expected_yaml = _dump_yaml(ownership)
    actual_yaml = (repo_root / OWNERSHIP_MAP_PATH).read_text(encoding="utf-8")
    if actual_yaml != expected_yaml:
        raise GrantOwnershipError(
            f"{OWNERSHIP_MAP_PATH} is stale; run scripts/generate_grant_ownership.py"
        )

    expected_markdown = render_ownership_markdown(ownership)
    actual_markdown = (repo_root / OWNERSHIP_DOC_PATH).read_text(encoding="utf-8")
    if actual_markdown != expected_markdown:
        raise GrantOwnershipError(
            f"{OWNERSHIP_DOC_PATH} is stale; run scripts/generate_grant_ownership.py"
        )


def apply_grant_edit(
    *,
    repo_root: Path,
    capability_id: str,
    key_path: str,
    raw_value: str,
    pr_body_path: Path | None = None,
) -> GrantEditResult:
    ownership = _load_yaml(repo_root / OWNERSHIP_MAP_PATH)
    capability = _required_mapping(
        _required_mapping(ownership.get("capabilities"), "capabilities").get(
            capability_id
        ),
        f"capability {capability_id}",
    )
    key_parts = _parse_key_path(key_path)
    key_info = _resolve_key_ownership(capability, key_parts)
    if not _is_deployment_editable(key_info, key_parts):
        source = _required_mapping(capability.get("source"), f"{capability_id}.source")
        source_path = _required_str(source.get("path"), f"{capability_id}.source.path")
        owner = key_info["owner"]
        raise GrantEditError(
            f"{capability_id} {key_path} is {owner}-owned. Edit "
            f"agent-workloads/{source_path}; consequence: workload digest moves, "
            "GarzAICluster must re-pin the release, and workload identity tokens must "
            "be re-minted."
        )

    configmap_path = repo_root / CONFIGMAP_PATH
    configmap = _load_yaml_rt(configmap_path)
    data = _required_mapping(configmap.get("data"), "registry overlay ConfigMap data")
    workload_imports = _load_yaml_text_rt(
        _required_str(data.get("workload_imports.yaml"), "data.workload_imports.yaml")
    )
    import_entry, capability_overlay = _find_capability_overlay(
        workload_imports,
        capability_id,
    )
    old_value = _get_nested(capability_overlay, key_parts)
    action = "unset" if raw_value == "unset" else "set"
    new_value: Any = None if action == "unset" else _parse_value(raw_value)
    if action == "unset":
        _delete_nested(capability_overlay, key_parts)
    else:
        _set_nested(capability_overlay, key_parts, new_value)
    data["workload_imports.yaml"] = _dump_yaml(workload_imports)
    _write_yaml(configmap_path, configmap)

    result = GrantEditResult(
        capability_id=capability_id,
        key_path=key_path,
        action=action,
        old_value=old_value,
        new_value=new_value,
        deploy_consequence=key_info["deploy_consequence"],
        config_path=(
            "apps/agent-control-plane-registry-overlay/configmap.yaml:"
            f"data.workload_imports.yaml.imports[id={import_entry['id']}]"
            f".capabilities.{capability_id}.{key_path}"
        ),
        owner=key_info["owner"],
    )
    if pr_body_path is not None:
        pr_body_path.parent.mkdir(parents=True, exist_ok=True)
        pr_body_path.write_text(render_grant_edit_pr_body(result), encoding="utf-8")
    return result


def render_grant_edit_pr_body(result: GrantEditResult) -> str:
    if result.deploy_consequence == CONTROL_PLANE_RESTART:
        consequence = "overlay-only: verify CP rollout after sync, no re-mint"
    else:
        consequence = result.deploy_consequence
    lines = [
        "## Summary",
        f"- {result.action} `{result.capability_id}` `{result.key_path}`.",
        "- Update the deployment-owned registry overlay only.",
        "",
        "## Ownership",
        f"- Owner: `{result.owner}`",
        f"- Config path: `{result.config_path}`",
        f"- Deploy consequence: `{consequence}`",
        "- Source: `docs/grant-ownership.yaml` generated from the agent-workloads "
        "release applier contract.",
        "",
        "## Authority Invariants",
        "- No workload manifest, image, or code digest moves.",
        "- No workload identity token re-mint is required.",
        "- No live ConfigMap mutation or runtime bypass is introduced.",
        "- The registry overlay remains the source of deployment authority. The "
        "CES-108 hook is intended to roll the control plane after sync; until live "
        "verification closes, confirm the rollout or restart manually so the frozen "
        "snapshot reloads.",
        "",
        "Refs CES-117.",
        "",
    ]
    return "\n".join(lines)


def create_grant_edit_pr(
    *,
    repo_root: Path,
    result: GrantEditResult,
    pr_body_path: Path,
    branch: str | None = None,
) -> None:
    branch = branch or _sanitize_branch(
        f"grant-edit/{result.capability_id}/{result.key_path}"
    )
    current_branch = _git(repo_root, "branch", "--show-current").strip()
    if current_branch in {"main", "master"}:
        _git(repo_root, "checkout", "-b", branch)
    elif current_branch != branch:
        raise GrantEditError(
            f"current branch is {current_branch!r}; pass --branch {current_branch} "
            "or run from main so the tool can create the grant-edit branch"
        )
    _git(repo_root, "add", str(CONFIGMAP_PATH))
    _git(
        repo_root,
        "commit",
        "-S",
        "-m",
        f"CES-117: edit {result.capability_id} {result.key_path}",
    )
    _git(repo_root, "push", "-u", "origin", branch)
    title = f"CES-117: edit {result.capability_id} {result.key_path}"
    subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--label",
            "grant-edit",
            "--title",
            title,
            "--body-file",
            str(pr_body_path),
        ],
        cwd=repo_root,
        check=True,
    )


def _ownership_for_key(key: str, contract: ApplierContract) -> dict[str, Any]:
    if key in contract.deployment_owned_capability_keys:
        return {
            "owner": DEPLOYMENT_OWNER,
            "deploy_consequence": CONTROL_PLANE_RESTART,
            "remint_required": False,
            "digest_moves": False,
            "source": "DEPLOYMENT_OWNED_CAPABILITY_KEYS",
        }
    if key in contract.preserve_existing_capability_keys:
        return {
            "owner": DEPLOYMENT_OWNER,
            "deploy_consequence": CONTROL_PLANE_RESTART,
            "remint_required": False,
            "digest_moves": False,
            "source": "PRESERVE_EXISTING_CAPABILITY_KEYS",
        }
    if key == "session_authority_budget":
        return {
            "owner": MIXED_OWNER,
            "deploy_consequence": CONTROL_PLANE_RESTART,
            "remint_required": False,
            "digest_moves": False,
            "deployment_owned_subkeys": list(
                contract.session_authority_budget_preserved_keys
            ),
            "release_owned_subkeys": "*",
            "source": "SESSION_AUTHORITY_BUDGET_PRESERVED_KEYS",
        }
    return {
        "owner": RELEASE_OWNER,
        "deploy_consequence": DIGEST_MOVES,
        "remint_required": True,
        "digest_moves": True,
        "source": "workload agent.yaml",
    }


def _resolve_key_ownership(
    capability: dict[str, Any],
    key_parts: list[str],
) -> dict[str, Any]:
    keys = _required_mapping(capability.get("keys"), "capability keys")
    root_info = keys.get(key_parts[0])
    if not isinstance(root_info, dict):
        known = ", ".join(sorted(str(key) for key in keys))
        raise GrantEditError(f"unknown capability key {key_parts[0]!r}; known keys: {known}")
    if root_info.get("owner") == MIXED_OWNER and len(key_parts) > 1:
        deployment_subkeys = set(root_info.get("deployment_owned_subkeys") or [])
        if key_parts[1] in deployment_subkeys:
            return {
                **root_info,
                "owner": DEPLOYMENT_OWNER,
            }
        return {
            **root_info,
            "owner": RELEASE_OWNER,
            "deploy_consequence": DIGEST_MOVES,
            "remint_required": True,
            "digest_moves": True,
        }
    return root_info


def _is_deployment_editable(info: dict[str, Any], key_parts: list[str]) -> bool:
    owner = info.get("owner")
    if owner == DEPLOYMENT_OWNER:
        return True
    if owner == MIXED_OWNER and len(key_parts) == 2:
        return key_parts[1] in set(info.get("deployment_owned_subkeys") or [])
    return False


def _find_capability_overlay(
    workload_imports: dict[str, Any],
    capability_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    imports = _required_list(workload_imports.get("imports"), "workload imports")
    for raw_entry in imports:
        entry = _required_mapping(raw_entry, "workload import")
        capabilities = _required_mapping(entry.get("capabilities"), "capabilities")
        capability = capabilities.get(capability_id)
        if isinstance(capability, dict):
            return entry, capability
    raise GrantEditError(f"capability not found in workload imports: {capability_id}")


def _parse_key_path(raw: str) -> list[str]:
    parts = [part for part in raw.split(".") if part]
    if not parts:
        raise GrantEditError("key path must not be empty")
    return parts


def _parse_value(raw_value: str) -> Any:
    parsed = YAML_SAFE.load(raw_value)
    if isinstance(parsed, (dict, list)):
        raise GrantEditError("set_grant accepts scalar values only")
    return parsed


def _get_nested(root: dict[str, Any], parts: list[str]) -> Any:
    current: Any = root
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_nested(root: dict[str, Any], parts: list[str], value: Any) -> None:
    current = root
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise GrantEditError(f"cannot descend into scalar key {part!r}")
        current = child
    current[parts[-1]] = value


def _delete_nested(root: dict[str, Any], parts: list[str]) -> None:
    current = root
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            return
        current = child
    current.pop(parts[-1], None)


def _sanitize_branch(raw: str) -> str:
    branch = re.sub(r"[^A-Za-z0-9._/-]+", "-", raw).strip("-/")
    return branch.replace("..", ".")


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_SAFE.load(path.read_text(encoding="utf-8"))
    return _required_mapping(loaded, str(path))


def _load_yaml_rt(path: Path) -> dict[str, Any]:
    loaded = YAML_RT.load(path.read_text(encoding="utf-8"))
    return _required_mapping(loaded, str(path))


def _load_yaml_text(raw: str) -> dict[str, Any]:
    loaded = YAML_SAFE.load(raw)
    return _required_mapping(loaded, "YAML text")


def _load_yaml_text_rt(raw: str) -> dict[str, Any]:
    loaded = YAML_RT.load(raw)
    return _required_mapping(loaded, "YAML text")


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_yaml(payload), encoding="utf-8")


def _dump_yaml(payload: Any) -> str:
    stream = StringIO()
    YAML_RT.dump(payload, stream)
    return stream.getvalue()


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GrantOwnershipError(f"YAML mapping expected: {label}")
    return value


def _required_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise GrantOwnershipError(f"YAML list expected: {label}")
    return value


def _required_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GrantOwnershipError(f"non-empty string expected: {label}")
    return value
