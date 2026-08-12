from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from scripts.check_agent_control_plane_config_coherence import (
    ConfigCoherenceError,
    check_agent_control_plane_config_coherence,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PARSER = YAML(typ="safe")
SKILLS_IMAGE = "registry.digitalocean.com/sendouq/agent-workloads-skills:main"


class AgentControlPlaneConfigCoherenceTests(unittest.TestCase):
    def test_clean_journeys_match_effective_source_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = _fixture(Path(raw_tmp))

            summary = _check(fixture)

            self.assertIn("has 2 coherent journey(s)", summary)
            self.assertIn("skills manifest", summary)

    def test_historical_probe_marker_defect_names_values_and_worker_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = _fixture(Path(raw_tmp))
            values = _load_yaml(fixture.repo / "apps/agent-control-plane/values.yaml")
            values["syntheticLiveVerify"]["journeys"][0][
                "required_result_fields"
            ].append("probe_marker")
            _write_yaml(fixture.repo / "apps/agent-control-plane/values.yaml", values)

            with self.assertRaises(ConfigCoherenceError) as raised:
                _check(fixture)

            message = str(raised.exception)
            self.assertIn(
                "apps/agent-control-plane/values.yaml "
                "syntheticLiveVerify.journeys[0].required_result_fields[2]='probe_marker'",
                message,
            )
            self.assertIn(
                "agent-platform/mandate/workers/builtin/deployment_smoke.py",
                message,
            )

    def test_historical_runtime_120_defect_names_values_and_merged_policy(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = _fixture(Path(raw_tmp))
            values = _load_yaml(fixture.repo / "apps/agent-control-plane/values.yaml")
            values["syntheticLiveVerify"]["journeys"][0][
                "max_runtime_seconds"
            ] = 120
            _write_yaml(fixture.repo / "apps/agent-control-plane/values.yaml", values)

            with self.assertRaises(ConfigCoherenceError) as raised:
                _check(fixture)

            message = str(raised.exception)
            self.assertIn(
                "apps/agent-control-plane/values.yaml "
                "syntheticLiveVerify.journeys[0].max_runtime_seconds=120",
                message,
            )
            self.assertIn("effective mandate.deploy.smoke cap 60", message)
            self.assertIn("agent-platform/registries/policy.base.yaml", message)
            self.assertIn(
                "apps/agent-control-plane-registry-overlay/registry/policy.prod.yaml",
                message,
            )

    def test_capability_and_skill_typos_name_effective_registry_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = _fixture(Path(raw_tmp))
            values = _load_yaml(fixture.repo / "apps/agent-control-plane/values.yaml")
            journey = values["syntheticLiveVerify"]["journeys"][1]
            journey["capability_id"] = "agent_workloads.readonly_typo"
            journey["required_skill_ids"] = ["xscraper-missing"]
            _write_yaml(fixture.repo / "apps/agent-control-plane/values.yaml", values)

            with self.assertRaises(ConfigCoherenceError) as raised:
                _check(fixture)

            message = str(raised.exception)
            self.assertIn("capability_id='agent_workloads.readonly_typo'", message)
            self.assertIn("agent-platform/registries/capabilities.yaml", message)
            self.assertIn(
                "apps/agent-control-plane-registry-overlay/registry/workload_imports.yaml",
                message,
            )
            self.assertIn("required_skill_ids[0]='xscraper-missing'", message)
            self.assertIn("skills manifest", message)

    def test_event_and_callback_typos_name_canonical_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = _fixture(Path(raw_tmp))
            values = _load_yaml(fixture.repo / "apps/agent-control-plane/values.yaml")
            journey = values["syntheticLiveVerify"]["journeys"][0]
            journey["required_event_types"] = ["run.create_typo"]
            journey["required_callback_types"] = ["job.success_typo"]
            _write_yaml(fixture.repo / "apps/agent-control-plane/values.yaml", values)

            with self.assertRaises(ConfigCoherenceError) as raised:
                _check(fixture)

            message = str(raised.exception)
            self.assertIn("required_event_types[0]='run.create_typo'", message)
            self.assertIn("agent-platform/mandate/contracts/events.py EventType", message)
            self.assertIn("required_callback_types[0]='job.success_typo'", message)
            self.assertIn(
                "agent-platform/mandate/contracts/callback_event_types.py",
                message,
            )

    def test_cost_uses_effective_per_capability_override(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fixture = _fixture(Path(raw_tmp))
            policy_path = (
                fixture.repo
                / "apps/agent-control-plane-registry-overlay/registry/policy.prod.yaml"
            )
            policy = _load_yaml(policy_path)
            policy["defaults"]["max_cost_usd_per_capability"] = {
                "agent_workloads.readonly_query": 0.5
            }
            _write_yaml(policy_path, policy)

            with self.assertRaisesRegex(
                ConfigCoherenceError,
                r"max_cost_usd=1 exceeds the effective "
                r"agent_workloads\.readonly_query cap 0\.5",
            ):
                _check(fixture)

    def test_ci_reuses_provider_gate_checkout_and_reads_deployed_skill_manifest(
        self,
    ) -> None:
        workflow = YAML_PARSER.load(
            (REPO_ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")
        )
        job = workflow["jobs"]["agent-control-plane-provider-digest-pins"]
        steps = job["steps"]

        check_step = next(
            step
            for step in steps
            if step.get("name") == "Check config coherence"
        )
        self.assertIn(
            "scripts/check_agent_control_plane_config_coherence.py",
            check_step["run"],
        )
        self.assertIn("--agent-platform-repo agent-platform", check_step["run"])
        manifest_step = next(
            step
            for step in steps
            if step.get("name") == "Read deployed skills bundle manifest"
        )
        self.assertIn(f"crane export {SKILLS_IMAGE}", manifest_step["run"])
        self.assertIn("skill-bundle/manifest.json", manifest_step["run"])
        checkout_steps = [
            step
            for step in steps
            if step.get("name") == "Check out pinned agent-platform source"
        ]
        self.assertEqual(len(checkout_steps), 1)


class _Fixture:
    def __init__(self, repo: Path, platform: Path, skills_manifest: Path) -> None:
        self.repo = repo
        self.platform = platform
        self.skills_manifest = skills_manifest


def _fixture(tmp: Path) -> _Fixture:
    repo = tmp / "GarzAICluster"
    platform = tmp / "agent-platform"
    values_path = repo / "apps/agent-control-plane/values.yaml"
    policy_path = (
        repo / "apps/agent-control-plane-registry-overlay/registry/policy.prod.yaml"
    )
    imports_path = (
        repo
        / "apps/agent-control-plane-registry-overlay/registry/workload_imports.yaml"
    )
    for path in (values_path, policy_path, imports_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    _write_yaml(
        values_path,
        {
            "syntheticLiveVerify": {
                "journeys": [
                    {
                        "id": "deployment-smoke",
                        "capability_id": "mandate.deploy.smoke",
                        "max_cost_usd": 0.05,
                        "max_runtime_seconds": 60,
                        "required_result_fields": ["output_text", "schema_version"],
                        "required_callback_types": [
                            "job.accepted",
                            "job.progress",
                            "job.succeeded",
                        ],
                        "required_event_types": [
                            "run.created",
                            "policy.evaluated",
                            "result.released",
                        ],
                    },
                    {
                        "id": "readonly-query",
                        "capability_id": "agent_workloads.readonly_query",
                        "max_cost_usd": 1.0,
                        "max_runtime_seconds": 180,
                        "required_result_fields": ["output_text"],
                        "required_callback_types": ["job.succeeded"],
                        "required_event_types": ["model_call.finished", "tool.started"],
                        "required_skill_ids": [
                            "xscraper-schema",
                            "xscraper-glossary",
                        ],
                    },
                ]
            }
        },
    )
    _write_yaml(
        policy_path,
        {
            "defaults": {
                "max_cost_usd_per_job": 10.0,
                "max_runtime_seconds_per_job": 60,
                "max_runtime_seconds_per_capability": {
                    "agent_workloads.readonly_query": 180
                },
            }
        },
    )
    _write_yaml(
        imports_path,
        {
            "schema_version": "workload-imports.v1",
            "imports": [
                {
                    "id": "data.workspace_probe",
                    "capabilities": {
                        "agent_workloads.readonly_query": {
                            "output_gate": {
                                "projection_id": "agent_workloads.readonly_query"
                            }
                        }
                    },
                }
            ],
        },
    )

    _write_yaml(
        platform / "registries/policy.base.yaml",
        {
            "defaults": {
                "max_cost_usd_per_job": 2.0,
                "max_runtime_seconds_per_job": 1800,
                "max_runtime_seconds_per_capability": {},
            }
        },
    )
    _write_yaml(
        platform / "registries/capabilities.yaml",
        {
            "capabilities": [
                {
                    "id": "mandate.deploy.smoke",
                    "output_gate": {"projection_id": "raw_public_result_v1"},
                }
            ]
        },
    )
    _write_yaml(
        platform / "registries/agents.yaml",
        {
            "agents": [
                {
                    "id": "deterministic.deployment_smoke",
                    "loop": {
                        "module": "mandate.workers.builtin.deployment_smoke"
                    },
                    "capabilities": ["mandate.deploy.smoke"],
                }
            ]
        },
    )
    _write_text(
        platform / "mandate/workers/builtin/deployment_smoke.py",
        textwrap.dedent(
            """
            def execute():
                return WorkerResult(
                    result={
                        "schema_version": "mandate_deployment_smoke_v1",
                        "output_text": "ok",
                        "checks": [],
                    }
                )
            """
        ).lstrip(),
    )
    _write_text(
        platform / "mandate/core/output_projection.py",
        textwrap.dedent(
            """
            READONLY_QUERY = "agent_workloads.readonly_query"
            READONLY_QUERY_FIELDS = ("output_text", "row_count", "rows_truncated")
            PUBLIC_RESULT_FIELDS_BY_PROJECTION_ID = {
                READONLY_QUERY: frozenset(READONLY_QUERY_FIELDS),
            }
            """
        ).lstrip(),
    )
    _write_text(
        platform / "mandate/contracts/events.py",
        textwrap.dedent(
            """
            class EventType(StrEnum):
                RUN_CREATED = "run.created"
                POLICY_EVALUATED = "policy.evaluated"
                MODEL_CALL_FINISHED = "model_call.finished"
                TOOL_STARTED = "tool.started"
                RESULT_RELEASED = "result.released"
                APPROVAL_REQUESTED = "approval.requested"
            """
        ).lstrip(),
    )
    _write_text(
        platform / "mandate/contracts/callback_event_types.py",
        textwrap.dedent(
            """
            JOB_ACCEPTED = "job.accepted"
            JOB_PROGRESS = "job.progress"
            JOB_SUCCEEDED = "job.succeeded"
            APPROVAL_REQUESTED = EventType.APPROVAL_REQUESTED.value
            DELIVERABLE_CALLBACK_EVENT_TYPES = (
                JOB_ACCEPTED,
                JOB_PROGRESS,
                JOB_SUCCEEDED,
                APPROVAL_REQUESTED,
            )
            """
        ).lstrip(),
    )
    skills_manifest = tmp / "skills-manifest.json"
    skills_manifest.write_text(
        json.dumps(
            {
                "schema_version": "agent-control-plane-skill-bundle.v1",
                "skills": [
                    {"id": "xscraper-schema"},
                    {"id": "xscraper-glossary"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return _Fixture(repo, platform, skills_manifest)


def _check(fixture: _Fixture) -> str:
    return check_agent_control_plane_config_coherence(
        repo_root=fixture.repo,
        agent_platform_repo=fixture.platform,
        skills_manifest_path=fixture.skills_manifest,
        skills_manifest_source="skills manifest",
        validate_checkout=False,
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = YAML_PARSER.load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.default_flow_style = False
    with path.open("w", encoding="utf-8") as stream:
        yaml.dump(payload, stream)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
