from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK_PATH = (
    REPO_ROOT / "docs" / "runbooks" / "citrus-payment-secret-isolation.md"
)
DOCS_README_PATH = REPO_ROOT / "docs" / "README.md"
ROOT_README_PATH = REPO_ROOT / "README.md"

SAFE_AUTHORIZATION_FIELDS = {
    "gate",
    "mutation_scope",
    "authorization_reference",
    "target_environment",
    "target_system",
    "target_resource",
    "reviewed_source_revision",
    "reviewed_tree_digest",
    "impact_matrix_reference",
    "authorized_by",
    "authorized_at",
    "expires_at",
}
SAFE_ACCESS_FIELDS = {
    "observed_at",
    "environment",
    "secret_source",
    "secret_object",
    "secret_key_name",
    "credential_role",
    "classification",
    "source_owner",
    "rotation_owner",
    "created_or_rotated_at",
    "supersession_status",
    "access_policy_reference",
    "isolation_result",
    "reviewer",
}
SAFE_ROLLOUT_FIELDS = {
    "observed_at",
    "environment",
    "source_sha",
    "gitops_revision",
    "reviewed_source_revision",
    "reviewed_tree_digest",
    "impact_matrix_reference",
    "merge_result_revision",
    "tree_equivalence_result",
    "argo_application",
    "argo_revision",
    "argo_sync_status",
    "argo_health_status",
    "namespace",
    "workload_kind",
    "workload_name",
    "workload_revision",
    "container_name",
    "container_image",
    "container_image_digest",
    "projection_path",
    "desired_replicas",
    "ready_replicas",
    "classifier_version",
    "verification_method_class",
    "verification_result",
    "supersession_status",
    "rollback_revision",
    "reviewer",
}


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _schema_fields(text: str, heading: str, next_heading: str) -> set[str]:
    section = _section(text, heading, next_heading)
    block = section.split("```text", 1)[1].split("```", 1)[0]
    return {line.strip() for line in block.splitlines() if line.strip()}


def _sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        normalized = " ".join(line.strip() for line in paragraph.splitlines())
        sentences.extend(re.split(r"(?<=[.!?])\s+", normalized))
    return [sentence for sentence in sentences if sentence]


def _clauses(text: str) -> list[str]:
    clauses: list[str] = []
    for sentence in _sentences(text):
        clauses.extend(
            re.split(
                r";\s+|,\s+(?=(?:but|however|then|and then)\b)",
                sentence,
                flags=re.IGNORECASE,
            )
        )
    return [clause for clause in clauses if clause]


class CitrusPaymentSecretIsolationRunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
        cls.gates = {
            number: _section(
                cls.runbook,
                f"### Gate {number}:",
                f"### Gate {number + 1}:",
            )
            for number in range(7)
        }
        cls.gates[7] = _section(
            cls.runbook,
            "### Gate 7:",
            "## Sanitized receipt schema",
        )
        cls.normalized_gates = {
            number: " ".join(section.split())
            for number, section in cls.gates.items()
        }

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

    def test_replacement_and_dev_isolation_precede_revocation(self) -> None:
        replacement_heading = (
            "### Gate 4: deploy and verify the production replacement"
        )
        dev_heading = "### Gate 5: isolate and roll dev"
        revocation_heading = (
            "### Gate 6: revoke the superseded production authority"
        )
        self.assertLess(
            self.runbook.index(replacement_heading),
            self.runbook.index(dev_heading),
        )
        self.assertLess(
            self.runbook.index(dev_heading),
            self.runbook.index(revocation_heading),
        )

        retirement_action = re.compile(
            r"\b(?:revok|disabl|deactivat|delet|expir|retir|invalidat|destroy|"
            r"suspend)\w*\b",
            re.IGNORECASE,
        )
        credential_subject = re.compile(
            r"\b(?:credential|authority|provider key|secret key)\b",
            re.IGNORECASE,
        )
        negative_or_deferred = re.compile(
            r"\b(?:never|do not|must not|may not|cannot|forbidden|only after|until)\b",
            re.IGNORECASE,
        )
        premature_instructions = []
        for gate, section in self.gates.items():
            if gate == 6:
                continue
            premature_instructions.extend(
                clause
                for clause in _clauses(section)
                if retirement_action.search(clause)
                and credential_subject.search(clause)
                and not negative_or_deferred.search(clause)
            )
        self.assertEqual([], premature_instructions)
        self.assertTrue(
            any(
                retirement_action.search(clause)
                and credential_subject.search(clause)
                and not negative_or_deferred.search(clause)
                for clause in _clauses(self.gates[6])
            )
        )
        self.assertIn("Gate 4 proves production uses the replacement", self.gates[6])
        self.assertIn("Gate 5 proves dev no longer", self.gates[6])

    def test_gate_1_inventories_all_authority_paths_semantically(self) -> None:
        required_phrases = (
            "ServiceAccount",
            "Role",
            "RoleBinding",
            "ClusterRole",
            "ClusterRoleBinding",
            "subject names, verbs, resources, and resourceNames only",
            "AppProject",
            "repo-server/CMP mounted Secret names",
            "workflow permissions",
            "environment protection metadata",
            "broker capability names",
            "GitHub App/repository access metadata",
            "zero Stripe network contact",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, self.normalized_gates[1])

    def test_each_mutating_gate_names_its_separate_authorizations(self) -> None:
        required_by_gate = {
            2: (
                "encrypted-source write",
                "PR-publication authorization",
                "Neither implies the other",
            ),
            3: (
                "provider credential-creation authorization",
                "permits provider creation only",
                "no secret-management",
                "Do not transfer it to secret management until Gate 4",
            ),
            4: (
                "provider-to-secret-manager handoff",
                "encrypted-source write",
                "production Secret mutation",
                "GitOps merge",
                "Argo reconciliation",
                "workload restart/rollout",
                "deployment",
                "independent provider verification",
                "rollback",
                "No authorization in this gate implies another",
            ),
            5: (
                "dev encrypted-source write",
                "dev Secret mutation",
                "GitOps merge",
                "Argo reconciliation",
                "workload restart/rollout",
                "deployment",
                "classifier execution",
                "rollback",
                "No authorization in this gate implies another",
            ),
            6: (
                "provider revocation",
                "post-revocation provider verification",
                "ledger/receipt update",
                "No authorization in this gate implies another",
            ),
        }
        for gate, phrases in required_by_gate.items():
            for phrase in phrases:
                self.assertIn(phrase, self.normalized_gates[gate])

    def test_runbook_requires_owned_workload_and_namespace_isolation(self) -> None:
        normalized_runbook = " ".join(self.runbook.split())
        required_phrases = (
            "Incident/operator owner",
            "Independent verifier/reviewer",
            "shared Argo CD instance and shared SOPS recipient are acceptable",
            "Secret references are namespace-local",
            "dev workload identities must have no production Secret-read",
            "cannot get or list Secrets and cannot create Pods",
            "separate authorization gates",
            "merge as causally sufficient to begin deployment",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, normalized_runbook)

        self.assertIn(
            "dev workload ServiceAccount cannot read or create paths to "
            "production Secrets",
            self.normalized_gates[2],
        )
        self.assertIn(
            "dev uses the production-runtime validation path and will not "
            "start with payment credentials intentionally absent",
            normalized_runbook,
        )

    def test_gitops_heads_are_content_isolated_and_authorize_every_application(self) -> None:
        required_by_gate = {
            2: (
                "separate production-content-only and dev-content-only deployable heads",
                "source-revision impact matrix",
                "every Argo Application and Secret Application",
                "including Applications with an unchanged render",
            ),
            4: (
                "production-content-only",
                "Any dev rendered-content change is a stop condition",
                "reconciliation authorization for every Application in the impact matrix",
                "exact reviewed head SHA, Git tree object, and immutable impact-matrix reference",
                "post-merge `main` tree is identical to the reviewed Git tree object",
            ),
            5: (
                "dev-content-only",
                "Any production rendered-content change is a stop condition",
                "reconciliation authorization for every Application in the impact matrix",
                "exact reviewed head SHA, Git tree object, and immutable impact-matrix reference",
                "post-merge `main` tree is identical to the reviewed Git tree object",
            ),
        }
        for gate, phrases in required_by_gate.items():
            for phrase in phrases:
                self.assertIn(phrase, self.normalized_gates[gate])

    def test_runbook_requires_value_free_receipts_and_zero_financial_smokes(self) -> None:
        normalized_runbook = " ".join(self.runbook.split())
        required_phrases = (
            "Never print, paste, hash, fingerprint, suffix, screenshot, attach, or commit",
            "Make zero Stripe network contact",
            "Do not create a charge, refund, PaymentIntent, SetupIntent, Checkout Session",
            "no credential value, fragment, hash, fingerprint",
            "non-live",
            "intentionally-absent",
            "restricted-live",
            "unknown",
            "unclassified",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, normalized_runbook)

        forbidden_commands = (
            "printenv",
            "kubectl exec",
            "sops --decrypt",
            "sops -d ",
            "kubectl get secret",
        )
        for command in forbidden_commands:
            self.assertNotIn(command, self.runbook)

    def test_ces_845_controls_remain_out_of_scope(self) -> None:
        introduction = _section(
            self.runbook,
            "# Citrus payment credential isolation and rotation",
            "## Absolute safety rules",
        )
        target_contract = _section(
            self.runbook,
            "## Target isolation contract",
            "## Ordered execution gates",
        )
        self.assertIn("does not implement", introduction)
        self.assertIn("CES-845", introduction)
        self.assertIn("are not implemented", target_contract)

        gate_instructions = " ".join(self.normalized_gates.values())
        forbidden_artifacts = (
            "NetworkPolicy",
            "CiliumNetworkPolicy",
            "iptables",
            "nftables",
            "egress proxy",
            "settings.py",
            "startup hook",
        )
        for artifact in forbidden_artifacts:
            self.assertNotIn(artifact, gate_instructions)
        implementation_instruction = re.compile(
            r"\b(?:implement|add|create|apply|configure|deploy)\b[^.]{0,80}"
            r"\b(?:startup|egress|networkpolicy|iptables|nftables)\b",
            re.IGNORECASE,
        )
        self.assertIsNone(implementation_instruction.search(gate_instructions))

    def test_receipt_schema_captures_authority_isolation_and_rollout(self) -> None:
        authorization_fields = _schema_fields(
            self.runbook,
            "### Authorization record",
            "### Access and isolation record",
        )
        access_fields = _schema_fields(
            self.runbook,
            "### Access and isolation record",
            "### Rollout and verification record",
        )
        rollout_fields = _schema_fields(
            self.runbook,
            "### Rollout and verification record",
            "Allowed final dev classifications",
        )

        self.assertTrue(
            {
                "gate",
                "mutation_scope",
                "authorization_reference",
                "target_environment",
                "target_system",
                "target_resource",
                "reviewed_source_revision",
                "reviewed_tree_digest",
                "impact_matrix_reference",
                "authorized_by",
                "authorized_at",
                "expires_at",
            }.issubset(authorization_fields)
        )
        self.assertTrue(
            {
                "secret_source",
                "secret_object",
                "secret_key_name",
                "credential_role",
                "classification",
                "source_owner",
                "rotation_owner",
                "created_or_rotated_at",
                "supersession_status",
                "access_policy_reference",
                "isolation_result",
                "reviewer",
            }.issubset(access_fields)
        )
        self.assertTrue(
            {
                "source_sha",
                "gitops_revision",
                "reviewed_source_revision",
                "reviewed_tree_digest",
                "impact_matrix_reference",
                "merge_result_revision",
                "tree_equivalence_result",
                "argo_application",
                "argo_revision",
                "argo_sync_status",
                "argo_health_status",
                "workload_kind",
                "workload_name",
                "workload_revision",
                "container_image",
                "container_image_digest",
                "projection_path",
                "desired_replicas",
                "ready_replicas",
                "classifier_version",
                "verification_method_class",
                "verification_result",
                "supersession_status",
                "rollback_revision",
                "reviewer",
            }.issubset(rollout_fields)
        )

        self.assertTrue(
            authorization_fields.issubset(SAFE_AUTHORIZATION_FIELDS),
            authorization_fields - SAFE_AUTHORIZATION_FIELDS,
        )
        self.assertTrue(
            access_fields.issubset(SAFE_ACCESS_FIELDS),
            access_fields - SAFE_ACCESS_FIELDS,
        )
        self.assertTrue(
            rollout_fields.issubset(SAFE_ROLLOUT_FIELDS),
            rollout_fields - SAFE_ROLLOUT_FIELDS,
        )


if __name__ == "__main__":
    unittest.main()
