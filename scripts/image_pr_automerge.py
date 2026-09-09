#!/usr/bin/env python3
"""Validate a build-generated image PR; optionally enable protected auto-merge.

The source build is the trusted caller. This never approves reviews, bypasses
branch protection, publishes images or syncs Argo. The CI event mode must run
from trusted base code and only reads candidate files as data.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.tokens import AliasToken, AnchorToken, TagToken

SHA = re.compile(r"[0-9a-f]{40}\Z")
ACTIONS_APP = 15368
POLICY_PATH = "automation/image-automerge.json"


class PolicyError(RuntimeError):
    """Candidate does not satisfy the automatic merge contract."""


def run(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        # Do not echo arbitrary output or credential-bearing payloads.
        raise PolicyError(f"{command[0]} operation failed (exit {result.returncode})")
    return result.stdout.strip()


def git(root: Path, *args: str) -> str:
    return run(["git", "-C", str(root), *args])


def api(endpoint: str) -> dict:
    try:
        return json.loads(run(["gh", "api", "--method", "GET", endpoint]))
    except PolicyError as exc:
        raise PolicyError(f"GitHub metadata read failed: GET {endpoint}") from exc


def source_sha(value: object) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise PolicyError("release revision must be an exact lowercase source SHA")
    return value


def load_policy(root: Path) -> dict:
    config = json.loads((root / POLICY_PATH).read_text())
    if config.get("schemaVersion") != 1 or not config.get("policies"):
        raise PolicyError("unsupported or empty auto-merge policy")
    return config


def matches(policy: dict, branch: str) -> bool:
    return (
        re.fullmatch(re.escape(policy["branchPrefix"]) + r"[0-9]+", branch) is not None
    )


def yaml_values(text: str) -> dict:
    if len(text.encode()) > 1024 * 1024:
        raise PolicyError("values document exceeds the size limit")
    parser = YAML(typ="safe", pure=True)
    parser.allow_duplicate_keys = False
    try:
        if any(
            isinstance(token, (AliasToken, AnchorToken, TagToken))
            for token in parser.scan(text)
        ):
            raise PolicyError(
                "automatic values updates cannot use YAML aliases or tags"
            )
        values = parser.load(text)
    except PolicyError:
        raise
    except Exception as exc:
        raise PolicyError("invalid or ambiguous values YAML") from exc
    if not isinstance(values, dict):
        raise PolicyError("values document must be a mapping")
    return values


def value_parent(values: dict, path: str) -> tuple[dict, str]:
    parts = path.split(".")
    parent = values
    for part in parts[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            raise PolicyError(f"missing release binding: {path}")
        parent = parent[part]
    if not isinstance(parent, dict) or parts[-1] not in parent:
        raise PolicyError(f"missing release binding: {path}")
    return parent, parts[-1]


def typed(value: object) -> object:
    """Preserve scalar types: YAML true must never compare equal to integer 1."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise PolicyError("values keys must be strings")
        return (dict, tuple((key, typed(item)) for key, item in sorted(value.items())))
    if isinstance(value, list):
        return (list, tuple(typed(item) for item in value))
    return (type(value), value)


def validate_values(
    before: dict, after: dict, fields: list[str], revision: str
) -> None:
    old, new = copy.deepcopy(before), copy.deepcopy(after)
    changed = False
    for field in fields:
        old_parent, key = value_parent(old, field)
        new_parent, _ = value_parent(new, field)
        source_sha(old_parent[key])
        if source_sha(new_parent[key]) != revision:
            raise PolicyError(f"release binding differs from the built source: {field}")
        changed = changed or old_parent[key] != new_parent[key]
        old_parent[key] = new_parent[key] = "<validated-release-revision>"
    if not changed:
        raise PolicyError("values file has no release revision change")
    if typed(old) != typed(new):
        raise PolicyError("PR changes values outside the allowed release bindings")


def validate_diff(
    root: Path, base: str, head: str, policy: dict, revision: str | None = None
) -> str:
    source_sha(base)
    source_sha(head)
    ancestor = git(root, "merge-base", base, head)
    changes = git(
        root, "diff", "--name-status", "--no-renames", ancestor, head
    ).splitlines()
    if not changes:
        raise PolicyError("image PR has no changes")
    changed_paths = set()
    for change in changes:
        status, separator, path = change.partition("\t")
        if not separator or status != "M" or path not in policy["files"]:
            raise PolicyError("PR contains an unexpected file change")
        changed_paths.add(path)
    if changed_paths != set(policy["files"]):
        raise PolicyError("PR must update the complete allowed release file set")
    for path, fields in policy["files"].items():
        for commit in (ancestor, head):
            if not git(root, "ls-tree", commit, "--", path).startswith("100644 blob "):
                raise PolicyError(
                    "release values must remain regular non-executable files"
                )
        before = yaml_values(git(root, "show", f"{ancestor}:{path}"))
        after = yaml_values(git(root, "show", f"{head}:{path}"))
        if revision is None:
            parent, key = value_parent(after, fields[0])
            revision = source_sha(parent[key])
        validate_values(before, after, fields, revision)
    return source_sha(revision)


def validate_pr(pr: dict, config: dict, policy: dict, head: str) -> None:
    repo = config["repository"]
    if pr.get("state") != "open" or pr.get("draft") or pr.get("merged"):
        raise PolicyError("PR must be open, unmerged and ready for review")
    if (
        pr["base"]["repo"]["full_name"] != repo
        or (pr["head"].get("repo") or {}).get("full_name") != repo
        or pr["base"]["ref"] != config["baseBranch"]
        or pr["head"]["sha"] != head
        or not matches(policy, pr["head"]["ref"])
    ):
        raise PolicyError("PR repository, branch, base or expected head does not match")


def validate_protection(
    repository: dict, protection: dict, required: list[str]
) -> None:
    if not repository.get("allow_auto_merge") or not repository.get(
        "allow_squash_merge"
    ):
        raise PolicyError(
            "repository auto-merge and squash merge must be enabled first"
        )
    if protection.get("protected") is not True:
        raise PolicyError("the target branch must remain protected")
    branch_protection = protection.get("protection") or {}
    required_status = branch_protection.get("required_status_checks") or {}
    if required_status.get("enforcement_level") != "everyone":
        raise PolicyError("required checks must apply to administrators too")
    checks = required_status.get("checks", [])
    present = {item["context"] for item in checks if item.get("app_id") == ACTIONS_APP}
    missing = set(required) - present
    if missing:
        raise PolicyError(
            "missing required GitHub Actions checks: " + ", ".join(sorted(missing))
        )


def check_pr_event(root: Path, event: dict) -> dict:
    config = load_policy(root)
    pr = event.get("pull_request")
    if not pr:
        return {"status": "not-an-image-pr"}
    matched = [
        (name, policy)
        for name, policy in config["policies"].items()
        if matches(policy, pr["head"]["ref"])
    ]
    if not matched:
        return {"status": "not-an-image-pr"}
    if len(matched) != 1:
        raise PolicyError("image policy must match exactly once")
    name, policy = matched[0]
    if policy.get("enabled") is not True:
        raise PolicyError("image policy is disabled; cancel queued auto-merge")
    validate_pr(pr, config, policy, source_sha(pr["head"]["sha"]))
    # Read Git objects only; never execute or check out candidate files.
    git(root, "fetch", "--no-tags", "origin", pr["head"]["sha"])
    revision = validate_diff(root, pr["base"]["sha"], pr["head"]["sha"], policy)
    return {"status": "image-scope-passed", "policy": name, "source_sha": revision}


def enable(args: argparse.Namespace) -> dict:
    root = args.repo_root.resolve()
    config = load_policy(root)
    policy = config["policies"].get(args.policy)
    if not policy or policy.get("enabled") is not True:
        raise PolicyError("image policy is absent or disabled; PR remains manual")
    if (
        args.source_repository != policy["sourceRepository"]
        or args.source_ref != policy["sourceRef"]
        or args.source_event != "push"
        or args.build_result != "success"
    ):
        raise PolicyError(
            "only a successful new build on the opted-in branch is eligible"
        )
    revision, head = source_sha(args.source_sha), source_sha(args.expected_head)
    if git(root, "rev-parse", "HEAD") != head:
        raise PolicyError("local release checkout differs from the expected PR head")
    prefix = "https://github.com/" + config["repository"] + "/pull/"
    if not args.pr_url or not re.fullmatch(
        re.escape(prefix) + r"[1-9][0-9]*", args.pr_url
    ):
        raise PolicyError("PR URL must identify the configured repository")
    endpoint = "repos/" + config["repository"]
    number = args.pr_url.removeprefix(prefix)
    pr = api(f"{endpoint}/pulls/{number}")
    validate_pr(pr, config, policy, head)
    parents = git(root, "rev-list", "--parents", "-n", "1", head).split()
    if len(parents) != 2 or int(pr["commits"]) != 1:
        raise PolicyError("generated PR must contain exactly one release commit")
    validate_diff(root, parents[1], head, policy, revision)
    validate_protection(
        api(endpoint),
        api(f"{endpoint}/branches/{config['baseBranch']}"),
        config["requiredChecks"],
    )
    receipt = {
        "status": "validated",
        "policy": args.policy,
        "pr_url": args.pr_url,
        "head_sha": head,
        "source_sha": revision,
        "applied": False,
    }
    if args.apply:
        validate_pr(api(f"{endpoint}/pulls/{number}"), config, policy, head)
        run(
            [
                "gh",
                "pr",
                "merge",
                args.pr_url,
                "--repo",
                config["repository"],
                "--auto",
                "--squash",
                "--match-head-commit",
                head,
            ]
        )
        observed = api(f"{endpoint}/pulls/{number}")
        if observed["head"]["sha"] != head or not (
            observed.get("auto_merge") or observed.get("merged")
        ):
            raise PolicyError("auto-merge activation was not confirmed; inspect the PR")
        receipt.update(
            status="merged" if observed.get("merged") else "auto-merge-enabled",
            applied=True,
        )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--check-pr-event", type=Path)
    for field in (
        "policy",
        "pr-url",
        "expected-head",
        "source-repository",
        "source-ref",
        "source-event",
        "source-sha",
        "build-result",
    ):
        parser.add_argument("--" + field)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.check_pr_event:
            if args.apply:
                parser.error("CI scope checks cannot enable auto-merge")
            result = check_pr_event(
                args.repo_root.resolve(), json.loads(args.check_pr_event.read_text())
            )
        else:
            result = enable(args)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (PolicyError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Image PR remains subject to manual handling: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
