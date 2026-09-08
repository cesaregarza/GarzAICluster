from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "splattop_writer_control.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("splattop_writer_control", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WriterControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def setUp(self) -> None:
        self.fake = self.FakeKubectl(self.module)
        self.entries = [
            {"deployment": "splattop-prod-fastapi", "uid": "api-uid", "baseline_replicas": 2},
            {"deployment": "splattop-prod-celery-beat", "uid": "beat-uid", "baseline_replicas": 1},
            {"deployment": "splattop-prod-celery-worker", "uid": "worker-uid", "baseline_replicas": 1},
        ]

    def test_contract_requires_exact_worker_membership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps({"writers": self.entries, "worker_deployment": "missing"}))
            with self.assertRaisesRegex(self.module.WriterControlError, "contracted writer"):
                self.module._contract(path)

    def test_stop_rejects_uid_mismatch_before_quiesce(self) -> None:
        self.fake.deployments["splattop-prod-celery-worker"]["metadata"]["uid"] = "replacement-uid"
        with self.assertRaisesRegex(self.module.WriterControlError, "UID mismatch"):
            self.module.stop(self.fake, self.entries, "splattop-prod-celery-worker", b"quiesce", 5)
        self.assertFalse(any(call[0][0] == "exec" for call in self.fake.calls))

    def test_stop_failure_restores_patched_writers_and_worker_consumer(self) -> None:
        self.fake.fail_patch = "splattop-prod-celery-beat"
        with self.assertRaisesRegex(self.module.WriterControlError, "patch failed"):
            self.module.stop(self.fake, self.entries, "splattop-prod-celery-worker", b"quiesce", 5)
        self.assertEqual(
            {name: deployment["spec"]["replicas"] for name, deployment in self.fake.deployments.items()},
            {"splattop-prod-fastapi": 2, "splattop-prod-celery-beat": 1, "splattop-prod-celery-worker": 1},
        )
        exec_actions = [call[0][8] for call in self.fake.calls if call[0][0] == "exec"]
        self.assertEqual(exec_actions, ["quiesce", "resume"])

    def test_quiesce_failure_still_attempts_same_worker_resume(self) -> None:
        self.fake.fail_quiesce = True
        with self.assertRaises(self.module.WriterControlError):
            self.module.stop(self.fake, self.entries, "splattop-prod-celery-worker", b"quiesce", 5)
        exec_actions = [call[0][8] for call in self.fake.calls if call[0][0] == "exec"]
        self.assertEqual(exec_actions, ["quiesce", "resume"])

    def test_stop_reports_recovery_failure_instead_of_hiding_it(self) -> None:
        self.fake.fail_patch = "splattop-prod-celery-beat"
        self.fake.fail_restore = "splattop-prod-fastapi"
        with mock.patch.object(self.module, "_wait_baseline", side_effect=self.module.WriterControlError("ready timeout")):
            with self.assertRaisesRegex(self.module.WriterControlError, "recovery failed"):
                self.module.stop(self.fake, self.entries, "splattop-prod-celery-worker", b"quiesce", 5)

    def test_verify_stopped_counts_terminating_pods_as_writers(self) -> None:
        for pods in self.fake.pods.values():
            pods.append({"metadata": {"name": "terminating", "deletionTimestamp": "now"}})
        for deployment in self.fake.deployments.values():
            deployment["spec"]["replicas"] = 0
        with mock.patch.object(self.module.time, "monotonic", side_effect=[0, 0, 0, 2]), \
             mock.patch.object(self.module.time, "sleep"):
            with self.assertRaisesRegex(self.module.WriterControlError, "did not reach zero"):
                self.module.verify_stopped(self.fake, self.entries)

    class FakeKubectl:
        def __init__(self, module) -> None:
            self.module = module
            self.calls: list[tuple[list[str], bytes | None]] = []
            self.fail_patch: str | None = None
            self.fail_restore: str | None = None
            self.fail_quiesce = False
            self.deployments: dict[str, dict[str, Any]] = {
                "splattop-prod-fastapi": self._deployment("api-uid", 2, "api"),
                "splattop-prod-celery-beat": self._deployment("beat-uid", 1, "beat"),
                "splattop-prod-celery-worker": self._deployment("worker-uid", 1, "worker"),
            }
            self.pods = {
                name: [
                    {"metadata": {"name": f"{name}-pod", "uid": f"{name}-pod-uid"},
                    "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
                ]
                for name in self.deployments
            }
            self.saved_worker = dict(self.pods["splattop-prod-celery-worker"][0])

        @staticmethod
        def _deployment(uid: str, replicas: int, label: str) -> dict[str, Any]:
            return {"metadata": {"uid": uid}, "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": label}}}, "status": {"readyReplicas": replicas}}

        def json(self, args: list[str]) -> dict[str, Any]:
            if args[1] == "deployment":
                return self.deployments[args[2]]
            if args[1] == "pods":
                label = args[3].split("=", 1)[1]
                name = next(name for name, deployment in self.deployments.items() if deployment["spec"]["selector"]["matchLabels"]["app"] == label)
                return {"items": self.pods[name]}
            raise AssertionError(args)

        def run(self, args: list[str], *, stdin: bytes | None = None, timeout: int = 45) -> bytes:
            self.calls.append((args, stdin))
            if args[0] == "exec":
                if self.fail_quiesce and args[8] == "quiesce":
                    raise self.module.WriterControlError("quiesce failed")
                return b"ok"
            if args[0] == "patch":
                name = args[2]
                if name == self.fail_patch:
                    raise self.module.WriterControlError("patch failed")
                target = json.loads(args[-1])[2]["value"]
                if name == self.fail_restore and target > 0:
                    raise self.module.WriterControlError("restore patch failed")
                self.deployments[name]["spec"]["replicas"] = target
                self.deployments[name]["status"]["readyReplicas"] = target
                if name == "splattop-prod-celery-worker" and target == 1:
                    self.pods[name] = [self.saved_worker]
                else:
                    self.pods[name] = [
                        {"metadata": {"name": f"{name}-pod-{index}", "uid": f"{name}-pod-uid-{index}"}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
                        for index in range(target)
                    ]
                return b""
            raise AssertionError(args)


if __name__ == "__main__":
    unittest.main()
