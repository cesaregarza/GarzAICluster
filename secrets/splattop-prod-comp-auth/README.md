# SplatTop competition auth secrets (prod)

Secrets are encrypted with SOPS/age. Edit with:

```
SOPS_AGE_KEY_FILE=keys/age-private.txt sops secrets/splattop-prod-comp-auth/comp-auth-secrets.enc.yaml
```

Expected keys (stringData):
- `COMP_AUTH_ADMIN_DISCORD_IDS`
- `COMP_DISCORD_CLIENT_ID`
- `COMP_DISCORD_CLIENT_SECRET`
- `COMP_DISCORD_REDIRECT_URI`
- `COMP_AUTH_SESSION_SECRET`
