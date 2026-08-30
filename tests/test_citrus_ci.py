from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
RENDER_CHECK_PATH = (
    REPO_ROOT / "scripts" / "check_citrus_recurring_runtime_render.py"
)
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

    def test_python_contract_job_installs_pinned_helm(self) -> None:
        steps = {
            step["name"]: step
            for step in _workflow()["jobs"]["python-contracts"]["steps"]
            if isinstance(step, dict) and "name" in step
        }
        helm = steps["Set up Helm for chart contract tests"]
        self.assertEqual(helm["uses"], "azure/setup-helm@v4")
        self.assertEqual(helm["with"]["version"], "v3.14.0")

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

    def test_python_contracts_pin_the_helm_negative_test_version(self) -> None:
        steps = _workflow()["jobs"]["python-contracts"]["steps"]
        named_steps = {
            step["name"]: step
            for step in steps
            if isinstance(step, dict) and "name" in step
        }
        self.assertEqual(
            named_steps["Set up Helm for chart contract tests"],
            {
                "name": "Set up Helm for chart contract tests",
                "uses": "azure/setup-helm@v4",
                "with": {"version": "v3.14.0"},
            },
        )
        names = [
            step.get("name")
            for step in steps
            if isinstance(step, dict)
        ]
        self.assertLess(
            names.index("Set up Helm for chart contract tests"),
            names.index("Run Python contract tests"),
        )

    def test_actual_environment_and_safe_runtime_renders_are_distinct(self) -> None:
        step = self.steps[
            "Render Citrus production, dev, payment, and safe runtime"
        ]
        self.assertEqual(step["if"], "matrix.chart.name == 'citrus'")
        run = " ".join(step["run"].split())
        self.assertEqual(
            run,
            (
                "python3 scripts/check_citrus_recurring_runtime_render.py "
                "--chart helm/citrus --output-dir rendered --helm helm "
                "--skip-lint"
            ),
        )

    def test_current_safeguard_render_matrix_is_preserved(self) -> None:
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
                '-f "$chart/values-payment-dev.yaml" '
                '> rendered/citrus-dev.yaml'
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
                '--set-string paymentSafety.policy.revision=ces-850-ci '
                '--set paymentSafety.networkPolicy.enabled=true '
                '--set billingWorker.enabled=true '
                '--set-string billingWorker.topologyRevision=ces-850-ci '
                '--set recurringRuntime.enabled=true '
                '--set-string recurringRuntime.topologyRevision=ces-850-ci '
                '--set recurringRuntime.preflight.enabled=true '
                '--set-string '
                'recurringRuntime.preflight.topologyRevision=ces-850-ci '
                '--set-string '
                'recurringRuntime.health.topologyRevision=ces-850-ci '
                '> rendered/citrus-optional-workloads.yaml'
            ),
            (
                'helm template citrus "$chart" '
                '--namespace default -f "$chart/values.yaml" '
                '--show-only templates/sms-reconciliation-cronjob.yaml '
                '--set-string '
                'image.tag=0e2258bf95c6170895c26780258eb42d5b5c557c '
                '--set smsReconciliation.enabled=true '
                '--set-string '
                'smsReconciliation.verifiedImageTag='
                '0e2258bf95c6170895c26780258eb42d5b5c557c '
                '--set-string '
                "'smsReconciliation.commandCompatibleImageTags[0]="
                "0e2258bf95c6170895c26780258eb42d5b5c557c' "
                '--set-string smsReconciliation.secretName='
                'citrus-sms-reconciliation-runtime '
                '--set smsReconciliation.networkPolicy.enabled=true '
                '--set-string '
                'smsReconciliation.networkPolicy.provider=cilium '
                '--set-string '
                'smsReconciliation.networkPolicy.revision=ces-848-ci '
                '--set-string smsReconciliation.networkPolicy.database.host='
                'db.sms-reconciliation.example '
                '> rendered/citrus-sms-reconciliation-prod.yaml'
            ),
            (
                'helm template citrus-dev "$chart" '
                '--namespace citrus-dev -f "$chart/values.yaml" '
                '-f "$chart/values-dev.yaml" '
                '--show-only templates/sms-reconciliation-cronjob.yaml '
                '--set-string '
                'image.tag=0e2258bf95c6170895c26780258eb42d5b5c557c '
                '--set smsReconciliation.enabled=true '
                '--set-string '
                'smsReconciliation.verifiedImageTag='
                '0e2258bf95c6170895c26780258eb42d5b5c557c '
                '--set-string '
                "'smsReconciliation.commandCompatibleImageTags[0]="
                "0e2258bf95c6170895c26780258eb42d5b5c557c' "
                '--set-string smsReconciliation.secretName='
                'citrus-dev-sms-reconciliation-runtime '
                '--set smsReconciliation.networkPolicy.enabled=true '
                '--set-string '
                'smsReconciliation.networkPolicy.provider=cilium '
                '--set-string '
                'smsReconciliation.networkPolicy.revision=ces-848-ci '
                '--set-string smsReconciliation.networkPolicy.database.host='
                'db.sms-reconciliation.example '
                '> rendered/citrus-sms-reconciliation-dev.yaml'
            ),
            (
                'helm template citrus "$chart" '
                '--namespace default -f "$chart/values.yaml" '
                '-f "$chart/values-payment-prod.yaml" '
                '--set-string '
                'image.tag=4353f11595094bc4893b5799233cfd56c52aed89 '
                '--set billingWorker.enabled=true '
                '--set-string billingWorker.topologyRevision=ces-850-ci '
                '--set recurringRuntime.enabled=true '
                '--set-string recurringRuntime.expectedSourceRevision='
                '4353f11595094bc4893b5799233cfd56c52aed89 '
                '--set-string recurringRuntime.topologyRevision=ces-850-ci '
                '--set recurringRuntime.preflight.enabled=true '
                '--set-string '
                'recurringRuntime.preflight.topologyRevision=ces-850-ci '
                '--set-string '
                'recurringRuntime.health.topologyRevision=ces-850-ci '
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
                '> rendered/citrus-payment-prod.yaml'
            ),
            (
                'helm template citrus-dev "$chart" '
                '--namespace citrus-dev -f "$chart/values.yaml" '
                '-f "$chart/values-dev.yaml" '
                '-f "$chart/values-payment-dev.yaml" '
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
                '--set-string billingWorker.topologyRevision=ces-850-ci '
                '--set recurringRuntime.enabled=true '
                '--set-string recurringRuntime.topologyRevision=ces-850-ci '
                '--set recurringRuntime.preflight.enabled=true '
                '--set-string '
                'recurringRuntime.preflight.topologyRevision=ces-850-ci '
                '--set-string '
                'recurringRuntime.health.topologyRevision=ces-850-ci '
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
                '--set billingWorker.enabled=true '
                '--set-string billingWorker.topologyRevision=ces-850-ci '
                '--set recurringRuntime.enabled=true '
                '--set-string recurringRuntime.topologyRevision=ces-850-ci '
                '--set recurringRuntime.preflight.enabled=true '
                '--set-string '
                'recurringRuntime.preflight.topologyRevision=ces-850-ci '
                '--set-string '
                'recurringRuntime.health.topologyRevision=ces-850-ci '
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
        checker = RENDER_CHECK_PATH.read_text(encoding="utf-8")
        for component in (
            "migrations",
            "media-worker",
            "media-requeue",
            "media-gc",
            "billing-worker",
            "recurring-preflight",
            "recurring-tick",
            "recurring-health",
        ):
            with self.subTest(component=component):
                self.assertIn(
                    f"app.kubernetes.io/component: {component}",
                    checker,
                )
        for marker in (
            "app: citrus-web",
            "app.kubernetes.io/instance: citrus\\n",
            "app.kubernetes.io/instance: citrus-dev\\n",
            'ALLOWED_HOSTS: "citrus-grace.com,www.citrus-grace.com"',
            'SITE_NAME: "Citrus Grace Dev"',
            "    - host: dev.citrus-grace.com",
            'matchName: "citrus-media-dev.nyc3.digitaloceanspaces.com"',
            'citrus.grace/payment-egress-policy-revision: "ces-845-ci"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, checker)

        run = self.steps["Verify Citrus workload render coverage"]["run"]
        self.assertNotIn("uv run", run)
        self.assertIn("mapfile -t active_dev_images", run)
        self.assertIn(
            "active_dev_sha=${active_dev_images[0]##*:}",
            run,
        )
        self.assertNotIn("ruamel", checker)
        for component in (
            "sms-reconciliation",
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
        self.assertIn("ces-844-test-mode-v2", run)
        self.assertIn("citrus-dev-payment-credentials", run)
        self.assertIn("STRIPE_WEBHOOK_SECRET_DEV", run)
        self.assertIn("must never project the production webhook field", run)
        self.assertIn(
            "Citrus CI renders must never contain Secret objects",
            run,
        )
        self.assertIn(
            "must never render a Secret object",
            checker,
        )
        self.assertIn(
            "Actual Citrus environment renders must keep SMS reconciliation disabled",
            run,
        )
        self.assertIn("suspend: true", run)
        self.assertIn("TWILIO_SMS_ENABLED", run)
        self.assertIn("sweep_manual_order_sms_attempts", run)
        self.assertIn("must not import a Secret with envFrom", run)
        self.assertIn("rendered a broad or provider Secret reference", run)
        self.assertIn("exactly six named runtime keys", run)
        for expected in (
            "kind: CiliumNetworkPolicy",
            'argocd.argoproj.io/sync-wave: "2"',
            "citrus.grace/sms-reconciliation-egress: restricted",
            "citrus.grace/sms-reconciliation-egress-policy-revision",
            "DNS-plus-exact-DB only",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, run)
        for expected in (
            "Citrus production Argo render must keep CES-845 disabled",
            "Citrus dev Argo render must activate CES-845 deny mode",
            "ces-845-dev-v1",
            "citrus-payment-safety-dev.yaml",
            "citrus-payment-safety-prod.yaml",
            "PAYMENT_EGRESS_POLICY_REVISION",
            "Development payment egress render must omit every Stripe destination",
            "citrus-cloudflare-access.yaml",
            "CLOUDFLARE_ACCESS_REQUIRED",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, run)
        for expected in (
            "citrus-payment-safety-dev",
            "citrus-payment-safety-prod",
            "RECURRING_RUNTIME_TOPOLOGY_REVISION",
            "must omit every Stripe destination",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, checker)
        for expected in (
            "active_dev=rendered/citrus-dev.yaml",
            "grep -Ec '^kind:[[:space:]]+CiliumNetworkPolicy$'",
            "grep -Fc 'citrus.grace/payment-egress-boundary: enabled'",
            "grep -Fc 'name: PAYMENT_EGRESS_POLICY_REVISION'",
            "grep -Fc 'matchName:'",
            "grep -Fc 'port: \"25060\"'",
            "matchName:.*stripe\\.(com|network)|toEntities:",
        ):
            with self.subTest(actual_dev_assertion=expected):
                self.assertIn(expected, run)

        render = self.steps[
            "Render Citrus production, dev, and optional workloads"
        ]["run"]
        for expected in (
            "rendered/citrus-stripe-smoke-enabled-dev.yaml",
            "--set stripeSmokeRunner.enabled=true",
        ):
            with self.subTest(stripe_smoke_render=expected):
                self.assertIn(expected, render)
        for expected in (
            "stripe_smoke=rendered/citrus-stripe-smoke-enabled-dev.yaml",
            "citrus-dev-stripe-smoke-runner-egress",
            "CITRUS_STRIPE_SMOKE_RUNNER",
            "ces-883-stripe-smoke-v1",
            'matchName: "api.stripe.com"',
            "Only the enabled dev runner render may contain Stripe egress",
            "must not add a ServiceAccount",
        ):
            with self.subTest(stripe_smoke_assertion=expected):
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
