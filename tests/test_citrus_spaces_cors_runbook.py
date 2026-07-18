from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORS_POLICY = REPO_ROOT / "infra" / "spaces" / "citrus-media-cors.json"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "citrus-spaces-cors.md"
SCRIPT = REPO_ROOT / "scripts" / "apply_citrus_spaces_cors.sh"
DOCS_README = REPO_ROOT / "docs" / "README.md"
ROOT_README = REPO_ROOT / "README.md"
PROD_VALUES = REPO_ROOT / "helm" / "citrus" / "values.yaml"
DEV_VALUES = REPO_ROOT / "helm" / "citrus" / "values-dev.yaml"


class CitrusSpacesCorsRunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = json.loads(CORS_POLICY.read_text(encoding="utf-8"))
        cls.runbook = RUNBOOK.read_text(encoding="utf-8")
        cls.script = SCRIPT.read_text(encoding="utf-8")
        cls.prod_values = PROD_VALUES.read_text(encoding="utf-8")
        cls.dev_values = DEV_VALUES.read_text(encoding="utf-8")

    def test_cors_policy_matches_direct_upload_contract(self) -> None:
        self.assertEqual(
            self.policy,
            {
                "CORSRules": [
                    {
                        "ID": "citrus-grace-browser-media-upload",
                        "AllowedOrigins": [
                            "https://citrus-grace.com",
                            "https://www.citrus-grace.com",
                            "https://dev.citrus-grace.com",
                        ],
                        "AllowedMethods": ["GET", "POST"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )

    def test_policy_tracks_citrus_helm_buckets_and_hosts(self) -> None:
        rule = self.policy["CORSRules"][0]
        prod_bucket = _extract_scalar(self.prod_values, "AWS_STORAGE_BUCKET_NAME")
        dev_bucket = _extract_scalar(self.dev_values, "AWS_STORAGE_BUCKET_NAME")

        self.assertEqual(prod_bucket, "citrus-media")
        self.assertEqual(dev_bucket, "citrus-media-dev")
        self.assertIn(prod_bucket, self.runbook)
        self.assertIn(dev_bucket, self.runbook)
        self.assertIn(prod_bucket, self.script)
        self.assertIn(dev_bucket, self.script)

        expected_origins = {f"https://{host}" for host in _extract_ingress_hosts(self.prod_values)}
        expected_origins.update(f"https://{host}" for host in _extract_ingress_hosts(self.dev_values))
        self.assertEqual(set(rule["AllowedOrigins"]), expected_origins)

    def test_runbook_is_discoverable_and_records_apply_commands(self) -> None:
        self.assertIn(
            "docs/runbooks/citrus-spaces-cors.md",
            ROOT_README.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "runbooks/citrus-spaces-cors.md",
            DOCS_README.read_text(encoding="utf-8"),
        )

        required_phrases = (
            "infra/spaces/citrus-media-cors.json",
            "scripts/apply_citrus_spaces_cors.sh",
            "doctl spaces keys create",
            "s3cmd setcors",
            "doctl spaces keys list",
            'CITRUS_SPACES_KEY_GRANTS="bucket=;permission=fullaccess"',
            "curl -i -X OPTIONS",
            "Access-Control-Request-Method: POST",
            "purge the CDN cache",
            "must not apply bucket CORS unattended",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, self.runbook)

    def test_apply_script_uses_checked_in_policy_for_both_buckets(self) -> None:
        required_phrases = (
            'CONFIG_PATH="${CITRUS_SPACES_CORS_CONFIG:-${REPO_ROOT}/infra/spaces/citrus-media-cors.json}"',
            'SPACES_REGION="${SPACES_REGION:-nyc3}"',
            'SPACES_HOST_BASE="${SPACES_HOST_BASE:-${SPACES_REGION}.digitaloceanspaces.com}"',
            'KEY_GRANTS="${CITRUS_SPACES_KEY_GRANTS:-bucket=;permission=fullaccess}"',
            'BUCKETS=("citrus-media" "citrus-media-dev")',
            "doctl spaces keys create",
            "doctl spaces keys delete",
            "s3cmd",
            "setcors",
            "curl -fsS -D - -o /dev/null -X OPTIONS",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, self.script)


def _extract_scalar(yaml_text: str, key: str) -> str:
    prefix = f"{key}:"
    for line in yaml_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped.removeprefix(prefix).strip().strip('"')
    raise AssertionError(f"Missing scalar key: {key}")


def _extract_ingress_hosts(yaml_text: str) -> list[str]:
    lines = yaml_text.splitlines()
    for index, line in enumerate(lines):
        if line == "ingress:":
            for hosts_index in range(index + 1, len(lines)):
                if lines[hosts_index] == "  hosts:":
                    hosts: list[str] = []
                    for host_line in lines[hosts_index + 1 :]:
                        if host_line.startswith("    - "):
                            hosts.append(host_line.removeprefix("    - ").strip().strip('"'))
                            continue
                        if host_line and not host_line.startswith("    "):
                            break
                    if hosts:
                        return hosts
    raise AssertionError("Missing ingress hosts")
