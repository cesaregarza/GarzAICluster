from __future__ import annotations

import unittest
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
APPLICATIONS_DIR = REPO_ROOT / "argocd" / "applications"
PROJECT_PATH = REPO_ROOT / "argocd" / "projects" / "splattop-project.yaml"
CITRUS_DEV_VALUES_PATH = REPO_ROOT / "helm" / "citrus" / "values-dev.yaml"
YAML_PARSER = YAML(typ="safe")

EXPECTED_AUTOMATED_APPLICATIONS = {
    "agent-control-plane-registry-overlay",
    "agent-control-plane-secrets",
    "agent-control-plane-skills",
    "argocd-repositories",
    "citrus",
    "citrus-dev",
    "citrus-dev-secrets",
    "citrus-secrets",
    "external-dns",
    "garz-ai",
    "garz-ai-secrets",
    "metrics-server",
    "poetry",
    "poetry-secrets",
    "splattop-bots",
    "splattop-root",
    "vanity-hosts",
}
EXPECTED_MANUAL_APPLICATIONS = {
    "agent-control-plane",
    "agent-workloads",
    "agent-workloads-secrets",
    "cegarza-blog",
    "cegarza-blog-secrets",
    "garz-observability",
    "skyquiet-server",
    "skyquiet-server-secrets",
    "splattop-blog-prod",
    "splattop-blog-secrets",
    "splattop-prod",
    "splattop-prod-comp-auth-secrets",
    "spotify-hot-100",
    "spotify-hot-100-secrets",
}


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"YAML mapping expected: {path}")
    return loaded


class ArgoCdSyncPolicyTests(unittest.TestCase):
    def test_project_has_no_time_based_sync_window(self) -> None:
        project = _load_yaml(PROJECT_PATH)
        self.assertNotIn("syncWindows", project["spec"])

    def test_citrus_dev_generated_tls_secret_has_exact_orphan_exception(
        self,
    ) -> None:
        project = _load_yaml(PROJECT_PATH)
        citrus_dev_values = _load_yaml(CITRUS_DEV_VALUES_PATH)
        orphaned_resources = project["spec"]["orphanedResources"]
        dev_tls_secret_name = citrus_dev_values["ingress"]["tls"]["secretName"]
        expected = {
            "group": "",
            "kind": "Secret",
            "name": dev_tls_secret_name,
        }

        self.assertIs(orphaned_resources["warn"], True)
        ignored_secrets = [
            resource
            for resource in orphaned_resources["ignore"]
            if resource.get("group") == "" and resource.get("kind") == "Secret"
        ]
        self.assertEqual(ignored_secrets.count(expected), 1)
        self.assertEqual(
            [
                resource
                for resource in ignored_secrets
                if fnmatchcase(dev_tls_secret_name, resource["name"])
            ],
            [expected],
        )
        self.assertFalse(
            any(
                fnmatchcase("citrus-grace-tls", resource["name"])
                for resource in ignored_secrets
            )
        )

    def test_every_application_has_an_explicit_reviewed_sync_boundary(self) -> None:
        applications = {
            application["metadata"]["name"]: application
            for application in (
                _load_yaml(path) for path in sorted(APPLICATIONS_DIR.glob("*.yaml"))
            )
        }
        automated = {
            name
            for name, application in applications.items()
            if application["spec"].get("syncPolicy", {}).get("automated") is not None
        }
        manual = set(applications) - automated

        self.assertEqual(automated, EXPECTED_AUTOMATED_APPLICATIONS)
        self.assertEqual(manual, EXPECTED_MANUAL_APPLICATIONS)


if __name__ == "__main__":
    unittest.main()
