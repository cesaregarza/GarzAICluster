from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "citrus"
DEV_VALUES = CHART_PATH / "values-dev.yaml"
YAML_PARSER = YAML(typ="safe")
DUMMY_BACKEND = "django.core.mail.backends.dummy.EmailBackend"


def _render(*, dev: bool) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    command = [
        "helm",
        "template",
        "citrus-dev" if dev else "citrus",
        str(CHART_PATH),
        "--namespace",
        "citrus-dev" if dev else "default",
    ]
    if dev:
        command.extend(["-f", str(DEV_VALUES)])

    result = subprocess.run(
        command,
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


def _config_map(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document.get("kind") == "ConfigMap"
        and document.get("metadata", {}).get("name") == "django-config"
    )


def _deployments(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name")
        in {"citrus", "citrus-media-worker", "citrus-dev", "citrus-dev-media-worker"}
    ]


class CitrusDevMailSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dev_documents = _render(dev=True)
        cls.prod_documents = _render(dev=False)

    def test_dev_discards_outbound_mail(self) -> None:
        self.assertEqual(
            _config_map(self.dev_documents)["data"]["EMAIL_BACKEND"],
            DUMMY_BACKEND,
        )

    def test_production_mail_backend_is_not_overridden(self) -> None:
        self.assertNotIn(
            "EMAIL_BACKEND",
            _config_map(self.prod_documents)["data"],
        )

    def test_dev_config_changes_roll_web_and_worker_pods(self) -> None:
        deployments = _deployments(self.dev_documents)
        self.assertEqual(len(deployments), 2)
        for deployment in deployments:
            checksum = (
                deployment["spec"]["template"]["metadata"]
                ["annotations"]["checksum/config"]
            )
            self.assertRegex(checksum, re.compile(r"^[0-9a-f]{64}$"))

    def test_production_config_changes_roll_web_and_worker_pods(self) -> None:
        deployments = _deployments(self.prod_documents)
        self.assertEqual(len(deployments), 2)
        for deployment in deployments:
            checksum = (
                deployment["spec"]["template"]["metadata"]
                ["annotations"]["checksum/config"]
            )
            self.assertRegex(checksum, re.compile(r"^[0-9a-f]{64}$"))


if __name__ == "__main__":
    unittest.main()
