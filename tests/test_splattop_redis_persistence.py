from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm" / "splattop"
YAML_PARSER = YAML(typ="safe")
SCRIPT = ROOT / "scripts" / "splattop_redis_snapshot_move.py"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("splattop_redis_snapshot_move", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(shutil.which("helm"), "helm is required for chart render tests")
class SplatTopRedisPersistenceTests(unittest.TestCase):
    @staticmethod
    def _render(*extra: str) -> list[dict[str, Any]]:
        command = ["helm", "template", "splattop-prod", str(CHART), "--namespace", "default"]
        command.extend(extra)
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        return [doc for doc in YAML_PARSER.load_all(result.stdout) if isinstance(doc, dict) and doc]

    @staticmethod
    def _redis(documents: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        deployment = next(doc for doc in documents if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == "splattop-prod-redis")
        pvc = next((doc for doc in documents if doc.get("kind") == "PersistentVolumeClaim"), None)
        return deployment, pvc

    def test_default_render_remains_ephemeral(self) -> None:
        deployment, pvc = self._redis(self._render())
        pod = deployment["spec"]["template"]["spec"]
        self.assertIsNone(pvc)
        self.assertNotIn("strategy", deployment["spec"])
        self.assertNotIn("volumes", pod)
        self.assertNotIn("volumeMounts", pod["containers"][0])
        self.assertNotIn("securityContext", pod)

    def test_production_is_single_writer_retained_and_nonroot(self) -> None:
        deployment, pvc = self._redis(self._render("-f", str(CHART / "values-prod.yaml")))
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        self.assertEqual(pvc["metadata"]["name"], "splattop-prod-redis-data")
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(pvc["spec"]["storageClassName"], "do-block-storage-retain")
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "1Gi")
        self.assertEqual(pvc["metadata"]["annotations"], {
            "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
            "helm.sh/resource-policy": "keep",
        })
        self.assertEqual(container["volumeMounts"], [{"name": "redis-data", "mountPath": "/data"}])
        self.assertEqual(pod["volumes"], [{"name": "redis-data", "persistentVolumeClaim": {"claimName": "splattop-prod-redis-data"}}])
        self.assertEqual(container["securityContext"]["runAsUser"], 999)
        self.assertEqual(container["securityContext"]["runAsGroup"], 999)
        self.assertTrue(container["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["fsGroup"], 999)
        self.assertEqual(container["readinessProbe"]["exec"]["command"], [
            "/bin/sh", "-ec", 'test "$(redis-cli --raw ping)" = PONG'
        ])

    def test_persistent_redis_rejects_multiple_replicas(self) -> None:
        result = subprocess.run(
            ["helm", "template", "splattop-prod", str(CHART), "-f", str(CHART / "values-prod.yaml"), "--set", "redis.replicas=2"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("redis.persistence.enabled requires redis.replicas", result.stderr)


class SplatTopRedisMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_migration_module()

    def test_source_uid_and_ephemeral_guard_fail_closed(self) -> None:
        deployment = {"metadata": {"uid": "new"}, "spec": {"replicas": 1, "template": {"spec": {}}}}
        with self.assertRaises(self.module.MigrationError):
            self.module.validate_source_deployment(deployment, expected_uid="reviewed")
        deployment["metadata"]["uid"] = "reviewed"
        deployment["spec"]["template"]["spec"]["volumes"] = [{"persistentVolumeClaim": {"claimName": "wrong"}}]
        with self.assertRaises(self.module.MigrationError):
            self.module.validate_source_deployment(deployment, expected_uid="reviewed")

    def test_destination_claim_and_helper_are_bounded(self) -> None:
        pvc = {"metadata": {"uid": "pvc-1"}, "status": {"phase": "Bound"}, "spec": {"storageClassName": "do-block-storage-retain", "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}}
        self.assertEqual(self.module.validate_target_pvc(pvc, expected_uid="pvc-1")["pvc_uid"], "pvc-1")
        helper = self.module.helper_manifest(pod_name="seed", pvc_name="target", node_name="node-1")
        self.assertEqual(helper["spec"]["containers"][0]["image"], "redis:7.2.4")
        self.assertEqual(helper["spec"]["containers"][0]["volumeMounts"][0]["mountPath"], "/data")
        self.assertTrue(helper["spec"]["securityContext"]["runAsNonRoot"])
        self.assertFalse(helper["spec"]["automountServiceAccountToken"])
        self.assertEqual(helper["spec"]["nodeSelector"], {"kubernetes.io/hostname": "node-1"})

    def test_preflight_rejects_source_node_hostname_mismatch(self) -> None:
        deployment = {
            "metadata": {"uid": "deployment-1"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "redis"}},
                "template": {"spec": {}},
            },
        }
        pvc = {
            "metadata": {"uid": "pvc-1"},
            "status": {"phase": "Bound"},
            "spec": {
                "storageClassName": "do-block-storage-retain",
                "accessModes": ["ReadWriteOnce"],
            },
        }
        pod = {
            "metadata": {"name": "redis-1", "uid": "pod-1"},
            "spec": {"nodeName": "node-1"},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }

        class NodeKubectl:
            def get_json(self, kind: str, name: str) -> dict[str, Any]:
                if kind == "deployment":
                    return deployment
                if kind == "pvc":
                    return pvc
                if kind == "node" and name == "node-1":
                    return {"metadata": {"labels": {"kubernetes.io/hostname": "other-node"}}}
                raise AssertionError((kind, name))

            def run(self, command: list[str], **_: Any) -> bytes:
                self_command = command
                if self_command[:2] == ["get", "pods"]:
                    return json.dumps({"items": [pod]}).encode()
                raise AssertionError(command)

        with mock.patch.object(self.module, "_redis", side_effect=["PONG", "appendonly\nno"]):
            with self.assertRaisesRegex(self.module.MigrationError, "hostname"):
                self.module._preflight(
                    NodeKubectl(), deployment_name="redis", pvc_name="redis-data",
                    expected_source_uid="deployment-1", expected_pvc_uid="pvc-1",
                )

    def test_seed_requires_immutable_pvc_and_source_pod_ids(self) -> None:
        args = self.module.build_parser().parse_args([
            "seed", "--context", "ctx", "--namespace", "default",
            "--deployment", "redis", "--pvc", "redis-data",
            "--expected-source-uid", "deployment-1", "--receipt", "/tmp/receipt",
        ])
        with self.assertRaisesRegex(self.module.MigrationError, "expected-pvc-uid"):
            self.module._validate_seed_args(args)
        args.expected_pvc_uid = "pvc-1"
        with self.assertRaisesRegex(self.module.MigrationError, "source-pod-uid"):
            self.module._validate_seed_args(args)

    def test_existing_helper_create_failure_is_never_deleted(self) -> None:
        module = self.module

        class ExistingHelperKubectl:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def run(self, command: list[str], **_: Any) -> bytes:
                self.calls.append(command)
                if command[:2] == ["create", "-f"]:
                    raise module.MigrationError("already exists")
                return b""

        args = self._seed_args()
        fake = ExistingHelperKubectl()
        with mock.patch.object(self.module, "_preflight", return_value=self._preflight_result()):
            with self.assertRaises(self.module.MigrationError):
                self.module.seed(args, fake)
        self.assertFalse(any(command and command[0] == "delete" for command in fake.calls))

    def test_partial_copy_failure_deletes_owned_helper_and_unpauses(self) -> None:
        fake = self._FakeKubectl(self.module)
        args = self._seed_args()
        with mock.patch.object(self.module, "_preflight", return_value=self._preflight_result()), \
             mock.patch.object(self.module, "_wait_helper"), \
             mock.patch.object(self.module, "_redis", side_effect=["OK", "OK", "OK"]), \
             mock.patch.object(self.module, "_stat_size", return_value=1), \
             mock.patch.object(self.module, "_stream_rdb", side_effect=self.module.MigrationError("copy failed")):
            with self.assertRaisesRegex(self.module.MigrationError, "copy failed"):
                self.module.seed(args, fake)
        self.assertTrue(any(command and command[0] == "delete" for command in fake.calls))
        self.assertEqual(fake.helper_uid_reads, 2)

    def test_checksum_failure_deletes_owned_helper_and_unpauses(self) -> None:
        fake = self._FakeKubectl(self.module)
        args = self._seed_args()
        with mock.patch.object(self.module, "_preflight", return_value=self._preflight_result()), \
             mock.patch.object(self.module, "_wait_helper"), \
             mock.patch.object(self.module, "_redis", side_effect=["OK", "OK", "OK"]), \
             mock.patch.object(self.module, "_stat_size", return_value=1), \
             mock.patch.object(self.module, "_stream_rdb", return_value=(1, "a" * 64)), \
             mock.patch.object(self.module, "_sha256", side_effect=["a" * 64, "b" * 64]):
            with self.assertRaisesRegex(self.module.MigrationError, "checksum"):
                self.module.seed(args, fake)
        self.assertTrue(any(command and command[0] == "delete" for command in fake.calls))

    def test_seed_rejects_unbounded_or_unsafe_timers(self) -> None:
        args = self._seed_args()
        args.max_bytes = self.module.MAX_MAX_BYTES + 1
        with self.assertRaisesRegex(self.module.MigrationError, "max-bytes"):
            self.module._validate_seed_args(args)
        args.max_bytes = self.module.DEFAULT_MAX_BYTES
        args.pause_timeout_ms = self.module.MIN_PAUSE_TIMEOUT_MS - 1
        with self.assertRaisesRegex(self.module.MigrationError, "pause-timeout"):
            self.module._validate_seed_args(args)
        args.pause_timeout_ms = self.module.MIN_PAUSE_TIMEOUT_MS
        args.cutover_grace_seconds = self.module.MIN_CUTOVER_GRACE_SECONDS
        with self.assertRaisesRegex(self.module.MigrationError, "must exceed"):
            self.module._validate_seed_args(args)

    def test_receipt_is_owner_only_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipt.json"
            self.module._write_receipt(receipt, {"phase": "awaiting-cutover"})
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(receipt.parent.stat().st_mode & 0o777, 0o700)
            with self.assertRaisesRegex(self.module.MigrationError, "already exists"):
                self.module._write_receipt(receipt, {"phase": "replacement"})

    def test_stream_timeout_uses_real_slow_consumer_without_hanging(self) -> None:
        class LocalKubectl:
            def popen(self, command: list[str], **kwargs: Any):
                if command[1] == "source":
                    script = "import sys; sys.stdout.buffer.write(b'x' * (8 * 1024 * 1024))"
                else:
                    script = "import sys,time; [time.sleep(0.25) for _ in iter(lambda: sys.stdin.buffer.read(65536), b'')]"
                return subprocess.Popen(
                    [sys.executable, "-c", script],
                    stdin=kwargs.get("stdin", subprocess.DEVNULL),
                    stdout=kwargs.get("stdout", subprocess.DEVNULL),
                    stderr=subprocess.DEVNULL,
                )

        with self.assertRaisesRegex(self.module.MigrationError, "timeout"):
            self.module._stream_rdb(
                LocalKubectl(), source_pod="source", helper_pod="helper",
                max_bytes=16 * 1024 * 1024, timeout=1,
            )

    def test_stream_success_reports_bounded_bytes_and_hash(self) -> None:
        class LocalKubectl:
            def popen(self, command: list[str], **kwargs: Any):
                if command[1] == "source":
                    script = "import sys; sys.stdout.buffer.write(b'x' * (128 * 1024))"
                else:
                    script = "import sys; [chunk for chunk in iter(lambda: sys.stdin.buffer.read(65536), b'')]"
                return subprocess.Popen(
                    [sys.executable, "-c", script],
                    stdin=kwargs.get("stdin", subprocess.DEVNULL),
                    stdout=kwargs.get("stdout", subprocess.DEVNULL),
                    stderr=subprocess.DEVNULL,
                )

        size, checksum = self.module._stream_rdb(
            LocalKubectl(), source_pod="source", helper_pod="helper",
            max_bytes=512 * 1024, timeout=5,
        )
        self.assertEqual(size, 128 * 1024)
        self.assertEqual(checksum, hashlib.sha256(b"x" * size).hexdigest())

    def test_stream_early_target_exit_fails_closed(self) -> None:
        class LocalKubectl:
            def popen(self, command: list[str], **kwargs: Any):
                if command[1] == "source":
                    script = "import sys; sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024))"
                else:
                    script = "import sys; sys.stdin.buffer.read(1)"
                return subprocess.Popen(
                    [sys.executable, "-c", script],
                    stdin=kwargs.get("stdin", subprocess.DEVNULL),
                    stdout=kwargs.get("stdout", subprocess.DEVNULL),
                    stderr=subprocess.DEVNULL,
                )

        with self.assertRaises(self.module.MigrationError):
            self.module._stream_rdb(
                LocalKubectl(), source_pod="source", helper_pod="helper",
                max_bytes=4 * 1024 * 1024, timeout=5,
            )

    def test_expired_pause_attempts_unpause_and_emits_no_receipt(self) -> None:
        fake = self._FakeKubectl(self.module)
        args = self._seed_args()
        with tempfile.TemporaryDirectory() as directory:
            args.receipt = str(Path(directory) / "receipt.json")
            with mock.patch.object(self.module, "_preflight", return_value=self._preflight_result()), \
                 mock.patch.object(self.module, "_wait_helper"), \
                 mock.patch.object(self.module, "_redis", side_effect=["OK", "OK", "OK"]), \
                 mock.patch.object(self.module, "_stat_size", return_value=1), \
                 mock.patch.object(self.module, "_stream_rdb", return_value=(1, "a" * 64)), \
                 mock.patch.object(self.module, "_sha256", return_value="a" * 64), \
                 mock.patch.object(self.module.time, "time", side_effect=[1000.0, 2000.0]):
                with self.assertRaisesRegex(self.module.MigrationError, "pause expired"):
                    self.module.seed(args, fake)
            self.assertFalse(Path(args.receipt).exists())
        self.assertTrue(any(command and command[0] == "delete" for command in fake.calls))

    def _seed_args(self):
        args = self.module.build_parser().parse_args([
            "seed", "--context", "ctx", "--namespace", "default",
            "--deployment", "redis", "--pvc", "redis-data",
            "--expected-source-uid", "deployment-1", "--expected-pvc-uid", "pvc-1",
            "--expected-source-pod-uid", "pod-1", "--receipt", "/tmp/unused-receipt",
        ])
        args.started_at = 1.0
        return args

    def _preflight_result(self):
        return (
            {"deployment_uid": "deployment-1", "pod_uid": "pod-1", "pod_name": "redis-1", "node_name": "node-1"},
            {"pvc_uid": "pvc-1"},
            {},
        )

    class _FakeKubectl:
        def __init__(self, module) -> None:
            self.module = module
            self.calls: list[list[str]] = []
            self.helper_uid_reads = 0

        def run(self, command: list[str], **_: Any) -> bytes:
            self.calls.append(command)
            return b""

        def get_json(self, kind: str, name: str) -> dict[str, Any]:
            if kind == "pod" and name == "splattop-redis-migration-seed":
                self.helper_uid_reads += 1
                return {"metadata": {"uid": "helper-1"}, "status": {"phase": "Running"}}
            raise AssertionError((kind, name))

        def get_json_optional(self, kind: str, name: str):
            return self.get_json(kind, name)

    def test_cli_help_exposes_read_only_dry_run(self) -> None:
        result = subprocess.run(["python3", str(SCRIPT), "--help"], check=True, capture_output=True, text=True)
        self.assertIn("--dry-run", result.stdout)
        self.assertIn("--pause-timeout-ms", result.stdout)
        self.assertIn("--cutover-grace-seconds", result.stdout)
        self.assertIn("preflight", result.stdout)


if __name__ == "__main__":
    unittest.main()
