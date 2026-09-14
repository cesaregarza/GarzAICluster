from __future__ import annotations

import json
import unittest

from scripts.identity_digest_helpers import DriftGateError
from tests.worker_identity_fixtures import (
    YAML_PARSER,
    _check,
    _fixture_repo,
    _write_yaml,
    replace_token_identity,
)


class WorkerIdentityBindingTests(unittest.TestCase):
    def test_retained_token_cannot_adopt_another_identity_even_with_matching_metadata(
        self,
    ):
        for claims in ({"sub": "opencode.proposer"}, {"aud": "rogue-audience"}):
            with self.subTest(claims=claims):
                root = _fixture_repo()
                self.assertIn("match release pins", _check(root))
                replace_token_identity(root, "data.workspace_probe", claims)
                with self.assertRaisesRegex(DriftGateError, "must match worker"):
                    _check(root)

    def test_import_cannot_alias_another_workers_manifest(self):
        root = _fixture_repo()
        self.assertIn("match release pins", _check(root))
        path = root / "apps/agent-control-plane-registry-overlay/configmap.yaml"
        configmap = YAML_PARSER.load(path.read_text())
        manifest = json.loads(configmap["data"]["agent-data.workspace_probe.json"])
        manifest["id"] = "opencode.proposer"
        configmap["data"]["agent-data.workspace_probe.json"] = json.dumps(manifest)
        _write_yaml(path, configmap)
        with self.assertRaisesRegex(DriftGateError, "manifest id must match"):
            _check(root)
