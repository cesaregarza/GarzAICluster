#!/usr/bin/env python3
"""Guard the legacy ingress-nginx admission webhook from Traefik objects.

The default action inventories only the named ValidatingWebhookConfiguration
and performs a Kubernetes server dry-run. ``--apply`` is an explicit root-only
mutation and requires a mode-0600 receipt written before the patch. Rollback
requires that receipt and tests the live UID, resourceVersion, safe webhook
identity, and exact current match conditions before restoring the old list.

The helper deliberately never prints or stores ``clientConfig.caBundle`` and
never reads Secret resources, DNS state, or application objects.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


CONTEXT = "do-nyc3-k8s-nyc3-garz-ai"
CONFIG_NAME = "ingress-nginx-admission"
WEBHOOK_NAME = "validate.nginx.ingress.kubernetes.io"
WEBHOOK_NAMESPACE = "ingress-nginx"
WEBHOOK_SERVICE = "ingress-nginx-controller-admission"
CONDITION_NAME = "skip-traefik-nginx-ingresses"
KUBECTL = "/root/dev/.tools/kubectl-v1.33.12"
ANNOTATION_KEY = "kubernetes.io/ingress.class"
TARGET_CLASS = "traefik-nginx"
CONDITION_EXPRESSION = (
    "!(has(object.spec.ingressClassName) && object.spec.ingressClassName == 'traefik-nginx' && "
    "(!has(object.metadata.annotations) || "
    "!('kubernetes.io/ingress.class' in object.metadata.annotations) || "
    "object.metadata.annotations['kubernetes.io/ingress.class'] == 'traefik-nginx'))"
)


class GuardError(ValueError):
    """A reviewed webhook precondition or drift guard rejected the operation."""


def _metadata(resource: dict[str, Any]) -> dict[str, Any]:
    metadata = resource.get("metadata")
    if not isinstance(metadata, dict):
        raise GuardError("webhook configuration has no metadata")
    return metadata


def _identity(resource: dict[str, Any]) -> tuple[str, str]:
    metadata = _metadata(resource)
    uid, resource_version = metadata.get("uid"), metadata.get("resourceVersion")
    if not isinstance(uid, str) or not uid or not isinstance(resource_version, str) or not resource_version:
        raise GuardError("webhook configuration lacks UID or resourceVersion")
    return uid, resource_version


def _target_index(configuration: dict[str, Any]) -> int:
    webhooks = configuration.get("webhooks")
    if not isinstance(webhooks, list):
        raise GuardError("webhook configuration has no webhooks list")
    matches = [index for index, webhook in enumerate(webhooks) if webhook.get("name") == WEBHOOK_NAME]
    if len(matches) != 1:
        raise GuardError("webhook configuration must contain exactly one target webhook")
    return matches[0]


def safe_projection(webhook: dict[str, Any]) -> dict[str, Any]:
    """Return all reviewed webhook fields while excluding CA/credential material."""
    projection: dict[str, Any] = {}
    for key in (
        "name", "matchPolicy", "failurePolicy", "rules", "namespaceSelector",
        "objectSelector", "sideEffects", "admissionReviewVersions", "timeoutSeconds",
        "reinvocationPolicy", "matchConditions",
    ):
        if key in webhook:
            projection[key] = copy.deepcopy(webhook[key])
    client = webhook.get("clientConfig")
    if not isinstance(client, dict):
        raise GuardError("target webhook lacks clientConfig")
    service = client.get("service")
    if not isinstance(service, dict):
        raise GuardError("target webhook must use a service clientConfig")
    projection["clientConfig.service"] = {
        key: copy.deepcopy(service[key])
        for key in ("namespace", "name", "path", "port")
        if key in service
    }
    return projection


def _without_conditions(projection: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(projection)
    result.pop("matchConditions", None)
    return result


def validate_configuration(configuration: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(configuration)
    if metadata.get("name") != CONFIG_NAME:
        raise GuardError("unexpected ValidatingWebhookConfiguration name")
    uid, resource_version = _identity(configuration)
    index = _target_index(configuration)
    webhook = configuration["webhooks"][index]
    client_service = (webhook.get("clientConfig") or {}).get("service") or {}
    if client_service.get("namespace") != WEBHOOK_NAMESPACE or client_service.get("name") != WEBHOOK_SERVICE:
        raise GuardError("target webhook client service is not the legacy admission service")
    if webhook.get("failurePolicy") != "Fail":
        raise GuardError("legacy webhook failurePolicy must remain Fail")
    if webhook.get("matchPolicy") != "Equivalent":
        raise GuardError("legacy webhook matchPolicy must remain Equivalent")
    rules = webhook.get("rules")
    if (
        not isinstance(rules, list) or len(rules) != 1
        or rules[0].get("apiGroups") != ["networking.k8s.io"]
        or rules[0].get("apiVersions") != ["v1"]
        or rules[0].get("operations") != ["CREATE", "UPDATE"]
        or rules[0].get("resources") != ["ingresses"]
        or any(key not in {"apiGroups", "apiVersions", "operations", "resources", "scope"} for key in rules[0])
        or ("scope" in rules[0] and rules[0]["scope"] != "*")
    ):
        raise GuardError("legacy webhook rules changed from the reviewed Ingress rules")
    old_conditions = webhook.get("matchConditions", [])
    if not isinstance(old_conditions, list):
        raise GuardError("legacy webhook matchConditions is not a list")
    if any(condition.get("name") == CONDITION_NAME for condition in old_conditions):
        raise GuardError("legacy webhook skip condition already exists")
    projection = safe_projection(webhook)
    return {
        "uid": uid,
        "resourceVersion": resource_version,
        "index": index,
        "old_conditions": copy.deepcopy(old_conditions),
        "old_projection": projection,
        "rules": copy.deepcopy(rules),
        "new_conditions": old_conditions + [{"name": CONDITION_NAME, "expression": CONDITION_EXPRESSION}],
    }


def build_patch(
    *, uid: str, resource_version: str, index: int, old_conditions: list[dict[str, Any]],
    new_conditions: list[dict[str, Any]], has_conditions: bool, rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build an atomic UID/RV-guarded patch changing only matchConditions."""
    path = f"/webhooks/{index}/matchConditions"
    operations: list[dict[str, Any]] = [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
        {"op": "test", "path": f"/webhooks/{index}/name", "value": WEBHOOK_NAME},
        {"op": "test", "path": f"/webhooks/{index}/failurePolicy", "value": "Fail"},
        {"op": "test", "path": f"/webhooks/{index}/matchPolicy", "value": "Equivalent"},
        {"op": "test", "path": f"/webhooks/{index}/rules", "value": rules},
    ]
    if has_conditions:
        operations.append({"op": "test", "path": path, "value": old_conditions})
        operations.append({"op": "replace", "path": path, "value": new_conditions})
    else:
        operations.append({"op": "add", "path": path, "value": new_conditions})
    return operations


def condition_skips(ingress: dict[str, Any]) -> bool:
    """Reference truth table for the condition's intended skip behavior."""
    spec = ingress.get("spec") or {}
    metadata = ingress.get("metadata") or {}
    annotations = metadata.get("annotations")
    return (
        spec.get("ingressClassName") == TARGET_CLASS
        and (not annotations or annotations.get(ANNOTATION_KEY) in (None, TARGET_CLASS))
    )


class Kubectl:
    def __init__(self, binary: str = KUBECTL, context: str = CONTEXT) -> None:
        self.binary, self.context = binary, context

    def run(self, args: list[str]) -> str:
        result = subprocess.run(
            [self.binary, "--context", self.context, *args],
            text=True, capture_output=True, check=False, timeout=60,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    def current_context(self) -> str:
        result = subprocess.run(
            [self.binary, "config", "current-context"],
            text=True, capture_output=True, check=False, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "could not read current context")
        return result.stdout.strip()

    def get(self) -> dict[str, Any]:
        return json.loads(self.run(["get", "validatingwebhookconfiguration", CONFIG_NAME, "-o", "json"]))

    def patch(self, operations: list[dict[str, Any]], *, dry_run: bool) -> dict[str, Any]:
        args = [
            "patch", "validatingwebhookconfiguration", CONFIG_NAME, "--type=json",
            "--patch=" + json.dumps(operations, separators=(",", ":")), "--output=json",
        ]
        if dry_run:
            args.append("--dry-run=server")
        return json.loads(self.run(args))


def _write_receipt(path: Path, value: dict[str, Any], *, create: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        return
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise GuardError("receipt must be a private regular file")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_receipt(path: Path) -> dict[str, Any]:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise GuardError("rollback receipt must be a private regular file")
    return json.loads(path.read_text(encoding="utf-8"))


def _forward(args: argparse.Namespace) -> int:
    kube = Kubectl(args.kubectl, args.context)
    if kube.current_context() != CONTEXT:
        raise GuardError(f"kubectl current-context must be exactly {CONTEXT!r}")
    configuration = kube.get()
    state = validate_configuration(configuration)
    webhook = configuration["webhooks"][state["index"]]
    operations = build_patch(
        uid=state["uid"], resource_version=state["resourceVersion"], index=state["index"],
        old_conditions=state["old_conditions"], new_conditions=state["new_conditions"],
        has_conditions="matchConditions" in webhook, rules=state["rules"],
    )
    admitted = kube.patch(operations, dry_run=True)
    expected_webhooks = copy.deepcopy(configuration["webhooks"])
    expected_webhooks[state["index"]]["matchConditions"] = state["new_conditions"]
    if admitted.get("webhooks") != expected_webhooks:
        raise GuardError("server dry-run changed fields outside the condition list")
    receipt = {
        "phase": "server-dry-run" if not args.apply else "planned-before-apply",
        "context": CONTEXT,
        "configuration": CONFIG_NAME,
        "webhook": WEBHOOK_NAME,
        "webhook_namespace": WEBHOOK_NAMESPACE,
        "service": WEBHOOK_SERVICE,
        "uid": state["uid"],
        "resourceVersion": state["resourceVersion"],
        "old_conditions": state["old_conditions"],
        "new_conditions": state["new_conditions"],
        "safe_projection": state["old_projection"],
        "patch": operations,
    }
    if not args.apply:
        if args.receipt:
            _write_receipt(args.receipt, receipt, create=True)
        print(json.dumps({"mode": "server-dry-run", "receipt": receipt}, indent=2, sort_keys=True))
        return 0
    if os.geteuid() != 0:
        raise GuardError("--apply is restricted to root")
    if not args.receipt:
        raise GuardError("--apply requires --receipt PATH")
    _write_receipt(args.receipt, receipt, create=True)
    try:
        kube.patch(operations, dry_run=False)
        post = kube.get()
        if post["metadata"]["uid"] != state["uid"] or post.get("webhooks") != expected_webhooks:
            raise GuardError("post-apply webhook identity or fields differ from the admitted patch")
        post_index = _target_index(post)
        post_projection = safe_projection(post["webhooks"][post_index])
        if _without_conditions(post_projection) != _without_conditions(state["old_projection"]):
            raise GuardError("post-apply safe webhook fields changed unexpectedly")
        post_webhook = post["webhooks"][post_index]
        if post_webhook.get("matchConditions") != state["new_conditions"]:
            raise GuardError("post-apply matchConditions do not match the receipt")
        receipt["phase"] = "applied-verified"
        _write_receipt(args.receipt, receipt, create=False)
        print(json.dumps({"mode": "applied-verified", "configuration": CONFIG_NAME, "webhook": WEBHOOK_NAME}, indent=2))
        return 0
    except Exception:
        receipt["phase"] = "applied-unverified"
        _write_receipt(args.receipt, receipt, create=False)
        raise


def _rollback(args: argparse.Namespace) -> int:
    if not args.receipt:
        raise GuardError("--rollback requires --receipt PATH")
    receipt = _read_receipt(args.receipt)
    if receipt.get("phase") not in {"planned-before-apply", "applied-unverified", "applied-verified"}:
        raise GuardError("rollback receipt is not an eligible webhook receipt")
    if receipt.get("context") != CONTEXT or receipt.get("configuration") != CONFIG_NAME or receipt.get("webhook") != WEBHOOK_NAME:
        raise GuardError("rollback receipt identity does not match the reviewed webhook")
    kube = Kubectl(args.kubectl, args.context)
    configuration = kube.get()
    uid, resource_version = _identity(configuration)
    if uid != receipt.get("uid"):
        raise GuardError("rollback webhook UID differs from the receipt")
    index = _target_index(configuration)
    webhook = configuration["webhooks"][index]
    current_projection = safe_projection(webhook)
    if current_projection.get("matchConditions") != receipt.get("new_conditions"):
        raise GuardError("rollback refused: current matchConditions differ from receipt")
    if _without_conditions(current_projection) != _without_conditions(receipt.get("safe_projection", {})):
        raise GuardError("rollback refused: safe webhook fields differ from receipt")
    operations = build_patch(
        uid=uid, resource_version=resource_version, index=index,
        old_conditions=receipt["new_conditions"], new_conditions=receipt["old_conditions"],
        has_conditions=True, rules=receipt["safe_projection"]["rules"],
    )
    kube.patch(operations, dry_run=True)
    if not args.apply:
        print(json.dumps({"mode": "rollback-server-dry-run", "patch": operations}, indent=2, sort_keys=True))
        return 0
    if os.geteuid() != 0:
        raise GuardError("--apply is restricted to root")
    kube.patch(operations, dry_run=False)
    post = kube.get()
    expected = copy.deepcopy(configuration["webhooks"])
    expected[index]["matchConditions"] = receipt["old_conditions"]
    if post["metadata"]["uid"] != uid or post.get("webhooks") != expected:
        raise GuardError("rollback did not preserve webhook identity and all other fields")
    receipt["phase"] = "rolled-back"
    _write_receipt(args.receipt, receipt, create=False)
    print(json.dumps({"mode": "rolled-back", "configuration": CONFIG_NAME, "webhook": WEBHOOK_NAME}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform explicit mutation; root only")
    parser.add_argument("--rollback", action="store_true", help="guarded rollback using --receipt")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--context", default=CONTEXT)
    parser.add_argument("--kubectl", default=KUBECTL)
    args = parser.parse_args(argv)
    if args.context != CONTEXT:
        parser.error(f"--context must be exactly {CONTEXT}")
    if args.rollback and not args.receipt:
        parser.error("--rollback requires --receipt PATH")
    if args.apply and os.geteuid() != 0:
        parser.error("--apply is restricted to root")
    try:
        return _rollback(args) if args.rollback else _forward(args)
    except (GuardError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"legacy webhook change refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
