# Citrus Secrets

Encrypted runtime secrets consumed by the `citrus-secrets` Argo CD app.

- `django-secrets.enc.yaml`: Django, Stripe, and Postgres runtime environment for the Citrus Helm release.
- `django-email-secrets.enc.yaml`: SMTP and contact-address runtime environment for the Citrus Helm release.
- `django-spaces-secrets.enc.yaml`: DO Spaces media upload credentials for the Citrus media bucket.
- `regcred.enc.yaml`: DOCR pull credentials for the `default` namespace Citrus deployment.

Regenerate environment secret files from `/root/dev/Citrus/.env` with the repo SOPS age key. Commit only encrypted `.enc.yaml` files.
