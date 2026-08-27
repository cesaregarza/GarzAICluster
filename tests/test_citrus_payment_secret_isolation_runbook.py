from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK_PATH = (
    REPO_ROOT / "docs" / "runbooks" / "citrus-payment-secret-isolation.md"
)
DOCS_README_PATH = REPO_ROOT / "docs" / "README.md"
ROOT_README_PATH = REPO_ROOT / "README.md"


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


class CitrusPaymentSecretIsolationRunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
        cls.normalized = " ".join(cls.runbook.split())

    def test_runbook_is_discoverable(self) -> None:
        relative_path = "docs/runbooks/citrus-payment-secret-isolation.md"
        self.assertIn(
            relative_path,
            ROOT_README_PATH.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "runbooks/citrus-payment-secret-isolation.md",
            DOCS_README_PATH.read_text(encoding="utf-8"),
        )

    def test_threat_model_trusts_shared_gitops_and_does_not_rotate_prod(self) -> None:
        threat_model = _section(
            self.runbook,
            "## Threat model and production decision",
            "## Incident baseline",
        )
        required = (
            "shared Argo CD instance and shared SOPS recipient are acceptable",
            "configuration error, not a credential disclosure",
            "No production replacement, rotation, or revocation is required",
            "Production Secret sources, values, Applications, workloads, and "
            "provider state remain unchanged",
        )
        for phrase in required:
            self.assertIn(phrase, " ".join(threat_model.split()))

    def test_baseline_inventories_projection_and_rbac_without_values(self) -> None:
        baseline = _section(
            self.runbook,
            "## Incident baseline",
            "## Ownership",
        )
        normalized = " ".join(baseline.split())
        required = (
            "citrus-dev/django-secrets",
            "default/django-secrets",
            "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET_PROD",
            "web and media Deployments",
            "billing Deployment and metrics sidecar",
            "cannot get or list Secrets and cannot create Pods",
            "not cross-namespace RBAC",
        )
        for phrase in required:
            self.assertIn(phrase, normalized)

    def test_target_removes_payment_from_broad_dev_secret(self) -> None:
        target = _section(
            self.runbook,
            "## Target state",
            "## Repository preparation in PR #576",
        )
        normalized = " ".join(target.split())
        required = (
            "Every payment key is removed from `citrus-dev/django-secrets`",
            "No dedicated payment Secret is projected elsewhere",
            "non-live",
            "intentionally-absent",
            "production webhook settings are forbidden in dev",
            "CES-845 remains separate",
        )
        for phrase in required:
            self.assertIn(phrase, normalized)

    def test_current_source_requires_real_dev_decision(self) -> None:
        decision = _section(
            self.runbook,
            "## Remaining dev credential decision",
            "## Ordered execution",
        )
        normalized = " ".join(decision.split())
        self.assertIn("will not start with payment API and webhook settings absent", normalized)
        self.assertIn("dedicated Stripe test-mode", normalized)
        self.assertIn("CES-845 implements and verifies intentional absence", normalized)
        self.assertIn("Do not disguise a placeholder or arbitrary sentinel", normalized)

    def test_dev_source_gate_is_dev_only_and_value_free(self) -> None:
        gate = _section(
            self.runbook,
            "### Gate B: prepare the dev-only encrypted source",
            "### Gate C: merge, reconcile, and roll dev",
        )
        normalized = " ".join(gate.split())
        required = (
            "explicit encrypted-source write and PR-publication authorization",
            "citrus-dev-payment-credentials",
            "Remove every payment key from the encrypted dev `django-secrets` source",
            "values-payment-dev.yaml",
            "Do not change the production Application",
            "No plaintext or identifying derivative",
            "Do not merge",
        )
        for phrase in required:
            self.assertIn(phrase, normalized)

    def test_live_gate_keeps_production_unchanged_and_requires_verification(self) -> None:
        gate = _section(
            self.runbook,
            "### Gate C: merge, reconcile, and roll dev",
            "## Sanitized receipt",
        )
        normalized = " ".join(gate.split())
        required = (
            "authorization for GitOps merge",
            "rollout/restart",
            "citrus-dev-secrets",
            "zero-network classifier",
            "independent verifier/reviewer",
            "Leave production unchanged",
        )
        for phrase in required:
            self.assertIn(phrase, normalized)

    def test_receipt_is_value_free_and_financially_inert(self) -> None:
        receipt = _section(
            self.runbook,
            "## Sanitized receipt",
            "## Stop and rollback rules",
        )
        normalized = " ".join(receipt.split())
        required = (
            "secret_object",
            "secret_key_name",
            "classification",
            "workload_revision",
            "no credential value, fragment, hash, fingerprint",
            "financial object",
            "test event",
        )
        for phrase in required:
            self.assertIn(phrase, normalized)

        forbidden_commands = (
            "printenv",
            "kubectl exec",
            "sops --decrypt",
            "sops -d ",
            "kubectl get secret",
        )
        for command in forbidden_commands:
            self.assertNotIn(command, self.runbook)


if __name__ == "__main__":
    unittest.main()
