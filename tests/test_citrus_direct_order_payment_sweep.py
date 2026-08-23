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
EXPECTED_COMMAND = [
    "python",
    "manage.py",
    "sweep_direct_order_payment_attempts",
    "--stale-minutes",
    "15",
    "--limit",
    "100",
]


def _render(*, dev: bool, enabled: bool = True) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    release = "citrus-dev" if dev else "citrus"
    command = [
        "helm",
        "template",
        release,
        str(CHART_PATH),
        "--namespace",
        release,
    ]
    if dev:
        command.extend(["-f", str(DEV_VALUES)])
    if not enabled:
        command.extend(["--set", "directOrderPaymentSweep.enabled=false"])

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


def _payment_sweeps(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        document
        for document in documents
        if document.get("kind") == "CronJob"
        and document.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "direct-order-payment-sweep"
    ]


class CitrusDirectOrderPaymentSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.renders = {
            "prod": _render(dev=False),
            "dev": _render(dev=True),
        }

    def test_prod_and_dev_render_one_guarded_sweep(self) -> None:
        for environment, documents in self.renders.items():
            with self.subTest(environment=environment):
                sweeps = _payment_sweeps(documents)
                self.assertEqual(len(sweeps), 1)
                sweep = sweeps[0]
                expected_name = (
                    "citrus-dev-direct-order-payment-sweep"
                    if environment == "dev"
                    else "citrus-direct-order-payment-sweep"
                )
                self.assertEqual(sweep["metadata"]["name"], expected_name)
                self.assertEqual(
                    sweep["metadata"]["annotations"][
                        "argocd.argoproj.io/sync-wave"
                    ],
                    "3",
                )
                self.assertEqual(sweep["spec"]["schedule"], "*/15 * * * *")
                self.assertEqual(sweep["spec"]["concurrencyPolicy"], "Forbid")

                pod_spec = sweep["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                self.assertFalse(pod_spec["automountServiceAccountToken"])
                self.assertEqual(pod_spec["restartPolicy"], "OnFailure")
                container = pod_spec["containers"][0]
                self.assertEqual(container["command"], EXPECTED_COMMAND)
                self.assertEqual(
                    container["envFrom"][0],
                    {"configMapRef": {"name": "django-config"}},
                )
                self.assertEqual(container["resources"]["requests"]["cpu"], "50m")
                self.assertEqual(container["resources"]["limits"]["memory"], "512Mi")

    def test_feature_can_be_disabled_without_leaving_a_cronjob(self) -> None:
        for dev in (False, True):
            with self.subTest(dev=dev):
                self.assertEqual(
                    _payment_sweeps(_render(dev=dev, enabled=False)),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
