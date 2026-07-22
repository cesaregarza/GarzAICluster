# Poetry secret activation

This directory contains the SOPS-encrypted runtime Secrets required before
setting `helm/poetry/values.yaml` `enabled: true`:

- `poetry-database`: `DATABASE_URL`
- `poetry-django`: `DJANGO_SECRET_KEY`
- `poetry-media`: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- `regcred` (`kubernetes.io/dockerconfigjson`): namespace-local, read-only
  `.dockerconfigjson`

Only ciphertext belongs here. Validate the directory with the Argo CD
KSOPS/kustomize build and confirm the `poetry-secrets` Application is Synced and
Healthy before enabling the chart.
