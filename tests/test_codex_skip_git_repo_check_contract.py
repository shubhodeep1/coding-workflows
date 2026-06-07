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

2. Every standalone single-line ``codex ... exec ... --model ... --sandbox``
   invocation in ``scripts/*.sh`` and ``.github/workflows/*.yml`` must carry
   ``--skip-git-repo-check``.

``--skip-git-repo-check`` is a no-op inside a real git checkout, so it is always
safe to require it — there is no codex call in this automation that wants the
trust check on.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "codex_thread_reuse.sh"

# A single-line, fully-specified codex invocation: the `codex` binary, the
# `exec` subcommand, and both `--model` and `--sandbox` on one physical line.
# Real invocations always carry all four on the same line; this deliberately
# does NOT try to reassemble multi-line array constructions (those keep the
# flag on its own continuation line and are covered by manual review + the
# helper default). Requiring --model and --sandbox keeps comments and prose
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


def test_all_codex_exec_invocations_skip_git_repo_check() -> None:
	"""No standalone codex exec call may omit --skip-git-repo-check.

	If this fails, a new (or migrated) codex invocation was added without the
	guard and will deterministically abort with "Not inside a trusted
	directory" the moment that workflow runs in the worktree workspace. Add
	``--skip-git-repo-check`` immediately after ``exec``.
	"""
	violations: list[str] = []
	for path in _scanned_files():
		if path.name in _SCANNER_EXCLUDE:
			continue
		for lineno, line in enumerate(
			path.read_text(encoding="utf-8").splitlines(), start=1
		):
			if line.lstrip().startswith("#"):
				continue
			if _INVOCATION.search(line) and "--skip-git-repo-check" not in line:
				rel = path.relative_to(REPO_ROOT)
				violations.append(f"{rel}:{lineno}: {line.strip()}")

	assert not violations, (
		"codex exec invocation(s) missing --skip-git-repo-check (will abort with "
		"'Not inside a trusted directory' in the worktree workspace):\n"
		+ "\n".join(violations)
	)


def test_scanner_actually_matches_known_invocations() -> None:
	"""Guard against the scanner silently matching nothing (e.g. a regex typo).

	A contract test that matches zero lines would pass vacuously and stop
	protecting anything, so assert it still sees the real fleet of calls.
	"""
	matched = 0
	for path in _scanned_files():
		if path.name in _SCANNER_EXCLUDE:
			continue
		for line in path.read_text(encoding="utf-8").splitlines():
			if line.lstrip().startswith("#"):
				continue
			if _INVOCATION.search(line):
				matched += 1
	assert matched >= 10, (
		f"scanner matched only {matched} codex exec invocations; the detection "
		"regex is probably broken"
	)
