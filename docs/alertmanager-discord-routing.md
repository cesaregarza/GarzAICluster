# Alertmanager Discord routing

The production Alertmanager config is a manually managed Secret because its receivers contain Discord webhook credentials. Keep those credentials out of Git. The root route uses this non-secret policy:

```yaml
route:
  receiver: discord-critical
  group_by: [alertname, service, severity]
  group_wait: 30s
  group_interval: 15m
  repeat_interval: 12h
```

Grouping by alert identity prevents a state change in one alert from resending unrelated active alerts. A still-firing group gets at most one reminder every twelve hours. Resolutions remain enabled. The narrower Citrus development Stripe route keeps its existing labels and four-hour repeat interval.

Before applying a route change, decode the Secret only inside a process, preserve every receiver and webhook URL in memory, validate the proposed YAML with the running Alertmanager's `amtool`, update only `data["alertmanager.yaml"]`, and restart the exact Alertmanager deployment. Never print the decoded config. Roll back by removing the four root timing/grouping fields; the receiver list does not change.
