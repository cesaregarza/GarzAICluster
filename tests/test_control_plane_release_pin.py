from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.check_control_plane_release_pin import (
    ControlPlanePinError,
    check_control_plane_release_pin,
)


YAML_PARSER = YAML(typ="safe")
REPO_ROOT = Path(__file__).resolve().parents[1]


class ControlPlaneReleasePinTests(unittest.TestCase):
    def test_current_control_plane_release_pin_matches(self) -> None:
        result = check_control_plane_release_pin(
            repo_root=REPO_ROOT,
            application_path=Path("argocd/applications/agent-control-plane.yaml"),
            values_path=Path("apps/agent-control-plane/values.yaml"),
        )

        self.assertIn("targetRevision and image tag match", result)

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
                        "repoURL": "https://github.com/cesaregarza/SplatTopConfig",
                        "targetRevision": "main",
                        "ref": "values",
                    },
                ]
            },
        },
    )
    _write_yaml(values_path, {"image": {"tag": image_tag}})
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
