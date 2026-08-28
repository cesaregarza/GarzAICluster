from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.check_citrus_recurring_runtime_render import (
    ContractError,
    run_contract,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / "helm" / "citrus"


class CitrusRecurringRuntimeRenderCheckTests(unittest.TestCase):
    def test_checker_renders_the_complete_synthetic_matrix(self) -> None:
        helm = shutil.which("helm")
        if helm is None:
            self.skipTest("helm is required for chart render tests")
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "rendered"
            receipt = run_contract(
                chart=CHART_PATH,
                output_dir=output_dir,
                helm=helm,
                lint=False,
            )
            self.assertEqual(receipt["result"], "succeeded")
            self.assertEqual(receipt["render_count"], 6)
            self.assertEqual(
                {path.name for path in output_dir.glob("*.yaml")},
                {
                    "citrus-prod.yaml",
                    "citrus-dev.yaml",
                    "citrus-payment-prod.yaml",
                    "citrus-payment-dev.yaml",
                    "citrus-payment-safety-prod.yaml",
                    "citrus-payment-safety-dev.yaml",
                },
            )

    def test_checker_rejects_a_nonempty_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "stale.yaml").write_text("stale\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "must be empty"):
                run_contract(
                    chart=CHART_PATH,
                    output_dir=output_dir,
                    helm="helm",
                    lint=False,
                )


if __name__ == "__main__":
    unittest.main()
