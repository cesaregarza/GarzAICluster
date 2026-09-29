"""Offline tests of broker firewall installation; never execute host commands."""
import copy
import importlib.util
from pathlib import Path
import shlex
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "infra/hermes/citrus-receipts/deploy.py"
SPEC = importlib.util.spec_from_file_location("receipt_native", PATH)
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


class FakeRules:
    def __init__(self):
        self.rules = {"INPUT": [["-j", "ACCEPT"]], "DOCKER-USER": [["-j", "RETURN"]]}
        self.writes = 0

    def __call__(self, *args, check=True):
        operation, chain, *tail = args
        if operation == "-S":
            if chain not in self.rules:
                if check:
                    raise DEPLOY.DeploymentError("absent test chain")
                return ""
            lines = ["-N " + chain]
            for original in self.rules[chain]:
                rule = list(original)
                if "-p" in rule and "-m" not in rule:
                    rule.extend(["-m", "tcp"])
                if "ESTABLISHED,RELATED" in rule:
                    rule[rule.index("ESTABLISHED,RELATED")] = "RELATED,ESTABLISHED"
                lines.append(shlex.join(["-A", chain, *rule]))
            return "\n".join(lines)
        self.writes += 1
        if operation == "-N":
            if chain in self.rules:
                raise AssertionError("must not recreate or flush existing chain")
            self.rules[chain] = []
        elif operation == "-A":
            self.rules[chain].append(tail)
        elif operation == "-I":
            self.rules[chain].insert(int(tail[0]) - 1, tail[1:])
        else:
            raise AssertionError("unexpected firewall mutation")
        return ""


class ReceiptNativeFirewallTests(unittest.TestCase):
    def test_install_is_idempotent_and_preserves_unrelated_rules(self):
        fake = FakeRules()
        with patch.object(DEPLOY, "iptables", fake):
            DEPLOY.install_firewall()
            installed = copy.deepcopy(fake.rules)
            writes = fake.writes
            DEPLOY.install_firewall()
        self.assertEqual(fake.rules, installed)
        self.assertEqual(fake.writes, writes)
        self.assertEqual(fake.rules["INPUT"], [DEPLOY.INPUT_HOOK, ["-j", "ACCEPT"]])
        self.assertEqual(fake.rules["DOCKER-USER"], DEPLOY.DOCKER_HOOKS + [["-j", "RETURN"]])
        self.assertEqual(fake.rules[DEPLOY.CHAIN], DEPLOY.CHAIN_RULES)

    def test_existing_broadened_rule_fails_without_repairing_or_flushing(self):
        fake = FakeRules()
        with patch.object(DEPLOY, "iptables", fake):
            DEPLOY.install_firewall()
            rule = fake.rules[DEPLOY.CHAIN][2]
            rule[rule.index("143.244.222.41/32")] = "0.0.0.0/0"
            writes = fake.writes
            with self.assertRaises(DEPLOY.DeploymentError):
                DEPLOY.install_firewall()
            self.assertEqual(fake.writes, writes)

    def test_rule_normalization_rejects_broader_authority(self):
        DEPLOY.check_rule_normalizer()
        for rule in (["-j", "ACCEPT", "-j", "DROP"], ["!", "-i", "br-receipts", "-j", "ACCEPT"]):
            with self.assertRaises(DEPLOY.DeploymentError):
                DEPLOY.normalize_owned_rule(rule)

    def test_container_has_exact_config_keys_and_no_host_or_docker_mount(self):
        argv = DEPLOY.docker_run_argv()
        env = {argv[i + 1].split("=", 1)[0] for i, arg in enumerate(argv) if arg == "--env"}
        self.assertEqual(env, {"CITRUS_RECEIPTS_" + key for key in (
            "BROKER_HOST", "BROKER_PORT", "SERVER_CERT_FILE", "SERVER_KEY_FILE", "CLIENT_CA_FILE",
            "CLIENT_URI_SAN", "API_ORIGIN", "TOKEN_FILE", "SERVICE_IDENTITY", "READER_SOCKET", "READER_UID", "STATE_DIR")})
        self.assertEqual(argv[argv.index("--user") + 1], "994:986")
        self.assertEqual(argv[argv.index("--publish") + 1], "10.108.0.8:8444:8443/tcp")
        mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
        self.assertEqual(len(mounts), 3)
        self.assertFalse(any("/var/lib/hermes" in m or "docker.sock" in m for m in mounts))
        self.assertEqual(sum(m.endswith(",readonly") for m in mounts), 2)

    def test_empty_docker_ipam_is_safe_but_subnet_overlap_is_rejected(self):
        networks = [{"Name": "host", "IPAM": {"Config": None}}, {"Name": "none", "IPAM": {}}]
        def route_read(argv, **kwargs):
            return "[]" if "route" in argv else ""
        with patch.object(DEPLOY, "run", side_effect=route_read):
            DEPLOY.check_route_conflicts(networks)
            networks.append({"Name": "foreign", "IPAM": {"Config": [{"Subnet": "172.30.94.0/24"}]}})
            with self.assertRaises(DEPLOY.DeploymentError):
                DEPLOY.check_route_conflicts(networks)
