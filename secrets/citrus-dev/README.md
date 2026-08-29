# Citrus Dev Secrets

Encrypted runtime secrets consumed by the `citrus-dev-secrets` Argo CD app.

- `django-secrets.enc.yaml`: legacy Django, payment, and Postgres source. When
  CES-844 isolation is enabled, workloads select only its Postgres fields and
  each dev Django runtime selects its exact dev webhook field; it is never
  imported broadly.
- `citrus-dev-payment-credentials.enc.yaml`: dedicated test-mode API and
  publishable settings for the `citrus-dev` Helm release. It contains no
  webhook or production setting.
- `citrus-dev-sweep-runtime.enc.yaml`: dedicated provider-free application key
  and database settings for the suspended direct-order payment sweep. It
  contains exactly `DJANGO_SECRET_KEY`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`,
  `DB_HOST`, and `DB_PORT`.
- `django-email-secrets.enc.yaml`: SMTP and contact-address runtime environment for the `citrus-dev` Helm release.
- `django-spaces-secrets.enc.yaml`: DO Spaces media upload credentials for the `citrus-media-dev` bucket.
- `regcred.enc.yaml`: DOCR pull credentials used by Citrus dev image pulls and migration hooks.

Commit only encrypted `.enc.yaml` files.
