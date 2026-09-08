#!/usr/bin/env python3
"""Reconcile an explicit Mandate subset without syncing the root Application."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import mandate_deploy_train as train
from mandate_verifier_window import verifier_window

argo = train.argo
ALLOWED_APPLICATIONS = (
    "agent-workloads-secrets",
    train.OVERLAY_APPLICATION,
    "agent-control-plane",
    "agent-workloads",
)


def selected_applications(names: list[str]) -> tuple[str, ...]:
    selected = tuple(names)
    if not selected or len(set(selected)) != len(selected):
        raise argo.ArgoCoreError("select at least one application without duplicates")
    ordered = tuple(
        name
        for stage in train.STAGES
        for name in stage.applications
        if name in ALLOWED_APPLICATIONS and name in selected
    )
    if selected != ordered:
        raise argo.ArgoCoreError(
            "application scope is forbidden or outside canonical order"
        )
    return selected


def dependencies(selected: tuple[str, ...]) -> set[str]:
    required = set(selected)
    while True:
        expanded = required | {
            predecessor
            for predecessor, successor in train.DEPENDENCIES
            if successor in required
        }
        if expanded == required:
            return required - set(selected)
        required = expanded


def state_receipt(snapshot: Any) -> dict[str, Any]:
    operation = snapshot.operation
    return {
        "sync": snapshot.sync_status,
        "health": snapshot.health_status,
        "revisions": list(snapshot.revisions),
        "operation": {
            "phase": operation.phase,
            "revisions": list(operation.revisions),
            "started_at": operation.started_at,
            "finished_at": operation.finished_at,
        },
    }


def read_snapshot(contract: Any, args: argparse.Namespace, kubeconfig: Path) -> Any:
    payload = argo.read_application_payload(
        contract.name,
        kubeconfig=kubeconfig,
        kubectl=args.kubectl,
        namespace=args.namespace,
    )
    return train.validate_live_application(contract, payload)


def dry_run(contract: Any, args: argparse.Namespace, kubeconfig: Path) -> None:
    command = [
        args.argocd,
        "--core",
        "app",
        "sync",
        contract.name,
        "--dry-run",
        "--strategy",
        "hook",
        "--prune",
    ]
    revisions = train.submission_revisions(contract)
    if len(revisions) == 1:
        command += ["--revision", revisions[0]]
    else:
        for position, revision in enumerate(revisions, 1):
            command += ["--revisions", revision, "--source-positions", str(position)]
    environment = os.environ.copy()
    environment["KUBECONFIG"] = str(kubeconfig)
    result = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=args.operation_timeout,
    )
    if result.returncode:
        raise argo.command_failure(f"dry run {contract.name}", result)
    train.emit_receipt("scoped-dry-run", "succeeded", application=contract.name)


def save_receipt(args: argparse.Namespace, receipt: dict[str, Any]) -> None:
    args.receipt_dir.mkdir(parents=True, exist_ok=True)
    (args.receipt_dir / "scoped-deploy.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def guard(args: argparse.Namespace) -> None:
    train.validate_release_checkout(args.repo_root, args.confirm_sha, git=args.git)


def application_preflight(
    args: argparse.Namespace,
    selected: tuple[str, ...],
    contracts: dict[str, Any],
    kubeconfig: Path,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    snapshots = {}
    upstream = dependencies(selected)
    for name in train.MANAGED_APPLICATIONS:
        if name not in upstream and name not in selected:
            continue
        snapshot = read_snapshot(contracts[name], args, kubeconfig)
        if name in upstream and (
            snapshot.sync_status != "Synced" or snapshot.health_status != "Healthy"
        ):
            raise argo.ArgoCoreError(f"omitted dependency is not ready: {name}")
        snapshots[name] = snapshot
        receipt["before"][name] = state_receipt(snapshot)
    return snapshots


def preflight(args, selected, contracts, kubeconfig, receipt):
    train.preflight_mandate_verify(
        invocation_id=receipt["run_id"],
        kubeconfig=kubeconfig,
        kubectl=args.kubectl,
    )
    for name in selected:
        guard(args)
        dry_run(contracts[name], args, kubeconfig)


def reconcile_one(
    args: argparse.Namespace,
    contract: Any,
    snapshot: Any,
    skills: Any,
    kubeconfig: Path,
    run_id: str,
) -> str:
    guard(args)
    return train.reconcile_application(
        contract,
        stage="scoped",
        invocation_id=run_id,
        desired_skills=skills,
        preflight_snapshot=snapshot,
        preflight_skill_digest=skills.digest,
        force_sync=True,
        kubeconfig=kubeconfig,
        argocd=args.argocd,
        kubectl=args.kubectl,
        namespace=args.namespace,
        refresh_timeout=args.refresh_timeout,
        operation_timeout=args.operation_timeout,
        adoption_timeout=0,
        interval=args.poll_interval,
        revision_guard=lambda: guard(args),
    )


def execute(
    args: argparse.Namespace,
    kubeconfig: Path,
    receipt: dict[str, Any],
) -> None:
    selected = selected_applications(args.application)
    guard(args)
    contracts = train.load_application_contracts(args.repo_root, args.confirm_sha)
    snapshots = application_preflight(args, selected, contracts, kubeconfig, receipt)
    with verifier_window(args, kubeconfig, receipt, save_receipt):
        preflight(args, selected, contracts, kubeconfig, receipt)
        deploy_selected(args, selected, contracts, snapshots, kubeconfig, receipt)


def deploy_selected(args, selected, contracts, snapshots, kubeconfig, receipt):
    skills = train.read_live_skill_bundle(kubeconfig=kubeconfig, kubectl=args.kubectl)
    receipt["skill_bundle_digest"] = skills.digest
    if not args.apply:
        receipt["status"] = "preflight-passed"
        return
    for name in selected:
        outcome = reconcile_one(
            args,
            contracts[name],
            snapshots[name],
            skills,
            kubeconfig,
            receipt["run_id"],
        )
        snapshot = read_snapshot(contracts[name], args, kubeconfig)
        if (
            snapshot.sync_status != "Synced"
            or snapshot.health_status != "Healthy"
            or snapshot.revisions != contracts[name].resolved_revisions
        ):
            raise argo.ArgoCoreError(f"selected application did not converge: {name}")
        train.assert_full_hook_operation(name, snapshot.operation)
        receipt["completed"].append(
            {
                "application": name,
                "outcome": outcome,
                **state_receipt(snapshot),
            }
        )
        save_receipt(args, receipt)
    guard(args)
    receipt["verification_job"] = train.run_mandate_verify(
        train.load_journey_contracts(args.repo_root),
        kubeconfig=kubeconfig,
        kubectl=args.kubectl,
        timeout=train.VERIFY_DEADLINE_SECONDS,
        run_id=receipt["run_id"],
    )
    guard(args)
    receipt["status"] = "verified"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=train.REPO_ROOT)
    parser.add_argument("--confirm-sha", required=True)
    parser.add_argument(
        "--application", action="append", required=True, choices=ALLOWED_APPLICATIONS
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--pause-verifier",
        action="store_true",
        help="Pause scheduled verification during a worker-only apply",
    )
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, default=Path.home() / ".kube/config")
    parser.add_argument("--context", required=True, choices=(train.PRODUCTION_CONTEXT,))
    parser.add_argument("--namespace", choices=("argocd",), default="argocd")
    parser.add_argument("--argocd", default=argo.default_argocd_executable())
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--git", default="git")
    parser.add_argument("--refresh-timeout", type=float, default=60)
    parser.add_argument("--operation-timeout", type=float, default=600)
    parser.add_argument("--poll-interval", type=float, default=3)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.repo_root = args.repo_root.resolve()
    args.receipt_dir = args.receipt_dir.resolve()
    if args.receipt_dir.is_relative_to(args.repo_root):
        parser.error("receipt directory must be outside the release checkout")
    receipt: dict[str, Any] = {
        "config_sha": args.confirm_sha,
        "applications": args.application,
        "run_id": f"scoped-{uuid.uuid4().hex[:12]}",
        "status": "started",
        "before": {},
        "completed": [],
    }
    try:
        selected_applications(args.application)
        if args.pause_verifier and (
            not args.apply or "agent-control-plane" in args.application
        ):
            parser.error("--pause-verifier requires --apply without Core sync")
        train.validate_production_context(args.context)
        timeouts = (args.refresh_timeout, args.operation_timeout, args.poll_interval)
        if any(not math.isfinite(value) or value <= 0 for value in timeouts):
            raise argo.ArgoCoreError("timeouts must be finite and positive")
        if args.repo_root != Path(__file__).resolve().parents[1]:
            raise argo.ArgoCoreError(
                "run the entrypoint from the selected release checkout"
            )
        for name in ("argocd", "kubectl", "git"):
            setattr(args, name, argo.resolve_executable(getattr(args, name)))
        argo.validate_argocd_version(args.argocd, argo.pinned_version())
        with argo.core_kubeconfig(
            args.kubeconfig,
            kubectl=args.kubectl,
            namespace=args.namespace,
            context=args.context,
        ) as kubeconfig:
            execute(args, kubeconfig, receipt)
        save_receipt(args, receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except (argo.ArgoCoreError, OSError, subprocess.TimeoutExpired) as error:
        receipt["status"] = "failed"
        receipt["error"] = str(error)
        save_receipt(args, receipt)
        print(f"mandate_scoped_deploy: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
