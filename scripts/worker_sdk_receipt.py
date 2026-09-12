"""Load and validate the reviewed Mandate worker SDK receipt offline."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


REVISION_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
WHEEL_FILENAME_RE = re.compile(
    r"^mandate_worker-(?P<version>[A-Za-z0-9][A-Za-z0-9._-]*)-py3-none-any\.whl$"
)


class SDKReceiptError(ValueError):
    """Raised when a pinned SDK receipt is absent or malformed."""


def load_sdk_receipt(path: Path) -> dict[str, Any]:
    """Read a receipt and validate the fields needed by offline consumers."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SDKReceiptError(f"cannot read SDK receipt: {path}") from exc
    if not isinstance(loaded, dict):
        raise SDKReceiptError("invalid SDK receipt identity")
    if (
        type(loaded.get("schema_version")) is not int
        or loaded.get("schema_version") != 1
        or loaded.get("distribution") != "mandate-worker"
    ):
        raise SDKReceiptError("invalid SDK receipt identity")

    for key in ("source_revision", "protocol_revision"):
        value = loaded.get(key)
        if not isinstance(value, str) or REVISION_RE.fullmatch(value) is None:
            raise SDKReceiptError("SDK receipt must name immutable source revisions")
    source_repository = loaded.get("source_repository")
    if not isinstance(source_repository, str) or not source_repository:
        raise SDKReceiptError("SDK receipt source_repository must be non-empty")
    digest_spec_version = loaded.get("digest_spec_version")
    if not isinstance(digest_spec_version, str) or not digest_spec_version:
        raise SDKReceiptError("SDK receipt digest_spec_version must be non-empty")

    wheel = loaded.get("wheel")
    if not isinstance(wheel, dict):
        raise SDKReceiptError("invalid SDK wheel receipt")
    filename = wheel.get("filename")
    if not isinstance(filename, str):
        raise SDKReceiptError("invalid SDK wheel receipt")
    match = WHEEL_FILENAME_RE.fullmatch(filename)
    if match is None or not match.group("version"):
        raise SDKReceiptError("invalid SDK wheel filename")
    sha256 = wheel.get("sha256")
    if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
        raise SDKReceiptError("invalid SDK wheel receipt")
    return loaded
