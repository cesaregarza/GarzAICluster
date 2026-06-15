# Agent Workloads Identity Digest Drift Gate

Local CI gate for this deployment repository. It fetches the scoped SOPS
drift-gate key from the credential broker, decrypts only the workload identity
token secret, and runs the repo-local check that compares token `code_digest`
claims to deployed release pins.

## Threat Model

This protects against deployment config that updates workload image or manifest
pins without re-minting the matching workload identity tokens. The brokered Age
key is scoped to the workload identity token secret and is exposed only to this
job.

It does not mint tokens, decide which workload digest is correct, approve a
deployment, grant dispatch, or validate runtime behavior. This repo still owns
its release pins, SOPS policy, branch protection, and required-check
configuration.

The action must never receive repository secrets, workload identity HMAC seeds,
provider credentials, database credentials, or raw SOPS keys as inputs. It uses
GitHub OIDC to request only the `sops-drift-gate-decrypt` capability from the
credential broker. The consuming job grants `id-token: write` and otherwise
keeps `contents: read`.
