#!/usr/bin/env python3
"""Focused tests for the S5 per-state concurrency-cap rollout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
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
