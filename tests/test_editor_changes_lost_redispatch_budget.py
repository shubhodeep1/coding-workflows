#!/usr/bin/env python3
"""Tests for the editor-changes-lost re-dispatch reachability fix.

Background (tele-funtoken-msg-scoring#3757, review run 32659591000): the
"Re-dispatch review on editor-changes-lost" step in review_autofix.yml was
guarded by ``github.event.pull_request.number``, but every autofix
iteration after the first is a ``workflow_dispatch`` run (the
``pull_request`` twins are concurrency-cancelled), so the recovery step
could never fire on the runs that actually execute the editor. An
editor-changes-lost iteration therefore always dead-ended with a PR
comment claiming "All automated retry attempts have been exhausted"
(zero attempts were made) until the orchestrator's generic stall
recovery re-triggered the review ~2h later.

The fix makes the step reachable from every trigger path
(``env.PR_NUMBER`` is job-level env covering workflow_call /
workflow_dispatch / pull_request) and replaces the event-payload loop
guard with a per-head-SHA budget: a changes-lost run pushes no commit,
so the head SHA cannot advance, and a completed non-cancelled review run
already recorded on the same head means the current run IS the automated
retry — ``autofix_changes_lost_head_retry_consumed`` (scripts/
gh_helpers.sh) answers that with one branch-scoped list-runs call and
fails CLOSED so a broken probe can never open an unbounded dispatch
loop.

Uses the same function-extraction-plus-stub pattern as
``test_retrigger_inflight_direct_fallback.py``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"

HEAD = "8390b53323a827f77494ef8665cc5e7ea7159e9f"
OTHER_HEAD = "964ef81e5d8cf12bc8793f2906b7a8b63e6885a1"
CURRENT_RUN = "32659591000"


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _redispatch_step(text: str) -> str:
	m = re.search(
		r"- name: Re-dispatch review on editor-changes-lost\n(.*?)\n      - name: ",
		text,
		re.DOTALL,
	)
	assert m, "Failed to locate the re-dispatch step in review_autofix.yml"
	return m.group(0)


def test_redispatch_step_is_reachable_from_workflow_dispatch_runs() -> None:
	step = _redispatch_step(_workflow_text())
	if_line = next(line for line in step.splitlines() if line.strip().startswith("if:"))
	# The event-payload guard made the step unreachable on
	# workflow_dispatch runs; the env-derived PR number covers all
	# trigger paths.
	assert "github.event.pull_request.number" not in if_line, if_line
	assert "env.PR_NUMBER != ''" in if_line, if_line


def test_redispatch_step_bounds_the_retry_per_head_sha() -> None:
	step = _redispatch_step(_workflow_text())
	assert "autofix_changes_lost_head_retry_consumed" in step
	assert "CHANGES_LOST_REDISPATCHED=skipped_budget_exhausted" in step
	# The budget skip must run the step to completion (exit 0), not fail it.
	assert "reason=changes_lost_budget_exhausted" in step


def test_telegram_step_treats_budget_exhausted_as_terminal_comment_path() -> None:
	# The warning step posts the blocked-PR comment in its fallback
	# branch (CHANGES_LOST_REDISPATCHED neither "true" nor
	# "skipped_peer_inflight"), which "skipped_budget_exhausted" lands
	# in — so the "retries exhausted" comment is only posted when the
	# budget really was consumed or the step could not run at all.
	text = _workflow_text()
	m = re.search(
		r"- name: Telegram editor-changes-lost warning\n(.*?)\n      - name: ",
		text,
		re.DOTALL,
	)
	assert m, "Failed to locate the Telegram editor-changes-lost step"
	step = m.group(0)
	assert 'CHANGES_LOST_REDISPATCHED:-false}" = "true"' in step
	assert '"skipped_peer_inflight"' in step


# ---------------------------------------------------------------------------
# Functional tests of autofix_changes_lost_head_retry_consumed
# ---------------------------------------------------------------------------

_RUNNER = r"""
extract_fn() {
	awk -v fn="autofix_changes_lost_head_retry_consumed" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__HELPERS__"
}

gh_retry() { "$@"; }
gh() {
	if [ "${GH_API_FAIL:-0}" = "1" ]; then
		return 1
	fi
	cat "${RUNS_FIXTURE}"
}

eval "$(extract_fn)"
autofix_changes_lost_head_retry_consumed "$@"
"""


def _run_helper(runs_json: str, *args: str, api_fail: bool = False) -> subprocess.CompletedProcess:
	with tempfile.TemporaryDirectory() as tmp:
		fixture = Path(tmp) / "runs.json"
		fixture.write_text(runs_json, encoding="utf-8")
		env = dict(os.environ)
		env["GITHUB_REPOSITORY"] = "owner/repo"
		env["RUNS_FIXTURE"] = str(fixture)
		if api_fail:
			env["GH_API_FAIL"] = "1"
		script = _RUNNER.replace("__HELPERS__", str(GH_HELPERS))
		return subprocess.run(
			["bash", "-c", script, "bash", *args],
			capture_output=True,
			text=True,
			env=env,
		)


def _runs_payload(runs: list[dict]) -> str:
	return json.dumps({"workflow_runs": runs})


def _run(run_id: int, head_sha: str, status: str, conclusion: str | None, path: str) -> dict:
	return {
		"id": run_id,
		"head_sha": head_sha,
		"status": status,
		"conclusion": conclusion,
		"path": path,
	}


def test_budget_available_when_only_cancelled_twin_exists_on_head() -> None:
	# The concurrency-cancelled pull_request twin never ran the editor
	# and must not consume the retry budget (this is the real
	# run-32659591000 shape: cancelled twin + the in-progress current run).
	payload = _runs_payload(
		[
			_run(1, HEAD, "completed", "cancelled", ".github/workflows/ai-review.yml"),
			_run(int(CURRENT_RUN), HEAD, "in_progress", None, ".github/workflows/ai-review.yml"),
		]
	)
	proc = _run_helper(payload, "3757", "ai/issue-3755", CURRENT_RUN, HEAD)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert "prior_completed=0" in proc.stdout, proc.stdout


def test_budget_consumed_by_prior_completed_run_on_same_head() -> None:
	payload = _runs_payload(
		[
			_run(1, HEAD, "completed", "cancelled", ".github/workflows/ai-review.yml"),
			_run(2, HEAD, "completed", "success", ".github/workflows/ai-review.yml"),
			_run(int(CURRENT_RUN), HEAD, "in_progress", None, ".github/workflows/ai-review.yml"),
		]
	)
	proc = _run_helper(payload, "3757", "ai/issue-3755", CURRENT_RUN, HEAD)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "prior_completed=1" in proc.stdout, proc.stdout


def test_budget_ignores_completed_runs_on_other_heads() -> None:
	# Earlier iterations that pushed commits ran on earlier head SHAs;
	# they must not consume the current head's budget.
	payload = _runs_payload(
		[
			_run(1, OTHER_HEAD, "completed", "success", ".github/workflows/ai-review.yml"),
			_run(2, OTHER_HEAD, "completed", "success", ".github/workflows/internal-review.yml"),
		]
	)
	proc = _run_helper(payload, "3757", "ai/issue-3755", CURRENT_RUN, HEAD)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)


def test_budget_ignores_unrelated_workflows_on_same_head() -> None:
	payload = _runs_payload(
		[
			_run(1, HEAD, "completed", "success", ".github/workflows/ci.yml"),
		]
	)
	proc = _run_helper(payload, "3757", "ai/issue-3755", CURRENT_RUN, HEAD)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)


def test_budget_excludes_the_current_run_itself() -> None:
	payload = _runs_payload(
		[
			_run(int(CURRENT_RUN), HEAD, "completed", "success", ".github/workflows/ai-review.yml"),
		]
	)
	proc = _run_helper(payload, "3757", "ai/issue-3755", CURRENT_RUN, HEAD)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)


def test_budget_fails_closed_on_api_error() -> None:
	proc = _run_helper(_runs_payload([]), "3757", "ai/issue-3755", CURRENT_RUN, HEAD, api_fail=True)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED" in proc.stderr, proc.stderr


def test_budget_fails_closed_on_missing_head_sha() -> None:
	proc = _run_helper(_runs_payload([]), "3757", "ai/issue-3755", CURRENT_RUN, "")
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "reason=missing_inputs" in proc.stderr, proc.stderr


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
