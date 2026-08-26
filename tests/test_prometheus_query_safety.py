from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from ruamel.yaml import YAML

from scripts import query_prometheus_history as history
from scripts import calculate_prometheus_history_ratio as ratio
from scripts import verify_prometheus_query_safety as reproduction
from scripts import validate_prometheus_config as validator


REPO_ROOT = Path(__file__).resolve().parents[1]
VALUES_PATH = REPO_ROOT / "helm" / "garz-observability" / "values.yaml"
RULES_TEMPLATE_PATH = (
    REPO_ROOT
    / "helm"
    / "garz-observability"
    / "templates"
    / "monitoring-prometheus-rules-configmap.yaml"
)
RULE_TEST_PATH = (
    REPO_ROOT
    / "helm"
    / "garz-observability"
    / "tests"
    / "prometheus-query-safety.test.yaml"
)
RUNBOOK_PATH = (
    REPO_ROOT / "docs" / "runbooks" / "prometheus-historical-query-safety.md"
)
YAML_PARSER = YAML(typ="safe")


def _api_payload(*, result_count: int = 1, peak_samples: int = 4033):
    result = [
        {
            "metric": {"__name__": reproduction.RECORDED_QUERY},
            "values": [[0, "1"], [300, "2"]],
        }
        for _ in range(result_count)
    ]
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": result,
            "stats": {
                "samples": {
                    "totalQueryableSamples": peak_samples,
                    "peakSamples": peak_samples,
                }
            },
        },
    }


class PrometheusQuerySafetyContractTests(unittest.TestCase):
    def test_guardrail_constants_cannot_drift(self) -> None:
        values = YAML_PARSER.load(VALUES_PATH.read_text(encoding="utf-8"))
        prometheus = values["monitoring"]["prometheus"]

        self.assertEqual(int(prometheus["query"]["maxSamples"]), 500_000)
        self.assertEqual(history.SERVER_MAX_SAMPLES, 500_000)
        self.assertEqual(reproduction.GUARDED_MAX_SAMPLES, 500_000)
        self.assertEqual(prometheus["query"]["maxConcurrency"], 2)
        self.assertEqual(reproduction.GUARDED_MAX_CONCURRENCY, 2)
        self.assertEqual(prometheus["query"]["timeout"], "30s")
        self.assertEqual(reproduction.GUARDED_QUERY_TIMEOUT, "30s")
        self.assertEqual(
            prometheus["querySafety"],
            {
                "recordingInterval": "15s",
                "maxSeriesPerRule": 20,
                "memoryPressureThresholdBytes": "1610612736",
            },
        )
        self.assertEqual(prometheus["resources"]["limits"]["memory"], "2Gi")
        self.assertEqual(reproduction.PRODUCTION_MEMORY_LIMIT, "2g")
        self.assertEqual(
            reproduction.PRODUCTION_MEMORY_LIMIT_BYTES,
            2 * 1024 * 1024 * 1024,
        )
        self.assertEqual(
            reproduction.PRODUCTION_GO_MEMORY_LIMIT,
            prometheus["goMemoryLimit"],
        )
        self.assertEqual(
            f"{prometheus['image']['repository']}:{prometheus['image']['tag']}",
            reproduction.PROMETHEUS_IMAGE,
        )
        self.assertEqual(history.MAX_REVIEWED_PEAK_SAMPLES, 10_000)
        self.assertEqual(
            history.MAX_RECORDING_SAMPLE_AGE_SECONDS,
            2 * reproduction.NATIVE_RECORDING_INTERVAL_SECONDS,
        )
        self.assertEqual(
            history.MAX_RANGE_SECONDS // history.MIN_STEP_SECONDS + 1,
            4033,
        )
        self.assertLessEqual(4033, 11_000)

    def test_supported_metrics_are_shipped_and_semantically_tested(self) -> None:
        template = RULES_TEMPLATE_PATH.read_text(encoding="utf-8")
        recording_section = template.split(
            "query-safety-recording-rules.yaml: |", 1
        )[1].split("prometheus-self-alerts.yaml: |", 1)[0]
        shipped_records = {
            line.split("record:", 1)[1].strip()
            for line in recording_section.splitlines()
            if "- record:" in line
        }
        rule_tests = YAML_PARSER.load(RULE_TEST_PATH.read_text(encoding="utf-8"))
        tested_records = {
            case["expr"]
            for test in rule_tests["tests"]
            for case in test.get("promql_expr_test", [])
        }

        self.assertEqual(shipped_records, set(history.SUPPORTED_METRICS))
        self.assertEqual(tested_records, shipped_records)
        self.assertEqual(reproduction.RECORDED_QUERY in shipped_records, True)
        self.assertEqual(len(shipped_records), 12)
        for metric_pair in ratio.RATIO_CONTRACTS:
            self.assertTrue(set(metric_pair).issubset(shipped_records))
        self.assertNotIn("[14d", recording_section)
        self.assertIn('phase=~"Pending|Running|Unknown"', recording_section)
        self.assertIn("max_over_time", recording_section)

    def test_every_self_alert_has_a_promtool_expectation(self) -> None:
        template = RULES_TEMPLATE_PATH.read_text(encoding="utf-8")
        alert_section = template.split("prometheus-self-alerts.yaml: |", 1)[1]
        shipped_alerts = {
            line.split("alert:", 1)[1].strip()
            for line in alert_section.splitlines()
            if "- alert:" in line
        }
        rule_tests = YAML_PARSER.load(RULE_TEST_PATH.read_text(encoding="utf-8"))
        tested_alerts = {
            case["alertname"]
            for test in rule_tests["tests"]
            for case in test.get("alert_rule_test", [])
        }
        self.assertEqual(tested_alerts, shipped_alerts)

    def test_contract_accepts_only_exact_bounded_selectors(self) -> None:
        metric = (
            "namespace_container:"
            "citrus_container_memory_working_set_bytes:max_over_time5m"
        )
        valid = {"namespace": "default", "container": "web"}
        history.validate_contract(
            metric=metric,
            labels=valid,
            start=0,
            end=history.MAX_RANGE_SECONDS,
            step_seconds=history.MIN_STEP_SECONDS,
        )

        invalid_cases = (
            ({"namespace": "default"}, 0, history.MAX_RANGE_SECONDS, 300),
            ({**valid, "pod": "wildcard"}, 0, history.MAX_RANGE_SECONDS, 300),
            ({"namespace": "other", "container": "web"}, 0, 300, 300),
            (valid, 0, history.MAX_RANGE_SECONDS + 1, 300),
            (valid, 300, 300, 300),
            (valid, 0, 300, 240),
            (valid, 0, 600, 600),
        )
        for labels, start, end, step in invalid_cases:
            with self.subTest(labels=labels, start=start, end=end, step=step):
                with self.assertRaises(ValueError):
                    history.validate_contract(
                        metric=metric,
                        labels=labels,
                        start=start,
                        end=end,
                        step_seconds=step,
                    )

    def test_timestamp_labels_and_local_url_validation_fail_closed(self) -> None:
        for value in ("nan", "inf", "-inf", "not-a-time"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                history.parse_timestamp(value)
        self.assertEqual(history.parse_timestamp("1970-01-01T00:05:00Z"), 300)
        self.assertEqual(
            history.parse_labels(["namespace=default", "container=web"]),
            {"namespace": "default", "container": "web"},
        )
        for labels in (["namespace=*"], ["namespace=default", "namespace=default"]):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                history.parse_labels(labels)
        self.assertEqual(
            history.validate_base_url("http://127.0.0.1:9090/"),
            "http://127.0.0.1:9090",
        )
        for url in (
            "https://prometheus.example.com",
            "http://user:password@localhost:9090",
            "http://localhost:9090/prometheus",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                history.validate_base_url(url)

    def test_history_request_is_one_exact_selector_with_stats(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return io.BytesIO(json.dumps(_api_payload()).encode("utf-8"))

        selector = reproduction.RECORDED_QUERY
        with mock.patch.object(history.urllib.request, "urlopen", fake_urlopen):
            payload = history.request_history(
                base_url="http://127.0.0.1:9090",
                selector=selector,
                start=0,
                end=300,
                step_seconds=300,
            )

        request = captured["request"]
        self.assertEqual(request.full_url, "http://127.0.0.1:9090/api/v1/query_range")
        parameters = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(parameters["query"], [selector])
        self.assertEqual(parameters["step"], ["300"])
        self.assertEqual(parameters["timeout"], ["30s"])
        self.assertEqual(parameters["lookback_delta"], ["30s"])
        self.assertEqual(parameters["stats"], ["all"])
        self.assertNotIn("limit", parameters)
        self.assertEqual(captured["timeout"], 35)
        self.assertEqual(payload["status"], "success")

    def test_history_request_rejects_cardinality_and_peak_drift(self) -> None:
        for payload in (
            _api_payload(result_count=0),
            _api_payload(result_count=2),
            _api_payload(peak_samples=history.MAX_REVIEWED_PEAK_SAMPLES + 1),
        ):
            with self.subTest(payload=payload), mock.patch.object(
                history.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode("utf-8")),
            ), self.assertRaises(RuntimeError):
                history.request_history(
                    base_url="http://127.0.0.1:9090",
                    selector=reproduction.RECORDED_QUERY,
                    start=0,
                    end=300,
                    step_seconds=300,
                )

    def test_receipt_aggregates_offline_and_rejects_empty_data(self) -> None:
        payload = _api_payload()
        payload["data"]["result"][0]["values"] = [
            [0, "1"],
            [300, "2"],
            [600, "3"],
            [900, "4"],
            [1200, "5"],
        ]
        receipt = history.build_receipt(
            metric=reproduction.RECORDED_QUERY,
            selector=reproduction.RECORDED_QUERY,
            labels={},
            start=0,
            end=1200,
            step_seconds=300,
            payload=payload,
        )
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["contract"]["aggregation"], "offline")
        self.assertEqual(receipt["summary"]["sample_count"], 5)
        self.assertEqual(receipt["summary"]["minimum"], 1)
        self.assertAlmostEqual(receipt["summary"]["p95"], 4.8)
        self.assertAlmostEqual(receipt["summary"]["p99"], 4.96)
        self.assertEqual(receipt["summary"]["maximum"], 5)
        self.assertEqual(receipt["completeness"]["missing_sample_count"], 0)
        self.assertEqual(len(receipt["points"]), 5)

        payload["data"]["result"][0]["values"] = [
            [0, "1"],
            [300, "NaN"],
        ]
        with self.assertRaises(RuntimeError):
            history.build_receipt(
                metric=reproduction.RECORDED_QUERY,
                selector=reproduction.RECORDED_QUERY,
                labels={},
                start=0,
                end=300,
                step_seconds=300,
                payload=payload,
            )

    def test_receipt_rejects_incomplete_or_misaligned_history(self) -> None:
        for points in (
            [[0, "1"]],
            [[0, "1"], [301, "2"]],
        ):
            payload = _api_payload()
            payload["data"]["result"][0]["values"] = points
            with self.subTest(points=points), self.assertRaises(RuntimeError):
                history.build_receipt(
                    metric=reproduction.RECORDED_QUERY,
                    selector=reproduction.RECORDED_QUERY,
                    labels={},
                    start=0,
                    end=300,
                    step_seconds=300,
                    payload=payload,
                )

    def test_reviewed_ratio_is_calculated_from_aligned_points(self) -> None:
        def receipt(metric: str, values: list[float]):
            payload = _api_payload()
            payload["data"]["result"][0]["metric"] = {"__name__": metric}
            payload["data"]["result"][0]["values"] = [
                [index * 300, str(value)] for index, value in enumerate(values)
            ]
            return history.build_receipt(
                metric=metric,
                selector=metric,
                labels={},
                start=0,
                end=(len(values) - 1) * 300,
                step_seconds=300,
                payload=payload,
            )

        result = ratio.calculate_ratio(
            receipt(ratio.CLUSTER_USAGE, [1, 2, 3]),
            receipt(ratio.CLUSTER_ALLOCATABLE, [2, 4, 6]),
        )
        self.assertEqual(result["contract"]["sample_count"], 3)
        self.assertEqual(result["summary"]["p95"], 0.5)
        self.assertEqual(
            [point["value"] for point in result["points"]],
            [0.5, 0.5, 0.5],
        )

    def test_ratio_rejects_unsupported_or_misaligned_receipts(self) -> None:
        def receipt(metric: str):
            payload = _api_payload()
            payload["data"]["result"][0]["metric"] = {"__name__": metric}
            return history.build_receipt(
                metric=metric,
                selector=metric,
                labels={},
                start=0,
                end=300,
                step_seconds=300,
                payload=payload,
            )

        usage = receipt(ratio.CLUSTER_USAGE)
        allocatable = receipt(ratio.CLUSTER_ALLOCATABLE)
        allocatable["points"][1]["timestamp"] = 301
        with self.assertRaises(ValueError):
            ratio.calculate_ratio(usage, allocatable)

        usage["points"][1]["timestamp"] = 301
        with self.assertRaises(ValueError):
            ratio.calculate_ratio(usage, allocatable)

        with self.assertRaises(ValueError):
            ratio.calculate_ratio(
                receipt(ratio.CLUSTER_ALLOCATABLE),
                receipt(ratio.CLUSTER_USAGE),
            )

        with self.assertRaises(ValueError):
            ratio.calculate_ratio(
                receipt(
                    "namespace_container:"
                    "citrus_container_cpu_usage_seconds_total:max_rate5m"
                ),
                receipt(
                    "namespace_container:"
                    "citrus_kube_pod_container_resource_requests_cpu_cores:"
                    "max_over_time5m"
                ),
            )

    def test_dry_run_never_opens_the_network(self) -> None:
        argv = [
            "query_prometheus_history.py",
            "--metric",
            reproduction.RECORDED_QUERY,
            "--start",
            "0",
            "--end",
            "300",
            "--dry-run",
        ]
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            history.urllib.request,
            "urlopen",
            side_effect=AssertionError("dry-run attempted network access"),
        ), contextlib.redirect_stdout(stdout):
            history.main()
        self.assertEqual(json.loads(stdout.getvalue())["dry_run"], True)

    def test_promtool_fixture_discovery_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            chart = Path(tempdir)
            tests = chart / "tests"
            tests.mkdir()
            (tests / "b.test.yaml").write_text("b\n", encoding="utf-8")
            (tests / "a.test.yaml").write_text("a\n", encoding="utf-8")
            (tests / "ignored.yaml").write_text("ignored\n", encoding="utf-8")
            self.assertEqual(
                validator.load_rule_test_files(str(chart)),
                {"a.test.yaml": "a\n", "b.test.yaml": "b\n"},
            )

    def test_runbook_is_discoverable_and_preserves_the_safety_gate(self) -> None:
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
        for phrase in (
            "one query at a time",
            "exact labels",
            "fixed step of 5m",
            "maximum range of 14 days",
            "offline",
            "do not backfill",
            "do not run raw historical joins",
            "separate authorization",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runbook.lower())
        self.assertIn(f"{reproduction.DEFAULT_POD_COUNT}-pod", runbook)
        for index_path in (
            REPO_ROOT / "README.md",
            REPO_ROOT / "docs" / "README.md",
            REPO_ROOT / "helm" / "garz-observability" / "README.md",
        ):
            self.assertIn(
                "prometheus-historical-query-safety.md",
                index_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
