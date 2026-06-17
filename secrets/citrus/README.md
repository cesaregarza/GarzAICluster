# Citrus Secrets

Encrypted runtime secrets consumed by the `citrus-secrets` Argo CD app.

- `django-secrets.enc.yaml`: Django and Stripe runtime environment for the Citrus Helm release.

Regenerate from `/root/dev/Citrus/.env` with the repo SOPS age key. Commit only encrypted `.enc.yaml` files.
