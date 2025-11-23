#!/usr/bin/env bash
set -euo pipefail

BOT="${1:-}"
IMAGE="${2:-}"
DIGEST="${3:-}"
RAW_TAG="${4:-}"
PAUSED="${5:-false}"

if [[ -z "$BOT" || -z "$IMAGE" || -z "$DIGEST" ]]; then
  echo "usage: patch-image.sh <bot> <image> <sha256:digest> [tag] [paused]" >&2
  exit 1
fi

if [[ "$DIGEST" != sha256:* ]]; then
  echo "digest must include sha256: prefix" >&2
  exit 1
fi

if ! command -v yq >/dev/null 2>&1; then
  echo "yq not found on PATH" >&2
  exit 1
fi

if [[ ! -f bots.yaml ]]; then
  echo "bots.yaml not found; run from repo root" >&2
  exit 1
fi

VALUES_FILE=$(yq -r ".bots[\"$BOT\"].valuesFile // \"\"" bots.yaml)
if [[ -z "$VALUES_FILE" || "$VALUES_FILE" == "null" ]]; then
  echo "Bot '$BOT' is missing valuesFile in bots.yaml" >&2
  exit 1
fi

if [[ ! -f "$VALUES_FILE" ]]; then
  echo "Values file '$VALUES_FILE' does not exist" >&2
  exit 1
fi

TAG_VALUE=""
if [[ -n "$RAW_TAG" && "$RAW_TAG" != sha256:* ]]; then
  TAG_VALUE="$RAW_TAG"
fi

REPLICA_COUNT=1
if [[ "${PAUSED,,}" =~ ^(true|pause)$ ]]; then
  REPLICA_COUNT=0
fi

export IMAGE DIGEST
yq -i '.image.repository = env(IMAGE)' "$VALUES_FILE"
if [[ -n "$TAG_VALUE" ]]; then
  TAG="$TAG_VALUE" yq -i '.image.tag = env(TAG)' "$VALUES_FILE"
else
  yq -i '.image.tag = ""' "$VALUES_FILE"
fi

yq -i '.image.digest = env(DIGEST)' "$VALUES_FILE"
yq -i ".replicaCount = ${REPLICA_COUNT}" "$VALUES_FILE"

echo "Pinned $BOT -> ${IMAGE}@${DIGEST} in $VALUES_FILE (replicas=${REPLICA_COUNT})"
