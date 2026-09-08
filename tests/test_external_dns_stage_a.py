from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "infra" / "external-dns" / "deployment.yaml"
YAML_PARSER = YAML(typ="safe")


class ExternalDnsStageATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        document = YAML_PARSER.load(MANIFEST.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise AssertionError("Deployment manifest must be a mapping")
        cls.deployment: dict[str, Any] = document
        cls.args = document["spec"]["template"]["spec"]["containers"][0]["args"]

    def test_stage_a_pauses_writes_and_accepts_both_ingress_classes(self) -> None:
        self.assertEqual(self.args.count("--dry-run"), 1)
        self.assertEqual(self.args.count("--ingress-class=nginx"), 1)
        self.assertEqual(self.args.count("--ingress-class=traefik-nginx"), 1)

    def test_stage_a_preserves_external_dns_identity_and_scope(self) -> None:
        for argument in (
            "--source=ingress",
            "--source=service",
            "--domain-filter=splat.top",
            "--domain-filter=garz.ai",
            "--domain-filter=cegarza.com",
            "--provider=cloudflare",
            "--policy=sync",
            "--registry=txt",
            "--txt-owner-id=splattop-prod",
            "--txt-prefix=_externaldns.",
            "--interval=1m",
        ):
            self.assertIn(argument, self.args)

        container = self.deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(
            container["env"][0]["valueFrom"]["secretKeyRef"],
            {"name": "cloudflare-api-token", "key": "api-token"},
        )


if __name__ == "__main__":
    unittest.main()
