#!/usr/bin/env python3
"""Tests for the default-branch review retrigger (issue #4898).

``review_autofix.yml`` re-dispatches the review workflow from two steps:
"Re-trigger review via workflow_dispatch" (after the editor pushed its own
``[ai-autofix]`` commit) and "Re-dispatch review on editor-changes-lost".
Both used to pass ``--ref "${TARGET_BRANCH}"`` (the PR head), so the next
run executed the PR branch's unmerged copy of the review workflow with
``secrets: inherit`` and write permissions (finding
``review-dispatches-unmerged-workflow``, #4618 / #4701). Both now dispatch
from the default branch with a validated PR number, PR-named wrappers
first and ``review_autofix.yml`` last. Their bodies live in
``scripts/review_autofix_step_post_commit_retrigger.sh`` and
``scripts/review_autofix_step_changes_lost_redispatch.sh``.

A default-branch run has the default branch as ``head_branch``, so the two
branch-scoped probes in ``scripts/gh_helpers.sh`` also look for runs named
for the PR (``_autofix_pr_named_review_runs``):

* ``autofix_retrigger_has_inflight_peer`` counts queued or running PR-named
  runs, with one extra call only when the branch lookup found no peer, and
  fails open;
* ``autofix_changes_lost_head_retry_consumed`` counts completed,
  non-cancelled PR-named runs created since the head was pushed, with one
  extra call only when the branch lookup counted nothing, and fails closed,
  so the changes-lost retry stays bounded once it runs from the default
  branch. A dispatch run that is not named for the PR (a renamed caller,
  ``review_autofix.yml``, or an ``ai-review.yml`` that predates the run
  name) consumes the budget itself, because its own retry could never
  count it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import (  # noqa: E402
	REVIEW_AUTOFIX_STEP_SCRIPTS,
	expanded_review_autofix_text,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
POST_COMMIT_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_post_commit_retrigger.sh"
CHANGES_LOST_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_changes_lost_redispatch.sh"

POST_COMMIT_STEP = "Re-trigger review via workflow_dispatch"
CHANGES_LOST_STEP = "Re-dispatch review on editor-changes-lost"

CHAIN_START = "# --- default-branch review dispatch (issue #4898) ---"
CHAIN_END = "# --- end default-branch review dispatch ---"

PR = "4898"
BRANCH = "ai/issue-4898"
HEAD = "8390b53323a827f77494ef8665cc5e7ea7159e9f"
CURRENT_RUN = "36500000000"
# 2026-09-29T00:00:00Z
PUSH_EPOCH = 1790640000


def _iso(epoch: int) -> str:
	import datetime

	return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _step_block(text: str, step_name: str) -> str:
	lines = text.splitlines()
	for idx, line in enumerate(lines):
		if line.strip() != f"- name: {step_name}":
			continue
		indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:") and len(candidate) - len(candidate.lstrip(" ")) == indent:
				end = j
				break
		return "\n".join(lines[idx:end])
	raise AssertionError(f"step not found: {step_name}")


def _chain(text: str) -> str:
	start = text.index(CHAIN_START)
	end = text.index(CHAIN_END, start)
	return text[start:end]


# ---------------------------------------------------------------------------
# Static contract
# ---------------------------------------------------------------------------


def test_both_retrigger_steps_source_their_step_scripts() -> None:
	assert REVIEW_AUTOFIX_STEP_SCRIPTS[POST_COMMIT_STEP] == ("review_autofix_step_post_commit_retrigger.sh", "error")
	assert REVIEW_AUTOFIX_STEP_SCRIPTS[CHANGES_LOST_STEP] == ("review_autofix_step_changes_lost_redispatch.sh", "error")
	raw = WORKFLOW.read_text(encoding="utf-8")
	for step, script in ((POST_COMMIT_STEP, POST_COMMIT_SCRIPT), (CHANGES_LOST_STEP, CHANGES_LOST_SCRIPT)):
		block = _step_block(raw, step)
		assert f'REVIEW_AUTOFIX_STEP_SCRIPT="${{SUPPORT_SCRIPTS_DIR:-}}/{script.name}"' in block
		assert "REVIEW_AUTOFIX_CALLER_WORKFLOW_REF: ${{ github.workflow_ref }}" in block
		assert "gh workflow run" not in block
		assert os.access(script, os.X_OK), script
		assert "${{" not in script.read_text(encoding="utf-8"), script


def test_no_retrigger_dispatch_passes_a_ref() -> None:
	expanded = expanded_review_autofix_text()
	for step in (POST_COMMIT_STEP, CHANGES_LOST_STEP):
		block = _step_block(expanded, step)
		assert "gh workflow run" in block
		code = [line for line in block.split("run: |", 1)[1].splitlines() if not line.lstrip().startswith("#")]
		assert not [line for line in code if "--ref" in line], step


def test_both_steps_share_one_dispatch_chain() -> None:
	post_commit = POST_COMMIT_SCRIPT.read_text(encoding="utf-8")
	changes_lost = CHANGES_LOST_SCRIPT.read_text(encoding="utf-8")
	assert _chain(post_commit) == _chain(changes_lost)
	chain = _chain(post_commit)
	assert 'if ! [[ "${PR_NUMBER:-}" =~ ^[1-9][0-9]*$ ]]; then' in chain
	assert "retrigger_candidates+=(review_autofix.yml)" in chain


def test_changes_lost_step_passes_the_head_commit_time() -> None:
	body = CHANGES_LOST_SCRIPT.read_text(encoding="utf-8")
	assert 'REVIEWED_HEAD_COMMIT_EPOCH="$(git log -1 --format=%ct HEAD 2>/dev/null || echo "")"' in body
	assert '"${REVIEWED_HEAD_SHA}" "${REVIEWED_HEAD_COMMIT_EPOCH}" "${GITHUB_EVENT_NAME:-}"; then' in body


# ---------------------------------------------------------------------------
# Functional: the dispatch chain
# ---------------------------------------------------------------------------

_SCRIPT_HARNESS = r"""
emit_event() { :; }
gh() {
	printf '%s\n' "$*" >> "${GH_CALLS}"
	case " $* " in
		*" workflow run ${GH_SUCCEED_ON:-none} "*) return 0 ;;
	esac
	return 1
}
source "__SCRIPT__"
echo "DISPATCHED=${dispatched:-unset}"
"""


def _run_script(script: Path, *, caller_ref: str, succeed_on: str, pr: str = PR, allow: str = "true", extra_env: dict | None = None) -> tuple[subprocess.CompletedProcess, list[str], str]:
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		calls = tmp_path / "calls.txt"
		calls.write_text("", encoding="utf-8")
		github_env = tmp_path / "github_env"
		github_env.write_text("", encoding="utf-8")
		# A throwaway repo so `git rev-parse HEAD` / `git log` answer.
		subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
		subprocess.run(
			["git", "-C", str(tmp_path / "repo"), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "head"],
			check=True,
		)
		env = {
			"PATH": os.environ.get("PATH", ""),
			"HOME": tmp,
			"GH_CALLS": str(calls),
			"GH_SUCCEED_ON": succeed_on,
			"GITHUB_ENV": str(github_env),
			# An empty support dir: gh_helpers.sh is not sourced, so the probes
			# below are the stubs each test defines (or absent).
			"SUPPORT_SCRIPTS_DIR": str(tmp_path / "empty-support"),
			"PR_NUMBER": pr,
			"TARGET_BRANCH": BRANCH,
			"CURRENT_RUN_ID": CURRENT_RUN,
			"ALLOW_WORKFLOW_EDITS": allow,
			"REVIEW_AUTOFIX_CALLER_WORKFLOW_REF": caller_ref,
			"AUTOFIX_RETRIGGER_PEER_WAIT_SECS": "0",
			# Legacy (non-continuation) path: no settle delay.
			"DID_COMMIT": "false",
			"CONFLICT_RESOLVED": "true",
		}
		env.update(extra_env or {})
		harness = _SCRIPT_HARNESS.replace("__SCRIPT__", str(script))
		prelude = env.pop("HARNESS_PRELUDE", "")
		proc = subprocess.run(
			["bash", "-c", prelude + harness],
			capture_output=True,
			text=True,
			env=env,
			cwd=str(tmp_path / "repo"),
		)
		return proc, [line for line in calls.read_text(encoding="utf-8").splitlines() if line], github_env.read_text(encoding="utf-8")


def _workflows(calls: list[str]) -> list[str]:
	return [call.split()[2] for call in calls if call.startswith("workflow run ")]


def test_internal_review_caller_is_dispatched_first_from_the_default_branch() -> None:
	proc, calls, _ = _run_script(
		POST_COMMIT_SCRIPT,
		caller_ref="shubhodeep1/coding-workflows/.github/workflows/internal-review.yml@refs/heads/main",
		succeed_on="internal-review.yml",
	)
	assert proc.returncode == 0, proc.stderr
	assert _workflows(calls) == ["internal-review.yml"]
	assert calls[0] == f"workflow run internal-review.yml -f pr_number={PR} -f allow_workflow_edits=true"
	assert "DISPATCHED=true" in proc.stdout


def test_ai_review_caller_is_dispatched_first_in_consumer_repos() -> None:
	proc, calls, _ = _run_script(
		POST_COMMIT_SCRIPT,
		caller_ref="consumer/repo/.github/workflows/ai-review.yml@refs/pull/7/merge",
		succeed_on="ai-review.yml",
	)
	assert proc.returncode == 0, proc.stderr
	assert _workflows(calls) == ["ai-review.yml"]


def test_fallback_order_is_wrappers_then_custom_caller_then_review_autofix() -> None:
	proc, calls, _ = _run_script(
		POST_COMMIT_SCRIPT,
		caller_ref="consumer/repo/.github/workflows/custom-review.yml@refs/heads/main",
		succeed_on="none",
	)
	assert proc.returncode == 0, proc.stderr
	assert _workflows(calls) == ["ai-review.yml", "internal-review.yml", "custom-review.yml", "review_autofix.yml"]
	assert all("--ref" not in call for call in calls), calls
	assert "DISPATCHED=false" in proc.stdout
	assert "Could not dispatch review workflow via workflow_dispatch" in proc.stdout


def test_review_autofix_caller_is_not_tried_twice() -> None:
	_, calls, _ = _run_script(
		POST_COMMIT_SCRIPT,
		caller_ref="shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@refs/heads/main",
		succeed_on="none",
	)
	assert _workflows(calls) == ["ai-review.yml", "internal-review.yml", "review_autofix.yml"]


def test_unparseable_caller_ref_falls_back_to_internal_review() -> None:
	_, calls, _ = _run_script(POST_COMMIT_SCRIPT, caller_ref="", succeed_on="none")
	assert _workflows(calls) == ["internal-review.yml", "ai-review.yml", "review_autofix.yml"]
	_, calls, _ = _run_script(POST_COMMIT_SCRIPT, caller_ref="x/.github/workflows/$(evil).yml@refs/heads/main", succeed_on="none")
	assert _workflows(calls) == ["internal-review.yml", "ai-review.yml", "review_autofix.yml"]


def test_invalid_pr_number_dispatches_nothing() -> None:
	for bad in ("", "0", "12a", "4898 --ref x", "-1"):
		proc, calls, _ = _run_script(
			POST_COMMIT_SCRIPT,
			caller_ref="shubhodeep1/coding-workflows/.github/workflows/internal-review.yml@refs/heads/main",
			succeed_on="internal-review.yml",
			pr=bad,
		)
		assert _workflows(calls) == [], (bad, calls)
		assert "Invalid PR number" in proc.stdout, (bad, proc.stdout)
		assert "DISPATCHED=false" in proc.stdout


def test_allow_workflow_edits_is_normalised() -> None:
	for value, expected in (("true", "true"), ("false", "false"), ("", "false"), ("yes", "false"), ("true -f x=y", "false")):
		_, calls, _ = _run_script(
			POST_COMMIT_SCRIPT,
			caller_ref="shubhodeep1/coding-workflows/.github/workflows/internal-review.yml@refs/heads/main",
			succeed_on="internal-review.yml",
			allow=value,
		)
		assert calls == [f"workflow run internal-review.yml -f pr_number={PR} -f allow_workflow_edits={expected}"], (value, calls)


def test_changes_lost_redispatch_uses_the_same_chain() -> None:
	# Budget available (stub returns 1), no peer: the step dispatches.
	proc, calls, github_env = _run_script(
		CHANGES_LOST_SCRIPT,
		caller_ref="consumer/repo/.github/workflows/ai-review.yml@refs/heads/main",
		succeed_on="ai-review.yml",
		extra_env={
			"HARNESS_PRELUDE": 'autofix_retrigger_has_inflight_peer() { return 1; }\n'
			'autofix_changes_lost_head_retry_consumed() { printf "%s\\n" "BUDGET_ARGS $*"; return 1; }\n',
			"GITHUB_EVENT_NAME": "workflow_dispatch",
		},
	)
	assert proc.returncode == 0, proc.stderr
	assert _workflows(calls) == ["ai-review.yml"]
	assert all("--ref" not in call for call in calls), calls
	assert "CHANGES_LOST_REDISPATCHED=true" in github_env
	budget_line = next(line for line in proc.stdout.splitlines() if line.startswith("BUDGET_ARGS "))
	fields = budget_line.split()
	assert fields[1:4] == [PR, BRANCH, CURRENT_RUN]
	assert len(fields[4]) == 40 and fields[5].isdigit(), budget_line
	assert fields[6] == "workflow_dispatch", budget_line


# ---------------------------------------------------------------------------
# Functional: the probes
# ---------------------------------------------------------------------------

_PROBE_RUNNER = r"""
extract_fn() {
	awk -v fn="$1" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__HELPERS__"
}

gh_retry() { "$@"; }
emit_event() { :; }
gh() {
	[ "${1:-}" = "api" ] || return 1
	printf '%s\n' "$*" >> "${GH_CALLS}"
	case " $* " in
		*" /repos/owner/repo --jq "*)
			[ "${DEFAULT_BRANCH_FAIL:-0}" = "1" ] && return 1
			printf '%s\n' "${DEFAULT_BRANCH_VALUE}"
			;;
		*" event=workflow_dispatch "*)
			[ "${PR_NAMED_FAIL:-0}" = "1" ] && return 1
			cat "${PR_NAMED_FIXTURE}"
			;;
		*" branch="*) cat "${BRANCH_FIXTURE}" ;;
		*) return 1 ;;
	esac
}

eval "$(extract_fn _autofix_pr_named_review_default_branch)"
eval "$(extract_fn _autofix_pr_named_review_runs)"
eval "$(extract_fn __FN__)"
__FN__ "$@"
"""


def _run_probe(fn: str, branch_runs: list[dict], pr_named_runs: list[dict], *args: str, pr_named_fail: bool = False, default_branch: str = "main", default_branch_fail: bool = False) -> tuple[subprocess.CompletedProcess, list[str]]:
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		branch_fixture = tmp_path / "branch.json"
		branch_fixture.write_text(json.dumps({"workflow_runs": branch_runs}), encoding="utf-8")
		pr_named_fixture = tmp_path / "pr_named.json"
		pr_named_fixture.write_text(json.dumps({"workflow_runs": pr_named_runs}), encoding="utf-8")
		calls = tmp_path / "calls.txt"
		calls.write_text("", encoding="utf-8")
		env = dict(os.environ)
		env.update(
			{
				"GITHUB_REPOSITORY": "owner/repo",
				"BRANCH_FIXTURE": str(branch_fixture),
				"PR_NAMED_FIXTURE": str(pr_named_fixture),
				"GH_CALLS": str(calls),
				"PR_NAMED_FAIL": "1" if pr_named_fail else "0",
				"DEFAULT_BRANCH_VALUE": default_branch,
				"DEFAULT_BRANCH_FAIL": "1" if default_branch_fail else "0",
			}
		)
		script = _PROBE_RUNNER.replace("__HELPERS__", str(GH_HELPERS)).replace("__FN__", fn)
		proc = subprocess.run(["bash", "-c", script, "bash", *args], capture_output=True, text=True, env=env)
		return proc, [line for line in calls.read_text(encoding="utf-8").splitlines() if line]


def _pr_named(run_id: int, status: str, conclusion: str | None, created_epoch: int, *, pr: str = PR, wrapper: str = "internal-review.yml") -> dict:
	title = f"Internal: AI Review & Autofix [pr:{pr}]" if wrapper == "internal-review.yml" else f"AI Review [pr:{pr}]"
	return {
		"id": run_id,
		"event": "workflow_dispatch",
		"status": status,
		"conclusion": conclusion,
		"created_at": _iso(created_epoch),
		"path": f".github/workflows/{wrapper}",
		"display_title": title,
		"head_branch": "main",
		"head_sha": "d" * 40,
	}


def _branch_run(run_id: int, head_sha: str, status: str, conclusion: str | None, created_epoch: int, path: str = ".github/workflows/internal-review.yml") -> dict:
	return {
		"id": run_id,
		"event": "pull_request",
		"status": status,
		"conclusion": conclusion,
		"created_at": _iso(created_epoch),
		"path": path,
		"head_sha": head_sha,
	}


PEER = "autofix_retrigger_has_inflight_peer"
BUDGET = "autofix_changes_lost_head_retry_consumed"


def test_peer_check_finds_a_pr_named_default_branch_run() -> None:
	proc, calls = _run_probe(PEER, [], [_pr_named(111, "in_progress", None, PUSH_EPOCH)], PR, BRANCH, CURRENT_RUN)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "peer_count=1 peer_run=111 peer_path=.github/workflows/internal-review.yml" in proc.stdout
	assert len(calls) == 3
	# Issue #5522: one default-branch read before the PR-named listing.
	assert "/repos/owner/repo --jq" in calls[1] and "-X GET" in calls[1]
	assert "-X GET" in calls[2] and "event=workflow_dispatch" in calls[2] and "per_page=100" in calls[2]
	# The in-flight lookup must not filter by one status.
	assert "status=" not in calls[2]


def test_peer_check_matches_consumer_ai_review_names() -> None:
	proc, _ = _run_probe(PEER, [], [_pr_named(112, "queued", None, PUSH_EPOCH, wrapper="ai-review.yml")], PR, BRANCH, CURRENT_RUN)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "peer_path=.github/workflows/ai-review.yml" in proc.stdout


def test_peer_check_skips_the_second_call_when_the_branch_lookup_found_a_peer() -> None:
	proc, calls = _run_probe(
		PEER,
		[_branch_run(222, HEAD, "queued", None, PUSH_EPOCH)],
		[_pr_named(111, "in_progress", None, PUSH_EPOCH)],
		PR,
		BRANCH,
		CURRENT_RUN,
	)
	assert proc.returncode == 0
	assert "peer_run=222" in proc.stdout
	assert len(calls) == 1, calls


def test_peer_check_ignores_itself_other_prs_finished_and_lookalike_runs() -> None:
	lookalike = _pr_named(115, "in_progress", None, PUSH_EPOCH)
	lookalike["display_title"] = f"Internal: AI Review & Autofix [pr:{PR}] extra"
	wrong_path = _pr_named(116, "in_progress", None, PUSH_EPOCH)
	wrong_path["path"] = ".github/workflows/other.yml"
	proc, _ = _run_probe(
		PEER,
		[],
		[
			_pr_named(int(CURRENT_RUN), "in_progress", None, PUSH_EPOCH),
			_pr_named(113, "in_progress", None, PUSH_EPOCH, pr="48980"),
			_pr_named(114, "completed", "success", PUSH_EPOCH),
			lookalike,
			wrong_path,
		],
		PR,
		BRANCH,
		CURRENT_RUN,
	)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert "peer_count=0" in proc.stdout


def test_peer_check_fails_open_when_the_pr_named_call_fails() -> None:
	proc, _ = _run_probe(PEER, [], [], PR, BRANCH, CURRENT_RUN, pr_named_fail=True)
	assert proc.returncode == 1
	assert "reason=pr_named_api_error" in proc.stderr
	assert "AUTOFIX_PEER_CHECK pr=4898" in proc.stdout


def test_peer_check_makes_no_pr_named_call_for_an_invalid_pr_number() -> None:
	proc, calls = _run_probe(PEER, [], [_pr_named(111, "in_progress", None, PUSH_EPOCH)], "abc", BRANCH, CURRENT_RUN)
	assert proc.returncode == 1
	assert len(calls) == 1, calls


def test_budget_counts_a_pr_named_retry_since_the_push() -> None:
	# The original run dispatched the retry from the default branch; the
	# retry (this run) must see the original completed PR-named run.
	proc, calls = _run_probe(
		BUDGET,
		[_branch_run(300, HEAD, "completed", "cancelled", PUSH_EPOCH)],
		[_pr_named(301, "completed", "success", PUSH_EPOCH + 600)],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH - 60),
	)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "prior_completed=1 pr_named_completed=1" in proc.stdout
	assert len(calls) == 3
	assert "/repos/owner/repo --jq" in calls[1]
	assert "event=workflow_dispatch" in calls[2] and "-X GET" in calls[2]
	# No status filter: the in-progress current run must be in the page so
	# an unnamed dispatch run can be recognised (completed is filtered locally).
	assert "status=" not in calls[2], calls[2]


def test_budget_ignores_pr_named_runs_before_the_push_cancelled_and_itself() -> None:
	proc, _ = _run_probe(
		BUDGET,
		[_branch_run(300, HEAD, "completed", "cancelled", PUSH_EPOCH)],
		[
			_pr_named(302, "completed", "success", PUSH_EPOCH - 3600),
			_pr_named(303, "completed", "cancelled", PUSH_EPOCH + 60),
			_pr_named(int(CURRENT_RUN), "completed", "success", PUSH_EPOCH + 60),
			_pr_named(304, "completed", "success", PUSH_EPOCH + 60, pr="1"),
		],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH - 60),
	)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert "prior_completed=0 pr_named_completed=0" in proc.stdout


def test_budget_bound_ignores_a_future_dated_commit() -> None:
	# A commit dated after the push must not move the bound past the
	# original run: the head's first branch run bounds it.
	proc, _ = _run_probe(
		BUDGET,
		[_branch_run(300, HEAD, "completed", "cancelled", PUSH_EPOCH)],
		[_pr_named(301, "completed", "success", PUSH_EPOCH + 600)],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH + 86400),
	)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "pr_named_completed=1" in proc.stdout


def test_budget_uses_the_commit_time_when_no_branch_run_is_on_the_head() -> None:
	proc, _ = _run_probe(
		BUDGET,
		[_branch_run(305, "e" * 40, "completed", "success", PUSH_EPOCH - 7200)],
		[_pr_named(301, "completed", "success", PUSH_EPOCH + 600)],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH),
	)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "pr_named_completed=1" in proc.stdout


def test_budget_fails_closed_without_a_push_bound() -> None:
	proc, calls = _run_probe(BUDGET, [], [], PR, BRANCH, CURRENT_RUN, HEAD)
	assert proc.returncode == 0
	assert "reason=missing_head_time" in proc.stderr
	assert len(calls) == 1, calls


def test_budget_fails_closed_when_the_pr_named_call_fails() -> None:
	proc, _ = _run_probe(BUDGET, [], [], PR, BRANCH, CURRENT_RUN, HEAD, str(PUSH_EPOCH), pr_named_fail=True)
	assert proc.returncode == 0
	assert "reason=pr_named_api_error" in proc.stderr


def test_budget_fails_closed_on_an_invalid_pr_number() -> None:
	proc, calls = _run_probe(BUDGET, [], [], "abc", BRANCH, CURRENT_RUN, HEAD, str(PUSH_EPOCH))
	assert proc.returncode == 0
	assert "reason=invalid_pr_number" in proc.stderr
	assert len(calls) == 1, calls


def test_budget_skips_the_second_call_when_the_branch_lookup_counted_a_run() -> None:
	proc, calls = _run_probe(
		BUDGET,
		[_branch_run(306, HEAD, "completed", "success", PUSH_EPOCH)],
		[_pr_named(301, "completed", "success", PUSH_EPOCH + 600)],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH),
	)
	assert proc.returncode == 0
	assert "prior_completed=1 pr_named_completed=-" in proc.stdout
	assert len(calls) == 1, calls


def test_budget_available_when_no_retry_ran_yet() -> None:
	proc, _ = _run_probe(
		BUDGET,
		[_branch_run(300, HEAD, "completed", "cancelled", PUSH_EPOCH)],
		[],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH),
	)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert "prior_completed=0 pr_named_completed=0" in proc.stdout
	# No sixth argument: the unnamed-dispatch check is skipped, and the budget
	# line says so rather than hiding it.
	assert "pr_named_completed=0 event=-" in proc.stdout


def _unnamed_dispatch(run_id: int, status: str, conclusion: str | None, created_epoch: int) -> dict:
	# A consumer ai-review.yml that predates the "[pr:<N>]" run name titles
	# its dispatch runs with the workflow name only.
	return {
		"id": run_id,
		"event": "workflow_dispatch",
		"status": status,
		"conclusion": conclusion,
		"created_at": _iso(created_epoch),
		"path": ".github/workflows/ai-review.yml",
		"display_title": "AI Review",
		"head_branch": "main",
		"head_sha": "d" * 40,
	}


def test_budget_fails_closed_on_a_dispatch_run_not_named_for_the_pr() -> None:
	# The loop the audit found: the head's pull_request twin was cancelled and
	# the earlier retry ran unnamed, so neither lookup counts it. This run is
	# unnamed too, so it must not dispatch another invisible retry.
	proc, calls = _run_probe(
		BUDGET,
		[_branch_run(300, HEAD, "completed", "cancelled", PUSH_EPOCH, path=".github/workflows/ai-review.yml")],
		[
			_unnamed_dispatch(777, "completed", "failure", PUSH_EPOCH + 600),
			_unnamed_dispatch(int(CURRENT_RUN), "in_progress", None, PUSH_EPOCH + 1200),
		],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH - 60),
		"workflow_dispatch",
	)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "reason=unnamed_dispatch_run" in proc.stderr
	assert len(calls) == 3, calls


def test_budget_available_for_a_pr_named_dispatch_run_with_no_prior_retry() -> None:
	# The current run is itself PR-named and in progress: it is recognised,
	# not counted, and the budget stays available.
	proc, calls = _run_probe(
		BUDGET,
		[_branch_run(300, HEAD, "completed", "cancelled", PUSH_EPOCH)],
		[
			_pr_named(int(CURRENT_RUN), "in_progress", None, PUSH_EPOCH + 600),
			_pr_named(308, "queued", None, PUSH_EPOCH + 700),
		],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH - 60),
		"workflow_dispatch",
	)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert "prior_completed=0 pr_named_completed=0 event=workflow_dispatch" in proc.stdout
	assert len(calls) == 3, calls


def test_budget_counts_the_prior_retry_for_a_pr_named_dispatch_run() -> None:
	proc, _ = _run_probe(
		BUDGET,
		[_branch_run(300, HEAD, "completed", "cancelled", PUSH_EPOCH)],
		[
			_pr_named(int(CURRENT_RUN), "in_progress", None, PUSH_EPOCH + 1200),
			_pr_named(301, "completed", "failure", PUSH_EPOCH + 600),
		],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH - 60),
		"workflow_dispatch",
	)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "prior_completed=1 pr_named_completed=1" in proc.stdout


def test_budget_unnamed_check_skips_pull_request_runs() -> None:
	# A pull_request run is on the head branch, so its retry can count it;
	# only workflow_dispatch runs are checked for a PR name.
	proc, _ = _run_probe(
		BUDGET,
		[_branch_run(300, HEAD, "completed", "cancelled", PUSH_EPOCH)],
		[],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH),
		"pull_request",
	)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert "reason=unnamed_dispatch_run" not in proc.stderr
	assert "event=pull_request" in proc.stdout


# ---------------------------------------------------------------------------
# Provenance (security, issue #5522): a PR-named run counts only when it ran
# from the default branch through the wrapper that sets that name.
# ---------------------------------------------------------------------------


def _forged_runs() -> list[dict]:
	branch_dispatch = _pr_named(401, "in_progress", None, PUSH_EPOCH)
	branch_dispatch["head_branch"] = "attacker/branch"
	null_head = _pr_named(402, "in_progress", None, PUSH_EPOCH)
	null_head["head_branch"] = None
	missing_head = _pr_named(403, "in_progress", None, PUSH_EPOCH)
	del missing_head["head_branch"]
	internal_title_consumer_path = _pr_named(404, "in_progress", None, PUSH_EPOCH)
	internal_title_consumer_path["path"] = ".github/workflows/ai-review.yml"
	consumer_title_internal_path = _pr_named(405, "in_progress", None, PUSH_EPOCH, wrapper="ai-review.yml")
	consumer_title_internal_path["path"] = ".github/workflows/internal-review.yml"
	push_event = _pr_named(406, "in_progress", None, PUSH_EPOCH)
	push_event["event"] = "push"
	return [branch_dispatch, null_head, missing_head, internal_title_consumer_path, consumer_title_internal_path, push_event]


def test_peer_check_ignores_forged_pr_named_runs() -> None:
	proc, _ = _run_probe(PEER, [], _forged_runs(), PR, BRANCH, CURRENT_RUN)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert "peer_count=0" in proc.stdout


def test_peer_check_uses_the_resolved_default_branch_not_main() -> None:
	on_trunk = _pr_named(407, "in_progress", None, PUSH_EPOCH)
	on_trunk["head_branch"] = "trunk"
	proc, _ = _run_probe(PEER, [], [_pr_named(408, "in_progress", None, PUSH_EPOCH), on_trunk], PR, BRANCH, CURRENT_RUN, default_branch="trunk")
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "peer_count=1 peer_run=407" in proc.stdout


def test_peer_check_fails_open_without_a_default_branch() -> None:
	for kwargs in ({"default_branch_fail": True}, {"default_branch": ""}):
		proc, calls = _run_probe(PEER, [], [_pr_named(111, "in_progress", None, PUSH_EPOCH)], PR, BRANCH, CURRENT_RUN, **kwargs)
		assert proc.returncode == 1, (kwargs, proc.stdout, proc.stderr)
		assert "AUTOFIX_PR_NAMED_PROVENANCE repo=owner/repo outcome=default_branch_unresolved" in proc.stderr
		assert "reason=pr_named_api_error" in proc.stderr
		# No PR-named listing without a default branch.
		assert not any("event=workflow_dispatch" in call for call in calls), calls


def test_budget_ignores_forged_completed_pr_named_runs() -> None:
	forged = []
	for run in _forged_runs():
		run.update({"status": "completed", "conclusion": "success", "created_at": _iso(PUSH_EPOCH + 600)})
		forged.append(run)
	proc, _ = _run_probe(
		BUDGET,
		[_branch_run(300, HEAD, "completed", "cancelled", PUSH_EPOCH)],
		forged,
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH - 60),
	)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert "prior_completed=0 pr_named_completed=0" in proc.stdout


def test_budget_fails_closed_without_a_default_branch() -> None:
	proc, calls = _run_probe(
		BUDGET,
		[_branch_run(300, HEAD, "completed", "cancelled", PUSH_EPOCH)],
		[],
		PR,
		BRANCH,
		CURRENT_RUN,
		HEAD,
		str(PUSH_EPOCH - 60),
		default_branch_fail=True,
	)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "reason=pr_named_api_error" in proc.stderr
	assert not any("event=workflow_dispatch" in call for call in calls), calls


def test_default_branch_is_read_once_per_shell() -> None:
	script = _PROBE_RUNNER.replace("__HELPERS__", str(GH_HELPERS)).replace(
		'eval "$(extract_fn __FN__)"\n__FN__ "$@"\n',
		'_autofix_pr_named_review_default_branch\n_autofix_pr_named_review_runs 4898 >/dev/null\n_autofix_pr_named_review_runs 4898 >/dev/null\n_autofix_pr_named_review_default_branch\n',
	)
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		fixture = tmp_path / "runs.json"
		fixture.write_text(json.dumps({"workflow_runs": []}), encoding="utf-8")
		calls = tmp_path / "calls.txt"
		calls.write_text("", encoding="utf-8")
		env = dict(os.environ)
		env.update({
			"GITHUB_REPOSITORY": "owner/repo",
			"BRANCH_FIXTURE": str(fixture),
			"PR_NAMED_FIXTURE": str(fixture),
			"GH_CALLS": str(calls),
			"DEFAULT_BRANCH_VALUE": "main",
		})
		proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
		assert proc.returncode == 0, proc.stderr
		lines = [line for line in calls.read_text(encoding="utf-8").splitlines() if line]
	assert sum("/repos/owner/repo --jq" in line for line in lines) == 1, lines
	assert sum("event=workflow_dispatch" in line for line in lines) == 2, lines


def test_ref_suffixed_wrapper_path_still_counts() -> None:
	suffixed = _pr_named(409, "in_progress", None, PUSH_EPOCH)
	suffixed["path"] = ".github/workflows/internal-review.yml@refs/heads/main"
	proc, _ = _run_probe(PEER, [], [suffixed], PR, BRANCH, CURRENT_RUN)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "peer_run=409" in proc.stdout
