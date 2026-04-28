# garz.ai Secrets

Encrypted secrets consumed by the `garz-ai-secrets` Argo CD app.

- `regcred.enc.yaml`: DOCR pull credentials for `registry.digitalocean.com/sendouq/garz-ai`.

Regenerate with the repo SOPS age key and the shared registry read token if the registry credential rotates.
