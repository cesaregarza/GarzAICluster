#!/usr/bin/env python3
"""Compare a reviewed baseline chart and values with the candidate resource set."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

from ruamel.yaml import YAML

CHART = "helm/agent-workloads"
VALUES = "apps/agent-workloads/values.yaml"
DEAD_ENV = frozenset(
    {
        "OPENAI_SQL_BROKER_MODEL",
        "OPENAI_SQL_BROKER_REASONING_EFFORT",
        "OPENAI_SQL_BROKER_TIMEOUT_SECONDS",
    }
)


def resources(rendered: str, *, baseline: bool) -> dict[str, dict]:
    result = {}
    for resource in YAML(typ="safe").load_all(rendered):
        if not resource:
            continue
        metadata = resource["metadata"]
        key = "/".join(
            (
                resource["apiVersion"],
                resource["kind"],
                metadata.get("namespace", ""),
                metadata["name"],
            )
        )
        if key in result:
            raise ValueError(f"duplicate rendered resource: {key}")
        if resource["kind"] == "Deployment":
            for container in resource["spec"]["template"]["spec"]["containers"]:
                env = container.get("env", [])
                dead = [entry for entry in env if entry.get("name") in DEAD_ENV]
                if dead and not baseline:
                    raise ValueError(f"dead broker environment still rendered in {key}")
                if dead:
                    container["env"] = [
                        entry for entry in env if entry.get("name") not in DEAD_ENV
                    ]
        result[key] = resource
    if not result:
        raise ValueError("chart rendered no resources")
    return result


def canonical(resource: dict) -> str:
    return json.dumps(resource, sort_keys=True, separators=(",", ":"), allow_nan=False)


def compare(before: dict, after: dict) -> dict:
    missing = sorted(before.keys() - after.keys())
    added = sorted(after.keys() - before.keys())
    changed = sorted(
        key
        for key in before.keys() & after.keys()
        if canonical(before[key]) != canonical(after[key])
    )
    return {
        "verified": not (missing or added or changed),
        "resource_count": len(after),
        "missing": missing,
        "added": added,
        "changed": changed,
        "resource_hashes": {
            key: hashlib.sha256(canonical(value).encode()).hexdigest()
            for key, value in sorted(after.items())
        },
    }


def render(repo: Path, helm: str) -> str:
    return subprocess.run(
        [
            helm,
            "template",
            "agent-workloads",
            str(repo / CHART),
            "-f",
            str(repo / VALUES),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def extract_baseline(repo: Path, revision: str, destination: Path) -> str:
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{revision}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", commit, CHART, VALUES],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination) or not (
                member.isfile() or member.isdir()
            ):
                raise ValueError(
                    "baseline archive must contain only native regular files/directories"
                )
        bundle.extractall(destination, filter="data")
    return commit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--helm", default="helm")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if repo.is_relative_to("/mnt"):
        parser.error("repository must be on the native filesystem")
    try:
        with tempfile.TemporaryDirectory(
            prefix="worker-chart-baseline-", dir="/tmp"
        ) as temporary:
            baseline = Path(temporary)
            commit = extract_baseline(repo, args.baseline_ref, baseline)
            proof = compare(
                resources(render(baseline, args.helm), baseline=True),
                resources(render(repo, args.helm), baseline=False),
            )
        proof["baseline_commit"] = commit
        print(json.dumps(proof, indent=2, sort_keys=True))
        return 0 if proof["verified"] else 1
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"worker chart migration proof failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
