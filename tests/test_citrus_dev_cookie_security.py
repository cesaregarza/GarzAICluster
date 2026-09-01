from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_PATH = REPO_ROOT / 'helm' / 'citrus'
DEV_VALUES = CHART_PATH / 'values-dev.yaml'
YAML_PARSER = YAML(typ='safe')


def _dev_config() -> dict[str, Any]:
    if shutil.which('helm') is None:
        raise unittest.SkipTest('helm is required for chart render tests')

    rendered = subprocess.run(
        [
            'helm',
            'template',
            'citrus-dev',
            str(CHART_PATH),
            '--namespace',
            'citrus-dev',
            '-f',
            str(DEV_VALUES),
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    documents = (
        document
        for document in YAML_PARSER.load_all(rendered.stdout)
        if isinstance(document, dict) and document
    )
    config_map = next(
        document
        for document in documents
        if document.get('kind') == 'ConfigMap'
        and document.get('metadata', {}).get('name') == 'django-config'
    )
    return config_map['data']


class CitrusDevCookieSecurityTests(unittest.TestCase):
    def test_https_dev_enforces_secure_cookie_boundary(self) -> None:
        config = _dev_config()

        self.assertEqual(config['SECURE_SSL_REDIRECT'], 'True')
        self.assertEqual(config['SESSION_COOKIE_SECURE'], 'True')
        self.assertEqual(config['CSRF_COOKIE_SECURE'], 'True')
