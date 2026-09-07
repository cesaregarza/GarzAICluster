import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "helm/garz-observability/files/sentry_discord.py"
if not SCRIPT.exists():
    SCRIPT = Path(__file__).with_name("sentry_discord.py")
spec = importlib.util.spec_from_file_location("sentry_discord", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026, 9, 7, 10, tzinfo=timezone.utc)


def issue(i, when=NOW, level="error"):
    return {"id": str(i), "firstSeen": m.stamp(when), "title": "Example", "level": level, "project": {"slug": "test"}}


class PollTests(unittest.TestCase):
    def setUp(self):
        self.db = m.open_state(":memory:")
        self.addCleanup(self.db.close)
        self.sentry = Mock()
        self.sentry.issues.return_value = []
        self.discord = Mock()

    def initialize(self):
        m.poll(self.db, self.sentry, self.discord, NOW)

    def test_first_run_does_not_flood_history(self):
        self.sentry.issues.return_value = [issue(1, NOW - timedelta(days=1))]
        result = m.poll(self.db, self.sentry, self.discord, NOW)
        self.assertTrue(result["initialized"])
        self.discord.send.assert_not_called()

    def test_auth_failure_does_not_initialize(self):
        self.sentry.issues.side_effect = m.PollError("HTTP request failed with status 403")
        with self.assertRaises(m.PollError):
            self.initialize()
        self.assertEqual(self.db.execute("SELECT count(*) FROM meta").fetchone()[0], 0)

    def test_duplicate_pages_and_runs_notify_once(self):
        self.initialize()
        self.sentry.issues.return_value = [issue(1), issue(1)]
        m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=3))
        m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=6))
        self.assertEqual(self.discord.send.call_count, 1)

    def test_failed_send_preserves_checkpoint_and_acknowledged_ids(self):
        self.initialize()
        self.sentry.issues.return_value = [issue(1), issue(2)]
        self.discord.send.side_effect = [None, m.PollError("failed")]
        with self.assertRaises(m.PollError):
            m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=3))
        self.assertEqual(dict(self.db.execute("SELECT * FROM meta"))["checkpoint"], m.stamp(NOW))
        self.discord.send.side_effect = None
        m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=6))
        self.assertEqual([call.args[0]["id"] for call in self.discord.send.call_args_list], ["1", "2", "2"])

    def test_delivery_cap_retains_backlog(self):
        self.initialize()
        self.sentry.issues.return_value = [issue(1), issue(2)]
        self.assertTrue(m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=3), max_alerts=1)["backlog"])
        self.assertEqual(dict(self.db.execute("SELECT * FROM meta"))["checkpoint"], m.stamp(NOW))
        result = m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=6), max_alerts=1)
        self.assertFalse(result["backlog"])
        self.assertEqual(self.discord.send.call_count, 2)

    def test_old_and_future_issues_are_ignored(self):
        self.initialize()
        self.sentry.issues.return_value = [issue(1, NOW-timedelta(seconds=1)), issue(2, NOW+timedelta(days=1))]
        m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=3))
        self.discord.send.assert_not_called()

    def test_only_error_and_fatal_are_delivered_and_count_toward_cap(self):
        self.initialize()
        rows = [issue(i, level=level) for i, level in enumerate(
            ("debug", "info", "warning", "critical", "ERROR", "", None, "error", "fatal"))]
        missing = issue("missing"); missing.pop("level")
        self.sentry.issues.return_value = rows + [missing]
        result = m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=3), max_alerts=2)
        self.assertEqual(result, {"sent": 2, "backlog": False})
        self.assertEqual([call.args[0]["level"] for call in self.discord.send.call_args_list], ["error", "fatal"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent").fetchone()[0], 2)
        self.assertEqual(dict(self.db.execute("SELECT * FROM meta"))["checkpoint"], m.stamp(NOW + timedelta(minutes=3)))
        m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=6))
        self.assertEqual(self.discord.send.call_count, 2)

    def test_overlap_recovers_delayed_indexing(self):
        self.initialize()
        m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=30))
        self.sentry.issues.return_value = [issue(1, NOW + timedelta(minutes=5))]
        m.poll(self.db, self.sentry, self.discord, NOW + timedelta(minutes=33))
        self.assertEqual(self.discord.send.call_count, 1)


class TransportTests(unittest.TestCase):
    @patch.object(m, "request_json")
    def test_pagination_never_forwards_token_to_link_host(self, request):
        request.side_effect = [([issue(1)], {"Link": '<https://evil.invalid/?cursor=next>; rel="next"; results="true"'}),
                               ([issue(2)], {})]
        result = m.Sentry("example", "fake").issues(NOW, NOW + timedelta(minutes=3))
        self.assertEqual(len(result), 2)
        self.assertTrue(request.call_args_list[1].args[0].startswith("https://sentry.io/"))
        self.assertIn("cursor=next", request.call_args_list[1].args[0])
        for call in request.call_args_list:
            query = parse_qs(urlsplit(call.args[0]).query)
            self.assertEqual(query["query"], [f"firstSeen:>={m.stamp(NOW)} level:[error,fatal]"])

    @patch.object(m, "request_json")
    def test_repeated_cursor_fails(self, request):
        request.return_value = ([], {"Link": '<https://sentry.io/?cursor=x>; rel="next"; results="true"'})
        with self.assertRaises(m.PollError):
            m.Sentry("example", "fake").issues(NOW, NOW)

    @patch.object(m, "request_json")
    def test_discord_disables_mentions_and_limits_title(self, request):
        row = issue(1); row["title"] = "@everyone " * 100; row["permalink"] = "https://evil.invalid/"
        m.Discord("https://discord.com/api/webhooks/123/fake").send(row)
        payload = request.call_args.args[2]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertLessEqual(len(payload["embeds"][0]["title"]), 256)
        self.assertNotIn("url", payload["embeds"][0])

    def test_wrong_secret_destination_rejected(self):
        with self.assertRaises(m.PollError):
            m.Discord("https://evil.invalid/api/webhooks/123/fake")
        with self.assertRaises(m.PollError):
            m.Sentry("../other", "fake")


if __name__ == "__main__":
    unittest.main()
