#!/usr/bin/env python3
"""Focused S2 contract tests for implement.yml + orchestrate_poll_process.sh."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
ORCHESTRATE_POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
THREAD_REUSE_HELPER = REPO_ROOT / "scripts" / "codex_thread_reuse.sh"


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


def _iso_timestamp_minutes_ago(minutes: int) -> str:
	return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
	extract_fn 'is_truthy'
	echo
	extract_fn 'normalize_stall_guard_thresholds'
	echo
	extract_fn 'workflow_run_cache_load'
	echo
	extract_fn 'workflow_run_is_implement'
	echo
	extract_fn 'workflow_run_is_review_family'
	echo
	extract_fn 'workflow_run_stall_threshold_seconds'
	echo
	extract_fn 'workflow_run_is_fresh'
	echo
	extract_fn 'build_active_issue_set'
	echo
	extract_fn 'cancel_zombie_runs_for_issue'
} > extracted/poller_stall.sh
'''


def _extract_poller_functions(repo_dir: Path) -> None:
	script = POLLER_EXTRACT_SCRIPT.replace('"\'"${POLLER_SCRIPT}"\'"', f'"{POLLER_SCRIPT}"')
	result = _run_bash(script, cwd=repo_dir)
	if result.returncode != 0:
		raise AssertionError(f"poller extraction failed: {result.stderr}\nstdout={result.stdout}")
	out = repo_dir / "extracted" / "poller_stall.sh"
	if not out.exists() or out.stat().st_size == 0:
		raise AssertionError("poller_stall.sh was not extracted")


def _run_poller_contract(
	repo_dir: Path,
	*,
	runs: list[dict[str, object]],
	issue_num: int = 42,
	stall_guard_enabled: bool,
	implementing_threshold: str = "",
	global_threshold_minutes: int = 120,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	cancelled_runs_file = repo_dir / "cancelled_runs.txt"
	script = textwrap.dedent(
		f"""
		set -euo pipefail
		source extracted/poller_stall.sh
		STALL_THRESHOLD_MINUTES={global_threshold_minutes}
		STALL_THRESHOLD_IMPLEMENTING_MINUTES='{implementing_threshold}'
		REVIEW_RUN_MAX_RUNTIME_MINUTES=250
		CODEX_STALL_GUARD_ENABLED={'true' if stall_guard_enabled else 'false'}
		normalize_stall_guard_thresholds
		GITHUB_REPOSITORY='octo/example'
		_load_actions_runs_cached() {{
			printf '%s' "${{ACTIONS_RUNS_JSON}}"
		}}
		gh() {{
			if [ "${{1:-}}" = "api" ] && [ -n "${{2:-}}" ]; then
				printf '%s\n' "${{2}}" >> '{cancelled_runs_file}'
				return 0
			fi
			return 0
		}}
		gh_retry() {{ "$@"; }}
		ACTIVE_WORKFLOW_ISSUES="$(build_active_issue_set)"
		printf 'ACTIVE_BEGIN\n%s\nACTIVE_END\n' "${{ACTIVE_WORKFLOW_ISSUES}}"
		cancel_zombie_runs_for_issue {issue_num}
		"""
	)
	result = _run_bash(
		script,
		cwd=repo_dir,
		env={"ACTIONS_RUNS_JSON": json.dumps({"workflow_runs": runs})},
	)
	cancelled = []
	if cancelled_runs_file.exists():
		cancelled = [line.strip() for line in cancelled_runs_file.read_text(encoding="utf-8").splitlines() if line.strip()]
	return result, cancelled


def _active_issues_from_stdout(stdout: str) -> list[str]:
	marker = "ACTIVE_BEGIN\n"
	if marker not in stdout or "\nACTIVE_END" not in stdout:
		return []
	block = stdout.split(marker, 1)[1].split("\nACTIVE_END", 1)[0]
	return [line.strip() for line in block.splitlines() if line.strip()]


def test_implement_workflow_delegates_stall_guard_launch_to_thread_reuse_helper() -> None:
	text = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	for snippet in [
		"codex_stall_guard.sh",
		"CODEX_STALL_GUARD_ENABLED:",
		"CODEX_STALL_TIMEOUT_SECONDS:",
		"CODEX_STALL_KILL_GRACE_SECONDS:",
		'CODEX_STALL_GUARD_HELPER="scripts/codex_stall_guard.sh"',
		'CODEX_THREAD_REUSE_STALL_GUARD_HELPER="${CODEX_STALL_GUARD_HELPER}"',
		'CODEX_THREAD_REUSE_STATUS_FILE="${attempt_stall_status_file}"',
		'CODEX_THREAD_REUSE_TIMEOUT_SECS="${attempt_wall}"',
		'CODEX_THREAD_REUSE_PHASE="implement"',
		"bash scripts/codex_thread_reuse.sh direct-run || cmd_rc=$?",
		"observed|killed)",
		"[ \"${stall_state}\" = \"killed\" ]",
	]:
		assert snippet in text, f"missing {snippet!r} in {IMPLEMENT_WORKFLOW}"


def test_thread_reuse_helper_owns_timeout_and_stall_guard_wrapper() -> None:
	text = THREAD_REUSE_HELPER.read_text(encoding="utf-8")
	for snippet in [
		"timeout --signal=TERM --kill-after=5s",
		'runner+=("${stall_guard}" --phase "${phase}" --stdout-file "${output_file}")',
		'runner+=(--status-file "${status_file}")',
		'"${timeout_cmd[@]}" "${runner[@]}" < "${prompt_file}"',
	]:
		assert snippet in text, f"missing {snippet!r} in {THREAD_REUSE_HELPER}"


def test_orchestrate_poll_workflow_surfaces_s2_guard_envs() -> None:
	text = ORCHESTRATE_POLL_WORKFLOW.read_text(encoding="utf-8")
	for snippet in [
		"CODEX_STALL_GUARD_ENABLED: ${{ vars.CODEX_STALL_GUARD_ENABLED || 'false' }}",
		"CODEX_STALL_TIMEOUT_SECONDS: ${{ vars.CODEX_STALL_TIMEOUT_SECONDS || '600' }}",
		"CODEX_STALL_KILL_GRACE_SECONDS: ${{ vars.CODEX_STALL_KILL_GRACE_SECONDS || '30' }}",
		"STALL_THRESHOLD_IMPLEMENTING_MINUTES: ${{ vars.STALL_THRESHOLD_IMPLEMENTING_MINUTES || '' }}",
	]:
		assert snippet in text, f"missing {snippet!r} in {ORCHESTRATE_POLL_WORKFLOW}"


def test_guard_enabled_makes_old_implement_run_non_blocking_and_cancellable() -> None:
	with tempfile.TemporaryDirectory(prefix="stall-guard-poller-") as td:
		repo_dir = Path(td)
		_extract_poller_functions(repo_dir)
		runs = [
			{
				"id": 101,
				"status": "in_progress",
				"name": "AI Implement",
				"path": ".github/workflows/implement.yml",
				"head_branch": "ai/issue-42",
				"run_started_at": _iso_timestamp_minutes_ago(95),
				"created_at": _iso_timestamp_minutes_ago(95),
			}
		]
		result, cancelled = _run_poller_contract(repo_dir, runs=runs, stall_guard_enabled=True)
		assert result.returncode == 0, f"bash failed: {result.stderr}"
		assert _active_issues_from_stdout(result.stdout) == [], result.stdout
		assert cancelled == ["repos/octo/example/actions/runs/101/cancel"], cancelled


def test_review_family_runs_are_still_excluded_from_zombie_cancellation() -> None:
	with tempfile.TemporaryDirectory(prefix="stall-guard-poller-") as td:
		repo_dir = Path(td)
		_extract_poller_functions(repo_dir)
		runs = [
			{
				"id": 202,
				"status": "in_progress",
				"name": "Review Autofix",
				"path": ".github/workflows/review_autofix.yml",
				"head_branch": "ai/issue-42",
				"run_started_at": _iso_timestamp_minutes_ago(200),
				"created_at": _iso_timestamp_minutes_ago(200),
			}
		]
		result, cancelled = _run_poller_contract(repo_dir, runs=runs, stall_guard_enabled=True)
		assert result.returncode == 0, f"bash failed: {result.stderr}"
		assert cancelled == [], cancelled


def test_observe_only_mode_keeps_legacy_implement_window() -> None:
	with tempfile.TemporaryDirectory(prefix="stall-guard-poller-") as td:
		repo_dir = Path(td)
		_extract_poller_functions(repo_dir)
		runs = [
			{
				"id": 303,
				"status": "in_progress",
				"name": "AI Implement",
				"path": ".github/workflows/implement.yml",
				"head_branch": "ai/issue-42",
				"run_started_at": _iso_timestamp_minutes_ago(95),
				"created_at": _iso_timestamp_minutes_ago(95),
			}
		]
		result, cancelled = _run_poller_contract(repo_dir, runs=runs, stall_guard_enabled=False)
		assert result.returncode == 0, f"bash failed: {result.stderr}"
		assert _active_issues_from_stdout(result.stdout) == ["42"], result.stdout
		assert cancelled == [], cancelled


def test_invalid_run_started_at_falls_back_to_created_at() -> None:
	with tempfile.TemporaryDirectory(prefix="stall-guard-poller-") as td:
		repo_dir = Path(td)
		_extract_poller_functions(repo_dir)
		runs = [
			{
				"id": 404,
				"status": "in_progress",
				"name": "AI Implement",
				"path": ".github/workflows/implement.yml",
				"head_branch": "ai/issue-42",
				"run_started_at": "not-a-timestamp",
				"created_at": _iso_timestamp_minutes_ago(95),
			}
		]
		result, cancelled = _run_poller_contract(repo_dir, runs=runs, stall_guard_enabled=True)
		assert result.returncode == 0, f"bash failed: {result.stderr}"
		assert _active_issues_from_stdout(result.stdout) == [], result.stdout
		assert cancelled == ["repos/octo/example/actions/runs/404/cancel"], cancelled


def main() -> int:
	test_funcs = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except AssertionError as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
		except Exception as exc:
			print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
