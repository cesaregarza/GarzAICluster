# Citrus Secrets

Encrypted runtime secrets consumed by the `citrus-secrets` Argo CD app.

- `django-secrets.enc.yaml`: Django, Stripe, and Postgres runtime environment for the Citrus Helm release.
- `django-email-secrets.enc.yaml`: SMTP and contact-address runtime environment for the Citrus Helm release.
- `regcred.enc.yaml`: DOCR pull credentials for the `default` namespace Citrus deployment.

Regenerate the Django environment files from `/root/dev/Citrus/.env` with the repo SOPS age key. Commit only encrypted `.enc.yaml` files.
