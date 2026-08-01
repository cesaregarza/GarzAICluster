from __future__ import annotations

import shutil
import subprocess
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")
VALUES_PATH = REPO_ROOT / "helm/cegarza-blog/values-cegarza.yaml"
SECRET_DIRECTORY = REPO_ROOT / "secrets/cegarza-blog"
EXPECTED_SECRET_FILES = {
    "cegarza-blog-secrets.enc.yaml",
    "cegarza-apex-tls.enc.yaml",
    "regcred.enc.yaml",
}
LEGACY_SELECTOR = "splattop-blog"
CANONICAL_SELECTOR = "cegarza-blog"
LEGACY_IMAGE_REPOSITORY = (
    f"registry.digitalocean.com/sendouq/{LEGACY_SELECTOR}"
)
CANONICAL_IMAGE_REPOSITORY = (
    f"registry.digitalocean.com/sendouq/{CANONICAL_SELECTOR}"
)
LEGACY_SETTINGS_MODULE = "splattopblog.settings"
CANONICAL_SETTINGS_MODULE = "cegarza_site.settings"
PRIVATE_DATABASE_HOST = (
    "private-db-postgresql-nyc3-xscraper-do-user-15543770-0.c."
    "db.ondigitalocean.com"
)


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = YAML_PARSER.load(path)
    if not isinstance(payload, dict):
        raise AssertionError(f"{path} must contain one YAML mapping")
    return payload


def _render_cegarza() -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        raise unittest.SkipTest("helm is required for chart render tests")
    result = subprocess.run(
        [
            "helm",
            "template",
            "cegarza-blog",
            "helm/cegarza-blog",
            "--namespace",
            "cegarza-blog",
            "-f",
            "helm/cegarza-blog/values.yaml",
            "-f",
            "helm/cegarza-blog/values-cegarza.yaml",
        ],
        check=True,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return [
        document
        for document in YAML_PARSER.load_all(result.stdout)
        if isinstance(document, dict) and document
    ]


def _document(
    documents: list[dict[str, Any]],
    *,
    kind: str,
    name: str,
) -> dict[str, Any]:
    matches = [
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {kind}/{name}, found {len(matches)}")
    return matches[0]


def _rollout_phase(values: dict[str, Any]) -> str:
    blog = values["blog"]
    signature = (
        blog["image"]["repository"],
        blog["env"]["DJANGO_SETTINGS_MODULE"],
        blog["selectorName"],
        blog["replaceOnSync"],
        tuple(values["networkPolicy"]["selectorNames"]),
    )
    phases = {
        (
            LEGACY_IMAGE_REPOSITORY,
            LEGACY_SETTINGS_MODULE,
            LEGACY_SELECTOR,
            False,
            (LEGACY_SELECTOR, CANONICAL_SELECTOR),
        ): "substrate",
        (
            CANONICAL_IMAGE_REPOSITORY,
            CANONICAL_SETTINGS_MODULE,
            CANONICAL_SELECTOR,
            True,
            (LEGACY_SELECTOR, CANONICAL_SELECTOR),
        ): "activation",
        (
            CANONICAL_IMAGE_REPOSITORY,
            CANONICAL_SETTINGS_MODULE,
            CANONICAL_SELECTOR,
            False,
            (CANONICAL_SELECTOR,),
        ): "steady-state",
    }
    try:
        return phases[signature]
    except KeyError as exc:
        raise AssertionError(
            "cegarza-blog values must be exactly one safe rollout phase; "
            f"got {signature!r}"
        ) from exc


class CegarzaBlogContractTests(unittest.TestCase):
    def test_rollout_phase_state_machine_is_explicit(self) -> None:
        substrate = deepcopy(_load_yaml(VALUES_PATH))
        substrate["blog"]["image"]["repository"] = LEGACY_IMAGE_REPOSITORY
        substrate["blog"]["env"]["DJANGO_SETTINGS_MODULE"] = (
            LEGACY_SETTINGS_MODULE
        )
        substrate["blog"]["selectorName"] = LEGACY_SELECTOR
        substrate["blog"]["replaceOnSync"] = False
        substrate["networkPolicy"]["selectorNames"] = [
            LEGACY_SELECTOR,
            CANONICAL_SELECTOR,
        ]
        self.assertEqual(_rollout_phase(substrate), "substrate")

        activation = deepcopy(substrate)
        activation["blog"]["image"]["repository"] = CANONICAL_IMAGE_REPOSITORY
        activation["blog"]["env"]["DJANGO_SETTINGS_MODULE"] = (
            CANONICAL_SETTINGS_MODULE
        )
        activation["blog"]["selectorName"] = CANONICAL_SELECTOR
        activation["blog"]["replaceOnSync"] = True
        self.assertEqual(_rollout_phase(activation), "activation")

        steady_state = deepcopy(activation)
        steady_state["blog"]["replaceOnSync"] = False
        steady_state["networkPolicy"]["selectorNames"] = [CANONICAL_SELECTOR]
        self.assertEqual(_rollout_phase(steady_state), "steady-state")

        unordered = deepcopy(steady_state)
        unordered["networkPolicy"]["selectorNames"] = [
            LEGACY_SELECTOR,
            CANONICAL_SELECTOR,
        ]
        with self.assertRaisesRegex(AssertionError, "safe rollout phase"):
            _rollout_phase(unordered)

    def test_values_are_activated_apex_configuration(self) -> None:
        values = _load_yaml(VALUES_PATH)
        self.assertIn(
            _rollout_phase(values),
            {"substrate", "activation", "steady-state"},
        )
        self.assertEqual(values["global"]["environment"], "production")
        self.assertEqual(values["global"]["databaseSecretName"], "cegarza-blog-secrets")
        self.assertEqual(values["global"]["imagePullSecrets"], ["regcred"])
        self.assertEqual(values["fullnameOverride"], "cegarza-blog")

        blog = values["blog"]
        image = blog["image"]
        self.assertIn(
            image["repository"],
            {LEGACY_IMAGE_REPOSITORY, CANONICAL_IMAGE_REPOSITORY},
        )
        self.assertRegex(image["tag"], r"^v\d+\.\d+\.\d+$")
        self.assertRegex(image["digest"], r"^sha256:[a-f0-9]{64}$")
        self.assertNotEqual(image["digest"], f"sha256:{'0' * 64}")
        self.assertEqual(image["pullPolicy"], "IfNotPresent")
        self.assertEqual(blog["replicas"], 1)
        self.assertIn(
            blog["selectorName"],
            {LEGACY_SELECTOR, CANONICAL_SELECTOR},
        )
        self.assertIsInstance(blog["replaceOnSync"], bool)
        self.assertEqual(blog["strategy"], {"type": "Recreate"})
        self.assertEqual(blog["secretKeys"], ["DATABASE_URL", "DJANGO_SECRET_KEY"])

        environment = blog["env"]
        self.assertEqual(
            environment["ALLOWED_HOSTS"],
            "cegarza.com,www.cegarza.com,dev.cegarza.com",
        )
        self.assertEqual(
            environment["CSRF_TRUSTED_ORIGINS"],
            "https://cegarza.com,https://www.cegarza.com,https://dev.cegarza.com",
        )
        self.assertEqual(
            environment["WAGTAILADMIN_BASE_URL"],
            "https://cegarza.com/admin/",
        )
        self.assertEqual(environment["SITE_NAME"], "Bringing Down The Gauss")
        self.assertEqual(environment["SITE_DESCRIPTION"], "Thoughts, stories and ideas.")
        self.assertEqual(environment["SITE_AUTHOR"], "Cesar Garza")
        self.assertEqual(environment["USE_SPACES"], "false")
        self.assertEqual(environment["SERVE_MEDIA"], "true")
        self.assertEqual(environment["CSP_ENFORCE"], "true")
        self.assertEqual(environment["SECURE_HSTS_INCLUDE_SUBDOMAINS"], "false")
        self.assertEqual(environment["SECURE_HSTS_PRELOAD"], "false")
        # Apex is activated; the release must intentionally serve cegarza.com.
        self.assertIn("cegarza.com", environment["ALLOWED_HOSTS"].split(","))

        persistence = blog["persistence"]
        self.assertEqual(persistence["accessMode"], "ReadWriteOnce")
        self.assertEqual(persistence["storageClass"], "do-block-storage")
        self.assertEqual(persistence["size"], "5Gi")
        self.assertTrue(persistence["retainOnDelete"])
        self.assertTrue(blog["migrations"]["enabled"])
        self.assertEqual(blog["health"]["hostHeader"], "dev.cegarza.com")
        self.assertEqual(
            blog["databaseTLS"],
            {
                "enabled": True,
                "secretName": "cegarza-blog-secrets",
                "secretKey": "DB_CA_CERT",
                "caMountPath": "/etc/database-ca",
                "caFileName": "ca.crt",
            },
        )

        ingress = values["ingress"]
        self.assertEqual(
            [entry["host"] for entry in ingress["hosts"]],
            ["cegarza.com", "www.cegarza.com", "dev.cegarza.com"],
        )
        self.assertEqual(ingress["tls"]["secretName"], "cegarza-dev-tls")
        self.assertEqual(
            ingress["tls"]["certificate"]["dnsNames"],
            ["cegarza.com", "www.cegarza.com", "dev.cegarza.com"],
        )
        self.assertNotIn("cert-manager.io/cluster-issuer", ingress["annotations"])
        self.assertEqual(
            ingress["annotations"]["external-dns.alpha.kubernetes.io/ttl"],
            "60",
        )
        self.assertEqual(
            ingress["annotations"][
                "external-dns.alpha.kubernetes.io/cloudflare-proxied"
            ],
            "true",
        )

        network_policy = values["networkPolicy"]
        self.assertTrue(network_policy["enabled"])
        self.assertIn(
            network_policy["selectorNames"],
            [
                [LEGACY_SELECTOR, CANONICAL_SELECTOR],
                [CANONICAL_SELECTOR],
            ],
        )
        self.assertEqual(
            network_policy["egress"]["database"],
            {"host": PRIVATE_DATABASE_HOST, "port": 25060},
        )

    def test_argocd_apps_are_manual_and_orderable(self) -> None:
        workload = _load_yaml(REPO_ROOT / "argocd/applications/cegarza-blog.yaml")
        secrets = _load_yaml(
            REPO_ROOT / "argocd/applications/cegarza-blog-secrets.yaml"
        )
        for application in (workload, secrets):
            self.assertEqual(application["spec"]["project"], "splattop")
            self.assertEqual(
                application["spec"]["destination"],
                {
                    "server": "https://kubernetes.default.svc",
                    "namespace": "cegarza-blog",
                },
            )
            self.assertIsNone(application["spec"]["syncPolicy"]["automated"])

        self.assertEqual(
            workload["spec"]["source"]["helm"]["valueFiles"],
            ["values.yaml", "values-cegarza.yaml"],
        )
        self.assertEqual(workload["spec"]["source"]["path"], "helm/cegarza-blog")
        workload_options = workload["spec"]["syncPolicy"]["syncOptions"]
        self.assertIn("CreateNamespace=false", workload_options)
        secret_options = secrets["spec"]["syncPolicy"]["syncOptions"]
        self.assertIn("CreateNamespace=true", secret_options)
        self.assertIn("ServerSideApply=true", secret_options)
        self.assertNotIn("RespectIgnoreDifferences=true", secret_options)
        self.assertNotIn("ignoreDifferences", secrets["spec"])

    def test_project_and_sops_scope_are_explicit(self) -> None:
        project = _load_yaml(REPO_ROOT / "argocd/projects/splattop-project.yaml")
        destinations = project["spec"]["destinations"]
        self.assertIn(
            {
                "namespace": "cegarza-blog",
                "server": "https://kubernetes.default.svc",
            },
            destinations,
        )
        ignored_secret_names = {
            resource["name"]
            for resource in project["spec"]["orphanedResources"]["ignore"]
            if resource.get("group") == "" and resource.get("kind") == "Secret"
        }
        self.assertIn("cegarza-dev-tls", ignored_secret_names)
        self.assertNotIn("cegarza-preview-tls", ignored_secret_names)

        sops_config = _load_yaml(REPO_ROOT / ".sops.yaml")
        rules = {
            rule["path_regex"]: rule
            for rule in sops_config["creation_rules"]
            if isinstance(rule, dict) and "path_regex" in rule
        }
        rule = rules[r"^secrets/cegarza-blog/.*\.enc\.yaml$"]
        self.assertEqual(rule["encrypted_regex"], "^(data|stringData)$")
        self.assertRegex(rule["age"], r"^age1[0-9a-z]+$")

        generator = _load_yaml(SECRET_DIRECTORY / "ksops.yaml")
        self.assertEqual(set(generator["files"]), EXPECTED_SECRET_FILES)

    def test_only_sops_ciphertext_is_committed_for_secrets(self) -> None:
        actual_files = {
            path.name for path in SECRET_DIRECTORY.glob("*.enc.yaml") if path.is_file()
        }
        self.assertEqual(actual_files, EXPECTED_SECRET_FILES)
        for filename in sorted(EXPECTED_SECRET_FILES):
            path = SECRET_DIRECTORY / filename
            payload = _load_yaml(path)
            self.assertEqual(payload["metadata"]["namespace"], "cegarza-blog")
            self.assertIn("sops", payload)
            self.assertTrue(payload["data"])
            for value in payload["data"].values():
                self.assertIsInstance(value, str)
                self.assertTrue(value.startswith("ENC[AES256_GCM,"))

    def test_render_is_isolated_and_digest_pinned(self) -> None:
        values = _load_yaml(VALUES_PATH)
        _rollout_phase(values)
        blog = values["blog"]
        expected_image = f"{blog['image']['repository']}@{blog['image']['digest']}"
        documents = _render_cegarza()

        deployment = _document(documents, kind="Deployment", name="cegarza-blog")
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        self.assertFalse(pod_spec["automountServiceAccountToken"])
        self.assertEqual(pod_spec["imagePullSecrets"], [{"name": "regcred"}])
        self.assertEqual(container["image"], expected_image)
        self.assertEqual(
            deployment["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"],
            blog["selectorName"],
        )
        sync_options = deployment["metadata"]["annotations"].get(
            "argocd.argoproj.io/sync-options"
        )
        if blog["replaceOnSync"]:
            self.assertEqual(sync_options, "Force=true,Replace=true")
        else:
            self.assertIsNone(sync_options)
        self.assertTrue(container["securityContext"]["runAsNonRoot"])
        self.assertEqual(container["securityContext"]["runAsUser"], 1000)

        secret_env = {
            entry["name"]: entry["valueFrom"]["secretKeyRef"]
            for entry in container["env"]
            if "valueFrom" in entry
        }
        self.assertEqual(
            secret_env,
            {
                "DATABASE_URL": {
                    "name": "cegarza-blog-secrets",
                    "key": "DATABASE_URL",
                },
                "DJANGO_SECRET_KEY": {
                    "name": "cegarza-blog-secrets",
                    "key": "DJANGO_SECRET_KEY",
                },
            },
        )
        self.assertFalse(any(name.startswith("SPACES_") for name in secret_env))
        volume_names = {volume["name"] for volume in pod_spec["volumes"]}
        self.assertEqual(volume_names, {"media", "database-ca"})

        migration_jobs = [
            document for document in documents if document.get("kind") == "Job"
        ]
        self.assertEqual(len(migration_jobs), 1)
        migration_spec = migration_jobs[0]["spec"]["template"]["spec"]
        self.assertFalse(migration_spec["automountServiceAccountToken"])
        migration_container = migration_spec["containers"][0]
        self.assertEqual(migration_container["image"], expected_image)
        self.assertEqual(
            migration_container["command"],
            ["/app/.venv/bin/python", "manage.py", "migrate", "--noinput"],
        )

        pvc = _document(
            documents,
            kind="PersistentVolumeClaim",
            name="cegarza-blog-media",
        )
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(pvc["spec"]["storageClassName"], "do-block-storage")
        self.assertEqual(
            pvc["metadata"]["annotations"],
            {
                "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
                "helm.sh/resource-policy": "keep",
            },
        )

        ingress = _document(documents, kind="Ingress", name="cegarza-blog")
        self.assertEqual(
            [rule["host"] for rule in ingress["spec"]["rules"]],
            ["cegarza.com", "www.cegarza.com", "dev.cegarza.com"],
        )
        self.assertEqual(
            ingress["spec"]["tls"],
            [
                {
                    "hosts": ["cegarza.com", "www.cegarza.com", "dev.cegarza.com"],
                    "secretName": "cegarza-dev-tls",
                }
            ],
        )
        certificate = _document(
            documents,
            kind="Certificate",
            name="cegarza-blog-cert",
        )
        self.assertEqual(certificate["spec"]["secretName"], "cegarza-dev-tls")
        self.assertEqual(
            certificate["spec"]["dnsNames"],
            ["cegarza.com", "www.cegarza.com", "dev.cegarza.com"],
        )

        cilium_policy = _document(
            documents,
            kind="CiliumNetworkPolicy",
            name="cegarza-blog",
        )
        self.assertEqual(
            cilium_policy["spec"]["endpointSelector"],
            {
                "matchLabels": {
                    "app.kubernetes.io/instance": "cegarza-blog",
                },
                "matchExpressions": [
                    {
                        "key": "app.kubernetes.io/name",
                        "operator": "In",
                        "values": values["networkPolicy"]["selectorNames"],
                    }
                ],
            },
        )
        dns_egress = cilium_policy["spec"]["egress"][0]
        self.assertEqual(
            dns_egress["toEndpoints"],
            [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "kube-system",
                        "k8s:k8s-app": "kube-dns",
                    }
                }
            ],
        )
        self.assertEqual(
            dns_egress["toPorts"][0]["rules"]["dns"],
            [{"matchPattern": "*"}],
        )
        fqdn_rules = cilium_policy["spec"]["egress"][1]["toFQDNs"]
        self.assertEqual(fqdn_rules, [{"matchName": PRIVATE_DATABASE_HOST}])

    def test_selector_activation_replaces_only_the_deployment(self) -> None:
        if shutil.which("helm") is None:
            raise unittest.SkipTest("helm is required for chart render tests")
        result = subprocess.run(
            [
                "helm",
                "template",
                "cegarza-blog",
                "helm/cegarza-blog",
                "--namespace",
                "cegarza-blog",
                "-f",
                "helm/cegarza-blog/values.yaml",
                "-f",
                "helm/cegarza-blog/values-cegarza.yaml",
                "--set",
                "blog.selectorName=cegarza-blog",
                "--set",
                "blog.replaceOnSync=true",
                "--set-json",
                'networkPolicy.selectorNames=["splattop-blog","cegarza-blog"]',
            ],
            check=True,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        documents = [
            document
            for document in YAML_PARSER.load_all(result.stdout)
            if isinstance(document, dict) and document
        ]
        deployment = _document(
            documents,
            kind="Deployment",
            name="cegarza-blog",
        )
        self.assertEqual(
            deployment["metadata"]["annotations"][
                "argocd.argoproj.io/sync-options"
            ],
            "Force=true,Replace=true",
        )
        self.assertEqual(
            deployment["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"],
            "cegarza-blog",
        )

        for document in documents:
            if document is deployment:
                continue
            self.assertNotEqual(
                document.get("metadata", {})
                .get("annotations", {})
                .get("argocd.argoproj.io/sync-options"),
                "Force=true,Replace=true",
            )

        cilium_policy = _document(
            documents,
            kind="CiliumNetworkPolicy",
            name="cegarza-blog",
        )
        self.assertEqual(
            cilium_policy["spec"]["endpointSelector"]["matchExpressions"][0][
                "values"
            ],
            ["splattop-blog", "cegarza-blog"],
        )

    def test_render_orders_policy_migration_and_serving_resources(self) -> None:
        documents = _render_cegarza()
        cilium_policy = _document(
            documents,
            kind="CiliumNetworkPolicy",
            name="cegarza-blog",
        )
        migration = _document(
            documents,
            kind="Job",
            name="cegarza-blog-migrate-1",
        )
        deployment = _document(
            documents,
            kind="Deployment",
            name="cegarza-blog",
        )
        service = _document(
            documents,
            kind="Service",
            name="cegarza-blog",
        )
        ingress = _document(
            documents,
            kind="Ingress",
            name="cegarza-blog",
        )

        cilium_annotations = cilium_policy["metadata"]["annotations"]
        migration_annotations = migration["metadata"]["annotations"]
        serving_resources = (deployment, service, ingress)

        self.assertEqual(
            cilium_annotations["argocd.argoproj.io/sync-wave"],
            "-2",
        )
        self.assertNotIn("argocd.argoproj.io/hook", cilium_annotations)
        self.assertEqual(
            migration_annotations,
            {
                "helm.sh/hook": "pre-install,pre-upgrade",
                "helm.sh/hook-weight": "-1",
                "helm.sh/hook-delete-policy": (
                    "before-hook-creation,hook-succeeded"
                ),
                "argocd.argoproj.io/hook": "Sync",
                "argocd.argoproj.io/sync-wave": "-1",
                "argocd.argoproj.io/hook-delete-policy": (
                    "BeforeHookCreation,HookSucceeded"
                ),
            },
        )
        for resource in serving_resources:
            annotations = resource["metadata"]["annotations"]
            self.assertEqual(
                annotations["argocd.argoproj.io/sync-wave"],
                "0",
            )
            self.assertNotIn("argocd.argoproj.io/hook", annotations)

        self.assertLess(
            int(cilium_annotations["argocd.argoproj.io/sync-wave"]),
            int(migration_annotations["argocd.argoproj.io/sync-wave"]),
        )
        self.assertLess(
            int(migration_annotations["argocd.argoproj.io/sync-wave"]),
            int(
                deployment["metadata"]["annotations"][
                    "argocd.argoproj.io/sync-wave"
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
