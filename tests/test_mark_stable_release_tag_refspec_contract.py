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


def test_script_releases_from_stable_branch_for_workflow_parity() -> None:
	text = _read(SCRIPT)
	assert "git fetch origin stable" in text, (
		"scripts/mark-stable.sh: must fetch origin/stable so manual releases "
		"match the workflow path (which releases from the stable branch)"
	)
	assert 'git tag -f "${VERSION_TAG}" origin/stable' in text, (
		"scripts/mark-stable.sh: VERSION_TAG must be created from origin/stable, "
		"not origin/main, to match the workflow release path"
	)
	assert "git fetch origin main" not in text, (
		"scripts/mark-stable.sh: must not fetch origin/main — that would tag a "
		"main commit that hasn't been validated on the stable branch"
	)


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
