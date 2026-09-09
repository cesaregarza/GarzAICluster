from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ruamel.yaml import YAML
from scripts import image_pr_automerge as subject

ROOT = Path(__file__).resolve().parents[1]
OLD = "a" * 40
NEW = "b" * 40
CONFIG = subject.load_policy(ROOT)
POLICY = CONFIG["policies"]["citrus-dev"]
EXPECTED_JOBS = {
    "python-contracts",
    "agent-workloads-identity-digest-drift",
    "agent-control-plane-deployed-registry-compat",
    "agent-control-plane-provider-digest-pins",
    "agent-control-plane-registry-overlay-render",
    "operator-manifests",
    "helm-and-kubeconform",
    "prometheus-rules",
}


def values(revision: str) -> dict[str, str]:
    return {
        "helm/citrus/values-dev.yaml": (
            f"image:\n  repository: registry.digitalocean.com/sendouq/citrus\n  tag: {revision}\n"
            f"recurringRuntime:\n  expectedSourceRevision: {revision}\nreplicas: 1\n"
        ),
        "helm/citrus/values-payment-dev.yaml": (
            f"directOrderPaymentSweep:\n  enabled: true\n  verifiedImageTag: {revision}\n"
        ),
    }


def pr(head: str) -> dict:
    repo = {"full_name": CONFIG["repository"]}
    return {
        "state": "open",
        "draft": False,
        "merged": False,
        "commits": 1,
        "head": {"repo": repo, "ref": "bot/update-citrus-dev-12345", "sha": head},
        "base": {"repo": repo, "ref": "main", "sha": OLD},
    }


class PolicyTests(unittest.TestCase):
    def test_current_policy_is_dev_only(self):
        self.assertEqual(set(CONFIG["policies"]), {"citrus-dev"})
        self.assertEqual(POLICY["sourceRepository"], "cesaregarza/Citrus")
        self.assertEqual(POLICY["sourceRef"], "refs/heads/dev")
        self.assertEqual(set(POLICY["files"]), set(values(OLD)))

    def test_forbidden_yaml(self):
        for text in ("x: 1\nx: 2", "x: &anchor 1\ny: *anchor", "x: !!str 1", "[]"):
            with self.subTest(text=text), self.assertRaises(subject.PolicyError):
                subject.yaml_values(text)

    def test_field_scope_and_scalar_types(self):
        before = subject.yaml_values(values(OLD)["helm/citrus/values-dev.yaml"])
        after = subject.yaml_values(values(NEW)["helm/citrus/values-dev.yaml"])
        fields = POLICY["files"]["helm/citrus/values-dev.yaml"]
        subject.validate_values(before, after, fields, NEW)
        for key, value in (
            ("replicas", 2),
            ("replicas", True),
            ("newSetting", "unexpected"),
        ):
            changed = copy.deepcopy(after)
            changed[key] = value
            with (
                self.subTest(key=key, value=value),
                self.assertRaises(subject.PolicyError),
            ):
                subject.validate_values(before, changed, fields, NEW)
        after["recurringRuntime"]["expectedSourceRevision"] = OLD
        with self.assertRaises(subject.PolicyError):
            subject.validate_values(before, after, fields, NEW)

    def test_pr_rejects_wrong_head_fork_base_and_state(self):
        subject.validate_pr(pr(NEW), CONFIG, POLICY, NEW)
        for mutate in (
            lambda p: p.update(draft=True),
            lambda p: p.update(state="closed"),
            lambda p: p.update(merged=True),
            lambda p: p["head"].update(sha=OLD),
            lambda p: p["head"].update(repo=None),
            lambda p: p["head"].update(repo={"full_name": "outsider/GarzAICluster"}),
            lambda p: p["head"].update(ref="bot/update-citrus-prod-12345"),
            lambda p: p["base"].update(ref="dev"),
        ):
            candidate = pr(NEW)
            mutate(candidate)
            with self.assertRaises(subject.PolicyError):
                subject.validate_pr(candidate, CONFIG, POLICY, NEW)

    def test_protection_requires_all_checks_with_actions_identity(self):
        repository = {"allow_auto_merge": True, "allow_squash_merge": True}
        protection = {
            "protected": True,
            "protection": {
                "required_status_checks": {
                    "enforcement_level": "everyone",
                    "checks": [
                        {"context": name, "app_id": subject.ACTIONS_APP}
                        for name in CONFIG["requiredChecks"]
                    ],
                },
            },
        }
        subject.validate_protection(repository, protection, CONFIG["requiredChecks"])
        for mutate in (
            lambda p: p.update(protected=False),
            lambda p: p["protection"]["required_status_checks"].update(
                enforcement_level="non_admins"
            ),
            lambda p: p["protection"]["required_status_checks"]["checks"].pop(),
            lambda p: p["protection"]["required_status_checks"]["checks"][0].update(
                app_id=None
            ),
        ):
            changed = copy.deepcopy(protection)
            mutate(changed)
            with self.assertRaises(subject.PolicyError):
                subject.validate_protection(
                    repository, changed, CONFIG["requiredChecks"]
                )

    def test_api_failure_identifies_endpoint_without_echoing_output(self):
        with patch.object(
            subject, "run", side_effect=subject.PolicyError("private diagnostic")
        ), self.assertRaises(subject.PolicyError) as raised:
            subject.api("repos/cesaregarza/GarzAICluster/branches/main")
        self.assertIn(
            "GET repos/cesaregarza/GarzAICluster/branches/main", str(raised.exception)
        )
        self.assertNotIn("private diagnostic", str(raised.exception))

    def test_non_candidate_event_never_fetches_or_merges(self):
        event = {"pull_request": pr(NEW)}
        event["pull_request"]["head"]["ref"] = "human/change"
        with patch.object(subject, "run") as command:
            self.assertEqual(
                subject.check_pr_event(ROOT, event)["status"], "not-an-image-pr"
            )
            command.assert_not_called()


class GitTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gaic-image-policy-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.assertNotEqual(self.root.parts[1], "mnt")
        self.git("init", "-q")
        self.git("config", "user.name", "Policy Test")
        self.git("config", "user.email", "policy-test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        for path, content in values(OLD).items():
            destination = self.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        (self.root / "automation").mkdir()
        (self.root / subject.POLICY_PATH).write_text(json.dumps(CONFIG))
        self.base = self.commit()
        for path, content in values(NEW).items():
            (self.root / path).write_text(content)
        self.head = self.commit()

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_real_commit_diff_is_eligible(self):
        self.assertEqual(
            subject.validate_diff(self.root, self.base, self.head, POLICY), NEW
        )

    def test_extra_path_and_mode_and_unrelated_setting_are_rejected(self):
        for kind in ("extra", "mode", "value", "symlink"):
            with self.subTest(kind=kind):
                self.git("reset", "--hard", self.head)
                self.git("clean", "-fd")
                target = self.root / "helm/citrus/values-dev.yaml"
                if kind == "extra":
                    (self.root / "unexpected.yaml").write_text("x: true\n")
                elif kind == "mode":
                    target.chmod(0o755)
                elif kind == "value":
                    target.write_text(
                        target.read_text().replace("replicas: 1", "replicas: 2")
                    )
                else:
                    target.unlink()
                    target.symlink_to("values-payment-dev.yaml")
                head = self.commit()
                with self.assertRaises(subject.PolicyError):
                    subject.validate_diff(self.root, self.base, head, POLICY)

    def test_comment_only_and_partial_release_files_are_rejected(self):
        for path, content in values(NEW).items():
            (self.root / path).write_text(content + "# cosmetic only\n")
        comment_head = self.commit()
        with self.assertRaises(subject.PolicyError):
            subject.validate_diff(self.root, self.head, comment_head, POLICY)
        self.git("reset", "--hard", self.base)
        (self.root / "helm/citrus/values-dev.yaml").write_text(
            values(NEW)["helm/citrus/values-dev.yaml"]
        )
        with self.assertRaises(subject.PolicyError):
            subject.validate_diff(self.root, self.base, self.commit(), POLICY)

    def test_missing_file_and_noop_are_rejected(self):
        with self.assertRaises(subject.PolicyError):
            subject.validate_diff(self.root, self.head, self.head, POLICY)
        (self.root / "helm/citrus/values-payment-dev.yaml").unlink()
        with self.assertRaises(subject.PolicyError):
            subject.validate_diff(self.root, self.base, self.commit(), POLICY)

    def args(self, apply=False):
        return argparse.Namespace(
            repo_root=self.root,
            policy="citrus-dev",
            pr_url="https://github.com/cesaregarza/GarzAICluster/pull/123",
            expected_head=self.head,
            source_repository="cesaregarza/Citrus",
            source_ref="refs/heads/dev",
            source_event="push",
            source_sha=NEW,
            build_result="success",
            apply=apply,
        )

    def test_read_only_default_and_exact_head_merge(self):
        candidate = pr(self.head)
        activated = dict(candidate, auto_merge={"merge_method": "squash"})
        real_run = subject.run
        commands = []

        def command(args):
            commands.append(args)
            return "" if args[:3] == ["gh", "pr", "merge"] else real_run(args)

        with (
            patch.object(subject, "api", return_value=candidate) as github,
            patch.object(subject, "validate_protection"),
            patch.object(subject, "run", side_effect=command),
        ):
            result = subject.enable(self.args())
            self.assertFalse(result["applied"])
            endpoints = [item.args[0] for item in github.call_args_list]
            self.assertIn("repos/cesaregarza/GarzAICluster/branches/main", endpoints)
            self.assertFalse(
                any(endpoint.endswith("/protection") for endpoint in endpoints)
            )
            self.assertFalse(
                any(args[:3] == ["gh", "pr", "merge"] for args in commands)
            )
        with (
            patch.object(
                subject, "api", side_effect=[candidate, {}, {}, candidate, activated]
            ),
            patch.object(subject, "validate_protection"),
            patch.object(subject, "run", side_effect=command),
        ):
            self.assertEqual(
                subject.enable(self.args(True))["status"], "auto-merge-enabled"
            )
        self.assertIn(
            [
                "gh",
                "pr",
                "merge",
                self.args().pr_url,
                "--repo",
                CONFIG["repository"],
                "--auto",
                "--squash",
                "--match-head-commit",
                self.head,
            ],
            commands,
        )

    def test_ineligible_build_and_stale_remote_head_never_mutate(self):
        for field, value in (
            ("source_ref", "refs/heads/main"),
            ("source_event", "workflow_dispatch"),
            ("build_result", "skipped"),
            ("source_repository", "outsider/Citrus"),
            ("expected_head", OLD),
            ("source_sha", OLD),
        ):
            with (
                self.subTest(field=field),
                patch.object(subject, "api", return_value=pr(self.head)),
                patch.object(subject, "validate_protection"),
            ):
                args = self.args(True)
                setattr(args, field, value)
                with self.assertRaises(subject.PolicyError):
                    subject.enable(args)
        with (
            patch.object(subject, "api", side_effect=[pr(self.head), {}, {}, pr(OLD)]),
            patch.object(subject, "validate_protection"),
            self.assertRaises(subject.PolicyError),
        ):
            subject.enable(self.args(True))


class WorkflowGateTests(unittest.TestCase):
    def setUp(self):
        self.yaml = YAML(typ="safe")
        self.ci = self.yaml.load((ROOT / ".github/workflows/ci.yaml").read_text())

    def test_aggregate_covers_every_validation_job(self):
        jobs = self.ci["jobs"]
        gate = jobs["config-ci-required"]
        self.assertEqual(set(gate["needs"]), EXPECTED_JOBS)
        self.assertEqual(set(gate["needs"]), set(jobs) - {"config-ci-required"})
        self.assertEqual(gate["if"], "always()")

    def test_aggregate_fails_for_unsuccessful_results(self):
        script = self.ci["jobs"]["config-ci-required"]["steps"][0]["run"]

        def execute(results):
            return subprocess.run(
                ["bash", "-c", script],
                env={**os.environ, "JOB_RESULTS": json.dumps(results)},
                capture_output=True,
                text=True,
                check=False,
            ).returncode

        green = {name: {"result": "success"} for name in EXPECTED_JOBS}
        self.assertEqual(execute(green), 0)
        for status in ("skipped", "cancelled", "failure", None):
            changed = copy.deepcopy(green)
            changed["python-contracts"]["result"] = status
            self.assertNotEqual(execute(changed), 0)
        self.assertNotEqual(execute({}), 0)
        partial = dict(green)
        partial.pop("python-contracts")
        self.assertNotEqual(execute(partial), 0)

    def test_security_scans_always_report_required_statuses(self):
        for file, check in (
            ("deny-plaintext-secrets.yaml", "plaintext-secrets"),
            ("no-latest.yaml", "immutable-image-tags"),
        ):
            workflow = self.yaml.load((ROOT / ".github/workflows" / file).read_text())
            self.assertNotIn("paths", workflow["on"]["pull_request"])
            self.assertIn("merge_group", workflow["on"])
            self.assertEqual(next(iter(workflow["jobs"].values()))["name"], check)

    def test_scope_guard_uses_target_event_and_base_code_only(self):
        workflow = self.yaml.load(
            (ROOT / ".github/workflows/image-pr-policy.yaml").read_text()
        )
        self.assertIn("pull_request_target", workflow["on"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        steps = workflow["jobs"]["image-pr-policy"]["steps"]
        self.assertEqual(
            steps[0]["with"]["ref"], "${{ github.event.pull_request.base.sha }}"
        )
        self.assertIs(steps[0]["with"]["persist-credentials"], False)
        self.assertTrue(all("secrets." not in str(step) for step in steps))
        self.assertTrue(all("head.sha" not in str(step) for step in steps))


if __name__ == "__main__":
    unittest.main()
