from __future__ import annotations

import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import argocd_client as client


class ArgocdResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def executable(self, directory: str, name: str = "argocd") -> Path:
        path = Path(directory) / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(stat.S_IRWXU)
        return path

    def test_precedence_is_explicit_then_environment_then_path_then_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            explicit = self.executable(directory, "explicit")
            environment = self.executable(directory, "environment")
            path = self.executable(directory, "path")
            pinned = self.executable(directory, "pinned")
            with mock.patch.object(client, "_pinned_path", return_value=pinned):
                os.environ["ARGOCD_BIN"] = str(environment)
                self.assertEqual(
                    client.resolve_argocd(str(explicit), "3.2.0"),
                    (str(explicit.resolve()), "explicit --argocd-bin"),
                )
                self.assertEqual(
                    client.resolve_argocd(None, "3.2.0"),
                    (str(environment.resolve()), "ARGOCD_BIN"),
                )
                os.environ.pop("ARGOCD_BIN")
                with mock.patch.object(client.shutil, "which", return_value=str(path)):
                    self.assertEqual(
                        client.resolve_argocd(None, "3.2.0"),
                        (str(path.resolve()), "PATH"),
                    )
                with mock.patch.object(client.shutil, "which", return_value=None):
                    self.assertEqual(
                        client.resolve_argocd(None, "3.2.0"),
                        (str(pinned.resolve()), "pinned"),
                    )

    def test_unrunnable_default_falls_back_to_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "path"
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            pinned = self.executable(directory, "pinned")
            with (
                mock.patch.object(client, "_pinned_path", return_value=pinned),
                mock.patch.object(client.shutil, "which", return_value=str(path)),
            ):
                result = client.resolve_argocd(None, "3.2.0")
            self.assertEqual(result, (str(pinned.resolve()), "pinned"))

    def test_invalid_explicit_and_environment_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pinned = self.executable(directory, "pinned")
            missing = str(Path(directory) / "missing")
            with mock.patch.object(client, "_pinned_path", return_value=pinned):
                with self.assertRaisesRegex(FileNotFoundError, "refusing to fall back"):
                    client.resolve_argocd(missing, "3.2.0")
                os.environ["ARGOCD_BIN"] = missing
                with self.assertRaisesRegex(FileNotFoundError, "ARGOCD_BIN"):
                    client.resolve_argocd(None, "3.2.0")

    def test_absent_diagnostic_names_sources_and_expected_pin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pinned = Path(directory) / "missing-pinned"
            with (
                mock.patch.object(client, "_pinned_path", return_value=pinned),
                mock.patch.object(client.shutil, "which", return_value=None),
            ):
                with self.assertRaisesRegex(FileNotFoundError, "expected repository version 3.2.0") as raised:
                    client.resolve_argocd(None, "3.2.0")
            message = str(raised.exception)
            for source in ("--argocd-bin/--argocd", "ARGOCD_BIN", "PATH (argocd)", str(pinned)):
                self.assertIn(source, message)

    def test_preflight_line_reports_selected_path_and_source(self) -> None:
        resolution = client.ArgocdResolution("/tmp/path with spaces/argocd", "PATH")
        output = io.StringIO()
        with redirect_stdout(output):
            client.emit_preflight(resolution)
        self.assertEqual(
            output.getvalue(),
            '{"result":"selected","selected_path":"/tmp/path with spaces/argocd","source":"PATH","stage":"argocd-preflight"}\n',
        )


class ParserTests(unittest.TestCase):
    def test_help_does_not_probe_pinned_path(self) -> None:
        script = SCRIPTS / "argocd_core.py"
        spec = importlib.util.spec_from_file_location("argocd_core_help", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        with mock.patch.object(client, "_pinned_path", side_effect=AssertionError("probed")):
            spec.loader.exec_module(module)
            with self.assertRaises(SystemExit):
                module.build_parser().parse_args(["--help"])


if __name__ == "__main__":
    unittest.main()
