from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "apps" / "agent-control-plane-skills"


class SkillMaterializerPatchTests(unittest.TestCase):
    def run_materializer(self, *, invalid_checksum=False, generation_failure=False):
        yaml = YAML(typ="safe")
        job = yaml.load((BUNDLE_DIR / "materialize-job.yaml").read_text())
        cron = yaml.load((BUNDLE_DIR / "materialize-cronjob.yaml").read_text())
        script = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        self.assertEqual(
            script,
            cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][-1],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            (bundle / "skills").mkdir(parents=True)
            data = {
                "manifest.json": '{"schema_version": "agent-control-plane-skill-bundle.v1"}',
                "skills/example.md": 'Quotes " and backslash \\ with newline\nUnicode: café',
            }
            for name, content in data.items():
                (bundle / name).write_text(content)
            checksums = "".join(
                f"{hashlib.sha256(content.encode()).hexdigest()}  {name}\n"
                for name, content in data.items()
            )
            (bundle / "SHA256SUMS").write_text(checksums)
            if invalid_checksum:
                (bundle / "skills/example.md").write_text("tampered")
            trace = root / "patch.json"
            fake = root / "kubectl"
            fake.write_text(f"#!{sys.executable}\n" + r'''import json, os, pathlib, sys
args = sys.argv[1:]
assert args[:2] == ["-n", "agent-control-plane"]
if args[2:5] == ["create", "configmap", "mandate-skill-packs"]:
    assert "--dry-run=client" in args and "jsonpath={.data}" in args
    if os.environ["FAIL_GENERATION"] == "1":
        sys.exit(9)
    data = {}
    for arg in args:
        if arg.startswith("--from-file="):
            key, path = arg[len("--from-file="):].split("=", 1)
            data[key] = pathlib.Path(path).read_text()
    print(json.dumps(data), end="")
elif args[2:5] == ["patch", "configmap", "mandate-skill-packs"]:
    assert "--type=json" in args
    path = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--patch-file="))
    patch = json.loads(pathlib.Path(path).read_text())
    pathlib.Path(os.environ["PATCH_TRACE"]).write_text(json.dumps(patch))
else:
    raise AssertionError("unexpected kubectl mutation")
''')
            fake.chmod(0o700)
            command = script.replace("/tmp/", str(root) + "/").replace("/bundle", str(bundle))
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       PATCH_TRACE=str(trace), FAIL_GENERATION=str(int(generation_failure)))
            result = subprocess.run(["/bin/sh", "-ceu", command], env=env,
                                    text=True, capture_output=True, check=False)
            patch = json.loads(trace.read_text()) if trace.exists() else None
            return result, patch, {Path(name).name: content for name, content in data.items()}

    def test_materialization_replaces_only_complete_data_map(self):
        result, patch, expected = self.run_materializer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(patch, [{"op": "add", "path": "/data", "value": expected}])
        self.assertNotIn("obsolete.md", patch[0]["value"])

    def test_invalid_bundle_never_patches(self):
        result, patch, _ = self.run_materializer(invalid_checksum=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(patch)

    def test_generation_failure_never_patches(self):
        result, patch, _ = self.run_materializer(generation_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(patch)


if __name__ == "__main__":
    unittest.main()
