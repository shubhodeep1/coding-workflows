#!/usr/bin/env python3
"""The poller queues one follow-up run of itself after a tick that moved a project forward (Q99).

GitHub runs internal-orchestrate-poll.yml's */5 cron every 25-60 minutes, so
a project that could take its next step right away waited for the next cron
run. scripts/orchestrate_poll_process.sh now records each tracking issue
whose persisted progress fields changed in this tick
(record_poll_progress_if_advanced, called from post_state_comment), and the
"Queue a follow-up poll after progress" step of
.github/workflows/orchestrate_poll.yml dispatches the caller workflow once
through scripts/orchestrate_force_tick.sh.

The tests run the real shell functions and the step's real run: body.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLLER = ROOT / "scripts" / "orchestrate_poll_process.sh"
POLL_WORKFLOW = ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
STEP_NAME = "Queue a follow-up poll after progress"


def _function_source(name: str) -> str:
	"""Return one top-level shell function from the poller, from `name() {` to its closing `}`."""
	lines = POLLER.read_text(encoding="utf-8").splitlines()
	start = lines.index(f"{name}() {{")
	end = lines.index("}", start)
	return "\n".join(lines[start : end + 1]) + "\n"


def _run_functions(body: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	script = "set -euo pipefail\n" + _function_source("poll_progress_fingerprint") + _function_source("record_poll_progress_if_advanced") + body
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ, **env}, check=False)


def _state(**overrides: object) -> dict[str, object]:
	state: dict[str, object] = {
		"schema_version": 1,
		"status": "in_progress",
		"current_wave": 2,
		"integration_sync_status": "in_progress",
		"final_merge_status": "",
		"security_pass_status": "",
		"security_pass_cycle": 0,
		"waves": [
			{"wave": 1, "issues": [{"id": "a", "github_issue": 11, "last_seen_phase": "done", "status_since_ts": 100}]},
			{"wave": 2, "issues": [{"id": "b", "github_issue": 12, "last_seen_phase": "implementing", "status_since_ts": 200, "stall_recovery_count": 0}]},
		],
	}
	state.update(overrides)
	return state


def _fingerprint(tmp: Path, state: dict[str, object]) -> str:
	state_file = tmp / "state.json"
	state_file.write_text(json.dumps(state), encoding="utf-8")
	result = _run_functions('poll_progress_fingerprint "${STATE_FILE}"\n', {"STATE_FILE": str(state_file)})
	assert result.returncode == 0, result.stderr
	return result.stdout.strip()


def test_fingerprint_ignores_timestamps_and_counters() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		base = _fingerprint(tmp, _state())
		assert re.fullmatch(r"[0-9a-f]{64}", base)
		churn = _state()
		churn["waves"][1]["issues"][0]["status_since_ts"] = 999  # type: ignore[index]
		churn["waves"][1]["issues"][0]["stall_recovery_count"] = 3  # type: ignore[index]
		churn["completion_status_comment_body_hash"] = "f" * 64
		churn["integration_conflict_total_dispatches"] = 4
		assert _fingerprint(tmp, churn) == base


def test_fingerprint_changes_when_a_project_moves_forward() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		base = _fingerprint(tmp, _state())
		phase = _state()
		phase["waves"][1]["issues"][0]["last_seen_phase"] = "reviewing"  # type: ignore[index]
		new_issue = _state()
		new_issue["waves"][1]["issues"].append({"id": "c", "github_issue": 13})  # type: ignore[index]
		for advanced in (
			phase,
			new_issue,
			_state(current_wave=3),
			_state(status="complete"),
			_state(integration_sync_status="validating"),
			_state(final_merge_status="merged"),
			_state(security_pass_status="fixing"),
			_state(security_pass_cycle=1),
		):
			assert _fingerprint(tmp, advanced) != base, advanced


def test_fingerprint_is_empty_for_an_unreadable_state() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		(tmp / "state.json").write_text("{not json", encoding="utf-8")
		result = _run_functions('poll_progress_fingerprint "${STATE_FILE}"\n', {"STATE_FILE": str(tmp / "state.json")})
		assert result.returncode == 0, result.stderr
		assert result.stdout == ""
		result = _run_functions('poll_progress_fingerprint "${STATE_FILE}"\n', {"STATE_FILE": str(tmp / "missing.json")})
		assert result.returncode == 0, result.stderr
		assert result.stdout == ""


_RECORD = """
POLL_PROGRESS_BASELINE_FP="$(poll_progress_fingerprint "${BASELINE_FILE}")"
POLL_PROGRESS_BASELINE_TRACKING_NUM="${BASELINE_TRACKING_NUM}"
record_poll_progress_if_advanced
record_poll_progress_if_advanced
"""


def _record(tmp: Path, baseline: dict[str, object], persisted: dict[str, object], *, tracking: str = "7126", baseline_tracking: str = "7126") -> tuple[subprocess.CompletedProcess[str], Path]:
	(tmp / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
	(tmp / "state.json").write_text(json.dumps(persisted), encoding="utf-8")
	runtime = tmp / "runtime"
	runtime.mkdir(exist_ok=True)
	result = _run_functions(
		_RECORD,
		{
			"BASELINE_FILE": str(tmp / "baseline.json"),
			"STATE_FILE": str(tmp / "state.json"),
			"RUNTIME_DIR": str(runtime),
			"TRACKING_NUM": tracking,
			"BASELINE_TRACKING_NUM": baseline_tracking,
		},
	)
	return result, runtime / "poll_progress_tracking_issues"


def test_record_writes_the_tracking_issue_once_after_progress() -> None:
	with tempfile.TemporaryDirectory() as raw:
		result, progress_file = _record(Path(raw), _state(), _state(current_wave=3))
		assert result.returncode == 0, result.stderr
		assert progress_file.read_text(encoding="utf-8") == "7126\n"
		assert result.stdout.count("POLL_PROGRESS tracking_issue=7126 outcome=advanced") == 1


def test_record_skips_a_timestamp_only_persist() -> None:
	with tempfile.TemporaryDirectory() as raw:
		churn = _state()
		churn["waves"][1]["issues"][0]["status_since_ts"] = 999  # type: ignore[index]
		result, progress_file = _record(Path(raw), _state(), churn)
		assert result.returncode == 0, result.stderr
		assert not progress_file.exists()


def test_record_ignores_a_baseline_from_another_tracking_issue() -> None:
	with tempfile.TemporaryDirectory() as raw:
		result, progress_file = _record(Path(raw), _state(), _state(current_wave=3), tracking="7126", baseline_tracking="6664")
		assert result.returncode == 0, result.stderr
		assert not progress_file.exists()


def test_record_needs_a_baseline_and_a_runtime_dir() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		(tmp / "state.json").write_text(json.dumps(_state(current_wave=3)), encoding="utf-8")
		base_env = {"STATE_FILE": str(tmp / "state.json"), "RUNTIME_DIR": str(tmp), "TRACKING_NUM": "7126"}
		no_baseline = _run_functions(
			'POLL_PROGRESS_BASELINE_FP=""\nPOLL_PROGRESS_BASELINE_TRACKING_NUM=7126\nrecord_poll_progress_if_advanced\n', base_env
		)
		assert no_baseline.returncode == 0, no_baseline.stderr
		no_runtime = _run_functions(
			'POLL_PROGRESS_BASELINE_FP=abc\nPOLL_PROGRESS_BASELINE_TRACKING_NUM=7126\nrecord_poll_progress_if_advanced\n',
			{**base_env, "RUNTIME_DIR": str(tmp / "absent")},
		)
		assert no_runtime.returncode == 0, no_runtime.stderr
		assert not (tmp / "poll_progress_tracking_issues").exists()


def test_poller_sets_the_baseline_and_records_after_each_persist() -> None:
	text = POLLER.read_text(encoding="utf-8")
	post = _function_source("post_state_comment")
	assert post.rstrip().endswith("record_poll_progress_if_advanced || true\n  return 0\n}".rstrip())
	load = text.index('  echo "${STATE_JSON}" > "${STATE_FILE}"\n  SECURITY_PASS_STATE_HASH_BEFORE=')
	normalize = text.index("  ensure_security_pass_state_fields || {", load)
	baseline = text.index("  # Progress baseline", load)
	# The baseline is taken after normalization, so filling in security-pass defaults is not progress.
	assert normalize < baseline < text.index("  SECURITY_PASS_STATE_HASH_AFTER=", load)
	block = text[baseline : baseline + 400]
	assert 'POLL_PROGRESS_BASELINE_FP="$(poll_progress_fingerprint "${STATE_FILE}")"' in block
	assert 'POLL_PROGRESS_BASELINE_TRACKING_NUM="${TRACKING_NUM}"' in block


def _step_block() -> tuple[str, str, str]:
	"""Return (if, env block, run body) of the follow-up step, parsed without PyYAML."""
	lines = POLL_WORKFLOW.read_text(encoding="utf-8").splitlines()
	start = lines.index(f"      - name: {STEP_NAME}")
	end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("      - name: "))
	block = lines[start:end]
	condition = next(line.split("if:", 1)[1].strip() for line in block if line.startswith("        if:"))
	env_start = block.index("        env:")
	run_start = block.index("        run: |")
	env = "\n".join(block[env_start + 1 : run_start])
	body = "\n".join(line[10:] if line.startswith("          ") else line.strip() for line in block[run_start + 1 :])
	return condition, env, body


def test_step_runs_only_after_a_successful_tick_with_work() -> None:
	condition, env, _ = _step_block()
	assert condition == "success() && steps.find_tracking.outputs.has_work == 'true'"
	assert "POLL_FOLLOW_UP_ON_PROGRESS_ENABLED: ${{ vars.POLL_FOLLOW_UP_ON_PROGRESS_ENABLED || 'true' }}" in env
	assert "POLL_CALLER_WORKFLOW_REF: ${{ github.workflow_ref }}" in env
	assert "FORCE_TICK_COOLDOWN_SECONDS: '300'" in env
	text = POLL_WORKFLOW.read_text(encoding="utf-8")
	assert text.index("      - name: Process each tracking issue") < text.index(f"      - name: {STEP_NAME}")


def _run_step(tmp: Path, *, progress: str | None, enabled: str = "true", caller_ref: str = "o/r/.github/workflows/ai-orchestrate-poll.yml@refs/heads/main", helper: bool = True) -> tuple[subprocess.CompletedProcess[str], Path]:
	_, _, body = _step_block()
	runtime = tmp / "runtime"
	runtime.mkdir()
	if progress is not None:
		(runtime / "poll_progress_tracking_issues").write_text(progress, encoding="utf-8")
	calls = tmp / "calls.log"
	if helper:
		scripts = tmp / ".codex-workflow-src" / "scripts"
		scripts.mkdir(parents=True)
		(scripts / "orchestrate_force_tick.sh").write_text(
			'printf "%s|%s|%s\\n" "${ORCHESTRATE_POLL_WORKFLOW_FILE}" "${FORCE_TICK_COOLDOWN_SECONDS}" "$*" >> "${CALLS_LOG}"\n',
			encoding="utf-8",
		)
	result = subprocess.run(
		["bash", "-c", body],
		cwd=tmp,
		capture_output=True,
		text=True,
		check=False,
		env={
			**os.environ,
			"RUNTIME_DIR": str(runtime),
			"POLL_FOLLOW_UP_ON_PROGRESS_ENABLED": enabled,
			"AI_MEMORY_ENABLED": "true",
			"POLL_CALLER_WORKFLOW_REF": caller_ref,
			"FORCE_TICK_COOLDOWN_SECONDS": "300",
			"GITHUB_REPOSITORY": "o/r",
			"GITHUB_RUN_ID": "42",
			"CALLS_LOG": str(calls),
		},
	)
	return result, calls


def test_step_dispatches_the_caller_workflow_after_progress() -> None:
	with tempfile.TemporaryDirectory() as raw:
		result, calls = _run_step(Path(raw), progress="7126\n6664\n")
		assert result.returncode == 0, result.stderr
		assert "POLL_FOLLOW_UP outcome=request tracking_issue=7126 workflow=ai-orchestrate-poll.yml" in result.stdout
		assert calls.read_text(encoding="utf-8").splitlines() == [
			"ai-orchestrate-poll.yml|300|--repo o/r --tracking-issue 7126 --reason poll-progress --source-workflow orchestrate_poll --run-id 42"
		]


def test_step_falls_back_to_the_internal_wrapper_for_an_unparseable_caller() -> None:
	with tempfile.TemporaryDirectory() as raw:
		result, calls = _run_step(Path(raw), progress="7126\n", caller_ref="")
		assert result.returncode == 0, result.stderr
		assert calls.read_text(encoding="utf-8").startswith("internal-orchestrate-poll.yml|300|")


def test_step_skips_without_progress() -> None:
	with tempfile.TemporaryDirectory() as raw:
		result, calls = _run_step(Path(raw), progress=None)
		assert result.returncode == 0, result.stderr
		assert "POLL_FOLLOW_UP outcome=skip reason=no_progress" in result.stdout
		assert not calls.exists()


def test_step_switch_defaults_on_and_false_turns_it_off() -> None:
	for value in ("", "TRUE", "True"):
		with tempfile.TemporaryDirectory() as raw:
			result, calls = _run_step(Path(raw), progress="7126\n", enabled=value)
			assert result.returncode == 0, result.stderr
			assert calls.exists(), value
	for value in ("false", "FALSE"):
		with tempfile.TemporaryDirectory() as raw:
			result, calls = _run_step(Path(raw), progress="7126\n", enabled=value)
			assert result.returncode == 0, result.stderr
			assert "POLL_FOLLOW_UP outcome=skip reason=disabled" in result.stdout
			assert not calls.exists()


def test_step_warns_on_an_unrecognised_switch_value() -> None:
	with tempfile.TemporaryDirectory() as raw:
		result, calls = _run_step(Path(raw), progress="7126\n", enabled="sometimes")
		assert result.returncode == 0, result.stderr
		assert "::warning::POLL_FOLLOW_UP_ON_PROGRESS_ENABLED has an unrecognised value" in result.stdout
		assert not calls.exists()


def test_step_skips_when_the_memory_cooldown_ledger_is_disabled() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		_, _, body = _step_block()
		runtime = tmp / "runtime"
		runtime.mkdir()
		(runtime / "poll_progress_tracking_issues").write_text("7126\n", encoding="utf-8")
		calls = tmp / "calls.log"
		scripts = tmp / ".codex-workflow-src" / "scripts"
		scripts.mkdir(parents=True)
		(scripts / "orchestrate_force_tick.sh").write_text('echo called >> "${CALLS_LOG}"\n', encoding="utf-8")
		result = subprocess.run(
			["bash", "-c", body],
			cwd=tmp,
			capture_output=True,
			text=True,
			check=False,
			env={
				**os.environ,
				"RUNTIME_DIR": str(runtime),
				"POLL_FOLLOW_UP_ON_PROGRESS_ENABLED": "true",
				"AI_MEMORY_ENABLED": "false",
				"POLL_CALLER_WORKFLOW_REF": "",
				"GITHUB_REPOSITORY": "o/r",
				"GITHUB_RUN_ID": "42",
				"CALLS_LOG": str(calls),
			},
		)
		assert result.returncode == 0, result.stderr
		assert "POLL_FOLLOW_UP outcome=skip reason=cooldown_unavailable" in result.stdout
		assert not calls.exists()


def test_step_skips_an_invalid_tracking_issue_and_a_missing_helper() -> None:
	with tempfile.TemporaryDirectory() as raw:
		result, calls = _run_step(Path(raw), progress="0\n")
		assert result.returncode == 0, result.stderr
		assert "reason=invalid_tracking_issue" in result.stdout
		assert not calls.exists()
	with tempfile.TemporaryDirectory() as raw:
		result, calls = _run_step(Path(raw), progress="7126\n", helper=False)
		assert result.returncode == 0, result.stderr
		assert "reason=helper_missing" in result.stdout
		assert not calls.exists()


if __name__ == "__main__":
	import sys

	failures = 0
	for name, func in sorted(globals().items()):
		if name.startswith("test_") and callable(func):
			try:
				func()
				print(f"PASS {name}")
			except Exception as exc:  # noqa: BLE001 - report every failing test
				failures += 1
				print(f"FAIL {name}: {exc!r}")
	sys.exit(1 if failures else 0)
