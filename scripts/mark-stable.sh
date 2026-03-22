#!/usr/bin/env bash
# Mark the current main branch as stable.
# Usage: ./scripts/mark-stable.sh v1.1.0
set -euo pipefail

VERSION_TAG="${1:?Usage: $0 <version-tag>  (e.g. v1.1.0)}"

if ! [[ "${VERSION_TAG}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Error: version tag must match vMAJOR.MINOR.PATCH (e.g. v1.1.0)." >&2
  exit 1
fi

echo "Tagging origin/main as ${VERSION_TAG} and updating stable pointer..."
git fetch origin main
if git ls-remote --exit-code --tags origin "refs/tags/${VERSION_TAG}" >/dev/null 2>&1; then
  echo "Error: ${VERSION_TAG} already exists on origin and immutable tags must not be moved." >&2
  exit 1
fi
git tag "${VERSION_TAG}" origin/main
git tag -f stable "${VERSION_TAG}"
git push origin "${VERSION_TAG}"
git push -f origin stable
echo "Done. ${VERSION_TAG} is now the stable release."
