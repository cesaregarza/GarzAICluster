#!/usr/bin/env python3
"""Query one bounded Prometheus recording series and summarize it offline."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_RANGE_SECONDS = 14 * 24 * 60 * 60
MIN_STEP_SECONDS = 5 * 60
MAX_QUERY_TIMEOUT_SECONDS = 30
SERVER_MAX_SAMPLES = 500_000
MAX_REVIEWED_PEAK_SAMPLES = 10_000
LABEL_VALUE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
DURATION_PATTERN = re.compile(r"([1-9][0-9]*)([smh])")


@dataclass(frozen=True)
class MetricContract:
    required_labels: tuple[str, ...]
    allowed_namespaces: tuple[str, ...] = ()


SUPPORTED_METRICS: dict[str, MetricContract] = {
    "cluster:container_cpu_usage_seconds_total:sum_rate5m": MetricContract(()),
    "cluster:kube_pod_container_resource_requests_cpu_cores:max_sum_active_bound5m": (
        MetricContract(())
    ),
    "cluster:kube_node_status_allocatable_cpu_cores:sum": MetricContract(()),
    "node:kube_pod_container_resource_requests_cpu_cores:max_sum_active_bound5m": (
        MetricContract(("node",))
    ),
    "node:kube_node_status_allocatable_cpu_cores:max": MetricContract(("node",)),
    "namespace_container:citrus_container_cpu_usage_seconds_total:max_rate5m": (
        MetricContract(("namespace", "container"), ("default", "citrus-dev"))
    ),
    "namespace_container:citrus_container_memory_working_set_bytes:max_over_time5m": (
        MetricContract(("namespace", "container"), ("default", "citrus-dev"))
    ),
    "namespace_container:citrus_kube_pod_container_resource_requests_cpu_cores:max_over_time5m": (
        MetricContract(("namespace", "container"), ("default", "citrus-dev"))
    ),
    "namespace_container:citrus_kube_pod_container_resource_requests_memory_bytes:max_over_time5m": (
        MetricContract(("namespace", "container"), ("default", "citrus-dev"))
    ),
    "namespace:citrus_kube_pod_scheduling_latency_seconds:p95_over_pods5m": MetricContract(
        ("namespace",), ("default", "citrus-dev")
    ),
    "namespace:citrus_kube_pod_scheduling_latency_seconds:max_over_pods5m": MetricContract(
        ("namespace",), ("default", "citrus-dev")
    ),
    "namespace:citrus_kube_pod_pending_age_seconds:max_over_time5m": MetricContract(
        ("namespace",), ("default", "citrus-dev")
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:9090",
        help="Prometheus base URL, normally a local port-forward.",
    )
    parser.add_argument("--metric", required=True, choices=sorted(SUPPORTED_METRICS))
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Exact label required by the selected metric; repeat as needed.",
    )
    parser.add_argument("--start", required=True, help="RFC3339 or Unix timestamp.")
    parser.add_argument("--end", required=True, help="RFC3339 or Unix timestamp.")
    parser.add_argument("--step", default="5m", help="Reviewed range step; exactly 5m.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the JSON receipt to this path instead of stdout.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the request contract without network access.",
    )
    return parser.parse_args()


def parse_timestamp(value: str) -> float:
    try:
        parsed_number = float(value)
    except ValueError:
        pass
    else:
        if not math.isfinite(parsed_number):
            raise ValueError(f"invalid timestamp: {value}")
        return parsed_number

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value}")
    return parsed.timestamp()


def parse_duration(value: str) -> int:
    match = DURATION_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"invalid duration: {value}")
    amount = int(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    return amount * multiplier


def parse_labels(values: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for value in values:
        name, separator, label_value = value.partition("=")
        if not separator or not name or not LABEL_VALUE_PATTERN.fullmatch(label_value):
            raise ValueError(f"invalid exact label: {value}")
        if name in labels:
            raise ValueError(f"duplicate label: {name}")
        labels[name] = label_value
    return labels


def validate_contract(
    *,
    metric: str,
    labels: dict[str, str],
    start: float,
    end: float,
    step_seconds: int,
) -> None:
    contract = SUPPORTED_METRICS[metric]
    if set(labels) != set(contract.required_labels):
        raise ValueError(
            f"{metric} requires exact labels: {', '.join(contract.required_labels) or '(none)'}"
        )
    if contract.allowed_namespaces and labels["namespace"] not in set(
        contract.allowed_namespaces
    ):
        raise ValueError(
            "namespace must be one of: " + ", ".join(contract.allowed_namespaces)
        )
    if end <= start:
        raise ValueError("--end must be later than --start")
    if end - start > MAX_RANGE_SECONDS:
        raise ValueError("history range must not exceed 14 days")
    if step_seconds != MIN_STEP_SECONDS:
        raise ValueError("history step must be exactly 5m")


def validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--url must be an http(s) Prometheus base URL")
    if parsed.username or parsed.password:
        raise ValueError("--url must not include credentials")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("--url must point to a local Prometheus port-forward")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("--url must be a base URL without a path, query, or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid --url port: {value}") from exc
    return value.rstrip("/")


def build_selector(metric: str, labels: dict[str, str]) -> str:
    if not labels:
        return metric
    matchers = ",".join(
        f"{name}={json.dumps(value)}" for name, value in sorted(labels.items())
    )
    return f"{metric}{{{matchers}}}"


def request_history(
    *,
    base_url: str,
    selector: str,
    start: float,
    end: float,
    step_seconds: int,
) -> dict[str, Any]:
    body = urllib.parse.urlencode(
        {
            "query": selector,
            "start": start,
            "end": end,
            "step": step_seconds,
            "timeout": f"{MAX_QUERY_TIMEOUT_SECONDS}s",
            "stats": "all",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/v1/query_range",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "garzaicluster-prometheus-safe-history/1",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=MAX_QUERY_TIMEOUT_SECONDS + 5
        ) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            error = json.load(exc)
        except json.JSONDecodeError:
            error = {"status": "error", "error": str(exc)}
        raise RuntimeError(
            f"Prometheus rejected the bounded query: HTTP {exc.code} {error}"
        ) from exc

    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    result = payload["data"]["result"]
    if len(result) != 1:
        raise RuntimeError(f"expected exactly one recorded series, received {len(result)}")
    validate_points(
        result[0].get("values", []),
        start=start,
        end=end,
        step_seconds=step_seconds,
    )
    peak_samples = int(payload["data"]["stats"]["samples"]["peakSamples"])
    if peak_samples > MAX_REVIEWED_PEAK_SAMPLES:
        raise RuntimeError(
            "query exceeded the reviewed "
            f"{MAX_REVIEWED_PEAK_SAMPLES}-sample peak: {peak_samples}"
        )
    return payload


def linear_quantile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a quantile without values")
    ordered = sorted(values)
    rank = quantile * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def expected_sample_count(*, start: float, end: float, step_seconds: int) -> int:
    return math.floor((end - start) / step_seconds) + 1


def validate_points(
    raw_points: list[list[Any]],
    *,
    start: float,
    end: float,
    step_seconds: int,
) -> list[tuple[float, float]]:
    expected_count = expected_sample_count(
        start=start,
        end=end,
        step_seconds=step_seconds,
    )
    if len(raw_points) != expected_count:
        raise RuntimeError(
            "recorded history is incomplete: expected "
            f"{expected_count} points, received {len(raw_points)}"
        )

    points: list[tuple[float, float]] = []
    for index, raw_point in enumerate(raw_points):
        if len(raw_point) != 2:
            raise RuntimeError(f"invalid point at index {index}: {raw_point}")
        timestamp = float(raw_point[0])
        value = float(raw_point[1])
        expected_timestamp = start + index * step_seconds
        if not math.isfinite(timestamp) or not math.isclose(
            timestamp,
            expected_timestamp,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise RuntimeError(
                "recorded history timestamp gap at index "
                f"{index}: expected {expected_timestamp}, received {timestamp}"
            )
        if not math.isfinite(value):
            raise RuntimeError(
                f"recorded history contains a non-finite value at {timestamp}"
            )
        points.append((timestamp, value))
    return points


def build_receipt(
    *,
    metric: str,
    selector: str,
    labels: dict[str, str],
    start: float,
    end: float,
    step_seconds: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    series = payload["data"]["result"][0]
    points = validate_points(
        series.get("values", []),
        start=start,
        end=end,
        step_seconds=step_seconds,
    )
    values = [value for _, value in points]
    expected_count = expected_sample_count(
        start=start,
        end=end,
        step_seconds=step_seconds,
    )
    return {
        "schema_version": 1,
        "contract": {
            "metric": metric,
            "selector": selector,
            "exact_labels": labels,
            "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            "step_seconds": step_seconds,
            "max_range_seconds": MAX_RANGE_SECONDS,
            "max_reviewed_peak_samples": MAX_REVIEWED_PEAK_SAMPLES,
            "server_max_samples": SERVER_MAX_SAMPLES,
            "aggregation": "offline",
        },
        "series_labels": series["metric"],
        "completeness": {
            "expected_sample_count": expected_count,
            "observed_sample_count": len(points),
            "missing_sample_count": 0,
            "first_timestamp": datetime.fromtimestamp(
                points[0][0], timezone.utc
            ).isoformat(),
            "last_timestamp": datetime.fromtimestamp(
                points[-1][0], timezone.utc
            ).isoformat(),
        },
        "summary": {
            "sample_count": len(values),
            "minimum": min(values),
            "p95": linear_quantile(values, 0.95),
            "p99": linear_quantile(values, 0.99),
            "maximum": max(values),
        },
        "points": [
            {"timestamp": timestamp, "value": value}
            for timestamp, value in points
        ],
        "prometheus_stats": payload["data"]["stats"],
    }


def emit_receipt(receipt: dict[str, Any], output: Path | None) -> None:
    serialized = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(serialized)
        return
    output.write_text(serialized, encoding="utf-8")
    print(f"Wrote bounded history receipt to {output}")


def main() -> None:
    args = parse_args()
    try:
        start = parse_timestamp(args.start)
        end = parse_timestamp(args.end)
        step_seconds = parse_duration(args.step)
        labels = parse_labels(args.label)
        base_url = validate_base_url(args.url)
        validate_contract(
            metric=args.metric,
            labels=labels,
            start=start,
            end=end,
            step_seconds=step_seconds,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    selector = build_selector(args.metric, labels)
    if args.dry_run:
        emit_receipt(
            {
                "dry_run": True,
                "metric": args.metric,
                "selector": selector,
                "start": start,
                "end": end,
                "step_seconds": step_seconds,
            },
            args.output,
        )
        return

    payload = request_history(
        base_url=base_url,
        selector=selector,
        start=start,
        end=end,
        step_seconds=step_seconds,
    )
    receipt = build_receipt(
        metric=args.metric,
        selector=selector,
        labels=labels,
        start=start,
        end=end,
        step_seconds=step_seconds,
        payload=payload,
    )
    emit_receipt(receipt, args.output)


if __name__ == "__main__":
    main()
