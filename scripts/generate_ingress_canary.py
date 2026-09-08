#!/usr/bin/env python3
"""Render a class-scoped Ingress canary without ownership annotations."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
URL_PATH = re.compile(r"^/[A-Za-z0-9._~:/%+@!$&'()*,-]*$")


def _dns_label(value: str, flag: str) -> str:
    if not value or len(value) > 63 or not DNS_LABEL.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{flag} must be a DNS label")
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--service-port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("--service-port must be 1..65535")
    return port


def _host(value: str) -> str:
    if (
        len(value) > 253
        or not value
        or value.startswith(".")
        or value.endswith(".")
        or any(
            len(label) > 63 or not DNS_LABEL.fullmatch(label)
            for label in value.split(".")
        )
    ):
        raise argparse.ArgumentTypeError(
            "--host must be a lowercase DNS hostname with valid individual labels"
        )
    return value


def _path(value: str) -> str:
    if len(value) > 2048 or not URL_PATH.fullmatch(value):
        raise argparse.ArgumentTypeError("--path must be a safe URL path")
    return value


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render a temporary Traefik Ingress canary. The output intentionally "
            "contains no cert-manager or external-dns annotations."
        )
    )
    parser.add_argument("--namespace", default="default", type=lambda v: _dns_label(v, "--namespace"))
    parser.add_argument("--name", default="traefik-ingress-canary", type=lambda v: _dns_label(v, "--name"))
    parser.add_argument(
        "--host",
        action="append",
        required=True,
        type=_host,
        help="Ingress host; repeat for aliases",
    )
    parser.add_argument("--service", required=True, type=lambda v: _dns_label(v, "--service"))
    parser.add_argument("--service-port", default=80, type=_port)
    parser.add_argument("--tls-secret", required=True, type=lambda v: _dns_label(v, "--tls-secret"))
    parser.add_argument("--path", default="/", type=_path)
    parser.add_argument("--output", type=Path, help="Write to this path; stdout when omitted")
    parsed = parser.parse_args()
    return parsed


def render(args: argparse.Namespace) -> str:
    lines = [
        "apiVersion: networking.k8s.io/v1",
        "kind: Ingress",
        "metadata:",
        f"  name: {args.name}",
        f"  namespace: {args.namespace}",
        "  labels:",
        "    app.kubernetes.io/part-of: gaic-ingress-migration-canary",
        "spec:",
        "  ingressClassName: traefik-nginx",
        "  tls:",
        "    - hosts:",
    ]
    lines.extend(f"        - {host}" for host in args.host)
    lines.extend(
        [
            f"      secretName: {args.tls_secret}",
            "  rules:",
        ]
    )
    for host in args.host:
        lines.extend(
            [
                f"    - host: {host}",
                "      http:",
                "        paths:",
                f"          - path: {args.path}",
                "            pathType: Prefix",
                "            backend:",
                "              service:",
                f"                name: {args.service}",
                "                port:",
                f"                  number: {args.service_port}",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _args()
    payload = render(args)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
