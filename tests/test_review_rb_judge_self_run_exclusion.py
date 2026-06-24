#!/usr/bin/env python3
"""Regression tests for the merge_with_followup self-deadlock fix.

Background
----------
scripts/review_rb_judge.sh runs inside the `review / codex-agent` job
hosted by .github/workflows/review_autofix.yml (or its dispatch wrapper
review_rb_judge_dispatch.yml). When the judge decides
`merge_with_followup`, the script queries
`/repos/{owner}/{repo}/commits/{head_sha}/check-runs` and refuses the
merge while ANY check-run is still incomplete.

Before this fix, that query counted the rb_judge's own host job — itself
a check-run on the PR head SHA with `status=in_progress` — as a blocking
check-run, so the gate ALWAYS refused merge_with_followup. The workflow
then fell through to the generic "max autofix iterations" review-blocked
comment, which misled operators into thinking the iteration cap was the
proximate cause. The orchestrator's `_pr_checks_completed` helper in
scripts/orchestrate_poll_process.sh has the same shape but does not need
this exclusion because it runs from a separate workflow (the orchestrate
poller), whose host job is never on the polled SHA's check-run list.

End-to-end evidence: shubhodeep1/tele-funtoken-msg-scoring PR #2989,
run 25993440211, log line "PR #2989 has 1 blocking check-run(s) for SHA
c52b838 — refusing merge_with_followup" emitted at 14:32:32Z while the
hosting codex-agent job ran from 14:23:47Z to 14:33:01Z.

The fix
-------
1. scripts/review_rb_judge.sh captures ${GITHUB_RUN_ID} into _self_run_id
   and threads it into the jq filter via --arg self_run. A new
   `_is_self_check_run` predicate matches Actions-emitted check-runs by
   their `details_url` (always `/actions/runs/<run_id>/job/<job_id>` for
   Actions check-runs). The `select` clauses now AND the existing
   pending-status predicate with `(_is_self_check_run | not)`. Empty
   $self_run preserves legacy behavior for non-Actions / test callers.
2. Each refusal path in the merge_with_followup branch now emits a
   distinct `judge_skip_reason`, including the pre-existing
   `missing_followup_details` / `followup_issue_create_failed` paths and
   the seven merge-gate refusal reasons added by this fix, so the
   workflow can distinguish them from a true "max iterations" exhaustion.
3. .github/workflows/review_autofix.yml's review-blocked PR-comment and
   Telegram-notification steps now branch on JUDGE_SKIP_REASON and post
   reason-specific bodies. Empty JUDGE_SKIP_REASON falls through to the
   original "max iterations" messaging for backward compatibility.

These tests pin both the script-side filter shape and the workflow-side
reason routing so a future refactor cannot silently re-introduce the
self-deadlock or the misleading fallback comment.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RB_JUDGE_SCRIPT = REPO_ROOT / "scripts" / "review_rb_judge.sh"
# The check-runs jq filter + self-run exclusion now live in the shared
# scripts/pr_checks_lib.sh (_pr_checks_completed), sourced by both
# review_rb_judge.sh and orchestrate_poll_process.sh so the two merge gates
# cannot drift. The script-side filter-shape assertions therefore pin the
# library; review_rb_judge.sh only has to thread GITHUB_RUN_ID into it.
PR_CHECKS_LIB_SCRIPT = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
TEST_SUBPROCESS_ENV = {
	"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
	"PYTHONDONTWRITEBYTECODE": "1",
}


def _rb_judge_text() -> str:
	return RB_JUDGE_SCRIPT.read_text(encoding="utf-8")


def _pr_checks_lib_text() -> str:
	return PR_CHECKS_LIB_SCRIPT.read_text(encoding="utf-8")


def _review_autofix_text() -> str:
	return REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")


def _rb_judge_local_sanitize_fallback_block() -> str:
	src = _rb_judge_text()
	start = src.index("if ! command -v sanitize_codex_prompt_file >/dev/null 2>&1; then")
	end = src.index("if ! command -v _init_prompt_budget >/dev/null 2>&1; then", start)
	block = src[start:end]
	assert block.count("if ! command -v sanitize_codex_prompt_file >/dev/null 2>&1; then") == 1
	assert "sanitize_codex_prompt_file() {" in block
	assert "Local prompt sanitization fallback could not sanitize" in block
	return block


# ---------------------------------------------------------------------------
# Script-side: self-run exclusion in the check-runs jq filter.
# ---------------------------------------------------------------------------


def test_script_captures_github_run_id() -> None:
	"""scripts/review_rb_judge.sh must thread ${GITHUB_RUN_ID:-} into the
	shared check-runs gate via PR_CHECKS_SELF_RUN_ID. A future refactor
	that drops it would re-deadlock the merge_with_followup path because
	the gate's jq filter would have no value to compare details_url
	against."""
	src = _rb_judge_text()
	assert 'PR_CHECKS_SELF_RUN_ID="${GITHUB_RUN_ID:-}"' in src, (
		"scripts/review_rb_judge.sh must pass PR_CHECKS_SELF_RUN_ID="
		"\"${GITHUB_RUN_ID:-}\" into the shared _pr_checks_completed gate "
		"so it can exclude this script's own host GitHub Actions run from "
		"the blocking-check-run count. Without it, the merge_with_followup "
		"path self-deadlocks (see tele-funtoken-msg-scoring PR #2989)."
	)


def test_script_calls_shared_gate_with_base_ref() -> None:
	"""scripts/review_rb_judge.sh must delegate to the shared
	_pr_checks_completed helper, passing the PR's base ref (3rd arg) so the
	required-checks filter is applied. This is what unblocks the merge when
	a non-required/environmental check (e.g. CodeQL with code scanning
	disabled) is permanently red."""
	src = _rb_judge_text()
	pat = re.compile(
		r'_pr_checks_completed "\$\{PR_NUMBER\}" "\$\{PR_HEAD_SHA\}" "\$\{PR_BASE_REF\}"'
	)
	assert pat.search(src), (
		"scripts/review_rb_judge.sh's merge_with_followup gate must call "
		"`_pr_checks_completed \"${PR_NUMBER}\" \"${PR_HEAD_SHA}\" "
		"\"${PR_BASE_REF}\"` (the shared helper from scripts/pr_checks_lib.sh) "
		"with the base ref so non-required/advisory failing checks no longer "
		"block the review-blocked-judge merge."
	)


def test_script_threads_self_run_into_jq_arg() -> None:
	"""The shared library's jq invocation that counts incomplete check-runs
	must pass the self-run id via --arg self_run. Without the --arg
	threading, the predicate has no value to compare and the gate
	self-deadlocks."""
	src = _pr_checks_lib_text()
	assert 'jq -r --arg self_run "${self_run}"' in src, (
		"scripts/pr_checks_lib.sh's legacy check-runs branch must invoke "
		"jq with `--arg self_run \"${self_run}\"` so the filter can "
		"identify and exclude the rb_judge's own host Actions run."
	)
	assert '--arg self_run "${self_run}"' in src, (
		"scripts/pr_checks_lib.sh must thread the self-run id into the jq "
		"filter (both the legacy and required-set branches)."
	)


def test_script_defines_self_check_run_predicate() -> None:
	"""The shared library's jq filter must define a _is_self_check_run
	predicate that matches an Actions check-run's details_url against the
	captured run id. The /actions/runs/<id>(/|$) anchor prevents prefix
	collisions (e.g. run id 12 matching run 123)."""
	src = _pr_checks_lib_text()
	assert (
		'def _is_self_check_run: ($self_run != "") and '
		'((.details_url // "") | test("/actions/runs/" + $self_run + "(/|$)"));'
	) in src, (
		"scripts/pr_checks_lib.sh must define _is_self_check_run "
		"in the check-runs jq filter exactly as documented; the "
		"empty-$self_run guard preserves legacy behavior for non-Actions "
		"callers and the (/|$) anchor prevents run-id prefix collisions."
	)


def test_script_excludes_self_in_both_jq_branches() -> None:
	"""The shared library must apply the self-run exclusion in BOTH the
	legacy (block-on-any) filter and the required-set filter. Dropping the
	predicate from either would leave a path where the gate self-deadlocks."""
	src = _pr_checks_lib_text()
	# Legacy "*" branch: AND _is_pending with the negated self-check
	# (paginated array + legacy single-object shapes => >=2 occurrences).
	legacy = "select(_is_pending and (_is_self_check_run | not))"
	legacy_occurrences = src.count(legacy)
	assert legacy_occurrences >= 2, (
		f"scripts/pr_checks_lib.sh's legacy filter must apply the self-run "
		f"exclusion in both shapes. Found {legacy_occurrences} occurrence(s) "
		f"of `{legacy}`; expected at least 2."
	)
	# Required-set branch: the select must AND the required-failure test
	# with the negated self-check.
	assert "and (_is_self_check_run | not)" in src, (
		"scripts/pr_checks_lib.sh's required-set filter must also exclude "
		"the self-run from the blocking count (`and (_is_self_check_run | "
		"not)`)."
	)


def test_local_sanitize_fallback_warns_when_all_rewrite_paths_fail() -> None:
	"""The local sanitize fallback keeps the shared helper's best-effort
	contract, but degraded harnesses still need a warning when every rewrite
	path fails so a later codex stdin error is diagnosable."""
	with tempfile.TemporaryDirectory(prefix="rb_judge_sanitize_") as td:
		prompt_file = Path(td) / "invalid_prompt.txt"
		prompt_file.write_bytes(b"\xff\xfe\xfa")
		result = subprocess.run(
			[
				"bash",
				"-c",
				(
					f"{_rb_judge_local_sanitize_fallback_block()}\n"
					"iconv() { return 1; }\n"
					"python3() { return 1; }\n"
					f"sanitize_codex_prompt_file {shlex.quote(str(prompt_file))}\n"
				),
			],
			cwd=str(REPO_ROOT),
			env=TEST_SUBPROCESS_ENV,
			capture_output=True,
			text=True,
			check=True,
		)

		assert prompt_file.read_bytes() == b"\xff\xfe\xfa"
		assert "Local prompt sanitization fallback could not sanitize" in result.stderr, (
			"scripts/review_rb_judge.sh must warn when the degraded local prompt "
			"sanitizer exhausts both rewrite paths and has to leave the original "
			"bytes in place."
		)


def test_local_sanitize_fallback_warns_when_tempfile_allocation_fails() -> None:
	"""Tempfile allocation failure also leaves the original prompt bytes in
	place, so degraded harnesses need the same warning instead of a silent
	fall-through to the later Codex read failure."""
	with tempfile.TemporaryDirectory(prefix="rb_judge_sanitize_") as td:
		prompt_file = Path(td) / "invalid_prompt.txt"
		prompt_file.write_bytes(b"\xff\xfe\xfa")
		result = subprocess.run(
			[
				"bash",
				"-c",
				(
					f"{_rb_judge_local_sanitize_fallback_block()}\n"
					"mktemp() { return 1; }\n"
					f"sanitize_codex_prompt_file {shlex.quote(str(prompt_file))}\n"
				),
			],
			cwd=str(REPO_ROOT),
			env=TEST_SUBPROCESS_ENV,
			capture_output=True,
			text=True,
			check=True,
		)

		assert prompt_file.read_bytes() == b"\xff\xfe\xfa"
		assert "Local prompt sanitization fallback could not sanitize" in result.stderr, (
			"scripts/review_rb_judge.sh must warn when the degraded local prompt "
			"sanitizer cannot even allocate its temp file and has to leave the "
			"original bytes in place."
		)


def test_local_sanitize_fallback_warns_when_replace_fails() -> None:
	"""A failed in-place replace also leaves the original prompt bytes in
	place, so degraded harnesses need the same warning instead of a silent
	fall-through to the later Codex read failure."""
	with tempfile.TemporaryDirectory(prefix="rb_judge_sanitize_") as td:
		prompt_file = Path(td) / "invalid_prompt.txt"
		prompt_file.write_bytes(b"\xff\xfe\xfa")
		result = subprocess.run(
			[
				"bash",
				"-c",
				(
					f"{_rb_judge_local_sanitize_fallback_block()}\n"
					"iconv() { printf sanitized; }\n"
					"mv() { return 1; }\n"
					f"sanitize_codex_prompt_file {shlex.quote(str(prompt_file))}\n"
				),
			],
			cwd=str(REPO_ROOT),
			env=TEST_SUBPROCESS_ENV,
			capture_output=True,
			text=True,
			check=True,
		)

		assert prompt_file.read_bytes() == b"\xff\xfe\xfa"
		assert "Local prompt sanitization fallback could not sanitize" in result.stderr, (
			"scripts/review_rb_judge.sh must warn when the degraded local prompt "
			"sanitizer cannot replace the original file and has to leave the "
			"original bytes in place."
		)


# ---------------------------------------------------------------------------
# Script-side: reason emission for every merge_with_followup refusal path.
# ---------------------------------------------------------------------------


EXPECTED_MERGE_WITH_FOLLOWUP_SKIP_REASONS = {
	"missing_followup_details",
	"unresolved_head_sha",
	"check_runs_query_failed",
	"blocking_check_runs",
	"sync_merge_failed",
	"auto_merge_disabled",
	"merge_conflict",
	"mergeability_pending",
	"followup_issue_create_failed",
}


def test_each_merge_with_followup_refusal_emits_distinct_skip_reason() -> None:
	"""Every refusal path inside the merge_with_followup branch must
	emit a distinct `judge_skip_reason` to $GITHUB_OUTPUT so the
	workflow's review-blocked fallback comment can distinguish them
	from "max iterations reached". Before this fix, these paths left
	judge_skip_reason empty and the workflow posted a misleading body."""
	src = _rb_judge_text()
	for reason in EXPECTED_MERGE_WITH_FOLLOWUP_SKIP_REASONS:
		needle = f'echo "judge_skip_reason={reason}" >> "$GITHUB_OUTPUT"'
		assert needle in src, (
			f"scripts/review_rb_judge.sh must emit "
			f"`judge_skip_reason={reason}` from the corresponding "
			f"merge_with_followup refusal path. Without it the workflow "
			f"falls through to the generic max-iterations body, which "
			f"is what confused operators on tele-funtoken-msg-scoring "
			f"PR #2989."
		)


# ---------------------------------------------------------------------------
# Workflow-side: review-blocked comment routes on JUDGE_SKIP_REASON.
# ---------------------------------------------------------------------------


def test_workflow_threads_judge_skip_reason_into_comment_step() -> None:
	"""The `Post review-blocked comment on PR (autofix exhaustion)` step
	must read steps.rb_judge.outputs.judge_skip_reason as env. Without
	this plumbing the case branches below cannot fire."""
	wf = _review_autofix_text()
	# Anchor on the step name to scope the search to this specific step.
	step_anchor = "- name: Post review-blocked comment on PR (autofix exhaustion)"
	idx = wf.find(step_anchor)
	assert idx >= 0, (
		"`Post review-blocked comment on PR (autofix exhaustion)` step "
		"must exist in .github/workflows/review_autofix.yml; the fix "
		"depends on it being present to route the reason-specific body."
	)
	# Look at the next ~120 lines following the step header (the step
	# body) for the env plumbing.
	step_body = wf[idx:idx + 6000]
	assert "JUDGE_SKIP_REASON: ${{ steps.rb_judge.outputs.judge_skip_reason }}" in step_body, (
		"`Post review-blocked comment on PR (autofix exhaustion)` step "
		"must thread `JUDGE_SKIP_REASON: ${{ steps.rb_judge.outputs."
		"judge_skip_reason }}` as env so the bash case below can route "
		"the body."
	)


def test_workflow_branches_comment_body_on_each_reason() -> None:
	"""The fallback comment step's case statement must include a branch
	for each merge_with_followup skip reason emitted by the script.
	Missing a branch means that reason falls through to the generic
	"max iterations" body, re-introducing the misleading-comment bug."""
	wf = _review_autofix_text()
	step_anchor = "- name: Post review-blocked comment on PR (autofix exhaustion)"
	idx = wf.find(step_anchor)
	assert idx >= 0
	# Scope to the step body (until the next "- name:" sibling).
	next_step = wf.find("\n      - name:", idx + len(step_anchor))
	step_body = wf[idx:next_step if next_step > 0 else len(wf)]

	for reason in EXPECTED_MERGE_WITH_FOLLOWUP_SKIP_REASONS:
		# Match the case label "    reason)" line — leading-whitespace
		# is workflow-indentation-dependent, just check the token.
		assert re.search(rf"\n\s*{re.escape(reason)}\)\s*\n", step_body), (
			f"`Post review-blocked comment on PR (autofix exhaustion)` "
			f"step must include a `case` branch for "
			f"`JUDGE_SKIP_REASON={reason}` so that reason no longer "
			f"falls through to the generic max-iterations body."
		)


def test_workflow_branches_telegram_message_on_each_reason() -> None:
	"""The Telegram review-blocked notification must mirror the
	reason-specific routing so merge_with_followup refusal reasons do not
	fall through to the generic autofix-exhausted alert."""
	wf = _review_autofix_text()
	step_anchor = "- name: Telegram review-blocked judge decision"
	idx = wf.find(step_anchor)
	assert idx >= 0
	next_step = wf.find("\n      - name:", idx + len(step_anchor))
	step_body = wf[idx:next_step if next_step > 0 else len(wf)]

	for reason in EXPECTED_MERGE_WITH_FOLLOWUP_SKIP_REASONS:
		assert re.search(rf"\n\s*{re.escape(reason)}\)\s*\n", step_body), (
			f"`Telegram review-blocked judge decision` step must include "
			f"a `case` branch for `JUDGE_SKIP_REASON={reason}` so "
			f"reason-specific refusals do not fall through to the generic "
			f"autofix-exhausted alert."
		)


def test_workflow_telegram_reason_messages_use_printf_for_newlines() -> None:
	"""Reason-specific Telegram branches must use printf so the embedded
	`\n` becomes a real newline before tg_send_msg URL-encodes the body."""
	wf = _review_autofix_text()
	step_anchor = "- name: Telegram review-blocked judge decision"
	idx = wf.find(step_anchor)
	assert idx >= 0
	next_step = wf.find("\n      - name:", idx + len(step_anchor))
	step_body = wf[idx:next_step if next_step > 0 else len(wf)]

	for reason in EXPECTED_MERGE_WITH_FOLLOWUP_SKIP_REASONS:
		assert re.search(
			rf'\n\s*{re.escape(reason)}\)\n\s+MSG="\$\(printf ',
			step_body,
		), (
			f"`Telegram review-blocked judge decision` step must format "
			f"`JUDGE_SKIP_REASON={reason}` via `printf` so Telegram gets "
			f"a real newline instead of a literal \\n sequence."
		)


def test_workflow_preserves_max_iterations_fallback() -> None:
	"""The default `*)` branch of the case must preserve the original
	"maximum number of autofix iterations" body so legacy callers (no
	judge_skip_reason set) keep the same behavior. This is the only
	backward-compatibility lever for the workflow change."""
	wf = _review_autofix_text()
	assert "maximum number of autofix iterations" in wf, (
		"The `*)` (default) branch of the comment step's case statement "
		"must keep the original `maximum number of autofix iterations` "
		"body for backward compatibility with pre-fix script callers "
		"that don't set judge_skip_reason."
	)


def test_workflow_merge_conflict_comment_is_branch_agnostic() -> None:
	"""The merge-conflict remediation text must not hardcode `main`
	because review_autofix runs on arbitrary base branches."""
	wf = _review_autofix_text()
	step_anchor = "- name: Post review-blocked comment on PR (autofix exhaustion)"
	idx = wf.find(step_anchor)
	assert idx >= 0
	next_step = wf.find("\n      - name:", idx + len(step_anchor))
	step_body = wf[idx:next_step if next_step > 0 else len(wf)]
	assert "merge the base branch" in step_body
	assert "merge in main" not in step_body


# ---------------------------------------------------------------------------
# Runtime: actually invoke jq on synthetic fixtures and verify the count.
# ---------------------------------------------------------------------------


# Snapshot of the production jq filter (the legacy block-on-any shape now
# hosted in scripts/pr_checks_lib.sh's "*" branch). The text-shape tests
# above are the change-detection guard against drift; keep this copy in
# sync with scripts/pr_checks_lib.sh when the live filter changes.
JQ_FILTER = '''
def _is_self_check_run: ($self_run != "") and ((.details_url // "") | test("/actions/runs/" + $self_run + "(/|$)"));
def _is_pending: .status != "completed" or (.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped" and .conclusion != "cancelled");
if (type == "array") then
  [.[]? | (.check_runs // [])[] | select(_is_pending and (_is_self_check_run | not))] | length
elif (type == "object" and (.check_runs | type == "array")) then
  [.check_runs[] | select(_is_pending and (_is_self_check_run | not))] | length
else
  empty
end
'''


def _run_jq(payload: object, self_run: str) -> str:
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")
	result = subprocess.run(
		["jq", "-r", "--arg", "self_run", self_run, JQ_FILTER],
		input=json.dumps(payload),
		capture_output=True,
		text=True,
		check=True,
	)
	return result.stdout.strip()


def test_run_jq_missing_binary_raises_skiptest() -> None:
	original_which = shutil.which
	try:
		shutil.which = lambda _binary: None
		try:
			_run_jq([], "")
		except unittest.SkipTest as exc:
			assert "jq binary not available" in str(exc)
		else:
			raise AssertionError("_run_jq() must raise SkipTest when jq is unavailable")
	finally:
		shutil.which = original_which


def test_jq_filter_excludes_self_run_only_blocker() -> None:
	"""Reproduces the tele-funtoken-msg-scoring PR #2989 scenario: the
	only in_progress check-run is the rb_judge's own host job. After
	exclusion the gate should see 0 blocking check-runs and let the
	merge proceed."""
	payload = [
		{
			"check_runs": [
				{
					"name": "review / codex-agent",
					"status": "in_progress",
					"conclusion": None,
					"details_url": "https://github.com/owner/repo/actions/runs/12345/job/789",
				},
				{
					"name": "other-ci",
					"status": "completed",
					"conclusion": "success",
					"details_url": "https://github.com/owner/repo/actions/runs/99999/job/000",
				},
			]
		}
	]
	assert _run_jq(payload, "12345") == "0"


def test_jq_filter_keeps_genuine_blocker_after_self_exclusion() -> None:
	"""The exclusion must be narrow — only the matching self run is
	removed. A second still-running CI check on the same SHA must still
	count as 1 blocker so the gate refuses the merge."""
	payload = [
		{
			"check_runs": [
				{
					"name": "review / codex-agent",
					"status": "in_progress",
					"conclusion": None,
					"details_url": "https://github.com/owner/repo/actions/runs/12345/job/789",
				},
				{
					"name": "blocking-ci",
					"status": "in_progress",
					"conclusion": None,
					"details_url": "https://github.com/owner/repo/actions/runs/99999/job/000",
				},
			]
		}
	]
	assert _run_jq(payload, "12345") == "1"


def test_jq_filter_legacy_single_object_shape_without_self_run() -> None:
	"""Empty $self_run (non-Actions callers, tests, or legacy invocation)
	must disable the exclusion entirely so the gate behaves exactly as
	before this fix — preserving backward compatibility."""
	payload = {
		"check_runs": [
			{
				"name": "ci-1",
				"status": "in_progress",
				"conclusion": None,
				"details_url": "https://github.com/owner/repo/actions/runs/77/job/1",
			},
			{
				"name": "ci-2",
				"status": "completed",
				"conclusion": "success",
				"details_url": None,
			},
		]
	}
	assert _run_jq(payload, "") == "1"


def test_jq_filter_self_run_prefix_does_not_match() -> None:
	"""The (/|$) anchor in the details_url regex must prevent run-id
	prefix collisions: run id 12 must NOT match a check-run pointing at
	run 123. Without the anchor, a numerically-shorter $self_run could
	accidentally exclude an unrelated check-run and let the gate pass
	while real blockers are still running."""
	payload = [
		{
			"check_runs": [
				{
					"name": "blocking-ci",
					"status": "in_progress",
					"conclusion": None,
					"details_url": "https://github.com/owner/repo/actions/runs/123/job/1",
				},
			]
		}
	]
	# self_run="12" must NOT match the run-id "123".
	assert _run_jq(payload, "12") == "1"


def test_jq_filter_no_self_collision_on_non_actions_details_url() -> None:
	"""Non-Actions check-runs (third-party CI integrations) have
	details_urls that don't carry /actions/runs/<id>/job/<id>. The
	self-exclusion must NOT match them — they must still count as
	blockers per their status."""
	payload = [
		{
			"check_runs": [
				{
					"name": "third-party-ci",
					"status": "in_progress",
					"conclusion": None,
					"details_url": "https://third-party-ci.example.com/builds/abc",
				},
			]
		}
	]
	assert _run_jq(payload, "12345") == "1"


def test_local_sanitize_fallback_rewrites_all_invalid_utf8_like_shared_helper() -> None:
	"""The degraded local fallback should mirror the shared helper's
	best-effort contract for all-invalid input by leaving valid UTF-8 on
	disk, even when every invalid byte is discarded."""
	with tempfile.TemporaryDirectory(prefix="rb_judge_sanitize_") as td:
		prompt_file = Path(td) / "invalid_prompt.txt"
		prompt_file.write_bytes(b"\xff\xfe\xfa")
		subprocess.run(
			[
				"bash",
				"-c",
				(
					f"{_rb_judge_local_sanitize_fallback_block()}\n"
					"iconv() { return 1; }\n"
					f"sanitize_codex_prompt_file {shlex.quote(str(prompt_file))}\n"
				),
			],
			cwd=str(REPO_ROOT),
			env=TEST_SUBPROCESS_ENV,
			capture_output=True,
			text=True,
			check=True,
		)
		assert prompt_file.read_bytes() == b""


def main() -> int:
	# Direct `python3 tests/<file>.py` entrypoint — sibling review-blocked
	# regression modules use this harness, and allowlisted contract tests
	# invoke them directly to ensure the assertions actually execute.
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	skipped = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except unittest.SkipTest as exc:
			print(f"  SKIP  {name}: {exc}")
			skipped += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {skipped} skipped, {failed} failed, {passed + skipped + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
