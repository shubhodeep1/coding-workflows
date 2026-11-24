#!/usr/bin/env python3
"""The review-blocked judge records a deferred merge verdict on the head.

Issue #9991: the judge's decision lived only in a PR review comment, so no
loop could tell "already judged, waiting to merge" from "needs a judge". The
judge now posts the commit status `ai-review/rb-judge-verdict` when it
decided merge but the merge was deferred for a reason a re-run cannot
change, and scripts/pr_checks_lib.sh decides whether anything relevant
changed since.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PR_CHECKS_LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
HEAD_GATE = REPO_ROOT / "scripts" / "review_head_gate.sh"
RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
HEAD = "a" * 40
BASE = "b" * 40

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


def _bash(script: str) -> subprocess.CompletedProcess:
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


def _desc(decision: str = "merge", wait: str = "lease_wait", base: str = BASE, ms: str = "blocked") -> str:
	return f"decision={decision} wait={wait} base={base} ms={ms}"


@pytest.mark.parametrize("desc, ok", [
	(_desc(), True),
	(_desc(decision="merge_with_followup", wait="auto_merge", ms="has_hooks"), True),
	(_desc(decision="fix"), False),
	(_desc(wait="merge_failed"), False),
	(_desc(base="B" * 40), False),
	(_desc(ms="weird"), False),
	(_desc() + " extra", False),
	("decision=merge wait=lease_wait base=" + BASE + " ms=blocked; rm -rf /", False),
	("", False),
	# The optional first-verdict time a base refresh carries (issue #9991).
	(_desc() + " at=2026-11-23T09:00:00Z", True),
	(_desc() + " at=2026-11-23 09:00:00", False),
	(_desc() + " at=2026-11-23T09:00:00Z extra", False),
])
def test_parse_is_strict(desc: str, ok: bool) -> None:
	result = _bash(f'source "{PR_CHECKS_LIB}"; if _pr_rb_judge_verdict_parse {json.dumps(desc)}; then echo "OK $PR_RB_VERDICT_WAIT"; else echo "BAD[$PR_RB_VERDICT_WAIT]"; fi')
	assert result.returncode == 0, result.stderr
	assert result.stdout.startswith("OK") is ok, result.stdout
	if not ok:
		assert "BAD[]" in result.stdout


def _waiting(desc: str, *, labels: str = '["ai:merge-lease-wait"]', cur_base: str = BASE, ms: str = "BLOCKED",
		auto: str = "false", freshness: str = "clean", env: str = "", checks: str = "") -> str:
	script = f'''
source "{PR_CHECKS_LIB}"
FRESH_LOG=$(mktemp)
_pr_base_freshness() {{ echo "FRESHNESS_CALLED $*" >> "$FRESH_LOG"; PR_BASE_FRESHNESS_OUTCOME="{freshness}"; echo "{freshness}"; }}
{env}
if _pr_rb_judge_verdict_waiting 77 {HEAD} main {json.dumps(cur_base)} {json.dumps(ms)} '{labels}' {json.dumps(auto)} {json.dumps(desc)} {json.dumps(checks)}; then
	echo "WAIT $PR_RB_VERDICT_WAIT_REASON${{PR_RB_VERDICT_BASE_REFRESH:+ refresh=$PR_RB_VERDICT_BASE_REFRESH}}"
else
	echo "CHANGE $PR_RB_VERDICT_CHANGE"
fi
cat "$FRESH_LOG" >&2
'''
	result = _bash(script)
	assert result.returncode == 0, result.stderr
	return result.stdout.strip() + ("|freshness" if "FRESHNESS_CALLED" in result.stderr else "")


def test_unchanged_lease_wait_is_waiting_without_reads() -> None:
	assert _waiting(_desc()) == "WAIT lease_wait"


@pytest.mark.parametrize("kwargs, expected", [
	({"labels": "[]"}, "CHANGE lease"),
	({"ms": "CLEAN"}, "CHANGE checks"),
	({"ms": ""}, "CHANGE checks"),
	({"cur_base": "c" * 40, "freshness": "overlap"}, "CHANGE base|freshness"),
	({"cur_base": "c" * 40, "freshness": "unknown"}, "CHANGE base_unknown|freshness"),
	({"cur_base": "c" * 40, "freshness": "disabled"}, "CHANGE base_unknown|freshness"),
	({"cur_base": "c" * 40, "freshness": "clean"}, "WAIT lease_wait refresh=" + "c" * 40 + "|freshness"),
	({"cur_base": "c" * 40, "freshness": "fresh"}, "WAIT lease_wait refresh=" + "c" * 40 + "|freshness"),
	({"checks": "FAILURE"}, "CHANGE checks"),
	({"checks": "error"}, "CHANGE checks"),
	({"checks": "pending"}, "WAIT lease_wait"),
	({"checks": "EXPECTED"}, "WAIT lease_wait"),
	({"cur_base": ""}, "CHANGE base_unknown"),
	({"env": "VERDICT_WAIT_DISPATCH_SKIP_ENABLED=false"}, "CHANGE disabled"),
	({"env": "VERDICT_WAIT_DISPATCH_SKIP_ENABLED=ON"}, "WAIT lease_wait"),
])
def test_relevant_changes(kwargs: dict, expected: str) -> None:
	assert _waiting(_desc(), **kwargs) == expected


def test_auto_merge_wait_needs_auto_merge_enabled() -> None:
	desc = _desc(wait="auto_merge")
	assert _waiting(desc, labels="[]", auto="true") == "WAIT auto_merge"
	assert _waiting(desc, labels="[]", auto="false") == "CHANGE auto_merge"


def test_malformed_description_is_a_change() -> None:
	assert _waiting("decision=merge") == "CHANGE parse"


def _post(*args: str, fail: bool = False) -> tuple[subprocess.CompletedProcess, list[str]]:
	script = f'''
source "{HEAD_GATE}"
gh() {{ printf '%s\\n' "$*" >> "$CALLS"; {"return 1" if fail else "return 0"}; }}
CALLS=$(mktemp); PR_NUMBER=77
review_rb_judge_verdict_post_status {" ".join(json.dumps(a) for a in args)}
echo "---"; cat "$CALLS"
'''
	result = _bash(script)
	out, _, calls = result.stdout.partition("---\n")
	return result, [out.strip()] + calls.splitlines()


def test_post_helper_writes_one_success_status() -> None:
	result, lines = _post("o/r", HEAD, "merge", "lease_wait", BASE, "BLOCKED")
	assert result.returncode == 0, result.stderr
	assert lines[0] == f"RB_JUDGE_VERDICT_STATUS outcome=posted pr=77 head_sha={HEAD} wait=lease_wait"
	assert len(lines) == 2
	call = lines[1]
	assert f"repos/o/r/statuses/{HEAD}" in call and "state=success" in call
	assert "context=ai-review/rb-judge-verdict" in call
	assert f"description=decision=merge wait=lease_wait base={BASE} ms=blocked" in call


@pytest.mark.parametrize("args", [
	("o/r", "short", "merge", "lease_wait", BASE, "blocked"),
	("o/r", HEAD, "close_and_reissue", "lease_wait", BASE, "blocked"),
	("o/r", HEAD, "merge", "merge_failed", BASE, "blocked"),
	("o/r", HEAD, "merge", "lease_wait", "x" * 40, "blocked"),
	("o/r", HEAD, "merge", "lease_wait", BASE, "nope"),
	("../r", HEAD, "merge", "lease_wait", BASE, "blocked"),
])
def test_post_helper_rejects_invalid_input(args: tuple[str, ...]) -> None:
	result, lines = _post(*args)
	assert result.returncode == 0
	assert lines[0].startswith("RB_JUDGE_VERDICT_STATUS outcome=invalid_input")
	assert len(lines) == 1


def test_post_failure_never_fails_the_caller() -> None:
	result, lines = _post("o/r", HEAD, "merge", "blocked", BASE, "blocked", fail=True)
	assert result.returncode == 0
	assert "outcome=post_failed" in lines[0]


def _record_fn() -> str:
	lines = RB_JUDGE.read_text(encoding="utf-8").splitlines()
	start = lines.index("rb_judge_record_verdict_wait() {")
	end = next(i for i in range(start, len(lines)) if lines[i] == "}")
	return "\n".join(lines[start:end + 1]) + "\n"


@pytest.mark.parametrize("token, pr_view, env, expected", [
	("lease_wait", f"open\t{BASE}\tblocked\t{HEAD}\tpending\tai:merge-lease-wait", "", f"base={BASE} ms=blocked"),
	# An unlabelled lease wait (holder_shares_paths) would read as a granted
	# lease on every tick, so it is not recorded (issue #9991).
	("lease_wait", f"open\t{BASE}\tblocked\t{HEAD}\tpending\tai:review-blocked", "", "reason=lease_unlabelled"),
	("lease_wait", f"open\t{BASE}\tblocked\t{HEAD}\tpending", "", "reason=lease_unlabelled"),
	("auto_merge", f"closed\t{BASE}\tunknown\t{HEAD}\tsuccess", "", "reason=not_open"),
	("", f"open\t{BASE}\tblocked\t{HEAD}\tpending", "", ""),
	# A rollup that already fails would read as a change on every tick.
	("blocked", f"open\t{BASE}\tblocked\t{HEAD}\tfailure", "", "reason=checks_failing"),
	("blocked", f"open\t{BASE}\tblocked\t{'c' * 40}\tpending", "", "reason=head_moved"),
	# The base the freshness check evaluated wins over the later read.
	("checks_not_ready", f"open\t{'c' * 40}\tblocked\t{HEAD}\tpending",
		f"PR_BASE_FRESHNESS_HEAD_SHA={HEAD} PR_BASE_FRESHNESS_EVAL_BASE_SHA={BASE}", f"base={BASE} ms=blocked"),
	# An evaluation of another head is not used.
	("checks_not_ready", f"open\t{'c' * 40}\tblocked\t{HEAD}\tpending",
		f"PR_BASE_FRESHNESS_HEAD_SHA={'d' * 40} PR_BASE_FRESHNESS_EVAL_BASE_SHA={BASE}", f"base={'c' * 40} ms=blocked"),
])
def test_judge_records_only_deferred_open_verdicts(token: str, pr_view: str, env: str, expected: str) -> None:
	script = f'''
set -euo pipefail
source "{HEAD_GATE}"
{_record_fn()}
CALLS=$(mktemp)
gh_retry() {{ "$@"; }}
gh() {{ printf '%s\\n' "$*" >> "$CALLS"; case "$*" in *statuses*) return 0 ;; esac; printf {json.dumps(pr_view + chr(10))}; }}
REPOSITORY=o/r PR_NUMBER=77 RB_JUDGED_HEAD_SHA={HEAD} RB_MERGE_WAIT_TOKEN={json.dumps(token)} {env}
rb_judge_record_verdict_wait merge
echo "CALLS=$(wc -l < "$CALLS")"
echo "---"; cat "$CALLS"
'''
	result = _bash(script)
	assert result.returncode == 0, result.stderr
	out, _, calls = result.stdout.partition("---\n")
	if not expected:
		assert "RB_JUDGE_VERDICT_STATUS" not in out and "CALLS=0" in out
		return
	posts = [c for c in calls.splitlines() if "statuses/" in c]
	if expected.startswith("base="):
		assert "outcome=posted" in out
		assert len(posts) == 1 and f"description=decision=merge wait={token} {expected}" in posts[0]
	else:
		assert expected in out and posts == []
	# One PR read, never the REST pulls endpoint.
	assert sum("graphql" in c for c in calls.splitlines()) == 1


def test_judge_sets_the_wait_token_only_on_benign_deferrals() -> None:
	text = RB_JUDGE.read_text(encoding="utf-8")
	assert text.count('RB_MERGE_WAIT_TOKEN="lease_wait"') == 4
	assert text.count('RB_MERGE_WAIT_TOKEN="checks_not_ready"') == 2
	assert text.count('RB_MERGE_WAIT_TOKEN="auto_merge"') == 2
	assert text.count('RB_MERGE_WAIT_TOKEN="blocked"') == 4
	# Never next to a status a re-run should answer.
	for status in ("merge_failed", "deferred_base_moved", "checks_failed", "merge_conflict"):
		for match in re.finditer(rf'RB_MERGE_STATUS="{status}"\n(.*)\n', text):
			# Only the guarded checks_not_ready line may follow (it does not fire for checks_failed).
			assert not match.group(1).strip().startswith("RB_MERGE_WAIT_TOKEN="), status
	# Only a readable, still-pending check state (timeout) records a wait:
	# query_failed / unavailable / unknown outcomes fail open.
	guarded = '[ "${RB_MERGE_STATUS}" = "checks_not_ready" ] && [ "${PR_CHECKS_WAIT_OUTCOME:-}" = "timeout" ] && RB_MERGE_WAIT_TOKEN="checks_not_ready"'
	assert text.count(guarded) == 2
	# Every merge-class branch records before reporting.
	assert text.count("rb_judge_record_verdict_wait merge\n") == 2
	assert text.count("rb_judge_record_verdict_wait merge_with_followup\n") == 1


@pytest.mark.parametrize("fail", [False, True])
def test_base_refresh_reposts_the_verdict_with_the_new_base(fail: bool) -> None:
	new_base = "c" * 40
	script = f'''
source "{PR_CHECKS_LIB}"
CALLS=$(mktemp)
gh() {{ printf '%s\\n' "$*" >> "$CALLS"; {"return 1" if fail else "return 0"}; }}
_pr_base_freshness() {{ PR_BASE_FRESHNESS_OUTCOME=clean; }}
_pr_rb_judge_verdict_waiting 77 {HEAD} main {new_base} blocked '["ai:merge-lease-wait"]' false {json.dumps(_desc())} pending
PR_NUMBER=77 _pr_rb_judge_verdict_refresh_base o/r {HEAD}
echo "LEFT=[$PR_RB_VERDICT_BASE_REFRESH]"
echo "---"; cat "$CALLS"
'''
	result = _bash(script)
	assert result.returncode == 0, result.stderr
	out, _, calls = result.stdout.partition("---\n")
	expected = "base_refresh_failed" if fail else "base_refreshed"
	assert f"RB_JUDGE_VERDICT_STATUS outcome={expected} pr=77 head_sha={HEAD} wait=lease_wait" in out
	assert "LEFT=[]" in out
	assert len(calls.splitlines()) == 1
	assert f"repos/o/r/statuses/{HEAD}" in calls and "context=ai-review/rb-judge-verdict" in calls
	assert f"description=decision=merge wait=lease_wait base={new_base} ms=blocked" in calls


@pytest.mark.parametrize("desc, created_at, expected_at", [
	# First refresh: the replaced status's createdAt becomes at=.
	(_desc(), "2026-11-23T08:00:00Z", "2026-11-23T08:00:00Z"),
	# Later refresh: the recorded first verdict time wins over createdAt.
	(_desc() + " at=2026-11-23T07:00:00Z", "2026-11-23T08:00:00Z", "2026-11-23T07:00:00Z"),
	# An unusable createdAt carries nothing.
	(_desc(), "yesterday", ""),
])
def test_base_refresh_carries_the_first_verdict_time(desc: str, created_at: str, expected_at: str) -> None:
	new_base = "c" * 40
	script = f'''
source "{PR_CHECKS_LIB}"
CALLS=$(mktemp)
gh() {{ printf '%s\\n' "$*" >> "$CALLS"; return 0; }}
_pr_base_freshness() {{ PR_BASE_FRESHNESS_OUTCOME=clean; }}
_pr_rb_judge_verdict_waiting 77 {HEAD} main {new_base} blocked '["ai:merge-lease-wait"]' false {json.dumps(desc)} pending
PR_NUMBER=77 _pr_rb_judge_verdict_refresh_base o/r {HEAD} {json.dumps(created_at)}
cat "$CALLS"
'''
	result = _bash(script)
	assert result.returncode == 0, result.stderr
	suffix = f" at={expected_at}" if expected_at else ""
	assert f"description=decision=merge wait=lease_wait base={new_base} ms=blocked{suffix}\n" in result.stdout + "\n", result.stdout
	# Within GitHub's 140-character status description limit at the longest.
	longest = _desc(decision="merge_with_followup", wait="checks_not_ready", ms="has_hooks") + " at=2026-11-23T07:00:00Z"
	assert len(longest) <= 140


def test_base_refresh_without_a_pending_refresh_writes_nothing() -> None:
	result = _bash(f'''
source "{PR_CHECKS_LIB}"
gh() {{ echo "GH_CALLED"; }}
PR_RB_VERDICT_BASE_REFRESH=""
_pr_rb_judge_verdict_refresh_base o/r {HEAD}
''')
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == ""
