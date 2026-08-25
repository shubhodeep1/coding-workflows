#!/usr/bin/env python3
"""Runtime tests for the retrigger_review in-flight-review cache-miss fallback.

Background (RC1 of the #11/#12 stall-recovery incident): the empty-commit
``retrigger_review`` stall recovery guards against clobbering a live review run
by scanning the per-tick ``_load_actions_runs_cached`` blob. That blob is the
same source ``build_active_issue_set`` consumes and can miss an in-flight run
(cache TTL / 304-reuse, the per-status 50-item listing window, pagination, or
``head_branch=null`` on ``workflow_dispatch``). When it does, the empty commit
advances the branch under a still-editing ``review_autofix`` run, tripping
``AUTOFIX_PRE_EDITOR_STALE_BASE -> soft_exit`` and discarding a full review pass.

The fix adds ``_direct_inflight_review_run_on_branch`` — a single branch-scoped
``gh run list`` confirmation issued only on the cache-miss path, just before the
destructive push, at both push sites (``execute_stall_recovery_action`` and
``run_standalone_stall_recovery``).

Uses the same function-extraction-plus-stub pattern as
``test_stall_freshness_branch_fallback.py``: pull the helper definition out of
the production script with awk, source it, and exercise it with a controlled
``gh`` / ``gh_retry`` environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


def _iso_utc_minutes_ago(minutes: int) -> str:
	return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	full_env["GITHUB_REPOSITORY"] = "owner/repo"
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=full_env,
		capture_output=True,
		text=True,
	)


# Extract a single shell function body from POLLER_SCRIPT: match the declaration
# line (``fn()`` at column 0) through the first closing ``}`` at column 0.
_EXTRACT_FN = r"""
extract_fn() {
	local fn="$1"
	awk -v fn="${fn}" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__POLLER__"
}
"""


# ``gh run list ... --json ...`` (the helper supplies no --jq; it pipes to its
# own jq) returns GH_RUN_LIST_JSON verbatim, mirroring real ``gh`` for the
# field-list form. Any other gh call returns empty so unrelated paths fail open.
_STUBS = r"""
gh_retry() { "$@"; }

gh() {
	local is_run=0 is_list=0
	for arg in "$@"; do
		[ "${arg}" = "run" ] && is_run=1
		[ "${arg}" = "list" ] && is_list=1
	done
	if [ "${is_run}" = 1 ] && [ "${is_list}" = 1 ]; then
		printf '%s' "${GH_RUN_LIST_JSON:-[]}"
		return 0
	fi
	printf ''
	return 0
}

export -f gh gh_retry 2>/dev/null || true
"""


def _bootstrap(tmp_dir: Path) -> Path:
	"""Write ``helpers.sh`` with the production helper, return its path."""
	extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
	script = textwrap.dedent(f"""
	set -euo pipefail
	{extractor}
	: > helpers.sh
	extract_fn '_direct_inflight_review_run_on_branch' >> helpers.sh
	""")
	r = _run_bash(script, cwd=tmp_dir)
	assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
	helpers = tmp_dir / "helpers.sh"
	assert helpers.exists() and helpers.stat().st_size > 0, "helpers.sh not produced"
	body = helpers.read_text(encoding="utf-8")
	assert "_direct_inflight_review_run_on_branch()" in body, "missing helper in helpers.sh"
	return helpers


def _run_id(
	run_list_json: str,
	branch: str = "ai/issue-11",
	threshold_minutes: str = "120",
	review_threshold_minutes: str = "250",
	extra_env: dict[str, str] | None = None,
) -> str:
	"""Source the extracted helper and return its stdout for the given payload."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		# The helper scopes itself to review-family runs, so its freshness
		# window is REVIEW_RUN_MAX_RUNTIME_MINUTES (not the generic stall
		# threshold).  STALL_THRESHOLD_MINUTES is still exported because the
		# production init floors the review window at it.
		full_env = {
			"GH_RUN_LIST_JSON": run_list_json,
			"STALL_THRESHOLD_MINUTES": threshold_minutes,
			"REVIEW_RUN_MAX_RUNTIME_MINUTES": review_threshold_minutes,
		}
		if extra_env:
			full_env.update(extra_env)
		body = textwrap.dedent(f"""
		set -uo pipefail
		{_STUBS}
		source helpers.sh
		_direct_inflight_review_run_on_branch '{branch}'
		""")
		r = _run_bash(body, cwd=tmp, env=full_env)
		assert r.returncode == 0, f"helper exited {r.returncode}: {r.stderr}"
		return r.stdout.strip()


def _run(name: str, **fields) -> dict:
	base = {
		"databaseId": fields.get("databaseId", 1),
		"status": fields.get("status", "in_progress"),
		"name": name,
		"workflowName": fields.get("workflowName", name),
		"startedAt": fields.get("startedAt", _iso_utc_minutes_ago(5)),
		"createdAt": fields.get("createdAt", _iso_utc_minutes_ago(5)),
	}
	return base


# ---------------------------------------------------------------------------
# _direct_inflight_review_run_on_branch
# ---------------------------------------------------------------------------

def test_fresh_in_progress_ai_review_is_detected():
	"""A fresh in_progress 'AI Review' run is reported by databaseId — this is
	exactly the run the cached blob missed in the incident."""
	payload = json.dumps([_run("AI Review", databaseId=555)])
	assert _run_id(payload) == "555"


def test_completed_runs_are_ignored():
	"""Only in_progress/queued runs block the push; a completed run does not."""
	payload = json.dumps([_run("AI Review", databaseId=9, status="completed")])
	assert _run_id(payload) == ""


def test_review_run_within_review_window_still_blocks_recovery():
	"""A review run older than STALL_THRESHOLD_MINUTES (120m) but within the
	review budget (REVIEW_RUN_MAX_RUNTIME_MINUTES, 250m) is still legitimately
	editing and MUST block the destructive empty-commit push.  This is the
	PR #3082 / issue #3081 regression: a review at 169m was previously
	misclassified as a zombie and clobbered."""
	mid = _iso_utc_minutes_ago(169)
	payload = json.dumps([_run("AI Review", databaseId=3082, startedAt=mid, createdAt=mid)])
	assert _run_id(payload) == "3082"


def test_stale_in_progress_run_does_not_block_recovery():
	"""A matching run older than the review window (REVIEW_RUN_MAX_RUNTIME_MINUTES,
	250m) is treated as hung and must not block recovery forever (mirrors the
	cached scan's freshness gate; GitHub force-terminates the job at its
	240-min timeout, so a run past the window is genuinely dead)."""
	old = _iso_utc_minutes_ago(300)
	payload = json.dumps([_run("AI Review", databaseId=2, startedAt=old, createdAt=old)])
	assert _run_id(payload) == ""


def test_non_review_workflow_is_ignored():
	"""An unrelated in_progress workflow (e.g. CI) on the same branch must not
	suppress a legitimate re-trigger."""
	payload = json.dumps([_run("CI", databaseId=3)])
	assert _run_id(payload) == ""


def test_queued_run_with_null_started_at_coalesces_to_created_at():
	"""Queued runs often have a null startedAt; the guard must fall back to
	createdAt and still treat a fresh queued review run as blocking."""
	payload = json.dumps([_run("Review Autofix", databaseId=7, status="queued", startedAt=None)])
	assert _run_id(payload) == "7"


def test_workflow_name_field_matches_when_display_name_differs():
	"""When a run's display `name` differs but `workflowName` is canonical, the
	run is still matched (defence-in-depth across the two gh fields)."""
	payload = json.dumps([_run("custom-display", databaseId=42, workflowName="Internal Review")])
	assert _run_id(payload) == "42"


def test_upstream_internal_review_autofix_workflow_name_is_detected():
	"""Regression for the PR #3823 / issue #3816 false stall-recovery: this
	repo's internal-review.yml is named "Internal: AI Review & Autofix" and
	`gh run list` surfaces the run's display title (e.g. the PR title) in
	`name`, so the guard matched neither field and let the destructive
	empty-commit push clobber a healthy 137-minute review pass."""
	payload = json.dumps([
		_run(
			"AI implementation for issue #3816",
			databaseId=32851496137,
			workflowName="Internal: AI Review & Autofix",
		)
	])
	assert _run_id(payload) == "32851496137"


def test_upstream_review_autofix_workflow_name_is_detected():
	"""review_autofix.yml's display name ("Codex PR Self-Healing Semantic
	Agent") must also be recognised as review-family."""
	payload = json.dumps([
		_run(
			"some PR title",
			databaseId=77,
			workflowName="Codex PR Self-Healing Semantic Agent",
		)
	])
	assert _run_id(payload) == "77"


def test_first_fresh_matching_run_is_returned_among_many():
	"""With a mix of runs, a fresh matching in_progress run is surfaced."""
	payload = json.dumps([
		_run("CI", databaseId=1),
		_run("AI Review", databaseId=88, status="completed"),
		_run("AI Review", databaseId=99, status="in_progress"),
	])
	assert _run_id(payload) == "99"


def test_empty_or_non_array_payload_fails_open():
	"""A transient gh/API failure (empty or non-array body) must fail open
	(echo nothing) so recovery is never blocked by an infrastructure blip."""
	assert _run_id("") == ""
	assert _run_id("[]") == ""
	assert _run_id('{"unexpected": "object"}') == ""


def test_empty_branch_argument_returns_empty():
	"""No branch -> no check, no call."""
	assert _run_id(json.dumps([_run("AI Review", databaseId=5)]), branch="") == ""


# ---------------------------------------------------------------------------
# Direct-invocation entrypoint
#
# `.github/workflows/*.yml` runs this test as `python3 tests/<file>.py` from
# explicit allowlists (no pytest discovery). Without this block, the file would
# import successfully and exit 0 without running any test_* functions.
# ---------------------------------------------------------------------------


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
