"""Queue backpressure hysteresis (stuck-backlog plan Phase 3 steps 2, 3 and 8).

Covers ``queue_backpressure_holds_dispatch`` in
``scripts/orchestrate_poll_process.sh`` with a persisted marker
(``QUEUE_BACKPRESSURE_STATE_FILE``): on at HIGH, held between LOW and HIGH
when the marker is active, off below LOW, fail-open rules, and the
``orchestrate_poll.yml`` wiring (repository variables and the
``actions/cache`` restore / save pair).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"

FUNCTIONS = (
	"queue_backpressure_holds_dispatch",
	"queue_backpressure_holds_uncapped_stall_action",
	"queue_backpressure_refresh_tick_state",
)
ENV_KEYS = (
	"ACTIONS_QUEUE_BACKPRESSURE_HIGH",
	"ACTIONS_QUEUE_BACKPRESSURE_LOW",
	"QUEUE_BACKPRESSURE_STATE_FILE",
	"QUEUED_RUN_ZOMBIE_MINUTES",
	"ACTIONS_QUEUED_TOTAL",
)
STATE_PATH_EXPR = "${{ runner.temp }}/queue-backpressure-state"
STATE_FILE_EXPR = STATE_PATH_EXPR + "/state.json"
CACHE_KEY = "queue-backpressure-state-v1-${{ github.run_id }}-${{ github.run_attempt }}"
SCRIPT_STEPS = (
	"Process each tracking issue",
	"Run standalone stall recovery without active projects",
	"Run unblock scan without active projects",
)


def _extract(names: tuple[str, ...]) -> str:
	lines = POLLER_SCRIPT.read_text(encoding="utf-8").splitlines()
	chunks = []
	for name in names:
		start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}()"))
		end = next(i for i in range(start, len(lines)) if lines[i] == "}")
		chunks.append("\n".join(lines[start:end + 1]))
	return "\n\n".join(chunks) + "\n"


def _q(value: str) -> str:
	return "'" + value.replace("'", "'\"'\"'") + "'"


def _run(tmp_path: Path, body: str, *, total: str | None = None, high: str | None = None,
		low: str | None = None, state_file: Path | None = None) -> subprocess.CompletedProcess[str]:
	(tmp_path / "qbp.sh").write_text(_extract(FUNCTIONS), encoding="utf-8")
	lines = ["set -euo pipefail", "source ./qbp.sh"]
	if total is not None:
		lines.append(f"ACTIONS_QUEUED_TOTAL={_q(total)}")
	if high is not None:
		lines.append(f"ACTIONS_QUEUE_BACKPRESSURE_HIGH={_q(high)}")
	if low is not None:
		lines.append(f"ACTIONS_QUEUE_BACKPRESSURE_LOW={_q(low)}")
	if state_file is not None:
		lines.append(f"QUEUE_BACKPRESSURE_STATE_FILE={_q(str(state_file))}")
	lines.append(body)
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	for key in ENV_KEYS:
		env.pop(key, None)
	return subprocess.run(["bash", "-c", "\n".join(lines) + "\n"], cwd=tmp_path, env=env,
		capture_output=True, text=True)


HOLD = "rc=0\nqueue_backpressure_holds_dispatch 'ai:planning' 'retrigger_plan' '42' || rc=$?\necho \"rc=${rc}\"\n"


def _hold(tmp_path: Path, **kwargs) -> subprocess.CompletedProcess[str]:
	result = _run(tmp_path, HOLD, **kwargs)
	assert result.returncode == 0, result.stderr
	return result


def _marker(path: Path) -> dict:
	return json.loads(path.read_text(encoding="utf-8"))


def _write_marker(path: Path, active: object, **extra: object) -> None:
	path.write_text(json.dumps({"active": active, **extra}), encoding="utf-8")


# --- hysteresis ---------------------------------------------------------------


def test_on_at_high_writes_active_marker(tmp_path: Path) -> None:
	state = tmp_path / "state" / "state.json"
	result = _hold(tmp_path, total="150", state_file=state)
	assert "rc=0" in result.stdout
	assert "QUEUE_BACKPRESSURE outcome=held state=ai:planning action=retrigger_plan issue=42 queued=150" in result.stdout
	assert "QUEUE_BACKPRESSURE outcome=on queued=150 high=150" in result.stderr
	assert "QUEUE_BACKPRESSURE outcome=on transition=off_to_on queued=150 high=150 low=50" in result.stderr
	assert _marker(state) == {"active": True, "intervals": []}


def test_held_between_low_and_high_when_marker_active(tmp_path: Path) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, True, intervals=[])
	result = _hold(tmp_path, total="80", state_file=state)
	assert "rc=0" in result.stdout
	assert "outcome=held" in result.stdout
	assert "QUEUE_BACKPRESSURE outcome=on queued=80 high=150" in result.stderr
	assert "transition=" not in result.stderr
	assert _marker(state)["active"] is True


@pytest.mark.parametrize("total", ["50", "149"])
def test_between_low_and_high_stays_off_when_marker_inactive(tmp_path: Path, total: str) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, False)
	result = _hold(tmp_path, total=total, state_file=state)
	assert "rc=1" in result.stdout
	assert "outcome=held" not in result.stdout
	assert "transition=" not in result.stderr
	assert _marker(state)["active"] is False


def test_off_below_low_clears_active_marker(tmp_path: Path) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, True)
	result = _hold(tmp_path, total="49", state_file=state)
	assert "rc=1" in result.stdout
	assert "QUEUE_BACKPRESSURE outcome=off queued=49 high=150" in result.stderr
	assert "QUEUE_BACKPRESSURE outcome=off transition=on_to_off queued=49 high=150 low=50" in result.stderr
	assert _marker(state) == {"active": False, "intervals": []}


def test_custom_low_threshold(tmp_path: Path) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, True)
	held = _hold(tmp_path, total="30", low="20", state_file=state)
	assert "rc=0" in held.stdout
	released = _hold(tmp_path, total="19", low="20", state_file=state)
	assert "rc=1" in released.stdout
	assert _marker(state)["active"] is False


@pytest.mark.parametrize(
	"content",
	[None, "not json", '"active"', '{"active": "true"}', '{"active": 1}', "[true]",
		'{"active": true}\n{"active": true}', "x" * 70000],
	ids=["missing", "corrupt", "string", "string_bool", "number", "array", "two_docs", "oversized"],
)
def test_missing_or_bad_marker_reads_inactive(tmp_path: Path, content: str | None) -> None:
	state = tmp_path / "state.json"
	if content is not None:
		state.write_text(content, encoding="utf-8")
	result = _hold(tmp_path, total="100", state_file=state)
	assert "rc=1" in result.stdout
	assert "outcome=held" not in result.stdout
	assert _marker(state) == {"active": False, "intervals": []}


def test_oversized_active_marker_is_ignored(tmp_path: Path) -> None:
	state = tmp_path / "state.json"
	state.write_text(json.dumps({"active": True, "pad": "x" * 70000}), encoding="utf-8")
	result = _hold(tmp_path, total="100", state_file=state)
	assert "rc=1" in result.stdout


def test_existing_intervals_are_kept(tmp_path: Path) -> None:
	state = tmp_path / "state.json"
	intervals = [{"start": 1, "end": 2}]
	_write_marker(state, True, intervals=intervals)
	_hold(tmp_path, total="10", state_file=state)
	assert _marker(state) == {"active": False, "intervals": intervals}


def test_non_array_intervals_are_dropped(tmp_path: Path) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, True, intervals={"bad": True})
	_hold(tmp_path, total="100", state_file=state)
	assert _marker(state) == {"active": True, "intervals": []}


def test_no_state_file_is_legacy_stateless_hold(tmp_path: Path) -> None:
	on = _hold(tmp_path, total="150")
	assert "rc=0" in on.stdout
	assert "QUEUE_BACKPRESSURE outcome=on queued=150 high=150" in on.stderr
	assert "transition=" not in on.stderr
	between = _hold(tmp_path, total="149")
	assert "rc=1" in between.stdout
	assert "QUEUE_BACKPRESSURE outcome=off queued=149 high=150" in between.stderr
	assert not list(tmp_path.glob("*.json"))


# --- fail-open rules ------------------------------------------------------------


@pytest.mark.parametrize("total", [None, "", "abc"])
def test_unknown_count_never_holds_and_keeps_marker(tmp_path: Path, total: str | None) -> None:
	state = tmp_path / "state.json"
	original = '{"active":true,"intervals":[7]}'
	state.write_text(original, encoding="utf-8")
	result = _hold(tmp_path, total=total, state_file=state)
	assert "rc=1" in result.stdout
	assert "outcome=held" not in result.stdout
	assert "QUEUE_BACKPRESSURE outcome=unknown queued=unknown high=150" in result.stderr
	assert state.read_text(encoding="utf-8") == original


def test_unknown_count_then_known_count_still_evaluates(tmp_path: Path) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, True)
	body = (
		"queue_backpressure_refresh_tick_state\n"
		"ACTIONS_QUEUED_TOTAL='100'\n" + HOLD
	)
	result = _run(tmp_path, body, state_file=state)
	assert result.returncode == 0, result.stderr
	assert "rc=0" in result.stdout
	assert "outcome=held" in result.stdout


@pytest.mark.parametrize(("high", "low"), [("abc", None), ("150", "abc"), ("1.5", None), ("150", "")])
def test_invalid_threshold_disables_hold(tmp_path: Path, high: str, low: str | None) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, True, intervals=[1])
	if low == "":
		# An empty LOW falls back to the default 50 like any unset variable.
		result = _hold(tmp_path, total="100", high=high, low=low, state_file=state)
		assert "rc=0" in result.stdout
		return
	result = _hold(tmp_path, total="999", high=high, low=low, state_file=state)
	assert "rc=1" in result.stdout
	assert "outcome=held" not in result.stdout
	assert result.stderr.count("::warning::QUEUE_BACKPRESSURE outcome=off reason=invalid_threshold") == 1
	assert _marker(state) == {"active": False, "intervals": [1]}


@pytest.mark.parametrize("high", ["-1", "0"])
def test_disabled_high_never_holds(tmp_path: Path, high: str) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, True)
	result = _hold(tmp_path, total="999", high=high, state_file=state)
	assert "rc=1" in result.stdout
	assert "QUEUE_BACKPRESSURE" not in result.stdout + result.stderr
	assert _marker(state)["active"] is False


@pytest.mark.parametrize(("high", "low"), [("150", "150"), ("150", "200"), ("150", "0"), ("150", "-5")])
def test_bad_low_falls_back_to_stateless_high(tmp_path: Path, high: str, low: str) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, True)
	body = HOLD + HOLD
	between = _run(tmp_path, body, total="100", high=high, low=low, state_file=state)
	assert between.returncode == 0, between.stderr
	assert between.stdout.count("rc=1") == 2
	warning = f"::warning::QUEUE_BACKPRESSURE outcome=stateless reason=invalid_low low={low} high={high}"
	assert between.stderr.count(warning) == 1
	assert _marker(state)["active"] is False
	at_high = _hold(tmp_path, total="150", high=high, low=low, state_file=state)
	assert "rc=0" in at_high.stdout
	assert _marker(state)["active"] is True


@pytest.mark.parametrize("action", ["attempt_merge", "escalate_human", "skip"])
def test_exempt_actions_never_hold(tmp_path: Path, action: str) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, True)
	body = f"rc=0\nqueue_backpressure_holds_dispatch 'none' '{action}' '42' || rc=$?\necho \"rc=${{rc}}\"\n"
	result = _run(tmp_path, body, total="999", state_file=state)
	assert result.returncode == 0, result.stderr
	assert "rc=1" in result.stdout
	assert "QUEUE_BACKPRESSURE" not in result.stdout + result.stderr


def test_tick_refresh_never_prints_held(tmp_path: Path) -> None:
	state = tmp_path / "state.json"
	result = _run(tmp_path, "queue_backpressure_refresh_tick_state\necho done\n", total="999", state_file=state)
	assert result.returncode == 0, result.stderr
	assert "outcome=held" not in result.stdout
	assert "done" in result.stdout
	assert _marker(state)["active"] is True


def test_evaluates_once_per_shell(tmp_path: Path) -> None:
	state = tmp_path / "state.json"
	body = (
		"queue_backpressure_refresh_tick_state\n"
		"ACTIONS_QUEUED_TOTAL='0'\n" + HOLD + HOLD
	)
	result = _run(tmp_path, body, total="200", state_file=state)
	assert result.returncode == 0, result.stderr
	assert result.stderr.count("QUEUE_BACKPRESSURE outcome=on queued=200 high=150") == 1
	assert result.stderr.count("transition=") == 1
	assert result.stdout.count("outcome=held state=ai:planning action=retrigger_plan issue=42 queued=200") == 2


def test_uncapped_stall_action_uses_hysteresis(tmp_path: Path) -> None:
	state = tmp_path / "state.json"
	_write_marker(state, True)
	body = "rc=0\nqueue_backpressure_holds_uncapped_stall_action 'retrigger_review' '42' || rc=$?\necho \"rc=${rc}\"\n"
	result = _run(tmp_path, body, total="100", state_file=state)
	assert "rc=0" in result.stdout
	assert "QUEUE_BACKPRESSURE outcome=held state=none action=retrigger_review issue=42 queued=100" in result.stdout


def test_marker_write_failure_warns_and_still_holds(tmp_path: Path) -> None:
	blocker = tmp_path / "not_a_dir"
	blocker.write_text("x", encoding="utf-8")
	result = _hold(tmp_path, total="200", state_file=blocker / "state.json")
	assert "rc=0" in result.stdout
	assert result.stderr.count("::warning::QUEUE_BACKPRESSURE outcome=marker_write_failed") == 1


# --- wiring -----------------------------------------------------------------------


def _poll_jobs() -> dict:
	import yaml

	return yaml.safe_load(POLL_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _poll_steps() -> list[dict]:
	return _poll_jobs()["poll"]["steps"]


def test_workflow_passes_variables_with_defaults() -> None:
	steps = {step.get("name"): step for step in _poll_steps()}
	for name in SCRIPT_STEPS:
		env = steps[name]["env"]
		assert env["ACTIONS_QUEUE_BACKPRESSURE_HIGH"] == "${{ vars.ACTIONS_QUEUE_BACKPRESSURE_HIGH || '150' }}"
		assert env["ACTIONS_QUEUE_BACKPRESSURE_LOW"] == "${{ vars.ACTIONS_QUEUE_BACKPRESSURE_LOW || '50' }}"
		assert env["QUEUE_BACKPRESSURE_STATE_FILE"] == STATE_FILE_EXPR
	for name in SCRIPT_STEPS[:2]:
		assert steps[name]["env"]["QUEUED_RUN_ZOMBIE_MINUTES"] == "${{ vars.QUEUED_RUN_ZOMBIE_MINUTES || '1440' }}"


def test_workflow_restores_and_saves_marker() -> None:
	steps = _poll_steps()
	names = [step.get("name") for step in steps]
	restore = steps[names.index("Restore queue backpressure state")]
	save = steps[names.index("Save queue backpressure state")]
	assert names.index("Restore queue backpressure state") < names.index("Process each tracking issue")
	assert names.index("Restore queue backpressure state") < min(names.index(n) for n in SCRIPT_STEPS)
	assert names.index("Save queue backpressure state") > names.index("Run worktree registry GC")
	assert restore["uses"] == "actions/cache/restore@v4"
	assert save["uses"] == "actions/cache/save@v4"
	assert restore.get("continue-on-error") is True
	assert save.get("continue-on-error") is True
	assert "if" not in restore
	assert save["if"] == "${{ !cancelled() }}"
	assert restore["with"]["path"] == save["with"]["path"] == STATE_PATH_EXPR
	assert restore["with"]["key"] == save["with"]["key"] == CACHE_KEY
	assert restore["with"]["restore-keys"] == "queue-backpressure-state-v1-"
	assert CACHE_KEY.startswith(restore["with"]["restore-keys"])
	assert STATE_FILE_EXPR.startswith(restore["with"]["path"] + "/")


def test_workflow_adds_no_job() -> None:
	jobs = _poll_jobs()
	assert set(jobs) == {"poll", "claude_pool_daily_usage"}


def test_refresh_runs_on_every_tick_path() -> None:
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	prime = 'prime_phase_concurrency_snapshot ".github/ai/concurrency_caps.yml"'
	# Unblock-scan post-merge verify path.
	assert (f'if _is_truthy "${{PERSIST_POSTMERGE_VERIFY_ENABLED:-false}}"; then\n    {prime}\n'
		"    queue_backpressure_refresh_tick_state\n  fi\n") in script
	# Full poller.
	assert (f'if [ "${{STANDALONE_PR_SWEEP_ONLY_ACTIVE}}" != "true" ]; then\n  {prime}\n'
		"  queue_backpressure_refresh_tick_state\nfi\n") in script
	# Standalone recovery sweep: first thing run_standalone_stall_recovery does.
	body = script[script.index("run_standalone_stall_recovery() {\n"):]
	body = body[:body.index('  if [ "${ENABLE_STANDALONE_STALL_RECOVERY}" != "true" ]; then')]
	assert "queue_backpressure_refresh_tick_state" in body
	assert 'declare -F queue_backpressure_refresh_tick_state' in body
