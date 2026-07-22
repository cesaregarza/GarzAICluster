from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.grant_ownership import (
    APPLIER_PATH,
    OWNERSHIP_DOC_PATH,
    OWNERSHIP_MAP_PATH,
    OWNERSHIP_SOURCE_PATH,
    REGISTRY_OVERLAY_DIR,
    GrantEditError,
    GrantOwnershipError,
    apply_grant_edit,
    build_ownership_map,
    check_ownership_outputs,
    load_registry_overlay_data,
    render_ownership_markdown,
    write_ownership_outputs,
    write_registry_overlay_values,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


class GrantOwnershipTests(unittest.TestCase):
    def test_generated_ownership_map_is_current(self) -> None:
        check_ownership_outputs(repo_root=REPO_ROOT)

    def test_generated_ownership_map_points_to_split_registry_sources(self) -> None:
        ownership = YAML_PARSER.load((REPO_ROOT / OWNERSHIP_MAP_PATH).read_text())

        self.assertEqual(
            ownership["policy_overlay"]["file"],
            "apps/agent-control-plane-registry-overlay/registry/policy.prod.yaml",
        )
        workload_imports_prefix = (
            "apps/agent-control-plane-registry-overlay/registry/"
            "workload_imports.yaml:"
        )
        for capability in ownership["capabilities"].values():
            self.assertTrue(
                capability["config_path"].startswith(workload_imports_prefix),
                capability["config_path"],
            )
            self.assertNotIn("configmap.yaml", capability["config_path"])

    def test_ci_checks_generated_grant_ownership_without_private_repo_checkout(
        self,
    ) -> None:
        workflow = YAML_PARSER.load((REPO_ROOT / ".github/workflows/ci.yaml").read_text())
        steps = workflow["jobs"]["python-contracts"]["steps"]
        self.assertFalse(
            any(
                step.get("with", {}).get("repository") == "cesaregarza/agent-workloads"
                for step in steps
            )
        )

        check_step = next(
            step
            for step in steps
            if step.get("run")
            == "uv run python scripts/generate_grant_ownership.py --check"
        )
        self.assertEqual(check_step["name"], "Check grant ownership map")

    def test_map_changes_when_applier_contract_snapshot_changes(self) -> None:
        root = Path(tempfile.mkdtemp())
        _copy_registry_overlay(root)
        for path in [OWNERSHIP_SOURCE_PATH]:
            source = REPO_ROOT / path
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

        source_path = root / OWNERSHIP_SOURCE_PATH
        source_contract = YAML_PARSER.load(source_path.read_text())
        source_contract["deployment_owned_capability_keys"].remove("model_bounds")
        _write_yaml(source_path, source_contract)

        current = build_ownership_map(repo_root=REPO_ROOT)
        changed = build_ownership_map(repo_root=root)

        self.assertNotEqual(changed, current)
        self.assertIn(
            "model_bounds",
            render_ownership_markdown(changed),
        )

    def test_explicit_agent_workloads_checkout_never_falls_back(self) -> None:
        base = Path(tempfile.mkdtemp())
        _fake_agent_workloads(base / "agent-workloads")
        explicit = base / "requested-agent-workloads"
        explicit.mkdir()

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "explicit agent-workloads checkout does not contain the expected applier",
        ):
            build_ownership_map(
                repo_root=base / "config",
                agent_workloads_repo=explicit,
            )

    def test_explicit_agent_workloads_checkout_rejects_constant_name_drift(
        self,
    ) -> None:
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
            deployment_constant_name="DEPLOYMENT_OWNED_CAPABILITY_ROOTS",
        )

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "missing expected applier contract constant.*"
            "DEPLOYMENT_OWNED_CAPABILITY_KEYS",
        ):
            build_ownership_map(
                repo_root=REPO_ROOT,
                agent_workloads_repo=agent_workloads,
            )

    def test_explicit_agent_workloads_checkout_rejects_string_key_collection(
        self,
    ) -> None:
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
            deployment_owned_capability_keys="model_bounds",
        )

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "deployment-owned capability keys must be a non-empty collection",
        ):
            build_ownership_map(
                repo_root=REPO_ROOT,
                agent_workloads_repo=agent_workloads,
            )

    def test_explicit_agent_workloads_checkout_rejects_duplicate_set_literal(
        self,
    ) -> None:
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
            deployment_owned_capability_keys_literal="{'model_bounds', 'model_bounds'}",
        )

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "DEPLOYMENT_OWNED_CAPABILITY_KEYS must not contain duplicate literal values",
        ):
            build_ownership_map(
                repo_root=REPO_ROOT,
                agent_workloads_repo=agent_workloads,
            )

    def test_explicit_agent_workloads_checkout_rejects_malformed_literal(
        self,
    ) -> None:
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
            deployment_owned_capability_keys_literal="{'model_bounds', dynamic_key}",
        )

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "DEPLOYMENT_OWNED_CAPABILITY_KEYS must be a literal value",
        ):
            build_ownership_map(
                repo_root=REPO_ROOT,
                agent_workloads_repo=agent_workloads,
            )

    def test_explicit_agent_workloads_checkout_rejects_non_string_key(self) -> None:
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
            deployment_owned_capability_keys=(1,),
        )

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "deployment-owned capability keys must be a non-empty collection",
        ):
            build_ownership_map(
                repo_root=REPO_ROOT,
                agent_workloads_repo=agent_workloads,
            )

    def test_explicit_agent_workloads_checkout_requires_preserved_influence_key(
        self,
    ) -> None:
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
            session_authority_budget_preserved_keys=("max_operations",),
        )

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "session-authority preserved keys must include influence key 'influence'",
        ):
            build_ownership_map(
                repo_root=REPO_ROOT,
                agent_workloads_repo=agent_workloads,
            )

    def test_explicit_agent_workloads_checkout_rejects_non_string_influence_key(
        self,
    ) -> None:
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
            influence_key=1,
        )

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "non-empty string expected: .* influence key",
        ):
            build_ownership_map(
                repo_root=REPO_ROOT,
                agent_workloads_repo=agent_workloads,
            )

    def test_fallback_ownership_source_rejects_non_string_key(self) -> None:
        root = _fixture_repo()
        source_path = root / OWNERSHIP_SOURCE_PATH
        source = YAML_PARSER.load(source_path.read_text())
        source["deployment_owned_capability_keys"] = [1]
        _write_yaml(source_path, source)

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "deployment-owned capability keys must be a non-empty collection",
        ):
            build_ownership_map(repo_root=root)

    def test_fallback_ownership_source_rejects_string_preserved_keys(self) -> None:
        root = _fixture_repo()
        source_path = root / OWNERSHIP_SOURCE_PATH
        source = YAML_PARSER.load(source_path.read_text())
        source["session_authority_budget_preserved_keys"] = "influence"
        _write_yaml(source_path, source)

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "session-authority preserved keys must be a non-empty collection",
        ):
            build_ownership_map(repo_root=root)

    def test_fallback_ownership_source_requires_preserved_influence_key(self) -> None:
        root = _fixture_repo()
        source_path = root / OWNERSHIP_SOURCE_PATH
        source = YAML_PARSER.load(source_path.read_text())
        source["session_authority_budget_preserved_keys"] = ["max_operations"]
        _write_yaml(source_path, source)

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "session-authority preserved keys must include influence key 'influence'",
        ):
            build_ownership_map(repo_root=root)

    def test_explicit_agent_workloads_checkout_regenerates_all_outputs(self) -> None:
        root = _fixture_repo()
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
            deployment_owned_capability_keys=(
                "artifacts",
                "custom_deployment_key",
                "disclosure",
                "model_bounds",
            ),
        )

        write_ownership_outputs(
            repo_root=root,
            agent_workloads_repo=agent_workloads,
        )

        source = YAML_PARSER.load((root / OWNERSHIP_SOURCE_PATH).read_text())
        ownership = YAML_PARSER.load((root / OWNERSHIP_MAP_PATH).read_text())
        markdown = (root / OWNERSHIP_DOC_PATH).read_text()
        self.assertIn(
            "custom_deployment_key",
            source["deployment_owned_capability_keys"],
        )
        self.assertIn(
            "custom_deployment_key",
            ownership["generated_from"]["deployment_owned_capability_keys"],
        )
        self.assertIn("`custom_deployment_key`", markdown)
        check_ownership_outputs(
            repo_root=root,
            agent_workloads_repo=agent_workloads,
        )

    def test_spend_limit_mapping_is_mixed_ownership(self) -> None:
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
        )

        ownership = build_ownership_map(
            repo_root=REPO_ROOT,
            agent_workloads_repo=agent_workloads,
        )

        broker_bounds = ownership["capabilities"][
            "agent_workloads.readonly_query"
        ]["keys"]["broker_bounds"]
        self.assertEqual(broker_bounds["owner"], "mixed")
        self.assertEqual(
            broker_bounds["deployment_owned_subkeys"],
            ["max_cost_usd"],
        )
        self.assertEqual(
            broker_bounds["source"],
            "SPEND_LIMIT_PRESERVING_MAPPING_KEYS/SPEND_LIMIT_KEYS",
        )

    def test_generation_validation_failure_preserves_all_outputs(self) -> None:
        root = _fixture_repo()
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
            deployment_owned_capability_keys=(
                "artifacts",
                "custom_deployment_key",
                "disclosure",
                "model_bounds",
            ),
        )
        output_paths = [
            root / OWNERSHIP_SOURCE_PATH,
            root / OWNERSHIP_MAP_PATH,
            root / OWNERSHIP_DOC_PATH,
        ]
        before = {path: path.read_bytes() for path in output_paths}
        (root / REGISTRY_OVERLAY_DIR / "registry/workload_imports.yaml").unlink()

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "registry overlay ConfigMap source file not found",
        ):
            write_ownership_outputs(
                repo_root=root,
                agent_workloads_repo=agent_workloads,
            )

        self.assertEqual(
            {path: path.read_bytes() for path in output_paths},
            before,
        )

    def test_explicit_check_rejects_stale_ownership_source(self) -> None:
        root = _fixture_repo()
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
        )
        write_ownership_outputs(
            repo_root=root,
            agent_workloads_repo=agent_workloads,
        )
        source_path = root / OWNERSHIP_SOURCE_PATH
        source = YAML_PARSER.load(source_path.read_text())
        source["deployment_owned_capability_keys"].remove("model_bounds")
        _write_yaml(source_path, source)

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "docs/grant-ownership-source.yaml is stale",
        ):
            check_ownership_outputs(
                repo_root=root,
                agent_workloads_repo=agent_workloads,
            )

    def test_explicit_check_rejects_stale_ownership_map(self) -> None:
        root = _fixture_repo()
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
        )
        write_ownership_outputs(
            repo_root=root,
            agent_workloads_repo=agent_workloads,
        )
        (root / OWNERSHIP_MAP_PATH).write_text("stale: true\n", encoding="utf-8")

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "docs/grant-ownership.yaml is stale",
        ):
            check_ownership_outputs(
                repo_root=root,
                agent_workloads_repo=agent_workloads,
            )

    def test_explicit_check_rejects_stale_ownership_markdown(self) -> None:
        root = _fixture_repo()
        agent_workloads = _fake_agent_workloads(
            Path(tempfile.mkdtemp()) / "agent-workloads",
        )
        write_ownership_outputs(
            repo_root=root,
            agent_workloads_repo=agent_workloads,
        )
        (root / OWNERSHIP_DOC_PATH).write_text("stale\n", encoding="utf-8")

        with self.assertRaisesRegex(
            GrantOwnershipError,
            "docs/grant-ownership.md is stale",
        ):
            check_ownership_outputs(
                repo_root=root,
                agent_workloads_repo=agent_workloads,
            )

    def test_set_grant_unsets_deployment_owned_model_bounds_key(self) -> None:
        root = _fixture_repo()
        _inject_model_completion_tokens(root, 4000)
        pr_body = root / "grant-edit-pr.md"

        result = apply_grant_edit(
            repo_root=root,
            capability_id="agent_workloads.opencode_propose",
            key_path="model_bounds.max_completion_tokens",
            raw_value="unset",
            pr_body_path=pr_body,
        )

        capability = _capability(root, "agent_workloads.opencode_propose")
        self.assertNotIn("max_completion_tokens", capability["model_bounds"])
        self.assertEqual(result.action, "unset")
        self.assertEqual(result.old_value, 4000)
        self.assertTrue(
            result.config_path.startswith(
                "apps/agent-control-plane-registry-overlay/registry/"
                "workload_imports.yaml:"
            ),
            result.config_path,
        )
        body = pr_body.read_text()
        self.assertIn("overlay-only: CP restart required, no re-mint", body)
        self.assertIn("No workload manifest, image, or code digest moves.", body)

    def test_set_grant_allows_session_authority_max_operations_overlay_edit(
        self,
    ) -> None:
        root = _fixture_repo()
        pr_body = root / "grant-edit-pr.md"

        result = apply_grant_edit(
            repo_root=root,
            capability_id="agent_workloads.opencode_propose",
            key_path="session_authority_budget.max_operations",
            raw_value="16",
            pr_body_path=pr_body,
        )

        capability = _capability(root, "agent_workloads.opencode_propose")
        self.assertEqual(capability["session_authority_budget"]["max_operations"], 16)
        self.assertEqual(result.action, "set")
        self.assertEqual(result.new_value, 16)
        body = pr_body.read_text()
        self.assertIn("overlay-only: CP restart required, no re-mint", body)
        self.assertIn("No workload manifest, image, or code digest moves.", body)

    def test_set_grant_allows_preserved_broker_spend_limit_edit(self) -> None:
        root = _fixture_repo()

        result = apply_grant_edit(
            repo_root=root,
            capability_id="agent_workloads.readonly_query",
            key_path="broker_bounds.max_cost_usd",
            raw_value="7.5",
        )

        capability = _capability(root, "agent_workloads.readonly_query")
        self.assertEqual(capability["broker_bounds"]["max_cost_usd"], 7.5)
        self.assertEqual(result.owner, "deployment_overlay")
        self.assertFalse(
            YAML_PARSER.load((root / OWNERSHIP_MAP_PATH).read_text())["capabilities"][
                "agent_workloads.readonly_query"
            ]["keys"]["broker_bounds"]["remint_required"]
        )

    def test_set_grant_refuses_release_owned_output_schema(self) -> None:
        root = _fixture_repo()

        with self.assertRaisesRegex(
            GrantEditError,
            "agents/opencode-proposer/agent.yaml.*re-minted",
        ):
            apply_grant_edit(
                repo_root=root,
                capability_id="agent_workloads.opencode_propose",
                key_path="output_schema",
                raw_value="not_allowed",
            )


def _fixture_repo() -> Path:
    root = Path(tempfile.mkdtemp())
    _copy_registry_overlay(root)
    for path in [
        OWNERSHIP_SOURCE_PATH,
        OWNERSHIP_MAP_PATH,
        OWNERSHIP_DOC_PATH,
    ]:
        source = REPO_ROOT / path
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return root


def _fake_agent_workloads(
    root: Path,
    *,
    deployment_owned_capability_keys: Any = (
        "artifacts",
        "disclosure",
        "model_bounds",
    ),
    deployment_owned_capability_keys_literal: str | None = None,
    deployment_constant_name: str = "DEPLOYMENT_OWNED_CAPABILITY_KEYS",
    preserve_existing_capability_keys: Any = ("approval_mode",),
    spend_limit_keys: Any = ("max_cost_usd",),
    spend_limit_preserving_mapping_keys: Any = ("broker_bounds",),
    influence_key: Any = "influence",
    session_authority_budget_preserved_keys: Any = (
        "influence",
        "max_operations",
    ),
) -> Path:
    applier_path = root / APPLIER_PATH
    applier_path.parent.mkdir(parents=True)
    deployment_literal = (
        deployment_owned_capability_keys_literal
        if deployment_owned_capability_keys_literal is not None
        else repr(deployment_owned_capability_keys)
    )
    applier_path.write_text(
        "\n".join(
            [
                f"{deployment_constant_name} = {deployment_literal}",
                (
                    "PRESERVE_EXISTING_CAPABILITY_KEYS = "
                    f"{preserve_existing_capability_keys!r}"
                ),
                f"SPEND_LIMIT_KEYS = {spend_limit_keys!r}",
                "SPEND_LIMIT_PRESERVING_MAPPING_KEYS = "
                f"{spend_limit_preserving_mapping_keys!r}",
                f"INFLUENCE_KEY = {influence_key!r}",
                "SESSION_AUTHORITY_BUDGET_PRESERVED_KEYS = "
                f"{session_authority_budget_preserved_keys!r}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return root


def _copy_registry_overlay(root: Path) -> None:
    source = REPO_ROOT / REGISTRY_OVERLAY_DIR
    target = root / REGISTRY_OVERLAY_DIR
    shutil.copytree(source, target, dirs_exist_ok=True)


def _inject_model_completion_tokens(root: Path, value: int) -> None:
    data = load_registry_overlay_data(root)
    workload_imports = YAML_PARSER.load(data["workload_imports.yaml"])
    capability = _find_capability(workload_imports, "agent_workloads.opencode_propose")
    capability["model_bounds"]["max_completion_tokens"] = value
    write_registry_overlay_values(root, {"workload_imports.yaml": _yaml_text(workload_imports)})


def _capability(root: Path, capability_id: str) -> dict[str, Any]:
    data = load_registry_overlay_data(root)
    workload_imports = YAML_PARSER.load(data["workload_imports.yaml"])
    return _find_capability(workload_imports, capability_id)


def _find_capability(workload_imports: dict[str, Any], capability_id: str) -> dict[str, Any]:
    for entry in workload_imports["imports"]:
        capability = entry.get("capabilities", {}).get(capability_id)
        if isinstance(capability, dict):
            return capability
    raise AssertionError(f"capability not found: {capability_id}")


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    yaml = YAML()
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(payload, handle)


def _yaml_text(payload: dict[str, Any]) -> str:
    from io import StringIO

    yaml = YAML()
    stream = StringIO()
    yaml.dump(payload, stream)
    return stream.getvalue()
