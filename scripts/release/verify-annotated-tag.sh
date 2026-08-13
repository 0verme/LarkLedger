#!/usr/bin/env bash
# Verify that a release tag is an annotated tag (project release convention).
#
# Usage: verify-annotated-tag.sh <tag>
#
# Requires a full checkout (fetch-depth: 0) so the annotated tag object is
# present locally. Exits 0 for annotated tags, 1 otherwise.
set -euo pipefail

TAG="${1:-}"
if [ -z "$TAG" ]; then
  echo "usage: verify-annotated-tag.sh <tag>" >&2
  exit 2
fi

if ! git cat-file -e "refs/tags/${TAG}^{tag}" 2>/dev/null; then
  echo "::error::tag '${TAG}' is not an annotated tag (expected object type 'tag');" >&2
  echo "::error::release workflow requires annotated tags (git tag -a -m ...). Use a lightweight tag only for local throwaway testing." >&2
  exit 1
fi

echo "tag '${TAG}' is an annotated tag: OK"
