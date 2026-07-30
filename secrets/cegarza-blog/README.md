# cegarza.com Wagtail secrets

This directory is decrypted only by the Argo CD KSOPS plugin into the dedicated
`cegarza-blog` namespace.

Expected resources:

- `cegarza-blog-secrets`: `DATABASE_URL`, `DJANGO_SECRET_KEY`, and
  `DB_CA_CERT`
- `cegarza-apex-tls`: the still-valid apex certificate/key copied from Ghost
  for zero-gap activation; cert-manager assumes renewal after activation.
  Argo ignores later `/data` drift and cannot prune or delete this Secret, so
  routine sync and cascading Application deletion cannot overwrite or remove a
  cert-manager renewal
- `regcred`: a namespace-local copy of the existing DigitalOcean registry pull
  credential

Never place the plaintext database receipt or TLS private key in this
repository. Only `*.enc.yaml` SOPS ciphertext is allowed.
