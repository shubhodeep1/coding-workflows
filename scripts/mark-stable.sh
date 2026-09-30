#!/usr/bin/env bash
# Mark the current stable branch as a released version.
# Tags origin/stable as <version-tag> and moves the 'stable' and major-version
# (e.g. v1) pointers to it, matching the release path in
# .github/workflows/{mark-stable,test-and-mark-stable}.yml.
# Like those workflows, it first runs the protected-path release check
# (scripts/protected_path_authorization.py release, issue #4919), so it needs
# `gh` authenticated for the repository and a full (not shallow) clone; exit 6
# means the check refused or its checker could not run, exit 7 that
# origin/stable moved after the check. No tag moves in either case.
# Usage: ./scripts/mark-stable.sh v1.1.0
set -euo pipefail

VERSION_TAG="${1:?Usage: $0 <version-tag>  (e.g. v1.1.0)}"
if [[ ! "${VERSION_TAG}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
	echo "error: VERSION_TAG '${VERSION_TAG}' is not a valid version tag (expected vMAJOR.MINOR.PATCH, e.g. v1.1.0)" >&2
	exit 2
fi
MAJOR="$(echo "${VERSION_TAG}" | cut -d. -f1)"

# Annotated `git tag -a` requires committer identity. Pre-check so a fresh
# machine / runner without global git identity fails fast with actionable
# guidance instead of mid-run with "Committer identity unknown".
if ! git config --get user.email >/dev/null 2>&1 || ! git config --get user.name >/dev/null 2>&1; then
	echo "error: git user.name and user.email must be configured before running this script." >&2
	echo "       Annotated release tags (matching the workflow path) require committer identity. Set with:" >&2
	echo "         git config --global user.name 'Your Name'" >&2
	echo "         git config --global user.email 'you@example.com'" >&2
	exit 5
fi

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

# Determine whether the immutable VERSION_TAG already exists on origin and
# distinguish three cases:
#   * rc=0 (tag exists)        → may be a partial-publish recovery; resolve
#                                  by SHA comparison below.
#   * rc=2 (tag absent)        → normal release path, create the tag fresh.
#   * other non-zero           → real error (auth/transport); refuse to
#                                  proceed so the safety gate isn't bypassed.
set +e
TAG_LSREMOTE_OUT="$(git ls-remote --exit-code --tags origin "refs/tags/${VERSION_TAG}" 2>&1)"
TAG_LSREMOTE_RC=$?
set -e

SKIP_VERSION_TAG_CREATE=0
if [ "${TAG_LSREMOTE_RC}" -eq 0 ]; then
	# Tag already published. The workflow release path uses non-forced
	# `git tag -a`, so a rerun for the *same* version would normally hard-fail.
	# But the manual recovery scenario — first attempt pushed the immutable
	# tag, then `stable` / `${MAJOR}` push failed mid-flight — is recoverable:
	# rerunning should finish the moving pointers, not force a new VERSION_TAG.
	# Distinguish recovery (tag commit == origin/stable HEAD) from a real
	# conflict (tag points at a different commit).
	git fetch origin "refs/tags/${VERSION_TAG}:refs/tags/${VERSION_TAG}" stable
	EXISTING_TAG_COMMIT="$(git rev-parse "${VERSION_TAG}^{commit}")"
	STABLE_COMMIT="$(git rev-parse refs/remotes/origin/stable)"
	if [ "${EXISTING_TAG_COMMIT}" = "${STABLE_COMMIT}" ]; then
		echo "info: refs/tags/${VERSION_TAG} already exists on origin at the same commit"
		echo "      as origin/stable HEAD (${STABLE_COMMIT})."
		echo "      Treating this as a partial-publish recovery: skipping immutable tag"
		echo "      (re)creation, finishing the moving-pointer pushes."
		SKIP_VERSION_TAG_CREATE=1
	else
		echo "error: refs/tags/${VERSION_TAG} already exists on origin but points to a" >&2
		echo "       different commit than origin/stable HEAD." >&2
		echo "       Existing tag commit: ${EXISTING_TAG_COMMIT}" >&2
		echo "       origin/stable HEAD:  ${STABLE_COMMIT}" >&2
		echo "       Refusing to retarget the immutable tag. To re-release, choose a" >&2
		echo "       new VERSION_TAG (or delete the remote tag deliberately and rerun)." >&2
		exit 4
	fi
elif [ "${TAG_LSREMOTE_RC}" -ne 2 ]; then
	echo "error: 'git ls-remote --tags origin refs/tags/${VERSION_TAG}' failed (rc=${TAG_LSREMOTE_RC}); this is not a tag-not-found result." >&2
	echo "       Output: ${TAG_LSREMOTE_OUT}" >&2
	echo "       Refusing to proceed — a transient auth/network failure here would otherwise bypass the immutable-tag safety check." >&2
	exit "${TAG_LSREMOTE_RC}"
fi

# Issue #4919: the release check the workflows' `validate` job runs before the
# `stable` tag moves. Every protected-equivalent change since the previous
# `stable` tag (`.claude/**`, `workflow-templates/.claude/**`, the template
# divergence pins, the gate itself) must come from a merged PR the repository
# owner authorized at its merged head. It needs `gh` authenticated for the
# repository and the full history, and it fails closed (exit 6). The
# repository is GITHUB_REPOSITORY, else the last two parts of the origin URL.
RELEASE_CHECK_SCRIPT="$(dirname -- "${BASH_SOURCE[0]}")/protected_path_authorization.py"
RELEASE_CHECK_REPO="${GITHUB_REPOSITORY:-}"
if [ -z "${RELEASE_CHECK_REPO}" ] && [[ "$(git remote get-url origin 2>/dev/null || true)" =~ ([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?$ ]]; then
	RELEASE_CHECK_REPO="${BASH_REMATCH[1]}/${BASH_REMATCH[2]%.git}"
fi
if [ ! -f "${RELEASE_CHECK_SCRIPT}" ] || [ -z "${RELEASE_CHECK_REPO}" ]; then
	echo "error: cannot run the protected-path release check (issue #4919): ${RELEASE_CHECK_SCRIPT} is missing or the repository is unknown (set GITHUB_REPOSITORY=<owner>/<repo>). Refusing to release." >&2
	exit 6
fi
# The previous release is the check's base; a private ref keeps the local
# `stable` tag untouched. No remote `stable` tag (a first release) checks the
# whole history.
RELEASE_CHECK_BASE_REF="refs/mark-stable/previous-stable"
git update-ref -d "${RELEASE_CHECK_BASE_REF}" 2>/dev/null || true
set +e
PREVIOUS_STABLE_LSREMOTE_OUT="$(git ls-remote --exit-code --tags origin refs/tags/stable 2>&1)"
PREVIOUS_STABLE_LSREMOTE_RC=$?
set -e
if [ "${PREVIOUS_STABLE_LSREMOTE_RC}" -eq 0 ]; then
	git fetch --no-tags origin "+refs/tags/stable:${RELEASE_CHECK_BASE_REF}"
elif [ "${PREVIOUS_STABLE_LSREMOTE_RC}" -ne 2 ]; then
	echo "error: 'git ls-remote --tags origin refs/tags/stable' failed (rc=${PREVIOUS_STABLE_LSREMOTE_RC}): ${PREVIOUS_STABLE_LSREMOTE_OUT}" >&2
	echo "       Refusing to release without the protected-path release check's base." >&2
	exit "${PREVIOUS_STABLE_LSREMOTE_RC}"
fi
# The commit this release tags: the existing version tag on a recovery rerun,
# otherwise the current origin/stable.
if [ "${SKIP_VERSION_TAG_CREATE}" -eq 1 ]; then
	RELEASE_CHECK_HEAD="${EXISTING_TAG_COMMIT}"
else
	git fetch --no-tags origin "+refs/heads/stable:refs/remotes/origin/stable"
	RELEASE_CHECK_HEAD="$(git rev-parse --verify "refs/remotes/origin/stable^{commit}")"
fi
set +e
PYTHONDONTWRITEBYTECODE=1 python3 "${RELEASE_CHECK_SCRIPT}" release --repo "${RELEASE_CHECK_REPO}" --base "${RELEASE_CHECK_BASE_REF}" --head "${RELEASE_CHECK_HEAD}"
RELEASE_CHECK_RC=$?
set -e
if [ "${RELEASE_CHECK_RC}" -ne 0 ]; then
	echo "error: protected-path authorization failed (exit ${RELEASE_CHECK_RC}, issue #4919). For each blocked PR above, the repository owner posts '/authorize-protected-paths <merged head sha>' on it, then reruns this script. No tag was moved." >&2
	exit 6
fi

if [ "${SKIP_VERSION_TAG_CREATE}" -eq 0 ]; then
	git fetch origin stable
	# Annotated tag (matches the workflow release path's `git tag -a "$VERSION" -m "Release $VERSION"`).
	# No -f: the workflow form fails if the local tag already exists, so mirror
	# that semantic — local rerun against an already-tagged VERSION_TAG should
	# fail loudly instead of silently retargeting the local immutable tag.
	git tag -a "${VERSION_TAG}" -m "Release ${VERSION_TAG}" origin/stable
fi
# Release exactly the commit the protected-path check passed.
if [ "$(git rev-parse --verify "${VERSION_TAG}^{commit}")" != "${RELEASE_CHECK_HEAD}" ]; then
	echo "error: ${VERSION_TAG} points at $(git rev-parse --verify "${VERSION_TAG}^{commit}"), not at ${RELEASE_CHECK_HEAD}, the commit the protected-path release check passed (origin/stable moved). Delete the local tag ('git tag -d ${VERSION_TAG}') and rerun. No tag was pushed." >&2
	exit 7
fi
git tag -f stable "${VERSION_TAG}"
git tag -f "${MAJOR}" "${VERSION_TAG}"
# Use refs/tags/ explicitly to disambiguate from any same-named branch refs.
if [ "${SKIP_VERSION_TAG_CREATE}" -eq 0 ]; then
	git push origin "refs/tags/${VERSION_TAG}"
fi
git push -f origin refs/tags/stable
git push -f origin "refs/tags/${MAJOR}"
echo "Done. ${VERSION_TAG} is now the stable release (${MAJOR} pointer updated)."
