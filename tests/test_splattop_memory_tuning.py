from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")


def _render_splattop_prod() -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")

    result = subprocess.run(
        [
            "helm",
            "template",
            "splattop-prod",
            "helm/splattop",
            "--namespace",
            "default",
            "-f",
            "helm/splattop/values-prod.yaml",
        ],
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return [
        doc
        for doc in YAML_PARSER.load_all(result.stdout)
        if isinstance(doc, dict) and doc
    ]


class SplatTopMemoryTuningTests(unittest.TestCase):
    def test_splatnlp_is_not_rendered_in_production(self) -> None:
        docs = _render_splattop_prod()
        splatnlp_objects = [
            doc
            for doc in docs
            if "splatnlp" in doc.get("metadata", {}).get("name", "")
        ]

        self.assertEqual(splatnlp_objects, [])


if __name__ == "__main__":
    unittest.main()
