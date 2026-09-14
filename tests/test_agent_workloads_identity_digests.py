from __future__ import annotations

import json
import unittest

from scripts.identity_digest_helpers import DriftGateError
from tests.worker_identity_fixtures import (
    DIGESTS,
    DRIFT_GATE_RECIPIENT,
    OLD_MUTABLE_BROKER_ACTION,
    REPO_ROOT,
    REPO_SOPS_SECRET_CONTEXT,
    RUNTIME_SECRET_PATH,
    SHARED_DRIFT_GATE_ACTION,
    TOKEN_KEYS,
    TOKEN_METADATA_PATH,
    TOKEN_SECRET_PATH,
    YAML_PARSER,
    _check,
    _configure_governed_hmac_identities,
    _configure_governed_release_subjects,
    _configure_retained_hmac_token,
    _configure_workspace_projected_identity,
    _fixture_repo,
    _mwit_token,
    _release_subject,
    _set_workspace_identity_audience,
    _sops_age_recipients,
    _write_metadata,
    _write_yaml,
    _yaml_text,
)


class AgentWorkloadsIdentityDigestGateTests(unittest.TestCase):
    def test_fourth_projected_worker_needs_no_hmac_credential(self) -> None:
        import copy

        root = _fixture_repo()
        worker_id = "extra.verification"
        values_path = root / "apps/agent-workloads/values.yaml"
        values = YAML_PARSER.load(values_path.read_text())
        worker = copy.deepcopy(values["workers"]["data.workspace_probe"])
        worker["identity"]["workerId"] = worker_id
        worker["identity"].pop("hmacRollbackRelease")
        worker["identity"].pop("hmacRollbackTokenKey")
        values["workers"][worker_id] = worker
        values["mandateReleasePins"][worker_id] = dict(DIGESTS["data.workspace_probe"])
        _write_yaml(values_path, values)
        path = root / "apps/agent-control-plane-registry-overlay/configmap.yaml"
        configmap = YAML_PARSER.load(path.read_text())
        imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
        entry = copy.deepcopy(imports["imports"][0])
        entry["id"] = worker_id
        manifest = json.loads(configmap["data"]["agent-data.workspace_probe.json"])
        manifest["id"] = worker_id
        entry["manifest_path"] = "registries/imports/agent-extra.verification.json"
        configmap["data"]["agent-extra.verification.json"] = json.dumps(manifest)
        entry["agent"]["service_account_subject"] = _release_subject(
            worker_id, DIGESTS["data.workspace_probe"]
        )
        imports["imports"].append(entry)
        configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
        _write_yaml(path, configmap)
        self.assertIn("match release pins", _check(root))
        values["workers"][worker_id]["identity"]["workerId"] = "data.workspace_probe"
        _write_yaml(values_path, values)
        with self.assertRaisesRegex(DriftGateError, "workerId must equal"):
            _check(root)

    def test_ci_drift_gate_uses_pinned_shared_brokered_sops_key(self) -> None:
        workflow_path = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
        workflow = YAML_PARSER.load(workflow_path.read_text())
        workflow_text = workflow_path.read_text()
        job = workflow["jobs"]["agent-workloads-identity-digest-drift"]

        self.assertNotIn(REPO_SOPS_SECRET_CONTEXT, workflow_text)
        self.assertNotIn(OLD_MUTABLE_BROKER_ACTION, workflow_text)
        self.assertEqual(job["permissions"]["contents"], "read")
        self.assertEqual(job["permissions"]["id-token"], "write")

        check_step = next(
            step
            for step in job["steps"]
            if step.get("uses", "").startswith(
                "cesaregarza/.github/actions/agent-workloads-identity-digest-drift-gate"
            )
        )
        self.assertEqual(check_step["uses"], SHARED_DRIFT_GATE_ACTION)
        self.assertNotIn(
            "@main",
            check_step["uses"],
        )

    def test_local_drift_gate_copy_is_deleted(self) -> None:
        self.assertFalse(
            (
                REPO_ROOT
                / ".github"
                / "actions"
                / "agent-workloads-identity-digest-drift-gate"
                / "action.yml"
            ).exists()
        )

    def test_plaintext_secret_guard_allows_non_secret_metadata_ledgers(self) -> None:
        workflow_text = (
            REPO_ROOT / ".github" / "workflows" / "deny-plaintext-secrets.yaml"
        ).read_text()

        self.assertIn('[[ "$base" == *.metadata.yaml ]]', workflow_text)

    def test_scoped_sops_recipient_only_decrypts_workload_identity_token_secret(
        self,
    ) -> None:
        sops_config = YAML_PARSER.load((REPO_ROOT / ".sops.yaml").read_text())
        rules = sops_config["creation_rules"]
        token_rule_index = next(
            index
            for index, rule in enumerate(rules)
            if rule["path_regex"]
            == r"^secrets/agent-workloads/workload-identity-tokens\.enc\.yaml$"
        )
        broad_agent_workloads_rule_index = next(
            index
            for index, rule in enumerate(rules)
            if rule["path_regex"] == r"^secrets/agent-workloads/.*\.enc\.yaml$"
        )

        self.assertLess(token_rule_index, broad_agent_workloads_rule_index)
        self.assertIn(DRIFT_GATE_RECIPIENT, rules[token_rule_index]["age"])
        self.assertNotIn(
            DRIFT_GATE_RECIPIENT,
            rules[broad_agent_workloads_rule_index]["age"],
        )

        runtime_recipients = _sops_age_recipients(
            REPO_ROOT / "secrets" / "agent-workloads" / "runtime-secret.enc.yaml"
        )
        token_recipients = _sops_age_recipients(
            REPO_ROOT
            / "secrets"
            / "agent-workloads"
            / "workload-identity-tokens.enc.yaml"
        )
        agent_workloads_regcred_recipients = _sops_age_recipients(
            REPO_ROOT / "secrets" / "agent-workloads" / "regcred.enc.yaml"
        )
        control_plane_recipients = _sops_age_recipients(
            REPO_ROOT / "secrets" / "agent-control-plane" / "runtime-secret.enc.yaml"
        )

        self.assertIn(DRIFT_GATE_RECIPIENT, token_recipients)
        self.assertNotIn(DRIFT_GATE_RECIPIENT, runtime_recipients)
        self.assertNotIn(DRIFT_GATE_RECIPIENT, agent_workloads_regcred_recipients)
        self.assertNotIn(DRIFT_GATE_RECIPIENT, control_plane_recipients)
        runtime_text = (
            REPO_ROOT / "secrets" / "agent-workloads" / "runtime-secret.enc.yaml"
        ).read_text()
        for token_key in TOKEN_KEYS.values():
            self.assertNotIn(token_key, runtime_text)

    def test_gate_skips_without_release_pins(self) -> None:
        root = _fixture_repo(include_pins=False)

        result = _check(root)

        self.assertIn("gate inactive", result)

    def test_gate_accepts_matching_release_pins_overlay_and_tokens(self) -> None:
        root = _fixture_repo()

        result = _check(root)

        self.assertIn("match release pins", result)

    def test_gate_rejects_missing_sdk_receipt_when_release_pins_are_present(
        self,
    ) -> None:
        root = _fixture_repo()
        (root / "contracts/mandate-worker/receipt.json").unlink()

        with self.assertRaisesRegex(DriftGateError, "SDK receipt"):
            _check(root)

    def test_gate_rejects_malformed_sdk_receipt_when_release_pins_are_present(
        self,
    ) -> None:
        root = _fixture_repo()
        (root / "contracts/mandate-worker/receipt.json").write_text("{}\n")

        with self.assertRaisesRegex(DriftGateError, "SDK receipt"):
            _check(root)

    def test_gate_accepts_distinct_governed_release_subject_bindings(self) -> None:
        root = _fixture_repo()
        _configure_governed_release_subjects(root)

        result = _check(root)

        self.assertIn("match release pins", result)

    def test_gate_accepts_omitted_workspace_identity_audience_default(self) -> None:
        root = _fixture_repo()
        _configure_workspace_projected_identity(root)
        _set_workspace_identity_audience(root, None)

        self.assertIn("match release pins", _check(root))

    def test_gate_accepts_explicit_workspace_identity_audience_override(self) -> None:
        root = _fixture_repo()
        _configure_workspace_projected_identity(root)
        _set_workspace_identity_audience(root, "mandate-api")

        self.assertIn("match release pins", _check(root))

    def test_gate_rejects_wrong_workspace_identity_audience(self) -> None:
        root = _fixture_repo()
        _configure_workspace_projected_identity(root)
        _set_workspace_identity_audience(root, "wrong-audience")

        with self.assertRaisesRegex(
            DriftGateError,
            "identity_audience differs from projected render",
        ):
            _check(root)

    def test_omitted_audience_still_rejects_wrong_worker_token_audiences(self) -> None:
        for worker in DIGESTS:
            with self.subTest(worker=worker):
                root = _fixture_repo()
                _configure_workspace_projected_identity(root)
                _configure_governed_release_subjects(root)
                path = root / "apps/agent-control-plane-registry-overlay/configmap.yaml"
                configmap = YAML_PARSER.load(path.read_text())
                imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
                for entry in imports["imports"]:
                    entry.setdefault("agent", {}).pop("identity_audience", None)
                configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
                _write_yaml(path, configmap)
                path = root / "apps/agent-workloads/values.yaml"
                values = YAML_PARSER.load(path.read_text())
                identity = values["workers"][worker]["identity"]
                identity["token"]["audience"] = "wrong-audience"
                _write_yaml(path, values)
                with self.assertRaisesRegex(
                    DriftGateError, "identity_audience differs"
                ):
                    _check(root)

    def test_omitted_audience_uses_configured_core_verifier_audience(self) -> None:
        root = _fixture_repo()
        _configure_workspace_projected_identity(root)
        _set_workspace_identity_audience(root, None)
        path = root / "apps/agent-control-plane/values.yaml"
        _write_yaml(
            path, {"env": {"AGENT_PLATFORM_WORKLOAD_IDENTITY_AUDIENCE": "custom-core"}}
        )
        with self.assertRaisesRegex(DriftGateError, "identity_audience differs"):
            _check(root)
        path = root / "apps/agent-workloads/values.yaml"
        values = YAML_PARSER.load(path.read_text())
        values["workers"]["data.workspace_probe"]["identity"]["token"]["audience"] = (
            "custom-core"
        )
        _write_yaml(path, values)
        from tests.worker_identity_fixtures import replace_token_identity
        replace_token_identity(root, "data.workspace_probe", {"aud": "custom-core"})
        self.assertIn("match release pins", _check(root))

    def test_gate_accepts_workspace_projected_subject_with_current_hmac_rollback(
        self,
    ) -> None:
        root = _fixture_repo()
        _configure_workspace_projected_identity(root)

        result = _check(root)

        self.assertIn("retained rollback tuples", result)

    def test_retained_hmac_survives_projected_overlap_retirement_and_next_roll(
        self,
    ) -> None:
        rollback = {
            key: "sha256:" + digit * 64
            for key, digit in zip(
                ("codeDigest", "manifestDigest", "imageDigest"), "456"
            )
        }
        next_previous = {
            key: "sha256:" + digit * 64
            for key, digit in zip(
                ("codeDigest", "manifestDigest", "imageDigest"), "789"
            )
        }
        for previous in (None, next_previous):
            with self.subTest(previous=previous):
                root = _fixture_repo()
                _configure_workspace_projected_identity(root, previous_release=previous)
                _configure_retained_hmac_token(
                    root, agent_id="data.workspace_probe", release=rollback
                )
                path = root / "apps/agent-workloads/values.yaml"
                values = YAML_PARSER.load(path.read_text())
                values["workers"]["data.workspace_probe"]["identity"][
                    "hmacRollbackRelease"
                ] = rollback
                _write_yaml(path, values)
                self.assertIn("retained rollback tuples", _check(root))

    def test_projected_rollback_requires_explicit_valid_exact_tuple(self) -> None:
        invalid = (
            None,
            {},
            "invalid",
            {**DIGESTS["data.workspace_probe"], "imageDigest": "bad"},
            {**DIGESTS["data.workspace_probe"], "extra": "bad"},
        )
        for rollback in invalid:
            with self.subTest(rollback=rollback):
                root = _fixture_repo()
                _configure_workspace_projected_identity(root)
                path = root / "apps/agent-workloads/values.yaml"
                values = YAML_PARSER.load(path.read_text())
                values["workers"]["data.workspace_probe"]["identity"][
                    "hmacRollbackRelease"
                ] = rollback
                _write_yaml(path, values)
                with self.assertRaisesRegex(DriftGateError, "hmacRollbackRelease"):
                    _check(root)
        root = _fixture_repo()
        _configure_workspace_projected_identity(root)
        path = root / "apps/agent-workloads/values.yaml"
        values = YAML_PARSER.load(path.read_text())
        del values["workers"]["data.workspace_probe"]["identity"]["hmacRollbackRelease"]
        _write_yaml(path, values)
        with self.assertRaisesRegex(DriftGateError, "hmacRollbackRelease"):
            _check(root)

    def test_projected_rollback_rejects_each_claim_drift(self) -> None:
        for key in ("codeDigest", "manifestDigest", "imageDigest"):
            with self.subTest(key=key):
                root = _fixture_repo()
                _configure_workspace_projected_identity(root)
                path = root / "apps/agent-workloads/values.yaml"
                values = YAML_PARSER.load(path.read_text())
                values["workers"]["data.workspace_probe"]["identity"][
                    "hmacRollbackRelease"
                ][key] = "sha256:" + "9" * 64
                _write_yaml(path, values)
                with self.assertRaisesRegex(DriftGateError, "mismatch"):
                    _check(root)

    def test_hmac_mode_cannot_use_rollback_to_override_current_claims(self) -> None:
        root = _fixture_repo()
        rollback = {
            key: "sha256:" + digit * 64
            for key, digit in zip(
                ("codeDigest", "manifestDigest", "imageDigest"), "456"
            )
        }
        _configure_retained_hmac_token(
            root, agent_id="data.workspace_probe", release=rollback
        )
        path = root / "apps/agent-workloads/values.yaml"
        values = YAML_PARSER.load(path.read_text())
        values["workers"]["data.workspace_probe"]["identity"]["mode"] = "hmac"
        _write_yaml(path, values)
        with self.assertRaisesRegex(DriftGateError, "mode must be projected"):
            _check(root)

    def test_gate_rejects_workspace_projected_subject_drift(self) -> None:
        root = _fixture_repo()
        _configure_workspace_projected_identity(root)
        configmap_path = (
            root / "apps" / "agent-control-plane-registry-overlay" / "configmap.yaml"
        )
        configmap = YAML_PARSER.load(configmap_path.read_text())
        imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
        workspace = next(
            entry
            for entry in imports["imports"]
            if entry["id"] == "data.workspace_probe"
        )
        workspace["agent"]["service_account_subject"] = (
            "system:serviceaccount:agent-workloads:wrong-release"
        )
        configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
        _write_yaml(configmap_path, configmap)

        with self.assertRaisesRegex(
            DriftGateError,
            "service_account_subject differs from projected render",
        ):
            _check(root)

    def test_gate_accepts_workspace_previous_tuple_hmac_during_projected_overlap(
        self,
    ) -> None:
        previous_release = {
            "codeDigest": "sha256:" + "4" * 64,
            "manifestDigest": "sha256:" + "5" * 64,
            "imageDigest": "sha256:" + "6" * 64,
        }
        root = _fixture_repo()
        _configure_workspace_projected_identity(
            root,
            previous_release=previous_release,
        )
        _configure_retained_hmac_token(
            root,
            agent_id="data.workspace_probe",
            release=previous_release,
        )

        result = _check(root)

        self.assertIn("retained rollback tuples", result)

    def test_gate_accepts_previous_tuple_hmac_during_projected_overlap(self) -> None:
        previous_release = {
            "codeDigest": "sha256:" + "4" * 64,
            "manifestDigest": "sha256:" + "5" * 64,
            "imageDigest": "sha256:" + "6" * 64,
        }
        for agent_id in ("opencode.proposer", "opencode.apply_executor"):
            with self.subTest(agent_id=agent_id):
                root = _fixture_repo()
                _configure_governed_release_subjects(
                    root,
                    previous_by_agent={agent_id: previous_release},
                )
                _configure_retained_hmac_token(
                    root,
                    agent_id=agent_id,
                    release=previous_release,
                )

                result = _check(root)

                self.assertIn("retained rollback tuples", result)

    def test_opencode_explicit_rollback_survives_retirement_and_next_overlap(
        self,
    ) -> None:
        rollback = {
            key: "sha256:" + digit * 64
            for key, digit in zip(
                ("codeDigest", "manifestDigest", "imageDigest"), "456"
            )
        }
        next_previous = {
            key: "sha256:" + digit * 64
            for key, digit in zip(
                ("codeDigest", "manifestDigest", "imageDigest"), "789"
            )
        }
        for agent_id in DIGESTS:
            for previous in (None, next_previous):
                with self.subTest(agent_id=agent_id, previous=previous):
                    root = _fixture_repo()
                    _configure_governed_release_subjects(
                        root,
                        previous_by_agent={agent_id: previous} if previous else None,
                    )
                    _configure_retained_hmac_token(
                        root, agent_id=agent_id, release=rollback
                    )
                    path = root / "apps/agent-workloads/values.yaml"
                    values = YAML_PARSER.load(path.read_text())
                    values["workers"][agent_id]["identity"]["hmacRollbackRelease"] = (
                        rollback
                    )
                    _write_yaml(path, values)
                    self.assertIn("retained rollback tuples", _check(root))

    def test_opencode_explicit_rollback_rejects_invalid_shape_and_claim_drift(
        self,
    ) -> None:
        keys = ("codeDigest", "manifestDigest", "imageDigest")
        for agent_id in DIGESTS:
            invalid = (
                None,
                {},
                "invalid",
                {**DIGESTS[agent_id], "imageDigest": "bad"},
                {**DIGESTS[agent_id], "extra": "bad"},
            )
            for rollback in invalid:
                with self.subTest(agent_id=agent_id, rollback=rollback):
                    root = _fixture_repo()
                    _configure_governed_release_subjects(root)
                    path = root / "apps/agent-workloads/values.yaml"
                    values = YAML_PARSER.load(path.read_text())
                    values["workers"][agent_id]["identity"]["hmacRollbackRelease"] = (
                        rollback
                    )
                    _write_yaml(path, values)
                    with self.assertRaisesRegex(DriftGateError, "hmacRollbackRelease"):
                        _check(root)
            for key in keys:
                with self.subTest(agent_id=agent_id, drift=key):
                    root = _fixture_repo()
                    _configure_governed_release_subjects(root)
                    path = root / "apps/agent-workloads/values.yaml"
                    values = YAML_PARSER.load(path.read_text())
                    values["workers"][agent_id]["identity"]["hmacRollbackRelease"] = {
                        **DIGESTS[agent_id],
                        key: "sha256:" + "9" * 64,
                    }
                    _write_yaml(path, values)
                    with self.assertRaisesRegex(DriftGateError, "mismatch"):
                        _check(root)

    def test_opencode_hmac_mode_cannot_override_current_claims_with_rollback(
        self,
    ) -> None:
        rollback = {
            key: "sha256:" + digit * 64
            for key, digit in zip(
                ("codeDigest", "manifestDigest", "imageDigest"), "456"
            )
        }
        for agent_id in DIGESTS:
            with self.subTest(agent_id=agent_id):
                root = _fixture_repo()
                _configure_governed_hmac_identities(root)
                _configure_retained_hmac_token(
                    root, agent_id=agent_id, release=rollback
                )
                path = root / "apps/agent-workloads/values.yaml"
                values = YAML_PARSER.load(path.read_text())
                values["workers"][agent_id]["identity"]["hmacRollbackRelease"] = (
                    rollback
                )
                _write_yaml(path, values)
                with self.assertRaisesRegex(DriftGateError, "mode must be projected"):
                    _check(root)

    def test_gate_rejects_current_hmac_token_during_previous_tuple_overlap(
        self,
    ) -> None:
        previous_release = {
            "codeDigest": "sha256:" + "4" * 64,
            "manifestDigest": "sha256:" + "5" * 64,
            "imageDigest": "sha256:" + "6" * 64,
        }
        for agent_id in ("opencode.proposer", "opencode.apply_executor"):
            with self.subTest(agent_id=agent_id):
                root = _fixture_repo()
                _configure_governed_release_subjects(
                    root,
                    previous_by_agent={agent_id: previous_release},
                )

                with self.assertRaisesRegex(
                    DriftGateError,
                    "code_digest mismatch",
                ):
                    _check(root)

    def test_gate_rejects_hmac_identity_mode(self) -> None:
        root = _fixture_repo()
        _configure_governed_hmac_identities(root)
        with self.assertRaisesRegex(DriftGateError, "mode must be projected"):
            _check(root)

    def test_gate_rejects_projected_subject_on_governed_hmac_identity(self) -> None:
        root = _fixture_repo()
        _configure_governed_hmac_identities(root)
        configmap_path = (
            root / "apps" / "agent-control-plane-registry-overlay" / "configmap.yaml"
        )
        configmap = YAML_PARSER.load(configmap_path.read_text())
        imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
        proposer = next(
            entry for entry in imports["imports"] if entry["id"] == "opencode.proposer"
        )
        proposer["agent"]["service_account_subject"] = _release_subject(
            "opencode.proposer",
            DIGESTS["opencode.proposer"],
        )
        configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
        _write_yaml(configmap_path, configmap)

        with self.assertRaisesRegex(
            DriftGateError,
            "mode must be projected",
        ):
            _check(root)

    def test_gate_rejects_governed_cross_worker_subject_reuse(self) -> None:
        root = _fixture_repo()
        _configure_governed_release_subjects(root)
        configmap_path = (
            root / "apps" / "agent-control-plane-registry-overlay" / "configmap.yaml"
        )
        configmap = YAML_PARSER.load(configmap_path.read_text())
        imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
        imports_by_id = {entry["id"]: entry for entry in imports["imports"]}
        imports_by_id["opencode.apply_executor"]["agent"]["service_account_subject"] = (
            imports_by_id["opencode.proposer"]["agent"]["service_account_subject"]
        )
        configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
        _write_yaml(configmap_path, configmap)

        with self.assertRaisesRegex(
            DriftGateError,
            "service_account_subject differs from projected render",
        ):
            _check(root)

    def test_gate_rejects_governed_previous_release_tuple_drift(self) -> None:
        root = _fixture_repo()
        _configure_governed_release_subjects(
            root,
            previous_by_agent={
                "opencode.proposer": {
                    "codeDigest": "sha256:" + "4" * 64,
                    "manifestDigest": "sha256:" + "5" * 64,
                    "imageDigest": "sha256:" + "6" * 64,
                }
            },
        )
        configmap_path = (
            root / "apps" / "agent-control-plane-registry-overlay" / "configmap.yaml"
        )
        configmap = YAML_PARSER.load(configmap_path.read_text())
        imports = YAML_PARSER.load(configmap["data"]["workload_imports.yaml"])
        proposer = next(
            entry for entry in imports["imports"] if entry["id"] == "opencode.proposer"
        )
        proposer["agent"]["previous_release"]["image_digest"] = "sha256:" + "9" * 64
        configmap["data"]["workload_imports.yaml"] = _yaml_text(imports)
        _write_yaml(configmap_path, configmap)

        with self.assertRaisesRegex(
            DriftGateError,
            "previous_release differs from projected render",
        ):
            _check(root)

    def test_gate_rejects_missing_workload_identity_rollout_checksum(self) -> None:
        root = _fixture_repo()
        values_path = root / "apps" / "agent-workloads" / "values.yaml"
        values = YAML_PARSER.load(values_path.read_text())
        del values["rolloutChecksums"]
        _write_yaml(values_path, values)

        with self.assertRaisesRegex(DriftGateError, "rolloutChecksums"):
            _check(root)

    def test_gate_rejects_stale_workload_identity_rollout_checksum(self) -> None:
        root = _fixture_repo()
        values_path = root / "apps" / "agent-workloads" / "values.yaml"
        values = YAML_PARSER.load(values_path.read_text())
        values["rolloutChecksums"]["workloadIdentityTokenSecret"] = "sha256:" + "9" * 64
        _write_yaml(values_path, values)

        with self.assertRaisesRegex(
            DriftGateError,
            "rolloutChecksums.workloadIdentityTokenSecret",
        ):
            _check(root)

    def test_gate_rejects_values_overlay_code_digest_mismatch(self) -> None:
        root = _fixture_repo()
        values_path = root / "apps" / "agent-workloads" / "values.yaml"
        values = YAML_PARSER.load(values_path.read_text())
        values["mandateReleasePins"]["opencode.proposer"]["codeDigest"] = (
            "sha256:" + "9" * 64
        )
        _write_yaml(values_path, values)

        with self.assertRaisesRegex(DriftGateError, "mandateReleasePins.codeDigest"):
            _check(root)

    def test_gate_rejects_values_image_digest_release_pin_mismatch(self) -> None:
        root = _fixture_repo()
        values_path = root / "apps" / "agent-workloads" / "values.yaml"
        values = YAML_PARSER.load(values_path.read_text())
        values["workers"]["opencode.proposer"]["image"]["digest"] = "sha256:" + "8" * 64
        _write_yaml(values_path, values)

        with self.assertRaisesRegex(DriftGateError, "values image.digest"):
            _check(root)

    def test_gate_rejects_stale_workload_identity_token_code_digest(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {"code_digest": "sha256:" + "9" * 64},
            }
        )

        with self.assertRaisesRegex(DriftGateError, "code_digest mismatch"):
            _check(root)

    def test_gate_rejects_missing_workload_identity_token_bundle_digest(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {"bundle_digest": None},
            }
        )

        with self.assertRaisesRegex(DriftGateError, "missing bundle_digest"):
            _check(root)

    def test_gate_rejects_code_only_workload_identity_token(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {
                    "manifest_digest": None,
                    "image_digest": None,
                    "bundle_digest": None,
                },
            }
        )

        with self.assertRaisesRegex(DriftGateError, "missing manifest_digest"):
            _check(root)

    def test_gate_rejects_stale_workload_identity_token_manifest_digest(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {"manifest_digest": "sha256:" + "9" * 64},
            }
        )

        with self.assertRaisesRegex(DriftGateError, "manifest_digest mismatch"):
            _check(root)

    def test_gate_rejects_stale_workload_identity_token_image_digest(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {"image_digest": "sha256:" + "9" * 64},
            }
        )

        with self.assertRaisesRegex(DriftGateError, "image_digest mismatch"):
            _check(root)

    def test_gate_rejects_stale_workload_identity_token_bundle_digest(self) -> None:
        root = _fixture_repo(
            token_claim_overrides={
                "opencode.proposer": {"bundle_digest": "sha256:" + "9" * 64},
            }
        )

        with self.assertRaisesRegex(DriftGateError, "bundle_digest mismatch"):
            _check(root)

    def test_gate_rejects_malformed_identity_token(self) -> None:
        root = _fixture_repo()
        secret_path = root / TOKEN_SECRET_PATH
        secret = YAML_PARSER.load(secret_path.read_text())
        secret["stringData"]["OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN"] = (
            "not-a-token"
        )
        _write_yaml(secret_path, secret)
        _write_metadata(root)

        with self.assertRaisesRegex(DriftGateError, "not an mwit_v1 token"):
            _check(root)

    def test_gate_rejects_token_keys_left_in_runtime_secret(self) -> None:
        root = _fixture_repo()
        runtime_secret_path = root / RUNTIME_SECRET_PATH
        runtime_secret = YAML_PARSER.load(runtime_secret_path.read_text())
        runtime_secret["stringData"]["OPENCODE_PROPOSER_WORKLOAD_IDENTITY_TOKEN"] = (
            _mwit_token("opencode.proposer")
        )
        _write_yaml(runtime_secret_path, runtime_secret)

        with self.assertRaisesRegex(DriftGateError, "runtime secret must not contain"):
            _check(root)

    def test_gate_rejects_stale_token_metadata_ciphertext_hash(self) -> None:
        root = _fixture_repo()
        metadata_path = root / TOKEN_METADATA_PATH
        metadata = YAML_PARSER.load(metadata_path.read_text())
        metadata["tokens"]["opencode.proposer"]["ciphertext_sha256"] = (
            "sha256:" + "9" * 64
        )
        _write_yaml(metadata_path, metadata)

        with self.assertRaisesRegex(DriftGateError, "ciphertext_sha256 mismatch"):
            _check(root)
