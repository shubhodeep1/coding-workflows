#!/usr/bin/env python3
"""Regression tests for the EDITOR_CHANGES_LOST false-positive fix.

Reproducing case: shubhodeep1/fun-token-multi-chain PR #117, runs
24537598009 and 24540975236. The editor emitted a summary where the
narrative "Changes made:" block was "- none" but the authoritative
"Change status:" bullet was "edited". The workflow's
"Detect editor-claimed-but-uncommitted changes" step trusted
"Change status: edited" as authoritative, set EDITOR_CHANGES_LOST=true,
and blocked auto-merge despite the working tree being clean.

These tests exercise the defense-in-depth shim
(scripts/detect_editor_changes_lost.sh) that the workflow consults
before firing the warning.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "scripts" / "detect_editor_changes_lost.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "editor_summaries"
REVIEW_AUTOFIX_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
APPLY_FIXES_SH = REPO_ROOT / "scripts" / "review_apply_fixes.sh"


def _init_clean_repo(tmp_path: Path) -> None:
	subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
	# Test-local config only: disable signing + gpg inside this throwaway
	# repo so environments that mandate signed commits still let the
	# fixture boot (the repo is never pushed anywhere).
	for key, val in (
		("user.email", "test@local"),
		("user.name", "test"),
		("commit.gpgsign", "false"),
		("tag.gpgsign", "false"),
		("gpg.format", "openpgp"),
	):
		subprocess.run(["git", "config", key, val], cwd=tmp_path, check=True)
	(tmp_path / "placeholder").write_text("seed\n", encoding="utf-8")
	subprocess.run(["git", "add", "placeholder"], cwd=tmp_path, check=True)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
		cwd=tmp_path,
		check=True,
	)


def _run_shim(tmp_path: Path, summary: Path) -> str:
	result = subprocess.run(
		["bash", str(SHIM), str(summary)],
		cwd=tmp_path,
		capture_output=True,
		text=True,
		check=True,
	)
	return result.stdout.strip()


def test_shim_exists_and_is_executable() -> None:
	assert SHIM.exists(), f"Expected shim at {SHIM}"
	assert SHIM.stat().st_mode & 0o111, "Expected shim to be executable"


def test_false_positive_narrative_none_status_edited_clean_tree(tmp_path: Path) -> None:
	"""Reproducing case from PR #117: narrative '- none', Change status
	'edited', working tree clean. Shim must report 'false' (not
	changes-lost) so the workflow can downgrade to an informational log
	and leave auto-merge unblocked."""
	_init_clean_repo(tmp_path)
	result = _run_shim(tmp_path, FIXTURES / "narrative_none_status_edited.txt")
	assert result == "false", (
		"Expected shim to treat narrative='- none' + Change status='edited' + "
		f"clean working tree as a false positive; got {result!r}."
	)


def test_real_edit_with_uncommitted_worktree_is_changes_lost(tmp_path: Path) -> None:
	"""Sanity guard: when the narrative actually claims concrete edits and
	the working tree has uncommitted modifications, the shim must still
	report 'true' so the existing warning path remains intact."""
	_init_clean_repo(tmp_path)
	(tmp_path / "placeholder").write_text("seed\nmodified\n", encoding="utf-8")
	result = _run_shim(tmp_path, FIXTURES / "narrative_real_edit_status_edited.txt")
	assert result == "true", (
		"Expected shim to preserve changes-lost detection when narrative "
		f"claims edits and working tree has uncommitted changes; got {result!r}."
	)


def test_real_edit_narrative_with_clean_tree_is_changes_lost(tmp_path: Path) -> None:
	"""Narrative claims concrete edits but the working tree is clean — the
	classic Serena-persistence-failure case that the original detector
	exists to catch. Shim must still report 'true'."""
	_init_clean_repo(tmp_path)
	result = _run_shim(tmp_path, FIXTURES / "narrative_real_edit_status_edited.txt")
	assert result == "true", (
		"Expected shim to keep reporting 'true' when narrative claims edits "
		f"even if working tree is clean; got {result!r}."
	)


def test_missing_summary_fails_open(tmp_path: Path) -> None:
	"""If the summary file is missing the shim must fail open ('true') so
	the existing heuristic keeps control rather than silently unblocking
	auto-merge."""
	_init_clean_repo(tmp_path)
	result = _run_shim(tmp_path, tmp_path / "does-not-exist.txt")
	assert result == "true", (
		f"Expected fail-open 'true' for missing summary; got {result!r}."
	)


def test_non_git_directory_fails_open(tmp_path: Path) -> None:
	"""If the shim runs outside a git worktree, git-state is unknown and it
	must fail open ('true') rather than classifying as a clean tree."""
	result = _run_shim(tmp_path, FIXTURES / "narrative_none_status_edited.txt")
	assert result == "true", (
		f"Expected fail-open 'true' when git status is unavailable; got {result!r}."
	)


# ---------------------------------------------------------------------------
# Static assertions: the normalization + defense-in-depth wiring stays wired
# ---------------------------------------------------------------------------

def test_apply_fixes_contains_change_status_normalization() -> None:
	contents = APPLY_FIXES_SH.read_text(encoding="utf-8")
	assert "normalizing Change status: edited" in contents, (
		"Expected scripts/review_apply_fixes.sh to normalize Change status: "
		"when narrative reports no changes and git is clean"
	)
	assert "_norm_git_clean" in contents


def test_apply_fixes_contains_editor_input_authority_contract() -> None:
	contents = APPLY_FIXES_SH.read_text(encoding="utf-8")
	assert "INPUT AUTHORITY CONTRACT" in contents
	assert "Authoritative input:" in contents
	assert "- ${RUNTIME_DIR}/reviewer_bundle.txt" in contents
	assert "Advisory inputs (fail-open if missing, empty, or malformed):" in contents
	assert "- ${RUNTIME_DIR}/review_issues.txt" in contents
	assert "- ${RUNTIME_DIR}/ledger_status.txt" in contents
	assert "- ${RUNTIME_DIR}/floor_tags.txt" in contents
	assert "Do not let advisory artifacts reduce or replace raw reviewer signal" in contents
	assert "Treat ${RUNTIME_DIR}/floor_tags.txt as non-skippable floor findings" in contents
	assert "CONSOLIDATOR_OVERRIDDEN: <reason>" in contents


def test_workflow_uses_defense_in_depth_shim() -> None:
	wf = REVIEW_AUTOFIX_WF.read_text(encoding="utf-8")
	assert "scripts/detect_editor_changes_lost.sh" in wf, (
		"Expected review_autofix.yml to invoke the defense-in-depth shim"
	)
	assert "treating as false positive (no warning, auto-merge not blocked)" in wf
