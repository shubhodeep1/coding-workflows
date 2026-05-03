#!/usr/bin/env bash
# Mark the current stable branch as a released version.
# Tags origin/stable as <version-tag> and moves the 'stable' and major-version
# (e.g. v1) pointers to it, matching the release path in
# .github/workflows/{mark-stable,test-and-mark-stable}.yml.
# Usage: ./scripts/mark-stable.sh v1.1.0
set -euo pipefail

VERSION_TAG="${1:?Usage: $0 <version-tag>  (e.g. v1.1.0)}"
MAJOR="$(echo "${VERSION_TAG}" | cut -d. -f1)"

echo "Tagging origin/stable as ${VERSION_TAG} and updating stable + ${MAJOR} pointers..."
git fetch origin stable
git tag -f "${VERSION_TAG}" origin/stable
git tag -f stable "${VERSION_TAG}"
git tag -f "${MAJOR}" "${VERSION_TAG}"
# Use refs/tags/ explicitly to disambiguate from any same-named branch refs.
git push origin "refs/tags/${VERSION_TAG}"
git push -f origin refs/tags/stable
git push -f origin "refs/tags/${MAJOR}"
echo "Done. ${VERSION_TAG} is now the stable release (${MAJOR} pointer updated)."
