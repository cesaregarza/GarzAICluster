from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mandate_verifier_window as window


class VerifierWindowTests(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(
            pause_verifier=True,
            apply=True,
            application=["agent-workloads"],
            poll_interval=1,
            kubectl="kubectl",
        )
        self.receipt = {"run_id": "owner-a"}
        self.saved = []
        self.current = {
            "metadata": {
                "uid": "cron-a",
                "resourceVersion": "1",
                "annotations": {"existing": "preserved"},
            },
            "spec": {"suspend": False},
        }
        self.patches = []
        self.drain_error = None
        self.context = mock.patch.object(window.VerifierWindow, "command", self.command)
        self.context.start()
        self.addCleanup(self.context.stop)
        self.no_active = mock.patch.object(window.train, "ensure_no_active_verify_job")
        self.jobs = self.no_active.start()
        self.addCleanup(self.no_active.stop)

    def command(self, *args):
        if args[0] == "get":
            return json.dumps(self.current)
        operations = json.loads(args[args.index("-p") + 1])
        candidate = copy.deepcopy(self.current)
        for operation in operations:
            parts = [
                part.replace("~1", "/") for part in operation["path"].split("/")[1:]
            ]
            parent = candidate
            for part in parts[:-1]:
                parent = parent[part]
            key = parts[-1]
            if operation["op"] == "test":
                if parent.get(key) != operation["value"]:
                    raise window.argo.ArgoCoreError("JSON Patch test failed")
            elif operation["op"] == "remove":
                del parent[key]
            else:
                parent[key] = operation["value"]
        self.current = candidate
        self.patches.append(operations)
        return "cronjob"

    def save(self, args, receipt):
        self.saved.append(copy.deepcopy(receipt))

    def run_window(self):
        return window.verifier_window(self.args, "kubeconfig", self.receipt, self.save)

    def test_success_pauses_before_work_and_restores_original_schedule(self):
        with self.run_window():
            self.assertTrue(self.current["spec"]["suspend"])
            self.assertEqual(
                self.current["metadata"]["annotations"][window.OWNER], "owner-a"
            )
            self.jobs.assert_called_once()
        self.assertFalse(self.current["spec"]["suspend"])
        self.assertEqual(
            self.current["metadata"]["annotations"], {"existing": "preserved"}
        )
        self.assertEqual(self.receipt["verifier_window"]["state"], "resumed")

    def test_failure_and_interrupt_both_restore_schedule(self):
        for error in (window.argo.ArgoCoreError("failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)), self.run_window():
                    raise error
                self.assertFalse(self.current["spec"]["suspend"])

    def test_preexisting_pause_is_never_resumed(self):
        self.current["spec"]["suspend"] = True
        with (
            self.assertRaisesRegex(window.argo.ArgoCoreError, "already paused"),
            self.run_window(),
        ):
            self.fail("must not deploy")
        self.assertTrue(self.current["spec"]["suspend"])
        self.assertEqual(self.patches, [])

    def test_replacement_or_new_owner_is_never_modified_on_cleanup(self):
        for key, value in (("uid", "cron-b"), ("owner", "owner-b")):
            self.current = {
                "metadata": {
                    "uid": "cron-a",
                    "resourceVersion": "1",
                    "annotations": {"existing": "preserved"},
                },
                "spec": {"suspend": False},
            }
            self.patches = []
            with self.subTest(key=key):
                with (
                    self.assertRaisesRegex(
                        window.argo.ArgoCoreError, "ownership changed"
                    ),
                    self.run_window(),
                ):
                    if key == "uid":
                        self.current["metadata"]["uid"] = value
                    else:
                        self.current["metadata"]["annotations"][window.OWNER] = value
                self.assertTrue(self.current["spec"]["suspend"])
                self.assertEqual(len(self.patches), 1)
                self.assertEqual(
                    self.receipt["verifier_window"]["state"], "resume-failed"
                )

    def test_drain_waits_only_for_known_overlap(self):
        self.jobs.side_effect = [
            window.argo.ArgoCoreError(
                "stage=mandate-verify reason=nonterminal-job-overlap jobs=existing"
            ),
            None,
        ]
        with mock.patch.object(window.time, "sleep") as sleep, self.run_window():
            self.assertEqual(self.jobs.call_count, 2)
            sleep.assert_called_once_with(1)

    def test_drain_timeout_restores_schedule_without_starting_work(self):
        self.jobs.side_effect = window.argo.ArgoCoreError(
            "stage=mandate-verify reason=nonterminal-job-overlap jobs=existing"
        )
        with (
            mock.patch.object(window.time, "monotonic", side_effect=[0, 511]),
            self.assertRaisesRegex(window.argo.ArgoCoreError, "deadline exceeded"),
            self.run_window(),
        ):
            self.fail("must not deploy")
        self.assertFalse(self.current["spec"]["suspend"])

    def test_unexpected_drain_error_is_not_retried(self):
        self.jobs.side_effect = window.argo.ArgoCoreError("API unavailable")
        with (
            self.assertRaisesRegex(window.argo.ArgoCoreError, "API unavailable"),
            self.run_window(),
        ):
            self.fail("must not deploy")
        self.jobs.assert_called_once()
        self.assertFalse(self.current["spec"]["suspend"])

    def test_dry_run_and_core_sync_cannot_request_a_pause(self):
        for apply, apps in (
            (False, ["agent-workloads"]),
            (True, ["agent-control-plane"]),
        ):
            self.args.apply, self.args.application = apply, apps
            with (
                self.assertRaisesRegex(window.argo.ArgoCoreError, "without Core sync"),
                self.run_window(),
            ):
                self.fail("must not deploy")
        self.assertEqual(self.patches, [])

    def test_disabled_window_never_reads_or_mutates_cronjob(self):
        self.args.pause_verifier = False
        with mock.patch.object(window.VerifierWindow, "command") as command:
            with self.run_window():
                pass
            command.assert_not_called()

    def test_lost_pause_response_still_restores_owned_pause(self):
        original_patch = window.VerifierWindow.patch
        calls = []

        def lose_response(instance, operations):
            original_patch(instance, operations)
            calls.append(True)
            if len(calls) == 1:
                raise window.subprocess.TimeoutExpired("kubectl", 30)

        with (
            mock.patch.object(window.VerifierWindow, "patch", lose_response),
            self.assertRaises(window.subprocess.TimeoutExpired),
            self.run_window(),
        ):
            self.fail("must not deploy")
        self.assertFalse(self.current["spec"]["suspend"])

    def test_failed_acquisition_does_not_resume_unowned_cronjob(self):
        with (
            mock.patch.object(
                window.VerifierWindow,
                "patch",
                side_effect=window.argo.ArgoCoreError("conflict"),
            ),
            self.assertRaisesRegex(window.argo.ArgoCoreError, "conflict"),
            self.run_window(),
        ):
            self.fail("must not deploy")
        self.assertFalse(self.current["spec"]["suspend"])
        self.assertEqual(self.receipt["verifier_window"]["state"], "not-acquired")

    def test_sigterm_restores_schedule_and_original_handler(self):
        previous = window.signal.getsignal(window.signal.SIGTERM)
        with (
            self.assertRaisesRegex(window.argo.ArgoCoreError, "interrupted by signal"),
            self.run_window(),
        ):
            window.signal.getsignal(window.signal.SIGTERM)(window.signal.SIGTERM, None)
        self.assertFalse(self.current["spec"]["suspend"])
        self.assertEqual(window.signal.getsignal(window.signal.SIGTERM), previous)

    def expired_window(self):
        self.current["spec"]["suspend"] = True
        self.current["metadata"]["annotations"].update(
            {
                window.OWNER: "stale-owner",
                window.EXPIRES: "2020-01-01T00:00:00+00:00",
                window.UID: "cron-a",
            }
        )

    def test_expired_owned_window_is_reclaimed_before_new_pause(self):
        self.expired_window()
        with self.run_window():
            self.assertEqual(
                self.current["metadata"]["annotations"][window.OWNER], "owner-a"
            )
            self.assertEqual(
                self.receipt["reclaimed_verifier_window"]["owner"], "stale-owner"
            )
        self.assertFalse(self.current["spec"]["suspend"])
        self.assertEqual(
            self.current["metadata"]["annotations"], {"existing": "preserved"}
        )

    def test_expired_replacement_and_nonexpired_windows_are_not_reclaimed(self):
        for key, value in (
            (window.UID, "other-cron"),
            (window.EXPIRES, "2999-01-01T00:00:00+00:00"),
            (window.EXPIRES, "invalid"),
        ):
            self.expired_window()
            self.current["metadata"]["annotations"][key] = value
            with (
                self.subTest(key=key, value=value),
                self.assertRaises(window.argo.ArgoCoreError),
                self.run_window(),
            ):
                self.fail("must not deploy")
            self.assertEqual(self.patches, [])
            self.assertTrue(self.current["spec"]["suspend"])

    def test_total_window_deadline_restores_schedule(self):
        with (
            self.assertRaisesRegex(window.argo.ArgoCoreError, "interrupted"),
            self.run_window(),
        ):
            self.assertGreater(window.signal.getitimer(window.signal.ITIMER_REAL)[0], 0)
            window.signal.getsignal(window.signal.SIGALRM)(window.signal.SIGALRM, None)
        self.assertFalse(self.current["spec"]["suspend"])
        self.assertEqual(window.signal.getitimer(window.signal.ITIMER_REAL), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
