#!/usr/bin/env python3
"""Tests for the shared PR check-runs merge gate (scripts/pr_checks_lib.sh).

scripts/pr_checks_lib.sh hosts `_pr_checks_completed` and
`_pr_required_check_names_for_base`, the single gate shared by
scripts/orchestrate_poll_process.sh (the orchestrator's merge gates) and
scripts/review_rb_judge.sh (the review-blocked judge's merge_with_followup
gate). Before this library, the two gates carried independent copies and
drifted: the orchestrator filtered to required checks while the rb_judge
blocked on ANY failing check-run. That asymmetry deadlocked the
review-blocked-judge merge whenever a non-required / environmental check
(e.g. CodeQL when code scanning is disabled at the repo level) was
permanently red — the judge approved, the stricter gate refused, the issue
stayed ai:review-blocked, and stall recovery re-fired forever.

These tests pin the required-checks behaviour (the deadlock fix), the
self-run exclusion in BOTH filter branches, the reason side-channel, and the
drift guard that keeps the library default identical to the orchestrator's
ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _page(check_runs: list[dict]) -> str:
	"""Production --paginate --slurp shape: an array of page objects."""
	return json.dumps([{"check_runs": check_runs}])


def _run(
	body: str,
	*,
	runs_json: str = "{}",
	prot_json: str = "",
	pr_json: str = "{}",
	jq_fail_uri_encode: bool = False,
	env: dict[str, str] | None = None,
):
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")
	# Stub gh_retry/_safe_gh_jq so the gate reads fixtures instead of hitting
	# the network. The router keys off the API path the helper requests.
	jq_wrapper = ""
	if jq_fail_uri_encode:
		jq_wrapper = """
jq() {
  if [ "$#" -ge 2 ] && [ "$1" = "-sRr" ] && [ "$2" = "@uri" ]; then
    return 127
  fi
  command jq "$@"
}
"""
	preamble = f"""
set -uo pipefail
{jq_wrapper}
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
  case "$*" in
    *"/protection"*)
      if [ -n "${{PROT_EXPECT_PATH:-}}" ] && [ "$*" != "${{PROT_EXPECT_PATH}}" ]; then
        printf '%s' ''
      else
        printf '%s' "${{PROT_JSON}}"
      fi
      ;;
    *"/check-runs"*) printf '%s' "${{RUNS_JSON}}" ;;
    *"/pulls/"*) printf '%s' "${{PR_JSON}}" ;;
    *) printf '%s' '{{}}' ;;
  esac
}}
source {str(LIB)!r}
"""
	full_env = {
		"PATH": __import__("os").environ.get("PATH", "/usr/bin:/bin"),
		"PYTHONDONTWRITEBYTECODE": "1",
		"RUNS_JSON": runs_json,
		"PROT_JSON": prot_json,
		"PR_JSON": pr_json,
	}
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", preamble + body],
		env=full_env,
		capture_output=True,
		text=True,
		timeout=60,
	)


def _gate(
	*,
	runs_json: str,
	args: str,
	prot_json: str = "",
	pr_json: str = "{}",
	jq_fail_uri_encode: bool = False,
	env: dict[str, str] | None = None,
) -> tuple[int, str]:
	"""Run _pr_checks_completed and return (rc, PR_CHECKS_LAST_REASON)."""
	body = (
		f'_pr_checks_completed {args} >/dev/null 2>&1; rc=$?; '
		'printf "%s\\n" "${rc}:${PR_CHECKS_LAST_REASON}"'
	)
	res = _run(
		body,
		runs_json=runs_json,
		prot_json=prot_json,
		pr_json=pr_json,
		jq_fail_uri_encode=jq_fail_uri_encode,
		env=env,
	)
	assert res.returncode == 0, res.stderr
	rc_str, _, reason = res.stdout.strip().partition(":")
	return int(rc_str), reason


REPO_ENV = {"PR_CHECKS_REPOSITORY": "owner/repo"}


# ---------------------------------------------------------------------------
# The deadlock fix: required-filter lets non-required failures through.
# ---------------------------------------------------------------------------


def test_non_required_failing_check_does_not_block() -> None:
	"""A FAILED non-required/advisory check (e.g. CodeQL with code scanning
	disabled) must NOT block when a base ref is passed (required filter).
	This is the core deadlock fix."""
	runs = _page([
		{"name": "CI", "status": "completed", "conclusion": "success"},
		{"name": "Security CodeQL JS-TS", "status": "completed", "conclusion": "failure"},
	])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=REPO_ENV)
	assert rc == 0 and reason == "ok", (rc, reason)


def test_required_failing_check_blocks() -> None:
	"""A FAILED required check still blocks under the required filter."""
	runs = _page([{"name": "CI", "status": "completed", "conclusion": "failure"}])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_default_required_set_blocks_failing_ci_aggregate_job() -> None:
	"""The built-in default must name the real CI check-run. The CI workflow
	reports its jobs (`lint` is the aggregate that requires every other job to
	succeed), never a check-run called `CI`, so a failing `lint` with every
	other listed check green has to block under the default set (PR #6643
	merged red on 2026-10-09 because it did not)."""
	runs = _page([
		{"name": "lint", "status": "completed", "conclusion": "failure"},
		{"name": "static-checks", "status": "completed", "conclusion": "success"},
		{"name": "Integration PR readiness check", "status": "completed", "conclusion": "success"},
		{"name": "review / gate", "status": "completed", "conclusion": "success"},
	])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_pending_check_always_blocks_even_if_not_required() -> None:
	"""Pending check-runs always block (an in-flight workflow must not race
	the merge), regardless of whether they are in the required set."""
	runs = _page([{"name": "Optional", "status": "in_progress", "conclusion": None}])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_legacy_two_arg_mode_blocks_on_any_failure() -> None:
	"""Omitting the base ref (2-arg call) preserves the legacy
	block-on-ANY-failure behaviour for backward compatibility."""
	runs = _page([
		{"name": "CI", "status": "completed", "conclusion": "success"},
		{"name": "Security CodeQL JS-TS", "status": "completed", "conclusion": "failure"},
	])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234"', env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


# ---------------------------------------------------------------------------
# Cancelled conclusions block; only the newest run per name counts.
# ---------------------------------------------------------------------------


CI_REQUIRED_ENV = {**REPO_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI"}


def test_cancelled_required_check_blocks() -> None:
	"""A cancelled required check is a settled failure: it blocks and does
	not count as pending, so the auto-merge wait refuses instead of
	waiting."""
	runs = _page([{"id": 1, "name": "CI", "status": "completed", "conclusion": "cancelled"}])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=CI_REQUIRED_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)
	res = _run(
		'_pr_checks_completed 5 "abc1234" "main" >/dev/null 2>&1; '
		'printf "%s\\n" "${PR_CHECKS_LAST_PENDING}"',
		runs_json=runs,
		env=CI_REQUIRED_ENV,
	)
	assert res.returncode == 0, res.stderr
	assert res.stdout.strip() == "0", res.stdout


def test_cancelled_non_required_check_does_not_block_in_required_mode() -> None:
	runs = _page([
		{"id": 1, "name": "CI", "status": "completed", "conclusion": "success"},
		{"id": 2, "name": "Copilot", "status": "completed", "conclusion": "cancelled"},
	])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=CI_REQUIRED_ENV)
	assert rc == 0 and reason == "ok", (rc, reason)


def test_cancelled_run_superseded_by_newer_success_does_not_block() -> None:
	"""A re-run creates a new check-run with the same name; the cancelled,
	superseded run must not block, whatever the array order."""
	older = {"id": 1, "name": "CI", "status": "completed", "conclusion": "cancelled"}
	newer = {"id": 2, "name": "CI", "status": "completed", "conclusion": "success"}
	for order in ([older, newer], [newer, older]):
		rc, reason = _gate(runs_json=_page(order), args='5 "abc1234" "main"', env=CI_REQUIRED_ENV)
		assert rc == 0 and reason == "ok", (order, rc, reason)


def test_newer_cancelled_run_after_success_blocks() -> None:
	older = {"id": 1, "name": "CI", "status": "completed", "conclusion": "success"}
	newer = {"id": 2, "name": "CI", "status": "completed", "conclusion": "cancelled"}
	for order in ([older, newer], [newer, older]):
		rc, reason = _gate(runs_json=_page(order), args='5 "abc1234" "main"', env=CI_REQUIRED_ENV)
		assert rc == 1 and reason == "blocking", (order, rc, reason)


def test_failed_run_is_not_hidden_by_newer_same_name_success() -> None:
	"""Check-runs carry no workflow identity, and this repo has several jobs
	named `lint`. A newer same-name success (possibly another workflow's)
	supersedes only a cancellation, never a failure."""
	runs = _page([
		{"id": 1, "name": "CI", "status": "completed", "conclusion": "failure"},
		{"id": 2, "name": "CI", "status": "completed", "conclusion": "success"},
	])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=CI_REQUIRED_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)
	rc, reason = _gate(runs_json=runs, args='5 "abc1234"', env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_running_run_is_not_hidden_by_newer_same_name_success() -> None:
	"""A still-running run blocks and counts as pending even when a newer
	same-name run already succeeded, so the wait keeps polling."""
	runs = _page([
		{"id": 1, "name": "CI", "status": "in_progress", "conclusion": None},
		{"id": 2, "name": "CI", "status": "completed", "conclusion": "success"},
	])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=CI_REQUIRED_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)
	res = _run(
		'_pr_checks_completed 5 "abc1234" "main" >/dev/null 2>&1; '
		'printf "%s\\n" "${PR_CHECKS_LAST_PENDING}"',
		runs_json=runs,
		env=CI_REQUIRED_ENV,
	)
	assert res.returncode == 0, res.stderr
	assert res.stdout.strip() == "1", res.stdout


def test_cancelled_check_blocks_in_star_mode() -> None:
	runs = _page([
		{"id": 1, "name": "CI", "status": "completed", "conclusion": "success"},
		{"id": 2, "name": "advisory", "status": "completed", "conclusion": "cancelled"},
	])
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "main"',
		env={**REPO_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": "*"},
	)
	assert rc == 1 and reason == "blocking", (rc, reason)
	rc, reason = _gate(runs_json=runs, args='5 "abc1234"', env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_timed_out_and_action_required_required_checks_block() -> None:
	for conclusion in ("timed_out", "action_required", "startup_failure"):
		runs = _page([{"id": 1, "name": "CI", "status": "completed", "conclusion": conclusion}])
		rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=CI_REQUIRED_ENV)
		assert rc == 1 and reason == "blocking", (conclusion, rc, reason)


def test_self_run_newest_hides_older_same_name_cancelled() -> None:
	"""Dedupe runs before the self-run exclusion: when the caller's own job
	is the newest run of its name, an older cancelled attempt of the same
	job does not resurface."""
	runs = _page([
		{
			"id": 1,
			"name": "review / codex-agent",
			"status": "completed",
			"conclusion": "cancelled",
			"details_url": "https://github.com/owner/repo/actions/runs/111/job/1",
		},
		{
			"id": 2,
			"name": "review / codex-agent",
			"status": "in_progress",
			"conclusion": None,
			"details_url": "https://github.com/owner/repo/actions/runs/999/job/9",
		},
	])
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "main"',
		env={**REPO_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": "*", "PR_CHECKS_SELF_RUN_ID": "999"},
	)
	assert rc == 0 and reason == "ok", (rc, reason)


def test_legacy_object_shape_is_deduped() -> None:
	"""The legacy single-object check-runs shape gets the same dedupe."""
	runs = json.dumps({"check_runs": [
		{"id": 1, "name": "CI", "status": "completed", "conclusion": "cancelled"},
		{"id": 2, "name": "CI", "status": "completed", "conclusion": "success"},
	]})
	rc, reason = _gate(runs_json=runs, args='5 "abc1234"', env=REPO_ENV)
	assert rc == 0 and reason == "ok", (rc, reason)


def _lint_run(run_id: str, conclusion: str, check_id: int) -> dict:
	return {
		"id": check_id,
		"name": "lint",
		"status": "completed",
		"conclusion": conclusion,
		"details_url": f"https://github.com/o/r/actions/runs/{run_id}/job/{check_id}",
	}


LINT_REQUIRED_ENV = {
	"PR_CHECKS_REPOSITORY": "shubhodeep1/coding-workflows",
	"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "lint",
}


def test_cancelled_ci_check_not_superseded_by_other_workflow_same_name() -> None:
	"""The CI check name (`lint` here) is shared with lint-pr-body-auto-close
	and lint-plan-archival, which re-run on every PR body edit. A newer
	`lint` from another Actions run must not hide a cancelled CI `lint`,
	in either filter mode, when the runs are not bound to CI run ids."""
	runs = _page([_lint_run("555", "cancelled", 1), _lint_run("777", "success", 2)])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=LINT_REQUIRED_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)
	rc, reason = _gate(runs_json=runs, args='5 "abc1234"', env=LINT_REQUIRED_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_cancelled_ci_check_superseded_by_rerun_of_same_actions_run() -> None:
	"""Re-running the cancelled job keeps the Actions run id, so the newer
	attempt supersedes the cancellation."""
	runs = _page([_lint_run("555", "cancelled", 1), _lint_run("555", "success", 2)])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', env=LINT_REQUIRED_ENV)
	assert rc == 0 and reason == "ok", (rc, reason)


def test_cancelled_ci_check_superseded_by_newer_bound_ci_run_only() -> None:
	"""With PR_CHECKS_CI_RUN_IDS a newer bound CI run supersedes a bound
	cancelled run; an unbound newer run still does not."""
	runs = _page([_lint_run("555", "cancelled", 1), _lint_run("556", "success", 2)])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"',
		env={**LINT_REQUIRED_ENV, "PR_CHECKS_CI_RUN_IDS": "555,556"})
	assert rc == 0 and reason == "ok", (rc, reason)
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"',
		env={**LINT_REQUIRED_ENV, "PR_CHECKS_CI_RUN_IDS": "555"})
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_cancelled_ci_check_not_superseded_when_bound_ids_empty() -> None:
	"""An empty PR_CHECKS_CI_RUN_IDS binds nothing: a newer `lint` from a
	different Actions run must not replace a cancelled CI `lint`."""
	runs = _page([_lint_run("555", "cancelled", 1), _lint_run("777", "success", 2)])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"',
		env={**LINT_REQUIRED_ENV, "PR_CHECKS_CI_RUN_IDS": ""})
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_non_ci_names_keep_cross_run_supersession() -> None:
	"""Names other than the CI check (a new review run's `review / gate`)
	still supersede a cancelled run from an earlier Actions run."""
	runs = _page([
		{**_lint_run("555", "cancelled", 1), "name": "review / gate"},
		{**_lint_run("777", "success", 2), "name": "review / gate"},
	])
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"',
		env={**LINT_REQUIRED_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": "review / gate"})
	assert rc == 0 and reason == "ok", (rc, reason)


def test_rb_judge_jq_mirror_matches_library_defs() -> None:
	"""The runtime jq mirror in test_review_rb_judge_self_run_exclusion.py
	must carry the library's supersession defs verbatim."""
	import re

	lib_text = LIB.read_text(encoding="utf-8")
	m = re.search(r"\nPR_CHECKS_JQ_SUPERSEDE_DEFS='\n(.*?)\n'\n", lib_text, re.S)
	assert m, "pr_checks_lib.sh must define PR_CHECKS_JQ_SUPERSEDE_DEFS"
	mirror = (REPO_ROOT / "tests" / "test_review_rb_judge_self_run_exclusion.py").read_text(encoding="utf-8")
	assert m.group(1) in mirror


_ACCEPT_EXPR = (
	'.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped" '
	'and (.conclusion != "cancelled" or (._superseded | not))'
)


def test_restore_7126_cancelled_not_in_accept_set() -> None:
	"""Static guard (no jq needed), project #7126: a sync or conflict
	resolution once dropped the #7127 hunks silently. Both gate filters in
	_pr_checks_completed must share PR_CHECKS_JQ_SUPERSEDE_DEFS and accept a
	cancelled check-run only when a newer run superseded it."""
	import re

	lib_text = LIB.read_text(encoding="utf-8")
	assert re.search(r"\nPR_CHECKS_JQ_SUPERSEDE_DEFS='\n.*def _mark_superseded:", lib_text, re.S), (
		"PR_CHECKS_JQ_SUPERSEDE_DEFS with _mark_superseded is missing from pr_checks_lib.sh"
	)
	start = lib_text.index("\n_pr_checks_completed()\n")
	end = lib_text.index("\n}\n", start)
	body = lib_text[start:end]
	assert body.count('"${PR_CHECKS_JQ_SUPERSEDE_DEFS}"') == 2, body.count('"${PR_CHECKS_JQ_SUPERSEDE_DEFS}"')
	assert body.count("_mark_superseded") == 2
	assert body.count(_ACCEPT_EXPR) == 2, "each gate filter must accept cancelled only when superseded"
	code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
	assert '.conclusion == "cancelled"' not in code
	assert 'and .conclusion != "cancelled" and' not in code
	assert not re.search(r'\[[^\]]*"success"[^\]]*"cancelled"[^\]]*\]', code), "cancelled must not be in an accepted list"


# ---------------------------------------------------------------------------
# Sentinels + branch-protection precedence.
# ---------------------------------------------------------------------------


def test_allow_all_empty_sentinel_passes() -> None:
	runs = _page([{"name": "CI", "status": "completed", "conclusion": "failure"}])
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "main"',
		env={**REPO_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""},
	)
	assert rc == 0 and reason == "allow_all", (rc, reason)


def test_star_sentinel_blocks_on_any_failure() -> None:
	runs = _page([{"name": "advisory", "status": "completed", "conclusion": "failure"}])
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "main"',
		env={**REPO_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": "*"},
	)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_branch_protection_contexts_take_precedence() -> None:
	"""When the base ref is protected, its required_status_checks.contexts
	define the required set (server-side truth)."""
	runs = _page([{"name": "tests/integration", "status": "completed", "conclusion": "failure"}])
	prot = json.dumps({"required_status_checks": {"contexts": ["CI", "tests/integration"]}})
	rc, reason = _gate(runs_json=runs, args='5 "abc1234" "main"', prot_json=prot, env=REPO_ENV)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_branch_protection_lookup_url_encodes_slash_base_ref() -> None:
	"""Slash-containing base refs must be URL-encoded for the protection
	lookup or GitHub returns 404 and the gate falls back to defaults."""
	runs = _page([{"name": "deploy-check", "status": "completed", "conclusion": "failure"}])
	prot = json.dumps({"required_status_checks": {"contexts": ["deploy-check"]}})
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "release/v1"',
		prot_json=prot,
		env={
			**REPO_ENV,
			"PROT_EXPECT_PATH": "repos/owner/repo/branches/release%2Fv1/protection",
		},
	)
	assert rc == 1 and reason == "blocking", (rc, reason)


def test_branch_protection_lookup_falls_back_to_python_encoder() -> None:
	"""If the jq @uri helper is unavailable, the slash-containing base ref
	must still be URL-encoded before the protection lookup."""
	runs = _page([{"name": "deploy-check", "status": "completed", "conclusion": "failure"}])
	prot = json.dumps({"required_status_checks": {"contexts": ["deploy-check"]}})
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "release/v1"',
		prot_json=prot,
		jq_fail_uri_encode=True,
		env={
			**REPO_ENV,
			"PROT_EXPECT_PATH": "repos/owner/repo/branches/release%2Fv1/protection",
		},
	)
	assert rc == 1 and reason == "blocking", (rc, reason)


# ---------------------------------------------------------------------------
# Self-run exclusion in the required-set branch (needed by review_rb_judge.sh).
# ---------------------------------------------------------------------------


def test_self_run_excluded_in_required_filter() -> None:
	"""The rb_judge's own still-in_progress host job (a check-run on the
	polled SHA) must be excluded when PR_CHECKS_SELF_RUN_ID is set, even
	under the required filter — otherwise the gate self-deadlocks."""
	runs = _page([
		{
			"name": "review / codex-agent",
			"status": "in_progress",
			"conclusion": None,
			"details_url": "https://github.com/owner/repo/actions/runs/999/job/9",
		},
	])
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "main"',
		env={**REPO_ENV, "PR_CHECKS_SELF_RUN_ID": "999"},
	)
	assert rc == 0 and reason == "ok", (rc, reason)


def test_self_run_exclusion_is_narrow() -> None:
	"""Only the matching self-run is excluded; a second genuine pending
	check on the same SHA still blocks."""
	runs = _page([
		{
			"name": "review / codex-agent",
			"status": "in_progress",
			"conclusion": None,
			"details_url": "https://github.com/owner/repo/actions/runs/999/job/9",
		},
		{
			"name": "CI",
			"status": "in_progress",
			"conclusion": None,
			"details_url": "https://github.com/owner/repo/actions/runs/111/job/1",
		},
	])
	rc, reason = _gate(
		runs_json=runs,
		args='5 "abc1234" "main"',
		env={**REPO_ENV, "PR_CHECKS_SELF_RUN_ID": "999"},
	)
	assert rc == 1 and reason == "blocking", (rc, reason)


# ---------------------------------------------------------------------------
# API-error fail-closed + reason side-channel.
# ---------------------------------------------------------------------------


def test_api_error_fails_closed_with_query_failed_reason() -> None:
	"""An empty-object '{}' check-runs payload (API error fallback) matches
	neither jq branch and must fail closed with reason=query_failed."""
	rc, reason = _gate(runs_json="{}", args='5 "abc1234" "main"', env=REPO_ENV)
	assert rc == 1 and reason == "query_failed", (rc, reason)


def test_unresolved_head_sha_reason() -> None:
	"""When the head SHA cannot be resolved (PR JSON has none), the gate
	fails closed with reason=unresolved_head_sha."""
	# No head_sha arg → helper fetches PR JSON, which lacks .head.sha.
	rc, reason = _gate(runs_json="{}", args='5 "" "main"', pr_json="{}", env=REPO_ENV)
	assert rc == 1 and reason == "unresolved_head_sha", (rc, reason)


def test_reason_side_channel_survives_env_prefixed_call() -> None:
	"""review_rb_judge.sh calls the helper with inline env overrides
	(PR_CHECKS_REPOSITORY=... PR_CHECKS_SELF_RUN_ID=... _pr_checks_completed).
	The side-channel reason must still be readable afterward in the same shell."""
	res = _run(
		'PR_CHECKS_REPOSITORY="owner/repo" PR_CHECKS_SELF_RUN_ID="999" '
		'_pr_checks_completed 5 "abc1234" "main" >/dev/null 2>&1; '
		'printf "%s\\n" "${PR_CHECKS_LAST_REASON}"',
		runs_json=_page([{"name": "CI", "status": "completed", "conclusion": "failure"}]),
	)
	assert res.returncode == 0, res.stderr
	assert res.stdout.strip() == "blocking", res.stdout


# ---------------------------------------------------------------------------
# Required-names resolution helper.
# ---------------------------------------------------------------------------


def test_required_names_uses_env_default_when_unset() -> None:
	res = _run(
		'unset ORCH_FINAL_MERGE_REQUIRED_CHECKS; '
		'_pr_required_check_names_for_base ""',
		env=REPO_ENV,
	)
	assert res.returncode == 0, res.stderr
	assert res.stdout.strip() == (
		"CI,lint,Integration PR readiness check,"
		"Lint plan-archival completeness,"
		"Lint PR body for auto-close keywords against orchestrator-tracking issues,"
		"review / gate"
	)


def test_required_names_preserves_explicit_empty_allow_all() -> None:
	res = _run(
		'_pr_required_check_names_for_base ""',
		env={**REPO_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""},
	)
	assert res.returncode == 0, res.stderr
	assert res.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Drift guard: library default == orchestrator default.
# ---------------------------------------------------------------------------


def test_library_default_matches_orchestrator_default() -> None:
	"""scripts/pr_checks_lib.sh and scripts/orchestrate_poll_process.sh must
	declare the SAME ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT string. The
	poller keeps a redundant assignment as a fail-safe (so a sourcing
	failure can never reach the empty-string allow-all sentinel); this test
	pins the two literals equal so they cannot drift."""
	import re

	lib_text = LIB.read_text(encoding="utf-8")
	poller_text = POLLER.read_text(encoding="utf-8")
	lib_m = re.search(
		r'ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT:=([^}]*)\}', lib_text
	)
	poller_m = re.search(
		r'ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT="([^"]*)"', poller_text
	)
	assert lib_m, "pr_checks_lib.sh must declare ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT via :="
	assert poller_m, "orchestrate_poll_process.sh must keep its DEFAULT assignment as a fail-safe"
	assert lib_m.group(1) == poller_m.group(1), (
		"ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT drifted between "
		"pr_checks_lib.sh and orchestrate_poll_process.sh:\n"
		f"  lib   : {lib_m.group(1)!r}\n"
		f"  poller: {poller_m.group(1)!r}"
	)


def test_orchestrator_rb_gates_pass_base_ref() -> None:
	"""All four orchestrator review-blocked merge gates must call
	_pr_checks_completed with a 3rd (base-ref) argument so they use the
	required-checks filter — not the legacy block-on-any-failure 2-arg
	form that deadlocks on a permanently-red non-required check."""
	import re

	poller_text = POLLER.read_text(encoding="utf-8")
	# 2-arg RB gate calls (PR + sha, no base ref) must NOT exist anymore.
	two_arg = re.findall(
		r'_pr_checks_completed "\$\{RB_PR\}" "\$\{_rb_[a-z]+_sha\}"(?!\s+")',
		poller_text,
	)
	assert not two_arg, (
		"orchestrate_poll_process.sh still has 2-arg review-blocked "
		f"_pr_checks_completed calls (legacy all-checks mode): {two_arg}. "
		"Every RB gate must pass the base ref so the required-checks filter "
		"applies."
	)
	# Exactly the four RB gates pass a base ref.
	three_arg = re.findall(
		r'_pr_checks_completed "\$\{RB_PR\}" "\$\{_rb_[a-z]+_sha\}" "\$\{_rb_[a-z]+_base\}"',
		poller_text,
	)
	assert len(three_arg) == 4, (
		"expected exactly 4 review-blocked merge gates passing a base ref "
		f"(merge / force-merge / no-fix / merge_with_followup); found "
		f"{len(three_arg)}: {three_arg}"
	)


# ---------------------------------------------------------------------------
# Issue #6963: the configured CI check side channel. _pr_checks_completed
# records PR_CHECKS_LAST_CI_NAME / PR_CHECKS_LAST_CI_STATE from the listing
# it already fetched; its return value and reason do not change.
# ---------------------------------------------------------------------------

THIS_REPO_ENV = {"PR_CHECKS_REPOSITORY": "shubhodeep1/coding-workflows"}


def _ci_side_channel(runs_json: str, env: dict[str, str], args: str = '5 "abc1234" "main"') -> tuple[str, str, str]:
	body = (
		f'_pr_checks_completed {args} >/dev/null 2>&1; rc=$?; '
		'printf "%s|%s|%s|%s\\n" "${rc}:${PR_CHECKS_LAST_REASON}" "${PR_CHECKS_LAST_CI_NAME}" "${PR_CHECKS_LAST_CI_STATE}" "end"'
	)
	res = _run(body, runs_json=runs_json, env=env)
	assert res.returncode == 0, res.stderr
	gate, name, state, _ = res.stdout.strip().split("|")
	return gate, name, state


def _ci_run(name: str, status: str, conclusion: str | None, run_id: str = "999") -> dict:
	return {"name": name, "status": status, "conclusion": conclusion, "head_sha": "abc1234",
		"details_url": f"https://github.com/o/r/actions/runs/{run_id}/job/1"}


def test_ci_side_channel_defaults_to_lint_only_in_this_repo() -> None:
	runs = _page([_ci_run("lint", "completed", "success")])
	gate, name, state = _ci_side_channel(runs, THIS_REPO_ENV)
	assert (gate, name, state) == ("0:ok", "lint", "success"), (gate, name, state)
	gate, name, state = _ci_side_channel(runs, {"PR_CHECKS_REPOSITORY": "SHUBHODEEP1/Coding-Workflows"})
	assert name == "lint" and state == "success", (name, state)
	gate, name, state = _ci_side_channel(runs, REPO_ENV)
	assert (gate, name, state) == ("0:ok", "none", "disabled"), (gate, name, state)


def test_ci_side_channel_does_not_change_the_gate_result() -> None:
	runs = _page([_ci_run("CI", "completed", "success"), _ci_run("review / gate", "completed", "success")])
	gate, name, state = _ci_side_channel(runs, THIS_REPO_ENV)
	assert gate == "0:ok" and name == "lint" and state == "absent", (gate, name, state)


def test_ci_side_channel_aggregates_every_matching_run() -> None:
	cases = [
		([_ci_run("lint", "completed", "success"), _ci_run("lint", "completed", "failure")], "failure"),
		([_ci_run("lint", "completed", "success"), _ci_run("lint", "in_progress", None)], "pending"),
		([_ci_run("lint", "completed", "skipped")], "failure"),
		([_ci_run("lint", "completed", "neutral")], "failure"),
		([_ci_run("lint", "completed", "cancelled")], "failure"),
		([_ci_run("lint", "completed", "success", run_id="4242")], "absent"),
		([{**_ci_run("lint", "completed", "success"), "head_sha": "fff0000"}], "absent"),
	]
	for runs, expected in cases:
		_, _, state = _ci_side_channel(_page(runs), {**THIS_REPO_ENV, "PR_CHECKS_SELF_RUN_ID": "4242"})
		assert state == expected, (runs, state, expected)


def test_ci_side_channel_rerun_supersedes_cancelled_only_when_bound() -> None:
	"""Bound to CI run ids, a successful re-run supersedes a cancelled run so
	the auto-merge wait agrees with the gate. Name-only matching keeps every
	run (fail closed), and a failure is never superseded."""
	cancelled = {**_ci_run("lint", "completed", "cancelled", run_id="555"), "id": 1}
	rerun = {**_ci_run("lint", "completed", "success", run_id="556"), "id": 2}
	failed = {**_ci_run("lint", "completed", "failure", run_id="555"), "id": 1}
	bound = {**THIS_REPO_ENV, "PR_CHECKS_CI_RUN_IDS": "555,556"}
	gate, _, state = _ci_side_channel(_page([cancelled, rerun]), bound)
	assert gate == "0:ok" and state == "success", (gate, state)
	_, _, state = _ci_side_channel(_page([rerun, cancelled]), bound)
	assert state == "success", state
	_, _, state = _ci_side_channel(_page([cancelled, rerun]), THIS_REPO_ENV)
	assert state == "failure", state
	_, _, state = _ci_side_channel(_page([failed, rerun]), bound)
	assert state == "failure", state


def test_ci_side_channel_counts_only_bound_runs_when_ids_are_given() -> None:
	runs = _page([_ci_run("lint", "completed", "success", run_id="555")])
	_, _, state = _ci_side_channel(runs, {**THIS_REPO_ENV, "PR_CHECKS_CI_RUN_IDS": "999"})
	assert state == "absent", state
	_, _, state = _ci_side_channel(runs, {**THIS_REPO_ENV, "PR_CHECKS_CI_RUN_IDS": "999,555"})
	assert state == "success", state
	_, _, state = _ci_side_channel(runs, {**THIS_REPO_ENV, "PR_CHECKS_CI_RUN_IDS": ""})
	assert state == "absent", state


def test_ci_side_channel_is_unknown_on_api_failure_and_set_on_allow_all() -> None:
	_, _, state = _ci_side_channel("{}", THIS_REPO_ENV)
	assert state == "unknown", state
	gate, _, state = _ci_side_channel(_page([_ci_run("lint", "completed", "success")]),
		{**THIS_REPO_ENV, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""})
	assert gate == "0:allow_all" and state == "success", (gate, state)


def test_ci_check_name_resolution() -> None:
	cases = [
		({**REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": "  build  "}, "build"),
		({**THIS_REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": "NONE"}, "none"),
		({**THIS_REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": ""}, "lint"),
		({**THIS_REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": "review / gate"}, "review / gate"),
		({**THIS_REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": "a,b"}, "lint"),
		({**THIS_REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": "li\nnt"}, "lint"),
		({**REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": "x" * 101}, "__invalid__"),
		({**REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": "lint,test"}, "__invalid__"),
	]
	for env, expected in cases:
		res = _run('printf "%s\\n" "$(_pr_auto_merge_required_ci_check_name)"', env=env)
		assert res.returncode == 0, res.stderr
		assert res.stdout.strip() == expected, (env.get("AUTO_MERGE_REQUIRED_CI_CHECK"), res.stdout)
		if expected != "none" and env.get("AUTO_MERGE_REQUIRED_CI_CHECK", "").strip() not in ("", expected):
			assert "::warning::AUTO_MERGE_REQUIRED_CI_CHECK" in res.stderr, res.stderr


def test_invalid_ci_check_name_in_a_consumer_fails_closed() -> None:
	"""A malformed explicit value must not silently drop the requirement."""
	runs = _page([_ci_run("lint,test", "completed", "success")])
	gate, name, state = _ci_side_channel(runs, {**REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": "lint,test"})
	assert (gate, name, state) == ("0:ok", "__invalid__", "unknown"), (gate, name, state)


def test_ci_check_default_applies_only_to_bases_ci_runs_on() -> None:
	"""ci.yml's pull_request trigger covers every base since issue #7283, so in
	this repo the `lint` default applies to every base, including a fix PR
	routed to a failing PR's ai/issue-* head. Consumers stay off."""
	cases = [
		(THIS_REPO_ENV, "", "lint"),
		(THIS_REPO_ENV, "main", "lint"),
		(THIS_REPO_ENV, "stable", "lint"),
		(THIS_REPO_ENV, "orchestrator/project-42", "lint"),
		(THIS_REPO_ENV, "ai/issue-9", "lint"),
		(THIS_REPO_ENV, "release/v1", "lint"),
		({**THIS_REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": "none"}, "ai/issue-9", "none"),
		({**THIS_REPO_ENV, "AUTO_MERGE_REQUIRED_CI_CHECK": "lint"}, "ai/issue-9", "lint"),
		(REPO_ENV, "main", "none"),
	]
	for env, base, expected in cases:
		res = _run(f'printf "%s\\n" "$(_pr_auto_merge_required_ci_check_name {base!r})"', env=env)
		assert res.returncode == 0, res.stderr
		assert res.stdout.strip() == expected, (env.get("AUTO_MERGE_REQUIRED_CI_CHECK"), base, res.stdout)
	gate, name, state = _ci_side_channel(_page([_ci_run("CI", "completed", "success")]), THIS_REPO_ENV,
		args='5 "abc1234" "ai/issue-9"')
	# No `lint` check-run in the listing: the side channel names the required
	# check and reports it absent; the gate result itself is unchanged.
	assert (gate, name, state) == ("0:ok", "lint", "absent"), (gate, name, state)


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = skipped = failed = 0
	for func in test_funcs:
		try:
			func()
			print(f"  PASS  {func.__name__}")
			passed += 1
		except unittest.SkipTest as exc:
			print(f"  SKIP  {func.__name__}: {exc}")
			skipped += 1
		except Exception as exc:  # noqa: BLE001
			print(f"  FAIL  {func.__name__}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
