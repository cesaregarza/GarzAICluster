# Citrus Dev Secrets

Encrypted runtime secrets consumed by the `citrus-dev-secrets` Argo CD app.

- `django-secrets.enc.yaml`: Django, Stripe, and Postgres runtime environment for the `citrus-dev` Helm release.
- `django-email-secrets.enc.yaml`: SMTP and contact-address runtime environment for the `citrus-dev` Helm release.
- `regcred.enc.yaml`: DOCR pull credentials used by Citrus dev image pulls and migration hooks.

Commit only encrypted `.enc.yaml` files.
