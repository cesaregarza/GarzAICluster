from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.worker_sdk_receipt import SDKReceiptError, load_sdk_receipt


REPO_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = REPO_ROOT / "contracts/mandate-worker/receipt.json"


class WorkerSDKReceiptTests(unittest.TestCase):
    def test_hosted_receipt_has_valid_offline_identity(self) -> None:
        receipt = load_sdk_receipt(RECEIPT_PATH)

        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["distribution"], "mandate-worker")
        self.assertEqual(receipt["digest_spec_version"], "agent-workloads-code-digest-v2")
        self.assertEqual(
            receipt["source_revision"],
            "a442b69892d25c142bdd6fb260b6fe7f496af188",
        )
        self.assertEqual(
            receipt["protocol_revision"],
            "3ac9b254f698b4b267a130a3a2993162c4d79942",
        )

    def test_missing_receipt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SDKReceiptError):
                load_sdk_receipt(Path(directory) / "receipt.json")

    def test_receipt_rejects_malformed_identity_fields(self) -> None:
        original = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
        cases = {
            "schema_version": 2,
            "source_revision": "main",
            "protocol_revision": "1" * 39,
            "digest_spec_version": "",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as directory:
                    candidate = Path(directory) / "receipt.json"
                    mutated = dict(original)
                    mutated[field] = value
                    candidate.write_text(json.dumps(mutated), encoding="utf-8")
                    with self.assertRaises(SDKReceiptError):
                        load_sdk_receipt(candidate)

    def test_receipt_rejects_malformed_wheel_identity(self) -> None:
        original = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
        for field, value in (("filename", "latest.whl"), ("sha256", "bad")):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as directory:
                    candidate = Path(directory) / "receipt.json"
                    mutated = dict(original)
                    mutated["wheel"] = {**original["wheel"], field: value}
                    candidate.write_text(json.dumps(mutated), encoding="utf-8")
                    with self.assertRaises(SDKReceiptError):
                        load_sdk_receipt(candidate)
