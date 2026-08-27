from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
YAML_PARSER = YAML(typ="safe")


def _workflow() -> dict[str, Any]:
    loaded = YAML_PARSER.load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"YAML mapping expected: {WORKFLOW_PATH}")
    return loaded


class CitrusCiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.job = _workflow()["jobs"]["helm-and-kubeconform"]
        cls.steps = {
            step["name"]: step
            for step in cls.job["steps"]
            if isinstance(step, dict) and "name" in step
        }

    def test_citrus_is_a_first_class_helm_matrix_entry(self) -> None:
        citrus = next(
            chart
            for chart in self.job["strategy"]["matrix"]["chart"]
            if chart["name"] == "citrus"
        )
        self.assertEqual(
            citrus,
            {
                "name": "citrus",
                "path": "helm/citrus",
                "release": "citrus",
                "prod_values": "helm/citrus/values.yaml",
            },
        )
        self.assertEqual(
            self.steps["Helm lint"]["run"],
            'helm lint "${{ matrix.chart.path }}"',
        )

    def test_actual_environment_and_optional_renders_are_distinct(self) -> None:
        step = self.steps[
            "Render Citrus production, dev, and optional workloads"
        ]
        self.assertEqual(step["if"], "matrix.chart.name == 'citrus'")
        run = " ".join(step["run"].replace("\\\n", " ").split())
        for expected in (
            (
                'helm template citrus "$chart" --namespace default '
                '-f "$chart/values.yaml" > rendered/citrus-prod.yaml'
            ),
            (
                'helm template citrus-dev "$chart" --namespace citrus-dev '
                '-f "$chart/values.yaml" -f "$chart/values-dev.yaml" '
                '> rendered/citrus-dev.yaml'
            ),
            (
                'helm template citrus-ci-workloads "$chart" '
                '--namespace citrus-ci -f "$chart/values.yaml" '
                '--set billingWorker.enabled=true '
                '--set recurringRuntime.enabled=true '
                '> rendered/citrus-optional-workloads.yaml'
            ),
            (
                'helm template citrus "$chart" '
                '--namespace default -f "$chart/values.yaml" '
                '-f "$chart/values-payment-prod.yaml" '
                '--set-string '
                'image.tag=4353f11595094bc4893b5799233cfd56c52aed89 '
                '--set billingWorker.enabled=true '
                '--set recurringRuntime.enabled=true '
                '--set directOrderPaymentSweep.enabled=true '
                '--set-string directOrderPaymentSweep.runtimeSecretName='
                'citrus-ci-direct-order-runtime '
                '--set-string directOrderPaymentSweep.verifiedImageTag='
                '4353f11595094bc4893b5799233cfd56c52aed89 '
                '--set paymentSafety.enabled=true '
                '--set-string paymentSafety.environment=production '
                '--set-string paymentSafety.owner=citrus '
                '--set-string paymentSafety.networkMode=allow '
                '--set paymentSafety.policy.required=true '
                '--set-string paymentSafety.policy.provider=cilium '
                '--set-string paymentSafety.policy.revision=ces-807-ci '
                '--set paymentSafety.networkPolicy.enabled=true '
                '> rendered/citrus-schedulers.yaml'
            ),
            (
                'helm template citrus "$chart" '
                '--namespace default -f "$chart/values.yaml" '
                '-f "$chart/values-payment-prod.yaml" '
                '--set paymentSafety.enabled=true '
                '--set-string paymentSafety.environment=production '
                '--set-string paymentSafety.owner=citrus '
                '--set-string paymentSafety.networkMode=allow '
                '--set paymentSafety.policy.required=true '
                '--set-string paymentSafety.policy.provider=cilium '
                '--set-string paymentSafety.policy.revision=ces-845-ci '
                '--set paymentSafety.networkPolicy.enabled=true '
                '> rendered/citrus-payment-prod.yaml'
            ),
            (
                'helm template citrus-dev "$chart" '
                '--namespace citrus-dev -f "$chart/values.yaml" '
                '-f "$chart/values-dev.yaml" '
                '-f "$chart/values-payment-dev.yaml" '
                '--set paymentSafety.enabled=true '
                '--set-string paymentSafety.environment=development '
                '--set-string paymentSafety.owner=citrus-dev '
                '--set-string paymentSafety.networkMode=deny '
                '--set paymentSafety.policy.required=true '
                '--set-string paymentSafety.policy.provider=cilium '
                '--set-string paymentSafety.policy.revision=ces-845-ci '
                '--set paymentSafety.networkPolicy.enabled=true '
                '--set-string '
                'paymentSafety.networkPolicy.database.host=db.dev.example '
                '> rendered/citrus-payment-dev.yaml'
            ),
            (
                'helm template citrus-dev "$chart" '
                '--namespace citrus-dev -f "$chart/values.yaml" '
                '-f "$chart/values-dev.yaml" '
                '--set paymentSafety.enabled=true '
                '--set-string paymentSafety.environment=development '
                '--set-string paymentSafety.owner=citrus-dev '
                '--set-string paymentSafety.networkMode=deny '
                '--set paymentSafety.policy.required=true '
                '--set-string paymentSafety.policy.provider=cilium '
                '--set-string paymentSafety.policy.revision=ces-845-ci '
                '--set paymentSafety.networkPolicy.enabled=true '
                '--set-string '
                'paymentSafety.networkPolicy.database.host=db.dev.example '
                '--set billingWorker.enabled=true '
                '--set recurringRuntime.enabled=true '
                '> rendered/citrus-payment-safety-dev.yaml'
            ),
            (
                'helm template citrus "$chart" '
                '--namespace default -f "$chart/values.yaml" '
                '--set paymentSafety.enabled=true '
                '--set-string paymentSafety.environment=production '
                '--set-string paymentSafety.owner=citrus '
                '--set-string paymentSafety.networkMode=allow '
                '--set paymentSafety.policy.required=true '
                '--set-string paymentSafety.policy.provider=cilium '
                '--set-string paymentSafety.policy.revision=ces-845-ci '
                '--set paymentSafety.networkPolicy.enabled=true '
                '> rendered/citrus-payment-safety-prod.yaml'
            ),
            (
                'helm template citrus-ci-cloudflare-access "$chart" '
                '--namespace default -f "$chart/values.yaml" '
                '--set cloudflareAccess.enabled=true '
                '--set-string cloudflareAccess.owner=citrus '
                '--set-string '
                'cloudflareAccess.secretName=citrus-cloudflare-access '
                '--set-string cloudflareAccess.rolloutRevision=ces-829-ci '
                '--set-string '
                'cloudflareAccess.verifiedImageTag="$cloudflare_sha" '
                '--set-string image.tag="$cloudflare_sha" '
                '> rendered/citrus-cloudflare-access.yaml'
            ),
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, run)

    def test_workload_coverage_and_secret_exclusion_are_enforced(self) -> None:
        run = self.steps["Verify Citrus workload render coverage"]["run"]
        for component in (
            "migrations",
            "media-worker",
            "media-requeue",
            "media-gc",
            "billing-worker",
            "recurring-tick",
            "recurring-health",
            "direct-order-payment-sweep",
        ):
            with self.subTest(component=component):
                self.assertIn(
                    f"app.kubernetes.io/component: {component}",
                    run,
                )
        self.assertIn("app: citrus-web", run)
        self.assertIn("suspend: true", run)
        self.assertIn("citrus.grace/verified-image-tag", run)
        self.assertIn("citrus-ci-direct-order-runtime", run)
        self.assertIn(
            "Citrus CI renders must never contain Secret objects",
            run,
        )
        for expected in (
            "Current Citrus Argo renders must keep CES-845 disabled",
            "citrus-payment-safety-dev.yaml",
            "citrus-payment-safety-prod.yaml",
            "PAYMENT_EGRESS_POLICY_REVISION",
            "Development payment egress render must omit every Stripe destination",
            "citrus-cloudflare-access.yaml",
            "CLOUDFLARE_ACCESS_REQUIRED",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, run)

    def test_pinned_strict_kubeconform_covers_every_render(self) -> None:
        helm_setup = self.steps["Set up Helm"]
        self.assertEqual(helm_setup["with"]["version"], "v3.14.0")
        self.assertIn(
            "releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz",
            self.steps["Install kubeconform"]["run"],
        )

        run = " ".join(self.steps["Run kubeconform"]["run"].split())
        self.assertIn("for file in rendered/*.yaml", run)
        self.assertIn("-skip CiliumNetworkPolicy,Certificate", run)
        self.assertIn("-strict -summary -exit-on-error", run)

        artifact = self.steps["Upload rendered manifests (for debugging)"]
        self.assertEqual(
            artifact["with"]["name"],
            "rendered-manifests-${{ matrix.chart.name }}",
        )
        self.assertEqual(artifact["with"]["path"], "rendered")


if __name__ == "__main__":
    unittest.main()
