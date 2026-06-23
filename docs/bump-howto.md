# How to request a bot deploy bump

### Easiest (one click)
- Open the **[Bump a bot image form](https://github.com/cesaregarza/GarzAICluster/issues/new?template=bump-bot.yml)**.
- Choose your bot, paste a **tag** (e.g., `v1.2.3`) or a **digest**.
- We’ll resolve to the canonical digest, open a PR, and mention you.

### Power users (comment)
Comment on your bump issue with the slash command:

```
/bump agent-8s v1.2.3
```

or

```
/bump agent-8s digest=sha256:<64-hex>
```

> All changes are gated by reviewers; nothing deploys without merge.
