"""Hold a scoped worker rollout between scheduled verification runs."""

from __future__ import annotations

import json
import signal
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import mandate_deploy_train as train

argo = train.argo
OWNER = "mandate.garz.ai/verifier-window"
OWNER_PATH = "/metadata/annotations/mandate.garz.ai~1verifier-window"
EXPIRES = OWNER + "-expires-at"
UID = OWNER + "-uid"
WINDOW_SECONDS = 1800
DELETE_TIMEOUT_SECONDS = train.VERIFY_SETTLEMENT_GRACE_SECONDS


class VerifierWindow:
    def __init__(self, args: Any, kubeconfig: Any, receipt: dict, save: Any):
        self.args, self.kubeconfig = args, kubeconfig
        self.receipt, self.save = receipt, save
        self.uid = ""

    def command(self, *args: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            [
                self.args.kubectl,
                "--kubeconfig",
                str(self.kubeconfig),
                "-n",
                train.VERIFY_NAMESPACE,
                *args,
            ],
            capture_output=True,
            text=True,
            input=input_text,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise argo.command_failure("verifier maintenance window", result)
        return result.stdout

    def read(self) -> dict:
        return json.loads(
            self.command("get", "cronjob", train.VERIFY_CRONJOB, "-o", "json")
        )

    def patch(self, operations: list[dict]) -> None:
        self.command(
            "patch",
            "cronjob",
            train.VERIFY_CRONJOB,
            "--type=json",
            "-p",
            json.dumps(operations),
            "-o",
            "name",
        )

    def record(self, state: str) -> None:
        self.receipt["verifier_window"] = {"uid": self.uid, "state": state}
        self.save(self.args, self.receipt)

    def reclaim_expired(self, current: dict) -> dict:
        metadata = current["metadata"]
        annotations = metadata.get("annotations") or {}
        if OWNER not in annotations:
            return current
        try:
            expires = datetime.fromisoformat(annotations[EXPIRES])
        except (KeyError, TypeError, ValueError) as error:
            raise argo.ArgoCoreError("owned verifier has no valid expiry") from error
        if expires.tzinfo is None or expires > datetime.now(UTC):
            raise argo.ArgoCoreError("verifier window has not expired")
        if annotations.get(UID) != metadata["uid"]:
            raise argo.ArgoCoreError("expired verifier UID changed; refusing reclaim")
        operations = [
            {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
            {"op": "test", "path": "/spec/suspend", "value": True},
        ]
        for key in (OWNER, EXPIRES, UID):
            path = "/metadata/annotations/" + key.replace("/", "~1")
            operations.append({"op": "test", "path": path, "value": annotations[key]})
            operations.append({"op": "remove", "path": path})
        operations.append({"op": "replace", "path": "/spec/suspend", "value": False})
        self.patch(operations)
        self.receipt["reclaimed_verifier_window"] = {
            "uid": metadata["uid"],
            "owner": annotations[OWNER],
            "expired_at": annotations[EXPIRES],
        }
        self.save(self.args, self.receipt)
        return self.read()

    def acquire(self) -> None:
        current = self.reclaim_expired(self.read())
        metadata, spec = current["metadata"], current["spec"]
        annotations = metadata.get("annotations") or {}
        if spec.get("suspend") is not False or OWNER in annotations:
            raise argo.ArgoCoreError("verifier is already paused or owned")
        self.uid = metadata["uid"]
        operations = [
            {"op": "test", "path": "/metadata/uid", "value": self.uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": metadata["resourceVersion"],
            },
            {"op": "test", "path": "/spec/suspend", "value": False},
        ]
        if not metadata.get("annotations"):
            operations.append(
                {"op": "add", "path": "/metadata/annotations", "value": {}}
            )
        operations += [
            {"op": "add", "path": OWNER_PATH, "value": self.receipt["run_id"]},
            {
                "op": "add",
                "path": OWNER_PATH + "-expires-at",
                "value": (
                    datetime.now(UTC) + timedelta(seconds=WINDOW_SECONDS)
                ).isoformat(),
            },
            {"op": "add", "path": OWNER_PATH + "-uid", "value": self.uid},
            {"op": "replace", "path": "/spec/suspend", "value": True},
        ]
        self.record("pause-requested")
        self.patch(operations)
        self.record("paused")

    def release(self) -> None:
        if not self.uid:
            return
        current = self.read()
        metadata = current["metadata"]
        owner = (metadata.get("annotations") or {}).get(OWNER)
        if (
            metadata["uid"] == self.uid
            and owner is None
            and current["spec"].get("suspend") is False
            and self.receipt["verifier_window"]["state"] == "pause-requested"
        ):
            self.record("not-acquired")
            return
        if metadata["uid"] != self.uid or owner != self.receipt["run_id"]:
            raise argo.ArgoCoreError("verifier ownership changed; refusing to resume")
        self.patch(
            [
                {"op": "test", "path": "/metadata/uid", "value": self.uid},
                {"op": "test", "path": OWNER_PATH, "value": self.receipt["run_id"]},
                {"op": "test", "path": "/spec/suspend", "value": True},
                {"op": "replace", "path": "/spec/suspend", "value": False},
                {"op": "remove", "path": OWNER_PATH},
                {"op": "remove", "path": OWNER_PATH + "-expires-at"},
                {"op": "remove", "path": OWNER_PATH + "-uid"},
            ]
        )
        self.record("resumed")

    def list_jobs(self) -> list[dict[str, Any]]:
        payload = json.loads(self.command("get", "jobs", "-o", "json"))
        items = payload.get("items")
        if not isinstance(items, list):
            raise argo.ArgoCoreError("synthetic live-verify Job list items must be a list")
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _terminal(job: dict[str, Any]) -> bool:
        status = job.get("status") or {}
        if not isinstance(status, dict):
            return False
        conditions = status.get("conditions") or []
        return isinstance(conditions, list) and any(
            isinstance(condition, dict)
            and condition.get("type") in {"Complete", "Failed"}
            and condition.get("status") == "True"
            for condition in conditions
        )

    def _owned_job(self, job: dict[str, Any]) -> tuple[str, str, str] | None:
        metadata = job.get("metadata")
        if not isinstance(metadata, dict):
            return None
        name, uid, version = (metadata.get(key) for key in ("name", "uid", "resourceVersion"))
        if not all(isinstance(value, str) and value for value in (name, uid, version)):
            return None
        references = metadata.get("ownerReferences") or []
        if not isinstance(references, list):
            return None
        if not any(
            isinstance(reference, dict)
            and reference.get("kind") == "CronJob"
            and reference.get("name") == train.VERIFY_CRONJOB
            and reference.get("uid") == self.uid
            and reference.get("controller") is True
            for reference in references
        ):
            return None
        return name, uid, version

    def _assert_owned_paused(self, current: dict[str, Any]) -> None:
        metadata = current.get("metadata") or {}
        annotations = metadata.get("annotations") or {}
        if (
            metadata.get("uid") != self.uid
            or annotations.get(OWNER) != self.receipt["run_id"]
            or annotations.get(UID) != self.uid
            or current.get("spec", {}).get("suspend") is not True
        ):
            raise argo.ArgoCoreError(
                "verifier ownership changed; refusing to cancel scheduled verification"
            )

    def _delete_job(self, name: str, uid: str, version: str) -> None:
        namespace = quote(train.VERIFY_NAMESPACE, safe="")
        encoded_name = quote(name, safe="")
        self.command(
            "delete",
            "--raw",
            f"/apis/batch/v1/namespaces/{namespace}/jobs/{encoded_name}",
            "-f",
            "-",
            input_text=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "propagationPolicy": "Foreground",
                    "preconditions": {"uid": uid, "resourceVersion": version},
                }
            ),
        )

    def wait_cancelled(self, uids: set[str]) -> None:
        deadline = time.monotonic() + DELETE_TIMEOUT_SECONDS
        while uids:
            remaining = {
                job.get("metadata", {}).get("uid") for job in self.list_jobs()
            }
            if not uids.intersection(remaining):
                return
            seconds_left = deadline - time.monotonic()
            if seconds_left <= 0:
                raise argo.ArgoCoreError("scheduled verifier cancellation deadline exceeded")
            time.sleep(min(self.args.poll_interval, seconds_left))

    def cancel_scheduled(self) -> None:
        active = [
            owned
            for job in self.list_jobs()
            if not self._terminal(job)
            for owned in [self._owned_job(job)]
            if owned is not None
        ]
        for name, uid, version in active:
            self._assert_owned_paused(self.read())
            self._delete_job(name, uid, version)
        # Foreground deletion must finish even if a Job becomes terminal while
        # its dependent pods are still being removed.
        self.wait_cancelled({uid for _, uid, _ in active})
        self._assert_owned_paused(self.read())
        # Manual or ambiguously owned verification must block, never be deleted.
        train.ensure_no_active_verify_job(
            kubeconfig=self.kubeconfig, kubectl=self.args.kubectl
        )


def recover_expired_window(
    args: Any, kubeconfig: Any, receipt: dict, save: Any
) -> bool:
    if not args.pause_verifier:
        return False
    if not args.apply or "agent-control-plane" in args.application:
        raise argo.ArgoCoreError("verifier pause requires --apply without Core sync")
    window = VerifierWindow(args, kubeconfig, receipt, save)
    window.reclaim_expired(window.read())
    return "reclaimed_verifier_window" in receipt


@contextmanager
def verifier_window(args: Any, kubeconfig: Any, receipt: dict, save: Any):
    if not args.pause_verifier:
        yield
        return
    if not args.apply or "agent-control-plane" in args.application:
        raise argo.ArgoCoreError("verifier pause requires --apply without Core sync")
    window = VerifierWindow(args, kubeconfig, receipt, save)
    old_handlers = {}
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise argo.ArgoCoreError("cannot replace an existing process timer")

    def interrupted(signum: int, _frame: Any) -> None:
        raise argo.ArgoCoreError(f"deployment interrupted by signal {signum}")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
            old_handlers[signum] = signal.signal(signum, interrupted)
        signal.alarm(WINDOW_SECONDS)
        window.acquire()
        window.cancel_scheduled()
        yield
    finally:
        signal.alarm(0)
        try:
            window.release()
        except (argo.ArgoCoreError, OSError, ValueError, subprocess.TimeoutExpired):
            window.record("resume-failed")
            raise
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
