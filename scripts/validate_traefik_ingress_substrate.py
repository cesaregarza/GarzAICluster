#!/usr/bin/env python3
"""Render and validate the inert Traefik ingress substrate."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from ruamel.yaml import YAML


EXPECTED_KINDS = {
    "ClusterRole",
    "ClusterRoleBinding",
    "Deployment",
    "IngressClass",
    "PodDisruptionBudget",
    "Service",
    "ServiceAccount",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    manifest_dir = args.repo_root / "infra" / "traefik-ingress"
    rendered = subprocess.run(
        ["kubectl", "kustomize", str(manifest_dir)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", encoding="utf-8"
    ) as handle:
        handle.write(rendered)
        handle.flush()
        subprocess.run(
            [
                "kubeconform",
                "-schema-location",
                "default",
                "-strict",
                "-summary",
                "-exit-on-error",
                handle.name,
            ],
            check=True,
        )

    parser_yaml = YAML(typ="safe")
    documents = [
        document
        for document in parser_yaml.load_all(rendered)
        if isinstance(document, dict) and document
    ]
    kinds = {document.get("kind") for document in documents}
    if kinds != EXPECTED_KINDS or len(documents) != len(EXPECTED_KINDS):
        raise SystemExit(f"unexpected substrate kinds: {sorted(kinds)}")
    if any(
        document.get("kind") in {"Secret", "Ingress", "Gateway"}
        for document in documents
    ):
        raise SystemExit("substrate unexpectedly contains a route, gateway, or secret")
    service = next(document for document in documents if document["kind"] == "Service")
    if service["spec"].get("type") != "ClusterIP":
        raise SystemExit("substrate Service must remain ClusterIP")
    print(f"validated {len(documents)} inert Traefik resources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
