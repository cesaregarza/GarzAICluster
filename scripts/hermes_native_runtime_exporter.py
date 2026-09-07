#!/usr/bin/env python3
"""Expose non-sensitive native Hermes health on a private Prometheus endpoint."""

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import time


HOST = os.getenv("HERMES_EXPORTER_HOST", "10.108.0.8")
PORT = int(os.getenv("HERMES_EXPORTER_PORT", "8080"))
HERMES_HOME = Path(os.getenv("HERMES_HOME", "/var/lib/hermes"))
TRIAGE_JOB_ID = os.environ["HERMES_ALERT_TRIAGE_JOB_ID"]


def gateway_ok() -> bool:
    result = subprocess.run(
        ["/bin/systemctl", "is-active", "--quiet", "hermes-gateway.service"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=3,
        check=False,
    )
    return result.returncode == 0


def cron_ticker_ok() -> bool:
    try:
        return time.time() - (HERMES_HOME / "cron/ticker_heartbeat").stat().st_mtime < 180
    except OSError:
        return False


def alert_triage_ok() -> bool:
    try:
        store = json.loads((HERMES_HOME / "cron/jobs.json").read_text())
    except (OSError, ValueError):
        return False
    jobs = store.get("jobs", []) if isinstance(store, dict) else store
    if not isinstance(jobs, list):
        return False
    job = next(
        (
            row
            for row in jobs
            if isinstance(row, dict) and row.get("id") == TRIAGE_JOB_ID
        ),
        None,
    )
    if not job or not job.get("enabled") or job.get("state") != "scheduled":
        return False
    if job.get("last_status") != "ok" or job.get("last_delivery_error"):
        return False
    try:
        completed = datetime.fromisoformat(job["last_run_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    return time.time() - completed < 600


def render_metrics() -> bytes:
    checks = {
        "gateway": gateway_ok(),
        "cron_ticker": cron_ticker_ok(),
        "alert_triage": alert_triage_ok(),
    }
    lines = [
        "# HELP hermes_native_runtime_ready Whether all native Hermes runtime checks pass.",
        "# TYPE hermes_native_runtime_ready gauge",
        f"hermes_native_runtime_ready {int(all(checks.values()))}",
        "# HELP hermes_native_runtime_check_ok Whether a named native Hermes check passes.",
        "# TYPE hermes_native_runtime_check_ok gauge",
    ]
    lines.extend(
        f'hermes_native_runtime_check_ok{{check="{name}"}} {int(ok)}'
        for name, ok in checks.items()
    )
    return ("\n".join(lines) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/metrics", "/healthz"}:
            self.send_error(404)
            return
        body = render_metrics()
        ready = b"hermes_native_runtime_ready 1\n" in body
        self.send_response(200 if self.path == "/metrics" or ready else 503)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
