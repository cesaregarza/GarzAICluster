# skyquiet-server Secrets

Runtime secret planned for the `skyquiet-server` Helm release.

Create a SOPS-encrypted `app-secret.enc.yaml` before enabling the deployment in
production. The secret must be named `skyquiet-server-secrets` in the `default`
namespace and include:

- `DATABASE_URL`: shared managed Postgres connection string for the dedicated
  `skyquiet` schema.
- `DEVICE_SECRET_PEPPER`: high-entropy secret used to hash per-install device
  bearer tokens.
- `EXPO_ACCESS_TOKEN`: optional Expo push access token. Omit it unless Expo
  push requests need to use an authenticated Expo access token.

The app reuses the existing `regcred` pull secret in the `default` namespace.
