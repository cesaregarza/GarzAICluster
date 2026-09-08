from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "mandate_scoped_deploy", SCRIPTS / "mandate_scoped_deploy.py"
)
assert SPEC is not None and SPEC.loader is not None
SCOPED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCOPED)
SHA = "a" * 40
WORKERS = (
    "agent-workloads-secrets",
    SCOPED.train.OVERLAY_APPLICATION,
    "agent-workloads",
)


def ready_snapshot() -> SimpleNamespace:
    operation = SimpleNamespace(
        phase="Succeeded", revisions=(SHA,), started_at="start", finished_at="finish"
    )
    return SimpleNamespace(
        sync_status="Synced",
        health_status="Healthy",
        revisions=(SHA,),
        operation=operation,
    )


class ScopedDeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.args = argparse.Namespace(
            repo_root=Path(self.directory.name),
            confirm_sha=SHA,
            application=list(WORKERS),
            apply=True,
            pause_verifier=False,
            namespace="argocd",
            receipt_dir=Path(self.directory.name) / "receipt",
            git="git",
            kubectl="kubectl",
            argocd="argocd",
            operation_timeout=60,
            refresh_timeout=30,
            poll_interval=1,
        )
        self.receipt = {
            "config_sha": SHA,
            "run_id": "scoped-test-run",
            "status": "started",
            "before": {},
            "completed": [],
        }
        self.contracts = {
            name: SimpleNamespace(name=name, resolved_revisions=(SHA,), automated=False)
            for name in SCOPED.train.MANAGED_APPLICATIONS
        }
        self.calls: list[str] = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.guard = self.patch(SCOPED, "guard")
        self.read = self.patch(SCOPED, "read_snapshot", return_value=ready_snapshot())
        self.dry = self.patch(SCOPED, "dry_run")
        self.patch(
            SCOPED.train, "load_application_contracts", return_value=self.contracts
        )
        self.patch(
            SCOPED.train,
            "read_live_skill_bundle",
            return_value=SimpleNamespace(digest="skill-digest"),
        )
        self.patch(SCOPED.train, "preflight_mandate_verify")
        self.patch(SCOPED.train, "load_journey_contracts", return_value=("journey",))
        self.patch(SCOPED.train, "assert_full_hook_operation")
        self.reconcile = self.patch(
            SCOPED.train, "reconcile_application", side_effect=self.record_reconcile
        )
        self.verify = self.patch(
            SCOPED.train, "run_mandate_verify", return_value="fresh-verify-job"
        )

    def patch(self, owner: object, name: str, **kwargs: object) -> mock.Mock:
        return self.stack.enter_context(mock.patch.object(owner, name, **kwargs))

    def record_reconcile(self, contract: object, **kwargs: object) -> str:
        self.calls.append(contract.name)
        return "manual"

    def test_scope_rejects_root_duplicates_unknown_and_reversed_order(self) -> None:
        for names in (
            [],
            ["splattop-root"],
            ["unknown"],
            ["agent-workloads"] * 2,
            list(reversed(WORKERS)),
        ):
            with (
                self.subTest(names=names),
                self.assertRaises(SCOPED.argo.ArgoCoreError),
            ):
                SCOPED.selected_applications(names)
        self.assertEqual(SCOPED.selected_applications(list(WORKERS)), WORKERS)

    def test_core_scope_has_upstream_dependencies_without_downstream_workers(
        self,
    ) -> None:
        upstream = SCOPED.dependencies(("agent-control-plane",))
        self.assertIn(SCOPED.train.OVERLAY_APPLICATION, upstream)
        self.assertIn(SCOPED.train.SKILLS_APPLICATION, upstream)
        self.assertNotIn("agent-workloads", upstream)
        self.assertNotIn(SCOPED.train.ROOT_APPLICATION, upstream)

    def test_preflight_never_reconciles_or_creates_a_live_verification(self) -> None:
        self.args.apply = False
        SCOPED.execute(self.args, Path("kubeconfig"), self.receipt)
        self.assertEqual(self.receipt["status"], "preflight-passed")
        self.assertEqual(
            [call.args[0].name for call in self.dry.call_args_list], list(WORKERS)
        )
        self.reconcile.assert_not_called()
        self.verify.assert_not_called()

    def test_success_reconciles_only_requested_subset_and_always_verifies(self) -> None:
        SCOPED.execute(self.args, Path("kubeconfig"), self.receipt)
        self.assertEqual(self.calls, list(WORKERS))
        self.assertEqual(self.receipt["status"], "verified")
        self.assertEqual(self.receipt["verification_job"], "fresh-verify-job")
        self.verify.assert_called_once()
        self.assertEqual(
            [x["application"] for x in self.receipt["completed"]], list(WORKERS)
        )
        self.assertTrue(all(x["revisions"] == [SHA] for x in self.receipt["completed"]))
        self.assertNotIn(
            SCOPED.train.ROOT_APPLICATION,
            [call.args[0].name for call in self.read.call_args_list],
        )
        self.assertTrue(
            all(call.kwargs["force_sync"] for call in self.reconcile.call_args_list)
        )

    def test_expired_pause_recovers_before_dependency_readiness_is_checked(
        self,
    ) -> None:
        sequence = []
        self.patch(
            SCOPED,
            "recover_expired_window",
            side_effect=lambda *args: sequence.append("recover") or True,
        )
        self.patch(
            SCOPED.argo,
            "hard_refresh_application",
            side_effect=lambda *args, **kwargs: sequence.append("refresh"),
        )
        self.patch(
            SCOPED.argo,
            "poll_application_ready",
            side_effect=lambda *args, **kwargs: sequence.append("ready"),
        )
        self.read.side_effect = lambda *args: (
            sequence.append("read") or ready_snapshot()
        )
        SCOPED.execute(self.args, Path("kubeconfig"), self.receipt)
        self.assertEqual(sequence[:4], ["recover", "refresh", "ready", "read"])
        self.assertEqual(self.calls, list(WORKERS))

    def test_guard_failure_stops_later_stages_and_preserves_completed_receipt(
        self,
    ) -> None:
        def reject_late_drift(*_args: object) -> None:
            if self.calls:
                raise SCOPED.argo.ArgoCoreError("remote main moved")

        self.guard.side_effect = reject_late_drift
        with self.assertRaisesRegex(SCOPED.argo.ArgoCoreError, "remote main moved"):
            SCOPED.execute(self.args, Path("kubeconfig"), self.receipt)
        self.assertEqual(self.calls, [WORKERS[0]])
        self.verify.assert_not_called()
        saved = json.loads((self.args.receipt_dir / "scoped-deploy.json").read_text())
        self.assertEqual([x["application"] for x in saved["completed"]], [WORKERS[0]])

    def test_unhealthy_omitted_dependency_prevents_dry_runs_and_mutations(self) -> None:
        def snapshot(contract: object, *_args: object) -> SimpleNamespace:
            value = ready_snapshot()
            if contract.name == SCOPED.train.SKILLS_APPLICATION:
                value.health_status = "Degraded"
            return value

        self.read.side_effect = snapshot
        with self.assertRaisesRegex(SCOPED.argo.ArgoCoreError, "omitted dependency"):
            SCOPED.execute(self.args, Path("kubeconfig"), self.receipt)
        self.dry.assert_not_called()
        self.reconcile.assert_not_called()
        self.verify.assert_not_called()

    def test_failed_dry_run_prevents_all_mutations(self) -> None:
        self.dry.side_effect = SCOPED.argo.ArgoCoreError("dry run failed")
        with self.assertRaisesRegex(SCOPED.argo.ArgoCoreError, "dry run failed"):
            SCOPED.execute(self.args, Path("kubeconfig"), self.receipt)
        self.reconcile.assert_not_called()
        self.verify.assert_not_called()

    def test_main_closes_temporary_kubeconfig_after_failure(self) -> None:
        self.args.context = SCOPED.train.PRODUCTION_CONTEXT
        self.args.kubeconfig = Path("original-kubeconfig")
        self.args.repo_root = SCRIPTS.parent
        cleaned = []

        @contextmanager
        def temporary_config(*_args: object, **_kwargs: object):
            try:
                yield Path("temporary-kubeconfig")
            finally:
                cleaned.append(True)

        parser = mock.Mock()
        parser.parse_args.return_value = self.args
        self.patch(SCOPED, "build_parser", return_value=parser)
        self.patch(SCOPED.argo, "resolve_executable", side_effect=lambda value: value)
        self.patch(SCOPED.argo, "validate_argocd_version")
        self.patch(SCOPED.argo, "core_kubeconfig", side_effect=temporary_config)
        self.patch(
            SCOPED, "execute", side_effect=SCOPED.argo.ArgoCoreError("scope failed")
        )
        self.assertEqual(SCOPED.main(), 1)
        self.assertEqual(cleaned, [True])
        saved = json.loads((self.args.receipt_dir / "scoped-deploy.json").read_text())
        self.assertEqual(saved["status"], "failed")


class DryRunTests(unittest.TestCase):
    def test_dry_run_uses_branch_for_automated_app_and_exact_revisions_for_manual(
        self,
    ) -> None:
        args = SimpleNamespace(argocd="argocd", operation_timeout=60)
        for automated, expected in ((True, "main"), (False, SHA)):
            with self.subTest(automated=automated):
                contract = SCOPED.train.ApplicationContract(
                    name="example",
                    identity={"spec": {"source": {"targetRevision": "main"}}},
                    resolved_revisions=(SHA,),
                    automated=automated,
                )
                with mock.patch.object(
                    SCOPED.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as run:
                    SCOPED.dry_run(contract, args, Path("temporary-kubeconfig"))
                command = run.call_args.args[0]
                self.assertIn("--dry-run", command)
                self.assertEqual(command[command.index("--revision") + 1], expected)
                self.assertEqual(command[command.index("--strategy") + 1], "hook")
                self.assertNotIn("--resource", command)


if __name__ == "__main__":
    unittest.main()
