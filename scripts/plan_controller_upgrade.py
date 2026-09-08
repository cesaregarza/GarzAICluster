#!/usr/bin/env python3
"""Validate pinned controller payloads and compose Argo's KSOPS overlay.

This tool is deliberately offline: it reads already downloaded files and writes a
reviewable plan. It never invokes kubectl, Helm, a network client, or a secret
reader. The caller must review the generated diff before applying anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "ops" / "controller-upgrade-manifests.json"
PATCH_DIR = ROOT / "k8s" / "argocd"
CERT_CRDS = {"certificates.cert-manager.io", "issuers.cert-manager.io", "clusterissuers.cert-manager.io", "certificaterequests.cert-manager.io", "orders.acme.cert-manager.io", "challenges.acme.cert-manager.io"}
YAML_SAFE = YAML(typ="safe")
YAML_OUT = YAML()
YAML_OUT.default_flow_style = False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def docs(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [item for item in YAML_SAFE.load_all(stream) if isinstance(item, dict)]


def verify_payloads(manifest_dir: Path, lock: dict[str, Any], include_cert: bool = True, include_argo: bool = True) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"cert_manager": [], "argo_cd": []}
    if include_cert:
      for tag, crd_hash, full_hash in lock["cert_manager"]:
        crds = manifest_dir / f"{tag}-cert-manager.crds.yaml"
        full = manifest_dir / f"{tag}-cert-manager.yaml"
        for path, expected in ((crds, crd_hash), (full, full_hash)):
            if not path.is_file():
                raise ValueError(f"missing pinned payload: {path}")
            actual = sha256(path)
            if actual != expected:
                raise ValueError(f"SHA256 mismatch for {path.name}: expected {expected}, got {actual}")
        crd_names = {item.get("metadata", {}).get("name") for item in docs(crds)}
        if not CERT_CRDS.issubset(crd_names):
            raise ValueError(f"{tag} CRD payload does not contain all six cert-manager CRDs")
        deployments = [item for item in docs(full) if item.get("kind") == "Deployment"]
        images = [container.get("image", "") for item in deployments for container in item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])]
        if len(deployments) != 3 or any(not image.endswith(f":{tag}") for image in images):
            raise ValueError(f"{tag} full payload does not contain the three pinned controller images")
        result["cert_manager"].append(tag)
    if include_argo:
      for tag, expected in lock["argo_cd"]:
        path = manifest_dir / f"{tag}-argocd-install.yaml"
        if not path.is_file():
            raise ValueError(f"missing pinned payload: {path}")
        actual = sha256(path)
        if actual != expected:
            raise ValueError(f"SHA256 mismatch for {path.name}: expected {expected}, got {actual}")
        result["argo_cd"].append(tag)
    return result


def merge(base: Any, overlay: Any) -> Any:
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for key, value in overlay.items():
            out[key] = merge(out[key], value) if key in out else value
        return out
    if isinstance(base, list) and isinstance(overlay, list):
        out = list(base)
        for item in overlay:
            if isinstance(item, dict) and "name" in item:
                matches = [i for i, old in enumerate(out) if isinstance(old, dict) and old.get("name") == item["name"]]
                if matches:
                    out[matches[0]] = merge(out[matches[0]], item)
                else:
                    out.append(item)
            else:
                out.append(item)
        return out
    return overlay


def compose_argo(manifest: Path, output: Path) -> None:
    upstream = docs(manifest)
    cm_patch = docs(PATCH_DIR / "argocd-cm-ksops-patch.yaml")[0]
    repo_patch = docs(PATCH_DIR / "repo-server-ksops-patch.yaml")[0]
    found_cm = found_repo = False
    for index, item in enumerate(upstream):
        if item.get("kind") == "ConfigMap" and item.get("metadata", {}).get("name") == "argocd-cm":
            # The live installation uses only this key. Do not reintroduce the
            # deprecated configManagementPlugins field from the old patch.
            upstream[index] = merge(item, {"data": {"kustomize.buildOptions": cm_patch["data"]["kustomize.buildOptions"]}})
            upstream[index].get("data", {}).pop("configManagementPlugins", None)
            found_cm = True
        if item.get("kind") == "Deployment" and item.get("metadata", {}).get("name") == "argocd-repo-server":
            # The repository patch is a strategic patch fragment; add its target identity.
            target = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "argocd-repo-server", "namespace": "argocd"}}
            upstream[index] = merge(item, merge(target, repo_patch))
            found_repo = True
    if not found_cm or not found_repo:
        raise ValueError("upstream Argo manifest lacks argocd-cm or argocd-repo-server")
    cm = next(item for item in upstream if item.get("kind") == "ConfigMap" and item.get("metadata", {}).get("name") == "argocd-cm")
    repo = next(item for item in upstream if item.get("kind") == "Deployment" and item.get("metadata", {}).get("name") == "argocd-repo-server")
    if cm.get("data", {}).get("kustomize.buildOptions") != "--enable-alpha-plugins --enable-exec":
        raise ValueError("KSOPS kustomize build options were not preserved")
    pod = repo["spec"]["template"]["spec"]
    init = next((x for x in pod.get("initContainers", []) if x.get("name") == "install-ksops"), None)
    main = next((x for x in pod.get("containers", []) if x.get("name") == "argocd-repo-server"), None)
    if not init or init.get("image") != "viaductoss/ksops:v4.3.2" or not main:
        raise ValueError("KSOPS init container or repo-server container invariant missing")
    env = {x.get("name"): x.get("value") for x in main.get("env", [])}
    if env.get("KUSTOMIZE_PLUGIN_HOME") != "/custom-tools/kustomize/plugin" or env.get("SOPS_AGE_KEY_FILE") != "/sops/age.agekey":
        raise ValueError("KSOPS environment invariant missing")
    volume_names = {x.get("name") for x in pod.get("volumes", [])}
    if not {"ksops-tools", "sops-age"}.issubset(volume_names):
        raise ValueError("KSOPS volumes invariant missing")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        YAML_OUT.dump_all(upstream, stream)


def preservation_findings(candidate: list[dict[str, Any]], baseline_path: Path, namespace: str) -> list[str]:
    """Return changed fields and fail on settings removed from the live baseline."""
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    wanted = [item for item in baseline if item.get("namespace") == namespace]
    findings: list[str] = []
    for old in wanted:
        new = next((item for item in candidate if item.get("kind") == "Deployment" and item.get("metadata", {}).get("namespace", "argocd") == namespace and item.get("metadata", {}).get("name") == old["name"]), None)
        if new is None:
            raise ValueError(f"baseline Deployment removed from candidate: {namespace}/{old['name']}")
        old_spec = old.get("pod", {})
        new_spec = new.get("spec", {}).get("template", {}).get("spec", {})
        if old.get("replicas", 1) != new.get("spec", {}).get("replicas", 1):
            findings.append(f"{namespace}/{old['name']}.spec.replicas changed")
        for field in ("serviceAccountName", "nodeSelector", "affinity", "tolerations", "topologySpreadConstraints", "securityContext"):
            if old_spec.get(field) and not new_spec.get(field):
                raise ValueError(f"baseline {namespace}/{old['name']} setting removed: pod.{field}")
            if old_spec.get(field) and old_spec.get(field) != new_spec.get(field):
                findings.append(f"{namespace}/{old['name']}.pod.{field} changed")
        new_containers = {item.get("name"): item for item in new_spec.get("containers", [])}
        for old_container in old_spec.get("containers", []):
            name = old_container.get("name")
            new_container = new_containers.get(name)
            # cert-manager renamed container names across the staged route;
            # each controller Deployment has one container, so preserve by
            # Deployment identity when the name itself changed.
            if new_container is None and len(old_spec.get("containers", [])) == len(new_spec.get("containers", [])) == 1:
                new_container = new_spec["containers"][0]
            if new_container is None:
                raise ValueError(f"baseline container removed: {namespace}/{old['name']}/{name}")
            for field in ("command", "args", "securityContext", "resources"):
                if old_container.get(field) and not new_container.get(field):
                    raise ValueError(f"baseline {namespace}/{old['name']}/{name} setting removed: {field}")
                if old_container.get(field) and old_container.get(field) != new_container.get(field):
                    findings.append(f"{namespace}/{old['name']}/{name}.{field} changed")
    return findings


def build_plan(verified: dict[str, list[str]], manifest_dir: Path, output_dir: Path, findings: list[str] | None = None) -> dict[str, Any]:
    cert = verified["cert_manager"]
    argo = verified["argo_cd"]
    return {
        "offline": True,
        "current": {"cert_manager": "v1.7.1", "argo_cd": "v3.2.0"},
        "cert_manager_stages": cert,
        "argo_cd_stages": argo,
        "cert_manager_commands": [f"kubectl --context do-nyc3-k8s-nyc3-garz-ai apply -f {manifest_dir}/{tag}-cert-manager.yaml" for tag in cert],
        "argo_cd_review_commands": [f"kubectl --context do-nyc3-k8s-nyc3-garz-ai diff -n argocd --server-side --field-manager=gaic-controller-upgrade -f {output_dir}/{tag}-argocd-composed.yaml" for tag in argo],
        "argo_cd_apply_commands": [f"kubectl --context do-nyc3-k8s-nyc3-garz-ai apply -n argocd --server-side --field-manager=gaic-controller-upgrade -f {output_dir}/{tag}-argocd-composed.yaml" for tag in argo],
        "preservation_findings": findings or [],
        "guardrails": ["review field-level diff before each apply", "reject unexpected changes to ConfigMaps, Secrets, KSOPS init/volumes/env, replicas, scheduling, or resource requests", "never apply the standalone CRD payload after applying full cert-manager payload"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("all", "cert-manager", "argo"), default="all")
    parser.add_argument("--baseline", type=Path, help="safe live controller baseline JSON; reject removed settings")
    args = parser.parse_args()
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    verified = verify_payloads(args.manifest_dir, lock, include_cert=args.mode in ("all", "cert-manager"), include_argo=args.mode in ("all", "argo"))
    findings: list[str] = []
    if args.baseline:
        for tag in verified["cert_manager"]:
            findings.extend(preservation_findings(docs(args.manifest_dir / f"{tag}-cert-manager.yaml"), args.baseline, "cert-manager"))
    if args.mode in ("all", "argo"):
        for tag in verified["argo_cd"]:
            composed = args.output_dir / f"{tag}-argocd-composed.yaml"
            compose_argo(args.manifest_dir / f"{tag}-argocd-install.yaml", composed)
            if args.baseline:
                findings.extend(preservation_findings(docs(composed), args.baseline, "argocd"))
    plan = build_plan(verified, args.manifest_dir, args.output_dir, findings)
    if args.mode == "cert-manager":
        plan["argo_cd_stages"] = []
    if args.mode == "argo":
        plan["cert_manager_stages"] = []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "controller-upgrade-plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
