import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML


SCRIPT = Path(__file__).parents[1] / "scripts" / "plan_controller_upgrade.py"
SPEC = importlib.util.spec_from_file_location("controller_upgrade_plan", SCRIPT)
plan = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(plan)
YAML_OUT = YAML()
YAML_SAFE = YAML(typ="safe")


class ControllerUpgradePlanTests(unittest.TestCase):
    def test_lock_has_sequential_minor_stages_and_sha256_values(self):
        lock = json.loads(plan.LOCK.read_text())
        cert = lock["cert_manager"]
        self.assertEqual([x[0] for x in cert], [
            "v1.7.3", "v1.8.2", "v1.9.2", "v1.10.2", "v1.11.5",
            "v1.12.17", "v1.13.6", "v1.14.7", "v1.15.5", "v1.16.5",
            "v1.17.4", "v1.18.6", "v1.19.6", "v1.20.3", "v1.21.1",
        ])
        self.assertTrue(all(len(value) == 64 for row in cert for value in row[1:]))
        self.assertEqual([x[0] for x in lock["argo_cd"]], ["v3.2.12", "v3.3.14", "v3.4.8", "v3.5.2"])

    def test_checksum_guard_rejects_changed_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload"
            path.write_text("changed")
            with self.assertRaises(ValueError, msg="changed artifacts must fail closed"):
                plan.verify_payloads(Path(directory), {"cert_manager": [["v1.7.3", "0" * 64, "0" * 64]], "argo_cd": []})

    def test_argo_composition_preserves_ksops_invariants(self):
        upstream = [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "argocd-cm", "namespace": "argocd"}, "data": {}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "argocd-repo-server", "namespace": "argocd"}, "spec": {"template": {"spec": {"containers": [{"name": "argocd-repo-server", "env": []}]}}}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "argocd-redis"}, "spec": {"template": {"spec": {"containers": [{"name": "redis", "args": ["--appendonly", "no"]}]}}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "argo.yaml"
            output = Path(directory) / "composed.yaml"
            stream = source.open("w")
            YAML_OUT.dump_all(upstream, stream)
            stream.close()
            original = plan.PATCH_DIR
            plan.PATCH_DIR = SCRIPT.parents[1] / "k8s" / "argocd"
            try:
                plan.compose_argo(source, output)
            finally:
                plan.PATCH_DIR = original
            rendered = list(YAML_SAFE.load_all(output.read_text()))
            cm = rendered[0]
            repo = rendered[1]["spec"]["template"]["spec"]
            self.assertEqual(cm["data"]["kustomize.buildOptions"], "--enable-alpha-plugins --enable-exec")
            self.assertNotIn("configManagementPlugins", cm.get("data", {}))
            self.assertEqual(next(x for x in repo["initContainers"] if x["name"] == "install-ksops")["image"], "viaductoss/ksops:v4.3.2")
            self.assertIn("ksops-tools", {x["name"] for x in repo["volumes"]})
            self.assertIn("sops-age", {x["name"] for x in repo["volumes"]})
            kubernetes_yaml = YAML(typ="safe")
            kubernetes_yaml.version = (1, 1)
            decoded = list(kubernetes_yaml.load_all(output.read_text()))
            self.assertEqual(decoded, rendered)
            self.assertEqual(decoded[2]["spec"]["template"]["spec"]["containers"][0]["args"], ["--appendonly", "no"])

    def test_plan_has_separate_review_and_apply_without_automatic_force(self):
        result = plan.build_plan({"cert_manager": [], "argo_cd": ["v3.5.2"]}, Path("/payloads"), Path("/plan"))
        self.assertEqual(len(result["argo_cd_review_commands"]), 1)
        self.assertEqual(len(result["argo_cd_apply_commands"]), 1)
        self.assertNotIn("&&", result["argo_cd_review_commands"][0])
        self.assertNotIn("force-conflicts", result["argo_cd_apply_commands"][0])

    def test_certificate_sources_pin_legacy_rotation_behavior(self):
        root = SCRIPT.parents[1]
        templates = list((root / "helm").glob("*/templates/certificate.yaml"))
        self.assertEqual(len(templates), 8)
        self.assertFalse((root / "k8s/argocd/certificate.yaml").exists())
        for path in templates:
            self.assertIn("rotationPolicy: Never", path.read_text(), path)
        shim_sources = [
            root / "helm/citrus/values.yaml",
            root / "helm/splattop/values.yaml",
            root / "helm/splatvote/values.yaml",
            root / "helm/splatvote/values-prod.yaml",
            root / "helm/splattop-teams/values-prod.yaml",
            root / "helm/poetry/templates/ingress.yaml",
            root / "helm/garz-observability/values-prod.yaml",
            root / "apps/vanity-hosts/values.yaml",
            root / "k8s/argocd/ingress.yaml",
        ]
        for path in shim_sources:
            self.assertIn("cert-manager.io/private-key-rotation-policy", path.read_text(), path)

    def test_live_rotation_patch_covers_exact_inventory(self):
        root = SCRIPT.parents[1]
        items = [item for item in YAML_SAFE.load_all((root / "ops/certificate-rotation-never-patch.yaml").read_text()) if item]
        self.assertEqual(len(items), 13)
        self.assertEqual(len({(item["metadata"]["namespace"], item["metadata"]["name"]) for item in items}), 13)
        self.assertTrue(all(item["spec"]["privateKey"]["rotationPolicy"] == "Never" for item in items))
        self.assertIn("private-key-rotation-policy", (root / "apps/agent-control-plane/values.yaml").read_text())


if __name__ == "__main__":
    unittest.main()
