from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.grant_ownership import (
    GrantEditError,
    GrantOwnershipError,
    apply_grant_edit,
    check_ownership_outputs,
    create_grant_edit_pr,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Edit deployment-owned Mandate capability grant knobs in the registry "
            "overlay without moving workload digests."
        )
    )
    parser.add_argument("capability", help="Capability id, e.g. agent_workloads.opencode_propose")
    parser.add_argument("key", help="Dot path under the capability overlay")
    parser.add_argument("value", help="YAML scalar value to set, or 'unset'")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to the GarzAICluster checkout.",
    )
    parser.add_argument(
        "--agent-workloads-repo",
        type=Path,
        help="Path to agent-workloads for ownership-map freshness validation.",
    )
    parser.add_argument(
        "--skip-map-check",
        action="store_true",
        help="Skip checking docs/grant-ownership.* against the applier contract.",
    )
    parser.add_argument(
        "--output-pr-body",
        type=Path,
        help="Write the grant-edit PR body to this path.",
    )
    parser.add_argument(
        "--create-pr",
        action="store_true",
        help="Commit, push, and create a GitHub PR labeled grant-edit.",
    )
    parser.add_argument(
        "--branch",
        help="Branch name to use with --create-pr. Defaults to grant-edit/<capability>/<key>.",
    )
    args = parser.parse_args()

    try:
        if not args.skip_map_check:
            check_ownership_outputs(
                repo_root=args.repo_root,
                agent_workloads_repo=args.agent_workloads_repo,
            )
        pr_body_path = args.output_pr_body
        if args.create_pr and pr_body_path is None:
            pr_body_path = args.repo_root / ".git" / "grant-edit-pr-body.md"
        result = apply_grant_edit(
            repo_root=args.repo_root,
            capability_id=args.capability,
            key_path=args.key,
            raw_value=args.value,
            pr_body_path=pr_body_path,
        )
        print(
            f"{result.action} {result.capability_id} {result.key_path} in "
            f"{result.config_path}"
        )
        print("deploy consequence: overlay-only: CP restart required, no re-mint")
        if args.create_pr:
            if pr_body_path is None:
                raise GrantEditError("--create-pr requires a PR body path")
            create_grant_edit_pr(
                repo_root=args.repo_root,
                result=result,
                pr_body_path=pr_body_path,
                branch=args.branch,
            )
    except (GrantEditError, GrantOwnershipError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
