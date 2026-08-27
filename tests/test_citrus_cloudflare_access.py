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
SOURCE_SHA = "a" * 40
HELM_JSON_POINTER = re.compile(
    r"(?P<quote>['\"])/(?P<path>[A-Za-z0-9_./-]+)(?P=quote)"
)


def _normalize_helm_error(stderr: str) -> str:
    return HELM_JSON_POINTER.sub(
        lambda match: (
            f"{match.group('quote')}"
            f"{match.group('path').replace('/', '.')}"
            f"{match.group('quote')}"
        ),
        stderr,
    )


def _helm(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    return subprocess.run(
        ["helm", "template", "citrus", str(CHART_PATH), *arguments],
        check=False,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _render(*, dev: bool = False, enabled: bool = False) -> list[dict[str, Any]]:
    arguments = [
        "--namespace",
        "citrus-dev" if dev else "default",
    ]
    if dev:
        arguments.extend(["-f", str(DEV_VALUES)])
    if enabled:
        owner = "citrus-dev" if dev else "citrus"
        secret_name = (
            "citrus-dev-cloudflare-access"
            if dev
            else "citrus-cloudflare-access"
        )
        arguments.extend([
            "--set",
            "cloudflareAccess.enabled=true",
            "--set-string",
            f"cloudflareAccess.owner={owner}",
            "--set-string",
            f"cloudflareAccess.secretName={secret_name}",
            "--set-string",
            "cloudflareAccess.rolloutRevision=ces-829-test",
            "--set-string",
            f"cloudflareAccess.verifiedImageTag={SOURCE_SHA}",
            "--set-string",
            f"image.tag={SOURCE_SHA}",
        ])
    result = _helm(arguments)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]


def _web(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") in {"citrus", "citrus-dev"}
    )


def _web_container(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        container
        for container in _web(documents)["spec"]["template"]["spec"]["containers"]
        if container["name"] == "django"
    )


class CitrusCloudflareAccessTests(unittest.TestCase):
    def test_current_argo_renders_remain_byte_inert(self) -> None:
        for dev in (False, True):
            default = _helm(
                [
                    "--namespace",
                    "citrus-dev" if dev else "default",
                    *(["-f", str(DEV_VALUES)] if dev else []),
                ]
            )
            explicit_disabled = _helm(
                [
                    "--namespace",
                    "citrus-dev" if dev else "default",
                    *(["-f", str(DEV_VALUES)] if dev else []),
                    "--set",
                    "cloudflareAccess.enabled=false",
                ]
            )
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertEqual(explicit_disabled.returncode, 0, explicit_disabled.stderr)
            self.assertEqual(default.stdout, explicit_disabled.stdout)
            self.assertNotIn("CLOUDFLARE_ACCESS_", default.stdout)
            self.assertNotIn("cloudflare-access-rollout-revision", default.stdout)

    def test_enabled_gate_projects_exact_non_optional_keys_to_web_only(self) -> None:
        for dev in (False, True):
            documents = _render(dev=dev, enabled=True)
            web = _web(documents)
            container = _web_container(documents)
            owner = "citrus-dev" if dev else "citrus"
            secret_name = (
                "citrus-dev-cloudflare-access"
                if dev
                else "citrus-cloudflare-access"
            )
            env = {item["name"]: item for item in container["env"]}

            self.assertEqual(env["CLOUDFLARE_ACCESS_REQUIRED"], {
                "name": "CLOUDFLARE_ACCESS_REQUIRED",
                "value": "true",
            })
            self.assertEqual(
                {
                    name: item["valueFrom"]["secretKeyRef"]
                    for name, item in env.items()
                    if name.startswith("CLOUDFLARE_ACCESS_")
                    and name != "CLOUDFLARE_ACCESS_REQUIRED"
                },
                {
                    "CLOUDFLARE_ACCESS_TEAM_DOMAIN": {
                        "name": secret_name,
                        "key": "CLOUDFLARE_ACCESS_TEAM_DOMAIN",
                        "optional": False,
                    },
                    "CLOUDFLARE_ACCESS_AUD": {
                        "name": secret_name,
                        "key": "CLOUDFLARE_ACCESS_AUD",
                        "optional": False,
                    },
                    "CLOUDFLARE_ACCESS_ALLOWED_EMAILS": {
                        "name": secret_name,
                        "key": "CLOUDFLARE_ACCESS_ALLOWED_EMAILS",
                        "optional": False,
                    },
                },
            )
            self.assertEqual(
                web["spec"]["template"]["metadata"]["annotations"]
                ["citrus.grace/cloudflare-access-rollout-revision"],
                "ces-829-test",
            )
            self.assertTrue(container["image"].endswith(f":{SOURCE_SHA}"))
            self.assertFalse(any(
                source.get("secretRef", {}).get("name") == secret_name
                for source in container.get("envFrom", [])
            ))
            self.assertFalse(any(
                secret_name in str(document)
                for document in documents
                if document is not web
            ))
            self.assertEqual(owner, "citrus-dev" if dev else "citrus")

    def test_enabled_gate_never_places_confidential_values_in_configmap(self) -> None:
        for documents in (_render(enabled=True), _render(dev=True, enabled=True)):
            config_map = next(
                document
                for document in documents
                if document.get("kind") == "ConfigMap"
                and document.get("metadata", {}).get("name") == "django-config"
            )
            self.assertFalse(any(
                key.startswith("CLOUDFLARE_ACCESS_")
                for key in config_map["data"]
            ))

    def test_activation_fails_closed_on_invalid_contract(self) -> None:
        valid = [
            "--namespace",
            "default",
            "--set",
            "cloudflareAccess.enabled=true",
            "--set-string",
            "cloudflareAccess.owner=citrus",
            "--set-string",
            "cloudflareAccess.secretName=citrus-cloudflare-access",
            "--set-string",
            "cloudflareAccess.rolloutRevision=ces-829-test",
            "--set-string",
            f"cloudflareAccess.verifiedImageTag={SOURCE_SHA}",
            "--set-string",
            f"image.tag={SOURCE_SHA}",
        ]
        cases = (
            (["--set", "cloudflareAccess.enabled=true"], "cloudflareAccess.owner"),
            (["--set-string", "cloudflareAccess.enabled=true"], "cloudflareAccess.enabled"),
            (
                [*valid, "--set-string", "cloudflareAccess.owner=citrus-dev"],
                "cloudflareAccess.secretName",
            ),
            (
                ["--namespace", "citrus-dev", *valid[2:]],
                "cloudflareAccess.owner=citrus requires the default namespace",
            ),
            (
                [*valid, "--set-string", "cloudflareAccess.rolloutRevision=not safe"],
                "cloudflareAccess.rolloutRevision",
            ),
            (
                [*valid, "--set-string", "cloudflareAccess.verifiedImageTag=" + "b" * 40],
                "cloudflareAccess.verifiedImageTag must exactly match image.tag",
            ),
            (
                [*valid, "--set-string", "image.tag=mutable-tag"],
                "cloudflareAccess.verifiedImageTag must exactly match image.tag",
            ),
            (
                [
                    "--namespace",
                    "citrus-dev",
                    "-f",
                    str(DEV_VALUES),
                    "--set",
                    "cloudflareAccess.enabled=true",
                    "--set-string",
                    "cloudflareAccess.owner=citrus-dev",
                    "--set-string",
                    "cloudflareAccess.secretName=citrus-dev-cloudflare-access",
                    "--set-string",
                    "cloudflareAccess.rolloutRevision=ces-829-test",
                    "--set-string",
                    f"cloudflareAccess.verifiedImageTag={SOURCE_SHA}",
                    "--set-string",
                    f"image.tag={SOURCE_SHA}",
                    "--set",
                    "paymentSafety.enabled=true",
                    "--set-string",
                    "paymentSafety.environment=development",
                    "--set-string",
                    "paymentSafety.owner=citrus-dev",
                    "--set-string",
                    "paymentSafety.networkMode=deny",
                    "--set",
                    "paymentSafety.policy.required=true",
                    "--set-string",
                    "paymentSafety.policy.provider=cilium",
                    "--set-string",
                    "paymentSafety.policy.revision=ces-829-test",
                    "--set",
                    "paymentSafety.networkPolicy.enabled=true",
                    "--set-string",
                    "paymentSafety.networkPolicy.database.host=db.dev.example",
                ],
                "cloudflareAccess cannot be enabled with "
                "paymentSafety.networkMode=deny",
            ),
        )
        for arguments, expected in cases:
            with self.subTest(expected=expected):
                result = _helm(arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, _normalize_helm_error(result.stderr))


if __name__ == "__main__":
    unittest.main()
