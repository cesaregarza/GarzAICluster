from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
PROD_VALUES = REPO_ROOT / "apps" / "agent-8s" / "values.yaml"
DEV_VALUES = REPO_ROOT / "apps" / "agent-8s" / "values.dev.yaml"
YAML_PARSER = YAML(typ="safe")


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = YAML_PARSER.load(path)
    if not isinstance(payload, dict):
        raise AssertionError(f"{path} must contain one YAML mapping")
    return payload


def _render(values_path: Path, namespace: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    result = subprocess.run(
        [
            "helm",
            "template",
            "agent-8s",
            "apps/agent-8s",
            "--namespace",
            namespace,
            "-f",
            str(values_path.relative_to(REPO_ROOT)),
        ],
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]


def _deployment(documents: list[dict[str, Any]]) -> dict[str, Any]:
    deployments = [
        document for document in documents if document.get("kind") == "Deployment"
    ]
    if len(deployments) != 1:
        raise AssertionError(
            f"expected exactly one Agent-8s Deployment, found {len(deployments)}"
        )
    return deployments[0]


def _bot_resources(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    bots = [container for container in containers if container["name"] == "bot"]
    if len(bots) != 1:
        raise AssertionError(f"expected exactly one bot container, found {len(bots)}")
    return bots[0]["resources"]


class Agent8sCpuRightsizingTests(unittest.TestCase):
    def test_production_render_has_the_reviewed_cpu_request(self) -> None:
        deployment = _deployment(
            _render(PROD_VALUES, namespace="splattop-bot-agent-8s")
        )

        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        self.assertEqual(
            _bot_resources(deployment),
            {
                "requests": {"cpu": "50m", "memory": "128Mi"},
                "limits": {"cpu": "300m", "memory": "256Mi"},
            },
        )

    def test_development_overlay_remains_disabled_and_unchanged(self) -> None:
        deployment = _deployment(
            _render(DEV_VALUES, namespace="splattop-bot-agent-8s-dev")
        )

        self.assertEqual(deployment["spec"]["replicas"], 0)
        self.assertEqual(
            _bot_resources(deployment)["requests"],
            {"cpu": "100m", "memory": "128Mi"},
        )

    def test_production_change_reclaims_exactly_50m(self) -> None:
        values = _load_yaml(PROD_VALUES)
        self.assertEqual(values["replicaCount"], 1)
        self.assertIs(values["hpa"]["enabled"], False)
        self.assertEqual(values["resources"]["requests"]["cpu"], "50m")
        self.assertEqual(100 - int(values["resources"]["requests"]["cpu"][:-1]), 50)

    def test_application_boundary_is_automated(self) -> None:
        bot = _load_yaml(REPO_ROOT / "apps" / "bots" / "agent-8s.yaml")
        self.assertEqual(bot["valuesFile"], "values.yaml")
        self.assertEqual(bot["chartPath"], "apps/agent-8s")

        appset = _load_yaml(REPO_ROOT / "argocd" / "appsets" / "bots-apps.yaml")
        child_sync = appset["spec"]["template"]["spec"]["syncPolicy"]
        self.assertEqual(child_sync["automated"], {"prune": True, "selfHeal": True})

        parent = _load_yaml(
            REPO_ROOT / "argocd" / "applications" / "splattop-bots.yaml"
        )
        self.assertEqual(
            parent["spec"]["syncPolicy"]["automated"],
            {"prune": True, "selfHeal": True},
        )

    def test_agent8s_chart_is_in_the_helm_kubeconform_matrix(self) -> None:
        workflow = _load_yaml(REPO_ROOT / ".github" / "workflows" / "ci.yaml")
        matrix = workflow["jobs"]["helm-and-kubeconform"]["strategy"]["matrix"]["chart"]
        agent8s = [entry for entry in matrix if entry["name"] == "agent-8s"]
        self.assertEqual(
            agent8s,
            [
                {
                    "name": "agent-8s",
                    "path": "apps/agent-8s",
                    "release": "agent-8s",
                    "prod_values": "apps/agent-8s/values.yaml",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
