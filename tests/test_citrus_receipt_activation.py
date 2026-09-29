"""Cross-file authority boundaries for the receipt deployment."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
PARSER = YAML(typ="safe")
CAPABILITY = "agent_workloads.citrus_receipt_draft"


def load(path):
    return PARSER.load((ROOT / path).read_text())


class CitrusReceiptActivationTests(unittest.TestCase):
    def test_receipt_grant_has_only_the_private_owner_binding(self):
        policy = load("apps/agent-control-plane-registry-overlay/registry/policy.prod.yaml")
        allowed = [b for b in policy["bindings"] if CAPABILITY in b.get("capabilities", {}).get("allow", [])]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed[0]["users"], {"admins": [], "authorized": ["94265880216612864"]})
        self.assertEqual(allowed[0]["surface_identifiers"], {"guild_id": "1523242748822425750", "channel_id": "1546428227411513488"})
        self.assertEqual(policy["defaults"]["max_runtime_seconds_per_capability"][CAPABILITY], 120)

    def test_worker_cannot_get_receipt_bytes_credentials_or_provider_network(self):
        values = load("apps/agent-workloads/values.yaml")
        worker = values["workers"]["citrus.receipt_draft"]
        self.assertFalse(worker["secretKeys"])
        self.assertFalse(worker["secretEnv"])
        self.assertFalse(any("CITRUS" in key for key in worker["env"]))
        self.assertEqual(worker["identity"]["mode"], "projected")
        self.assertIsNone(worker["identity"].get("previousRelease"))
        destinations = worker["networkPolicy"]["egress"]["destinations"]
        self.assertEqual(len(destinations), 1)
        self.assertEqual(destinations[0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"], "agent-control-plane")
        self.assertEqual(destinations[0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"], "api")
        self.assertNotIn("ipBlock", destinations[0])
        self.assertEqual({p["port"] for p in destinations[0]["ports"]}, {80, 8000})
        imports = load("apps/agent-control-plane-registry-overlay/registry/workload_imports.yaml")["imports"]
        item = next(i for i in imports if i["id"] == "citrus.receipt_draft")
        cap = item["capabilities"][CAPABILITY]
        self.assertEqual(cap["broker"]["allowed_broker"], "citrus_receipts")
        self.assertEqual(cap["broker_bounds"]["allowed_operations"], ["create_receipt_draft"])
        self.assertEqual(cap["broker_bounds"]["max_runtime_seconds"], 30)
        self.assertEqual(cap["session_authority_budget"]["max_operations"], 1)
        self.assertEqual(cap["result_contract"]["released_result_fields"], ["output_text"])
        self.assertFalse(cap["artifacts"]["allowed"])

    def test_only_core_api_gets_native_broker_transport(self):
        values = load("apps/agent-control-plane/values.yaml")
        providers = json.loads(values["apiEnv"]["AGENT_PLATFORM_BROKER_OPERATION_PROVIDERS_JSON"])
        receipt = next(p for p in providers if p["broker_id"] == "citrus_receipts")
        self.assertEqual(receipt["endpoint"], "https://10.108.0.8:8444/v1/execute")
        self.assertTrue(receipt["client_key_file"].startswith("/var/run/mandate/citrus-dev-receipts/"))
        self.assertNotIn("citrus_receipts", json.dumps(values.get("env", {})))
        self.assertNotIn("citrus-dev-receipts", json.dumps(values.get("extraVolumes", [])))
        secret = next(v for v in values["apiExtraVolumes"] if v["name"] == "citrus-dev-receipts-client-tls")
        self.assertEqual(secret["secret"]["defaultMode"], 0o440)
        self.assertEqual({i["key"] for i in secret["secret"]["items"]}, {"ca.crt", "tls.crt", "tls.key"})
        policy = load("apps/agent-control-plane-runtime-controls/citrus-receipts-networkpolicy.yaml")["spec"]
        self.assertEqual(policy["podSelector"]["matchLabels"], {"app.kubernetes.io/name": "agent-control-plane", "app.kubernetes.io/instance": "agent-control-plane", "app.kubernetes.io/component": "api"})
        self.assertEqual(policy["policyTypes"], ["Egress"])
        self.assertEqual(policy["egress"], [{"to": [{"ipBlock": {"cidr": "10.108.0.8/32"}}], "ports": [{"protocol": "TCP", "port": 8444}]}])
        for destination in values["networkPolicy"]["egress"]["destinations"]:
            self.assertNotIn(8444, [p["port"] for p in destination.get("ports", [])])
