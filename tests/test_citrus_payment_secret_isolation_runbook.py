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

    def test_target_removes_broad_dev_secret_projection(self) -> None:
        target = _section(
            self.runbook,
            "## Target state",
            "## Repository preparation in PR #576",
        )
        normalized = " ".join(target.split())
        required = (
            "no longer imports it with `envFrom`",
            "No process receives its legacy payment API, generic webhook, or "
            "production webhook fields",
            "Every dev Django process receives the same dedicated test-mode API",
            "separates dev from production, not one dev process from another",
            "non-live",
            "environment-explicit dev webhook field",
            "production webhook settings are forbidden in dev",
            "CES-845 remains separate",
        )
        for phrase in required:
            self.assertIn(phrase, normalized)

    def test_operator_decision_is_value_free_and_test_mode(self) -> None:
        decision = _section(
            self.runbook,
            "## Remaining dev credential decision",
            "## Ordered execution",
        )
        normalized = " ".join(decision.split())
        self.assertIn("test-mode API setting", normalized)
        self.assertIn("semantic mode only", normalized)
        self.assertIn("existing `STRIPE_WEBHOOK_SECRET_DEV` field", normalized)
        self.assertIn("No generic or production webhook field is projected", normalized)

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
            "Leave the encrypted dev `django-secrets` source unchanged",
            "replace its broad `envFrom` import",
            "every dev Django runtime",
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
