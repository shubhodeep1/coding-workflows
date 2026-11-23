#!/usr/bin/env python3
"""Contract tests for the heal counterfactual hold expiry (issue #9986).

Plan of record: docs/plans/pipeline-human-wait-elimination-plan.md, Phase 7
(R4b) and decision D6. A heal counterfactual hold nobody answers is escalated
at half of HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS and closed as not planned at the
limit, once the escalation is at least half the limit old.

The production function is extracted from scripts/orchestrate_poll_process.sh
(the same extract-and-source pattern as tests/test_stall_recovery_pr_lookup.py)
and run under stubbed `gh`, `gh_retry`, `tg_notify_issue` and `date`.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"

ISSUE = 42
LOGIN = "pipeline-owner"
NOW = calendar.timegm(time.strptime("2026-11-23T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
DAY = 86400
HOUR = 3600

HOLD_MARKER = f"<!-- ai:heal-counterfactual-hold:v1 issue={ISSUE} plan_comment=7 result=fail source=poller -->"
ESC_MARKER = f"<!-- ai:heal-counterfactual-hold-escalated:v1 issue={ISSUE} -->"
EXPIRED_MARKER = f"<!-- ai:heal-counterfactual-hold-expired:v1 issue={ISSUE} days=4 -->"


def _extract_fn(name: str) -> str:
	lines = POLLER_SCRIPT.read_text(encoding="utf-8").splitlines()
	for idx, line in enumerate(lines):
		if line.startswith(f"{name}()"):
			out = []
			for body_line in lines[idx:]:
				out.append(body_line)
				if body_line == "}":
					return "\n".join(out) + "\n"
	raise AssertionError(f"function not found: {name}")


def _iso(seconds_ago: int) -> str:
	return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - seconds_ago))


def _comment(body: str, seconds_ago: int, login: str = LOGIN, assoc: str = "OWNER") -> dict:
	return {
		"id": seconds_ago,
		"body": body,
		"created_at": _iso(seconds_ago),
		"author_association": assoc,
		"user": {"login": login},
	}


def _hold(seconds_ago: int, **kwargs) -> dict:
	return _comment(f"**Automatic approval withheld.**\n\n{HOLD_MARKER}", seconds_ago, **kwargs)


def _esc(seconds_ago: int, **kwargs) -> dict:
	return _comment(f"Hold open.\n\n{ESC_MARKER}", seconds_ago, **kwargs)


_HARNESS = r"""
set -uo pipefail
source ./fn.sh
gh_retry() { "$@"; }
gh() {
	printf '%s\x1f' "$@" >> "${GH_LOG}"
	printf '\x1e' >> "${GH_LOG}"
	if [ "${1:-}" = "api" ] && [ "${2:-}" = "-X" ] && [ "${3:-}" = "PATCH" ] && [ "${GH_PATCH_FAIL:-0}" = "1" ]; then
		return 1
	fi
	if [ "${1:-}" = "api" ] && [ "${2:-}" = "-X" ] && [ "${3:-}" = "PATCH" ] && [ "${6:-}" = "state=open" ] && [ "${GH_REOPEN_FAIL:-0}" = "1" ]; then
		return 1
	fi
	if [ "${1:-}" = "api" ] && [[ "${2:-}" == *"/comments" ]] && [ "${GH_POST_FAIL:-0}" = "1" ]; then
		return 1
	fi
	return 0
}
tg_notify_issue() {
	printf '%s\x1f' "$@" >> "${TG_LOG}"
	printf '\x1e' >> "${TG_LOG}"
}
date() { printf '%s\n' "${FAKE_NOW}"; }
if [ "${TRACE_JQ:-0}" = "1" ]; then
	jq() { echo jq >> "${JQ_LOG}"; command jq "$@"; }
fi
HEAL_CF_HOLD_EXPIRY_OUTCOME="unset"
_heal_counterfactual_hold_expiry_check "${ISSUE_NUM}" "${COMMENTS_JSON}" "${GATE_LOGIN}"
echo "RC=$? OUTCOME=${HEAL_CF_HOLD_EXPIRY_OUTCOME}"
"""


def _calls(path: Path) -> list[list[str]]:
	if not path.exists():
		return []
	raw = path.read_text(encoding="utf-8")
	return [[a for a in rec.split("\x1f") if a != ""] for rec in raw.split("\x1e") if rec]


def _run(comments: list[dict], env: dict[str, str] | None = None, unset: tuple[str, ...] = ()):
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		(tmp / "fn.sh").write_text(_extract_fn("_heal_counterfactual_hold_expiry_check"), encoding="utf-8")
		full_env = os.environ.copy()
		for key in (
			"HEAL_COUNTERFACTUAL_HOLD_EXPIRY_ENABLED",
			"HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS",
			"HEAL_CF_GATE_REASON",
			*unset,
		):
			full_env.pop(key, None)
		full_env.update({
			"PYTHONDONTWRITEBYTECODE": "1",
			"GITHUB_REPOSITORY": "owner/repo",
			"GH_LOG": str(tmp / "gh.log"),
			"TG_LOG": str(tmp / "tg.log"),
			"JQ_LOG": str(tmp / "jq.log"),
			"FAKE_NOW": str(NOW),
			"ISSUE_NUM": str(ISSUE),
			"COMMENTS_JSON": json.dumps(comments),
			"GATE_LOGIN": LOGIN,
		})
		if env:
			full_env.update(env)
		proc = subprocess.run(
			["bash", "-c", _HARNESS], cwd=tmp, env=full_env, capture_output=True, text=True, timeout=60
		)
		jq_calls = (tmp / "jq.log").read_text(encoding="utf-8") if (tmp / "jq.log").exists() else ""
		return proc, _calls(tmp / "gh.log"), _calls(tmp / "tg.log"), jq_calls


def _log_line(proc) -> str:
	lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("HEAL_COUNTERFACTUAL_HOLD_EXPIRY ")]
	assert len(lines) == 1, proc.stdout + proc.stderr
	assert re.fullmatch(
		r"HEAL_COUNTERFACTUAL_HOLD_EXPIRY issue=[0-9a-z]+ outcome=(escalated|expired|kept|skipped) reason=[a-z_]+ age_days=[0-9]+",
		lines[0],
	), lines[0]
	assert "RC=0 " in proc.stdout, proc.stdout
	return lines[0]


def _assert_outcome(proc, outcome: str, reason: str, age_days: int | None = None) -> None:
	line = _log_line(proc)
	assert f"outcome={outcome} reason={reason} " in line, line
	assert f"OUTCOME={outcome}" in proc.stdout, proc.stdout
	if age_days is not None:
		assert line.endswith(f" age_days={age_days}"), line


# --- kept / skipped ---------------------------------------------------------


def test_kept_below_half_limit():
	proc, gh, tg, _ = _run([_hold(1 * DAY)])
	_assert_outcome(proc, "kept", "below_half_limit", 1)
	assert gh == [] and tg == []


def test_no_hold_marker_is_skipped():
	proc, gh, tg, _ = _run([_comment("plain comment", 10 * DAY)])
	_assert_outcome(proc, "skipped", "no_hold_marker")
	assert gh == [] and tg == []


def test_heal_route_unknown_is_skipped():
	proc, gh, tg, _ = _run([_hold(10 * DAY)], env={"HEAL_CF_GATE_REASON": "heal_route_unknown"})
	_assert_outcome(proc, "skipped", "heal_route_unknown")
	assert gh == [] and tg == []


def test_unreadable_snapshot_is_skipped():
	proc, gh, tg, _ = _run([], env={"COMMENTS_JSON": '{"not": "an array"}'})
	_assert_outcome(proc, "skipped", "snapshot_unreadable")
	assert gh == [] and tg == []


# --- escalation -------------------------------------------------------------


def test_escalates_once_at_half_limit():
	proc, gh, tg, _ = _run([_hold(2 * DAY)])
	_assert_outcome(proc, "escalated", "half_limit", 2)
	assert len(gh) == 1, gh
	assert gh[0][:2] == ["api", f"repos/owner/repo/issues/{ISSUE}/comments"], gh
	body = next(a for a in gh[0] if a.startswith("body="))
	assert body.rstrip().endswith(ESC_MARKER), body
	assert "`/approved`" in body and "`/reclarify`" in body
	assert len(tg) == 1 and tg[0][0] == str(ISSUE) and tg[0][2] == "WARNING", tg
	assert "closes as not planned at 3 days" in tg[0][1]


def test_second_run_with_escalation_marker_posts_nothing():
	proc, gh, tg, _ = _run([_hold(2 * DAY), _esc(1 * HOUR)])
	_assert_outcome(proc, "kept", "escalated_awaiting_expiry", 2)
	assert gh == [] and tg == []


def test_failed_escalation_post_sends_no_telegram():
	proc, gh, tg, _ = _run([_hold(2 * DAY)], env={"GH_POST_FAIL": "1"})
	_assert_outcome(proc, "skipped", "escalation_post_failed")
	assert len(gh) == 1 and tg == []


# --- expiry -----------------------------------------------------------------


def test_expires_after_limit_closing_before_commenting():
	proc, gh, tg, _ = _run([_hold(4 * DAY), _esc(2 * DAY)])
	_assert_outcome(proc, "expired", "max_days", 4)
	assert len(gh) == 2, gh
	assert gh[0] == ["api", "-X", "PATCH", f"repos/owner/repo/issues/{ISSUE}",
		"-f", "state=closed", "-f", "state_reason=not_planned"], gh[0]
	assert gh[1][:2] == ["api", f"repos/owner/repo/issues/{ISSUE}/comments"], gh[1]
	body = next(a for a in gh[1] if a.startswith("body="))
	assert body.rstrip().endswith(f"<!-- ai:heal-counterfactual-hold-expired:v1 issue={ISSUE} days=4 -->"), body
	assert "reopen" in body and "`/reclarify`" in body and "`/approved`" in body
	assert len(tg) == 1 and tg[0][2] == "DEBUG", tg
	# Labels are never touched.
	assert not any("labels" in a for call in gh for a in call), gh


def test_legacy_hold_past_limit_escalates_instead_of_closing():
	proc, gh, tg, _ = _run([_hold(13 * DAY)])
	_assert_outcome(proc, "escalated", "half_limit", 13)
	assert len(gh) == 1 and "PATCH" not in gh[0], gh


def test_kept_inside_escalation_window():
	proc, gh, tg, _ = _run([_hold(13 * DAY), _esc(1 * DAY)])
	_assert_outcome(proc, "kept", "escalation_window", 13)
	assert gh == [] and tg == []


def test_close_failure_posts_nothing_and_retries_next_tick():
	proc, gh, tg, _ = _run([_hold(4 * DAY), _esc(2 * DAY)], env={"GH_PATCH_FAIL": "1"})
	_assert_outcome(proc, "skipped", "close_failed")
	assert len(gh) == 1 and gh[0][2] == "PATCH", gh
	assert tg == []


def test_failed_expired_comment_reopens_and_retries_next_tick():
	# The expired marker is the only reopen guard, so a close without it is
	# undone and the next tick retries the whole expiry.
	proc, gh, tg, _ = _run([_hold(4 * DAY), _esc(2 * DAY)], env={"GH_POST_FAIL": "1"})
	_assert_outcome(proc, "skipped", "expired_marker_post_failed")
	assert "::warning::" in proc.stdout
	assert len(gh) == 3, gh
	assert gh[0][:3] == ["api", "-X", "PATCH"] and "state=closed" in gh[0], gh[0]
	assert gh[2] == ["api", "-X", "PATCH", f"repos/owner/repo/issues/{ISSUE}", "-f", "state=open"], gh[2]
	# A lost response may have left the marker behind: operators are told.
	assert "appears anyway" in proc.stdout
	# This branch repeats every tick while the POST fails, so the Telegram
	# send is DEBUG (filtered by ALERT_MSG_LEVEL), not a per-tick WARNING.
	assert len(tg) == 1 and tg[0][2] == "DEBUG" and "appears anyway" in tg[0][1], tg


def test_failed_expired_comment_and_reopen_stays_expired_and_warns():
	proc, gh, tg, _ = _run([_hold(4 * DAY), _esc(2 * DAY)], env={"GH_POST_FAIL": "1", "GH_REOPEN_FAIL": "1"})
	_assert_outcome(proc, "expired", "reopen_failed")
	assert "reopening it failed" in proc.stdout
	assert len(gh) == 3, gh
	assert len(tg) == 1 and tg[0][2] == "WARNING", tg
	assert "/approved or /reclarify" in tg[0][1], tg


# --- answers and trust ------------------------------------------------------


def test_human_approved_after_hold_keeps_issue():
	proc, gh, tg, _ = _run([_hold(10 * DAY), _comment("/approved", 1 * DAY, login="maintainer")])
	_assert_outcome(proc, "kept", "answered")
	assert gh == [] and tg == []


def test_clock_restarts_after_answer_and_new_hold():
	comments = [
		_hold(10 * DAY),
		_comment("/reclarify please re-plan", 5 * DAY, login="maintainer", assoc="MEMBER"),
		_hold(1 * DAY),
	]
	proc, gh, tg, _ = _run(comments)
	_assert_outcome(proc, "kept", "below_half_limit", 1)


def test_auto_answers_and_stall_recovery_answers_are_not_human_answers():
	comments = [
		_hold(2 * DAY),
		_comment("/answer [auto-answered-by-orchestrator]\n\nAuto answer was posted.", 1 * DAY),
		_comment("/answer\n\n_Orchestrator stall recovery: planning phase stalled._", 1 * HOUR),
		_comment("/approved\n\n_Standalone stall recovery: plan approval stalled. Auto-approving to proceed._", 30 * 60),
		_comment("/answer\n\n_Standalone stall recovery: planning stalled. Re-triggering plan generation._", 10 * 60),
	]
	proc, gh, tg, _ = _run(comments)
	_assert_outcome(proc, "escalated", "half_limit")


def test_untrusted_answer_and_forged_markers_are_ignored():
	# An outside /approved does not keep the hold.
	proc, gh, _, _ = _run([_hold(2 * DAY), _comment("/approved", 1 * HOUR, login="outsider", assoc="NONE")])
	_assert_outcome(proc, "escalated", "half_limit")
	# A bot /approved is not a human answer either.
	proc, _, _, _ = _run([_hold(2 * DAY), _comment("/approved", 1 * HOUR, login="helper[bot]", assoc="NONE")])
	_assert_outcome(proc, "escalated", "half_limit")
	# A forged old hold marker from an outsider does not start a clock.
	proc, gh, _, _ = _run([_hold(20 * DAY, login="outsider", assoc="NONE")])
	_assert_outcome(proc, "skipped", "no_hold_marker")
	assert gh == []
	# A forged escalation marker from an outsider does not allow a close.
	proc, gh, _, _ = _run([_hold(4 * DAY), _esc(2 * DAY, login="outsider", assoc="CONTRIBUTOR")])
	_assert_outcome(proc, "escalated", "half_limit")
	assert not any("PATCH" in call for call in gh), gh
	# An untrusted forged expired marker does not mean "reopened".
	proc, _, _, _ = _run([_hold(1 * DAY), _comment(EXPIRED_MARKER, 1 * HOUR, login="outsider", assoc="NONE")])
	_assert_outcome(proc, "kept", "below_half_limit")


def test_escalation_older_than_current_hold_does_not_count():
	comments = [
		_hold(20 * DAY),
		_esc(18 * DAY),
		_comment("/reclarify", 10 * DAY, login="maintainer"),
		_hold(4 * DAY),
	]
	proc, gh, _, _ = _run(comments)
	_assert_outcome(proc, "escalated", "half_limit", 4)
	assert len(gh) == 1 and "PATCH" not in gh[0]


def test_trusted_expired_marker_means_reopened():
	proc, gh, tg, _ = _run([_hold(30 * DAY), _esc(20 * DAY), _comment(EXPIRED_MARKER, 10 * DAY)])
	_assert_outcome(proc, "kept", "reopened")
	assert gh == [] and tg == []


# --- switch and limit -------------------------------------------------------


def test_switch_false_restores_todays_behaviour():
	for value in ("false", "FALSE", " False "):
		proc, gh, tg, jq_calls = _run(
			[_hold(4 * DAY), _esc(2 * DAY)],
			env={"HEAL_COUNTERFACTUAL_HOLD_EXPIRY_ENABLED": value, "TRACE_JQ": "1"},
		)
		_assert_outcome(proc, "skipped", "disabled", 0)
		assert gh == [] and tg == [], value
		assert jq_calls == "", value


def test_invalid_switch_warns_and_is_treated_as_true():
	proc, gh, _, _ = _run([_hold(2 * DAY)], env={"HEAL_COUNTERFACTUAL_HOLD_EXPIRY_ENABLED": "maybe"})
	assert "::warning::HEAL_COUNTERFACTUAL_HOLD_EXPIRY_ENABLED has invalid value; treating as true" in proc.stdout
	_assert_outcome(proc, "escalated", "half_limit")


def test_max_days_default_validation_and_clamping():
	# Unset: 3 days (half = 36h).
	proc, _, _, _ = _run([_hold(35 * HOUR)])
	_assert_outcome(proc, "kept", "below_half_limit")
	proc, _, _, _ = _run([_hold(37 * HOUR)])
	_assert_outcome(proc, "escalated", "half_limit")
	# Non-numeric: warning, then 3.
	proc, _, _, _ = _run([_hold(37 * HOUR)], env={"HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS": "abc"})
	assert "::warning::HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS must be an integer" in proc.stdout
	_assert_outcome(proc, "escalated", "half_limit")
	proc, _, _, _ = _run([_hold(35 * HOUR)], env={"HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS": "abc"})
	_assert_outcome(proc, "kept", "below_half_limit")
	# 0 clamps to 1 day (half = 12h).
	proc, _, _, _ = _run([_hold(13 * HOUR)], env={"HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS": "0"})
	assert "::warning::HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS below 1; using 1" in proc.stdout
	_assert_outcome(proc, "escalated", "half_limit")
	# 99 clamps to 30 days (half = 15 days).
	proc, _, _, _ = _run([_hold(14 * DAY)], env={"HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS": "99"})
	assert "::warning::HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS above 30; using 30" in proc.stdout
	_assert_outcome(proc, "kept", "below_half_limit")


# --- static wiring ----------------------------------------------------------


def test_script_defaults_and_hold_branch_order():
	poller = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert 'HEAL_COUNTERFACTUAL_HOLD_EXPIRY_ENABLED="${HEAL_COUNTERFACTUAL_HOLD_EXPIRY_ENABLED:-true}"' in poller
	assert 'HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS="${HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS:-3}"' in poller
	assert 'STALL_PR_RESOLVE_IMPLEMENTATION_FIRST_ENABLED="${STALL_PR_RESOLVE_IMPLEMENTATION_FIRST_ENABLED:-true}"' in poller
	assert "declare -gA STALL_PR_DOCS_PLANS_ONLY_CACHE=()" in poller
	hold = poller.split('if [ "${gate_reason}" = "heal_counterfactual_hold" ]; then', 1)[1].split("    return 1\n", 1)[0]
	stale = hold.index("_heal_stale_close_check ")
	stale_guard = hold.index('if [ "${HEAL_STALE_CLOSE_OUTCOME:-}" != "closed" ]; then')
	expiry = hold.index('_heal_counterfactual_hold_expiry_check "${issue_num}" "${comments_json}" "${gate_login}"')
	expired_guard = hold.index('if [ "${HEAL_CF_HOLD_EXPIRY_OUTCOME:-}" != "expired" ]; then')
	notify = hold.index("_heal_counterfactual_hold_notify ")
	assert stale < stale_guard < expiry < expired_guard < notify
	body = _extract_fn("_heal_counterfactual_hold_expiry_check")
	assert "-f state=closed -f state_reason=not_planned" in body
	assert "labels" not in body.split("gh_retry gh api -X PATCH", 1)[1].split("\n", 2)[1]
	assert 'echo "HEAL_COUNTERFACTUAL_HOLD_EXPIRY issue=' in body
	# Only the comment snapshot is read: no new GitHub read in the check.
	assert "--paginate" not in body and "gh api -X GET" not in body


def test_workflow_forwards_the_switches():
	workflow = POLL_WORKFLOW.read_text(encoding="utf-8")
	gate_count = workflow.count(
		"HEAL_COUNTERFACTUAL_GATE_ENABLED: ${{ vars.HEAL_COUNTERFACTUAL_GATE_ENABLED || 'true' }}")
	assert gate_count == 5
	for line in (
		"HEAL_COUNTERFACTUAL_HOLD_EXPIRY_ENABLED: ${{ vars.HEAL_COUNTERFACTUAL_HOLD_EXPIRY_ENABLED || 'true' }}",
		"HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS: ${{ vars.HEAL_COUNTERFACTUAL_HOLD_MAX_DAYS || '3' }}",
		"STALL_PR_RESOLVE_IMPLEMENTATION_FIRST_ENABLED: ${{ vars.STALL_PR_RESOLVE_IMPLEMENTATION_FIRST_ENABLED || 'true' }}",
	):
		assert workflow.count(line) == gate_count, line


def test_log_prefixes_are_emitted():
	poller = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert 'echo "HEAL_COUNTERFACTUAL_HOLD_EXPIRY issue=' in poller
	assert 'echo "STALL_JUDGE_LINKED_PR issue=' in poller


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	failed = 0
	for func in test_funcs:
		try:
			func()
			print(f"  PASS  {func.__name__}")
		except Exception as exc:  # noqa: BLE001
			print(f"  FAIL  {func.__name__}: {exc}")
			failed += 1
	print(f"\n{len(test_funcs) - failed} passed, {failed} failed, {len(test_funcs)} total")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
