from __future__ import annotations

import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'helm/splattop'


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.languages = []
        self.external_resources = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'article':
            self.languages.append(attrs.get('lang'))
        if tag in {'script', 'iframe', 'img'} or 'src' in attrs:
            self.external_resources.append(tag)
        if tag == 'link' and attrs.get('rel') == 'stylesheet':
            self.external_resources.append(tag)


@unittest.skipUnless(shutil.which('helm'), 'Helm is required for chart contracts')
class SplatTopFarewellTests(unittest.TestCase):
    @staticmethod
    def render(*settings):
        command = ['helm', 'template', 'splattop-prod', str(CHART),
                   '--namespace', 'default', '-f', str(CHART / 'values-prod.yaml')]
        for setting in settings:
            command += ['--set', setting]
        return subprocess.run(command, text=True, capture_output=True)

    @classmethod
    def setUpClass(cls):
        result = cls.render()
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.docs = [doc for doc in YAML(typ='safe').load_all(result.stdout) if doc]

    def test_only_static_server_and_sendou_proxy_remain(self):
        for kind in ('Deployment', 'Service'):
            self.assertEqual(sorted(doc['metadata']['name'] for doc in self.docs if doc['kind'] == kind),
                             ['splattop-prod-farewell', 'splattop-prod-sendou-proxy'])
        self.assertFalse(any(doc['kind'] in {'Job', 'CronJob', 'Secret', 'PodDisruptionBudget'}
                             for doc in self.docs))
        for deployment in (doc for doc in self.docs if doc['kind'] == 'Deployment'):
            pod = deployment['spec']['template']['spec']
            with self.subTest(deployment=deployment['metadata']['name']):
                self.assertFalse(pod['automountServiceAccountToken'])
                self.assertNotIn('env', pod['containers'][0])
                self.assertNotIn('envFrom', pod['containers'][0])
                self.assertFalse(any('secret' in vol or 'persistentVolumeClaim' in vol for vol in pod['volumes']))
                self.assertRegex(pod['containers'][0]['image'], r'@sha256:[0-9a-f]{64}$')

    def test_both_hosts_only_exempt_sendou_from_the_static_server(self):
        ingress = next(doc for doc in self.docs if doc['kind'] == 'Ingress')
        self.assertEqual(ingress['metadata']['name'], 'prod-ingress')
        self.assertEqual(ingress['spec']['tls'][0]['secretName'], 'tls-secret')
        rules = ingress['spec']['rules']
        self.assertEqual({rule['host'] for rule in rules}, {'splat.top', 'comp.splat.top'})
        for rule in rules:
            self.assertEqual(rule['http']['paths'], [{
                'path': '/api/sendou', 'pathType': 'Prefix',
                'backend': {'service': {'name': 'splattop-prod-sendou-proxy', 'port': {'number': 8000}}},
            }, {
                'path': '/', 'pathType': 'Prefix',
                'backend': {'service': {'name': 'splattop-prod-farewell', 'port': {'number': 8080}}},
            }])

    def test_retained_redis_claim_has_no_running_consumer(self):
        claim = next(doc for doc in self.docs if doc['kind'] == 'PersistentVolumeClaim')
        self.assertEqual(claim['metadata']['name'], 'splattop-prod-redis-data')
        self.assertEqual(claim['spec']['storageClassName'], 'do-block-storage-retain')
        self.assertEqual(claim['metadata']['annotations']['argocd.argoproj.io/sync-options'],
                         'Prune=false,Delete=false')

    def test_four_languages_are_available_without_scripts_or_external_resources(self):
        config = next(doc for doc in self.docs if doc['metadata']['name'] == 'splattop-prod-farewell'
                      and doc['kind'] == 'ConfigMap')['data']
        parser = PageParser()
        parser.feed(config['index.html'])
        self.assertEqual(parser.languages, ['en', 'es', 'ja', 'zh-Hans'])
        self.assertEqual(parser.external_resources, [])

    def test_sendou_process_overrides_the_legacy_api_entrypoint(self):
        deployment = next(doc for doc in self.docs if doc['kind'] == 'Deployment'
                          and doc['metadata']['name'] == 'splattop-prod-sendou-proxy')
        self.assertEqual(deployment['spec']['replicas'], 1)
        container = deployment['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['command'], ['/app/.venv/bin/python', '-B', '-m', 'uvicorn'])
        self.assertEqual(container['ports'], [{'name': 'http', 'containerPort': 8000}])
        self.assertEqual(container['args'][:3], ['app:app', '--app-dir', '/etc/sendou-proxy'])
        self.assertEqual(container['resources']['limits']['memory'], '128Mi')

    def test_sendou_requires_retirement_mode_and_an_immutable_image(self):
        for setting in ('farewell.enabled=false', 'sendouProxy.image.digest=sha256:invalid'):
            with self.subTest(setting=setting):
                self.assertNotEqual(self.render(setting).returncode, 0)

    def test_accidentally_reenabled_legacy_components_fail_closed(self):
        for component in ('fastapi', 'react', 'celeryWorker', 'celeryBeat', 'redis', 'splatnlp', 'warmupJob'):
            with self.subTest(component=component):
                result = self.render(component + '.enabled=true')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('farewell.enabled requires all legacy application components', result.stderr)


if __name__ == '__main__':
    unittest.main()
