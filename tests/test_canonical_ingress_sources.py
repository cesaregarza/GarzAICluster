from pathlib import Path
import unittest

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[1]
YAML_SAFE = YAML(typ="safe")


def read_yaml(relative: str):
    with (ROOT / relative).open(encoding="utf-8") as stream:
        return YAML_SAFE.load(stream)


class CanonicalIngressSourceTests(unittest.TestCase):
    def test_all_application_owner_inputs_use_traefik_class(self):
        value_paths = {
            "agent-control-plane": ("apps/agent-control-plane/values.yaml", ("ingress", "className")),
            "cegarza-blog": ("helm/cegarza-blog/values-cegarza.yaml", ("ingress", "className")),
            "citrus": ("helm/citrus/values.yaml", ("ingress", "className")),
            "splattop": ("helm/splattop/values.yaml", ("ingress", "className")),
            "skyquiet": ("helm/skyquiet-server/values.yaml", ("ingress", "className")),
            "splattop-blog": ("helm/splattop-blog/values.yaml", ("ingress", "className")),
            "spotify": ("helm/spotify-hot-100/values.yaml", ("ingress", "className")),
            "garz-ai": ("helm/garz-ai/values.yaml", ("ingress", "className")),
            "grafana": ("helm/garz-observability/values-prod.yaml", ("monitoring", "grafana", "ingress", "className")),
            "poetry": ("helm/poetry/values.yaml", ("ingress", "className")),
            "vanity": ("apps/vanity-hosts/values.yaml", ("ingressClassName",)),
        }
        for owner, (path, keys) in value_paths.items():
            value = read_yaml(path)
            for key in keys:
                value = value[key]
            self.assertEqual(value, "traefik-nginx", owner)

        argocd = read_yaml("k8s/argocd/ingress.yaml")
        self.assertEqual(argocd["spec"]["ingressClassName"], "traefik-nginx")

    def test_citrus_keeps_edit_in_place_without_solver_class_overrides(self):
        ingress = read_yaml("helm/citrus/values.yaml")["ingress"]
        annotations = ingress["annotations"]
        self.assertEqual(annotations["acme.cert-manager.io/http01-edit-in-place"], "true")
        self.assertNotIn("acme.cert-manager.io/http01-ingress-ingressclassname", annotations)
        self.assertNotIn("kubernetes.io/ingress.class", annotations)
        self.assertNotIn("acme.cert-manager.io/http01-ingress-class", annotations)

    def test_cluster_issuer_uses_modern_http01_solver_field(self):
        values = read_yaml("helm/splattop/values.yaml")
        self.assertEqual(values["clusterIssuer"]["ingressClass"], "traefik-nginx")
        template = (ROOT / "helm/splattop/templates/cluster-issuer.yaml").read_text(encoding="utf-8")
        self.assertIn("ingressClassName: {{ .Values.clusterIssuer.ingressClass }}", template)
        self.assertNotIn("            class: {{ .Values.clusterIssuer.ingressClass }}", template)


if __name__ == "__main__":
    unittest.main()
