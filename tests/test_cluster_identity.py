from __future__ import annotations

import unittest
from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "cluster-identity.md"
DOCS_README = REPO_ROOT / "docs" / "README.md"
ROOT_README = REPO_ROOT / "README.md"
SPLATTOP_PROJECT = REPO_ROOT / "argocd" / "projects" / "splattop-project.yaml"
SPLATTOP_ROOT = REPO_ROOT / "argocd" / "applications" / "root.yaml"
SPLATTOP_PROD = REPO_ROOT / "argocd" / "applications" / "splattop-prod.yaml"

YAML_PARSER = YAML(typ="safe")


class ClusterIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.project = _load_yaml(SPLATTOP_PROJECT)
        cls.root = _load_yaml(SPLATTOP_ROOT)
        cls.prod = _load_yaml(SPLATTOP_PROD)

    def test_cluster_identity_doc_is_discoverable(self) -> None:
        self.assertIn("docs/cluster-identity.md", ROOT_README.read_text())
        self.assertIn("cluster-identity.md", DOCS_README.read_text())

    def test_live_garzai_cluster_identity_is_recorded(self) -> None:
        self.assertIn("k8s-nyc3-garz-ai", self.doc)
        self.assertIn("do-nyc3-k8s-nyc3-garz-ai", self.doc)
        self.assertIn("4124737a-6816-4ee9-af15-cf1c0f6f2f65", self.doc)
        self.assertIn("cesaregarza/GarzAICluster", self.doc)

    def test_internal_splattop_names_are_explicit_stays(self) -> None:
        project_name = self.project["metadata"]["name"]
        root_name = self.root["metadata"]["name"]
        prod_name = self.prod["metadata"]["name"]
        release_name = self.prod["spec"]["source"]["helm"]["releaseName"]

        self.assertEqual(project_name, "splattop")
        self.assertEqual(root_name, "splattop-root")
        self.assertEqual(prod_name, "splattop-prod")
        self.assertEqual(release_name, "splattop-prod")

        for name in (project_name, root_name, prod_name, release_name):
            self.assertIn(name, self.doc)
        self.assertIn("splattop-prod-prometheus", self.doc)
        self.assertIn("Prometheus TSDB", self.doc)
        self.assertIn("Grafana state", self.doc)

    def test_future_rename_sequence_preserves_state(self) -> None:
        required_phrases = (
            "staged maintenance event",
            "Preserve monitoring state first",
            "Create replacement AppProject and Application manifests",
            "Sync replacement apps while old apps are still present",
            "Delete the old Application resources only after",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, self.doc)


def _load_yaml(path: Path) -> dict:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"YAML mapping expected: {path}")
    return loaded
