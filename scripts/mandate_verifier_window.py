"""Hold a scoped worker rollout between scheduled verification runs."""

from __future__ import annotations

import json
import signal
import subprocess
import time
from contextlib import contextmanager
from typing import Any

import mandate_deploy_train as train

argo = train.argo
OWNER = "mandate.garz.ai/verifier-window"
OWNER_PATH = "/metadata/annotations/mandate.garz.ai~1verifier-window"


class VerifierWindow:
    def __init__(self, args: Any, kubeconfig: Any, receipt: dict, save: Any):
        self.args, self.kubeconfig = args, kubeconfig
        self.receipt, self.save = receipt, save
        self.uid = ""

    def command(self, *args: str) -> str:
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

    def acquire(self) -> None:
        current = self.read()
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
            ]
        )
        self.record("resumed")

    def drain(self) -> None:
        deadline = time.monotonic() + train.VERIFY_DEADLINE_SECONDS + 30
        while True:
            try:
                train.ensure_no_active_verify_job(
                    kubeconfig=self.kubeconfig, kubectl=self.args.kubectl
                )
                return
            except argo.ArgoCoreError as error:
                if not str(error).startswith(
                    "stage=mandate-verify reason=nonterminal-job-overlap "
                ):
                    raise
                if time.monotonic() >= deadline:
                    raise argo.ArgoCoreError(
                        "verifier drain deadline exceeded"
                    ) from error
                time.sleep(self.args.poll_interval)


@contextmanager
def verifier_window(args: Any, kubeconfig: Any, receipt: dict, save: Any):
    if not args.pause_verifier:
        yield
        return
    if not args.apply or "agent-control-plane" in args.application:
        raise argo.ArgoCoreError("verifier pause requires --apply without Core sync")
    window = VerifierWindow(args, kubeconfig, receipt, save)
    old_handlers = {}

    def interrupted(signum: int, _frame: Any) -> None:
        raise argo.ArgoCoreError(f"deployment interrupted by signal {signum}")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, interrupted)
        window.acquire()
        window.drain()
        yield
    finally:
        try:
            window.release()
        except (argo.ArgoCoreError, OSError, ValueError, subprocess.TimeoutExpired):
            window.record("resume-failed")
            raise
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
