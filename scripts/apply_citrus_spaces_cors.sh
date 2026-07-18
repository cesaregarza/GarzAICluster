#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Apply the Citrus Grace Spaces bucket CORS policy with a temporary Spaces key.

This creates a short-lived DigitalOcean Spaces key, applies
infra/spaces/citrus-media-cors.json to citrus-media and citrus-media-dev, runs
POST preflight checks for all allowed origins, and deletes the temporary key on
exit.

Prerequisites:
  - doctl authenticated to a DigitalOcean account that can manage Spaces keys.
  - s3cmd, jq, python3, and curl.

Environment overrides:
  CITRUS_SPACES_CORS_CONFIG   default: infra/spaces/citrus-media-cors.json
  CITRUS_SPACES_KEY_NAME      default: ces-478-cors-<UTC timestamp>
  CITRUS_SPACES_KEY_GRANTS    default: bucket=;permission=fullaccess
  SPACES_REGION               default: nyc3
  SPACES_HOST_BASE            default: nyc3.digitaloceanspaces.com

Usage:
  bash scripts/apply_citrus_spaces_cors.sh
  bash scripts/apply_citrus_spaces_cors.sh /path/to/cors.json
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -gt 1 ]]; then
  usage >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${CITRUS_SPACES_CORS_CONFIG:-${REPO_ROOT}/infra/spaces/citrus-media-cors.json}"
if [[ $# -eq 1 ]]; then
  CONFIG_PATH="$1"
fi

SPACES_REGION="${SPACES_REGION:-nyc3}"
SPACES_HOST_BASE="${SPACES_HOST_BASE:-${SPACES_REGION}.digitaloceanspaces.com}"
SPACES_HOST_BUCKET="${SPACES_HOST_BUCKET:-%(bucket)s.${SPACES_HOST_BASE}}"
KEY_NAME="${CITRUS_SPACES_KEY_NAME:-ces-478-cors-$(date -u +%Y%m%d%H%M%S)}"
KEY_GRANTS="${CITRUS_SPACES_KEY_GRANTS:-bucket=;permission=fullaccess}"
BUCKETS=("citrus-media" "citrus-media-dev")
ORIGINS=(
  "https://citrus-grace.com"
  "https://www.citrus-grace.com"
  "https://dev.citrus-grace.com"
)

for command_name in doctl jq python3 s3cmd curl; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name is required." >&2
    exit 127
  fi
done

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "CORS config not found: $CONFIG_PATH" >&2
  exit 2
fi

ACCESS_KEY=""
SECRET_KEY=""
CORS_XML="$(mktemp)"

cleanup() {
  rm -f "$CORS_XML"
  if [[ -n "$ACCESS_KEY" ]]; then
    echo "Deleting temporary Spaces key ${KEY_NAME}"
    doctl spaces keys delete "$ACCESS_KEY" --interactive=false >/dev/null
  fi
}
trap cleanup EXIT

python3 - "$CONFIG_PATH" "$CORS_XML" <<'PY'
import json
import sys
import xml.etree.ElementTree as ET

config_path, output_path = sys.argv[1:3]
with open(config_path, encoding="utf-8") as config_file:
    policy = json.load(config_file)

root = ET.Element(
    "CORSConfiguration",
    {"xmlns": "http://s3.amazonaws.com/doc/2006-03-01/"},
)
for rule in policy["CORSRules"]:
    rule_element = ET.SubElement(root, "CORSRule")
    if rule.get("ID"):
        ET.SubElement(rule_element, "ID").text = rule["ID"]
    for origin in rule["AllowedOrigins"]:
        ET.SubElement(rule_element, "AllowedOrigin").text = origin
    for method in rule["AllowedMethods"]:
        ET.SubElement(rule_element, "AllowedMethod").text = method
    for header in rule.get("AllowedHeaders", []):
        ET.SubElement(rule_element, "AllowedHeader").text = header
    for header in rule.get("ExposeHeaders", []):
        ET.SubElement(rule_element, "ExposeHeader").text = header
    if "MaxAgeSeconds" in rule:
        ET.SubElement(rule_element, "MaxAgeSeconds").text = str(rule["MaxAgeSeconds"])

ET.indent(root, space="  ")
ET.ElementTree(root).write(output_path, encoding="utf-8", xml_declaration=True)
PY

echo "Creating temporary Spaces key ${KEY_NAME}"
create_json="$(
  doctl spaces keys create "$KEY_NAME" \
    --grants "$KEY_GRANTS" \
    --interactive=false \
    -o json
)"

ACCESS_KEY="$(jq -r 'if type == "array" then .[0] else . end | .access_key // empty' <<<"$create_json")"
SECRET_KEY="$(jq -r 'if type == "array" then .[0] else . end | .secret_key // empty' <<<"$create_json")"

if [[ -z "$ACCESS_KEY" || -z "$SECRET_KEY" ]]; then
  echo "Unable to parse temporary Spaces key response." >&2
  exit 1
fi

for bucket in "${BUCKETS[@]}"; do
  echo "Applying CORS to ${bucket}"
  s3cmd \
    --access_key="$ACCESS_KEY" \
    --secret_key="$SECRET_KEY" \
    --host="$SPACES_HOST_BASE" \
    --host-bucket="$SPACES_HOST_BUCKET" \
    --region="$SPACES_REGION" \
    setcors "$CORS_XML" "s3://${bucket}"
done

for bucket in "${BUCKETS[@]}"; do
  for origin in "${ORIGINS[@]}"; do
    echo "Verifying preflight for ${bucket} from ${origin}"
    curl -fsS -D - -o /dev/null -X OPTIONS "https://${bucket}.${SPACES_HOST_BASE}/" \
      -H "Origin: ${origin}" \
      -H "Access-Control-Request-Method: POST" \
      -H "Access-Control-Request-Headers: content-type,x-amz-algorithm,x-amz-credential,x-amz-date,x-amz-signature,policy" \
      | awk 'BEGIN { status_ok=0; allow_origin=0; allow_methods=0 }
          /^HTTP\// { print; if ($2 ~ /^2/) status_ok=1 }
          tolower($0) ~ /^access-control-allow-origin:/ { print; allow_origin=1 }
          tolower($0) ~ /^access-control-allow-methods:/ { print; if ($0 ~ /POST/) allow_methods=1 }
          END { exit !(status_ok && allow_origin && allow_methods) }'
  done
done

echo "CORS applied and verified for citrus-media and citrus-media-dev."
