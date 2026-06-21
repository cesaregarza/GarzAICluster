from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "postgres-restore.md"
DOCS_README = REPO_ROOT / "docs" / "README.md"
ROOT_README = REPO_ROOT / "README.md"
VALUES = REPO_ROOT / "apps" / "agent-control-plane" / "values.yaml"
APPLICATION = REPO_ROOT / "argocd" / "applications" / "agent-control-plane.yaml"
SECRETS_APPLICATION = (
    REPO_ROOT / "argocd" / "applications" / "agent-control-plane-secrets.yaml"
)
RUNTIME_SECRET = REPO_ROOT / "secrets" / "agent-control-plane" / "runtime-secret.enc.yaml"

YAML_PARSER = YAML(typ="safe")


class PostgresRestoreRunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runbook = RUNBOOK.read_text(encoding="utf-8")
        cls.normalized = _normalized(cls.runbook)
        cls.values = _load_yaml(VALUES)
        cls.application = _load_yaml(APPLICATION)
        cls.secrets_application = _load_yaml(SECRETS_APPLICATION)
        cls.runtime_secret = _load_yaml(RUNTIME_SECRET)

    def test_runbook_is_discoverable_from_readmes(self) -> None:
        self.assertIn(
            "docs/runbooks/postgres-restore.md",
            ROOT_README.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "runbooks/postgres-restore.md",
            DOCS_README.read_text(encoding="utf-8"),
        )

    def test_runbook_records_live_cluster_posture_and_manifest_refs(self) -> None:
        image = self.values["image"]
        image_ref = f"{image['repository']}:{image['tag']}"
        hostname = self.values["ingress"]["hosts"][0]
        runtime_secret_name = self.values["global"]["runtimeSecretName"]

        self.assertIn("db-postgresql-nyc3-xscraper", self.runbook)
        self.assertIn("3adbbc39-dd07-4c0c-b172-081a247810d6", self.runbook)
        self.assertIn("PostgreSQL 16", self.runbook)
        self.assertIn("nyc3", self.runbook)
        self.assertIn("db-amd-1vcpu-2gb", self.runbook)
        self.assertIn("2026-06-17", self.runbook)
        self.assertIn("2026-06-24T02:32:18Z", self.runbook)

        self.assertIn(hostname, self.runbook)
        self.assertIn(runtime_secret_name, self.runbook)
        self.assertIn(image_ref, self.runbook)
        self.assertIn("apps/agent-control-plane/values.yaml", self.runbook)
        self.assertIn("argocd/applications/agent-control-plane.yaml", self.runbook)
        self.assertIn(
            "argocd/applications/agent-control-plane-secrets.yaml",
            self.runbook,
        )
        self.assertIn(
            self.application["metadata"]["name"],
            self.runbook,
        )
        self.assertIn(
            self.secrets_application["metadata"]["name"],
            self.runbook,
        )

        secret_keys = set(self.runtime_secret["stringData"])
        self.assertIn("AGENT_PLATFORM_DATABASE_URL", secret_keys)
        self.assertIn("AGENT_PLATFORM_READONLY_SQL_DATABASE_URL", secret_keys)
        self.assertIn("AGENT_PLATFORM_DATABASE_URL", self.runbook)
        self.assertIn("AGENT_PLATFORM_READONLY_SQL_DATABASE_URL", self.runbook)
        self.assertIn("GarzAICluster", self.runbook)
        self.assertNotIn("SplatTopConfig", self.runbook)

    def test_runbook_preserves_restore_commands_and_recovery_targets(self) -> None:
        required_phrases = (
            "latest available DigitalOcean PITR point inside the seven-day native recovery window",
            "within 4 hours",
            "replicaCount: 1",
            "no API PodDisruptionBudget",
            "doctl databases fork",
            "--restore-from-cluster-id",
            "--restore-from-timestamp",
            "sops secrets/agent-control-plane/runtime-secret.enc.yaml",
            "argocd app sync agent-control-plane-secrets",
            "argocd app sync agent-control-plane",
            "kubectl -n agent-control-plane scale deploy/agent-control-plane --replicas=0",
            "kubectl -n agent-control-plane scale deploy/agent-control-plane-git-deliverer --replicas=0",
            "envFrom:",
            "secretRef:",
            "mandate-postgres-schema-check",
            "curl -fsS https://agent-control-plane.garz.ai/readyz",
            "curl -fsS https://agent-control-plane.garz.ai/healthz",
            "mandate.deploy.smoke",
            "audit hash chain",
            "do not claim cryptographic continuity across manual row-level recovery",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, self.normalized)

    def test_runbook_does_not_claim_operator_drill_completed(self) -> None:
        self.assertIn(
            "Live scratch-restore drill status: pending operator approval",
            self.runbook,
        )
        self.assertIn("No scratch resource created", self.runbook)
        self.assertIn("Cost-bearing restore drills", self.runbook)
        self.assertIn("must not create, fork, delete, or repoint", self.normalized)


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"YAML mapping expected: {path}")
    return loaded


def _normalized(value: str) -> str:
    return " ".join(value.split())
