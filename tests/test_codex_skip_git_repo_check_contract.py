#!/usr/bin/env python3
"""Repo-wide contract: every codex `exec` invocation must skip the git-repo check.

Background — the recurring "Not inside a trusted directory" regression
=====================================================================

Every AI workflow runs ``codex exec`` inside a worktree-only workspace: a
materialized copy of the tree whose git linkage is supplied purely via the
``GIT_DIR`` / ``GIT_WORK_TREE`` environment variables, with no ``.git`` entry
in the working directory. Codex's git-repo / trust discovery ignores those env
vars, so when the flag is missing codex aborts every attempt with::

    Not inside a trusted directory and --skip-git-repo-check was not specified.

returning empty output. This is deterministic, not flaky.

This exact failure has now bitten the repo repeatedly, each time in a workflow
that was migrated into the workspace model without carrying the guard over:

* #3076/#3077 — review + validate codex calls.
* #3194/#3195 — implement codex calls (regressed by the Symphony change #3048,
  which moved implement into the worktree workspace but never added the flag).

Both rounds were point-fixes to individual call sites. Nothing prevented the
next omission, so the same deterministic failure kept resurfacing. This test
makes the invariant enforceable at PR time instead of in production:

1. ``scripts/codex_thread_reuse.sh`` (the shared thread-reuse runner that every
   ``direct-run`` caller funnels through) must default ``skip_git_repo_check``
   to ``true`` — so a caller that forgets the env var is still safe.

2. Every standalone ``codex ... exec ... --model ... --sandbox`` invocation
   in ``scripts/*.sh`` and ``.github/workflows/*.yml`` must carry
   ``--skip-git-repo-check``. The scanner reassembles backslash-continued
   shell / YAML commands into one logical line before checking.

``--skip-git-repo-check`` is a no-op inside a real git checkout, so it is always
safe to require it — there is no codex call in this automation that wants the
trust check on.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "codex_thread_reuse.sh"
DISCOVERY_BOOTSTRAP = REPO_ROOT / "scripts" / "validation_discovery_bootstrap.py"
REVIEW_AUTOFIX = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
P3_REVIEW_SCRIPTS = (
	"review_apply_fixes.sh",
	"review_rb_judge.sh",
	"review_consolidate.sh",
	"review_conflict_resolve.sh",
)

# A fully-specified codex invocation on one logical shell / YAML command line:
# after reassembling backslash-continued physical lines, the command must
# mention the `codex` binary, the `exec` subcommand, and both `--model` and
# `--sandbox`. Requiring `--model` and `--sandbox` keeps comments and prose
# that merely mention "codex exec" from matching.
_INVOCATION = re.compile(r"\bcodex\b.*\bexec\b.*--model\b.*--sandbox\b")

# The shared runner builds its codex command line conditionally across several
# array-append lines, so it is validated by the default-value assertion below
# rather than the line scanner.
_SCANNER_EXCLUDE = {"codex_thread_reuse.sh"}


def _scanned_files() -> list[Path]:
	files = sorted((REPO_ROOT / "scripts").glob("*.sh"))
	files += sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
	return files


def _iter_logical_lines(path: Path) -> list[tuple[int, str]]:
	logical_lines: list[tuple[int, str]] = []
	pending: list[str] = []
	start_lineno = 0

	for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
		if not pending:
			if not line.strip() or line.lstrip().startswith("#"):
				continue
			start_lineno = lineno
		pending.append(line)

		if line.rstrip().endswith("\\"):
			continue

		parts: list[str] = []
		for part in pending:
			trimmed = part.rstrip()
			if trimmed.endswith("\\"):
				trimmed = trimmed[:-1].rstrip()
			if trimmed.strip():
				parts.append(trimmed.strip())
		logical_lines.append((start_lineno, " ".join(parts)))
		pending = []

	if pending:
		parts = []
		for part in pending:
			trimmed = part.rstrip()
			if trimmed.endswith("\\"):
				trimmed = trimmed[:-1].rstrip()
			if trimmed.strip():
				parts.append(trimmed.strip())
		logical_lines.append((start_lineno, " ".join(parts)))

	return logical_lines


def _iter_codex_exec_invocations() -> list[tuple[Path, int, str]]:
	matches: list[tuple[Path, int, str]] = []
	for path in _scanned_files():
		if path.name in _SCANNER_EXCLUDE:
			continue
		for lineno, logical_line in _iter_logical_lines(path):
			if _INVOCATION.search(logical_line):
				matches.append((path.relative_to(REPO_ROOT), lineno, logical_line))
	return matches


def test_helper_defaults_skip_git_repo_check_on() -> None:
	"""The shared runner must be safe-by-default for every direct-run caller."""
	text = HELPER.read_text(encoding="utf-8")
	# Positional default in codex_thread_reuse_run_once (arg 9).
	assert 'local skip_git_repo_check="${9:-true}"' in text, (
		"codex_thread_reuse_run_once must default skip_git_repo_check to true "
		"so workspace runs skip codex's git-repo/trust check"
	)
	# Env-var default in codex_thread_reuse_direct_run.
	assert (
		'local skip_git_repo_check="${CODEX_THREAD_REUSE_SKIP_GIT_REPO_CHECK:-true}"'
		in text
	), (
		"codex_thread_reuse_direct_run must default "
		"CODEX_THREAD_REUSE_SKIP_GIT_REPO_CHECK to true"
	)
	# And it must still be the flag actually appended to the command line.
	assert "cmd+=(--skip-git-repo-check)" in text


def test_python_discovery_helper_skips_git_repo_check() -> None:
	"""The production Python discovery helper must carry the same flag."""
	text = DISCOVERY_BOOTSTRAP.read_text(encoding="utf-8")
	assert re.search(
		r'command = \[\s*"codex",.*?"exec",\s*"--skip-git-repo-check",\s*"--model",.*?"--sandbox",',
		text,
		re.S,
	), (
		"validation_discovery_bootstrap.py must pass --skip-git-repo-check "
		"in its codex command list"
	)


def test_all_codex_exec_invocations_skip_git_repo_check() -> None:
	"""No standalone codex exec call may omit --skip-git-repo-check.

	If this fails, a new (or migrated) codex invocation was added without the
	guard and will deterministically abort with "Not inside a trusted
	directory" the moment that workflow runs in the worktree workspace. Add
	``--skip-git-repo-check`` immediately after ``exec``.
	"""
	violations: list[str] = []
	for rel, lineno, logical_line in _iter_codex_exec_invocations():
		if "--skip-git-repo-check" not in logical_line:
			violations.append(f"{rel}:{lineno}: {logical_line}")

	assert not violations, (
		"codex exec invocation(s) missing --skip-git-repo-check (will abort with "
		"'Not inside a trusted directory' in the worktree workspace):\n"
		+ "\n".join(violations)
	)


def test_review_autofix_write_side_has_no_codex_runtime() -> None:
	workflow = REVIEW_AUTOFIX.read_text(encoding="utf-8")
	assert "Install Codex CLI" not in workflow
	assert "Create Codex config" not in workflow
	assert "write_codex_config.sh" not in workflow
	assert "command -v codex" not in workflow
	assert ".codex/config.toml" not in workflow
	for script_name in P3_REVIEW_SCRIPTS:
		text = (REPO_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
		assert not _INVOCATION.search(" ".join(text.splitlines())), script_name



def test_scanner_matches_backslash_continued_invocations() -> None:
	"""Backslash-continued codex commands must be part of the contract scan."""
	matches = [
		logical_line
		for path, _, logical_line in _iter_codex_exec_invocations()
		if path == Path(".github/workflows/orchestrate_clarify_respond.yml")
	]
	assert any("critic_prompt.txt" in logical_line for logical_line in matches), (
		"scanner must detect the self-critique critic codex invocation in "
		"orchestrate_clarify_respond.yml"
	)
	assert any('${CODEX_PROMPT_FILE}.v2' in logical_line for logical_line in matches), (
		"scanner must detect the self-critique re-run codex invocation in "
		"orchestrate_clarify_respond.yml"
	)


def test_scanner_actually_matches_known_invocations() -> None:
	"""Guard against the scanner silently matching nothing (e.g. a regex typo).

	A contract test that matches zero lines would pass vacuously and stop
	protecting anything, so assert it still sees the real fleet of calls.
	"""
	matched = len(_iter_codex_exec_invocations())
	assert matched >= 10, (
		f"scanner matched only {matched} codex exec invocations; the detection "
		"regex is probably broken"
	)


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
