#!/usr/bin/env python3
"""Safely stop or restore the contracted SplatTop Redis writers.

The contract supplies live Deployment UIDs and baseline replicas.  The worker
is quiesced through the reviewed celery_quiesce.py helper; this script does
not duplicate Celery control logic or touch Redis data.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


MAX_TIMEOUT = 900
EXPECTED_BASELINES = {
    "splattop-prod-fastapi": 2,
    "splattop-prod-celery-beat": 1,
    "splattop-prod-celery-worker": 1,
}


class WriterControlError(RuntimeError):
    """A writer identity, transition, or bounded wait failed."""


class Kubectl:
    def __init__(self, binary: str, context: str, namespace: str) -> None:
        self.binary, self.context, self.namespace = binary, context, namespace

    def run(self, args: list[str], *, stdin: bytes | None = None, timeout: int = 45) -> bytes:
        result = subprocess.run(
            [self.binary, "--context", self.context, "-n", self.namespace, *args],
            input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        if result.returncode:
            raise WriterControlError(f"kubectl command failed with status {result.returncode}")
        return result.stdout

    def json(self, args: list[str]) -> dict[str, Any]:
        try:
            value = json.loads(self.run(args))
        except (json.JSONDecodeError, TypeError) as exc:
            raise WriterControlError("kubectl did not return a JSON object") from exc
        if not isinstance(value, dict):
            raise WriterControlError("kubectl JSON object required")
        return value


def _contract(path: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WriterControlError("writer contract is unreadable or invalid") from exc
    entries = document.get("writers") if isinstance(document, dict) else None
    worker = document.get("worker_deployment") if isinstance(document, dict) else None
    if not isinstance(entries, list) or not entries or not isinstance(worker, str):
        raise WriterControlError("contract requires writers and worker_deployment")
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise WriterControlError("writer contract entries must be objects")
        name, uid, baseline = entry.get("deployment"), entry.get("uid"), entry.get("baseline_replicas")
        if not isinstance(name, str) or not name or name in names:
            raise WriterControlError("writer deployment names must be unique")
        if not isinstance(uid, str) or not uid:
            raise WriterControlError(f"writer UID missing for {name}")
        if type(baseline) is not int or baseline <= 0:
            raise WriterControlError(f"baseline replicas must be positive for {name}")
        names.add(name)
        result.append({"deployment": name, "uid": uid, "baseline_replicas": baseline})
    if worker not in names:
        raise WriterControlError("worker_deployment is not a contracted writer")
    if names != set(EXPECTED_BASELINES) or any(
        entry["baseline_replicas"] != EXPECTED_BASELINES[entry["deployment"]] for entry in result
    ) or worker != "splattop-prod-celery-worker":
        raise WriterControlError("contract must contain the three SplatTop writers with baselines 2/1/1")
    return result, worker


def _state(kubectl: Kubectl, entry: dict[str, Any]) -> dict[str, Any]:
    name = entry["deployment"]
    deployment = kubectl.json(["get", "deployment", name, "-o", "json"])
    actual_uid = deployment.get("metadata", {}).get("uid")
    if actual_uid != entry["uid"]:
        raise WriterControlError(f"Deployment UID mismatch for {name}")
    replicas = deployment.get("spec", {}).get("replicas")
    selector = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
    if type(replicas) is not int or not isinstance(selector, dict) or not selector or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in selector.items()
    ):
        raise WriterControlError(f"Deployment state is invalid for {name}")
    return {**entry, "replicas": replicas, "selector": selector, "status": deployment.get("status", {})}


def _pods(kubectl: Kubectl, state: dict[str, Any]) -> list[dict[str, Any]]:
    selector = ",".join(f"{k}={v}" for k, v in sorted(state["selector"].items()))
    document = kubectl.json(["get", "pods", "-l", selector, "-o", "json"])
    items = document.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise WriterControlError(f"Pod inventory is invalid for {state['deployment']}")
    return items


def _patch_replicas(kubectl: Kubectl, state: dict[str, Any], current: int, target: int, timeout: int) -> None:
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": state["uid"]},
        {"op": "test", "path": "/spec/replicas", "value": current},
        {"op": "replace", "path": "/spec/replicas", "value": target},
    ]
    kubectl.run(["patch", "deployment", state["deployment"], "--type=json", "-p", json.dumps(patch)], timeout=timeout)


def _wait_stopped(kubectl: Kubectl, states: list[dict[str, Any]], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stopped = True
        for state in states:
            current = _state(kubectl, state)
            if current["replicas"] != 0 or _pods(kubectl, current):
                stopped = False
        if stopped:
            return
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise WriterControlError("contracted writer pods did not reach zero")


def _wait_baseline(kubectl: Kubectl, states: list[dict[str, Any]], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = True
        for state in states:
            current = _state(kubectl, state)
            if current["replicas"] != current["baseline_replicas"]:
                ready = False
                break
            if current.get("status", {}).get("readyReplicas") != current["baseline_replicas"]:
                ready = False
                break
            pods = [
                pod for pod in _pods(kubectl, current)
                if not pod.get("metadata", {}).get("deletionTimestamp")
                and pod.get("status", {}).get("phase") == "Running"
                and any(condition.get("type") == "Ready" and condition.get("status") == "True" for condition in pod.get("status", {}).get("conditions", []))
            ]
            if len(pods) < state["baseline_replicas"]:
                ready = False
                break
        if ready:
            return
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise WriterControlError("contracted writers did not return to baseline")


def _worker_pod(kubectl: Kubectl, state: dict[str, Any]) -> dict[str, Any] | None:
    pods = _pods(kubectl, state)
    active = [p for p in pods if not p.get("metadata", {}).get("deletionTimestamp")]
    if len(active) != 1:
        raise WriterControlError("worker must have exactly one active pod")
    pod = active[0]
    if pod.get("status", {}).get("phase") != "Running":
        raise WriterControlError("worker pod must be Running")
    return pod


def _quiesce(kubectl: Kubectl, pod_name: str, action: str, script: bytes, timeout: int) -> None:
    worker = f"celery@{pod_name}"
    kubectl.run(
        ["exec", "-i", pod_name, "--", "env", f"HOSTNAME={pod_name}", "python3", "-", action, "--expected-worker", worker],
        stdin=script, timeout=timeout,
    )


def _restore(kubectl: Kubectl, states: list[dict[str, Any]], old_worker: dict[str, Any] | None, script: bytes, timeout: int) -> bool:
    for state in states:
        if state["replicas"] not in (0, state["baseline_replicas"]):
            raise WriterControlError(f"{state['deployment']} is neither stopped nor baseline")
    for state in states:
        if state["replicas"] == 0:
            _patch_replicas(kubectl, state, 0, state["baseline_replicas"], timeout)
    _wait_baseline(kubectl, states, timeout)
    resumed = False
    if old_worker:
        worker_state = next(state for state in states if state["deployment"] == old_worker["deployment"])
        for pod in _pods(kubectl, worker_state):
            if pod.get("metadata", {}).get("uid") == old_worker["uid"] and not pod.get("metadata", {}).get("deletionTimestamp"):
                _quiesce(kubectl, pod["metadata"]["name"], "resume", script, timeout)
                resumed = True
                break
    return resumed


def stop(kubectl: Kubectl, entries: list[dict[str, Any]], worker_name: str, script: bytes, timeout: int) -> dict[str, Any]:
    states = [_state(kubectl, entry) for entry in entries]
    if any(state["replicas"] != state["baseline_replicas"] for state in states):
        raise WriterControlError("all writers must match contracted baseline before stop")
    worker_state = next(state for state in states if state["deployment"] == worker_name)
    worker_pod = _worker_pod(kubectl, worker_state)
    patched: list[dict[str, Any]] = []
    quiesce_started = False
    try:
        quiesce_started = True
        _quiesce(kubectl, worker_pod["metadata"]["name"], "quiesce", script, timeout)
        for state in states:
            _patch_replicas(kubectl, state, state["replicas"], 0, timeout)
            patched.append(state)
        _wait_stopped(kubectl, states, timeout)
    except BaseException as exc:
        recovery_errors: list[str] = []
        for state in reversed(patched):
            try:
                _patch_replicas(kubectl, state, 0, state["baseline_replicas"], timeout)
            except Exception as recovery_exc:
                recovery_errors.append(f"restore {state['deployment']}: {recovery_exc}")
        resumed = False
        if quiesce_started:
            try:
                resumed = _restore(
                    kubectl,
                    [_state(kubectl, entry) for entry in entries],
                    {"deployment": worker_name, "uid": worker_pod["metadata"]["uid"]},
                    script,
                    timeout,
                )
            except Exception as recovery_exc:
                recovery_errors.append(f"restore baseline/resume: {recovery_exc}")
            if not resumed:
                try:
                    current_worker = _state(kubectl, next(entry for entry in entries if entry["deployment"] == worker_name))
                    same_pod = next(
                        (pod for pod in _pods(kubectl, current_worker)
                         if pod.get("metadata", {}).get("uid") == worker_pod["metadata"].get("uid")
                         and not pod.get("metadata", {}).get("deletionTimestamp")),
                        None,
                    )
                    if same_pod is None:
                        raise WriterControlError("original worker Pod is no longer present")
                    _quiesce(kubectl, same_pod["metadata"]["name"], "resume", script, timeout)
                    resumed = True
                except Exception as recovery_exc:
                    recovery_errors.append(f"resume original worker: {recovery_exc}")
        if recovery_errors:
            raise WriterControlError(f"writer stop failed ({exc}); recovery failed: {'; '.join(recovery_errors)}") from exc
        if isinstance(exc, WriterControlError):
            raise
        raise WriterControlError("writer stop failed") from exc
    return {"action": "stop", "status": "stopped", "writers": [state["deployment"] for state in states]}


def verify_stopped(kubectl: Kubectl, entries: list[dict[str, Any]], timeout: int = 1) -> dict[str, Any]:
    states = [_state(kubectl, entry) for entry in entries]
    if any(state["replicas"] != 0 for state in states):
        raise WriterControlError("contracted writer replicas are not zero")
    _wait_stopped(kubectl, states, timeout)
    return {"action": "verify-stopped", "status": "stopped", "writers": [state["deployment"] for state in states]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stop", "restore", "verify-stopped"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--quiesce-script", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--kubectl", default="kubectl")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.timeout_seconds <= MAX_TIMEOUT:
            raise WriterControlError("--timeout-seconds must be between 1 and 900")
        entries, worker = _contract(args.contract)
        script = args.quiesce_script.read_bytes()
        kubectl = Kubectl(args.kubectl, args.context, args.namespace)
        if args.action == "stop":
            result = stop(kubectl, entries, worker, script, args.timeout_seconds)
        elif args.action == "verify-stopped":
            result = verify_stopped(kubectl, entries, args.timeout_seconds)
        else:
            states = [_state(kubectl, entry) for entry in entries]
            if any(state["replicas"] != 0 for state in states):
                raise WriterControlError("standalone restore requires every writer to be stopped")
            worker_state = next(state for state in states if state["deployment"] == worker)
            old_worker = None
            if worker_state["replicas"] == 0:
                pods = _pods(kubectl, worker_state)
                active = [p for p in pods if not p.get("metadata", {}).get("deletionTimestamp")]
                if len(active) == 1:
                    old_worker = {"deployment": worker, "uid": active[0].get("metadata", {}).get("uid")}
            result = {"action": "restore", "status": "restored", "writers": [state["deployment"] for state in states]}
            _restore(kubectl, states, old_worker, script, args.timeout_seconds)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, subprocess.SubprocessError, WriterControlError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
