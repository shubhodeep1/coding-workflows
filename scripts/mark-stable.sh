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
# Verify refs/heads/stable exists on the remote so a missing branch fails fast
# with actionable guidance instead of a generic 'couldn't find remote ref stable'.
# Distinguish rc=2 (truly missing) from other failures (auth/transport) so an
# operator hitting a network or permission issue doesn't get steered toward
# the wrong remediation.
set +e
LSREMOTE_OUT="$(git ls-remote --exit-code --heads origin stable 2>&1)"
LSREMOTE_RC=$?
set -e
if [ "${LSREMOTE_RC}" -eq 2 ]; then
	echo "error: refs/heads/stable does not exist on origin." >&2
	echo "       Create it first (e.g. via .github/workflows/promote-main-to-stable.yml" >&2
	echo "       or 'git push origin <commit>:refs/heads/stable') before running this script." >&2
	exit 3
elif [ "${LSREMOTE_RC}" -ne 0 ]; then
	echo "error: 'git ls-remote --heads origin stable' failed (rc=${LSREMOTE_RC}); this is not a missing-branch error." >&2
	echo "       Output: ${LSREMOTE_OUT}" >&2
	echo "       Check network connectivity, remote URL ('git remote -v'), and credentials." >&2
	exit "${LSREMOTE_RC}"
fi

# Refuse to retarget an already-published immutable release tag. The workflows
# create the tag without -f so an attempt to re-release a published version
# fails before any push; mirror that safety here so a rerun against an
# already-released VERSION_TAG doesn't silently retarget the local tag before
# the push fails.
if git ls-remote --exit-code --tags origin "refs/tags/${VERSION_TAG}" >/dev/null 2>&1; then
	echo "error: refs/tags/${VERSION_TAG} already exists on origin." >&2
	echo "       Immutable release tags must not be retargeted. If you need to" >&2
	echo "       re-release, choose a new VERSION_TAG (or delete the remote tag" >&2
	echo "       deliberately and rerun)." >&2
	exit 4
fi

git fetch origin stable
# Annotated tag (matches the workflow release path's `git tag -a "$VERSION" -m "Release $VERSION"`).
# No -f: the workflow form fails if the local tag already exists, so mirror
# that semantic — local rerun against an already-tagged VERSION_TAG should
# fail loudly instead of silently retargeting the local immutable tag.
git tag -a "${VERSION_TAG}" -m "Release ${VERSION_TAG}" origin/stable
git tag -f stable "${VERSION_TAG}"
git tag -f "${MAJOR}" "${VERSION_TAG}"
# Use refs/tags/ explicitly to disambiguate from any same-named branch refs.
git push origin "refs/tags/${VERSION_TAG}"
git push -f origin refs/tags/stable
git push -f origin "refs/tags/${MAJOR}"
echo "Done. ${VERSION_TAG} is now the stable release (${MAJOR} pointer updated)."
