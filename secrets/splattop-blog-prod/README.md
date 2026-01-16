# SplatTop Blog secrets (prod)

Secrets are encrypted with SOPS/age. Edit with:

```
SOPS_AGE_KEY_FILE=keys/age-private.txt sops secrets/splattop-blog-prod/blog-db-secrets.enc.yaml
```

Expected keys (stringData):
- `DATABASE_URL`
- `DJANGO_SECRET_KEY`
