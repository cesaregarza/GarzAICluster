# Argo CD Repository Credentials

Encrypted repository credentials consumed by the `argocd-repositories` Argo CD
app.

- `agent-platform-repository.enc.yaml`: read-only deploy-key credential for the
  private `cesaregarza/agent-platform` repository.

The deploy key is repo-scoped and read-only. Rotate it by creating a new GitHub
deploy key, replacing the encrypted private key Secret, syncing
`argocd-repositories`, then deleting the old deploy key from GitHub.
