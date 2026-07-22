#!/usr/bin/env python3
"""Semantic CI assertions for the inert Poetry GitOps substrate."""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")
EXPECTED_SECRETS = ["poetry-database", "poetry-django", "poetry-media"]
AGE_RECIPIENT = "age16yxsawhpecdrhas2q3z246q3tjq8889m552lqhjcgf8jnt7naszqqgz8vt"
EXPECTED_SECRET_FILES = {
    "database.enc.yaml": ("poetry-database", "Opaque", {"DATABASE_URL"}),
    "django.enc.yaml": ("poetry-django", "Opaque", {"DJANGO_SECRET_KEY"}),
    "media.enc.yaml": (
        "poetry-media",
        "Opaque",
        {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"},
    ),
    "regcred.enc.yaml": (
        "regcred",
        "kubernetes.io/dockerconfigjson",
        {".dockerconfigjson"},
    ),
}
EXPECTED_HEADERS = {
    "Host": "poetry.cegarza.com",
    "X-Forwarded-Proto": "https",
}
IMAGE_PATTERN = re.compile(
    r"^registry\.digitalocean\.com/sendouq/poetry(?:"
    r":sha-[a-f0-9]{40}|@sha256:[a-f0-9]{64})$"
)


def _load_one(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"expected a YAML mapping in {path}")
    return loaded


def _load_all(path: Path) -> list[dict[str, Any]]:
    return [
        document
        for document in YAML_PARSER.load_all(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document
    ]


def _find(docs: Iterable[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for document in docs:
        if (
            document.get("kind") == kind
            and document.get("metadata", {}).get("name") == name
        ):
            return document
    raise AssertionError(f"missing {kind}/{name}")


def _assert_no_key(value: Any, forbidden_key: str) -> None:
    if isinstance(value, dict):
        if forbidden_key in value:
            raise AssertionError(f"render contains forbidden key {forbidden_key!r}")
        for nested in value.values():
            _assert_no_key(nested, forbidden_key)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_key(nested, forbidden_key)


def _assert_application(path: Path, *, name: str, wave: str, source_path: str) -> None:
    application = _load_one(path)
    assert application["kind"] == "Application"
    assert application["metadata"]["name"] == name
    assert application["metadata"]["namespace"] == "argocd"
    assert (
        application["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == wave
    )
    assert application["spec"]["source"]["path"] == source_path
    assert application["spec"]["destination"] == {
        "server": "https://kubernetes.default.svc",
        "namespace": "poetry",
    }
    assert application["spec"]["syncPolicy"]["automated"] == {
        "prune": True,
        "selfHeal": True,
    }
    assert "CreateNamespace=true" in application["spec"]["syncPolicy"]["syncOptions"]


def _assert_encrypted_secret(
    path: Path, *, name: str, secret_type: str, data_keys: set[str]
) -> None:
    secret = _load_one(path)
    assert secret["apiVersion"] == "v1"
    assert secret["kind"] == "Secret"
    assert secret["metadata"] == {"name": name, "namespace": "poetry"}
    assert secret["type"] == secret_type
    assert "data" not in secret
    assert set(secret["stringData"]) == data_keys
    assert all(
        isinstance(value, str) and value.startswith("ENC[AES256_GCM,")
        for value in secret["stringData"].values()
    )
    sops = secret["sops"]
    assert sops["encrypted_regex"] == "^(data|stringData)$"
    assert {entry["recipient"] for entry in sops["age"]} == {AGE_RECIPIENT}


def check(default_render: Path, enabled_render: Path) -> None:
    values = _load_one(REPO_ROOT / "helm" / "poetry" / "values.yaml")
    assert values["enabled"] is False
    assert values["replicaCount"] == 1
    assert values["ingress"]["enabled"] is False
    assert values["ingress"]["cloudflareProxied"] == "false"
    assert values["application"]["cloudflareAccess"] == {
        "enabled": False,
        "teamDomain": "",
        "audience": "",
        "allowedEmail": "cesar@cegarza.com",
    }
    assert re.fullmatch(r"sha-[a-f0-9]{40}", values["image"]["tag"])

    assert _load_all(default_render) == [], "disabled values must render no resources"
    docs = _load_all(enabled_render)
    assert [document["kind"] for document in docs] == [
        "ConfigMap",
        "Service",
        "Deployment",
        "Job",
        "Ingress",
    ]
    assert all(document["kind"] != "PersistentVolumeClaim" for document in docs)
    _assert_no_key(docs, "persistentVolumeClaim")

    deployment = _find(docs, "Deployment", "poetry")
    assert deployment["spec"]["replicas"] == 1
    config_checksum = deployment["spec"]["template"]["metadata"]["annotations"][
        "checksum/config"
    ]
    assert re.fullmatch(r"[a-f0-9]{64}", config_checksum)
    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["imagePullSecrets"] == [{"name": "regcred"}]
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["volumes"] == [{"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}]
    container = pod_spec["containers"][0]
    assert IMAGE_PATTERN.fullmatch(container["image"])
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["volumeMounts"] == [{"name": "tmp", "mountPath": "/tmp"}]
    assert [
        source["secretRef"]["name"]
        for source in container["envFrom"]
        if "secretRef" in source
    ] == EXPECTED_SECRETS

    config_map = _find(docs, "ConfigMap", "poetry-config")
    assert config_map["data"]["CLOUDFLARE_ACCESS_REQUIRED"] == "true"
    assert config_map["data"]["CLOUDFLARE_ACCESS_TEAM_DOMAIN"] == (
        "https://poetry-ci.cloudflareaccess.com"
    )
    assert config_map["data"]["CLOUDFLARE_ACCESS_AUD"] == (
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    assert config_map["data"]["CLOUDFLARE_ACCESS_ALLOWED_EMAIL"] == (
        "cesar@cegarza.com"
    )

    for probe_name, expected_path in (
        ("startupProbe", "/healthz"),
        ("livenessProbe", "/healthz"),
        ("readinessProbe", "/readyz"),
    ):
        http_get = container[probe_name]["httpGet"]
        assert http_get["path"] == expected_path
        assert http_get["port"] == "http"
        rendered_headers = {
            header["name"]: header["value"] for header in http_get["httpHeaders"]
        }
        assert rendered_headers == EXPECTED_HEADERS

    job = _find(docs, "Job", "poetry-migrate")
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PreSync"
    job_pod_spec = job["spec"]["template"]["spec"]
    assert job_pod_spec["imagePullSecrets"] == [{"name": "regcred"}]
    job_container = job_pod_spec["containers"][0]
    assert IMAGE_PATTERN.fullmatch(job_container["image"])
    assert [
        source["secretRef"]["name"]
        for source in job_container["envFrom"]
        if "secretRef" in source
    ] == EXPECTED_SECRETS

    ingress = _find(docs, "Ingress", "poetry")
    ingress_spec = ingress["spec"]
    assert ingress_spec["ingressClassName"] == "nginx"
    assert ingress_spec["rules"][0]["host"] == "poetry.cegarza.com"
    assert ingress_spec["tls"] == [
        {
            "hosts": ["poetry.cegarza.com"],
            "secretName": "poetry-cegarza-com-tls",
        }
    ]
    annotations = ingress["metadata"]["annotations"]
    assert annotations["external-dns.alpha.kubernetes.io/cloudflare-proxied"] == "false"
    assert annotations["cert-manager.io/cluster-issuer"] == "letsencrypt-prod"
    assert "nginx.ingress.kubernetes.io/whitelist-source-range" not in annotations

    _assert_application(
        REPO_ROOT / "argocd" / "applications" / "poetry.yaml",
        name="poetry",
        wave="10",
        source_path="helm/poetry",
    )
    poetry_application = _load_one(
        REPO_ROOT / "argocd" / "applications" / "poetry.yaml"
    )
    assert poetry_application["spec"]["source"]["helm"]["valueFiles"] == ["values.yaml"]
    _assert_application(
        REPO_ROOT / "argocd" / "applications" / "poetry-secrets.yaml",
        name="poetry-secrets",
        wave="-10",
        source_path="secrets/poetry",
    )
    project = _load_one(REPO_ROOT / "argocd" / "projects" / "splattop-project.yaml")
    assert {
        "namespace": "poetry",
        "server": "https://kubernetes.default.svc",
    } in project["spec"]["destinations"]
    secret_kustomization = _load_one(
        REPO_ROOT / "secrets" / "poetry" / "kustomization.yaml"
    )
    assert secret_kustomization["namespace"] == "poetry"
    assert secret_kustomization["resources"] == []
    assert secret_kustomization["generators"] == ["ksops.yaml"]
    secret_ksops = _load_one(REPO_ROOT / "secrets" / "poetry" / "ksops.yaml")
    assert secret_ksops["apiVersion"] == "viaduct.ai/v1"
    assert secret_ksops["kind"] == "ksops"
    assert secret_ksops["metadata"]["name"] == "poetry-secrets"
    assert secret_ksops["files"] == list(EXPECTED_SECRET_FILES)
    for filename, (name, secret_type, data_keys) in EXPECTED_SECRET_FILES.items():
        _assert_encrypted_secret(
            REPO_ROOT / "secrets" / "poetry" / filename,
            name=name,
            secret_type=secret_type,
            data_keys=data_keys,
        )

    rollout_notes = (REPO_ROOT / "helm" / "poetry" / "README.md").read_text(
        encoding="utf-8"
    )
    for required_text in (
        "waves order Application reconciliation only",
        "Synced` and `Healthy",
        "Let's Encrypt HTTP-01 issuance",
        "Access-signed RS256 JWT",
        "does not interfere with HTTP-01 certificate renewal",
        "same GitOps revision restarts the Deployment",
        "refuses to render a proxied Ingress",
    ):
        assert required_text in rollout_notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--default-render", type=Path, required=True)
    parser.add_argument("--enabled-render", type=Path, required=True)
    args = parser.parse_args()
    check(args.default_render, args.enabled_render)
    print("poetry substrate semantic checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
