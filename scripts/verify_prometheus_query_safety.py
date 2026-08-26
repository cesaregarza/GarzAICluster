#!/usr/bin/env python3
"""Verify the bounded Prometheus history path against an isolated 2.52 TSDB."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from scripts.query_prometheus_history import build_receipt, request_history
except ModuleNotFoundError:  # Direct execution adds scripts/, not its parent.
    from query_prometheus_history import build_receipt, request_history

PROMETHEUS_IMAGE = "prom/prometheus:v2.52.0"
INCIDENT_MAX_SAMPLES = 5_000_000
GUARDED_MAX_SAMPLES = 500_000
GUARDED_MAX_CONCURRENCY = 2
GUARDED_QUERY_TIMEOUT = "30s"
PRODUCTION_MEMORY_LIMIT = "2g"
PRODUCTION_MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
PRODUCTION_GO_MEMORY_LIMIT = "1200MiB"
HISTORY_SECONDS = 14 * 24 * 60 * 60
LOOKBACK_SECONDS = 30 * 60
STEP_SECONDS = 5 * 60
NATIVE_RECORDING_INTERVAL_SECONDS = 15
DEFAULT_POD_COUNT = 80
MAX_RECORDED_RANGE_PEAK_SAMPLES = 10_000
MAX_RECORDED_SUBQUERY_PEAK_SAMPLES = 100_000
MIN_RAW_TO_RECORDED_PEAK_RATIO = 100

RAW_JOIN_QUERY = """
sum(
  kube_pod_container_resource_requests{
    job="kube-state-metrics",
    node!="",
    resource="cpu",
    unit="core"
  }
    * on (namespace, pod, uid) group_left()
  max by (namespace, pod, uid) (
    kube_pod_status_phase{
      job="kube-state-metrics",
      phase=~"Pending|Running|Unknown"
    } == 1
  )
)
""".strip()

RAW_SUBQUERY = f"""
quantile_over_time(
  0.95,
  (
    {RAW_JOIN_QUERY}
  )[14d:5m]
)
""".strip()

RECORDED_QUERY = (
    "cluster:kube_pod_container_resource_requests_cpu_cores:"
    "max_sum_active_bound5m"
)
RECORDED_SUBQUERY = f"quantile_over_time(0.95, {RECORDED_QUERY}[14d])"
GAP_PROBE_QUERY = "ces856_recording_gap_probe"


@dataclass(frozen=True)
class FixtureWindow:
    fixture_start: int
    query_start: int
    end: int


@dataclass(frozen=True)
class ApiResult:
    status_code: int
    payload: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pod-count",
        type=int,
        default=DEFAULT_POD_COUNT,
        help=f"Synthetic pod cardinality (default: {DEFAULT_POD_COUNT}).",
    )
    args = parser.parse_args()
    if args.pod_count < 1:
        parser.error("--pod-count must be positive")
    return args


def generate_openmetrics(
    destination: Path,
    *,
    pod_count: int,
) -> FixtureWindow:
    end = int(time.time()) // STEP_SECONDS * STEP_SECONDS - STEP_SECONDS
    query_start = end - HISTORY_SECONDS
    fixture_start = query_start - LOOKBACK_SECONDS

    with destination.open("w", encoding="utf-8", buffering=1024 * 1024) as output:
        output.write("# TYPE kube_pod_container_resource_requests gauge\n")
        for pod in range(pod_count):
            request = 0.10 + (pod % 3) * 0.05
            for timestamp in range(fixture_start, end + 1, STEP_SECONDS):
                output.write(
                    "kube_pod_container_resource_requests"
                    f'{{job="kube-state-metrics",namespace="default",'
                    f'pod="citrus-app-{pod:03d}",uid="uid-{pod:03d}",'
                    f'container="app",node="node-{pod % 4}",'
                    'resource="cpu",unit="core"} '
                    f"{request:.2f} {timestamp}\n"
                )

        output.write("# TYPE kube_pod_status_phase gauge\n")
        for pod in range(pod_count):
            for timestamp in range(fixture_start, end + 1, STEP_SECONDS):
                output.write(
                    "kube_pod_status_phase"
                    f'{{job="kube-state-metrics",namespace="default",'
                    f'pod="citrus-app-{pod:03d}",uid="uid-{pod:03d}",'
                    f'phase="Running"}} 1 {timestamp}\n'
                )

        output.write(
            "# TYPE "
            "cluster:kube_pod_container_resource_requests_cpu_cores:"
            "max_sum_active_bound5m gauge\n"
        )
        recorded_value = sum(0.10 + (pod % 3) * 0.05 for pod in range(pod_count))
        for timestamp in range(
            fixture_start,
            end + 1,
            NATIVE_RECORDING_INTERVAL_SECONDS,
        ):
            output.write(
                "cluster:kube_pod_container_resource_requests_cpu_cores:"
                "max_sum_active_bound5m "
                f"{recorded_value:.9f} {timestamp}\n"
            )

        gap_probe_start = query_start + HISTORY_SECONDS // 2
        output.write(f"# TYPE {GAP_PROBE_QUERY} gauge\n")
        output.write(f"{GAP_PROBE_QUERY} 1 {gap_probe_start - 60}\n")
        output.write(f"{GAP_PROBE_QUERY} 1 {gap_probe_start + STEP_SECONDS}\n")

        output.write("# EOF\n")

    return FixtureWindow(
        fixture_start=fixture_start,
        query_start=query_start,
        end=end,
    )


def run_command(command: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout[-4000:])
        if exc.stderr:
            print(exc.stderr[-4000:])
        raise


def import_fixture(work_dir: Path) -> None:
    container_name = f"ces856-prom-import-{os.getpid()}"
    try:
        run_command(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                container_name,
                "-u",
                f"{os.getuid()}:{os.getgid()}",
                "--entrypoint=promtool",
                "-v",
                f"{work_dir}:/work",
                PROMETHEUS_IMAGE,
                "tsdb",
                "create-blocks-from",
                "openmetrics",
                "/work/fixture.openmetrics",
                "/work/tsdb",
            ],
            timeout=240,
        )
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


def start_prometheus(
    work_dir: Path,
    *,
    name: str,
    max_samples: int,
    max_concurrency: int,
    query_timeout: str,
) -> tuple[str, str]:
    run_command(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--restart",
            "on-failure:1",
            "--memory",
            PRODUCTION_MEMORY_LIMIT,
            "--memory-swap",
            PRODUCTION_MEMORY_LIMIT,
            "--env",
            f"GOMEMLIMIT={PRODUCTION_GO_MEMORY_LIMIT}",
            "-u",
            f"{os.getuid()}:{os.getgid()}",
            "-p",
            "127.0.0.1::9090",
            "-v",
            f"{work_dir}:/work",
            PROMETHEUS_IMAGE,
            "--config.file=/work/prometheus.yml",
            "--storage.tsdb.path=/work/tsdb",
            "--storage.tsdb.retention.time=15d",
            f"--query.max-samples={max_samples}",
            f"--query.max-concurrency={max_concurrency}",
            f"--query.timeout={query_timeout}",
            "--web.listen-address=0.0.0.0:9090",
        ]
    )
    try:
        mapped = run_command(["docker", "port", name, "9090/tcp"]).stdout.strip()
        port = mapped.rsplit(":", 1)[-1]
        base_url = f"http://127.0.0.1:{port}"
        wait_ready(base_url)
        return name, base_url
    except BaseException:
        stop_prometheus(name)
        raise


def stop_prometheus(name: str) -> None:
    stop_error: BaseException | None = None
    try:
        subprocess.run(
            ["docker", "stop", "--time", "10", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except BaseException as exc:
        stop_error = exc
    finally:
        removal = subprocess.run(
            ["docker", "rm", "--force", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        inspection = subprocess.run(
            ["docker", "inspect", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if removal.returncode != 0 and inspection.returncode == 0:
            raise RuntimeError(f"failed to remove disposable container {name}")
    if stop_error is not None:
        raise stop_error


def wait_ready(base_url: str) -> None:
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/-/ready", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"disposable Prometheus did not become Ready: {last_error}")


def request_json(
    base_url: str,
    path: str,
    parameters: dict[str, str | int] | None = None,
) -> ApiResult:
    data = None
    if parameters is not None:
        data = urllib.parse.urlencode(parameters).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return ApiResult(response.status, json.load(response))
    except urllib.error.HTTPError as exc:
        return ApiResult(exc.code, json.load(exc))


def query_range(
    base_url: str,
    query: str,
    window: FixtureWindow,
) -> ApiResult:
    return request_json(
        base_url,
        "/api/v1/query_range",
        {
            "query": query,
            "start": window.query_start,
            "end": window.end,
            "step": STEP_SECONDS,
            "stats": "all",
        },
    )


def query_instant(base_url: str, query: str, window: FixtureWindow) -> ApiResult:
    return request_json(
        base_url,
        "/api/v1/query",
        {"query": query, "time": window.end, "stats": "all"},
    )


def require_success(result: ApiResult, label: str) -> dict[str, Any]:
    if result.status_code != 200 or result.payload.get("status") != "success":
        raise AssertionError(f"{label} failed: HTTP {result.status_code} {result.payload}")
    return result.payload["data"]


def require_sample_rejection(result: ApiResult, label: str) -> None:
    if not (
        result.status_code == 422
        and result.payload.get("status") == "error"
        and result.payload.get("errorType") == "execution"
    ):
        raise AssertionError(
            f"{label} was not rejected by the sample guard: "
            f"HTTP {result.status_code} {result.payload}"
        )


def sample_stats(data: dict[str, Any]) -> tuple[int, int]:
    samples = data["stats"]["samples"]
    return int(samples["totalQueryableSamples"]), int(samples["peakSamples"])


def assert_equivalent_results(
    raw_data: dict[str, Any],
    recorded_data: dict[str, Any],
) -> None:
    raw_series = raw_data["result"]
    recorded_series = recorded_data["result"]
    if len(raw_series) != len(recorded_series):
        raise AssertionError("raw and recorded queries returned different cardinality")

    for raw, recorded in zip(raw_series, recorded_series, strict=True):
        recorded_labels = {
            key: value
            for key, value in recorded["metric"].items()
            if key != "__name__"
        }
        if raw["metric"] != recorded_labels:
            raise AssertionError("raw and recorded queries returned different labels")
        if len(raw["values"]) != len(recorded["values"]):
            raise AssertionError("raw and recorded queries returned different step counts")
        for raw_point, recorded_point in zip(
            raw["values"], recorded["values"], strict=True
        ):
            if raw_point[0] != recorded_point[0] or not math.isclose(
                float(raw_point[1]),
                float(recorded_point[1]),
                rel_tol=1e-7,
                abs_tol=1e-9,
            ):
                raise AssertionError("raw and recorded query values differ")


def assert_flags(base_url: str) -> None:
    result = request_json(base_url, "/api/v1/status/flags")
    data = require_success(result, "guarded flag inspection")
    expected = {
        "query.max-samples": str(GUARDED_MAX_SAMPLES),
        "query.max-concurrency": str(GUARDED_MAX_CONCURRENCY),
        "query.timeout": GUARDED_QUERY_TIMEOUT,
    }
    actual = {key: data[key] for key in expected}
    if actual != expected:
        raise AssertionError(f"guarded flags differ: expected {expected}, got {actual}")


def assert_container_healthy(container_name: str) -> None:
    inspection = run_command(
        [
            "docker",
            "inspect",
            "--format",
            "{{json .State}}|{{.RestartCount}}|{{.HostConfig.Memory}}|"
            "{{.HostConfig.MemorySwap}}|{{json .Config.Env}}",
            container_name,
        ]
    ).stdout.strip()
    state_text, restart_count, memory, memory_swap, environment = inspection.split(
        "|", 4
    )
    state = json.loads(state_text)
    if not state["Running"] or state["OOMKilled"]:
        raise AssertionError(f"disposable Prometheus is unhealthy: {state}")
    if restart_count != "0":
        raise AssertionError(f"disposable Prometheus restarted {restart_count} time(s)")
    if int(memory) != PRODUCTION_MEMORY_LIMIT_BYTES:
        raise AssertionError(f"disposable memory limit differs: {memory}")
    if int(memory_swap) != PRODUCTION_MEMORY_LIMIT_BYTES:
        raise AssertionError(f"disposable memory+swap limit differs: {memory_swap}")
    if f"GOMEMLIMIT={PRODUCTION_GO_MEMORY_LIMIT}" not in json.loads(environment):
        raise AssertionError("disposable Prometheus GOMEMLIMIT differs")


def run_test(*, pod_count: int) -> None:
    with tempfile.TemporaryDirectory(prefix="ces856-prometheus-") as tempdir:
        work_dir = Path(tempdir)
        (work_dir / "prometheus.yml").write_text(
            "global:\n  scrape_interval: 1m\n",
            encoding="utf-8",
        )
        fixture_path = work_dir / "fixture.openmetrics"
        window = generate_openmetrics(
            fixture_path,
            pod_count=pod_count,
        )
        fixture_megabytes = fixture_path.stat().st_size / (1024 * 1024)
        print(
            f"Generated {pod_count}-pod, 14-day synthetic fixture "
            f"({fixture_megabytes:.1f} MiB)."
        )
        import_fixture(work_dir)

        permissive_name = f"ces856-prom-permissive-{os.getpid()}"
        guarded_name = f"ces856-prom-guarded-{os.getpid()}"
        active_container: str | None = None
        try:
            active_container, base_url = start_prometheus(
                work_dir,
                name=permissive_name,
                max_samples=INCIDENT_MAX_SAMPLES,
                max_concurrency=GUARDED_MAX_CONCURRENCY,
                query_timeout=GUARDED_QUERY_TIMEOUT,
            )
            raw_data = require_success(
                query_range(base_url, RAW_JOIN_QUERY, window),
                "permissive raw joined range",
            )
            recorded_data = require_success(
                query_range(base_url, RECORDED_QUERY, window),
                "permissive recorded range",
            )
            assert_equivalent_results(raw_data, recorded_data)
            raw_total, raw_peak = sample_stats(raw_data)
            recorded_total, recorded_peak = sample_stats(recorded_data)
            if raw_peak <= GUARDED_MAX_SAMPLES:
                raise AssertionError(
                    f"raw peak {raw_peak} did not exceed {GUARDED_MAX_SAMPLES}"
                )
            if recorded_peak > MAX_RECORDED_RANGE_PEAK_SAMPLES:
                raise AssertionError(
                    f"recorded peak {recorded_peak} exceeded "
                    f"{MAX_RECORDED_RANGE_PEAK_SAMPLES}"
                )
            if raw_peak < recorded_peak * MIN_RAW_TO_RECORDED_PEAK_RATIO:
                raise AssertionError("synthetic join did not amplify peak samples enough")

            raw_subquery_data = require_success(
                query_instant(base_url, RAW_SUBQUERY, window),
                "permissive raw 14-day subquery",
            )
            _, raw_subquery_peak = sample_stats(raw_subquery_data)
            if raw_subquery_peak <= GUARDED_MAX_SAMPLES:
                raise AssertionError("raw subquery did not exceed the guarded sample cap")
            stop_prometheus(active_container)
            active_container = None

            active_container, base_url = start_prometheus(
                work_dir,
                name=guarded_name,
                max_samples=GUARDED_MAX_SAMPLES,
                max_concurrency=GUARDED_MAX_CONCURRENCY,
                query_timeout=GUARDED_QUERY_TIMEOUT,
            )
            assert_flags(base_url)
            helper_payload = request_history(
                base_url=base_url,
                selector=RECORDED_QUERY,
                start=window.query_start,
                end=window.end,
                step_seconds=STEP_SECONDS,
            )
            guarded_recorded = helper_payload["data"]
            _, guarded_recorded_peak = sample_stats(guarded_recorded)
            if guarded_recorded_peak > MAX_RECORDED_RANGE_PEAK_SAMPLES:
                raise AssertionError("guarded recorded range exceeded its sample envelope")
            helper_receipt = build_receipt(
                metric=RECORDED_QUERY,
                selector=RECORDED_QUERY,
                labels={},
                start=window.query_start,
                end=window.end,
                step_seconds=STEP_SECONDS,
                payload=helper_payload,
            )
            if helper_receipt["summary"]["sample_count"] != 4033:
                raise AssertionError("safe helper did not return the 14-day 5m envelope")
            gap_probe_start = window.query_start + HISTORY_SECONDS // 2
            try:
                request_history(
                    base_url=base_url,
                    selector=GAP_PROBE_QUERY,
                    start=gap_probe_start,
                    end=gap_probe_start + STEP_SECONDS,
                    step_seconds=STEP_SECONDS,
                )
            except RuntimeError as exc:
                if "incomplete" not in str(exc):
                    raise AssertionError(
                        f"bounded lookback failed unexpectedly: {exc}"
                    ) from exc
            else:
                raise AssertionError("bounded lookback did not expose a recording gap")
            recorded_subquery_data = require_success(
                query_instant(base_url, RECORDED_SUBQUERY, window),
                "guarded recorded 14-day subquery",
            )
            _, recorded_subquery_peak = sample_stats(recorded_subquery_data)
            if recorded_subquery_peak > MAX_RECORDED_SUBQUERY_PEAK_SAMPLES:
                raise AssertionError(
                    "guarded recorded subquery exceeded its one-series sample envelope"
                )
            require_sample_rejection(
                query_instant(base_url, RAW_SUBQUERY, window),
                "guarded raw 14-day subquery",
            )

            barrier = threading.Barrier(2)

            def concurrent_raw_query() -> ApiResult:
                barrier.wait(timeout=5)
                return query_range(base_url, RAW_JOIN_QUERY, window)

            with ThreadPoolExecutor(max_workers=2) as executor:
                guarded_raw_results = list(
                    executor.map(lambda _: concurrent_raw_query(), range(2))
                )
            for index, result in enumerate(guarded_raw_results, start=1):
                require_sample_rejection(result, f"guarded concurrent raw query {index}")

            wait_ready(base_url)
            assert_container_healthy(active_container)
            print(
                "Synthetic query-safety proof passed: "
                f"raw total/peak={raw_total}/{raw_peak}, "
                f"recorded total/peak={recorded_total}/{recorded_peak}, "
                "two guarded raw queries rejected, Prometheus Ready, restarts=0."
                f" memory={PRODUCTION_MEMORY_LIMIT}, "
                f"GOMEMLIMIT={PRODUCTION_GO_MEMORY_LIMIT}."
            )
        finally:
            if active_container is not None:
                stop_prometheus(active_container)


def main() -> None:
    args = parse_args()
    run_test(pod_count=args.pod_count)


if __name__ == "__main__":
    main()
