#!/usr/bin/env python3
"""Compute a broker-operation pin from public inputs in the selected Core env."""

from __future__ import annotations

import base64
import json
import sys
from typing import Any

from mandate.adapters.broker_operation.mtls import (
    BrokerOperationEndpoint,
    PROTOCOL,
    broker_operation_fingerprint,
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def main() -> int:
    try:
        payload = json.load(sys.stdin, object_pairs_hook=_unique_object)
        if not isinstance(payload, dict) or set(payload) != {
            "config", "ca_pem_base64", "client_cert_pem_base64"
        }:
            raise ValueError("invalid helper input")
        config = BrokerOperationEndpoint.model_validate(payload["config"])
        ca_pem = base64.b64decode(payload["ca_pem_base64"], validate=True)
        client_cert_pem = base64.b64decode(payload["client_cert_pem_base64"], validate=True)
        digest = broker_operation_fingerprint(
            config, ca_pem=ca_pem, client_cert_pem=client_cert_pem
        )
    except Exception as exc:
        print(f"broker operation fingerprint input invalid: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({"digest": digest, "protocol": PROTOCOL}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
