from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


os.environ.setdefault("HERMES_ALERT_TRIAGE_JOB_ID", "triage-test")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/hermes_native_runtime_exporter.py"
SPEC = importlib.util.spec_from_file_location("hermes_native_runtime_exporter", SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(exporter)


class HermesNativeRuntimeExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.home = Path(self.tempdir.name)
        (self.home / "cron").mkdir()
        self.home_patch = patch.object(exporter, "HERMES_HOME", self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def write_job(self, **overrides: object) -> None:
        job = {
            "id": "triage-test",
            "enabled": True,
            "state": "scheduled",
            "last_status": "ok",
            "last_delivery_error": None,
            "last_run_at": "2026-09-07T17:15:00+00:00",
            **overrides,
        }
        (self.home / "cron/jobs.json").write_text(
            json.dumps({"jobs": [job], "updated_at": "ignored"})
        )

    @patch.object(exporter.time, "time", return_value=1788801600)
    def test_alert_triage_accepts_current_dict_job_store(self, _time: Mock) -> None:
        self.write_job()
        self.assertTrue(exporter.alert_triage_ok())

    @patch.object(exporter.time, "time", return_value=1788802501)
    def test_alert_triage_rejects_stale_or_failed_run(self, _time: Mock) -> None:
        self.write_job()
        self.assertFalse(exporter.alert_triage_ok())
        self.write_job(last_status="error")
        self.assertFalse(exporter.alert_triage_ok())

    @patch.object(exporter, "gateway_ok", return_value=True)
    @patch.object(exporter, "cron_ticker_ok", return_value=True)
    @patch.object(exporter, "alert_triage_ok", return_value=True)
    def test_metrics_report_each_check_and_aggregate_ready(
        self, _triage: Mock, _ticker: Mock, _gateway: Mock
    ) -> None:
        body = exporter.render_metrics().decode()
        self.assertIn("hermes_native_runtime_ready 1", body)
        self.assertIn('check="gateway"} 1', body)
        self.assertIn('check="cron_ticker"} 1', body)
        self.assertIn('check="alert_triage"} 1', body)


if __name__ == "__main__":
    unittest.main()
