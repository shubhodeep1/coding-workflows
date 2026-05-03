#!/usr/bin/env bash
# Mark the current stable branch as a released version.
# Tags origin/stable as <version-tag> and moves the 'stable' and major-version
# (e.g. v1) pointers to it, matching the release path in
# .github/workflows/{mark-stable,test-and-mark-stable}.yml.
# Usage: ./scripts/mark-stable.sh v1.1.0
set -euo pipefail

VERSION_TAG="${1:?Usage: $0 <version-tag>  (e.g. v1.1.0)}"
if [[ ! "${VERSION_TAG}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
	echo "error: VERSION_TAG '${VERSION_TAG}' is not a valid version tag (expected vMAJOR.MINOR.PATCH, e.g. v1.1.0)" >&2
	exit 2
fi
MAJOR="$(echo "${VERSION_TAG}" | cut -d. -f1)"

echo "Tagging origin/stable as ${VERSION_TAG} and updating stable + ${MAJOR} pointers..."
git fetch origin stable
# Annotated tag (matches the workflow release path's `git tag -a "$VERSION" -m "Release $VERSION"`).
git tag -fa "${VERSION_TAG}" -m "Release ${VERSION_TAG}" origin/stable
git tag -f stable "${VERSION_TAG}"
git tag -f "${MAJOR}" "${VERSION_TAG}"
# Use refs/tags/ explicitly to disambiguate from any same-named branch refs.
git push origin "refs/tags/${VERSION_TAG}"
git push -f origin refs/tags/stable
git push -f origin "refs/tags/${MAJOR}"
echo "Done. ${VERSION_TAG} is now the stable release (${MAJOR} pointer updated)."
