from __future__ import annotations

import tempfile
import unittest
import shutil
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.check_control_plane_release_pin import (
    ControlPlanePinError,
    check_control_plane_release_pin,
    update_control_plane_release_pin,
)


YAML_PARSER = YAML(typ="safe")
REPO_ROOT = Path(__file__).resolve().parents[1]


class ControlPlaneReleasePinTests(unittest.TestCase):
    def test_update_all_sites_and_is_idempotent(self) -> None:
        root = _pin_fixture()
        kwargs = dict(repo_root=root, source_sha="d3d4d2f955805fd66da131f29cd3bec108a27f75", image_digest="sha256:" + "a" * 64)
        self.assertIn("files=5", update_control_plane_release_pin(**kwargs, apply=True))
        snapshot = {p: (root / p).read_text() for p in _PIN_FILES}
        self.assertIn("already current", update_control_plane_release_pin(**kwargs, apply=True))
        self.assertEqual(snapshot, {p: (root / p).read_text() for p in _PIN_FILES})

    def test_update_rejects_malformed_inputs_without_changes(self) -> None:
        root = _pin_fixture()
        before = {p: (root / p).read_text() for p in _PIN_FILES}
        with self.assertRaisesRegex(ControlPlanePinError, "full lowercase"):
            update_control_plane_release_pin(repo_root=root, source_sha="bad", image_digest="sha256:" + "a" * 64, apply=True)
        self.assertEqual(before, {p: (root / p).read_text() for p in _PIN_FILES})

    def test_update_rejects_ambiguous_site_without_changes(self) -> None:
        root = _pin_fixture()
        path = root / "apps/agent-control-plane-runtime-controls/postgres-sweep-cronjob.yaml"
        path.write_text(path.read_text() + "\n" + path.read_text())
        before = {p: (root / p).read_text() for p in _PIN_FILES}
        with self.assertRaisesRegex(ControlPlanePinError, "postgres sweep"):
            update_control_plane_release_pin(repo_root=root, source_sha="d3d4d2f955805fd66da131f29cd3bec108a27f75", image_digest="sha256:" + "a" * 64, apply=True)
        self.assertEqual(before, {p: (root / p).read_text() for p in _PIN_FILES})
    def test_current_control_plane_release_pin_matches(self) -> None:
        result = check_control_plane_release_pin(
            repo_root=REPO_ROOT,
            application_path=Path("argocd/applications/agent-control-plane.yaml"),
            values_path=Path("apps/agent-control-plane/values.yaml"),
        )

        self.assertIn("targetRevision and image tag match", result)

    def test_control_plane_ingress_allows_both_split_opencode_workers(self) -> None:
        values = YAML_PARSER.load(
            (REPO_ROOT / "apps" / "agent-control-plane" / "values.yaml").read_text()
        )
        sources = values["networkPolicy"]["ingress"]["sources"]
        workload_sources = {
            source["podSelector"]["matchLabels"].get("app.kubernetes.io/name")
            for source in sources
            if source.get("namespaceSelector", {})
            .get("matchLabels", {})
            .get("kubernetes.io/metadata.name")
            == "agent-workloads"
        }

        self.assertIn("opencode-proposer", workload_sources)
        self.assertIn("opencode-apply-executor", workload_sources)

    def test_control_plane_release_pin_check_runs_in_python_contracts_ci(self) -> None:
        workflow = YAML_PARSER.load(
            (REPO_ROOT / ".github" / "workflows" / "ci.yaml").read_text()
        )
        steps = workflow["jobs"]["python-contracts"]["steps"]

        self.assertTrue(
            any(
                step.get("run")
                == "uv run python scripts/check_control_plane_release_pin.py"
                for step in steps
            )
        )

    def test_control_plane_release_pin_rejects_tag_mismatch(self) -> None:
        root = _fixture_repo(
            target_revision="abcdef1234567890abcdef1234567890abcdef12",
            image_tag="sha-deadbeef0000",
        )

        with self.assertRaisesRegex(
            ControlPlanePinError,
            "image.tag must match agent-platform targetRevision",
        ):
            check_control_plane_release_pin(
                repo_root=root,
                application_path=Path("argocd/applications/agent-control-plane.yaml"),
                values_path=Path("apps/agent-control-plane/values.yaml"),
            )


def _fixture_repo(*, target_revision: str, image_tag: str) -> Path:
    root = Path(tempfile.mkdtemp())
    application_path = root / "argocd" / "applications" / "agent-control-plane.yaml"
    values_path = root / "apps" / "agent-control-plane" / "values.yaml"
    application_path.parent.mkdir(parents=True)
    values_path.parent.mkdir(parents=True)

    _write_yaml(
        application_path,
        {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Application",
            "spec": {
                "sources": [
                    {
                        "repoURL": "git@github.com:cesaregarza/agent-platform.git",
                        "targetRevision": target_revision,
                        "path": "helm/mandate",
                    },
                    {
                        "repoURL": "https://github.com/cesaregarza/GarzAICluster",
                        "targetRevision": "main",
                        "ref": "values",
                    },
                ]
            },
        },
    )
    _write_yaml(values_path, {"image": {"tag": image_tag}})
    return root


_PIN_FILES = (
    "apps/agent-control-plane/values.yaml",
    "argocd/applications/agent-control-plane.yaml",
    "apps/agent-control-plane-runtime-controls/postgres-sweep-cronjob.yaml",
    "docs/runbooks/postgres-restore.md",
    "tests/test_mandate_deploy_train.py",
)


def _pin_fixture() -> Path:
    root = Path(tempfile.mkdtemp())
    for relative in _PIN_FILES:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, destination)
        text = destination.read_text()
        text = text.replace("d3d4d2f955805fd66da131f29cd3bec108a27f75", "fa3afd59e3afe9e55c79387521bd6099da89f97e")
        text = text.replace("sha-d3d4d2f95580", "sha-fa3afd59e3af")
        text = text.replace("sha256:a62a0b6d3608d810dfb1bf0fe82b0a4bf35aaa668b7f097ae05f3e9106441008", "sha256:" + "6" * 64)
        text = "\n".join(line for line in text.splitlines() if not line.startswith("Current GitOps Core release pin:")) + "\n"
        destination.write_text(text)
    return root


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    from io import StringIO

    stream = StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.dump(payload, stream)
    path.write_text(stream.getvalue(), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
