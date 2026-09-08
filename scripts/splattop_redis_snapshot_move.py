#!/usr/bin/env python3
"""Seed a pre-created SplatTop Redis PVC from the still-ephemeral broker.

The command has two intentionally separate phases. ``preflight`` is read-only;
``seed`` creates one temporary PVC helper, pauses writes, saves one RDB, and
leaves the source paused after a successful checksum-verified seed. Cutover is
an explicit Argo/Helm operation described in the Redis runbook.

The helper never prints Redis command output or RDB bytes. A receipt contains
only resource identities, byte counts, checksums, and phase timestamps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence


DEFAULT_HELPER_POD = "splattop-redis-migration-seed"
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_TIMEOUT = 45
DEFAULT_PAUSE_TIMEOUT_MS = 600_000
DEFAULT_CUTOVER_GRACE_SECONDS = 120
MAX_MAX_BYTES = 512 * 1024 * 1024
MIN_PAUSE_TIMEOUT_MS = 10_000
MAX_PAUSE_TIMEOUT_MS = 900_000
MIN_CUTOVER_GRACE_SECONDS = 30
MAX_CUTOVER_GRACE_SECONDS = 600
MIN_TRANSFER_TIMEOUT_SECONDS = 30
MAX_TRANSFER_TIMEOUT_SECONDS = 900
STREAM_CHUNK_BYTES = 1024 * 1024


class MigrationError(RuntimeError):
    """A migration precondition or integrity check failed."""


class Kubectl:
    """Small kubectl adapter that keeps command output out of terminal logs."""

    def __init__(self, *, binary: str, context: str, namespace: str) -> None:
        self.binary = binary
        self.context = context
        self.namespace = namespace

    def command(self, args: Sequence[str]) -> list[str]:
        return [
            self.binary,
            "--context",
            self.context,
            "-n",
            self.namespace,
            *args,
        ]

    def popen(
        self,
        args: Sequence[str],
        *,
        stdin: Any = subprocess.DEVNULL,
        stdout: Any = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            self.command(args),
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
        )

    def run(
        self,
        args: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> bytes:
        result = subprocess.run(
            self.command(args),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
        if result.returncode != 0:
            # stderr can contain command output or values. Keep the error
            # bounded and diagnostic without echoing it to the operator.
            raise MigrationError(
                f"kubectl phase command failed with status {result.returncode}"
            )
        return result.stdout

    def get_json(self, kind: str, name: str) -> dict[str, Any]:
        try:
            raw = self.run(["get", kind, name, "-o", "json"])
        except MigrationError as exc:
            raise MigrationError(f"unable to read {kind}/{name}") from exc
        try:
            document = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MigrationError(f"{kind}/{name} did not return JSON") from exc
        if not isinstance(document, dict):
            raise MigrationError(f"{kind}/{name} JSON object is required")
        return document

    def get_json_optional(self, kind: str, name: str) -> dict[str, Any] | None:
        try:
            return self.get_json(kind, name)
        except MigrationError:
            return None

def _metadata_uid(resource: dict[str, Any], description: str) -> str:
    uid = resource.get("metadata", {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise MigrationError(f"{description} has no UID")
    return uid


def validate_source_deployment(
    deployment: dict[str, Any], *, expected_uid: str
) -> dict[str, Any]:
    """Reject a deployment that is not the known ephemeral source."""

    actual_uid = _metadata_uid(deployment, "source deployment")
    if actual_uid != expected_uid:
        raise MigrationError("source deployment UID does not match the reviewed source")
    replicas = deployment.get("spec", {}).get("replicas", 1)
    if replicas != 1:
        raise MigrationError("source deployment must have exactly one replica")
    pod_spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
    if any("persistentVolumeClaim" in volume for volume in pod_spec.get("volumes", [])):
        raise MigrationError("source deployment already mounts a PVC; refusing to seed")
    return {
        "deployment_uid": actual_uid,
        "selector": deployment.get("spec", {}).get("selector", {}).get("matchLabels", {}),
    }


def validate_target_pvc(
    pvc: dict[str, Any], *, expected_uid: str | None = None
) -> dict[str, Any]:
    """Require a bound, retained, single-writer destination claim."""

    uid = _metadata_uid(pvc, "destination PVC")
    if expected_uid and uid != expected_uid:
        raise MigrationError("destination PVC UID does not match the reviewed claim")
    spec = pvc.get("spec", {})
    if pvc.get("status", {}).get("phase") != "Bound":
        raise MigrationError("destination PVC must be Bound before seeding")
    if spec.get("storageClassName") != "do-block-storage-retain":
        raise MigrationError("destination PVC must use do-block-storage-retain")
    if spec.get("accessModes") != ["ReadWriteOnce"]:
        raise MigrationError("destination PVC must be exactly ReadWriteOnce")
    return {"pvc_uid": uid, "storage": spec.get("resources", {}).get("requests", {}).get("storage")}


def helper_manifest(*, pod_name: str, pvc_name: str, node_name: str) -> dict[str, Any]:
    """Return the bounded, non-root pod used only while copying the RDB."""

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "labels": {"migration": "splattop-redis-persistence"},
        },
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": {"kubernetes.io/hostname": node_name},
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsUser": 999,
                "runAsGroup": 999,
                "runAsNonRoot": True,
                "fsGroup": 999,
                "fsGroupChangePolicy": "OnRootMismatch",
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "seed",
                    "image": "redis:7.2.4",
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sh", "-c", "trap 'exit 0' TERM; sleep 3600 & wait"],
                    "resources": {
                        "requests": {"cpu": "1m", "memory": "16Mi"},
                        "limits": {"cpu": "100m", "memory": "64Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                }
            ],
            "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}}
            ],
        },
    }


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True) + "\n").encode()


def _command_output(kubectl: Kubectl, pod: str, command: Sequence[str]) -> str:
    return kubectl.run(["exec", pod, "--", *command]).decode("utf-8", "replace").strip()


def _redis(kubectl: Kubectl, pod: str, *command: str) -> str:
    output = _command_output(kubectl, pod, ("redis-cli", "--raw", *command))
    if output.startswith(("ERR ", "MISCONF ", "BUSY ", "NOAUTH ")):
        raise MigrationError(f"Redis rejected {command[0]}")
    return output


def _stat_size(kubectl: Kubectl, pod: str, path: str = "/data/dump.rdb") -> int:
    output = _command_output(kubectl, pod, ("stat", "-c", "%s", path))
    try:
        return int(output)
    except ValueError as exc:
        raise MigrationError("Redis snapshot size was not numeric") from exc


def _sha256(kubectl: Kubectl, pod: str, path: str = "/data/dump.rdb") -> str:
    output = _command_output(kubectl, pod, ("sha256sum", path))
    digest = output.split()[0] if output else ""
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise MigrationError("Redis snapshot checksum was invalid")
    return digest


def _validate_seed_args(args: argparse.Namespace) -> None:
    if not 1 <= args.max_bytes <= MAX_MAX_BYTES:
        raise MigrationError("--max-bytes must be between 1 and 512 MiB")
    if not MIN_PAUSE_TIMEOUT_MS <= args.pause_timeout_ms <= MAX_PAUSE_TIMEOUT_MS:
        raise MigrationError("--pause-timeout-ms must be between 10000 and 900000")
    if not MIN_CUTOVER_GRACE_SECONDS <= args.cutover_grace_seconds <= MAX_CUTOVER_GRACE_SECONDS:
        raise MigrationError("--cutover-grace-seconds must be between 30 and 600")
    if not MIN_TRANSFER_TIMEOUT_SECONDS <= args.transfer_timeout_seconds <= MAX_TRANSFER_TIMEOUT_SECONDS:
        raise MigrationError("--transfer-timeout-seconds must be between 30 and 900")
    if args.cutover_grace_seconds * 1000 >= args.pause_timeout_ms:
        raise MigrationError("pause timeout must exceed the cutover grace period")
    if not args.expected_pvc_uid:
        raise MigrationError("seed requires --expected-pvc-uid")
    if not args.expected_source_pod_uid:
        raise MigrationError("seed requires --expected-source-pod-uid")


def _stream_rdb(
    kubectl: Kubectl,
    *,
    source_pod: str,
    helper_pod: str,
    max_bytes: int,
    timeout: int,
) -> tuple[int, str]:
    """Copy source RDB to helper stdin without retaining it in process memory."""

    source = kubectl.popen(["exec", source_pod, "--", "cat", "/data/dump.rdb"], stdout=subprocess.PIPE)
    target = kubectl.popen(
        [
            "exec",
            "-i",
            helper_pod,
            "--",
            "sh",
            "-c",
            "umask 077; cat >> /data/dump.rdb.partial",
        ],
        stdin=subprocess.PIPE,
    )
    assert source.stdout is not None and target.stdin is not None
    source_fd = source.stdout.fileno()
    target_fd = target.stdin.fileno()
    os.set_blocking(source_fd, False)
    os.set_blocking(target_fd, False)
    selector = selectors.DefaultSelector()
    pending = b""
    total = 0
    digest = hashlib.sha256()
    deadline = time.monotonic() + timeout
    source_done = False
    source_registered = False
    target_registered = False

    def update_interests() -> None:
        nonlocal source_registered, target_registered
        source_wanted = not source_done and len(pending) < STREAM_CHUNK_BYTES
        target_wanted = bool(pending)
        if source_wanted and not source_registered:
            selector.register(source.stdout, selectors.EVENT_READ, "source")
            source_registered = True
        elif not source_wanted and source_registered:
            selector.unregister(source.stdout)
            source_registered = False
        if target_wanted and not target_registered:
            selector.register(target.stdin, selectors.EVENT_WRITE, "target")
            target_registered = True
        elif not target_wanted and target_registered:
            selector.unregister(target.stdin)
            target_registered = False

    try:
        while not source_done or pending:
            update_interests()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MigrationError("RDB stream exceeded its transfer timeout")
            events = selector.select(remaining)
            if not events:
                raise MigrationError("RDB stream exceeded its transfer timeout")
            for key, mask in events:
                if key.data == "source" and mask & selectors.EVENT_READ:
                    try:
                        chunk = os.read(source_fd, STREAM_CHUNK_BYTES - len(pending))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        source_done = True
                        selector.unregister(source.stdout)
                        source_registered = False
                        source.stdout.close()
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise MigrationError("RDB exceeds the reviewed transfer bound")
                    digest.update(chunk)
                    pending += chunk
                if key.data == "target" and mask & selectors.EVENT_WRITE and pending:
                    try:
                        written = os.write(target_fd, pending)
                    except BlockingIOError:
                        continue
                    if written <= 0:
                        raise MigrationError("RDB stream made no target pipe progress")
                    pending = pending[written:]
                    update_interests()
            if source_done and not pending:
                if target_registered:
                    selector.unregister(target.stdin)
                    target_registered = False
                target.stdin.close()
        target.wait(timeout=max(1, int(deadline - time.monotonic())))
        source.wait(timeout=max(1, int(deadline - time.monotonic())))
        if source.returncode != 0 or target.returncode != 0:
            raise MigrationError("RDB stream command failed")
        return total, digest.hexdigest()
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired) as exc:
        raise MigrationError("RDB stream failed before completion") from exc
    finally:
        selector.close()
        for process in (source, target):
            if process.poll() is None:
                process.kill()
                process.wait()
        for stream in (source.stdout, target.stdin):
            if stream is not None and not stream.closed:
                stream.close()


def _delete_owned_helper(kubectl: Kubectl, pod_name: str, expected_uid: str) -> None:
    current = kubectl.get_json("pod", pod_name)
    current_uid = _metadata_uid(current, "migration helper pod")
    if current_uid != expected_uid:
        raise MigrationError("migration helper UID changed; refusing deletion")
    kubectl.run(["delete", "pod", pod_name, "--wait=true", "--timeout=30s"])


def _delete_owned_helper_if_present(kubectl: Kubectl, pod_name: str, expected_uid: str | None) -> None:
    if not expected_uid:
        return
    current = kubectl.get_json_optional("pod", pod_name)
    if current is None:
        return
    current_uid = _metadata_uid(current, "migration helper pod")
    if current_uid != expected_uid:
        raise MigrationError("migration helper UID changed; refusing deletion")
    kubectl.run(["delete", "pod", pod_name, "--wait=true", "--timeout=30s"])


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        parent_stat = os.lstat(path.parent)
    except OSError as exc:
        raise MigrationError("receipt directory could not be inspected") from exc
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid():
        raise MigrationError("receipt directory must be an owner directory")
    os.chmod(path.parent, 0o700)
    mode = stat.S_IMODE(path.parent.stat().st_mode)
    if mode & 0o077:
        raise MigrationError("receipt directory must be owner-only")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise MigrationError("receipt already exists; inspect it before any retry") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise


def _source_pod(kubectl: Kubectl, deployment: dict[str, Any], deployment_name: str) -> dict[str, Any]:
    selector = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
    if not selector:
        raise MigrationError("source deployment selector is empty")
    selector_text = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    raw = kubectl.run(["get", "pods", "-l", selector_text, "-o", "json"])
    try:
        items = json.loads(raw).get("items", [])
    except (TypeError, json.JSONDecodeError) as exc:
        raise MigrationError("source pod inventory was not JSON") from exc
    live = [pod for pod in items if not pod.get("metadata", {}).get("deletionTimestamp")]
    if len(live) != 1:
        raise MigrationError(f"source deployment {deployment_name} must have exactly one live pod")
    pod = live[0]
    if pod.get("status", {}).get("phase") != "Running":
        raise MigrationError("source Redis pod must be Running")
    ready = any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in pod.get("status", {}).get("conditions", [])
    )
    if not ready:
        raise MigrationError("source Redis pod must be Ready")
    node_name = pod.get("spec", {}).get("nodeName")
    if not isinstance(node_name, str) or not node_name:
        raise MigrationError("source Redis pod has no assigned node")
    return pod


def _preflight(
    kubectl: Kubectl,
    *,
    deployment_name: str,
    pvc_name: str,
    expected_source_uid: str,
    expected_pvc_uid: str | None,
    expected_source_pod_uid: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    deployment = kubectl.get_json("deployment", deployment_name)
    source = validate_source_deployment(deployment, expected_uid=expected_source_uid)
    pvc = kubectl.get_json("pvc", pvc_name)
    destination = validate_target_pvc(pvc, expected_uid=expected_pvc_uid)
    pod = _source_pod(kubectl, deployment, deployment_name)
    pod_name = pod.get("metadata", {}).get("name")
    if not pod_name:
        raise MigrationError("source Redis pod has no name")
    pod_uid = _metadata_uid(pod, "source Redis pod")
    if expected_source_pod_uid and pod_uid != expected_source_pod_uid:
        raise MigrationError("source Redis pod UID does not match the reviewed source")
    node_name = pod.get("spec", {}).get("nodeName")
    if not isinstance(node_name, str) or not node_name:
        raise MigrationError("source Redis pod has no assigned node")
    node = kubectl.get_json("node", node_name)
    node_hostname = node.get("metadata", {}).get("labels", {}).get("kubernetes.io/hostname")
    if node_hostname != node_name:
        raise MigrationError("source node kubernetes.io/hostname label does not match its name")
    if _redis(kubectl, pod_name, "PING") != "PONG":
        raise MigrationError("source Redis did not answer PING")
    if _redis(kubectl, pod_name, "CONFIG", "GET", "appendonly") != "appendonly\nno":
        raise MigrationError("RDB migration requires appendonly=no")
    source["pod_name"] = pod_name
    source["pod_uid"] = pod_uid
    source["node_name"] = node_name
    return source, destination, pod


def _wait_helper(kubectl: Kubectl, pod_name: str, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = kubectl.get_json("pod", pod_name)
        if pod.get("status", {}).get("phase") == "Running":
            return
        if pod.get("status", {}).get("phase") in {"Failed", "Succeeded"}:
            raise MigrationError("migration helper exited before becoming Ready")
        time.sleep(2)
    raise MigrationError("migration helper did not become Ready before timeout")


def seed(args: argparse.Namespace, kubectl: Kubectl) -> dict[str, Any]:
    _validate_seed_args(args)
    if not args.receipt:
        raise MigrationError("seed requires --receipt on owner-only local storage")
    receipt_path = Path(args.receipt)
    if receipt_path.exists():
        raise MigrationError("receipt already exists; inspect it before any retry")
    source, destination, _ = _preflight(
        kubectl,
        deployment_name=args.deployment,
        pvc_name=args.pvc,
        expected_source_uid=args.expected_source_uid,
        expected_pvc_uid=args.expected_pvc_uid,
        expected_source_pod_uid=args.expected_source_pod_uid,
    )
    helper_name = args.helper_pod
    if args.dry_run:
        return {
            "phase": "dry-run-preflight",
            "namespace": args.namespace,
            "deployment": args.deployment,
            "pvc": args.pvc,
            "source_pod": source["pod_name"],
            "source_node": source["node_name"],
            "pvc_uid": destination["pvc_uid"],
            "max_bytes": args.max_bytes,
        }

    paused = False
    pause_expires_at: float | None = None
    created_helper_uid: str | None = None
    phase = "create-helper"
    try:
        # create, rather than apply, so a stale helper can never be adopted.
        created_document = json.loads(
            kubectl.run(
                ["create", "-f", "-", "-o", "json"],
                stdin=_json_bytes(
                    helper_manifest(
                        pod_name=helper_name,
                        pvc_name=args.pvc,
                        node_name=source["node_name"],
                    )
                ),
            )
        )
        if not isinstance(created_document, dict):
            raise MigrationError("helper create did not return a JSON object")
        created_helper_uid = _metadata_uid(created_document, "created migration helper pod")
        _wait_helper(kubectl, helper_name)
        # Existing final or partial files may be prior/partial seeds. Never
        # overwrite either, and create the partial file exclusively.
        kubectl.run(
            [
                "exec",
                helper_name,
                "--",
                "sh",
                "-c",
                "set -eu; test ! -e /data/dump.rdb; test ! -e /data/dump.rdb.partial; (set -C; : > /data/dump.rdb.partial); chmod 600 /data/dump.rdb.partial",
            ]
        )
        phase = "revalidate-before-pause"
        source, destination, _ = _preflight(
            kubectl,
            deployment_name=args.deployment,
            pvc_name=args.pvc,
            expected_source_uid=args.expected_source_uid,
            expected_pvc_uid=args.expected_pvc_uid,
            expected_source_pod_uid=args.expected_source_pod_uid,
        )
        phase = "pause-source"
        if _redis(
            kubectl,
            source["pod_name"],
            "CLIENT",
            "PAUSE",
            str(args.pause_timeout_ms),
            "WRITE",
        ) != "OK":
            raise MigrationError("source Redis rejected the write pause")
        paused = True
        pause_expires_at = time.time() + (args.pause_timeout_ms / 1000)
        phase = "save-rdb"
        if _redis(kubectl, source["pod_name"], "SAVE") != "OK":
            raise MigrationError("source Redis rejected SAVE")
        size = _stat_size(kubectl, source["pod_name"])
        if not 0 < size <= args.max_bytes:
            raise MigrationError(f"RDB is outside the reviewed {args.max_bytes}-byte bound")
        phase = "stream-rdb"
        streamed_size, source_checksum = _stream_rdb(
            kubectl,
            source_pod=source["pod_name"],
            helper_pod=helper_name,
            max_bytes=args.max_bytes,
            timeout=args.transfer_timeout_seconds,
        )
        if streamed_size != size:
            raise MigrationError("RDB stream length did not match stat size")
        if source_checksum != _sha256(kubectl, source["pod_name"]):
            raise MigrationError("source RDB checksum changed during transfer")
        if pause_expires_at is None or time.time() >= pause_expires_at:
            raise MigrationError("Redis write pause expired before the RDB copy")
        phase = "verify-rdb"
        target_size = _stat_size(kubectl, helper_name, "/data/dump.rdb.partial")
        target_checksum = _sha256(kubectl, helper_name, "/data/dump.rdb.partial")
        if target_size != size or target_checksum != source_checksum:
            raise MigrationError("destination RDB checksum or size did not match source")
        kubectl.run(["exec", helper_name, "--", "redis-check-rdb", "/data/dump.rdb.partial"])
        phase = "revalidate-before-cutover"
        _preflight(
            kubectl,
            deployment_name=args.deployment,
            pvc_name=args.pvc,
            expected_source_uid=args.expected_source_uid,
            expected_pvc_uid=args.expected_pvc_uid,
            expected_source_pod_uid=args.expected_source_pod_uid,
        )
        if pause_expires_at is None or time.time() + args.cutover_grace_seconds >= pause_expires_at:
            raise MigrationError("insufficient Redis pause time remains for reviewed cutover")
        phase = "awaiting-cutover"
        kubectl.run(
            [
                "exec",
                helper_name,
                "--",
                "sh",
                "-c",
                "set -eu; test ! -e /data/dump.rdb; mv -- /data/dump.rdb.partial /data/dump.rdb",
            ]
        )
        if _stat_size(kubectl, helper_name) != size or _sha256(kubectl, helper_name) != source_checksum:
            raise MigrationError("destination RDB changed during atomic rename")
        receipt = {
            "phase": phase,
            "namespace": args.namespace,
            "deployment": args.deployment,
            "deployment_uid": source["deployment_uid"],
            "source_pod_uid": source["pod_uid"],
            "source_node": source["node_name"],
            "pvc": args.pvc,
            "pvc_uid": destination["pvc_uid"],
            "bytes": size,
            "sha256": source_checksum,
            "pause_timeout_ms": args.pause_timeout_ms,
            "pause_expires_at": pause_expires_at,
            "cutover_grace_seconds": args.cutover_grace_seconds,
            "transfer_timeout_seconds": args.transfer_timeout_seconds,
            "started_at": args.started_at,
            "seeded_at": time.time(),
        }
        _delete_owned_helper(kubectl, helper_name, created_helper_uid)
        if pause_expires_at is None or time.time() + args.cutover_grace_seconds >= pause_expires_at:
            raise MigrationError("insufficient Redis pause time remains after helper cleanup")
        _write_receipt(receipt_path, receipt)
        # The source stays paused until the operator verifies the cutover. This
        # is the rollback guard: the old writer must not race the new one.
        return receipt
    except BaseException as exc:
        try:
            _delete_owned_helper_if_present(kubectl, helper_name, created_helper_uid)
        except Exception:
            pass
        if paused:
            try:
                _redis(kubectl, source["pod_name"], "CLIENT", "UNPAUSE")
            except Exception as unpause_exc:
                raise MigrationError(
                    f"migration failed in phase {phase}; source unpause requires operator action"
                ) from unpause_exc
        if isinstance(exc, MigrationError):
            raise MigrationError(f"migration failed in phase {phase}: {exc}") from exc
        raise MigrationError(f"migration failed in phase {phase}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "seed"))
    parser.add_argument("--context", required=True, help="exact kubectl context")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True, help="ephemeral source Deployment")
    parser.add_argument("--pvc", required=True, help="pre-created retained destination PVC")
    parser.add_argument("--expected-source-uid", required=True)
    parser.add_argument("--expected-pvc-uid")
    parser.add_argument("--expected-source-pod-uid")
    parser.add_argument("--helper-pod", default=DEFAULT_HELPER_POD)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--pause-timeout-ms",
        type=int,
        default=DEFAULT_PAUSE_TIMEOUT_MS,
        help="Redis CLIENT PAUSE WRITE duration (default: 600000)",
    )
    parser.add_argument(
        "--cutover-grace-seconds",
        type=int,
        default=DEFAULT_CUTOVER_GRACE_SECONDS,
        help="minimum pause time reserved for the explicit cutover (default: 120)",
    )
    parser.add_argument(
        "--transfer-timeout-seconds",
        type=int,
        default=300,
        help="bounded source-to-PVC stream timeout (default: 300)",
    )
    parser.add_argument("--receipt", help="owner-only receipt path (required for seed)")
    parser.add_argument("--dry-run", action="store_true", help="read preflight and print plan; never create or copy")
    parser.add_argument("--kubectl", default="kubectl")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.started_at = time.time()
    kubectl = Kubectl(binary=args.kubectl, context=args.context, namespace=args.namespace)
    try:
        if args.action == "preflight":
            source, destination, _ = _preflight(
                kubectl,
                deployment_name=args.deployment,
                pvc_name=args.pvc,
                expected_source_uid=args.expected_source_uid,
                expected_pvc_uid=args.expected_pvc_uid,
                expected_source_pod_uid=args.expected_source_pod_uid,
            )
            result = {
                "phase": "preflight-ok",
                "namespace": args.namespace,
                "deployment": args.deployment,
                "deployment_uid": source["deployment_uid"],
                "source_pod_uid": source["pod_uid"],
                "source_node": source["node_name"],
                "pvc": args.pvc,
                "pvc_uid": destination["pvc_uid"],
            }
        else:
            result = seed(args, kubectl)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (MigrationError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
