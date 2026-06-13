from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.grant_ownership import (
    GrantOwnershipError,
    check_ownership_outputs,
    write_ownership_outputs,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate or check the SplatTopConfig grant ownership map."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to the SplatTopConfig checkout.",
    )
    parser.add_argument(
        "--agent-workloads-repo",
        type=Path,
        help=(
            "Path to a cesaregarza/agent-workloads checkout. Defaults to "
            "$AGENT_WORKLOADS_REPO, .ci/agent-workloads, or ../agent-workloads."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if docs/grant-ownership.* is stale.",
    )
    args = parser.parse_args()

    try:
        if args.check:
            check_ownership_outputs(
                repo_root=args.repo_root,
                agent_workloads_repo=args.agent_workloads_repo,
            )
        else:
            write_ownership_outputs(
                repo_root=args.repo_root,
                agent_workloads_repo=args.agent_workloads_repo,
            )
    except GrantOwnershipError as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
