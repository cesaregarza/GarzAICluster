#!/usr/bin/env python3
"""Calculate one reviewed right-sizing ratio from two bounded receipts offline."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.query_prometheus_history import (
        linear_quantile,
        parse_timestamp,
        validate_points,
    )
except ModuleNotFoundError:  # Direct execution adds scripts/, not its parent.
    from query_prometheus_history import (
        linear_quantile,
        parse_timestamp,
        validate_points,
    )


CLUSTER_USAGE = "cluster:container_cpu_usage_seconds_total:sum_rate5m"
CLUSTER_REQUEST = (
    "cluster:kube_pod_container_resource_requests_cpu_cores:"
    "max_sum_active_bound5m"
)
CLUSTER_ALLOCATABLE = "cluster:kube_node_status_allocatable_cpu_cores:sum"
NODE_REQUEST = (
    "node:kube_pod_container_resource_requests_cpu_cores:"
    "max_sum_active_bound5m"
)
NODE_ALLOCATABLE = "node:kube_node_status_allocatable_cpu_cores:max"

RATIO_CONTRACTS = {
    (CLUSTER_USAGE, CLUSTER_ALLOCATABLE): "cluster_cpu_usage_to_allocatable",
    (CLUSTER_REQUEST, CLUSTER_ALLOCATABLE): "cluster_active_request_to_allocatable",
    (NODE_REQUEST, NODE_ALLOCATABLE): "node_active_request_to_allocatable",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--numerator", type=Path, required=True)
    parser.add_argument("--denominator", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the ratio receipt to this path instead of stdout.",
    )
    return parser.parse_args()


def load_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read receipt {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"unsupported receipt schema in {path}")
    return payload


def _validated_points(receipt: dict[str, Any], label: str) -> list[tuple[float, float]]:
    contract = receipt.get("contract", {})
    completeness = receipt.get("completeness", {})
    points = receipt.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError(f"{label} receipt has no bounded points")
    if (
        completeness.get("expected_sample_count") != len(points)
        or completeness.get("observed_sample_count") != len(points)
        or completeness.get("missing_sample_count") != 0
    ):
        raise ValueError(f"{label} receipt is incomplete")

    raw_points: list[list[Any]] = []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError(f"{label} point {index} is invalid")
        try:
            timestamp = float(point["timestamp"])
            value = float(point["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{label} point {index} is invalid") from exc
        if not math.isfinite(timestamp) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{label} point {index} is not a finite nonnegative value")
        raw_points.append([timestamp, value])
    try:
        return validate_points(
            raw_points,
            start=parse_timestamp(str(contract["start"])),
            end=parse_timestamp(str(contract["end"])),
            step_seconds=int(contract["step_seconds"]),
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise ValueError(f"{label} receipt point coverage is invalid: {exc}") from exc


def calculate_ratio(
    numerator: dict[str, Any],
    denominator: dict[str, Any],
) -> dict[str, Any]:
    numerator_contract = numerator.get("contract", {})
    denominator_contract = denominator.get("contract", {})
    metric_pair = (
        numerator_contract.get("metric"),
        denominator_contract.get("metric"),
    )
    try:
        ratio_name = RATIO_CONTRACTS[metric_pair]
    except KeyError as exc:
        raise ValueError(f"unsupported receipt metric pair: {metric_pair}") from exc

    contract_fields = ("exact_labels", "start", "end", "step_seconds")
    for field in contract_fields:
        if numerator_contract.get(field) != denominator_contract.get(field):
            raise ValueError(f"receipt contracts differ on {field}")
    if (
        numerator_contract.get("aggregation") != "offline"
        or denominator_contract.get("aggregation") != "offline"
    ):
        raise ValueError("source receipts must use offline aggregation")

    numerator_points = _validated_points(numerator, "numerator")
    denominator_points = _validated_points(denominator, "denominator")
    if len(numerator_points) != len(denominator_points):
        raise ValueError("receipt point counts differ")

    ratio_points: list[dict[str, float]] = []
    ratios: list[float] = []
    for index, ((num_timestamp, num_value), (den_timestamp, den_value)) in enumerate(
        zip(numerator_points, denominator_points, strict=True)
    ):
        if num_timestamp != den_timestamp:
            raise ValueError(f"receipt timestamps differ at point {index}")
        if den_value <= 0:
            raise ValueError(f"denominator is not positive at point {index}")
        ratio = num_value / den_value
        ratios.append(ratio)
        ratio_points.append({"timestamp": num_timestamp, "value": ratio})

    return {
        "schema_version": 1,
        "analysis": "offline_prometheus_history_ratio",
        "contract": {
            "name": ratio_name,
            "numerator_metric": metric_pair[0],
            "denominator_metric": metric_pair[1],
            "exact_labels": numerator_contract["exact_labels"],
            "start": numerator_contract["start"],
            "end": numerator_contract["end"],
            "step_seconds": numerator_contract["step_seconds"],
            "sample_count": len(ratios),
        },
        "summary": {
            "minimum": min(ratios),
            "p95": linear_quantile(ratios, 0.95),
            "p99": linear_quantile(ratios, 0.99),
            "maximum": max(ratios),
        },
        "points": ratio_points,
    }


def emit_result(result: dict[str, Any], output: Path | None) -> None:
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(serialized)
        return
    output.write_text(serialized, encoding="utf-8")
    print(f"Wrote offline ratio receipt to {output}")


def main() -> None:
    args = parse_args()
    try:
        result = calculate_ratio(
            load_receipt(args.numerator),
            load_receipt(args.denominator),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    emit_result(result, args.output)


if __name__ == "__main__":
    main()
