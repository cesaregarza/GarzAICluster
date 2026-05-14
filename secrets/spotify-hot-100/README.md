# spotify-hot-100 Secrets

Encrypted runtime secrets consumed by the `spotify-hot-100-secrets` Argo CD app.

- `app-secret.enc.yaml`: Postgres connection URL, Spotify OAuth client values, and session signing secret.

The app runs in the `default` namespace and reuses the existing `regcred` pull secret there.
