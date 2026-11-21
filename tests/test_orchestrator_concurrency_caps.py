#!/usr/bin/env python3
"""Focused tests for the S5 per-state concurrency-cap rollout."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
ORCHESTRATE_LIB_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_lib.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import orchestrate_lib  # noqa: E402


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=full_env,
		capture_output=True,
		text=True,
	)


def _utc_now_iso8601() -> str:
	return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


POLLER_EXTRACT_SCRIPT = r'''
set -euo pipefail

extract_fn() {
	local fn="$1"
	awk -v fn="${fn}" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^}$/ { exit }
	' "'"${POLLER_SCRIPT}"'"
}

mkdir -p extracted
{
	echo 'set -euo pipefail'
	echo
	extract_fn 'phase_cap_state_for_action'
	echo
	extract_fn 'phase_cap_running_for_state'
	echo
	extract_fn 'phase_cap_can_dispatch'
	echo
	extract_fn 'phase_cap_note_dispatch'
	echo
	extract_fn 'prime_phase_concurrency_snapshot'
	echo
	extract_fn '_dispatch_rb_judge_for_pr'
	echo
	extract_fn 'recover_stalled_issue'
} > extracted/poller_caps.sh
'''


def _extract_poller_functions(repo_dir: Path) -> None:
	(repo_dir / "scripts").mkdir(parents=True, exist_ok=True)
	(repo_dir / "scripts" / "orchestrate_lib.py").write_text(
		ORCHESTRATE_LIB_SCRIPT.read_text(encoding="utf-8"),
		encoding="utf-8",
	)
	script = POLLER_EXTRACT_SCRIPT.replace('"\'"${POLLER_SCRIPT}"\'"', f'"{POLLER_SCRIPT}"')
	result = _run_bash(script, cwd=repo_dir)
	if result.returncode != 0:
		raise AssertionError(f"poller extraction failed: {result.stderr}\nstdout={result.stdout}")
	out = repo_dir / "extracted" / "poller_caps.sh"
	if not out.exists() or out.stat().st_size == 0:
		raise AssertionError("poller_caps.sh was not extracted")


def test_load_concurrency_caps_missing_file_disables_caps() -> None:
	with tempfile.TemporaryDirectory(prefix="caps-missing-") as td:
		caps = orchestrate_lib.load_concurrency_caps(Path(td) / "does-not-exist.yml")
		assert caps["enabled"] is False
		assert caps["status"] == "missing"
		assert caps["max_concurrent_by_state"] == {}
		assert caps["global_max_concurrent"] is None


def test_load_concurrency_caps_empty_file_disables_caps() -> None:
	with tempfile.TemporaryDirectory(prefix="caps-empty-") as td:
		path = Path(td) / "concurrency_caps.yml"
		path.write_text("\n", encoding="utf-8")
		caps = orchestrate_lib.load_concurrency_caps(path)
		assert caps["enabled"] is False
		assert caps["status"] == "empty"


def test_build_concurrency_snapshot_counts_only_fresh_supported_runs() -> None:
	with tempfile.TemporaryDirectory(prefix="caps-snapshot-") as td:
		caps_path = Path(td) / "concurrency_caps.yml"
		caps_path.write_text(
			"global_max_concurrent: -1\nmax_concurrent_by_state:\n  ai:review-blocked: 2\n",
			encoding="utf-8",
		)
		caps = orchestrate_lib.load_concurrency_caps(caps_path)
		snapshot = orchestrate_lib.build_concurrency_snapshot(
			{
				"workflow_runs": [
					{
						"status": "in_progress",
						"path": ".github/workflows/review_rb_judge_dispatch.yml",
						"name": "Internal: Review-Blocked Judge Dispatch",
						"run_started_at": "2026-06-02T11:00:00Z",
						"created_at": "2026-06-02T11:00:00Z",
					},
					{
						"status": "queued",
						"path": ".github/workflows/internal-implement.yml",
						"name": "Internal: AI Implement",
						"created_at": "2026-06-02T11:30:00Z",
					},
					{
						"status": "completed",
						"path": ".github/workflows/review_rb_judge_dispatch.yml",
						"name": "Internal: Review-Blocked Judge Dispatch",
						"run_started_at": "2026-06-02T11:15:00Z",
						"created_at": "2026-06-02T11:15:00Z",
					},
					{
						"status": "in_progress",
						"path": ".github/workflows/internal-implement.yml",
						"name": "Internal: AI Implement",
						"run_started_at": "2026-06-02T08:00:00Z",
						"created_at": "2026-06-02T08:00:00Z",
					},
				],
			},
			caps=caps,
			threshold_minutes=120,
			now_ts=1_780_400_000,
		)
		assert snapshot["enabled"] is True
		assert snapshot["max_concurrent_by_state"] == {"ai:review-blocked": 2}
		assert snapshot["running_by_state"]["ai:review-blocked"] == 1
		assert snapshot["running_by_state"]["ai:implementing"] == 1
		assert snapshot["global_running"] == 2


def test_load_concurrency_caps_accepts_whole_number_floats() -> None:
	with tempfile.TemporaryDirectory(prefix="caps-floats-") as td:
		caps_path = Path(td) / "concurrency_caps.yml"
		caps_path.write_text(
			"global_max_concurrent: 3.0\nmax_concurrent_by_state:\n  ai:review-blocked: 2.0\n",
			encoding="utf-8",
		)
		caps = orchestrate_lib.load_concurrency_caps(caps_path)
		assert caps["enabled"] is True
		assert caps["global_max_concurrent"] == 3
		assert caps["max_concurrent_by_state"] == {"ai:review-blocked": 2}


def test_load_concurrency_caps_falls_back_without_pyyaml() -> None:
	with tempfile.TemporaryDirectory(prefix="caps-no-yaml-") as td:
		caps_path = Path(td) / "concurrency_caps.yml"
		caps_path.write_text(
			"global_max_concurrent: 3.0\nmax_concurrent_by_state:\n  ai:review-blocked: 2\n",
			encoding="utf-8",
		)
		original_yaml = orchestrate_lib.yaml
		try:
			orchestrate_lib.yaml = None
			caps = orchestrate_lib.load_concurrency_caps(caps_path)
		finally:
			orchestrate_lib.yaml = original_yaml
		assert caps["enabled"] is True
		assert caps["status"] == "enabled"
		assert caps["global_max_concurrent"] == 3
		assert caps["max_concurrent_by_state"] == {"ai:review-blocked": 2}


def test_phase_cap_snapshot_uses_single_fetch_and_local_increment() -> None:
	with tempfile.TemporaryDirectory(prefix="caps-poller-") as td:
		repo_dir = Path(td)
		recent_run_ts = _utc_now_iso8601()
		_extract_poller_functions(repo_dir)
		(repo_dir / ".github" / "ai").mkdir(parents=True, exist_ok=True)
		(repo_dir / ".github" / "ai" / "concurrency_caps.yml").write_text(
			"global_max_concurrent: -1\nmax_concurrent_by_state:\n  ai:review-blocked: 2\n",
			encoding="utf-8",
		)
		script = r'''
set -euo pipefail
source extracted/poller_caps.sh
mkdir -p .github/ai runtime
RUNTIME_DIR="runtime"
STALL_THRESHOLD_MINUTES=120
unset STALL_THRESHOLD_IMPLEMENTING_MINUTES
FETCH_COUNT_FILE="fetch_count.txt"
printf '0' > "${FETCH_COUNT_FILE}"
_load_actions_runs_cached() {
	local count
	count="$(cat "${FETCH_COUNT_FILE}")"
	count="$(( count + 1 ))"
	printf '%s' "${count}" > "${FETCH_COUNT_FILE}"
	printf '%s' "${ACTIONS_RUNS_JSON}"
}
prime_phase_concurrency_snapshot .github/ai/concurrency_caps.yml
printf 'FETCH_AFTER_PRIME=%s\n' "$(cat "${FETCH_COUNT_FILE}")"
if phase_cap_can_dispatch ai:review-blocked dispatch_rb_judge 101; then
	echo ALLOW_FIRST
else
	echo DENY_FIRST
fi
phase_cap_note_dispatch ai:review-blocked
if phase_cap_can_dispatch ai:review-blocked dispatch_rb_judge 102; then
	echo ALLOW_SECOND
else
	echo DENY_SECOND
fi
printf 'FETCH_AFTER_CHECKS=%s\n' "$(cat "${FETCH_COUNT_FILE}")"
printf 'RUNNING=%s\n' "$(phase_cap_running_for_state ai:review-blocked)"
'''
		result = _run_bash(
			script,
			cwd=repo_dir,
			env={
				"ACTIONS_RUNS_JSON": json.dumps(
					{
						"workflow_runs": [
							{
								"status": "in_progress",
								"path": ".github/workflows/review_rb_judge_dispatch.yml",
								"name": "Internal: Review-Blocked Judge Dispatch",
								"run_started_at": recent_run_ts,
								"created_at": recent_run_ts,
							}
						]
					}
				)
			},
		)
		assert result.returncode == 0, result.stderr
		assert "FETCH_AFTER_PRIME=1" in result.stdout
		assert "ALLOW_FIRST" in result.stdout
		assert "DENY_SECOND" in result.stdout
		assert "phase_capped state=ai:review-blocked action=dispatch_rb_judge issue=102 limit=2 running=2" in result.stdout
		assert "FETCH_AFTER_CHECKS=1" in result.stdout
		assert "RUNNING=2" in result.stdout


def test_phase_cap_snapshot_defers_dispatch_when_actions_snapshot_unavailable() -> None:
	with tempfile.TemporaryDirectory(prefix="caps-actions-unavailable-") as td:
		repo_dir = Path(td)
		_extract_poller_functions(repo_dir)
		(repo_dir / ".github" / "ai").mkdir(parents=True, exist_ok=True)
		(repo_dir / ".github" / "ai" / "concurrency_caps.yml").write_text(
			"global_max_concurrent: -1\nmax_concurrent_by_state:\n  ai:review-blocked: 2\n",
			encoding="utf-8",
		)
		script = r'''
set -euo pipefail
source extracted/poller_caps.sh
mkdir -p .github/ai runtime
RUNTIME_DIR="runtime"
STALL_THRESHOLD_MINUTES=120
unset STALL_THRESHOLD_IMPLEMENTING_MINUTES
_load_actions_runs_cached() {
	echo "simulated actions-runs failure" >&2
	return 1
}
prime_phase_concurrency_snapshot .github/ai/concurrency_caps.yml
if phase_cap_can_dispatch ai:review-blocked dispatch_rb_judge 101; then
	echo ALLOW
else
	echo DENY
fi
printf 'STATUS=%s\n' "${PHASE_CAPS_STATUS}"
'''
		result = _run_bash(script, cwd=repo_dir)
		assert result.returncode == 0, result.stderr
		assert "DENY" in result.stdout
		assert "STATUS=actions_runs_unavailable" in result.stdout
		assert "phase_capped state=ai:review-blocked action=dispatch_rb_judge issue=101 reason=actions_runs_unavailable" in result.stdout
		assert "::warning::phase_concurrency_caps status=actions_runs_unavailable path=.github/ai/concurrency_caps.yml error=simulated actions-runs failure" in result.stderr


def test_recover_stalled_issue_skips_before_zombie_cancellation_when_capped() -> None:
	with tempfile.TemporaryDirectory(prefix="caps-recover-") as td:
		repo_dir = Path(td)
		_extract_poller_functions(repo_dir)
		state_path = repo_dir / "state.json"
		state_path.write_text(
			json.dumps({"waves": [{"issues": [{"id": "issue-1", "status": "pending"}]}]}),
			encoding="utf-8",
		)
		script = f'''
set -euo pipefail
source extracted/poller_caps.sh
STATE_FILE="{state_path}"
WAVE_IDX=0
GITHUB_REPOSITORY='octo/example'
PHASE_CAPS_ENABLED='true'
PHASE_CAPS_MAX_BY_STATE='{{"ai:clarification":0}}'
PHASE_CAPS_RUNNING_BY_STATE='{{"ai:clarification":0}}'
PHASE_CAPS_GLOBAL_MAX='-1'
PHASE_CAPS_GLOBAL_RUNNING='0'
STALL_MANAGED_LINKED_PR_CACHE=''
ENABLE_STALL_MERGED_PR_GUARD='true'
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{ printf 'open\n'; }}
get_issue_labels_json() {{ echo '[]'; }}
has_label() {{ return 1; }}
ensure_label_exists() {{ return 0; }}
add_healing_note() {{ :; }}
_check_merged_pr_guard() {{ return 1; }}
_check_fresh_push_guard_with_fallback() {{ return 1; }}
issue_has_active_workflow() {{ return 1; }}
_single_issue_linked_pr_status_graphql() {{ echo 'null'; }}
_issue_cross_ref_pr_number_last() {{ echo ''; }}
_fetch_pr_json() {{ echo '{{}}'; }}
_pr_json_closes_issue() {{ return 2; }}
tg_notify() {{ :; }}
cancel_zombie_runs_for_issue() {{ echo cancel >> cancel.log; }}
execute_stall_recovery_action() {{ echo execute >> execute.log; return 0; }}
if recover_stalled_issue 42 no_labels retrigger_pipeline 0 issue-1 30; then
	rc=0
else
	rc=$?
fi
printf 'RC=%s\n' "${{rc}}"
[ -f cancel.log ] && cat cancel.log || true
[ -f execute.log ] && cat execute.log || true
'''
		result = _run_bash(script, cwd=repo_dir)
		assert result.returncode == 0, result.stderr
		assert "phase_capped state=ai:clarification action=retrigger_pipeline issue=42 limit=0 running=0" in result.stdout
		assert "STALL_SKIP issue=42 reason=phase_capped phase=no_labels action=retrigger_pipeline" in result.stdout
		assert "RC=1" in result.stdout
		assert "cancel" in result.stdout
		assert "execute" not in result.stdout


def test_rb_judge_dispatch_returns_cap_skip_without_workflow_dispatch() -> None:
	with tempfile.TemporaryDirectory(prefix="caps-rb-judge-") as td:
		repo_dir = Path(td)
		_extract_poller_functions(repo_dir)
		script = r'''
set -euo pipefail
source extracted/poller_caps.sh
PHASE_CAPS_ENABLED='true'
PHASE_CAPS_MAX_BY_STATE='{"ai:review-blocked":0}'
PHASE_CAPS_RUNNING_BY_STATE='{"ai:review-blocked":0}'
PHASE_CAPS_GLOBAL_MAX='-1'
PHASE_CAPS_GLOBAL_RUNNING='0'
_CONFLICT_DISPATCH_TRACKER='tracker.txt'
: > "${_CONFLICT_DISPATCH_TRACKER}"
gh() {
	echo "$*" >> gh.log
}
gh_retry() { "$@"; }
if _dispatch_rb_judge_for_pr 55 99; then
	rc=0
else
	rc=$?
fi
printf 'RC=%s\n' "${rc}"
[ -f gh.log ] && cat gh.log || true
'''
		result = _run_bash(script, cwd=repo_dir)
		assert result.returncode == 0, result.stderr
		assert "phase_capped state=ai:review-blocked action=dispatch_rb_judge issue=99 limit=0 running=0" in result.stdout
		assert "RC=3" in result.stdout
		assert "workflow run" not in result.stdout


def test_automation_path_replan_waits_for_planning_cap() -> None:
	# Issue #8339: a replannable hold waits for the ai:planning cap. While
	# capped it moves no label, posts no /answer, marker or hold notice, so a
	# later tick still gets its one re-plan.
	with tempfile.TemporaryDirectory(prefix="caps-replan-") as td:
		repo_dir = Path(td)
		extracted = repo_dir / "extracted"
		extracted.mkdir()
		(extracted / "files_touched_scope_guard.py").write_text(
			(REPO_ROOT / "scripts" / "files_touched_scope_guard.py").read_text(encoding="utf-8"), encoding="utf-8")
		poller = POLLER_SCRIPT.read_text(encoding="utf-8").splitlines()
		chunks = []
		for name in ("phase_cap_running_for_state", "phase_cap_can_dispatch", "phase_cap_note_dispatch",
				"_plan_auto_approval_gate"):
			start = next(i for i, line in enumerate(poller) if line.startswith(f"{name}()"))
			end = next(i for i in range(start, len(poller)) if poller[i] == "}")
			chunks.append("\n".join(poller[start:end + 1]))
		(extracted / "gate.sh").write_text("\n\n".join(chunks) + "\n", encoding="utf-8")
		plan = "<!-- ai:plan-proposal:v1 -->\n## Files likely to change\n- `scripts/a.sh`\n- `scripts/`\n"
		comments = [{"body": plan, "user": {"login": "pipeline[bot]"}, "author_association": "NONE",
			"created_at": "2026-01-01T00:00:01Z"}]
		details = {"42": {"body": "Fix it.\n\nfiles_touched:\n  - scripts/a.sh\n", "author_login": "maintainer",
			"author_association": "OWNER", "labels": []}}
		(repo_dir / "comments.json").write_text(json.dumps(comments), encoding="utf-8")
		(repo_dir / "details.json").write_text(json.dumps(details), encoding="utf-8")
		script = r'''
set -euo pipefail
source extracted/gate.sh
GITHUB_REPOSITORY='octo/example'
RUNTIME_DIR="$PWD"
ALLOW_WORKFLOW_EDITS='true'
AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED='true'
PHASE_CAPS_ENABLED='true'
PHASE_CAPS_MAX_BY_STATE='{"ai:planning":0}'
PHASE_CAPS_RUNNING_BY_STATE='{"ai:planning":0}'
PHASE_CAPS_GLOBAL_MAX='-1'
PHASE_CAPS_GLOBAL_RUNNING='0'
unblock_trusted_login() { UNBLOCK_TRUSTED_LOGIN='pipeline[bot]'; }
_plan_heal_counterfactual_check() { return 0; }
gh_retry() { echo "GH $*" >> gh.log; return 1; }
tg_notify_issue() { :; }
_heal_counterfactual_hold_notify() { :; }
_plan_auto_approval_hold_notify() { echo notify >> notify.log; }
if _plan_auto_approval_gate 42 "$(cat details.json)" "$(cat comments.json)" 'ai:awaiting-approval'; then
	rc=0
else
	rc=$?
fi
printf 'RC=%s\n' "${rc}"
[ -f gh.log ] && cat gh.log || true
[ -f notify.log ] && cat notify.log || true
'''
		result = _run_bash(script, cwd=repo_dir)
		assert result.returncode == 0, result.stderr
		assert "phase_capped state=ai:planning action=automation_path_replan issue=42 limit=0 running=0" in result.stdout
		assert ("STALL_SKIP issue=42 reason=phase_capped phase=ai:awaiting-approval "
			"action=automation_path_replan") in result.stdout, result.stdout
		assert "RC=1" in result.stdout
		assert "GH " not in result.stdout, result.stdout
		assert "notify" not in result.stdout, result.stdout
		assert "outcome=defer" not in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Stall-recovery actions-runs self-run check
# ---------------------------------------------------------------------------
# Background (binance-blessings #557/#559): the poller escalated issues to a
# human ("stuck in ai:done") while their AI Review runs were still running,
# because the stall check used an actions-runs blob from the TTL cache, the
# ETag-304 reuse path or a fallback that lacked the poller's own in_progress
# run. These cases cover _load_actions_runs_cached source tracking (the loader
# prime_phase_concurrency_snapshot above calls first each tick),
# _ensure_actions_runs_blob_has_self_run and the escalate_human hold. They live
# in this file because it already runs in ci.yml.

STALL_ARC_POLLER_TEXT = POLLER_SCRIPT.read_text(encoding="utf-8")

STALL_ARC_SELF_RUN_ID = 4242
STALL_ARC_OTHER_RUN = {"id": 1001, "status": "completed", "name": "AI Review"}
STALL_ARC_SELF_RUN = {"id": STALL_ARC_SELF_RUN_ID, "status": "in_progress", "name": "Internal: AI Orchestrate Poll"}


def _stall_arc_iso(delta_seconds: int) -> str:
	ts = datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)
	return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stall_arc_run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	full_env["GITHUB_REPOSITORY"] = "owner/repo"
	full_env.pop("GITHUB_RUN_ID", None)
	if env:
		full_env.update(env)
	return subprocess.run(["bash", "-c", script], cwd=cwd, env=full_env, capture_output=True, text=True)


def _stall_arc_extract_fn(name: str) -> str:
	lines = STALL_ARC_POLLER_TEXT.splitlines()
	start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}()"))
	end = next(i for i in range(start, len(lines)) if lines[i] == "}")
	return "\n".join(lines[start:end + 1]) + "\n"


def _stall_arc_global_decls() -> str:
	names = (
		"_ACTIONS_RUNS_BLOB_CACHE",
		"_ACTIONS_RUNS_BLOB_READY",
		"_ACTIONS_RUNS_BLOB_SOURCE",
		"_ACTIONS_RUNS_FORCE_LIVE",
		"_ACTIONS_RUNS_SELF_CHECK_DONE",
		"STALL_ACTIVE_SET_UNVERIFIABLE",
	)
	out = []
	for name in names:
		match = re.search(rf"^declare -g {name}=.*$", STALL_ARC_POLLER_TEXT, flags=re.MULTILINE)
		assert match, f"missing global declaration for {name}"
		out.append(match.group(0))
	return "\n".join(out) + "\n"


# Fake ai_memory.py: `get` serves CACHE_JSON, `put` logs to put.log.
STALL_ARC_FAKE_AI_MEMORY = r'''
import os, sys
args = sys.argv[1:]
if args[:2] == ["actions-runs-cache", "get"]:
	print(os.environ.get("CACHE_JSON", "{}"))
elif args[:2] == ["actions-runs-cache", "put"]:
	with open("put.log", "a", encoding="utf-8") as fh:
		fh.write(" ".join(args) + "\n")
	print("{}")
'''

# gh stub: call N reads mode GH_MODE_<N> (304 | live | live_noself | fail),
# logs argv to gh.log. _safe_gh_jq (queued/completed companions) logs too.
STALL_ARC_STUBS = r'''
_memory_enabled() { return 0; }
gh_retry() { "$@"; }
_safe_gh_jq() { echo "SAFE $*" >> gh.log; printf '%s' '[]'; }
gh() {
	local n
	n="$(( $(grep -c '^GH ' gh.log 2>/dev/null || true) + 1 ))"
	echo "GH $*" >> gh.log
	local mode_var="GH_MODE_${n}"
	local mode="${!mode_var:-fail}"
	case "${mode}" in
		304) printf 'HTTP/2.0 304 Not Modified\r\netag: "e2"\r\n\r\n' ;;
		live) printf 'HTTP/2.0 200 OK\r\netag: "e3"\r\n\r\n%s' "${LIVE_BODY}" ;;
		live_noself) printf 'HTTP/2.0 200 OK\r\netag: "e3"\r\n\r\n%s' "${LIVE_NOSELF_BODY}" ;;
		*) echo "boom" >&2; return 1 ;;
	esac
}
'''


def _stall_arc_setup(tmp: Path) -> None:
	(tmp / "scripts").mkdir()
	(tmp / "scripts" / "ai_memory.py").write_text(STALL_ARC_FAKE_AI_MEMORY, encoding="utf-8")
	parts = [
		_stall_arc_global_decls(),
		_stall_arc_extract_fn("_emit_actions_runs_source_line"),
		_stall_arc_extract_fn("_load_actions_runs_cached"),
		_stall_arc_extract_fn("_ensure_actions_runs_blob_has_self_run"),
	]
	(tmp / "helpers.sh").write_text("\n".join(parts), encoding="utf-8")


def _stall_arc_cache_json(runs: list[dict], age_seconds: int, etag: str = '"e1"') -> str:
	return json.dumps({"ok": True, "hit": True, "cache": {"runs": runs, "etag": etag, "fetched_at": _stall_arc_iso(age_seconds)}})


def _stall_arc_drive(body: str, env: dict[str, str]) -> tuple[subprocess.CompletedProcess, str]:
	with tempfile.TemporaryDirectory(prefix="stall-active-runs-") as td:
		tmp = Path(td)
		_stall_arc_setup(tmp)
		base_env = {
			"ACTIONS_RUNS_CACHE_TTL_SECONDS": "300",
			"LIVE_BODY": json.dumps({"workflow_runs": [STALL_ARC_SELF_RUN]}),
			"LIVE_NOSELF_BODY": json.dumps({"workflow_runs": [{"id": 7, "status": "in_progress"}]}),
		}
		base_env.update(env)
		script = "set -euo pipefail\n" + STALL_ARC_STUBS + "\nsource helpers.sh\n" + textwrap.dedent(body)
		r = _stall_arc_run_bash(script, cwd=tmp, env=base_env)
		gh_log = (tmp / "gh.log").read_text(encoding="utf-8") if (tmp / "gh.log").exists() else ""
		assert r.returncode == 0, f"rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
		return r, gh_log


STALL_ARC_SELF_CHECK_TWICE = r'''
_load_actions_runs_cached >/dev/null
_ensure_actions_runs_blob_has_self_run
_ensure_actions_runs_blob_has_self_run
printf 'SOURCE=%s\n' "${_ACTIONS_RUNS_BLOB_SOURCE}"
printf 'UNVERIFIABLE=%s\n' "${STALL_ACTIVE_SET_UNVERIFIABLE}"
printf 'FORCE=%s\n' "${_ACTIONS_RUNS_FORCE_LIVE}"
printf 'BLOB=%s\n' "$(_load_actions_runs_cached)"
'''


def test_ttl_hit_without_self_run_refetches_live_once() -> None:
	r, gh_log = _stall_arc_drive(STALL_ARC_SELF_CHECK_TWICE, {
		"GITHUB_RUN_ID": str(STALL_ARC_SELF_RUN_ID),
		"CACHE_JSON": _stall_arc_cache_json([STALL_ARC_OTHER_RUN], age_seconds=10),
		"GH_MODE_1": "live",
	})
	gh_calls = [line for line in gh_log.splitlines() if line.startswith("GH ")]
	assert len(gh_calls) == 1, gh_log
	assert "If-None-Match" not in gh_calls[0], gh_calls[0]
	assert "ACTIONS_RUNS_SOURCE source=ttl_hit forced_live=false total=1 in_progress=0 queued=0 completed=1 self_run=absent" in r.stderr, r.stderr
	assert "ACTIONS_RUNS_SOURCE source=live forced_live=true total=1 in_progress=1 queued=0 completed=0 self_run=present" in r.stderr, r.stderr
	assert "ACTIONS_RUNS_SELF_CHECK result=refetched source=live prior_source=ttl_hit" in r.stdout, r.stdout
	assert r.stdout.count("ACTIONS_RUNS_SELF_CHECK") == 1, r.stdout
	assert "SOURCE=live" in r.stdout
	assert "UNVERIFIABLE=false" in r.stdout
	assert "FORCE=false" in r.stdout
	assert f'"id":{STALL_ARC_SELF_RUN_ID}' in r.stdout


def test_etag_304_without_self_run_refetches_live_once() -> None:
	r, gh_log = _stall_arc_drive(STALL_ARC_SELF_CHECK_TWICE, {
		"GITHUB_RUN_ID": str(STALL_ARC_SELF_RUN_ID),
		"CACHE_JSON": _stall_arc_cache_json([STALL_ARC_OTHER_RUN], age_seconds=3600),
		"GH_MODE_1": "304",
		"GH_MODE_2": "live",
	})
	gh_calls = [line for line in gh_log.splitlines() if line.startswith("GH ")]
	assert len(gh_calls) == 2, gh_log
	assert "If-None-Match" in gh_calls[0], gh_calls[0]
	assert "If-None-Match" not in gh_calls[1], gh_calls[1]
	assert "ACTIONS_RUNS_SOURCE source=etag_304 forced_live=false" in r.stderr, r.stderr
	assert "ACTIONS_RUNS_SOURCE source=live forced_live=true" in r.stderr, r.stderr
	assert "result=refetched source=live prior_source=etag_304" in r.stdout, r.stdout
	assert "UNVERIFIABLE=false" in r.stdout


def test_live_blob_with_self_run_does_not_refetch() -> None:
	r, gh_log = _stall_arc_drive(STALL_ARC_SELF_CHECK_TWICE, {
		"GITHUB_RUN_ID": str(STALL_ARC_SELF_RUN_ID),
		"CACHE_JSON": "{}",
		"GH_MODE_1": "live",
	})
	gh_calls = [line for line in gh_log.splitlines() if line.startswith("GH ")]
	assert len(gh_calls) == 1, gh_log
	assert "ACTIONS_RUNS_SOURCE source=live forced_live=false" in r.stderr, r.stderr
	assert "ACTIONS_RUNS_SELF_CHECK result=present source=live" in r.stdout, r.stdout
	assert "UNVERIFIABLE=false" in r.stdout


def test_live_blob_without_self_run_warns_and_is_used() -> None:
	r, gh_log = _stall_arc_drive(STALL_ARC_SELF_CHECK_TWICE, {
		"GITHUB_RUN_ID": str(STALL_ARC_SELF_RUN_ID),
		"CACHE_JSON": "{}",
		"GH_MODE_1": "live_noself",
	})
	gh_calls = [line for line in gh_log.splitlines() if line.startswith("GH ")]
	assert len(gh_calls) == 1, gh_log
	assert "::warning::ACTIONS_RUNS_SELF_CHECK result=absent_live" in r.stdout, r.stdout
	assert "UNVERIFIABLE=false" in r.stdout
	assert "SOURCE=live" in r.stdout


def test_failed_refetch_marks_active_set_unverifiable() -> None:
	r, gh_log = _stall_arc_drive(STALL_ARC_SELF_CHECK_TWICE, {
		"GITHUB_RUN_ID": str(STALL_ARC_SELF_RUN_ID),
		"CACHE_JSON": _stall_arc_cache_json([STALL_ARC_OTHER_RUN], age_seconds=10),
		"GH_MODE_1": "fail",
	})
	gh_calls = [line for line in gh_log.splitlines() if line.startswith("GH ")]
	assert len(gh_calls) == 1, gh_log
	assert "reason=fetch_unconfirmed" in r.stderr, r.stderr
	assert "ACTIONS_RUNS_SOURCE source=fallback forced_live=true" in r.stderr, r.stderr
	assert ("::warning::ACTIONS_RUNS_SELF_CHECK result=unverifiable source=fallback "
		f"prior_source=ttl_hit run_id={STALL_ARC_SELF_RUN_ID} reason=refetch_failed") in r.stdout, r.stdout
	assert "UNVERIFIABLE=true" in r.stdout
	assert "FORCE=false" in r.stdout


def test_missing_run_id_skips_without_fetch() -> None:
	r, gh_log = _stall_arc_drive(STALL_ARC_SELF_CHECK_TWICE, {
		"CACHE_JSON": _stall_arc_cache_json([STALL_ARC_OTHER_RUN], age_seconds=10),
	})
	assert "GH " not in gh_log, gh_log
	assert "result=skipped" in r.stdout and "reason=no_run_id" in r.stdout, r.stdout
	assert "self_run=unknown" in r.stderr, r.stderr
	assert "UNVERIFIABLE=false" in r.stdout


def test_no_repo_skips_without_fetch() -> None:
	r, gh_log = _stall_arc_drive(STALL_ARC_SELF_CHECK_TWICE, {
		"GITHUB_RUN_ID": str(STALL_ARC_SELF_RUN_ID),
		"GITHUB_REPOSITORY": "",
	})
	assert "GH " not in gh_log, gh_log
	assert "ACTIONS_RUNS_SOURCE source=no_repo" in r.stderr, r.stderr
	assert "reason=no_repo" in r.stdout, r.stdout
	assert "UNVERIFIABLE=false" in r.stdout


def test_stubbed_loader_source_unknown_skips() -> None:
	body = r'''
	_load_actions_runs_cached() { _ACTIONS_RUNS_BLOB_READY=true; _ACTIONS_RUNS_BLOB_CACHE='{"workflow_runs":[]}'; printf '%s' "${_ACTIONS_RUNS_BLOB_CACHE}"; }
	_ensure_actions_runs_blob_has_self_run
	printf 'UNVERIFIABLE=%s\n' "${STALL_ACTIVE_SET_UNVERIFIABLE}"
	'''
	r, gh_log = _stall_arc_drive(body, {"GITHUB_RUN_ID": str(STALL_ARC_SELF_RUN_ID)})
	assert "GH " not in gh_log, gh_log
	assert "reason=source_unknown" in r.stdout, r.stdout
	assert "UNVERIFIABLE=false" in r.stdout


# ---------------------------------------------------------------------------
# Escalation hold in execute_stall_recovery_action
# ---------------------------------------------------------------------------

def _stall_arc_drive_execute(action: str, unverifiable: str) -> subprocess.CompletedProcess:
	with tempfile.TemporaryDirectory(prefix="stall-exec-") as td:
		tmp = Path(td)
		parts = [
			_stall_arc_extract_fn("phase_cap_state_for_action"),
			_stall_arc_extract_fn("phase_cap_running_for_state"),
			_stall_arc_extract_fn("phase_cap_can_dispatch"),
			_stall_arc_extract_fn("phase_cap_note_dispatch"),
			_stall_arc_extract_fn("execute_stall_recovery_action"),
		]
		(tmp / "exec.sh").write_text("\n".join(parts), encoding="utf-8")
		script = textwrap.dedent(f'''
		set -euo pipefail
		source exec.sh
		PHASE_CAPS_ENABLED='false'
		STALL_ACTIVE_SET_UNVERIFIABLE='{unverifiable}'
		gh_retry() {{ echo "GH_RETRY $*" >> calls.log; }}
		gh() {{ echo "GH $*" >> calls.log; }}
		ensure_label_exists() {{ echo "ENSURE_LABEL $*" >> calls.log; }}
		get_issue_labels_json() {{ echo '["ai:needs-human"]'; }}
		has_label() {{ return 0; }}
		tg_notify() {{ echo "TG $2"; }}
		_gh_url() {{ echo "https://example/$1"; }}
		if execute_stall_recovery_action 42 ai:done {action} 1 issue-1 600; then rc=0; else rc=$?; fi
		printf 'RC=%s\\n' "${{rc}}"
		[ -f calls.log ] && cat calls.log || true
		''')
		r = _stall_arc_run_bash(script, cwd=tmp)
		assert r.returncode == 0, r.stderr
		return r


def test_execute_holds_escalate_human_when_unverifiable() -> None:
	r = _stall_arc_drive_execute("escalate_human", "true")
	assert "STALL_SKIP issue=42 reason=active_set_unverifiable phase=ai:done action=escalate_human" in r.stdout, r.stdout
	assert "RC=1" in r.stdout
	assert "GH_RETRY" not in r.stdout and "ENSURE_LABEL" not in r.stdout, r.stdout


def test_execute_escalates_when_verified() -> None:
	r = _stall_arc_drive_execute("escalate_human", "false")
	assert "active_set_unverifiable" not in r.stdout, r.stdout
	assert "ENSURE_LABEL ai:needs-human" in r.stdout, r.stdout
	assert "--add-label ai:needs-human" in r.stdout, r.stdout
	assert "RC=0" in r.stdout


def test_execute_does_not_hold_other_actions_when_unverifiable() -> None:
	r = _stall_arc_drive_execute("retrigger_pipeline", "true")
	assert "active_set_unverifiable" not in r.stdout, r.stdout
	assert "GH_RETRY gh api repos/" in r.stdout, r.stdout
	assert "RC=0" in r.stdout


# ---------------------------------------------------------------------------
# Source-order contracts
# ---------------------------------------------------------------------------

def _stall_arc_fn_body(name: str) -> str:
	return _stall_arc_extract_fn(name)


def test_standalone_calls_self_check_before_active_set() -> None:
	body = _stall_arc_fn_body("run_standalone_stall_recovery")
	check = body.index("\n    _ensure_actions_runs_blob_has_self_run || true")
	build = body.index('ACTIVE_WORKFLOW_ISSUES="$(build_active_issue_set)"')
	assert check < build
	assert "$(_ensure_actions_runs_blob_has_self_run" not in STALL_ARC_POLLER_TEXT


def test_standalone_holds_escalation_before_case_dispatch() -> None:
	body = _stall_arc_fn_body("run_standalone_stall_recovery")
	hold = body.index('if [ "${action}" = "escalate_human" ] && [ "${STALL_ACTIVE_SET_UNVERIFIABLE:-false}" = "true" ]; then')
	dispatch = body.index('    case "${action}" in\n      run_stall_judge)')
	assert hold < dispatch
	assert "reason=active_set_unverifiable" in body[hold:dispatch]


def test_tracking_block_calls_self_check_before_active_set() -> None:
	prime = STALL_ARC_POLLER_TEXT.index("      _load_actions_runs_cached >/dev/null || true\n")
	check = STALL_ARC_POLLER_TEXT.index("        _ensure_actions_runs_blob_has_self_run || true", prime)
	build = STALL_ARC_POLLER_TEXT.index('      ACTIVE_WORKFLOW_ISSUES="$(build_active_issue_set)"', prime)
	assert prime < check < build


def test_prime_snapshot_forwards_loader_stderr_on_success() -> None:
	body = _stall_arc_fn_body("prime_phase_concurrency_snapshot")
	success = body.index('if _load_actions_runs_cached >"${actions_runs_blob_file}"')
	forward = body.index('cat "${actions_runs_fetch_err_file}" >&2')
	failure = body.index("    actions_runs_fetch_rc=$?")
	assert success < forward < failure


def main() -> int:
	try:
		sys.stdout.reconfigure(line_buffering=True)
	except Exception:
		pass

	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}", flush=True)
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}", flush=True)
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
