from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "infra" / "external-dns" / "deployment.yaml"
YAML_PARSER = YAML(typ="safe")


class ExternalDnsLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        document = YAML_PARSER.load(MANIFEST.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise AssertionError("Deployment manifest must be a mapping")
        cls.deployment: dict[str, Any] = document
        cls.args = document["spec"]["template"]["spec"]["containers"][0]["args"]

    def test_resume_removes_pause_and_legacy_class(self) -> None:
        self.assertEqual(self.args.count("--dry-run"), 0)
        self.assertEqual(self.args.count("--ingress-class=nginx"), 0)
        self.assertEqual(self.args.count("--ingress-class=traefik-nginx"), 1)

    def test_resume_preserves_external_dns_identity_and_scope(self) -> None:
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
            "--txt-prefix=_externaldns%{record_type}.",
            "--interval=1m",
        ):
            self.assertIn(argument, self.args)

        container = self.deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(
            container["env"][0]["valueFrom"]["secretKeyRef"],
            {"name": "cloudflare-api-token", "key": "api-token"},
        )

    def test_prefix_preserves_legacy_read_form_and_keeps_apex_in_zone(self) -> None:
        prefix = next(arg.split("=", 1)[1] for arg in self.args if arg.startswith("--txt-prefix="))
        self.assertEqual(prefix.replace("%{record_type}", ""), "_externaldns.")
        for host, zone in (("cegarza.com", "cegarza.com"), ("dev.cegarza.com", "cegarza.com"), ("blog.splat.top", "splat.top")):
            name = prefix.replace("%{record_type}", "a") + host
            self.assertTrue(name.endswith("." + zone))
        self.assertFalse("_externaldns.a-cegarza.com".endswith(".cegarza.com"))


if __name__ == "__main__":
    unittest.main()
