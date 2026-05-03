#!/usr/bin/env bash
# Mark the current main branch as stable.
# Usage: ./scripts/mark-stable.sh v1.1.0
set -euo pipefail

VERSION_TAG="${1:?Usage: $0 <version-tag>  (e.g. v1.1.0)}"
MAJOR="$(echo "${VERSION_TAG}" | cut -d. -f1)"

echo "Tagging origin/main as ${VERSION_TAG} and updating stable + ${MAJOR} pointers..."
git fetch origin main
git tag -f "${VERSION_TAG}" origin/main
git tag -f stable "${VERSION_TAG}"
git tag -f "${MAJOR}" "${VERSION_TAG}"
# Use refs/tags/ explicitly to disambiguate from any same-named branch refs.
git push origin "refs/tags/${VERSION_TAG}"
git push -f origin refs/tags/stable
git push -f origin "refs/tags/${MAJOR}"
echo "Done. ${VERSION_TAG} is now the stable release (${MAJOR} pointer updated)."
