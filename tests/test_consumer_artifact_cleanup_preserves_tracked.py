#!/usr/bin/env python3
"""Consumer-repo artifact cleanup must never delete a repo-TRACKED path.

Four sites clean workflow-staged artifacts out of a consumer repo's working
tree before committing: the merge-conflict resolver and its prepare step,
the review-blocked judge, and the implement commit helper. Each cleanup
feeds a later `git add -u` / `git add -A` staging pass, so a working-tree
deletion is recorded in the commit as a real deletion.

The orchestrator poller's own inline review-blocked judge used to be a
fifth site (its old "combined mode" ran codex and committed fixes directly,
so it needed the same `_orch_cleanup_artifact` guard as the standalone
`scripts/review_rb_judge.sh`). Combined mode was retired: the poller's judge
is now decision-only, documented in `scripts/orchestrate_poll_process.sh` as
never receiving a writable checkout and never applying or pushing changes
directly, and its `fix` action only pushes a no-tree-change boundary commit
before dispatching the real edit to `review_autofix.yml` (which runs the
already-guarded `scripts/review_rb_judge.sh`). With no local write left to
protect, `_orch_cleanup_artifact` was removed rather than kept as dead code;
`test_poller_restores_bootstrap_overwrite` below pins the invariant that
replaced it.

The artifact names are not private to the pipeline. A consumer repo
legitimately owns a root-level `agents.md` — CLAUDE.md §22.C and §24.F
require DigitalOcean and Cloudflare resource IDs to live there — and that
name collides exactly with the artifact the workflow stages at the same
path.

Observed on shubhodeep1/binance-blessings PR #255: merge-resolve commit
b974f8b deleted the tracked `agents.md` holding the production App Platform
ID even though BOTH merge parents still carried it. The next review round's
editor tried to restore the file, the restore was wiped as a "newly created
file", and the run dead-ended in a false EDITOR_CHANGES_LOST retry loop
(AI Review run 34099352704). Same bug class as the ~10,700-line deletion in
PRs #917/#931.

Each test executes the real cleanup block extracted from the script against
a throwaway git repo, and asserts a tracked path survives while an
untracked artifact of the same name is still removed.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

# (script path, cleanup loop variable, tracked path, untracked path).
CLEANUP_SITES = [
	("scripts/review_conflict_resolve.sh", "_rs_cleanup_artifact", "agents.md", "pre_assembled_static.txt"),
	("scripts/review_rb_judge.sh", "_rb_cleanup_artifact", "agents.md", "pre_assembled_static.txt"),
	(
		"scripts/review_conflict_prepare.sh",
		"_conflict_prepare_cleanup_artifact",
		"scripts/ai_memory.py",
		"scripts/ai_memory_lib.py",
	),
]

# Artifact name that collides with a documented consumer-owned path.
COLLIDING_ARTIFACT = "agents.md"
UNTRACKED_ARTIFACT = "pre_assembled_static.txt"


def _extract_cleanup_block(script_rel: str, loop_var: str) -> str:
	"""Return the cleanup for-loop verbatim, from `for <var>` to `unset <var>`."""
	text = (REPO_ROOT / script_rel).read_text(encoding="utf-8")
	match = re.search(
		rf"^[ \t]*for {re.escape(loop_var)} in\b.*?^[ \t]*unset {re.escape(loop_var)}[ \t]*$",
		text,
		flags=re.S | re.M,
	)
	assert match, f"{script_rel}: cleanup loop over {loop_var} not found"
	block = match.group(0)
	# Strip the shared leading indentation so the fragment runs standalone.
	populated = [line for line in block.splitlines() if line.strip()]
	indent = min(len(line) - len(line.lstrip()) for line in populated)
	return "\n".join(line[indent:] if len(line) >= indent else line for line in block.splitlines())


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(
		["git", *args],
		cwd=cwd,
		check=True,
		capture_output=True,
		env=_temp_repo_env(),
		text=True,
	).stdout


def _temp_repo_env() -> dict[str, str]:
	"""Return an environment that lets Git discover the throwaway repo."""
	temp_repo_env = os.environ.copy()
	for git_env_name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "BASH_ENV", "ENV"):
		temp_repo_env.pop(git_env_name, None)
	return temp_repo_env


def _make_consumer_repo(tmp: Path, tracked_artifact: str = COLLIDING_ARTIFACT) -> Path:
	"""A minimal consumer repo that owns one tracked artifact path."""
	_git(tmp, "init", "-q", "-b", "main")
	_git(tmp, "config", "user.email", "test@example.com")
	_git(tmp, "config", "user.name", "test")
	tracked_path = tmp / tracked_artifact
	tracked_path.parent.mkdir(parents=True, exist_ok=True)
	tracked_path.write_text(
		"# agents.md\n\n## DigitalOcean resources\n\n| binance-blessings | app | id |\n",
		encoding="utf-8",
	)
	_git(tmp, "add", tracked_artifact)
	_git(tmp, "commit", "-qm", f"consumer repo owns {tracked_artifact}")
	return tmp


def test_cleanup_preserves_tracked_agents_md() -> None:
	"""The tracked agents.md survives cleanup and no deletion gets staged."""
	for script_rel, loop_var, tracked_artifact, untracked_artifact in CLEANUP_SITES:
		block = _extract_cleanup_block(script_rel, loop_var)
		with tempfile.TemporaryDirectory(prefix="artifact-cleanup-") as raw_tmp:
			repo = _make_consumer_repo(Path(raw_tmp), tracked_artifact)
			# The workflow also stages an untracked artifact at the repo root.
			untracked_path = repo / untracked_artifact
			untracked_path.parent.mkdir(parents=True, exist_ok=True)
			untracked_path.write_text("staged artifact\n", encoding="utf-8")

			subprocess.run(
				["bash", "-euo", "pipefail", "-c", block],
				cwd=repo,
				check=True,
				capture_output=True,
				env=_temp_repo_env(),
				text=True,
			)

			assert (repo / tracked_artifact).exists(), (
				f"{script_rel}: cleanup deleted the repo-tracked {tracked_artifact}"
			)
			assert not (repo / untracked_artifact).exists(), (
				f"{script_rel}: cleanup failed to remove the untracked {untracked_artifact}"
			)

			# The staging pass that follows each cleanup block must not record
			# a deletion — that is what turned the working-tree rm into a
			# committed data loss on PR #255.
			if script_rel == "scripts/orchestrate_poll_process.sh":
				_git(repo, "add", "-A")
			else:
				_git(repo, "add", "-u")
			staged = _git(repo, "diff", "--cached", "--name-status").strip()
			assert staged == "", f"{script_rel}: cleanup staged {staged!r} for commit"


def test_cleanup_block_guards_every_path() -> None:
	"""Each cleanup block routes every removal through the tracked-path guard."""
	for script_rel, loop_var, _tracked_artifact, _untracked_artifact in CLEANUP_SITES:
		block = _extract_cleanup_block(script_rel, loop_var)
		assert "git ls-files --error-unmatch" in block, (
			f"{script_rel}: cleanup loop lost its tracked-path guard"
		)
		removals = re.findall(r"^[ \t]*rm[ \t]+-[a-zA-Z]+[ \t]", block, flags=re.M)
		assert len(removals) == 1, (
			f"{script_rel}: expected exactly 1 guarded rm, found {len(removals)}"
		)


def test_no_unguarded_agents_md_removal_remains() -> None:
	"""No bare `rm ... agents.md` survives outside the guarded loop."""
	for script_rel, loop_var, _tracked_artifact, _untracked_artifact in CLEANUP_SITES:
		text = (REPO_ROOT / script_rel).read_text(encoding="utf-8")
		block = _extract_cleanup_block(script_rel, loop_var)
		for line in text.splitlines():
			stripped = line.strip()
			if not stripped.startswith("rm ") or COLLIDING_ARTIFACT not in stripped:
				continue
			assert stripped in block, (
				f"{script_rel}: unguarded removal of {COLLIDING_ARTIFACT}: {stripped!r}"
			)


def test_poller_restores_bootstrap_overwrite() -> None:
	"""The invariant that replaced the poller's `_orch_cleanup_artifact` guard.

	The old guard restored bootstrap-overwritten tracked support files
	(scripts/git_ref_health_check.sh, scripts/tg_helpers.sh,
	scripts/codex_model_catalog.json, .github/ai/orchestrate_schema.v1.json)
	from HEAD after the poller's "combined mode" ran codex and committed
	fixes to the working tree directly. Combined mode is gone: the
	review-blocked judge is pinned decision-only (never a writable
	checkout, never a direct commit/push — see the sourced comment below),
	and its `fix` action pushes a no-tree-change boundary commit instead
	of editing files locally. With no local write left, there is nothing
	for a bootstrap-overwrite restore to guard, so `_orch_cleanup_artifact`
	was removed rather than kept as dead code (see
	tests/test_consumer_artifact_cleanup_preserves_tracked.py's module
	docstring). This test pins the invariant that makes that safe: combined
	mode is never turned back on without also reinstating the guard."""
	poller_text = (REPO_ROOT / "scripts/orchestrate_poll_process.sh").read_text(encoding="utf-8")
	assert 'RB_COMBINED_MODE="false"' in poller_text, (
		"scripts/orchestrate_poll_process.sh must pin RB_COMBINED_MODE to "
		"false — the review-blocked judge must stay decision-only."
	)
	assert 'RB_COMBINED_MODE="true"' not in poller_text, (
		"scripts/orchestrate_poll_process.sh must never flip RB_COMBINED_MODE "
		"to true without reinstating the _orch_cleanup_artifact guard that "
		"combined-mode direct commits used to need."
	)
	assert "The review-blocked judge is decision-only." in poller_text and "never applies or pushes changes directly" in poller_text, (
		"scripts/orchestrate_poll_process.sh lost the decision-only documentation for the review-blocked judge"
	)


def test_implement_cleanup_preserves_tracked_static_context() -> None:
	"""Implement cleanup restores a tracked static-context path from HEAD."""
	text = (REPO_ROOT / "scripts/implement_commit_changes.sh").read_text(encoding="utf-8")
	match = re.search(
		r'^if git cat-file -e "HEAD:pre_assembled_static\.txt".*?^fi$',
		text,
		flags=re.S | re.M,
	)
	assert match, "implement cleanup guard for pre_assembled_static.txt not found"
	block = match.group(0)
	with tempfile.TemporaryDirectory(prefix="artifact-cleanup-") as raw_tmp:
		repo = _make_consumer_repo(Path(raw_tmp), UNTRACKED_ARTIFACT)
		expected = (repo / UNTRACKED_ARTIFACT).read_text(encoding="utf-8")
		(repo / UNTRACKED_ARTIFACT).write_text("generated static context\n", encoding="utf-8")

		subprocess.run(
			["bash", "-euo", "pipefail", "-c", block],
			cwd=repo,
			check=True,
			capture_output=True,
			env=_temp_repo_env(),
			text=True,
		)

		assert (repo / UNTRACKED_ARTIFACT).read_text(encoding="utf-8") == expected
		assert _git(repo, "diff", "--name-only").strip() == ""

	with tempfile.TemporaryDirectory(prefix="artifact-cleanup-") as raw_tmp:
		repo = _make_consumer_repo(Path(raw_tmp), "consumer-owned.txt")
		(repo / UNTRACKED_ARTIFACT).write_text("generated static context\n", encoding="utf-8")
		subprocess.run(
			["bash", "-euo", "pipefail", "-c", block],
			cwd=repo,
			check=True,
			capture_output=True,
			env=_temp_repo_env(),
			text=True,
		)
		assert not (repo / UNTRACKED_ARTIFACT).exists()


def test_review_cleanup_restores_tracked_static_context() -> None:
	"""Review editor, resolver, and judge cleanup restore tracked context."""
	for script_rel, loop_var in (
		("scripts/review_commit_changes.sh", "_artifact"),
		("scripts/review_conflict_resolve.sh", "_rs_cleanup_artifact"),
		("scripts/review_rb_judge.sh", "_rb_cleanup_artifact"),
	):
		block = _extract_cleanup_block(script_rel, loop_var)
		with tempfile.TemporaryDirectory(prefix="artifact-cleanup-") as raw_tmp:
			repo = _make_consumer_repo(Path(raw_tmp), UNTRACKED_ARTIFACT)
			expected = (repo / UNTRACKED_ARTIFACT).read_text(encoding="utf-8")
			(repo / UNTRACKED_ARTIFACT).write_text("generated static context\n", encoding="utf-8")

			subprocess.run(
				["bash", "-euo", "pipefail", "-c", block],
				cwd=repo,
				check=True,
				capture_output=True,
				env=_temp_repo_env(),
				text=True,
			)

			assert (repo / UNTRACKED_ARTIFACT).read_text(encoding="utf-8") == expected
			_git(repo, "add", "-u")
			assert _git(repo, "diff", "--cached", "--name-status").strip() == ""


def main() -> int:
	tests = [
		test_cleanup_preserves_tracked_agents_md,
		test_cleanup_block_guards_every_path,
		test_no_unguarded_agents_md_removal_remains,
		test_poller_restores_bootstrap_overwrite,
		test_implement_cleanup_preserves_tracked_static_context,
		test_review_cleanup_restores_tracked_static_context,
	]
	passed = 0
	failed = 0
	for func in tests:
		try:
			func()
			print(f"  PASS  {func.__name__}", flush=True)
			passed += 1
		except Exception as exc:  # noqa: BLE001 - direct-run harness reports all failures
			print(f"  FAIL  {func.__name__}: {exc}", flush=True)
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
