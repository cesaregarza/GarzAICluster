from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.grant_ownership import (
    CONFIGMAP_PATH,
    OWNERSHIP_DOC_PATH,
    OWNERSHIP_MAP_PATH,
    OWNERSHIP_SOURCE_PATH,
    GrantEditError,
    apply_grant_edit,
    build_ownership_map,
    check_ownership_outputs,
    render_ownership_markdown,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


class GrantOwnershipTests(unittest.TestCase):
    def test_generated_ownership_map_is_current(self) -> None:
        check_ownership_outputs(repo_root=REPO_ROOT)

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
        for path in [CONFIGMAP_PATH, OWNERSHIP_SOURCE_PATH]:
            source = REPO_ROOT / path
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

        source_path = root / OWNERSHIP_SOURCE_PATH
        source_contract = YAML_PARSER.load(source_path.read_text())
        source_contract["deployment_owned_capability_keys"].remove("model_lease")
        _write_yaml(source_path, source_contract)

        current = build_ownership_map(repo_root=REPO_ROOT)
        changed = build_ownership_map(repo_root=root)

        self.assertNotEqual(changed, current)
        self.assertIn(
            "model_lease",
            render_ownership_markdown(changed),
        )

    def test_set_grant_unsets_deployment_owned_model_lease_key(self) -> None:
        root = _fixture_repo()
        _inject_model_completion_tokens(root, 4000)
        pr_body = root / "grant-edit-pr.md"

        result = apply_grant_edit(
            repo_root=root,
            capability_id="agent_workloads.opencode_propose",
            key_path="model_lease.max_completion_tokens",
            raw_value="unset",
            pr_body_path=pr_body,
        )

        capability = _capability(root, "agent_workloads.opencode_propose")
        self.assertNotIn("max_completion_tokens", capability["model_lease"])
        self.assertEqual(result.action, "unset")
        self.assertEqual(result.old_value, 4000)
        body = pr_body.read_text()
        self.assertIn("overlay-only: CP restart required, no re-mint", body)
        self.assertIn("No workload manifest, image, or code digest moves.", body)

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
    for path in [
        CONFIGMAP_PATH,
        OWNERSHIP_SOURCE_PATH,
        OWNERSHIP_MAP_PATH,
        OWNERSHIP_DOC_PATH,
    ]:
        source = REPO_ROOT / path
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return root


def _inject_model_completion_tokens(root: Path, value: int) -> None:
    configmap = YAML_PARSER.load((root / CONFIGMAP_PATH).read_text())
    workload_imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
    capability = _find_capability(workload_imports, "agent_workloads.opencode_propose")
    capability["model_lease"]["max_completion_tokens"] = value
    configmap["data"]["workload_imports.yaml"] = _yaml_text(workload_imports)
    _write_yaml(root / CONFIGMAP_PATH, configmap)


def _capability(root: Path, capability_id: str) -> dict[str, Any]:
    configmap = YAML_PARSER.load((root / CONFIGMAP_PATH).read_text())
    workload_imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
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
