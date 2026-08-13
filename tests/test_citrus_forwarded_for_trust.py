from __future__ import annotations

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
TRUSTED_DOKS_POD_CIDR = "10.244.0.0/16"


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


class CitrusForwardedForTrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dev_config = _config_map(_render(dev=True))["data"]
        cls.prod_config = _config_map(_render(dev=False))["data"]

    def test_prod_and_dev_trust_only_the_doks_pod_network(self) -> None:
        for config in (self.prod_config, self.dev_config):
            self.assertEqual(config["TRUST_X_FORWARDED_FOR"], "True")
            self.assertEqual(
                config["TRUSTED_PROXY_IPS"],
                TRUSTED_DOKS_POD_CIDR,
            )

    def test_payment_setup_feature_is_not_activated_by_proxy_fix(self) -> None:
        for config in (self.prod_config, self.dev_config):
            self.assertNotIn("DIRECT_ORDER_PAYMENT_SETUP_ENABLED", config)


if __name__ == "__main__":
    unittest.main()
