"""Statically read agent-platform output projection contracts."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProjectionContractError(ValueError):
    """Raised when pinned projection declarations cannot be validated."""


@dataclass(frozen=True)
class ProjectionContract:
    """Output fields exposed by pinned Core projection declarations."""

    source_label: str
    legacy_fields_by_projection: Mapping[str, frozenset[str]]
    generic_projection_id: str | None
    compatibility_projection_ids: frozenset[str]
    registered_schema_fields: Mapping[str, frozenset[str]]

    @property
    def modern(self) -> bool:
        return self.generic_projection_id is not None

    @property
    def card_projection_ids(self) -> frozenset[str]:
        ids = set(self.compatibility_projection_ids)
        if self.generic_projection_id is not None:
            ids.add(self.generic_projection_id)
        return frozenset(ids)

    def result_fields(
        self,
        *,
        capability_id: str,
        capability: Mapping[str, Any],
        projection_id: str,
    ) -> frozenset[str] | None:
        """Resolve a capability card field contract for its projection."""
        if not self.modern:
            return self.legacy_fields_by_projection.get(projection_id)
        if projection_id not in self.card_projection_ids:
            return None

        configured = capability.get("result_contract")
        has_explicit = (
            isinstance(configured, Mapping)
            and "released_result_fields" in configured
        )
        if has_explicit:
            fields = _validated_fields(
                configured["released_result_fields"], self.source_label, capability_id
            )
            schema_fields = self._schema_fields(capability_id, capability)
            unknown = sorted(set(fields) - schema_fields)
            if unknown:
                raise ProjectionContractError(
                    f"{self.source_label} capability {capability_id!r} releases "
                    f"unregistered result fields {unknown} for schema "
                    f"{capability.get('output_schema')!r}"
                )
            return frozenset(fields)

        if projection_id == self.generic_projection_id:
            raise ProjectionContractError(
                f"{self.source_label} generic projection {projection_id!r} "
                f"requires capability {capability_id!r} "
                "result_contract.released_result_fields"
            )
        return self._schema_fields(capability_id, capability)

    def _schema_fields(
        self, capability_id: str, capability: Mapping[str, Any]
    ) -> frozenset[str]:
        schema = capability.get("output_schema")
        if not isinstance(schema, str) or not schema:
            raise ProjectionContractError(
                f"{self.source_label} compatibility projection for "
                f"{capability_id!r} has no output_schema"
            )
        fields = self.registered_schema_fields.get(schema)
        if fields is None:
            raise ProjectionContractError(
                f"{self.source_label} capability {capability_id!r} references "
                f"unknown registered output schema {schema!r}"
            )
        return fields


def read_projection_contract(path: Path, source_label: str) -> ProjectionContract:
    """Read legacy maps or modern card-driven declarations without importing Core."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ProjectionContractError(f"could not parse {source_label}: {exc}") from exc

    values = _module_assignments(tree)
    legacy = values.get("PUBLIC_RESULT_FIELDS_BY_PROJECTION_ID")
    if isinstance(legacy, Mapping):
        result: dict[str, frozenset[str]] = {}
        for projection_id, fields in legacy.items():
            if (
                isinstance(projection_id, str)
                and isinstance(fields, (set, frozenset, tuple, list))
                and all(isinstance(field, str) for field in fields)
            ):
                result[projection_id] = frozenset(fields)
        return ProjectionContract(source_label, result, None, frozenset(), {})

    generic = values.get("RELEASED_FIELDS_PROJECTION_ID")
    compatibility = values.get("COMPAT_CARD_PROJECTION_IDS")
    if not isinstance(generic, str) or not generic:
        raise ProjectionContractError(
            f"{source_label} does not declare a legacy result-field map or "
            "RELEASED_FIELDS_PROJECTION_ID"
        )
    if not isinstance(compatibility, (set, frozenset, tuple, list)) or not all(
        isinstance(item, str) and item for item in compatibility
    ):
        raise ProjectionContractError(
            f"{source_label} does not declare a valid COMPAT_CARD_PROJECTION_IDS set"
        )
    schemas = _registered_schema_fields(tree, source_label)
    if not schemas:
        raise ProjectionContractError(
            f"{source_label} does not declare registered output schemas"
        )
    return ProjectionContract(
        source_label, {}, generic, frozenset(compatibility), schemas
    )


def _registered_schema_fields(
    tree: ast.Module, source_label: str
) -> dict[str, frozenset[str]]:
    for statement in tree.body:
        name, expression = _assignment(statement)
        if name != "REGISTERED_OUTPUT_SCHEMAS" or not isinstance(expression, ast.Dict):
            continue
        result: dict[str, frozenset[str]] = {}
        for key, value in zip(expression.keys, expression.values, strict=True):
            schema = _constant_string(key)
            if (
                schema is None
                or not isinstance(value, ast.Call)
                or _call_name(value.func) != "RegisteredOutputSchema"
            ):
                raise ProjectionContractError(
                    f"{source_label} REGISTERED_OUTPUT_SCHEMAS contains an invalid entry"
                )
            fields_node = next(
                (
                    keyword.value
                    for keyword in value.keywords
                    if keyword.arg == "field_types"
                ),
                None,
            )
            if not isinstance(fields_node, ast.Dict):
                raise ProjectionContractError(
                    f"{source_label} registered schema {schema!r} "
                    "has no static field_types"
                )
            fields: set[str] = set()
            for field, _field_type in zip(
                fields_node.keys, fields_node.values, strict=True
            ):
                field_name = _constant_string(field)
                if field_name is None:
                    raise ProjectionContractError(
                        f"{source_label} registered schema {schema!r} "
                        "has a non-string field"
                    )
                fields.add(field_name)
            result[schema] = frozenset(fields)
        return result
    return {}


def _validated_fields(
    value: Any, source_label: str, capability_id: str
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ProjectionContractError(
            f"{source_label} capability {capability_id!r} requires a nonempty "
            "released_result_fields list"
        )
    if any(not isinstance(field, str) or not field for field in value):
        raise ProjectionContractError(
            f"{source_label} capability {capability_id!r} has invalid "
            "released_result_fields"
        )
    fields = tuple(value)
    if len(set(fields)) != len(fields):
        raise ProjectionContractError(
            f"{source_label} capability {capability_id!r} has duplicate "
            "released_result_fields"
        )
    return fields


def _module_assignments(
    tree: ast.Module,
    *,
    event_members: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for statement in tree.body:
        name, expression = _assignment(statement)
        if name is None or expression is None:
            continue
        try:
            values[name] = _literal_value(
                expression,
                values=values,
                event_members=event_members or {},
            )
        except ValueError:
            continue
    return values


def _literal_value(
    node: ast.expr,
    *,
    values: Mapping[str, Any],
    event_members: Mapping[str, str],
) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in values:
            return values[node.id]
        raise ValueError(node.id)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        members = [
            _literal_value(item, values=values, event_members=event_members)
            for item in node.elts
        ]
        if isinstance(node, ast.Tuple):
            return tuple(members)
        if isinstance(node, ast.Set):
            return set(members)
        return members
    if isinstance(node, ast.Dict):
        return {
            _literal_value(key, values=values, event_members=event_members): _literal_value(
                value,
                values=values,
                event_members=event_members,
            )
            for key, value in zip(node.keys, node.values, strict=True)
            if key is not None
        }
    if (
        isinstance(node, ast.Call)
        and _call_name(node.func) == "frozenset"
        and len(node.args) == 1
    ):
        return frozenset(
            _literal_value(node.args[0], values=values, event_members=event_members)
        )
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "value"
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "EventType"
    ):
        member = event_members.get(node.value.attr)
        if member is not None:
            return member
    raise ValueError(ast.dump(node))


def _assignment(statement: ast.stmt) -> tuple[str | None, ast.expr | None]:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0].id, statement.value
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id, statement.value
    return None, None


def _constant_string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None
