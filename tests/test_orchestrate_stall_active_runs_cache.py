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

import sys
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


def test_judge_escalate_streak_is_not_counted_on_unverifiable_ticks() -> None:
	text = caps.STALL_ARC_POLLER_TEXT
	hold = text.index('if [ "${judge_action}" = "escalate_human" ] && [ "${STALL_ACTIVE_SET_UNVERIFIABLE:-false}" = "true" ]; then')
	increment = text.index("_judge_escalate_streak=$(( _judge_escalate_streak + 1 ))", hold)
	branch = text.index('elif [ "${judge_action}" = "escalate_human" ]; then', hold)
	assert hold < branch < increment


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
