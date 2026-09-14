import unittest

from scripts.check_worker_chart_migration import compare


class WorkerChartMigrationProofTests(unittest.TestCase):
    def test_map_order_is_irrelevant_but_field_types_and_list_order_are_not(self):
        baseline = {"deployment": {"enabled": True, "args": ["a", "b"]}}
        self.assertTrue(
            compare(baseline, {"deployment": {"args": ["a", "b"], "enabled": True}})[
                "verified"
            ]
        )
        for changed in (
            {"enabled": 1, "args": ["a", "b"]},
            {"enabled": True, "args": ["b", "a"]},
        ):
            with self.subTest(changed=changed):
                self.assertFalse(compare(baseline, {"deployment": changed})["verified"])
        self.assertFalse(compare(baseline, {})["verified"])
