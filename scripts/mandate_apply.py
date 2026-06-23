from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.workload_enablement import (
    WorkloadEnablementError,
    apply_workload_enablement,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or apply a Mandate workload enablement document into the "
            "deployment repo. Dry-run is the default; --write edits files for a PR."
        )
    )
    parser.add_argument("document", type=Path, help="MandateWorkloadEnablement YAML")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to the GarzAICluster checkout.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write automatic deployment-owned edits. Never writes secret values.",
    )
    parser.add_argument(
        "--output-pr-body",
        type=Path,
        help="Write a PR body describing edits, gaps, and authority invariants.",
    )
    args = parser.parse_args()

    try:
        result = apply_workload_enablement(
            repo_root=args.repo_root,
            document_path=args.document,
            write=args.write,
            pr_body_path=args.output_pr_body,
        )
    except WorkloadEnablementError as exc:
        parser.exit(1, f"error: {exc}\n")

    mode = "wrote" if args.write else "planned"
    print(f"{mode} enablement for {result.capability} on {result.workload}")
    if result.changed_files:
        print("changed files:")
        for path in result.changed_files:
            print(f"- {path}")
    print("actions:")
    for action in result.actions:
        print(f"- {action.code}: {action.message}")
    if result.gaps:
        print("operator gaps:")
        for gap in result.gaps:
            print(f"- {gap.code}: {gap.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
