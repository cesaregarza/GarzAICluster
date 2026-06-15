from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.grant_ownership import (
    CONFIGMAP_PATH,
    OWNERSHIP_MAP_PATH,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_WORKLOADS_VALUES_PATH = Path("apps/agent-workloads/values.yaml")
AGENT_WORKLOADS_RUNTIME_SECRET_PATH = Path(
    "secrets/agent-workloads/runtime-secret.enc.yaml"
)

SCHEMA_VERSION = "mandate-workload-enablement.v1"
KIND = "MandateWorkloadEnablement"

YAML_SAFE = YAML(typ="safe")
YAML_RT = YAML()
YAML_RT.preserve_quotes = True
YAML_RT.width = 4096

ALLOWED_TOP_LEVEL_KEYS = {
    "schema_version",
    "kind",
    "workload",
    "capability",
    "grant",
    "model_lease",
    "worker",
    "secrets",
    "network",
}
ALLOWED_GRANT_KEYS = {"binding"}
ALLOWED_MODEL_LEASE_KEYS = {"allowed_profile"}
ALLOWED_WORKER_KEYS = {"claims"}
ALLOWED_SECRET_KEYS = {"key"}
ALLOWED_NETWORK_KEYS = {"to"}

WORKER_CAPABILITY_ENV_PATHS = {
    "data.workspace_probe": ("env", "AGENT_WORKLOADS_WORKER_CAPABILITIES"),
    "opencode.proposer": (
        "opencodeProposer",
        "env",
        "AGENT_WORKLOADS_WORKER_CAPABILITIES",
    ),
    "opencode.apply_executor": (
        "opencodeApplyExecutor",
        "env",
        "AGENT_WORKLOADS_WORKER_CAPABILITIES",
    ),
}


class WorkloadEnablementError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnablementAction:
    code: str
    message: str
    path: str


@dataclass(frozen=True)
class WorkloadEnablementResult:
    workload: str
    capability: str
    actions: tuple[EnablementAction, ...]
    gaps: tuple[EnablementAction, ...]
    changed_files: tuple[str, ...]

    @property
    def has_gaps(self) -> bool:
        return bool(self.gaps)

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_files)


def plan_workload_enablement(
    *,
    repo_root: Path = REPO_ROOT,
    document_path: Path,
) -> WorkloadEnablementResult:
    return apply_workload_enablement(
        repo_root=repo_root,
        document_path=document_path,
        write=False,
    )


def apply_workload_enablement(
    *,
    repo_root: Path = REPO_ROOT,
    document_path: Path,
    write: bool,
    pr_body_path: Path | None = None,
) -> WorkloadEnablementResult:
    document = _load_document(document_path)
    capability_id = _required_str(document.get("capability"), "capability")
    workload_id = _required_str(document.get("workload"), "workload")

    configmap_path = repo_root / CONFIGMAP_PATH
    configmap = _load_yaml_rt(configmap_path)
    data = _required_mapping(configmap.get("data"), "registry overlay ConfigMap data")
    workload_imports = _load_yaml_text_rt(
        _required_str(data.get("workload_imports.yaml"), "data.workload_imports.yaml")
    )
    policy = _load_yaml_text_rt(
        _required_str(data.get("policy.prod.yaml"), "data.policy.prod.yaml")
    )
    values_path = repo_root / AGENT_WORKLOADS_VALUES_PATH
    values = _load_yaml_rt(values_path)
    ownership = _load_yaml(repo_root / OWNERSHIP_MAP_PATH)

    import_entry, capability = _find_imported_capability(
        workload_imports=workload_imports,
        workload_id=workload_id,
        capability_id=capability_id,
    )
    _require_deployment_owned(ownership, capability_id, "model_lease")
    _require_deployment_owned(ownership, capability_id, "session_authority_budget")

    actions: list[EnablementAction] = []
    gaps: list[EnablementAction] = []
    changed_files: set[str] = set()

    if _plan_policy_grant(
        document=document,
        capability_id=capability_id,
        policy=policy,
        actions=actions,
    ):
        changed_files.add(str(CONFIGMAP_PATH))

    if _plan_model_lease(
        document=document,
        capability_id=capability_id,
        capability=capability,
        actions=actions,
    ):
        changed_files.add(str(CONFIGMAP_PATH))

    if _plan_worker_claims(
        document=document,
        workload_id=workload_id,
        capability_id=capability_id,
        values=values,
        actions=actions,
    ):
        changed_files.add(str(AGENT_WORKLOADS_VALUES_PATH))

    _plan_secret_references(
        repo_root=repo_root,
        document=document,
        values=values,
        actions=actions,
        gaps=gaps,
    )
    _plan_network_requests(document=document, gaps=gaps)

    if write and changed_files:
        data["workload_imports.yaml"] = _dump_yaml(workload_imports)
        data["policy.prod.yaml"] = _dump_yaml(policy)
        _write_yaml(configmap_path, configmap)
        _write_yaml(values_path, values)

    result = WorkloadEnablementResult(
        workload=_required_str(import_entry.get("id"), "workload import id"),
        capability=capability_id,
        actions=tuple(actions),
        gaps=tuple(gaps),
        changed_files=tuple(sorted(changed_files)),
    )
    if pr_body_path is not None:
        pr_body_path.parent.mkdir(parents=True, exist_ok=True)
        pr_body_path.write_text(render_workload_enablement_pr_body(result), encoding="utf-8")
    return result


def render_workload_enablement_pr_body(result: WorkloadEnablementResult) -> str:
    lines = [
        "## Summary",
        f"- reconcile `{result.capability}` for workload `{result.workload}`.",
        "- Apply deployment-owned enablement edits only.",
        "",
        "## Automatic edits",
    ]
    if result.actions:
        lines.extend(f"- `{action.code}`: {action.message}" for action in result.actions)
    else:
        lines.append("- none")
    lines.extend(["", "## Operator gaps"])
    if result.gaps:
        lines.extend(f"- `{gap.code}`: {gap.message}" for gap in result.gaps)
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Authority Invariants",
            "- No live ConfigMap, Secret, or Kubernetes object is mutated.",
            "- Secret values are never read or written; only key names are inspected.",
            "- Deployment-owned overlay edits require the normal PR and control-plane restart.",
            "- Workload-release-owned fields remain refused by the ownership map.",
            "- Import and enablement documents are not dispatch authority by themselves.",
            "",
            "Refs CES-123.",
            "",
        ]
    )
    return "\n".join(lines)


def _load_document(path: Path) -> dict[str, Any]:
    document = _load_yaml(path)
    _reject_unknown_keys(document, ALLOWED_TOP_LEVEL_KEYS, "enablement document")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise WorkloadEnablementError(
            f"schema_version must be {SCHEMA_VERSION!r}"
        )
    if document.get("kind") != KIND:
        raise WorkloadEnablementError(f"kind must be {KIND!r}")
    _validate_grant(document.get("grant"))
    _validate_model_lease(document.get("model_lease"))
    _validate_worker(document.get("worker"))
    _validate_secrets(document.get("secrets"))
    _validate_network(document.get("network"))
    return document


def _validate_grant(value: Any) -> None:
    if value is None:
        return
    grant = _required_mapping(value, "grant")
    _reject_unknown_keys(grant, ALLOWED_GRANT_KEYS, "grant")
    _required_str(grant.get("binding"), "grant.binding")


def _validate_model_lease(value: Any) -> None:
    if value is None:
        return
    lease = _required_mapping(value, "model_lease")
    _reject_unknown_keys(lease, ALLOWED_MODEL_LEASE_KEYS, "model_lease")
    profile = lease.get("allowed_profile")
    if not isinstance(profile, str) or not profile:
        raise WorkloadEnablementError(
            "model_lease must declare exactly one non-empty allowed_profile"
        )


def _validate_worker(value: Any) -> None:
    if value is None:
        return
    worker = _required_mapping(value, "worker")
    _reject_unknown_keys(worker, ALLOWED_WORKER_KEYS, "worker")
    if worker.get("claims") is not True:
        raise WorkloadEnablementError("worker.claims must be true when declared")


def _validate_secrets(value: Any) -> None:
    if value is None:
        return
    secrets = _required_list(value, "secrets")
    for index, secret in enumerate(secrets):
        item = _required_mapping(secret, f"secrets[{index}]")
        _reject_unknown_keys(item, ALLOWED_SECRET_KEYS, f"secrets[{index}]")
        _required_str(item.get("key"), f"secrets[{index}].key")


def _validate_network(value: Any) -> None:
    if value is None:
        return
    requests = _required_list(value, "network")
    for index, request in enumerate(requests):
        item = _required_mapping(request, f"network[{index}]")
        _reject_unknown_keys(item, ALLOWED_NETWORK_KEYS, f"network[{index}]")
        _required_str(item.get("to"), f"network[{index}].to")


def _plan_policy_grant(
    *,
    document: dict[str, Any],
    capability_id: str,
    policy: dict[str, Any],
    actions: list[EnablementAction],
) -> bool:
    grant = document.get("grant")
    if grant is None:
        return False
    binding_id = _required_str(grant.get("binding"), "grant.binding")
    binding = _find_policy_binding(policy, binding_id)
    capabilities = _required_mapping(binding.get("capabilities"), "binding.capabilities")
    allowed = _required_list(capabilities.get("allow"), "binding.capabilities.allow")
    if capability_id in allowed:
        actions.append(
            EnablementAction(
                code="policy_grant_present",
                message=f"`{binding_id}` already grants `{capability_id}`.",
                path="data.policy.prod.yaml",
            )
        )
        return False
    allowed.append(capability_id)
    actions.append(
        EnablementAction(
            code="policy_grant_added",
            message=f"Add `{capability_id}` to policy binding `{binding_id}`.",
            path="data.policy.prod.yaml",
        )
    )
    return True


def _plan_model_lease(
    *,
    document: dict[str, Any],
    capability_id: str,
    capability: dict[str, Any],
    actions: list[EnablementAction],
) -> bool:
    requested = document.get("model_lease")
    if requested is None:
        return False
    profile = _required_str(requested.get("allowed_profile"), "model_lease.allowed_profile")
    lease = _required_mapping(
        capability.get("model_lease"),
        f"{capability_id}.model_lease",
    )
    current_profiles = lease.get("allowed_profiles") or []
    if current_profiles == [profile]:
        actions.append(
            EnablementAction(
                code="model_profile_present",
                message=f"`{capability_id}` already allows exactly `{profile}`.",
                path="data.workload_imports.yaml",
            )
        )
        return False
    lease["allowed_profiles"] = [profile]
    actions.append(
        EnablementAction(
            code="model_profile_set",
            message=f"Set `{capability_id}` allowed model profile to `{profile}`.",
            path="data.workload_imports.yaml",
        )
    )
    return True


def _plan_worker_claims(
    *,
    document: dict[str, Any],
    workload_id: str,
    capability_id: str,
    values: dict[str, Any],
    actions: list[EnablementAction],
) -> bool:
    worker = document.get("worker")
    if worker is None or worker.get("claims") is not True:
        return False
    env_path = WORKER_CAPABILITY_ENV_PATHS.get(workload_id)
    if env_path is None:
        raise WorkloadEnablementError(
            f"worker claim-list path is unknown for workload {workload_id!r}"
        )
    current = _get_nested(values, list(env_path))
    if not isinstance(current, str):
        raise WorkloadEnablementError(
            f"worker capability env is missing or not scalar for workload {workload_id!r}"
        )
    capabilities = [item.strip() for item in current.split(",") if item.strip()]
    if capability_id in capabilities:
        actions.append(
            EnablementAction(
                code="worker_claim_present",
                message=f"`{workload_id}` already claims `{capability_id}`.",
                path=str(AGENT_WORKLOADS_VALUES_PATH),
            )
        )
        return False
    capabilities.append(capability_id)
    _set_nested(values, list(env_path), ",".join(sorted(capabilities)))
    actions.append(
        EnablementAction(
            code="worker_claim_added",
            message=f"Add `{capability_id}` to `{workload_id}` worker claim list.",
            path=str(AGENT_WORKLOADS_VALUES_PATH),
        )
    )
    return True


def _plan_secret_references(
    *,
    repo_root: Path,
    document: dict[str, Any],
    values: dict[str, Any],
    actions: list[EnablementAction],
    gaps: list[EnablementAction],
) -> None:
    secret_items = document.get("secrets") or []
    if not secret_items:
        return
    known_key_refs = set(str(key) for key in values.get("secretKeys") or [])
    runtime_secret_path = repo_root / AGENT_WORKLOADS_RUNTIME_SECRET_PATH
    runtime_secret_text = runtime_secret_path.read_text(encoding="utf-8")
    for item in secret_items:
        key = _required_str(item.get("key"), "secret.key")
        if key in known_key_refs:
            actions.append(
                EnablementAction(
                    code="secret_key_ref_present",
                    message=f"`{key}` is referenced by agent-workloads values.",
                    path=str(AGENT_WORKLOADS_VALUES_PATH),
                )
            )
        else:
            gaps.append(
                EnablementAction(
                    code="secret_key_ref_missing",
                    message=(
                        f"`{key}` is not in agent-workloads secretKeys; add the key "
                        "reference without adding any secret value."
                    ),
                    path=str(AGENT_WORKLOADS_VALUES_PATH),
                )
            )
        if f"{key}:" in runtime_secret_text:
            actions.append(
                EnablementAction(
                    code="sops_secret_key_present",
                    message=f"`{key}` exists in the encrypted runtime Secret.",
                    path=str(AGENT_WORKLOADS_RUNTIME_SECRET_PATH),
                )
            )
        else:
            gaps.append(
                EnablementAction(
                    code="sops_secret_key_missing",
                    message=(
                        f"`{key}` is absent from the encrypted runtime Secret; "
                        "operator must add it through SOPS. The apply tool does not "
                        "read or write secret values."
                    ),
                    path=str(AGENT_WORKLOADS_RUNTIME_SECRET_PATH),
                )
            )


def _plan_network_requests(
    *,
    document: dict[str, Any],
    gaps: list[EnablementAction],
) -> None:
    for request in document.get("network") or []:
        target = _required_str(request.get("to"), "network.to")
        gaps.append(
            EnablementAction(
                code="network_mapping_review_required",
                message=(
                    f"`{target}` requires operator review against the Helm "
                    "NetworkPolicy CIDR/selector model before it can be edited."
                ),
                path=str(AGENT_WORKLOADS_VALUES_PATH),
            )
        )


def _find_imported_capability(
    *,
    workload_imports: dict[str, Any],
    workload_id: str,
    capability_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    imports = _required_list(workload_imports.get("imports"), "workload imports")
    for raw_entry in imports:
        entry = _required_mapping(raw_entry, "workload import")
        if entry.get("id") != workload_id:
            continue
        capabilities = _required_mapping(entry.get("capabilities"), "capabilities")
        capability = capabilities.get(capability_id)
        if isinstance(capability, dict):
            return entry, capability
        raise WorkloadEnablementError(
            f"workload {workload_id!r} does not declare capability {capability_id!r}"
        )
    raise WorkloadEnablementError(f"workload import not found: {workload_id}")


def _find_policy_binding(policy: dict[str, Any], binding_id: str) -> dict[str, Any]:
    for binding in _required_list(policy.get("bindings"), "policy.bindings"):
        item = _required_mapping(binding, "policy binding")
        if item.get("id") == binding_id:
            return item
    raise WorkloadEnablementError(f"policy binding not found: {binding_id}")


def _require_deployment_owned(
    ownership: dict[str, Any],
    capability_id: str,
    root_key: str,
) -> None:
    capabilities = _required_mapping(
        ownership.get("capabilities"),
        "ownership.capabilities",
    )
    capability = _required_mapping(
        capabilities.get(capability_id),
        f"ownership.capabilities.{capability_id}",
    )
    keys = _required_mapping(capability.get("keys"), f"{capability_id}.keys")
    info = keys.get(root_key)
    if info is None:
        return
    info_map = _required_mapping(info, f"{capability_id}.keys.{root_key}")
    owner = info_map.get("owner")
    if owner not in {"deployment_overlay", "mixed"}:
        raise WorkloadEnablementError(
            f"{capability_id}.{root_key} is {owner}-owned; edit the workload release "
            "instead because that moves digests and requires re-minting"
        )


def _reject_unknown_keys(
    payload: dict[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        rendered = ", ".join(unknown)
        raise WorkloadEnablementError(f"{label} contains unsupported keys: {rendered}")


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_SAFE.load(path.read_text(encoding="utf-8"))
    return _required_mapping(loaded, str(path))


def _load_yaml_rt(path: Path) -> dict[str, Any]:
    loaded = YAML_RT.load(path.read_text(encoding="utf-8"))
    return _required_mapping(loaded, str(path))


def _load_yaml_text_rt(raw: str) -> dict[str, Any]:
    loaded = YAML_RT.load(raw)
    return _required_mapping(loaded, "YAML text")


def _dump_yaml(payload: Any) -> str:
    stream = StringIO()
    YAML_RT.dump(payload, stream)
    return stream.getvalue()


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(_dump_yaml(payload), encoding="utf-8")


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
        child = current.get(part)
        if not isinstance(child, dict):
            raise WorkloadEnablementError(f"cannot descend into scalar key {part!r}")
        current = child
    current[parts[-1]] = value


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkloadEnablementError(f"YAML mapping expected: {label}")
    return value


def _required_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise WorkloadEnablementError(f"YAML list expected: {label}")
    return value


def _required_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise WorkloadEnablementError(f"non-empty string expected: {label}")
    return value
