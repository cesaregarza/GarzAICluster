# Citrus Cloudflare Access activation and rollback

CES-829 prepares an origin-side identity gate for Citrus operator paths. The
chart support is disabled by default and does not create a Secret or change the
current production or development render.

## Confidential inputs

Create an environment-owned encrypted Secret outside this preparation change.
It must expose exactly these keys to the web Deployment:

- `CLOUDFLARE_ACCESS_TEAM_DOMAIN`
- `CLOUDFLARE_ACCESS_AUD`
- `CLOUDFLARE_ACCESS_ALLOWED_EMAILS`

Never place their values in chart values, a ConfigMap, a pull request, a command
line, or logs. Development and production use distinct Secret names and
namespace ownership.

## Preconditions

1. The exact immutable Citrus image has passed the source tests for `/admin`,
   `/dashboard`, and `/api/dashboard` origin validation.
2. One Cloudflare Access application covers every protected Citrus hostname
   and every checked-in source path in
   `docs/cloudflare-access-origin-validation.md` with one shared audience, and
   its allowlist policy exists through a separately approved operator change.
   This includes the legacy payment, event, media-upload, and catalog action
   patterns outside `/dashboard` and `/api/dashboard`. Separate applications
   with distinct audiences are not supported by this release. This chart does
   not create either resource.
3. The confidential Secret is encrypted, namespace-owned, and independently
   reviewed.
4. `verifiedImageTag` exactly matches the 40-character `image.tag` containing
   the middleware. The chart rejects mutable or mismatched receipts.
5. If a development payment-deny policy is active, exact JWK egress must first
   be authorized without publishing the team domain. The chart deliberately
   rejects this unsafe combination until that separate contract exists.

## Activation

Set `cloudflareAccess.enabled=true`, the environment owner and dedicated Secret
name, a new `rolloutRevision`, and the matching `verifiedImageTag`. The chart
projects three non-optional Secret keys only into the Django web container and
sets `CLOUDFLARE_ACCESS_REQUIRED=true` explicitly.

After a separately approved sync, verify through synthetic identities that:

- missing, expired, wrong-audience, wrong-issuer, and non-allowlisted assertions
  receive a generic `403` on every protected prefix;
- direct-origin requests cannot bypass the gate;
- customer, health, webhook, and machine-ingestion routes remain reachable by
  their existing contracts; and
- Django authentication still applies after a valid Access assertion.

Do not use payment, refund, SMS, or other provider-mutating actions for this
verification.

## Rollback and break glass

Cloudflare failure intentionally locks operators out. A rollback requires a
separately approved edge-policy change and GitOps change in the same window:

1. retain Django authentication and the dedicated Secret;
2. disable the Cloudflare edge policy and origin requirement together;
3. reconcile and verify the protected routes return to Django authentication;
4. remove the Secret only after no pod references it.

Never expose an unprotected operator origin while assuming the edge policy is
the only control.
