# Poetry secret activation

This directory is intentionally inert. Before setting `helm/poetry/values.yaml`
`enabled: true`, create and SOPS-encrypt the following Kubernetes Secrets with
the repository age recipient, list the encrypted files in `ksops.yaml`, and
enable the generator in `kustomization.yaml`:

- `poetry-database`: `DATABASE_URL`
- `poetry-django`: `DJANGO_SECRET_KEY`
- `poetry-media`: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- `regcred` (`kubernetes.io/dockerconfigjson`): `.dockerconfigjson`

Do not commit unencrypted Secret manifests or placeholder credentials. Validate
the completed directory with the Argo CD KSOPS/kustomize build before enabling
the chart.
