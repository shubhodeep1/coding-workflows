#!/usr/bin/env python3
"""Regression guard: release jobs must push tags via fully qualified refs/tags/ refspecs.

Bare refspecs like `git push -f origin stable` fail with
`error: src refspec stable matches more than one` whenever the runner has both
a refs/heads/<name> branch and a refs/tags/<name> tag locally (which the
release jobs trigger by checking out the `stable` branch and then creating a
`stable` tag). See PR #2031 / job-logs.txt L1500-1502 for the live failure.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = (
	REPO_ROOT / ".github" / "workflows" / "mark-stable.yml",
	REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml",
)
SCRIPT = REPO_ROOT / "scripts" / "mark-stable.sh"


def _read(p: Path) -> str:
	return p.read_text(encoding="utf-8")


def test_workflows_push_stable_tag_via_refs_tags() -> None:
	for wf in WORKFLOWS:
		text = _read(wf)
		assert "git push -f origin refs/tags/stable" in text, (
			f"{wf.name}: stable tag push must use fully qualified refs/tags/stable"
		)
		assert not re.search(r"^\s*git push -f origin stable\s*$", text, re.MULTILINE), (
			f"{wf.name}: bare 'git push -f origin stable' would re-introduce the "
			f"'src refspec stable matches more than one' regression"
		)


def test_workflows_push_major_tag_via_refs_tags() -> None:
	for wf in WORKFLOWS:
		text = _read(wf)
		assert 'git push -f origin "refs/tags/$MAJOR"' in text, (
			f"{wf.name}: major-version tag push must use fully qualified refs/tags/$MAJOR"
		)
		assert not re.search(r'^\s*git push -f origin "\$MAJOR"\s*$', text, re.MULTILINE), (
			f"{wf.name}: bare 'git push -f origin \"$MAJOR\"' would re-introduce the regression"
		)


def test_workflows_push_immutable_version_via_refs_tags() -> None:
	for wf in WORKFLOWS:
		text = _read(wf)
		assert 'git push origin "refs/tags/$VERSION"' in text, (
			f"{wf.name}: immutable version tag push must use fully qualified refs/tags/$VERSION"
		)
		assert not re.search(r'^\s*git push origin "\$VERSION"\s*$', text, re.MULTILINE), (
			f"{wf.name}: bare 'git push origin \"$VERSION\"' would re-introduce the regression"
		)


def test_script_pushes_all_three_tags_via_refs_tags() -> None:
	text = _read(SCRIPT)
	assert 'git push origin "refs/tags/${VERSION_TAG}"' in text, (
		"scripts/mark-stable.sh: VERSION_TAG push must use refs/tags/${VERSION_TAG}"
	)
	assert "git push -f origin refs/tags/stable" in text, (
		"scripts/mark-stable.sh: stable tag push must use refs/tags/stable"
	)
	assert 'git push -f origin "refs/tags/${MAJOR}"' in text, (
		"scripts/mark-stable.sh: major-version push must use refs/tags/${MAJOR}"
	)
	assert not re.search(r'^\s*git push origin "\$\{?VERSION_TAG\}?"\s*$', text, re.MULTILINE), (
		"scripts/mark-stable.sh: bare 'git push origin \"${VERSION_TAG}\"' would re-introduce the regression"
	)
	assert not re.search(r"^\s*git push -f origin stable\s*$", text, re.MULTILINE), (
		"scripts/mark-stable.sh: bare 'git push -f origin stable' would re-introduce the regression"
	)
	assert not re.search(r'^\s*git push -f origin "\$\{?MAJOR\}?"\s*$', text, re.MULTILINE), (
		"scripts/mark-stable.sh: bare 'git push -f origin \"${MAJOR}\"' would re-introduce the regression"
	)


def test_script_creates_annotated_release_tag_like_workflows() -> None:
	text = _read(SCRIPT)
	assert 'git tag -a "${VERSION_TAG}" -m "Release ${VERSION_TAG}" origin/stable' in text, (
		"scripts/mark-stable.sh: VERSION_TAG must be an annotated tag (-a/-m), "
		"matching the workflows' `git tag -a \"$VERSION\" -m \"Release $VERSION\"` "
		"so manual and automated release paths produce identical tag metadata"
	)
	# The workflows do not pass -f when creating the immutable VERSION tag, so a
	# rerun against an already-tagged version fails loudly. Pin the same safety
	# semantic for the script: forcing here would silently retarget the local
	# immutable tag before the push fails.
	assert not re.search(
		r'^\s*git tag -fa? "\$\{?VERSION_TAG\}?"', text, re.MULTILINE
	), (
		"scripts/mark-stable.sh: must not use 'git tag -f' / 'git tag -fa' for "
		"the immutable VERSION_TAG — that diverges from the workflow safety "
		"model (which fails if the tag already exists locally)"
	)
	assert not re.search(
		r'^\s*git tag -f "\$\{?VERSION_TAG\}?" origin/stable\s*$', text, re.MULTILINE
	), (
		"scripts/mark-stable.sh: lightweight 'git tag -f \"${VERSION_TAG}\" origin/stable' "
		"would re-introduce metadata divergence with the workflow release path"
	)


def test_script_releases_from_stable_branch_for_workflow_parity() -> None:
	text = _read(SCRIPT)
	assert "git fetch origin stable" in text, (
		"scripts/mark-stable.sh: must fetch origin/stable so manual releases "
		"match the workflow path (which releases from the stable branch)"
	)
	assert "origin/stable" in text, (
		"scripts/mark-stable.sh: VERSION_TAG must be created from origin/stable, "
		"matching the workflow release path"
	)
	# Two precise regression patterns to forbid:
	#   1. `git tag ... origin/main`    — re-introduces tagging from main
	#   2. `git fetch origin main`      — re-introduces fetching main
	# Use targeted regexes (not a broad substring match) so harmless future
	# strings or comments mentioning `origin/main` don't trip the guard.
	assert not re.search(r"^\s*git\s+tag\b[^\n]*\borigin/main\b", text, re.MULTILINE), (
		"scripts/mark-stable.sh: must not tag from origin/main — that would tag a "
		"main commit that hasn't been validated on the stable branch"
	)
	assert not re.search(r"^\s*git\s+fetch\s+origin\s+main\b", text, re.MULTILINE), (
		"scripts/mark-stable.sh: must not fetch origin/main — manual releases "
		"must match the workflow path which fetches and releases from stable"
	)


def test_script_distinguishes_missing_branch_from_other_lsremote_failures() -> None:
	text = _read(SCRIPT)
	# rc=2 from `git ls-remote --exit-code` means "no matching ref"; any other
	# non-zero is a real failure (auth/network/transport). Collapsing both into
	# "create the branch first" steers operators toward the wrong remediation
	# on auth/transport errors.
	assert re.search(r"LSREMOTE_RC.*-eq\s+2", text), (
		"scripts/mark-stable.sh: must distinguish git ls-remote rc=2 (missing) "
		"from other failures (auth/transport) so misleading 'create the branch' "
		"guidance is only emitted for the actual missing-branch case"
	)


def test_script_handles_already_published_version_tag() -> None:
	text = _read(SCRIPT)
	# 1. Existence check: must run before any tag creation.
	assert 'git ls-remote --exit-code --tags origin "refs/tags/${VERSION_TAG}"' in text, (
		"scripts/mark-stable.sh: must check that refs/tags/${VERSION_TAG} is "
		"unused on origin before creating any tags, mirroring the workflow's "
		"non-forced `git tag -a` safety semantic"
	)
	# 2. Recovery vs conflict: when the tag already exists, the script must
	#    compare its commit against origin/stable HEAD instead of
	#    unconditionally hard-failing. A rerun after a partial publish (tag
	#    pushed, moving pointers failed) is a legitimate recovery and must
	#    finish the moving-tag pushes against the existing immutable tag.
	assert '${VERSION_TAG}^{commit}' in text, (
		"scripts/mark-stable.sh: must resolve the existing tag's commit via "
		"`git rev-parse \"${VERSION_TAG}^{commit}\"` so partial-publish recovery "
		"can be distinguished from a real retarget conflict"
	)
	assert "refs/remotes/origin/stable" in text, (
		"scripts/mark-stable.sh: must compare against `refs/remotes/origin/stable` "
		"to detect the partial-publish recovery case"
	)
	assert "SKIP_VERSION_TAG_CREATE" in text, (
		"scripts/mark-stable.sh: must guard the tag-create + tag-push steps with a "
		"recovery flag (e.g. SKIP_VERSION_TAG_CREATE) so a partial-publish rerun "
		"finishes the moving-tag pushes without recreating or re-pushing the "
		"immutable VERSION_TAG"
	)


def test_script_validates_git_identity_for_annotated_tag() -> None:
	text = _read(SCRIPT)
	# `git tag -a` requires committer identity; the script must pre-check it
	# instead of failing mid-run with "Committer identity unknown".
	assert "git config --get user.email" in text, (
		"scripts/mark-stable.sh: must pre-check `git config --get user.email` "
		"so a missing identity fails fast with actionable guidance instead of "
		"mid-run with 'Committer identity unknown'"
	)
	assert "git config --get user.name" in text, (
		"scripts/mark-stable.sh: must pre-check `git config --get user.name` "
		"alongside user.email — both are required for annotated tags"
	)


def main() -> None:
	# Failures surface via uncaught AssertionError → Python's default non-zero
	# exit; on full success we just print and return None.
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")


if __name__ == "__main__":
	main()
