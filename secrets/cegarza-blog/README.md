# cegarza.com Wagtail secrets

This directory is decrypted only by the Argo CD KSOPS plugin into the dedicated
`cegarza-blog` namespace.

Expected resources:

- `cegarza-blog-secrets`: `DATABASE_URL`, `DJANGO_SECRET_KEY`, and
  `DB_CA_CERT`
- `cegarza-apex-tls`: a freshly reissued apex certificate/key with a dedicated
  post-migration private key for zero-gap activation; cert-manager assumes
  renewal only after a separately authorized activation. During the dev phase,
  Argo replaces the formerly exposed key with this clean pair. It cannot prune
  or delete the Secret; the future activation change must add cert-manager
  `/data` drift ownership after the replacement is proven live
- `regcred`: the namespace-local copy of the shared DigitalOcean registry
  credential. Replacing the shared read/write credential with one pull-only
  runtime credential is a separate, coordinated change and is not part of the
  dev preview rollout

The Secret application uses server-side apply so future Argo updates do not
write Secret payloads into the
`kubectl.kubernetes.io/last-applied-configuration` annotation. The rotated app
and apex Secrets additionally use replace semantics to remove their previously
persisted annotations; `regcred` remains part of the separately coordinated
registry-credential cleanup.

Never place the plaintext database receipt or TLS private key in this
repository. Only `*.enc.yaml` SOPS ciphertext is allowed.
