#!/usr/bin/env python3
"""Poll new Sentry issues and deliver Discord alerts with persistent deduplication."""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class PollError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise PollError("Timestamp is missing its timezone")
    return result


def request_json(url, headers, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=body, headers={"User-Agent": "GarzAI-Sentry-Alerts/1.0", **headers})
    for attempt in range(3):
        try:
            with build_opener(NoRedirect).open(req, timeout=25) as response:
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise PollError("Response exceeded size limit")
                return json.loads(data) if data else None, response.headers
        except HTTPError as exc:
            # Never log request URLs, response bodies, or exception reprs: webhook URLs are secrets.
            if exc.code == 429 and attempt < 2:
                try:
                    delay = float(exc.headers.get("Retry-After", "5"))
                except ValueError:
                    delay = 5
                if 0 <= delay <= 30:
                    time.sleep(delay + 0.5)
                    continue
            raise PollError(f"HTTP request failed with status {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise PollError("Network request failed; retry on next scheduled run") from None
    raise PollError("HTTP retry limit reached")


def next_cursor(link):
    for part in link.split(","):
        if 'rel="next"' in part and 'results="true"' in part:
            match = re.search(r"<([^>]+)>", part)
            cursor = parse_qs(urlsplit(match.group(1)).query).get("cursor", []) if match else []
            if not cursor:
                raise PollError("Sentry returned a next page without a cursor")
            # Reuse only the cursor; never follow an arbitrary Link URL with our token.
            return cursor[0]
    return None


class Sentry:
    def __init__(self, organization, token, projects=()):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", organization):
            raise PollError("Invalid Sentry organization slug")
        self.url = f"https://sentry.io/api/0/organizations/{organization}/issues/"
        self.token = token
        self.projects = projects

    def issues(self, since, until):
        cursor = None
        visited = set()
        issues = {}
        for _ in range(20):
            params = {"query": f"firstSeen:>={stamp(since)}", "sort": "new", "limit": 100,
                      "start": stamp(since), "end": stamp(until)}
            if self.projects:
                params["project"] = self.projects
            if cursor:
                params["cursor"] = cursor
            rows, headers = request_json(self.url + "?" + urlencode(params, doseq=True),
                                         {"Authorization": "Bearer " + self.token})
            if not isinstance(rows, list):
                raise PollError("Unexpected Sentry response")
            for issue in rows:
                if not isinstance(issue, dict) or not issue.get("id") or not issue.get("firstSeen"):
                    raise PollError("Sentry issue is missing identity or first-seen time")
                parse_time(issue["firstSeen"])
                issues[str(issue["id"])] = issue
            cursor = next_cursor(headers.get("Link", ""))
            if not cursor:
                return list(issues.values())
            if cursor in visited:
                raise PollError("Sentry pagination repeated a cursor")
            visited.add(cursor)
        raise PollError("Sentry pagination limit reached; checkpoint was not advanced")


class Discord:
    def __init__(self, url):
        if not re.fullmatch(r"https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9._-]+", url):
            raise PollError("Invalid Discord webhook URL")
        self.url = url

    def send(self, issue):
        project = issue.get("project", {}).get("slug", "unknown")
        title = f"Sentry · {issue.get('shortId', issue['id'])}: {issue.get('title', 'New issue')}"[:256]
        embed = {"title": title, "color": 15158332,
                 "description": f"Project: {project}\nLevel: {issue.get('level', 'error')}\nFirst seen: {issue['firstSeen']}"[:1500]}
        link = issue.get("permalink", "")
        parsed = urlsplit(link)
        if parsed.scheme == "https" and (parsed.hostname == "sentry.io" or (parsed.hostname or "").endswith(".sentry.io")):
            embed["url"] = link
        request_json(self.url + "?wait=true", {"Content-Type": "application/json"},
                     {"username": "Sentry Alerts", "embeds": [embed], "allowed_mentions": {"parse": []}})

    def test(self):
        request_json(self.url + "?wait=true", {"Content-Type": "application/json"},
                     {"username": "Sentry Alerts", "content": "Sentry poller connected from GarzAICluster. This is a setup test, not an incident.",
                      "allowed_mentions": {"parse": []}})


def open_state(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, first_seen TEXT NOT NULL)")
    db.commit()
    return db


def poll(db, sentry, discord, now, max_alerts=20):
    meta = dict(db.execute("SELECT key, value FROM meta"))
    if not meta:
        # Prove API access before establishing the baseline; do not announce historical issues.
        sentry.issues(now - timedelta(minutes=1), now)
        with db:
            db.executemany("INSERT INTO meta VALUES (?, ?)", [("baseline", stamp(now)), ("checkpoint", stamp(now))])
        return {"initialized": True, "sent": 0}
    baseline = parse_time(meta["baseline"])
    since = max(baseline, parse_time(meta["checkpoint"]) - timedelta(hours=1))
    issues = sentry.issues(since, now)
    count = 0
    for issue in sorted(issues, key=lambda row: (parse_time(row["firstSeen"]), str(row["id"]))):
        first_seen = parse_time(issue["firstSeen"])
        if first_seen < baseline or first_seen > now:
            continue
        if db.execute("SELECT 1 FROM sent WHERE id = ?", (str(issue["id"]),)).fetchone():
            continue
        if count >= max_alerts:
            return {"sent": count, "backlog": True}
        discord.send(issue)
        # Commit after every acknowledged send so later failures do not replay earlier messages.
        with db:
            db.execute("INSERT INTO sent VALUES (?, ?)", (str(issue["id"]), issue["firstSeen"]))
        count += 1
    with db:
        db.execute("UPDATE meta SET value = ? WHERE key = 'checkpoint'", (stamp(now),))
        db.execute("DELETE FROM sent WHERE first_seen < ?", (stamp(since - timedelta(days=2)),))
    return {"sent": count, "backlog": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read Sentry only; do not change state or notify")
    parser.add_argument("--test-notification", action="store_true", help="Send one explicit Discord setup test")
    args = parser.parse_args()
    token = Path(os.environ.get("SENTRY_TOKEN_FILE", "/secrets/sentry-token")).read_text().strip()
    if not token:
        raise PollError("Sentry token is empty")
    sentry = Sentry(os.environ["SENTRY_ORG"], token, os.environ.get("SENTRY_PROJECTS", "").split(",") if os.environ.get("SENTRY_PROJECTS") else [])
    now = datetime.now(timezone.utc)
    if args.check:
        print(json.dumps({"api_access": "ok", "recent_issues": len(sentry.issues(now - timedelta(hours=1), now))}))
        return
    webhook = Path(os.environ.get("DISCORD_WEBHOOK_FILE", "/secrets/discord-webhook-url")).read_text().strip()
    discord = Discord(webhook)
    if args.test_notification:
        discord.test()
        print(json.dumps({"test_notification": "sent"}))
        return
    state_dir = Path(os.environ.get("STATE_DIR", "/state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "poll.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = open_state(state_dir / "state.sqlite3")
        try:
            print(json.dumps(poll(db, sentry, discord, now)))
        finally:
            db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Unexpected exceptions also avoid exposing secret URLs or provider response bodies.
        print(json.dumps({"error": str(exc) if isinstance(exc, PollError) else type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
