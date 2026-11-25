#!/usr/bin/env python3
"""Stall-recovery actions-runs self-run check (issue #9520).

The main scenarios (TTL hit, ETag 304, live with/without the self run, failed
refetch, escalation hold, call-site ordering) live in
tests/test_orchestrator_concurrency_caps.py because that file already runs in
ci.yml. This file is the acceptance target named by the issue: it reuses that
file's extract-and-run helpers and adds the cases not covered there.

Runnable under pytest or directly as `python3 tests/<this file>.py`.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_orchestrator_concurrency_caps as caps  # noqa: E402


def test_forced_refetch_live_blob_still_missing_self_run_warns_and_is_used() -> None:
	r, gh_log = caps._stall_arc_drive(caps.STALL_ARC_SELF_CHECK_TWICE, {
		"GITHUB_RUN_ID": str(caps.STALL_ARC_SELF_RUN_ID),
		"CACHE_JSON": caps._stall_arc_cache_json([caps.STALL_ARC_OTHER_RUN], age_seconds=10),
		"GH_MODE_1": "live_noself",
	})
	gh_calls = [line for line in gh_log.splitlines() if line.startswith("GH ")]
	assert len(gh_calls) == 1, gh_log
	assert "ACTIONS_RUNS_SOURCE source=ttl_hit forced_live=false" in r.stderr, r.stderr
	assert "ACTIONS_RUNS_SOURCE source=live forced_live=true total=1 in_progress=1 queued=0 completed=0 self_run=absent" in r.stderr, r.stderr
	assert ("::warning::ACTIONS_RUNS_SELF_CHECK result=refetched source=live prior_source=ttl_hit "
		f"run_id={caps.STALL_ARC_SELF_RUN_ID} reason=self_run_missing self_run=absent") in r.stdout, r.stdout
	assert r.stdout.count("ACTIONS_RUNS_SELF_CHECK") == 1, r.stdout
	assert "UNVERIFIABLE=false" in r.stdout
	assert "SOURCE=live" in r.stdout
	assert "FORCE=false" in r.stdout


def test_forced_refetch_with_self_run_present_is_not_a_warning() -> None:
	r, _gh_log = caps._stall_arc_drive(caps.STALL_ARC_SELF_CHECK_TWICE, {
		"GITHUB_RUN_ID": str(caps.STALL_ARC_SELF_RUN_ID),
		"CACHE_JSON": caps._stall_arc_cache_json([caps.STALL_ARC_OTHER_RUN], age_seconds=10),
		"GH_MODE_1": "live",
	})
	assert "ACTIONS_RUNS_SELF_CHECK result=refetched source=live" in r.stdout, r.stdout
	assert "self_run=present" in r.stdout, r.stdout
	assert "::warning::ACTIONS_RUNS_SELF_CHECK" not in r.stdout, r.stdout


def test_etag_304_then_failed_refetch_marks_active_set_unverifiable() -> None:
	r, gh_log = caps._stall_arc_drive(caps.STALL_ARC_SELF_CHECK_TWICE, {
		"GITHUB_RUN_ID": str(caps.STALL_ARC_SELF_RUN_ID),
		"CACHE_JSON": caps._stall_arc_cache_json([caps.STALL_ARC_OTHER_RUN], age_seconds=3600),
		"GH_MODE_1": "304",
		"GH_MODE_2": "fail",
	})
	gh_calls = [line for line in gh_log.splitlines() if line.startswith("GH ")]
	assert len(gh_calls) == 2, gh_log
	assert "If-None-Match" in gh_calls[0], gh_calls[0]
	assert "If-None-Match" not in gh_calls[1], gh_calls[1]
	assert "ACTIONS_RUNS_SOURCE source=etag_304 forced_live=false" in r.stderr, r.stderr
	assert "ACTIONS_RUNS_SOURCE source=fallback forced_live=true" in r.stderr, r.stderr
	assert ("::warning::ACTIONS_RUNS_SELF_CHECK result=unverifiable source=fallback "
		f"prior_source=etag_304 run_id={caps.STALL_ARC_SELF_RUN_ID} reason=refetch_failed") in r.stdout, r.stdout
	assert "UNVERIFIABLE=true" in r.stdout
	assert "FORCE=false" in r.stdout


def test_source_line_reports_unknown_self_run_for_unparseable_blob() -> None:
	# A blob that cannot be parsed must not be reported as `self_run=absent`
	# (a confirmed absence from a valid listing); it matches its `?` counts.
	body = r'''
	_ACTIONS_RUNS_BLOB_SOURCE=fallback
	_ACTIONS_RUNS_BLOB_CACHE='not json'
	_emit_actions_runs_source_line
	_ACTIONS_RUNS_BLOB_CACHE='{"workflow_runs":[]}'
	_emit_actions_runs_source_line
	'''
	r, gh_log = caps._stall_arc_drive(body, {"GITHUB_RUN_ID": str(caps.STALL_ARC_SELF_RUN_ID)})
	assert "GH " not in gh_log, gh_log
	assert ("ACTIONS_RUNS_SOURCE source=fallback forced_live=false total=? in_progress=? queued=? "
		"completed=? self_run=unknown") in r.stderr, r.stderr
	assert ("ACTIONS_RUNS_SOURCE source=fallback forced_live=false total=0 in_progress=0 queued=0 "
		"completed=0 self_run=absent") in r.stderr, r.stderr


def _run_judge_streak_block(stored: int | None, unverifiable: str) -> tuple[str, dict]:
	"""Run invoke_stall_judge's escalate-streak block (verbatim from the poller)
	for a judge escalate_human verdict; return (effective_action, state)."""
	text = caps.STALL_ARC_POLLER_TEXT
	start = text.index("  local _judge_escalate_streak=0\n")
	end = text.index('  STALL_JUDGE_TARGET_PR="$(echo', start)
	block = text[start:end]
	with tempfile.TemporaryDirectory(prefix="stall-streak-") as td:
		tmp = Path(td)
		state = {} if stored is None else {"judge_escalate_streak": {"42": stored}}
		(tmp / "state.json").write_text(json.dumps(state), encoding="utf-8")
		script = (
			"set -euo pipefail\n"
			'normalize_stall_recovery_action() { echo "retrigger_review"; }\n'
			"run_block() {\n"
			"  local _judge_state_file_ready=true _judge_state_file=state.json\n"
			"  local judge_action=escalate_human issue_num=42 _judge_force_escalate=false\n"
			"  local phase=ai:done recovery_count=1\n"
			+ block
			+ '  echo "EFFECTIVE=${effective_action}"\n'
			"}\n"
			"MAX_JUDGE_REPLAY=2\n"
			f"STALL_ACTIVE_SET_UNVERIFIABLE={unverifiable}\n"
			"run_block\n"
		)
		r = caps._stall_arc_run_bash(script, cwd=tmp)
		assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
		effective = next(line.split("=", 1)[1] for line in r.stdout.splitlines() if line.startswith("EFFECTIVE="))
		return effective, json.loads((tmp / "state.json").read_text(encoding="utf-8"))


def test_unverifiable_tick_at_streak_cap_keeps_escalate_human_for_the_hold() -> None:
	# The backstop must still produce escalate_human so execute_stall_recovery_action
	# holds it (STALL_SKIP reason=active_set_unverifiable) instead of dispatching
	# the ladder action (retrigger_review) on an unverifiable tick.
	effective, state = _run_judge_streak_block(stored=1, unverifiable="true")
	assert effective == "escalate_human", effective
	assert state == {"judge_escalate_streak": {"42": 1}}, state


def test_unverifiable_tick_below_cap_does_not_count_toward_streak() -> None:
	effective, state = _run_judge_streak_block(stored=None, unverifiable="true")
	assert effective == "retrigger_review", effective
	assert state == {}, state


def test_verified_tick_counts_escalate_streak() -> None:
	effective, state = _run_judge_streak_block(stored=1, unverifiable="false")
	assert effective == "escalate_human", effective
	assert state == {"judge_escalate_streak": {"42": 2}}, state


def _run_judge_effective_label(effective: str, unverifiable: str) -> str:
	"""Run invoke_stall_judge's human-facing effective-label block (verbatim)."""
	text = caps.STALL_ARC_POLLER_TEXT
	start = text.index('  local _judge_effective_label="${effective_action}"\n')
	end = text.index("  local judge_comment\n", start)
	block = text[start:end]
	assert "STALL_ACTIVE_SET_UNVERIFIABLE" in block and block.count("local _judge_effective_label") == 1, block
	with tempfile.TemporaryDirectory(prefix="stall-label-") as td:
		script = (
			"set -euo pipefail\n"
			"run_block() {\n"
			f"  local effective_action={effective}\n"
			+ block
			+ '  echo "LABEL=${_judge_effective_label}"\n'
			"}\n"
			f"STALL_ACTIVE_SET_UNVERIFIABLE={unverifiable}\n"
			"run_block\n"
		)
		r = caps._stall_arc_run_bash(script, cwd=Path(td))
		assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
		return next(line.split("=", 1)[1] for line in r.stdout.splitlines() if line.startswith("LABEL="))


def test_held_escalation_is_labelled_in_judge_comment_and_notify() -> None:
	# The comment and Telegram line must not report an escalation that
	# execute_stall_recovery_action holds on an unverifiable tick.
	assert _run_judge_effective_label("escalate_human", "true") == "escalate_human (held: active_set_unverifiable)"
	assert _run_judge_effective_label("escalate_human", "false") == "escalate_human"
	assert _run_judge_effective_label("retrigger_review", "true") == "retrigger_review"
	text = caps.STALL_ARC_POLLER_TEXT
	assert "**Decision (effective):** ${_judge_effective_label}" in text
	assert "effective=${_judge_effective_label}." in text


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	failed = 0
	for func in test_funcs:
		try:
			func()
			print(f"  PASS  {func.__name__}", flush=True)
		except Exception as exc:
			print(f"  FAIL  {func.__name__}: {exc}", flush=True)
			failed += 1
	print(f"\n{len(test_funcs) - failed} passed, {failed} failed, {len(test_funcs)} total", flush=True)
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
