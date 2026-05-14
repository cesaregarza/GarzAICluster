# spotify-hot-100 Secrets

Encrypted runtime secrets consumed by the `spotify-hot-100-secrets` Argo CD app.

- `app-secret.enc.yaml`: Postgres connection URL, Spotify OAuth client values, and session signing secret.

The app uses the shared managed Postgres cluster's `xscraper` database with the
dedicated `spotify_hot_100` schema. The database role is configured with that
schema in its `search_path`, matching the schema-isolated pattern used by the
other SplatTop services.

The app runs in the `default` namespace and reuses the existing `regcred` pull secret there.
