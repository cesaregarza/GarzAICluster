from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'helm/agent-workloads'
PROD = ROOT / 'apps/agent-workloads/values.yaml'
FIXTURE = ROOT / 'tests/fixtures/citrus-purchasing-broker-enabled.yaml'
YAML_SAFE = YAML(typ='safe')
DIGEST = 'sha256:216e824e52a2e4e3c8ea09f6ddf6d24633947d3dc143dcdabe61e9b116f3ef59'
LABELS = {
    'app.kubernetes.io/name': 'citrus-purchasing-broker',
    'app.kubernetes.io/instance': 'agent-workloads',
    'app.kubernetes.io/component': 'citrus-purchasing-broker',
}


def fixture():
    return YAML_SAFE.load(FIXTURE.read_text())


def render(values, *, production=False, release='agent-workloads'):
    helm = shutil.which('helm')
    if not helm:
        raise unittest.SkipTest('Helm is required for chart rendering')
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'values.yaml'
        with path.open('w') as stream:
            YAML_SAFE.dump(values, stream)
        cmd = [helm, 'template', release, str(CHART)]
        if production:
            cmd += ['-f', str(PROD)]
        result = subprocess.run(cmd + ['-f', str(path)], cwd=ROOT,
                                capture_output=True, text=True, check=False)
    return result


def documents(result):
    if result.returncode:
        raise AssertionError(result.stderr)
    return [d for d in YAML_SAFE.load_all(result.stdout) if d]


def broker_docs(docs):
    return [d for d in docs if d.get('metadata', {}).get('labels', {}).get(
        'app.kubernetes.io/component') == 'citrus-purchasing-broker']


class CitrusPurchasingBrokerChartTests(unittest.TestCase):
    def test_disabled_and_existing_worker_parity(self):
        for production in (False, True):
            with self.subTest(production=production):
                baseline = render(
                    {'citrusPurchasingBroker': {'enabled': False}},
                    production=production,
                )
                disabled = fixture()
                disabled['citrusPurchasingBroker']['enabled'] = False
                self.assertEqual(baseline.returncode, 0, baseline.stderr)
                disabled_result = render(disabled, production=production)
                self.assertEqual(disabled_result.returncode, 0, disabled_result.stderr)
                self.assertEqual(baseline.stdout, disabled_result.stdout)
                old = documents(baseline)
                self.assertEqual(broker_docs(old), [])
                enabled = documents(render(fixture(), production=production))
                self.assertEqual(len(broker_docs(enabled)), 4)
                self.assertEqual([d for d in enabled if d not in broker_docs(enabled)], old)
                self.assertEqual(len(enabled), len(old) + 4)

    def test_separate_identity_security_and_secret_mounts(self):
        docs = broker_docs(documents(render(fixture(), production=True)))
        self.assertEqual({d['kind'] for d in docs},
                         {'Deployment', 'Service', 'ServiceAccount', 'NetworkPolicy'})
        by_kind = {d['kind']: d for d in docs}
        deployment = by_kind['Deployment']
        name = deployment['metadata']['name']
        self.assertEqual({d['metadata']['name'] for d in docs}, {name})
        self.assertEqual(deployment['spec']['replicas'], 1)
        self.assertFalse(by_kind['ServiceAccount']['automountServiceAccountToken'])
        pod = deployment['spec']['template']
        self.assertEqual(deployment['spec']['selector']['matchLabels'], LABELS)
        for key, value in LABELS.items():
            self.assertEqual(pod['metadata']['labels'][key], value)
        self.assertIn('fixture-v1', pod['metadata']['annotations'].values())
        spec = pod['spec']
        self.assertEqual(spec['serviceAccountName'], name)
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertFalse(spec['enableServiceLinks'])
        self.assertEqual(spec['securityContext'], {
            'runAsNonRoot': True, 'runAsUser': 65532, 'runAsGroup': 65532,
            'fsGroup': 65532, 'seccompProfile': {'type': 'RuntimeDefault'},
        })
        self.assertEqual(len(spec['containers']), 1)
        container = spec['containers'][0]
        self.assertEqual(container['image'],
            'registry.digitalocean.com/sendouq/agent-workloads-citrus-purchasing-broker@' + DIGEST)
        self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertEqual(container['resources'], {
            'requests': {'cpu': '50m', 'memory': '64Mi'},
            'limits': {'cpu': '500m', 'memory': '256Mi'},
        })
        for probe in ('readinessProbe', 'startupProbe'):
            self.assertEqual(container[probe]['tcpSocket']['port'], 'https')
            self.assertNotIn('httpGet', container[probe])
        env = {e['name']: e['value'] for e in container['env']}
        self.assertEqual(env, {
            'CITRUS_PURCHASING_SERVER_CERT_FILE': '/var/run/citrus/server-tls/tls.crt',
            'CITRUS_PURCHASING_SERVER_KEY_FILE': '/var/run/citrus/server-tls/tls.key',
            'CITRUS_PURCHASING_CLIENT_CA_FILE': '/var/run/citrus/client-ca/ca.crt',
            'CITRUS_PURCHASING_TOKEN_FILE': '/var/run/citrus/purchasing-token/token',
            'CITRUS_PURCHASING_CLIENT_URI_SAN': 'spiffe://mandate.test/core/citrus-purchasing',
            'CITRUS_PURCHASING_API_ORIGIN': 'https://purchasing.example.test',
            'CITRUS_PURCHASING_BROKER_PORT': '8443',
        })
        mounts = {v['name']: v for v in container['volumeMounts']}
        volumes = {v['name']: v for v in spec['volumes']}
        self.assertEqual(set(volumes), {'server-tls', 'client-ca', 'purchasing-token', 'tmp'})
        self.assertEqual(volumes['tmp']['emptyDir']['sizeLimit'], '16Mi')
        expected = {
            'server-tls': ('citrus-broker-server-tls', ['tls.crt', 'tls.key']),
            'client-ca': ('citrus-broker-client-ca', ['ca.crt']),
            'purchasing-token': ('citrus-broker-purchasing-token', ['token']),
        }
        for volume, (secret, keys) in expected.items():
            self.assertTrue(mounts[volume]['readOnly'])
            self.assertEqual(mounts[volume]['mountPath'], '/var/run/citrus/' + volume)
            self.assertEqual(volumes[volume]['secret'], {
                'secretName': secret, 'defaultMode': 288,
                'items': [{'key': key, 'path': key} for key in keys],
            })
        service = by_kind['Service']['spec']
        self.assertEqual(service['type'], 'ClusterIP')
        self.assertEqual(service['selector'], LABELS)
        self.assertEqual(service['ports'], [
            {'name': 'https', 'port': 8443, 'targetPort': 'https', 'protocol': 'TCP'}])

    def test_network_boundary_is_exact(self):
        docs = broker_docs(documents(render(fixture())))
        policy = next(d['spec'] for d in docs if d['kind'] == 'NetworkPolicy')
        self.assertEqual(policy['podSelector']['matchLabels'], LABELS)
        self.assertEqual(set(policy['policyTypes']), {'Ingress', 'Egress'})
        self.assertEqual(policy['ingress'], [{
            'from': [{'namespaceSelector': {'matchLabels': {
                'kubernetes.io/metadata.name': 'agent-control-plane'}},
                'podSelector': {'matchLabels': {
                    'app.kubernetes.io/name': 'mandate',
                    'app.kubernetes.io/instance': 'agent-control-plane',
                    'app.kubernetes.io/component': 'api'}}}],
            'ports': [{'port': 8443, 'protocol': 'TCP'}],
        }])
        self.assertEqual(len(policy['egress']), 2)
        dns, citrus = policy['egress']
        self.assertEqual(dns['to'], [{
            'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
            'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}])
        self.assertCountEqual(dns['ports'], [{'port': 53, 'protocol': 'UDP'}, {'port': 53, 'protocol': 'TCP'}])
        self.assertEqual(citrus, {'to': [{'ipBlock': {'cidr': '192.0.2.10/32'}}],
                                 'ports': [{'port': 443, 'protocol': 'TCP'}]})

    def test_optional_api_ca_and_global_pull_secret(self):
        values = fixture()
        values['global'] = {'imagePullSecrets': ['registry-test']}
        values['citrusPurchasingBroker']['apiCaSecretName'] = 'citrus-api-ca'
        docs = broker_docs(documents(render(values)))
        pod = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['spec']
        self.assertEqual(pod['imagePullSecrets'], [{'name': 'registry-test'}])
        ca = next(v for v in pod['volumes'] if v['name'] == 'api-ca')
        self.assertEqual(ca['secret'], {'secretName': 'citrus-api-ca', 'defaultMode': 288,
                                      'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]})
        c = pod['containers'][0]
        self.assertIn({'name': 'CITRUS_PURCHASING_API_CA_FILE',
                       'value': '/var/run/citrus/api-ca/ca.crt'}, c['env'])
        self.assertIn({'name': 'api-ca', 'mountPath': '/var/run/citrus/api-ca', 'readOnly': True}, c['volumeMounts'])

    def test_enabled_rejects_incomplete_or_unsafe_settings(self):
        cases = [
            ('image.digest', ''), ('image.digest', 'sha256:abc'),
            ('image.repository', 'example.test/unreviewed'),
            ('serverTlsSecretName', ''), ('clientCaSecretName', ''),
            ('purchasingTokenSecretName', ''), ('purchasingTokenSecretName', 'bad/name'),
            ('apiCaSecretName', 'bad/name'), ('clientUriSan', ''),
            ('clientUriSan', 'spiffe://user:pass@mandate.test/core'),
            ('clientUriSan', 'spiffe://mandate.test/core?query'),
            ('apiOrigin', ''), ('apiOrigin', 'http://citrus.test'),
            ('apiOrigin', 'https://citrus.test:444'),
            ('apiOrigin', 'https://user:pass@citrus.test'),
            ('apiOrigin', 'https://citrus.test/orders'),
            ('apiOrigin', 'https://citrus.test?token=x'),
            ('rolloutRevision', ''), ('network.core.namespace', ''),
            ('network.core.appName', ''), ('network.core.releaseName', ''),
            ('network.dns.namespace', ''), ('network.dns.podLabels', {}),
            ('network.dns.podLabels', {'bad key': 'dns'}),
            ('network.citrusIpv4Addresses', []),
            ('network.citrusIpv4Addresses', ['0.0.0.0/0']),
            ('network.citrusIpv4Addresses', ['192.0.2.10/24']),
            ('network.citrusIpv4Addresses', ['999.0.2.10/32']),
            ('network.citrusIpv4Addresses', ['192.0.2.10/32'] * 2),
            ('network.enabled', False), ('env', {'TOKEN': 'inline'}),
            ('serviceAccountName', 'default'), ('resources', {}),
        ]
        for path, bad in cases:
            with self.subTest(path=path, value=bad):
                values = fixture()
                target = values['citrusPurchasingBroker']
                parts = path.split('.')
                for key in parts[:-1]:
                    target = target[key]
                target[parts[-1]] = bad
                result = render(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('citrusPurchasingBroker', result.stderr)
        result = render({'citrusPurchasingBroker': {'enabled': True}})
        self.assertNotEqual(result.returncode, 0)

    def test_worker_name_collision_rejected_and_long_release_bounded(self):
        values = fixture()
        values['workers'] = {'data.workspace_probe': {'metadataName': 'agent-workloads-citrus-broker'}}
        result = render(values, production=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('collid', result.stderr.lower())
        docs = broker_docs(documents(render(fixture(), release='a' * 53)))
        self.assertEqual(len(docs), 4)
        for doc in docs:
            self.assertLessEqual(len(doc['metadata']['name']), 63)
