from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "citrus"
CURRENT_PROD_SHA = "3f68967f777b2665fccb4f0ab423f339b8ea1357"
NEXT_SHA = "a" * 40
YAML_PARSER = YAML(typ="safe")


def _run(*extra: str, dev: bool = False) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    command = [
        "helm",
        "template",
        "citrus-dev" if dev else "citrus",
        str(CHART_PATH),
        "--namespace",
        "citrus-dev" if dev else "default",
        "-f",
        str(CHART_PATH / "values.yaml"),
    ]
    if dev:
        command.extend(["-f", str(CHART_PATH / "values-dev.yaml")])
    command.extend(extra)
    return subprocess.run(
        command,
        check=False,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


class CitrusStripeSmokePromotionTests(unittest.TestCase):
    def test_repository_contract_arms_prod_and_exempts_dev_rolls(self) -> None:
        values = YAML_PARSER.load(
            (CHART_PATH / "values.yaml").read_text(encoding="utf-8")
        )
        dev_values = YAML_PARSER.load(
            (CHART_PATH / "values-dev.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(values["image"]["tag"], CURRENT_PROD_SHA)
        self.assertEqual(
            values["stripeSmokePromotion"],
            {"enabled": True, "verifiedImageTag": CURRENT_PROD_SHA},
        )
        self.assertEqual(
            dev_values["stripeSmokePromotion"],
            {"enabled": False},
        )

        registry = json.loads(
            (CHART_PATH / "release-bindings.json").read_text(encoding="utf-8")
        )
        binding = next(
            item
            for item in registry["bindings"]
            if item["name"] == "stripe-smoke-promotion-image"
        )
        self.assertEqual(
            binding,
            {
                "name": "stripe-smoke-promotion-image",
                "enabledPath": "stripeSmokePromotion.enabled",
                "valuePath": "stripeSmokePromotion.verifiedImageTag",
                "policy": "manual-attestation",
            },
        )

        schema = json.loads(
            (CHART_PATH / "values.schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("stripeSmokePromotion", schema["required"])
        self.assertEqual(
            schema["properties"]["stripeSmokePromotion"],
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["enabled", "verifiedImageTag"],
                "properties": {
                    "enabled": {"type": "boolean"},
                    "verifiedImageTag": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{40}$",
                    },
                },
            },
        )

    def test_prod_render_accepts_only_an_exact_attested_sha(self) -> None:
        seeded = _run()
        self.assertEqual(seeded.returncode, 0, seeded.stderr)

        promoted = _run(
            "--set-string",
            f"image.tag={NEXT_SHA}",
            "--set-string",
            f"stripeSmokePromotion.verifiedImageTag={NEXT_SHA}",
        )
        self.assertEqual(promoted.returncode, 0, promoted.stderr)

        mismatch = _run("--set-string", f"image.tag={NEXT_SHA}")
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn(
            "stripeSmokePromotion.verifiedImageTag",
            mismatch.stderr,
        )
        self.assertIn("must exactly match image.tag", mismatch.stderr)

    def test_prod_render_rejects_nonimmutable_attestation_inputs(self) -> None:
        mutable_image = _run("--set-string", "image.tag=latest")
        self.assertNotEqual(mutable_image.returncode, 0)
        self.assertIn("lowercase 40-hex immutable source tag", mutable_image.stderr)

        short_attestation = _run(
            "--set-string",
            "stripeSmokePromotion.verifiedImageTag=abcd",
        )
        self.assertNotEqual(short_attestation.returncode, 0)
        self.assertIn(
            "stripeSmokePromotion.verifiedImageTag",
            short_attestation.stderr,
        )

    def test_dev_image_roll_is_not_a_production_attestation(self) -> None:
        dev = _run("--set-string", f"image.tag={NEXT_SHA}", dev=True)
        self.assertEqual(dev.returncode, 0, dev.stderr)


if __name__ == "__main__":
    unittest.main()
