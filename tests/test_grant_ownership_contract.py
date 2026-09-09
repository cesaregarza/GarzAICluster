from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.grant_ownership import (
    APPLIER_PATH,
    GrantOwnershipError,
    extract_applier_contract,
)
from scripts.grant_ownership_contract import COMMON_PATH
from tests.test_grant_ownership import _fake_agent_workloads


class SplitContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = _fake_agent_workloads(Path(self.directory.name) / "workloads")
        self.original = extract_applier_contract(self.root)
        self.entry = self.root / APPLIER_PATH
        self.common = self.root / COMMON_PATH
        self.common.write_bytes(self.entry.read_bytes())
        self.entry.write_text("from scripts.release_applier_common import REPO_ROOT\n")

    def test_split_contract_matches_monolith_without_execution(self) -> None:
        marker = Path(self.directory.name) / "executed"
        executable = f"\nopen({str(marker)!r}, 'w').write('executed')\n"
        for path in (self.entry, self.common):
            path.write_text(path.read_text() + executable)
        self.assertEqual(extract_applier_contract(self.root), self.original)
        self.assertFalse(marker.exists())

    def test_split_contract_rejects_invalid_source(self) -> None:
        original = self.common.read_text()
        cases = {
            "missing": original.replace("INFLUENCE_KEY", "RENAMED_KEY"),
            "dynamic": original.replace("'influence'", "dynamic_value", 1),
            "duplicate": original + "INFLUENCE_KEY = 'influence'\n",
            "duplicate_literal": original.replace("('approval_mode',)", "{'approval_mode', 'approval_mode'}"),
            "malformed": original + "broken syntax!\n",
            "wrong_type": original.replace("('approval_mode',)", "'approval_mode'"),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.common.write_text(text)
                with self.assertRaises(GrantOwnershipError):
                    extract_applier_contract(self.root)

    def test_conflicting_layouts_fail(self) -> None:
        self.entry.write_text(self.common.read_text())
        with self.assertRaisesRegex(GrantOwnershipError, "ambiguous"):
            extract_applier_contract(self.root)

    def test_unreferenced_common_does_not_mask_missing_contract(self) -> None:
        self.entry.write_text("from unreviewed.module import *\n")
        with self.assertRaisesRegex(GrantOwnershipError, "explicit common import"):
            extract_applier_contract(self.root)

    def test_missing_common_does_not_fall_back_to_snapshot(self) -> None:
        self.common.unlink()
        with self.assertRaisesRegex(GrantOwnershipError, "missing expected"):
            extract_applier_contract(self.root)

    def test_common_symlink_cannot_escape_checkout(self) -> None:
        outside = Path(self.directory.name) / "outside.py"
        self.common.rename(outside)
        self.common.symlink_to(outside)
        with self.assertRaisesRegex(GrantOwnershipError, "inside the explicit checkout"):
            extract_applier_contract(self.root)
