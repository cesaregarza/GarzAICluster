# Citrus Secrets

Encrypted runtime secrets consumed by the `citrus-secrets` Argo CD app.

- `django-secrets.enc.yaml`: Django, Stripe, and Postgres runtime environment for the Citrus Helm release.
- `django-email-secrets.enc.yaml`: SMTP and contact-address runtime environment for the Citrus Helm release.
- `citrus-backup-secrets` must exist in the live namespace for the backup CronJob. It is expected to provide
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` with write/delete access to the configured DO Spaces backup prefix.

Regenerate from `/root/dev/Citrus/.env` with the repo SOPS age key. Commit only encrypted `.enc.yaml` files.
