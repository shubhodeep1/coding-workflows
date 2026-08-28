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

import inspect
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "scripts" / "detect_editor_changes_lost.sh"
STAGE_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "editor_summaries"
REVIEW_AUTOFIX_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
APPLY_FIXES_SH = REPO_ROOT / "scripts" / "review_apply_fixes.sh"


def _init_clean_repo(tmp_path: Path) -> None:
	import os
	clean_git_env = os.environ.copy()
	for key in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "WORKSPACE_PATH"):
		clean_git_env.pop(key, None)
	clean_git_env["PWD"] = str(tmp_path)
	clean_git_env.pop("OLDPWD", None)
	subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, env=clean_git_env, check=True)
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
		subprocess.run(["git", "config", key, val], cwd=tmp_path, env=clean_git_env, check=True)
	(tmp_path / "placeholder").write_text("seed\n", encoding="utf-8")
	subprocess.run(["git", "add", "placeholder"], cwd=tmp_path, env=clean_git_env, check=True)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
		cwd=tmp_path,
		env=clean_git_env,
		check=True,
	)


def _run_shim(tmp_path: Path, summary: Path, committed_files: Path | None = None) -> str:
	import os
	env = os.environ.copy()
	for key in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "WORKSPACE_PATH"):
		env.pop(key, None)
	env["PWD"] = str(tmp_path)
	env.pop("OLDPWD", None)
	if committed_files is None:
		env.pop("COMMITTED_FILES_FILE", None)
	else:
		env["COMMITTED_FILES_FILE"] = str(committed_files)
	result = subprocess.run(
		["bash", str(SHIM), str(summary)],
		cwd=tmp_path,
		capture_output=True,
		text=True,
		check=True,
		env=env,
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
	classic editor-tool-persistence-failure case that the original detector
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


def _write_external(tmp_path: Path, name: str, contents: str) -> Path:
	"""Write a helper file OUTSIDE the git worktree so it does not show up
	in `git status --porcelain` inside the scratch repo. Mirrors the real
	workflow layout where COMMITTED_FILES_FILE lives under /tmp/codex-pr-…
	rather than inside the checkout."""
	external_dir = tmp_path.parent / f"{tmp_path.name}-external"
	external_dir.mkdir(parents=True, exist_ok=True)
	path = external_dir / name
	path.write_text(contents, encoding="utf-8")
	return path


def test_ledger_delete_covered_by_committed_files_is_false(tmp_path: Path) -> None:
	"""PR #1524 reproducing case: the editor's productive fix is to delete
	the tracked review-issue ledger file. The commit step records this as
	LEDGER_ONLY_COMMIT=true and writes the committed path into
	COMMITTED_FILES_FILE. The narrative's only file-path reference is the
	ledger path itself, so the subset check must downgrade the detector
	to 'false' and leave auto-merge unblocked."""
	_init_clean_repo(tmp_path)
	committed = _write_external(
		tmp_path,
		"committed_files.txt",
		"- .ai/review_issue_ledger/pr-1524.txt\n",
	)
	result = _run_shim(
		tmp_path,
		FIXTURES / "narrative_ledger_delete_status_edited.txt",
		committed_files=committed,
	)
	assert result == "false", (
		"Expected shim to treat a ledger-only commit whose narrative only "
		f"references the committed ledger path as a false positive; got {result!r}."
	)


def test_claimed_path_not_in_commit_still_reports_changes_lost(tmp_path: Path) -> None:
	"""PR #1472 safety: when the editor narrates a non-ledger edit but the
	commit only contains the ledger path, the narrative path is NOT in the
	committed set, so the subset check must fall through to 'true' and
	keep the existing detector warning intact."""
	_init_clean_repo(tmp_path)
	committed = _write_external(
		tmp_path,
		"committed_files.txt",
		"- .ai/review_issue_ledger/pr-999.txt\n",
	)
	result = _run_shim(
		tmp_path,
		FIXTURES / "narrative_real_edit_status_edited.txt",
		committed_files=committed,
	)
	assert result == "true", (
		"Expected shim to keep reporting 'true' when the narrative claims a "
		f"non-ledger edit that is missing from COMMITTED_FILES_FILE; got {result!r}."
	)


def test_reference_clause_path_not_treated_as_edit_target(tmp_path: Path) -> None:
	"""bitsafe.io PR #135 / run 25628091558 reproducing case: the editor
	bullet edits a single test file but its trailing "This matches `Y`"
	clause references a sibling production file that was NOT edited.
	Pre-fix, the path extractor pulled BOTH paths and the subset check
	against COMMITTED_FILES_FILE failed because the referenced sibling
	was correctly absent from the commit, producing a false-positive
	EDITOR_CHANGES_LOST. The shim now strips reference clauses before
	path extraction so only the actual edit target is checked."""
	_init_clean_repo(tmp_path)
	committed = _write_external(
		tmp_path,
		"committed_files.txt",
		"- apps/api/test/auth-service-verification.test.mjs\n",
	)
	result = _run_shim(
		tmp_path,
		FIXTURES / "narrative_reference_clause_status_edited.txt",
		committed_files=committed,
	)
	assert result == "false", (
		"Expected shim to strip the trailing 'This matches `Y`' reference "
		f"clause and treat the bullet as a single-file edit; got {result!r}."
	)
def test_reference_clause_capitalised_verb_is_stripped(tmp_path: Path) -> None:
	"""Same shape as the bitsafe.io reproducer above, but with a
	title-cased verb ('This Matches `Y`'). The strip regex must
	tolerate capitalised verbs so a stylistic choice in the editor's
	narrative cannot resurrect the EDITOR_CHANGES_LOST false-positive
	the original fix was designed to eliminate."""
	_init_clean_repo(tmp_path)
	external_dir = tmp_path.parent / f"{tmp_path.name}-fixtures"
	external_dir.mkdir(parents=True, exist_ok=True)
	fixture = FIXTURES / "narrative_reference_clause_status_edited.txt"
	mutated = external_dir / "narrative_reference_clause_capitalised.txt"
	mutated.write_text(
		fixture.read_text(encoding="utf-8").replace(
			"This matches", "This Matches"
		),
		encoding="utf-8",
	)
	committed = _write_external(
		tmp_path,
		"committed_files.txt",
		"- apps/api/test/auth-service-verification.test.mjs\n",
	)
	result = _run_shim(tmp_path, mutated, committed_files=committed)
	assert result == "false", (
		"Expected shim to strip the trailing 'This Matches `Y`' reference "
		f"clause even when the verb is capitalised; got {result!r}."
	)


def test_reference_clause_all_caps_verb_is_stripped(tmp_path: Path) -> None:
	"""Same reproducer, but with the verb in ALL CAPS
	('This MATCHES `Y`'). The strip regex is full-word case-
	insensitive (per-letter `[Aa]`-style alternation), so all-caps
	and mixed-case spellings ('mIrRoRs', 'REFERENCES') must also be
	stripped — not just title case."""
	_init_clean_repo(tmp_path)
	external_dir = tmp_path.parent / f"{tmp_path.name}-fixtures"
	external_dir.mkdir(parents=True, exist_ok=True)
	fixture = FIXTURES / "narrative_reference_clause_status_edited.txt"
	mutated = external_dir / "narrative_reference_clause_all_caps.txt"
	mutated.write_text(
		fixture.read_text(encoding="utf-8").replace(
			"This matches", "This MATCHES"
		),
		encoding="utf-8",
	)
	committed = _write_external(
		tmp_path,
		"committed_files.txt",
		"- apps/api/test/auth-service-verification.test.mjs\n",
	)
	result = _run_shim(tmp_path, mutated, committed_files=committed)
	assert result == "false", (
		"Expected shim to strip the trailing 'This MATCHES `Y`' reference "
		f"clause even when the verb is all-caps; got {result!r}."
	)


def test_committed_files_file_unset_preserves_legacy_behaviour(tmp_path: Path) -> None:
	"""When COMMITTED_FILES_FILE is unset the shim must behave exactly as
	before — the new subset check is purely additive. narrative_real_edit
	+ clean tree still returns 'true'."""
	_init_clean_repo(tmp_path)
	result = _run_shim(tmp_path, FIXTURES / "narrative_real_edit_status_edited.txt")
	assert result == "true", (
		"Expected shim with no COMMITTED_FILES_FILE env to match pre-change "
		f"behaviour and still report 'true'; got {result!r}."
	)


def test_committed_files_file_with_marker_line_falls_through(tmp_path: Path) -> None:
	"""A COMMITTED_FILES_FILE containing only a marker line (e.g. '- none'
	or '- commit skipped ...') has no real committed paths, so the subset
	check must fall through to 'true' rather than silently succeeding."""
	_init_clean_repo(tmp_path)
	committed = _write_external(tmp_path, "committed_files.txt", "- none\n")
	result = _run_shim(
		tmp_path,
		FIXTURES / "narrative_ledger_delete_status_edited.txt",
		committed_files=committed,
	)
	assert result == "true", (
		"Expected shim to fall through to 'true' when COMMITTED_FILES_FILE "
		f"contains only the marker line '- none'; got {result!r}."
	)


def test_ledger_delete_covered_by_normalized_committed_path_is_false(tmp_path: Path) -> None:
	"""Equivalent path spellings should still match after normalization.
	The committed file list may contain `./`-prefixed paths; the shim must
	accept them as the same ledger path and downgrade to `false`."""
	_init_clean_repo(tmp_path)
	committed = _write_external(
		tmp_path,
		"committed_files.txt",
		"- ./.ai/review_issue_ledger/pr-1524.txt\n",
	)
	result = _run_shim(
		tmp_path,
		FIXTURES / "narrative_ledger_delete_status_edited.txt",
		committed_files=committed,
	)
	assert result == "false", (
		f"Expected shim to normalize committed-file paths before matching; got {result!r}."
	)


def test_edit_claim_with_backticked_non_file_identifier_still_reports_changes_lost(tmp_path: Path) -> None:
	"""Backticked code identifiers on edit-claim lines must not be treated
	as file paths. When the real file edit is present in the committed
	set, the shim should still downgrade to `false`."""
	_init_clean_repo(tmp_path)
	# Write the summary OUTSIDE the worktree (mirroring the other tests via
	# _write_external) so it does not show up in `git status --porcelain`
	# inside the scratch repo; otherwise the shim's subset check at
	# scripts/detect_editor_changes_lost.sh:81 (gated on a clean tree)
	# never runs and the assertion below cannot be exercised.
	fixture = _write_external(
		tmp_path,
		"summary.txt",
		"Changes made:\n"
		"- Modified `LEDGER_ONLY_COMMIT` handling in `scripts/detect_editor_changes_lost.sh`.\n"
		"\n"
		"Change status:\n"
		"- edited\n",
	)
	committed = _write_external(
		tmp_path,
		"committed_files.txt",
		"- scripts/detect_editor_changes_lost.sh\n",
	)
	result = _run_shim(tmp_path, fixture, committed_files=committed)
	assert result == "false", (
		"Expected shim to ignore backticked non-file identifiers and "
		f"downgrade when the real file edit is committed; got {result!r}."
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
	assert "CONSOLIDATOR_OVERRIDDEN: <issue_id> — <reason>" in contents
	assert "CONSOLIDATOR_OVERRIDDEN: no-issue-id — <reason>" in contents
	assert 'Place that bullet inside "Ignored suggestions (with short reason):"' in contents


def test_apply_fixes_uses_opencode_writer_with_fresh_prompt_fallback() -> None:
	contents = APPLY_FIXES_SH.read_text(encoding="utf-8")
	assert 'opencode_run_cmd "$@"' in contents
	assert 'writer\n    "${editor_attempt_model}"' in contents
	assert '--model "${editor_attempt_model}"' in contents
	assert "CODEX_THREAD_REUSE_ENABLED requested; OpenCode editor uses the fresh full-prompt path." in contents
	assert "exec codex " not in contents


def test_workflow_uses_defense_in_depth_shim() -> None:
	wf = REVIEW_AUTOFIX_WF.read_text(encoding="utf-8")
	stage_helper = STAGE_HELPER.read_text(encoding="utf-8")
	# The shim must be installed by the bootstrap step so consumer
	# repos — not just the workflow-source repo — actually have the
	# script on disk at runtime.  Reading it from a non-bootstrapped
	# path silently no-ops the recheck in every consumer (bitsafe.io
	# PR #177 / run 25653654000 escape).
	bootstrap_line = next(
		(line for line in stage_helper.splitlines() if "REQUIRED_BOOTSTRAP_SCRIPTS=" in line),
		"",
	)
	assert "detect_editor_changes_lost.sh" in bootstrap_line, (
		"Expected detect_editor_changes_lost.sh in REQUIRED_BOOTSTRAP_SCRIPTS so the shim is reachable in consumer repos"
	)
	# The recheck must read from the bootstrapped directory; the
	# previous `${GITHUB_WORKSPACE}/scripts/` path only exists in the
	# workflow-source repo and silently failed in every consumer.
	assert '${SUPPORT_SCRIPTS_DIR}/detect_editor_changes_lost.sh' in wf, (
		"Expected review_autofix.yml to invoke the defense-in-depth shim from SUPPORT_SCRIPTS_DIR"
	)
	assert "treating as false positive (no warning, auto-merge not blocked)" in wf


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	passed = 0
	failed = 0

	for func in test_funcs:
		name = func.__name__
		try:
			params = list(inspect.signature(func).parameters)
			if not params:
				func()
			elif params == ["tmp_path"]:
				with tempfile.TemporaryDirectory(prefix="detect-editor-changes-lost-") as td:
					func(Path(td))
			else:
				raise TypeError(f"unsupported test signature for {name}: {params}")
			print(f"  PASS  {name}")
			passed += 1
		except AssertionError as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
		except Exception as e:
			print(f"  ERROR {name}: {type(e).__name__}: {e}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
