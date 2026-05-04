#!/usr/bin/env python3
"""Contract tests for the review_autofix codex-log capture pattern.

Mirrors tests/test_implement_post_codex_recovery.py::
test_failure_log_artifact_upload_contract for the parallel steps added to
.github/workflows/review_autofix.yml's codex-agent job (PR #2086 against
the editor empty-output failure mode).

Why a literal contract test:
- review_autofix is fail-open on editor empty-output. When gpt-5.3-codex
  completes with 0-byte stdout the job exits success() and the only
  remaining trace of the failure is whatever the staging step copied
  before the cleanup step's rm -rf. A rename of the gate, artifact name,
  staging-dir name, or cleanup guard would silently break the diagnostic
  path with no other CI signal.
- The implement.yml equivalent is pinned by
  test_implement_post_codex_recovery.py — this test maintains parity so
  drift between the two workflows is also caught.

The test does NOT execute the run: block (the steps are mostly cp/mkdir
and a final github.env write); it asserts on workflow text shape, which
is the right granularity for a "did anyone refactor this away" guard.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


def _workflow_text() -> str:
	return REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")


def _step_block(step_name: str) -> list[str]:
	lines = _workflow_text().splitlines()
	needle = f"- name: {step_name}"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		step_indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:"):
				indent = len(candidate) - len(candidate.lstrip(" "))
				if indent == step_indent:
					end = j
					break
		return lines[idx:end]
	raise AssertionError(f"Step not found in workflow: {step_name}")


def _step_block_text(step_name: str) -> str:
	return "\n".join(_step_block(step_name))


def test_review_autofix_failure_log_artifact_upload_contract() -> None:
	"""Pin the editor-empty-output codex-log artifact upload added in #2086.

	The artifact captures per-attempt editor stdout/stderr (the only
	place the OpenRouter response / finish_reason / tool-router rejection
	surfaces for the gpt-5.3-codex empty-output failure documented in
	scripts/review_apply_fixes.sh:947–951 and the workflow's own
	"Switch reasoning effort for editor" step), the editor / reviewer /
	conflict-resolver prompts and summaries, the advisory editor inputs
	(floor_tags, review_issues, ledger_status, consolidator_raw,
	parser_stats), and the codex CLI's own ~/.codex/log directory.
	"""
	stage_block = _step_block_text(
		"Stage codex logs for upload (failure or empty-editor)"
	)
	upload_block = _step_block_text(
		"Upload codex logs (failure or empty-editor)"
	)
	cleanup_block = _step_block_text("Cleanup temporary artifacts")

	# The gate is intentionally broader than implement.yml's
	# (failure() || cancelled()) because review_autofix is fail-open on
	# editor empty-output: when codex returns 0-byte stdout the job
	# posts the noop-comment path and exits success(), so a plain
	# failure gate would miss the case this artifact exists for.
	# EDITOR_NOOP_SUSPICIOUS / EDITOR_CHANGES_LOST are the existing
	# flags downstream "Telegram editor-noop-suspicious" /
	# "Telegram editor-changes-lost" alerts already key off, so the
	# capture stays aligned with those alerts.
	assert (
		"if: always() && env.RUNTIME_DIR != '' && (failure() || cancelled() "
		"|| env.EDITOR_NOOP_SUSPICIOUS == 'true' || "
		"env.EDITOR_CHANGES_LOST == 'true')"
	) in stage_block, (
		"Stage step gate must include EDITOR_NOOP_SUSPICIOUS and "
		"EDITOR_CHANGES_LOST so the success()-with-empty-editor case "
		"this artifact targets is actually captured"
	)
	assert (
		"if: always() && env.CODEX_REVIEW_AUTOFIX_FAILURE_LOG_DIR != ''"
	) in upload_block, (
		"Upload step must gate on the env var the staging step exports "
		"so the upload doesn't fire when staging was skipped"
	)

	# Staging dir must live under RUNNER_TEMP, not directly in
	# RUNTIME_DIR, so it survives the cleanup step's
	# rm -rf "${RUNTIME_DIR}".
	assert (
		'STAGE_DIR="${RUNNER_TEMP:-/tmp}/codex-review-autofix-failure-logs"'
	) in stage_block, (
		"Staging dir must be rooted at RUNNER_TEMP so the existing "
		"Cleanup step that wipes RUNTIME_DIR doesn't also wipe the "
		"about-to-be-uploaded files"
	)

	# Per-attempt editor stdout / stderr is the primary signal — pin
	# the globs explicitly so a refactor that drops them fails the
	# test. review_apply_fixes.sh writes editor_attempt_${attempt}.txt
	# (codex stdout) and .err (codex stderr) for every retry; without
	# them the only trace of why codex returned 0 bytes is the
	# truncated stderr fragment in the live job log.
	assert "editor_attempt_*.txt" in stage_block, (
		"Per-attempt editor stdout (editor_attempt_*.txt) is the "
		"primary artefact — it holds the codex stdout for every retry"
	)
	assert "editor_attempt_*.err" in stage_block, (
		"Per-attempt editor stderr (editor_attempt_*.err) is the "
		"primary artefact — it holds the codex stderr for every retry"
	)
	assert "editor_attempt_*_changes_lost.txt" in stage_block, (
		"editor_attempt_*_changes_lost.txt must be staged so the "
		"claimed-changes-lost failure mode is also debuggable"
	)

	# Top-level prompt / summary / context files. Without these a cold
	# post-mortem can't reconstruct what context the editor saw.
	# The reviewer_prompt_pass1.txt / reviewer_prompt_pass2.txt /
	# cross_pollination_summary.txt files are the actual prompts the
	# two-pass reviewer pipeline uses (review_run_reviewers.sh:1133,
	# :1155, :1160) — without them a reviewer-side empty-output
	# failure is undebuggable because reviewer_prompt.txt is just the
	# legacy single-pass artefact (Copilot review on #2086).
	# reviewer_prompt_body.txt is the un-rendered template that
	# assemble_reviewer_prompt() uses to build pass1/pass2 — pinned so
	# template-side regressions are still attributable from the
	# artifact alone (claude-branch-review on #2086).
	for f in (
		"editor_summary.txt",
		"editor_prompt.txt",
		"editor_prompt_body.txt",
		"pr_editor_comment.txt",
		"reviewer_consensus.txt",
		"reviewer_prompt.txt",
		"reviewer_prompt_body.txt",
		"reviewer_prompt_pass1.txt",
		"reviewer_prompt_pass2.txt",
		"cross_pollination_summary.txt",
		"reviewer_bundle.txt",
		"reviewer_manifest.txt",
		"conflict_resolver_summary.txt",
		"conflict_resolver_prompt.txt",
		"committed_files.txt",
		"pr_check_runs_context.txt",
		"pr_meta.json",
		"pr_all_comments_context.txt",
		"linked_issue_context.txt",
		"memory_context.txt",
		"pr_diff.patch",
		"original_pr_diff.patch",
		"last_run_diff.patch",
		"last_run_changed_files.txt",
		"pr_changed_files.txt",
		"last_commit_stat.txt",
		"last_run_diff_stat.txt",
		"symbol_diff_summary.txt",
	):
		assert f in stage_block, (
			f"Staging step must copy {f} so post-mortem has the full "
			f"context block, not just per-attempt editor stderr"
		)

	# Advisory editor inputs declared at the top of
	# scripts/review_apply_fixes.sh (FLOOR_TAGS_FILE, REVIEW_ISSUES_FILE,
	# LEDGER_STATUS_FILE, CONSOLIDATOR_RAW_FILE, PARSER_STATS_FILE).
	# These are what the editor actually reads from RUNTIME_DIR; without
	# them we can't tell whether bad consolidator/parser output was the
	# upstream cause of an empty editor turn (Copilot review on #2086).
	for f in (
		"floor_tags.txt",
		"review_issues.txt",
		"ledger_status.txt",
		"consolidator_raw.txt",
		"parser_stats.txt",
	):
		assert f in stage_block, (
			f"Staging step must copy {f} — it's a documented advisory "
			f"editor input in scripts/review_apply_fixes.sh, missing it "
			f"would leave the empty-output post-mortem unable to "
			f"distinguish 'editor saw bad input' from 'codex empty turn'"
		)

	# Reviewer-fanout / two-pass / consensus artefacts. The codex
	# stderr for each reviewer is preserved as ${PREVIOUS_REVIEWS_DIR}/
	# review_<model>.log by review_run_reviewers.sh:736 BEFORE the
	# reviewer's ephemeral CODEX_HOME is wiped, so these globs are the
	# correct surrogate for the reviewer wire trace. The pass1_*.log /
	# pass2_*.log files are the per-pass codex stderr; cache_probe_*
	# captures the prompt-cache HTTP probe diagnostics
	# (review_run_reviewers.sh:114-117). All four .log/cache_probe
	# globs were pinned in response to the claude-branch-review on
	# #2086 — without them a refactor that drops them would lose
	# diagnostics with no CI signal. Note: there is no review_*.err
	# glob — reviewers produce .log not .err (the .err extension is
	# editor-only), and the reviewer script never writes review_*.err.
	for glob in (
		"review_*.txt",
		"review_*.log",
		"pass1_*.txt",
		"pass1_*.log",
		"pass2_*.txt",
		"pass2_*.log",
		"status_*.txt",
		"consensus_pass1.txt",
		"cache_probe_*.txt",
		"cache_probe_*.log",
	):
		assert glob in stage_block, (
			f"Staging step must include reviewer-fanout glob {glob} "
			f"so reviewer-side codex failures (not just editor) are "
			f"diagnosable from the artifact"
		)
	# Anti-assertion: the dead review_*.err glob must NOT be staged.
	# review_run_reviewers.sh produces .log files for reviewer stderr,
	# never .err — the .err extension is reserved for editor attempts
	# (review_apply_fixes.sh:964). A stale review_*.err glob is a
	# harmless no-op but actively misleading to future maintainers.
	# claude-branch-review consensus across 6 reviewers flagged this
	# on #2086.
	assert '"${PREVIOUS_REVIEWS_DIR}"/review_*.err' not in stage_block, (
		"Staging step must NOT include review_*.err: reviewers produce "
		".log not .err. The dead glob misleads readers into thinking "
		"reviewer stderr lives in .err files when it actually lives "
		"in review_*.log."
	)

	# ~/.codex/log must be copied WITHOUT a wall-clock filter — same
	# rationale as the implement.yml equivalent. A single review_autofix
	# job can run up to ~3 hours and a single editor attempt up to
	# EDITOR_MAX_WALL=3300s, so any -mmin -N filter risks dropping the
	# earliest session log on a multi-retry failure — exactly the case
	# this artifact exists for.
	assert "${CODEX_HOME:-$HOME/.codex}/log" in stage_block, (
		"Staging step must copy ~/.codex/log (the codex CLI's own "
		"session log directory — raw HTTP wire logs)"
	)
	assert "-mmin" not in stage_block, (
		"Do NOT use a -mmin filter on the ~/.codex/log copy: a single "
		"review_autofix job can run up to ~3 hours and a single editor "
		"attempt up to EDITOR_MAX_WALL=3300s, so any wall-clock filter "
		"risks dropping the earliest session log on a multi-retry "
		"failure — exactly the case this artifact exists for"
	)

	# Artifact name must be unique per (run_id, run_attempt) so a
	# rerun-failed-jobs invocation doesn't 409 on a duplicate name.
	# Mirrors implement.yml's equivalent assertion.
	assert (
		"codex-review-autofix-failure-logs-${{ github.run_id }}-"
		"${{ github.run_attempt }}"
	) in upload_block, (
		"Artifact name must be unique per (run_id, run_attempt) so "
		"rerun-failed-jobs doesn't 409 on a duplicate name"
	)
	assert "if-no-files-found: ignore" in upload_block, (
		"Upload step must fail-open on empty staging dir so an early-"
		"skip failure path doesn't add a second cascading failure"
	)
	assert "retention-days:" in upload_block, (
		"Upload step must declare retention-days explicitly (artifacts "
		"can otherwise hit the org-default ceiling and get pruned at "
		"unpredictable times)"
	)

	# Cleanup step must rm the staging dir on a warm runner so the
	# next review_autofix job on the same runner doesn't see a stale
	# CODEX_REVIEW_AUTOFIX_FAILURE_LOG_DIR pointing at a previous run's
	# files. Mirrors implement.yml's equivalent assertion.
	assert "CODEX_REVIEW_AUTOFIX_FAILURE_LOG_DIR" in cleanup_block, (
		"Cleanup step must remove CODEX_REVIEW_AUTOFIX_FAILURE_LOG_DIR "
		"so warm runners don't leak stale staging dirs across "
		"review_autofix jobs"
	)
	assert "codex-review-autofix-failure-logs" in cleanup_block, (
		"Cleanup step's rm guard must reference the same staging-dir "
		"name as the staging step — drift between the two would leave "
		"stale dirs on warm runners"
	)


def main() -> int:
	test_review_autofix_failure_log_artifact_upload_contract()
	print("OK: review_autofix failure-log upload contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
