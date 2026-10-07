#!/usr/bin/env python3
"""Behaviour: the autofix round count only resets on a [judge-fix] commit.

scripts/review_autofix_step_count_iterations.sh (the "Count autofix
iterations" step, id retrigger_guard) walks the PR's own first-parent
history back to the base branch. [ai-autofix] / [claude-autofix] commits
count, a [judge-fix] commit ends the walk, and every other commit is skipped
without resetting the count. Each case below builds a scratch origin with a
`main` base and a PR branch, runs the real script, and reads its
GITHUB_OUTPUT.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_count_iterations.sh"


def _git(cwd: Path, *args: str) -> str:
	env = {
		"PATH": os.environ.get("PATH", ""),
		"HOME": str(cwd),
		"GIT_AUTHOR_NAME": "test",
		"GIT_AUTHOR_EMAIL": "test@example.invalid",
		"GIT_COMMITTER_NAME": "test",
		"GIT_COMMITTER_EMAIL": "test@example.invalid",
		"GIT_CONFIG_NOSYSTEM": "1",
	}
	return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def _commit(repo: Path, subject: str) -> None:
	_git(repo, "commit", "-q", "--allow-empty", "-m", subject)


def _setup(tmp_path: Path, pr_subjects: list[str], *, merge_main_after: int | None = None) -> Path:
	"""Return a clone checked out on the PR branch `feature`."""
	origin = tmp_path / "origin.git"
	work = tmp_path / "work"
	_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
	_git(tmp_path, "clone", "-q", str(origin), str(work))
	_commit(work, "base")
	# Base-branch history that must never be counted.
	_commit(work, "[ai-autofix] apply PR fixes (an older PR, already on main)")
	_git(work, "push", "-q", "origin", "HEAD:main")
	_git(work, "checkout", "-q", "-b", "feature")
	for idx, subject in enumerate(pr_subjects):
		_commit(work, subject)
		if merge_main_after is not None and idx == merge_main_after:
			_git(work, "checkout", "-q", "main")
			_commit(work, "unrelated main commit")
			_commit(work, "[ai-autofix] apply PR fixes (another PR on main)")
			_git(work, "push", "-q", "origin", "main")
			_git(work, "checkout", "-q", "feature")
			_git(work, "merge", "-q", "--no-ff", "-m", "Merge branch 'main' into feature", "main")
	_git(work, "push", "-q", "origin", "feature")
	return work


def _run(work: Path, tmp_path: Path, *, base_ref: str = "main", max_iterations: int = 5) -> dict[str, str]:
	meta = tmp_path / "pr_meta.json"
	meta.write_text(json.dumps({"headRefName": "feature", "baseRefName": base_ref}), encoding="utf-8")
	output = tmp_path / "github_output"
	output.write_text("", encoding="utf-8")
	env = {
		"PATH": os.environ.get("PATH", ""),
		"HOME": str(tmp_path),
		"GIT_CONFIG_NOSYSTEM": "1",
		"GITHUB_OUTPUT": str(output),
		"PR_META_FILE": str(meta),
		"TARGET_BRANCH": "feature",
		"MAX_AUTOFIX_ITERATIONS": str(max_iterations),
		"FORCE_RB_JUDGE": "false",
		"ORCH_PR_AUTOFIX_FLOW_ENABLED": "true",
		"ORCH_INTEGRATION_BRANCH_PATTERN": "^orchestrator/project-",
	}
	result = subprocess.run(["bash", str(SCRIPT)], cwd=work, env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stdout + result.stderr
	values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if "=" in line)
	values["_stdout"] = result.stdout
	return values


def test_other_commits_are_skipped_not_reset(tmp_path: Path) -> None:
	work = _setup(tmp_path, [
		"feat: the change",
		"[ai-autofix] apply PR fixes",
		"[ai-merge-resolve] resolve merge conflicts",
		"[ai-autofix] apply PR fixes",
		"Keep the guard files at main's version",
		"AI implementation for issue #6221 (#6223)",
		"[claude-autofix] review round 2",
	])
	out = _run(work, tmp_path)
	assert out["autofix_iteration"] == "3"
	assert out["max_iterations_reached"] == "false"
	assert "AUTOFIX_COUNT_MODE=since_judge_fix" in out["_stdout"]


def test_judge_fix_opens_a_fresh_budget(tmp_path: Path) -> None:
	work = _setup(tmp_path, ["feat: x"] + ["[ai-autofix] apply PR fixes"] * 5 + [
		"[judge-fix] address review-blocked issues",
		"[ai-autofix] apply PR fixes",
		"[ai-merge-resolve] resolve merge conflicts",
		"[ai-autofix] apply PR fixes",
	])
	out = _run(work, tmp_path)
	assert out["autofix_iteration"] == "2"
	assert out["max_iterations_reached"] == "false"
	assert out["judge_fix_count"] == "1"


def test_cap_is_reached_across_interleaved_commits(tmp_path: Path) -> None:
	subjects = ["feat: x"]
	for _ in range(5):
		subjects += ["[ai-autofix] apply PR fixes", "[ai-merge-resolve] resolve merge conflicts"]
	work = _setup(tmp_path, subjects)
	out = _run(work, tmp_path)
	assert out["autofix_iteration"] == "5"
	assert out["max_iterations_reached"] == "true"


def test_merging_the_base_branch_does_not_count_base_history(tmp_path: Path) -> None:
	work = _setup(tmp_path, [
		"feat: x",
		"[ai-autofix] apply PR fixes",
		"[ai-autofix] apply PR fixes",
	], merge_main_after=1)
	out = _run(work, tmp_path)
	# Two PR autofix rounds; main's [ai-autofix] commits (before the branch
	# point and on the merged side) are not on the PR's first-parent history.
	assert out["autofix_iteration"] == "2"


@pytest.mark.parametrize("base_ref", ["", "does-not-exist", "bad..ref"])
def test_unusable_base_falls_back_to_consecutive_count(tmp_path: Path, base_ref: str) -> None:
	work = _setup(tmp_path, [
		"feat: x",
		"[ai-autofix] apply PR fixes",
		"[ai-merge-resolve] resolve merge conflicts",
		"[ai-autofix] apply PR fixes",
		"[ai-autofix] apply PR fixes",
	])
	out = _run(work, tmp_path, base_ref=base_ref)
	assert out["autofix_iteration"] == "2"
	assert "AUTOFIX_COUNT_MODE=legacy_consecutive" in out["_stdout"]
	assert "::warning::" in out["_stdout"]
