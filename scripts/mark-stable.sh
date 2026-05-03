#!/usr/bin/env bash
# Mark the current main branch as stable.
# Usage: ./scripts/mark-stable.sh v1.1.0
set -euo pipefail

VERSION_TAG="${1:?Usage: $0 <version-tag>  (e.g. v1.1.0)}"

echo "Tagging origin/main as ${VERSION_TAG} and updating stable pointer..."
git fetch origin main
git tag -f "${VERSION_TAG}" origin/main
git tag -f stable "${VERSION_TAG}"
git push origin "${VERSION_TAG}"
# Use refs/tags/ to disambiguate from a refs/heads/stable branch when both exist.
git push -f origin refs/tags/stable
echo "Done. ${VERSION_TAG} is now the stable release."
