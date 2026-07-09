# Citrus Spaces CORS Runbook

This runbook applies the deployment-owned CORS contract for Citrus Grace browser
media uploads to DigitalOcean Spaces. The helm chart configures the application
bucket names and credentials, but Spaces bucket CORS is external bucket state and
must be applied by an operator with Spaces admin credentials.

Live infrastructure mutations require explicit operator approval. Codex may
update this source-controlled runbook, policy payload, and helper script, but
must not apply bucket CORS unattended.

## Scope

Apply the same CORS policy to both Citrus media buckets:

| Environment | Bucket | Helm value source |
| --- | --- | --- |
| Prod | `citrus-media` | `helm/citrus/values.yaml` |
| Dev | `citrus-media-dev` | `helm/citrus/values-dev.yaml` |

The checked-in CORS payload is `infra/spaces/citrus-media-cors.json`.

The allowed browser origins are:

- `https://citrus-grace.com`
- `https://www.citrus-grace.com`
- `https://dev.citrus-grace.com`

The allowed methods are `GET` and `POST`. Preflight `OPTIONS` is evaluated by
Spaces against this rule and does not need to be listed as an allowed method.
The payload allows all request headers because presigned browser uploads can
include `Content-Type`, `policy`, and multiple `x-amz-*` form/header fields.

## References

- DigitalOcean Spaces CORS docs: https://docs.digitalocean.com/products/spaces/how-to/configure-cors/
- DigitalOcean Spaces S3 compatibility docs: https://docs.digitalocean.com/products/spaces/reference/s3-compatibility/
- AWS CLI `put-bucket-cors` command reference: https://docs.aws.amazon.com/cli/latest/reference/s3api/put-bucket-cors.html

## Apply

Prerequisites:

- `doctl` authenticated to a DigitalOcean account that can create and delete
  Spaces keys.
- `s3cmd`, `jq`, `python3`, and `curl`.
- Operator approval to mutate both buckets.

Run the checked-in helper:

```bash
cd /path/to/GarzAICluster
bash scripts/apply_citrus_spaces_cors.sh
```

The helper:

- Creates a temporary Spaces key with `doctl spaces keys create`.
- Converts `infra/spaces/citrus-media-cors.json` to temporary S3 CORS XML.
- Applies the policy to `citrus-media` and `citrus-media-dev` with
  `s3cmd setcors`.
- Runs POST preflight checks for all allowed origins against both buckets.
- Deletes the temporary Spaces key in an exit trap.

By default the helper uses `CITRUS_SPACES_KEY_GRANTS="bucket=;permission=fullaccess"`.
DigitalOcean rejects bucket-scoped `fullaccess` grants and bucket-scoped
`readwrite` grants cannot update bucket CORS (`AccessDenied`). Keep the key
temporary and confirm deletion after the run.

The operation replaces the full bucket CORS configuration, so preserve any
future additional rules in `infra/spaces/citrus-media-cors.json` before
applying.

## Verify

Confirm no temporary CES-478 key remains:

```bash
doctl spaces keys list --format Name,AccessKey,Grants,CreatedAt -o json \
  | jq -r '.[] | select(.name | startswith("ces-478-cors-"))'
```

The helper already verifies browser preflight behavior from every allowed origin
to each bucket. To rerun only the preflight checks:

```bash
for bucket in citrus-media citrus-media-dev; do
  for origin in \
    https://citrus-grace.com \
    https://www.citrus-grace.com \
    https://dev.citrus-grace.com; do
    echo "== ${bucket} ${origin}"
    curl -i -X OPTIONS "https://${bucket}.nyc3.digitaloceanspaces.com/" \
      -H "Origin: ${origin}" \
      -H "Access-Control-Request-Method: POST" \
      -H "Access-Control-Request-Headers: content-type,x-amz-algorithm,x-amz-credential,x-amz-date,x-amz-signature,policy"
  done
done
```

Each response should be a successful preflight and include
`Access-Control-Allow-Origin` for the tested origin plus `POST` in
`Access-Control-Allow-Methods`.

For an end-to-end browser upload, generate a real presigned POST from the Citrus
API and upload through the frontend. A generic unsigned POST to the bucket is
expected to fail authorization even when CORS is correct.

If validating GET responses through the Spaces CDN domains
`citrus-media.nyc3.cdn.digitaloceanspaces.com` or
`citrus-media-dev.nyc3.cdn.digitaloceanspaces.com`, purge the CDN cache after
changing CORS so cached responses do not hide the new CORS headers.

## Rollback

There is no GitOps controller reconciling this bucket state. To roll back, commit
the previous policy payload and rerun the apply script. Do not delete bucket
CORS unless the frontend direct-upload flow has been disabled or replaced.
