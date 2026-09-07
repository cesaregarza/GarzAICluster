# Hermes native runtime monitoring

The nyc3 Hermes host runs the Python gateway directly under `hermes-gateway.service`. The former `hermes-governed-runtime.service` container must not be restarted: its image starts another Discord gateway and would compete for the Canary bot session.

The service name now owns a small read-only exporter instead. Install `scripts/hermes_native_runtime_exporter.py` as `/usr/local/lib/hermes-native-runtime-exporter.py` and `infra/hermes/hermes-native-runtime-exporter.service` as `/etc/systemd/system/hermes-governed-runtime.service`. The compatibility service name avoids an unmanaged orphan unit; the Prometheus job and metrics use `hermes-native-runtime` naming.

The exporter binds only to the host's private `10.108.0.8:8080` address and exposes three checks:

- `gateway`: the native gateway systemd unit is active;
- `cron_ticker`: the native ticker heartbeat is newer than three minutes;
- `alert_triage`: the private triage job is enabled, recently successful, and has no delivery error.

It reads no Discord content or credentials. The existing monitoring policy permits the Prometheus pod to reach the private endpoint. `/metrics` always returns Prometheus text; `/healthz` returns 503 when a check fails.

Hermes cron workers need a lingering user manager because the managed gateway deliberately launches restart-safe work outside its own cgroup. Enable lingering for user `hermes`, start `user@997.service`, and give `hermes-gateway.service` these environment variables in a drop-in:

```ini
[Unit]
After=user@997.service
Wants=user@997.service

[Service]
Environment="XDG_RUNTIME_DIR=/run/user/997"
Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/997/bus"
```

After restart, verify `systemd-run --user --scope` as `hermes`, wait for one scheduled triage run, and require every exported check to equal one. Keep the previous systemd unit as a timestamped backup for evidence, but never start that backup as a second gateway.
