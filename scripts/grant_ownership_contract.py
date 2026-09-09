"""Read the two supported applier layouts without importing workload code."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

COMMON_PATH = Path("scripts/release_applier_common.py")


def read_contract_literals(
    repo: Path, entrypoint: Path, names: frozenset[str]
) -> dict[str, Any]:
    source = repo / entrypoint
    module = _read_module(repo, source)
    values = _constants(module, source, names)
    common = repo / COMMON_PATH
    if common.exists():
        shared = _constants(_read_module(repo, common), common, names)
        if values and shared:
            raise ValueError("ambiguous applier contract: constants appear in both supported layouts")
        if shared:
            if not any(
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module == "scripts.release_applier_common"
                for node in module.body
            ):
                raise ValueError("split applier contract requires the entrypoint's explicit common import")
            source, values = common, shared
    missing = sorted(names.difference(values))
    if missing:
        raise ValueError(
            f"{source} is missing expected applier contract constant(s): " + ", ".join(missing)
        )
    return values


def _read_module(repo: Path, source: Path) -> ast.Module:
    if not source.resolve().is_relative_to(repo.resolve()):
        raise ValueError("applier contract source must remain inside the explicit checkout")
    try:
        return ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"cannot read the agent-workloads applier contract at {source}: {exc}") from exc


def _constants(module: ast.Module, source: Path, names: frozenset[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for node in module.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in names:
                continue
            if target.id in values:
                raise ValueError(f"{source} repeats applier contract constant {target.id}")
            values[target.id] = _literal(node.value, source, target.id)
    return values


def _literal(node: ast.expr | None, source: Path, name: str) -> Any:
    try:
        value = ast.literal_eval(node)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} applier contract constant {name} must be a literal value") from exc
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        items = [ast.literal_eval(element) for element in node.elts]
        if any(item in items[:index] for index, item in enumerate(items)):
            raise ValueError(
                f"{source} applier contract constant {name} must not contain duplicate literal values"
            )
    return value
