#!/usr/bin/env python3
"""Contract tests for the noop-suspicious recovery sweep in
`scripts/orchestrate_poll_process.sh`.

The sweep recovers PRs that hit
`EDITOR_NOOP_SUSPICIOUS=true` in `.github/workflows/review_autofix.yml`
and would otherwise stall forever (the workflow's "Enable auto-merge"
step is gated off). Issue-linked PRs eventually recover via the
generic standalone-stall recovery path, but linked-issue-less PRs
(branches like `claude/**`) never do — the original incident was
shubhodeep1/tele-funtoken-msg-scoring#3053.

These tests are grep-based contracts on the sweep's invariants:
detection, retry counter shape, dispatch path, force-merge gates,
shared-helper reuse, and Telegram alert levels. They are deliberately
NOT behavioural — the existing `tests/test_orchestrate_poll_process.py`
infrastructure for sandboxed poller runs is heavy enough that adding
behavioural fixtures for this sweep would dwarf the sweep itself. The
contracts cover the failure modes that matter:

  1. The sweep section header and its core invariants survive future
     edits.
  2. The retry threshold matches the design spec (3).
  3. Detection uses the exact warning literal the workflow emits
     (`⚠️ **Editor no-op suspicious**`) — drift between the two
     would break the recovery silently.
  4. The force-merge gate consults the shared audit helper, not a
     local re-implementation that could drift from review_autofix.yml.
  5. Force-merge fails CLOSED on every individual precondition.
  6. The sweep reuses the conflict sweep's `STANDALONE_PRS` cache
     (no second `gh pr list` per cycle, per CLAUDE.md §15).
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
HELPER = REPO_ROOT / "scripts" / "validate_editor_audit.sh"

SWEEP_HEADER = "Standalone PR noop-suspicious recovery sweep"
NOOP_WARNING_LITERAL = "⚠️ **Editor no-op suspicious**"


def _poller_text() -> str:
	return POLLER.read_text(encoding="utf-8")


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _sweep_block() -> str:
	"""Return the source slice from the sweep section header to EOF.

	The sweep is the final block of the poller; all contract checks
	below restrict their search to this block so an accidental match
	on unrelated code elsewhere can't falsely satisfy a contract."""
	text = _poller_text()
	start = text.find(SWEEP_HEADER)
	assert start != -1, f"Noop-suspicious recovery sweep section is missing — expected header {SWEEP_HEADER!r}"
	return text[start:]


def test_sweep_section_exists():
	"""The sweep is present. The header appears in two intentional
	places: the documentation comment block and the runtime banner
	echo. Anything other than exactly 2 indicates either the sweep is
	missing (0/1) or the block was accidentally duplicated (4+)."""
	text = _poller_text()
	occurrences = text.count(SWEEP_HEADER)
	assert occurrences == 2, (
		f"Expected exactly 2 occurrences of the sweep header (1 in "
		f"docs comment, 1 in echo banner); found {occurrences}. If "
		f"this jumped to >2 the sweep block was likely pasted twice."
	)


def test_sweep_uses_exact_workflow_warning_literal():
	"""Detection must use the exact warning literal that
	`review_autofix.yml` emits — a drift here would break recovery
	silently. The literal appears in both files."""
	wf = _workflow_text()
	assert NOOP_WARNING_LITERAL in wf, "Workflow must still emit the noop-suspicious warning literal"
	sweep = _sweep_block()
	assert NOOP_WARNING_LITERAL in sweep, "Poller sweep must grep for the exact workflow literal"
	# And it must be assigned to a named constant (so a future renamer
	# updating one site is forced to update the other too, via the
	# test failure here if they don't).
	assert 'NOOP_WARNING_LITERAL=' in sweep


def test_retry_threshold_is_three():
	"""The design spec is 3 retries before force-merge. Pinning the
	constant guards against accidental bumps."""
	sweep = _sweep_block()
	assert "NOOP_MAX_RETRIES=3" in sweep


def test_sweep_uses_shared_audit_helper():
	"""The force-merge gate B (reviewer audit health) MUST call the
	shared helper, not a local re-implementation. Otherwise the poller
	and the workflow can disagree about what "audit healthy" means."""
	sweep = _sweep_block()
	assert "source scripts/validate_editor_audit.sh" in sweep, (
		"Sweep must source scripts/validate_editor_audit.sh — "
		"never re-implement the arithmetic check inline."
	)
	assert "validate_editor_audit_arithmetic" in sweep, (
		"Sweep must invoke the shared function validate_editor_audit_arithmetic."
	)


def _strip_shell_comments(text: str) -> str:
	"""Remove lines that are pure comments (start with `#` after
	whitespace). Lets contract tests check for forbidden tokens like
	`gh pr list` in actual executable code without false-matching on
	the documentation comments that explain why those tokens are
	absent."""
	out = []
	for line in text.splitlines():
		stripped = line.lstrip()
		if stripped.startswith("#"):
			continue
		out.append(line)
	return "\n".join(out)


def test_sweep_reuses_standalone_prs_cache():
	"""Per CLAUDE.md §15, the sweep MUST reuse the conflict sweep's
	`STANDALONE_PRS` cache rather than running a second `gh pr list`.
	A new `gh pr list` inside the sweep block is a regression."""
	sweep_code = _strip_shell_comments(_sweep_block())
	assert "STANDALONE_PRS" in sweep_code, "Sweep must reference STANDALONE_PRS"
	assert "gh pr list" not in sweep_code, (
		"Sweep must NOT call `gh pr list` again — it should reuse the "
		"STANDALONE_PRS cache populated by the conflict sweep above."
	)


def test_sweep_uses_dispatch_helper_not_raw_workflow_run():
	"""Re-dispatches must go through `_dispatch_review_for_conflicts`
	so the cycle-local `_CONFLICT_DISPATCH_TRACKER` and active-run
	guard fire. A raw `gh workflow run` would bypass both."""
	sweep = _sweep_block()
	assert "_dispatch_review_for_conflicts" in sweep
	# Permit `gh pr merge --squash --auto` (force-merge path) but
	# forbid `gh workflow run` (would race the existing guards).
	assert "gh workflow run" not in sweep


def test_force_merge_call_uses_squash_and_auto():
	"""The actual force-merge invocation must use `--squash --auto` to
	mirror the workflow's auto-merge step
	(`.github/workflows/review_autofix.yml:5073`).

	Restricted to executable code (comments and Telegram message
	strings are stripped) so error-message strings like
	"'gh pr merge --auto' failed" don't false-fail."""
	sweep_code = _strip_shell_comments(_sweep_block())
	# Match only invocations that look like commands (lines that
	# include `gh pr merge` not wrapped in single quotes from a
	# message string). The simplest heuristic: look for at least one
	# line that has both --squash and --auto on the same gh-pr-merge
	# line.
	for line in sweep_code.splitlines():
		if "gh pr merge" in line and "--squash" in line and "--auto" in line:
			return  # found a valid invocation
	raise AssertionError(
		"Sweep must contain at least one `gh pr merge ... --squash --auto` "
		"invocation in executable code (not in a comment or string)."
	)


def test_force_merge_skip_labels_include_force_review_and_e2e():
	"""Operator opt-outs: `force-review` lets a human pin a PR for
	manual review; `e2e-smoke-test` mirrors the workflow's existing
	auto-merge suppression for smoke-test PRs."""
	sweep = _sweep_block()
	assert "e2e-smoke-test" in sweep
	assert "force-review" in sweep


def test_force_merge_gate_distinguishes_warning_and_error_levels():
	"""Re-dispatches alert at WARNING (expected operational chatter).
	Force-merge gate failures alert at ERROR (action needed —
	something genuinely broken). This distinction is part of the
	operator contract."""
	sweep = _sweep_block()
	# Match `tg_send_msg <args...> "WARNING"` / `"ERROR"` on a single
	# line — the calls use bash quote-concatenation for multi-segment
	# bodies but the level token is always the last quoted argument
	# on its own line.
	warning_lines = [
		line for line in sweep.splitlines()
		if "tg_send_msg" in line and '"WARNING"' in line
	]
	assert warning_lines, (
		"Sweep must emit at least one Telegram WARNING (the re-dispatch / "
		"successful-force-merge path)."
	)
	error_lines = [
		line for line in sweep.splitlines()
		if "tg_send_msg" in line and '"ERROR"' in line
	]
	assert error_lines, (
		"Sweep must emit Telegram ERROR alerts whenever a force-merge "
		"precondition fails — operators rely on the ERROR level to "
		"distinguish 'stuck PR genuinely broken' from routine chatter."
	)


def test_sweep_comment_refresh_failure_fails_closed():
	"""A comments-refresh API failure must abort the force-merge path for
	that cycle rather than evaluating Gate B on the stale pre-filter
	snapshot. Force-merge is a safety-sensitive fallback, so refresh
	failure is a closed gate, not a fail-open path."""
	sweep = _sweep_block()
	sweep_code = _strip_shell_comments(sweep)
	assert "could not refresh comments snapshot for noop-suspicious Gate B; failing force-merge closed this cycle." in sweep
	assert "could not refresh latest PR comments snapshot" in sweep
	assert "using pre-filter snapshot" not in sweep_code, (
		"Refresh failure must fail closed; executable sweep code should not "
		"continue Gate B using the pre-filter snapshot."
	)


def test_force_merge_each_gate_has_distinct_failure_branch():
	"""Gates A/B/C/D must each have an explicit failure branch that
	calls `continue` (so a downstream gate cannot accidentally fire
	on a PR that already failed an earlier gate). The contract is the
	individual gate identifiers showing up in alert text — operators
	debugging an ERROR alert need to see which gate tripped."""
	sweep = _sweep_block()
	for gate_id in ("gate A", "gate B", "gate C", "gate D"):
		assert gate_id in sweep, (
			f"Force-merge {gate_id} must have a labeled failure branch "
			"so operators reading the ERROR alert know which precondition failed."
		)


def test_force_merge_records_audit_trail_comment():
	"""Successful force-merges MUST post a PR comment alongside the
	Telegram alert. The audit trail on the PR thread is the durable
	record; Telegram is ephemeral."""
	sweep = _sweep_block()
	assert "Auto-merge enabled after" in sweep, (
		"Force-merge success path must post a PR comment explaining "
		"the override; the body is the audit trail."
	)
	assert "comments" in sweep, "Sweep must POST to issues/${PR}/comments"


def test_sweep_force_merge_gate_skips_when_no_productive_commit():
	"""Gate C: at least one `[ai-autofix]` / `[judge-fix]` commit must
	exist before force-merge fires. A PR whose editor was broken from
	the very first invocation is NOT force-merge eligible — the
	whole point of force-merge is "the editor produced N productive
	rounds and is now stuck in convergence."
	"""
	sweep = _sweep_block()
	# The detection block already filters for productive-commit
	# timestamp; we confirm the force-merge path explicitly references
	# the absence-of-productive-commit case.
	assert "no prior [ai-autofix] / [judge-fix] commit" in sweep


def test_sweep_force_merge_gate_d_checks_required_checks():
	"""Gate D blocks force-merge when any completed required check is
	failure/cancelled/timed_out/action_required, but it must ignore the
	stale `review / codex-agent` retry artifacts that the noop-suspicious
	recovery loop is explicitly trying to heal."""
	sweep = _sweep_block()
	for conclusion in ("failure", "cancelled", "timed_out", "action_required"):
		assert conclusion in sweep, (
			f"Gate D must treat conclusion={conclusion} as a blocker."
		)
	assert 'startswith("review / codex-agent")' in sweep, (
		"Gate D must match stale `review / codex-agent` retry artifacts by prefix so suffixed variants stay excluded."
	)
	assert "_is_retry_artifact | not" in sweep, (
		"Gate D must exclude stale `review / codex-agent` retry artifacts from the blocker set."
	)


def test_sweep_skips_orchestrator_project_branches():
	"""Integration / orchestrator-managed branches have their own
	merge cadence; this sweep must NOT touch them."""
	sweep = _sweep_block()
	assert '[[ "${N_BASE}" == orchestrator/project-* ]] || [[ "${N_HEAD}" == orchestrator/project-* ]]' in sweep, (
		"Sweep must explicitly skip both PRs targeting orchestrator/project-* base branches "
		"and final integration PRs whose head branch is orchestrator/project-*."
	)


def test_sweep_skips_forward_merge_fallback_branches():
	"""Forward-merge fallback PRs must be merged manually via Create a
	merge commit, so the noop-suspicious sweep must not redispatch or
	force-merge their `auto/forward-merge-stable-*` branches."""
	sweep = _sweep_block()
	assert '[[ "${N_HEAD}" == auto/forward-merge-stable-* ]]' in sweep, (
		"Sweep must explicitly skip forward-merge fallback PR branches."
	)
	assert "Create a merge commit" in sweep and "gh pr merge --squash --auto" in sweep, (
		"Forward-merge skip rationale must document the ancestry-preserving manual merge requirement."
	)


def test_sweep_counts_only_post_productive_warnings():
	"""The retry counter is "noop warnings newer than latest
	[ai-autofix] / [judge-fix] commit". This is the implicit reset on
	new head SHA — a new productive commit drops the count to 0.
	"""
	sweep = _sweep_block()
	# The jq filter must compare comment created_at against the
	# productive commit timestamp.
	assert "N_LATEST_PROD_TS" in sweep
	assert "[ai-autofix]" in sweep
	assert "[judge-fix]" in sweep
	assert 'select((.created_at // "") > $since)' in sweep


def test_sweep_stale_warning_guard_short_circuits_before_retry_counting():
	"""Warnings older than the latest productive commit must be skipped
	before retry counting, or stale noop comments could still drive the
	redispatch / force-merge decision on a newer head SHA."""
	sweep = _sweep_block()
	assert re.search(
		r'if \[ -n "\$\{N_LATEST_PROD_TS\}" \] && \[\[ "\$\{N_NOOP_LATEST_TS\}" < "\$\{N_LATEST_PROD_TS\}" \]\]; then',
		sweep,
	), (
		"Sweep must short-circuit when the latest noop warning predates "
		"the latest productive commit."
	)
	assert "stale, skipping." in sweep, (
		"Stale-warning branch must log that the PR was skipped so operator "
		"logs explain why the retry counter reset."
	)


def test_sweep_appears_after_conflict_sweep():
	"""Ordering matters: the conflict sweep must run first so a
	dirty PR gets routed to the conflict resolver rather than the
	noop-suspicious force-merge (which would refuse it anyway via
	mergeable_state=dirty, but doing conflict resolution first is
	cheaper and avoids spurious ERROR alerts)."""
	text = _poller_text()
	conflict_idx = text.find("Standalone PR conflict sweep")
	noop_idx = text.find(SWEEP_HEADER)
	assert conflict_idx != -1 and noop_idx != -1
	assert conflict_idx < noop_idx, (
		"Conflict sweep must precede the noop-suspicious sweep in the script."
	)


def test_sweep_emits_summary_line():
	"""Every poll cycle must log how many PRs were dispatched /
	force-merged / blocked so operators can grep `Noop-suspicious
	recovery complete` in the orchestrator log."""
	sweep = _sweep_block()
	assert "Noop-suspicious recovery complete" in sweep


def main() -> int:
	# Direct `python3 tests/<file>.py` entrypoint — the repo's CI runs
	# tests via that pattern rather than pytest discovery, so without
	# this block the contract assertions never execute under CI.
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	passed = 0
	failed = 0

	for func in test_funcs:
		name = func.__name__
		try:
			func()
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
