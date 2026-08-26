#!/usr/bin/env python3
"""Render Prometheus config/rules from Helm and validate them with promtool."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG_TEMPLATE = "templates/monitoring-prometheus-configmap.yaml"
RULES_TEMPLATE = "templates/monitoring-prometheus-rules-configmap.yaml"
DEFAULT_CHART_DIR = "helm/garz-observability"
DEFAULT_RELEASE_NAME = "garz-observability"
DEFAULT_VALUES_FILE = "helm/garz-observability/values-prod.yaml"
PROMTOOL_IMAGE = "prom/prometheus:v2.52.0"
INDENT = "  "
RULE_TEST_PATTERN = "*.test.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chart-dir",
        default=DEFAULT_CHART_DIR,
        help=f"Path to the Helm chart directory (default: {DEFAULT_CHART_DIR}).",
    )
    parser.add_argument(
        "--release-name",
        default=DEFAULT_RELEASE_NAME,
        help=(
            "Helm release name used during templating "
            f"(default: {DEFAULT_RELEASE_NAME})."
        ),
    )
    parser.add_argument(
        "-f",
        "--values",
        dest="values_files",
        action="append",
        help="Additional Helm values files to pass with -f. Specify multiple times for overlays.",
    )
    parser.add_argument(
        "--namespace",
        help="Optional namespace to pass to helm template (overrides default release namespace).",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Treat missing Prometheus templates as a no-op (useful for environments without monitoring).",
    )
    parser.add_argument(
        "--label",
        help="Friendly label for log output (defaults to the last provided values file stem).",
    )
    args = parser.parse_args()

    if not args.values_files:
        args.values_files = [DEFAULT_VALUES_FILE]

    missing_files = [p for p in args.values_files if not Path(p).exists()]
    if missing_files:
        raise SystemExit(f"Values file(s) not found: {', '.join(missing_files)}")

    if not args.label:
        args.label = Path(args.values_files[-1]).stem

    return args


def render_template(args: argparse.Namespace, template: str, allow_empty: bool) -> str:
    cmd = [
        "helm",
        "template",
        args.release_name,
        args.chart_dir,
        "--show-only",
        template,
    ]

    if args.namespace:
        cmd.extend(["--namespace", args.namespace])

    for values_file in args.values_files:
        cmd.extend(["-f", values_file])

    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)

    rendered = completed.stdout.strip()
    if not rendered:
        if allow_empty:
            return ""
        raise SystemExit(
            "Helm rendered no output for template "
            f"{template} using values {', '.join(args.values_files)}. "
            "Ensure monitoring.prometheus.enabled and monitoring.prometheus.rules.enabled are true."
        )

    return rendered


def extract_configmap_entries_from_text(text: str) -> dict[str, str]:
    """Return ConfigMap data entries keyed by filename."""

    lines = text.splitlines()
    entries: dict[str, str] = {}
    in_data_section = False
    data_indent = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if not in_data_section:
            if stripped == "data:":
                in_data_section = True
                data_indent = indent
            i += 1
            continue

        if indent <= data_indent:
            break

        if stripped.endswith(": |"):
            key = stripped[:-3]
            value_indent = indent + len(INDENT)
            value_lines: list[str] = []
            i += 1

            while i < len(lines):
                value_line = lines[i]
                if not value_line.strip():
                    value_lines.append("")
                    i += 1
                    continue

                current_indent = len(value_line) - len(value_line.lstrip())
                if current_indent < value_indent:
                    break

                value_lines.append(value_line[value_indent:])
                i += 1

            entries[key] = "\n".join(value_lines).rstrip() + "\n"
            continue

        i += 1

    if not entries:
        raise SystemExit("No ConfigMap data entries discovered in rendered template output")

    return entries


def extract_prometheus_config(rendered_text: str) -> str:
    entries = extract_configmap_entries_from_text(rendered_text)
    try:
        config_text = entries["prometheus.yml"]
    except KeyError as exc:  # pragma: no cover - misconfigured configmap
        raise SystemExit("prometheus.yml entry not found in rendered ConfigMap") from exc

    if not config_text.strip():
        raise SystemExit("Failed to extract prometheus.yml contents from rendered ConfigMap")

    return config_text


def extract_rule_files(rendered_text: str) -> dict[str, str]:
    entries = extract_configmap_entries_from_text(rendered_text)
    filtered = {name: contents for name, contents in entries.items() if name.endswith(".yaml")}

    if not filtered:
        raise SystemExit("No rule files discovered in rendered Prometheus rules ConfigMap")

    return filtered


def load_rule_test_files(chart_dir: str) -> dict[str, str]:
    tests_dir = Path(chart_dir) / "tests"
    if not tests_dir.is_dir():
        return {}

    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(tests_dir.glob(RULE_TEST_PATTERN))
    }


def run_promtool_config(config_text: str) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
        cfg_dir = Path(tempdir)
        config_path = cfg_dir / "prometheus.yml"
        config_path.write_text(config_text)
        # promtool checks referenced bearer_token_file/ca_file paths even though
        # CI is not running inside a Kubernetes pod.
        service_account_dir = cfg_dir / "serviceaccount"
        service_account_dir.mkdir()
        (service_account_dir / "token").write_text("promtool-ci-token\n")
        (service_account_dir / "ca.crt").write_text(
            "-----BEGIN CERTIFICATE-----\n"
            "MIIBhTCCASugAwIBAgIJAO7gPromtoolCiMAoGCCqGSM49BAMCMBQxEjAQBgNV\n"
            "BAMMCXByb210b29sMB4XDTI2MDEwMTAwMDAwMFoXDTM2MDEwMTAwMDAwMFow\n"
            "FDESMBAGA1UEAwwJcHJvbXRvb2wwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNC\n"
            "AAS8LL2hHmgKTu0eYi5iqHe4UjsJ61rVjuzh0f4fPTTW5s12rEc3D9GYm0NG\n"
            "CMxUOMwxvKbL0fPfwdoF8Sero1MwUTAdBgNVHQ4EFgQUpromtoolci000000\n"
            "000000000000000000wHwYDVR0jBBgwFoAUpromtoolci0000000000000000\n"
            "00000000wDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNJADBGAiEAzj2p\n"
            "sJ5yQfztwfdlKZs8dcOQGv4qaZFrBFZyV0sCIQC3B0vY0FSk5Fhl33FjzZzz\n"
            "TfGv9dbAQHbXQz8mqQ==\n"
            "-----END CERTIFICATE-----\n"
        )

        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-u",
                f"{os.getuid()}:{os.getgid()}",
                "--entrypoint=promtool",
                "-v",
                f"{cfg_dir}:/etc/prometheus/conf",
                "-v",
                (
                    f"{service_account_dir}:"
                    "/var/run/secrets/kubernetes.io/serviceaccount:ro"
                ),
                PROMTOOL_IMAGE,
                "check",
                "config",
                "/etc/prometheus/conf/prometheus.yml",
            ],
            check=True,
        )


def run_promtool_rules(rule_files: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
        rules_dir = Path(tempdir)
        container_files: list[str] = []
        for name, contents in sorted(rule_files.items()):
            safe_name = Path(name).name
            target = rules_dir / safe_name
            target.write_text(contents)
            container_files.append(f"/etc/prometheus/rules/{safe_name}")

        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-u",
                f"{os.getuid()}:{os.getgid()}",
                "--entrypoint=promtool",
                "-v",
                f"{rules_dir}:/etc/prometheus/rules",
                PROMTOOL_IMAGE,
                "check",
                "rules",
                *container_files,
            ],
            check=True,
        )


def run_promtool_rule_tests(
    rule_files: dict[str, str],
    test_files: dict[str, str],
) -> None:
    if not test_files:
        return

    with tempfile.TemporaryDirectory() as tempdir:
        rules_dir = Path(tempdir)
        for name, contents in sorted(rule_files.items()):
            (rules_dir / Path(name).name).write_text(contents, encoding="utf-8")

        container_tests: list[str] = []
        for name, contents in sorted(test_files.items()):
            safe_name = Path(name).name
            (rules_dir / safe_name).write_text(contents, encoding="utf-8")
            container_tests.append(f"/etc/prometheus/rules/{safe_name}")

        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-u",
                f"{os.getuid()}:{os.getgid()}",
                "--entrypoint=promtool",
                "-w",
                "/etc/prometheus/rules",
                "-v",
                f"{rules_dir}:/etc/prometheus/rules:ro",
                PROMTOOL_IMAGE,
                "test",
                "rules",
                *container_tests,
            ],
            check=True,
        )


def main() -> None:
    args = parse_args()
    values_label = ", ".join(args.values_files)
    print(f"[{args.label}] Rendering Helm templates with values: {values_label}")

    config_rendered = render_template(args, CONFIG_TEMPLATE, allow_empty=args.allow_missing)
    if not config_rendered:
        print(f"[{args.label}] Monitoring is disabled; skipping promtool validation.")
        return

    config_text = extract_prometheus_config(config_rendered)
    run_promtool_config(config_text)

    rules_rendered = render_template(args, RULES_TEMPLATE, allow_empty=False)
    rule_files = extract_rule_files(rules_rendered)
    run_promtool_rules(rule_files)
    test_files = load_rule_test_files(args.chart_dir)
    run_promtool_rule_tests(rule_files, test_files)

    test_summary = f" and {len(test_files)} rule test file(s)" if test_files else ""
    print(
        f"[{args.label}] Prometheus config, rules{test_summary} "
        "validated successfully."
    )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
