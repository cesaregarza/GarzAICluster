from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = (
    REPO_ROOT / "docs" / "runbooks" / "citrus-stripe-smoke-gate.md"
)


class CitrusStripeSmokeRunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runbook = RUNBOOK.read_text(encoding="utf-8")
        cls.normalized = " ".join(cls.runbook.split())

    def test_runbook_pins_dev_only_activation_and_no_blind_retry(self) -> None:
        for required in (
            "citrus-dev",
            "CES-854",
            "test-mode-only",
            "ApplyOutOfSyncOnly=true",
            "no Argo retry block",
            "backoffLimit: 0",
            "Never delete the whole ConfigMap",
            "Never add an Argo retry policy or Kubernetes Job retry",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.normalized)

    def test_runbook_documents_durable_outcomes_and_exact_rerun_scope(self) -> None:
        for required in (
            "citrus-dev-stripe-smoke-receipts",
            "`passed`",
            "`failed`",
            "`running`",
            "zero provider contact",
            "remove only the exact SHA entry",
            "remove only the failed exact-SHA Job",
            "zero active runner-owned objects",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.normalized)

    def test_runbook_forbids_sensitive_receipt_content_and_prod_mutation(self) -> None:
        for required in (
            "does not authorize a production read, write, deployment",
            "Never put the",
            "provider object identifier",
            "credential",
            "secret fragment",
            "does not erase",
            "or mutate production",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.normalized)

    def test_runbook_pins_exact_sha_promotion_attestation(self) -> None:
        for required in (
            "CES-806 merge-day checklist",
            "durable `passed` receipt",
            "`image.tag` and `stripeSmokePromotion.verifiedImageTag`",
            "same reviewed promotion commit",
            "release updater deliberately fails closed",
            "Never disable the gate",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.normalized)


if __name__ == "__main__":
    unittest.main()
