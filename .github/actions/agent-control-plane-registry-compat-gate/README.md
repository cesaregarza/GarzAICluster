# Agent Control Plane Registry Compatibility Gate

Local CI gate for this deployment repository. It reads the deployment-pinned
`agent-platform` revision, fetches a broker-scoped read token, checks out that
exact revision, and runs the repo-local compatibility validator against the
pull request config.

## Threat Model

This protects against merging deployment registry overlays that are incompatible
with the actually deployed Mandate control-plane revision. This repo still owns
the pin and the overlay; the action only materializes the deployed source and
runs the repo-local validator.

It does not grant dispatch, import manifests, approve capabilities, deploy the
control plane, or decide whether an overlay is operationally desirable. A
passing compatibility check is only one required input to normal review, policy,
admission, lease, broker, and output-gate paths.

The action must never receive repository secrets, provider credentials,
database credentials, deployment tokens, or raw vault access as inputs. It uses
GitHub OIDC to request only the `mandate-contracts-read` capability from the
credential broker. The consuming job grants `id-token: write` and otherwise
keeps `contents: read`.
