#!/usr/bin/env python3
"""Render a class-scoped Ingress canary without ownership annotations."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

from ruamel.yaml import YAML


DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
URL_PATH = re.compile(r"^/[A-Za-z0-9._~:/%+@!$&'()*,-]*$")
CANARY_SUFFIX = "-traefik-canary"

# Provider-specific annotations (including nginx.org/websocket-services) are
# intentionally omitted. These are the reviewed routing annotations for this
# migration slice; unsupported behavior needs an explicit follow-up change.
SUPPORTED_NGINX_ANNOTATIONS = frozenset(
    {
        "nginx.ingress.kubernetes.io/backend-protocol",
        "nginx.ingress.kubernetes.io/force-ssl-redirect",
        "nginx.ingress.kubernetes.io/limit-burst-multiplier",
        "nginx.ingress.kubernetes.io/limit-rps",
        "nginx.ingress.kubernetes.io/permanent-redirect",
        "nginx.ingress.kubernetes.io/permanent-redirect-code",
        "nginx.ingress.kubernetes.io/proxy-body-size",
        "nginx.ingress.kubernetes.io/ssl-redirect",
    }
)
INTENTIONALLY_DROPPED_ANNOTATIONS = frozenset({"nginx.org/websocket-services"})
OWNERSHIP_LABELS = frozenset(
    {
        "app.kubernetes.io/instance",
        "argocd.argoproj.io/instance",
        "argocd.argoproj.io/tracking-id",
    }
)


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


def _canary_name(value: str) -> str:
    candidate = f"{value}{CANARY_SUFFIX}"
    if len(candidate) <= 63:
        return candidate
    prefix = value[: 63 - len(CANARY_SUFFIX)].rstrip("-")
    if not prefix:
        raise ValueError(f"Ingress name {value!r} cannot receive canary suffix")
    return f"{prefix}{CANARY_SUFFIX}"


def _load_ingresses(path: Path) -> list[dict[str, object]]:
    raw = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--input must contain kubectl JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("--input JSON must be an object")
    items = payload.get("items")
    if items is None and payload.get("kind") == "Ingress":
        items = [payload]
    if not isinstance(items, list) or not items:
        raise ValueError("--input must be an IngressList with one or more items")
    ingresses: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("kind") != "Ingress":
            raise ValueError("--input items must all be Ingress objects")
        metadata = item.get("metadata")
        spec = item.get("spec")
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            raise ValueError("each Ingress must contain metadata and spec objects")
        name = metadata.get("name")
        namespace = metadata.get("namespace", "default")
        if not isinstance(name, str) or len(name) > 63 or not DNS_LABEL.fullmatch(name):
            raise ValueError("each Ingress metadata.name must be a DNS label")
        if not isinstance(namespace, str) or len(namespace) > 63 or not DNS_LABEL.fullmatch(namespace):
            raise ValueError("each Ingress metadata.namespace must be a DNS label")
        annotations = metadata.get("annotations", {})
        if not isinstance(annotations, dict):
            raise ValueError("Ingress metadata.annotations must be an object")
        labels = metadata.get("labels", {})
        if not isinstance(labels, dict):
            raise ValueError("Ingress metadata.labels must be an object")
        unknown_nginx_annotations = sorted(
            key
            for key in annotations
            if key.startswith("nginx.ingress.kubernetes.io/")
            and key not in SUPPORTED_NGINX_ANNOTATIONS
        )
        if unknown_nginx_annotations:
            raise ValueError(
                "unsupported nginx ingress annotations: "
                + ", ".join(unknown_nginx_annotations)
            )
        unknown_nginx_org_annotations = sorted(
            key
            for key in annotations
            if key.startswith("nginx.org/")
            and key not in INTENTIONALLY_DROPPED_ANNOTATIONS
        )
        if unknown_nginx_org_annotations:
            raise ValueError(
                "unsupported nginx.org annotations: "
                + ", ".join(unknown_nginx_org_annotations)
            )
        canary_spec = copy.deepcopy(spec)
        canary_spec["ingressClassName"] = "traefik-nginx"
        canary_annotations = {
            key: value
            for key, value in annotations.items()
            if key in SUPPORTED_NGINX_ANNOTATIONS
        }
        canary_metadata: dict[str, object] = {
            "name": _canary_name(name),
            "namespace": namespace,
            "labels": {
                **{
                    key: value
                    for key, value in labels.items()
                    if key not in OWNERSHIP_LABELS
                    and not key.startswith("argocd.argoproj.io/")
                },
                "app.kubernetes.io/part-of": "gaic-ingress-migration-canary",
            },
        }
        if canary_annotations:
            canary_metadata["annotations"] = canary_annotations
        ingresses.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": canary_metadata,
                "spec": canary_spec,
            }
        )
    identities = [
        (ingress["metadata"]["namespace"], ingress["metadata"]["name"])
        for ingress in ingresses
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("generated canary names collide within a namespace")
    return sorted(
        ingresses,
        key=lambda ingress: (
            ingress["metadata"]["namespace"],
            ingress["metadata"]["name"],
        ),
    )


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render a temporary Traefik Ingress canary. The output intentionally "
            "contains no cert-manager or external-dns annotations."
        )
    )
    parser.add_argument("--input", type=Path, help="kubectl get ingress -A -o json; use - for stdin")
    parser.add_argument("--namespace", default="default", type=lambda v: _dns_label(v, "--namespace"))
    parser.add_argument("--name", default="traefik-ingress-canary", type=lambda v: _dns_label(v, "--name"))
    parser.add_argument(
        "--host",
        action="append",
        type=_host,
        help="Ingress host; repeat for aliases",
    )
    parser.add_argument("--service", type=lambda v: _dns_label(v, "--service"))
    parser.add_argument("--service-port", default=80, type=_port)
    parser.add_argument("--tls-secret", type=lambda v: _dns_label(v, "--tls-secret"))
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
    try:
        args = _args()
        if args.input:
            if any(value is not None for value in (args.host, args.service, args.tls_secret)):
                raise ValueError("--input cannot be combined with single-Ingress route flags")
            yaml = YAML()
            yaml.default_flow_style = False
            yaml.explicit_start = True
            documents = _load_ingresses(args.input)
            if args.output:
                with args.output.open("w", encoding="utf-8") as destination:
                    yaml.dump_all(documents, destination)
            else:
                yaml.dump_all(documents, sys.stdout)
            return 0
        if not args.host or not args.service or not args.tls_secret:
            raise ValueError("--host, --service, and --tls-secret are required without --input")
        payload = render(args)
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
        else:
            sys.stdout.write(payload)
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
