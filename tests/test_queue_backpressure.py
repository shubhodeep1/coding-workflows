#!/usr/bin/env python3
"""Stuck-backlog Phase 3c: the persistence budget pauses while the Actions
queue is under backpressure.

These tests pin:

- ``persist_budget_govern_issue`` skipping with
  ``PERSIST governor outcome=skipped reason=queue_backpressure_pause`` before
  any API call or ``decide`` while the queue depth is at or above
  ``ACTIONS_QUEUE_BACKPRESSURE_HIGH`` (unless ``PERSIST_QUEUE_PAUSE_ENABLED``
  is off);
- ``decide`` receiving the marker's intervals through
  ``--paused-intervals-file`` when the queue is below the threshold;
- ``queue_backpressure_record_pause_interval`` opening and closing intervals
  through the real ``scripts/persist_budget.py record-pause``, and leaving the
  marker alone when the depth is unknown;
- the ``orchestrate_poll.yml`` wiring (switch defaults, marker path, cache
  restore and save steps).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"

FUNCTIONS = (
	"_is_truthy",
	"queue_backpressure_holds_dispatch",
	"queue_backpressure_sample_state",
	"queue_backpressure_record_pause_interval",
	"persist_budget_govern_issue",
)

STUBS = r"""
issue_has_active_workflow() { return 1; }
gh_retry() { echo "GH_CALL $*" >> "${RUNTIME_DIR}/gh.log"; return 1; }
phase_cap_can_dispatch() { return 0; }
phase_cap_note_dispatch() { return 0; }
tg_notify_issue() { return 0; }
PERSIST_BUDGET_TICK_DECISIONS=0
PERSIST_BUDGET_TICK_CAP=5
PERSIST_JOURNAL_LOGIN_CACHE=pipeline-bot
GITHUB_REPOSITORY=example/repo
"""

# Stands in for scripts/persist_budget.py decide: records its argv and the
# paused-intervals file content, and answers `continue`.
FAKE_DECIDE = r"""
import json, sys
from pathlib import Path
args = sys.argv[1:]
log = {"argv": args, "paused": None}
if "--paused-intervals-file" in args:
	log["paused"] = json.loads(Path(args[args.index("--paused-intervals-file") + 1]).read_text())
Path("decide.log").write_text(json.dumps(log))
out = args[args.index("--out") + 1]
Path(out).write_text(json.dumps({"action": "continue", "reason": "within_budget", "basis": "", "summary": ""}))
"""


def _extract(names: tuple[str, ...]) -> str:
	lines = POLLER_SCRIPT.read_text(encoding="utf-8").splitlines()
	chunks = []
	for name in names:
		start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}()"))
		end = next(i for i in range(start, len(lines)) if lines[i] == "}")
		chunks.append("\n".join(lines[start:end + 1]))
	return "\n\n".join(chunks) + "\n"


def _run_bash(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	full_env = {k: v for k, v in os.environ.items() if not k.startswith(("PERSIST_", "ACTIONS_QUEUE", "QUEUE_BACKPRESSURE"))}
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	full_env["RUNTIME_DIR"] = str(cwd)
	full_env.update(env)
	return subprocess.run(["bash", "-c", script], cwd=cwd, env=full_env, capture_output=True, text=True, timeout=120)


def _setup(tmp_path: Path, *, real_helper: bool) -> None:
	(tmp_path / "scripts").mkdir()
	if real_helper:
		for name in ("persist_budget.py", "attempt_journal.py"):
			shutil.copy(REPO_ROOT / "scripts" / name, tmp_path / "scripts" / name)
	else:
		(tmp_path / "scripts" / "persist_budget.py").write_text(FAKE_DECIDE, encoding="utf-8")
	(tmp_path / "funcs.sh").write_text(_extract(FUNCTIONS) + STUBS, encoding="utf-8")


def _govern(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	_setup(tmp_path, real_helper=False)
	script = "set -euo pipefail\nsource ./funcs.sh\npersist_budget_govern_issue 42 '[]' '[]' '' standalone\necho rc=$?\n"
	return _run_bash(script, tmp_path, env)


def test_governor_skips_while_queue_backed_up(tmp_path: Path) -> None:
	result = _govern(tmp_path, {"ACTIONS_QUEUED_TOTAL": "200"})
	assert result.returncode == 0, result.stderr
	assert "rc=0" in result.stdout
	assert "PERSIST governor outcome=skipped reason=queue_backpressure_pause issue=42" in result.stderr
	assert "outcome=held" not in result.stdout
	assert not (tmp_path / "decide.log").exists()
	assert not (tmp_path / "gh.log").exists()


def test_governor_pause_switch_off_runs_decide(tmp_path: Path) -> None:
	result = _govern(tmp_path, {"ACTIONS_QUEUED_TOTAL": "200", "PERSIST_QUEUE_PAUSE_ENABLED": "false"})
	assert result.returncode == 0, result.stderr
	assert "reason=queue_backpressure_pause" not in result.stderr
	log = json.loads((tmp_path / "decide.log").read_text(encoding="utf-8"))
	assert log["argv"][0] == "decide"
	assert "--paused-intervals-file" not in log["argv"]
	assert "PERSIST governor outcome=continue reason=within_budget issue=42" in result.stderr


def test_governor_passes_marker_intervals_below_threshold(tmp_path: Path) -> None:
	marker = tmp_path / "marker" / "state.json"
	marker.parent.mkdir()
	intervals = [["2026-11-18T01:00:00Z", "2026-11-18T05:00:00Z"], ["2026-11-19T01:00:00Z", None]]
	marker.write_text(json.dumps({"active": False, "intervals": intervals}), encoding="utf-8")
	result = _govern(tmp_path, {"ACTIONS_QUEUED_TOTAL": "10", "QUEUE_BACKPRESSURE_STATE_FILE": str(marker)})
	assert result.returncode == 0, result.stderr
	assert "reason=queue_backpressure_pause" not in result.stderr
	log = json.loads((tmp_path / "decide.log").read_text(encoding="utf-8"))
	assert "--paused-intervals-file" in log["argv"]
	assert log["paused"] == intervals
	# The scratch file is removed after decide.
	assert not list(tmp_path.glob("persist_budget_paused.*.json"))


def test_governor_legacy_marker_is_no_pause(tmp_path: Path) -> None:
	marker = tmp_path / "state.json"
	marker.write_text("true\n", encoding="utf-8")
	result = _govern(tmp_path, {"ACTIONS_QUEUED_TOTAL": "10", "QUEUE_BACKPRESSURE_STATE_FILE": str(marker)})
	assert result.returncode == 0, result.stderr
	log = json.loads((tmp_path / "decide.log").read_text(encoding="utf-8"))
	assert log["paused"] == []


def test_governor_unknown_depth_fails_open(tmp_path: Path) -> None:
	result = _govern(tmp_path, {})
	assert result.returncode == 0, result.stderr
	assert "reason=queue_backpressure_pause" not in result.stderr
	assert (tmp_path / "decide.log").exists()


def _record(tmp_path: Path, marker: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	script = (
		"set -euo pipefail\nsource ./funcs.sh\n"
		"queue_backpressure_record_pause_interval\nqueue_backpressure_record_pause_interval\necho rc=$?\n"
	)
	return _run_bash(script, tmp_path, {"QUEUE_BACKPRESSURE_STATE_FILE": str(marker), **env})


def test_recorder_opens_and_closes_intervals(tmp_path: Path) -> None:
	_setup(tmp_path, real_helper=True)
	marker = tmp_path / "state" / "state.json"
	result = _record(tmp_path, marker, {"ACTIONS_QUEUED_TOTAL": "200"})
	assert result.returncode == 0, result.stderr
	data = json.loads(marker.read_text(encoding="utf-8"))
	assert data["active"] is True
	assert len(data["intervals"]) == 1 and data["intervals"][0][1] is None
	result = _record(tmp_path, marker, {"ACTIONS_QUEUED_TOTAL": "3"})
	assert result.returncode == 0, result.stderr
	data = json.loads(marker.read_text(encoding="utf-8"))
	assert data["active"] is False
	assert len(data["intervals"]) == 1 and data["intervals"][0][1] is not None


def test_recorder_leaves_marker_when_depth_unknown_or_switch_off(tmp_path: Path) -> None:
	_setup(tmp_path, real_helper=True)
	marker = tmp_path / "state.json"
	before = json.dumps({"active": True, "intervals": [["2026-11-19T01:00:00Z", None]]})
	marker.write_text(before, encoding="utf-8")
	for env in ({}, {"ACTIONS_QUEUED_TOTAL": "unknown"}, {"ACTIONS_QUEUED_TOTAL": "3", "PERSIST_QUEUE_PAUSE_ENABLED": "false"}):
		result = _record(tmp_path, marker, env)
		assert result.returncode == 0, result.stderr
		assert marker.read_text(encoding="utf-8") == before


def test_recorder_is_wired_after_phase_caps_prime() -> None:
	text = POLLER_SCRIPT.read_text(encoding="utf-8")
	prime = 'prime_phase_concurrency_snapshot ".github/ai/concurrency_caps.yml"\n'
	call = "queue_backpressure_record_pause_interval || true\n"
	assert text.count(call) == 2
	assert f'{prime}  # Queue-pause interval for the persistence budget (stuck-backlog Phase 3c).\n  {call}  run_standalone_stall_recovery' in text


def test_workflow_wiring() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	assert "PERSIST_QUEUE_PAUSE_ENABLED: ${{ vars.PERSIST_QUEUE_PAUSE_ENABLED || 'true' }}" in text
	assert "PERSIST_INHERITED_ROUNDS_EXEMPT: ${{ vars.PERSIST_INHERITED_ROUNDS_EXEMPT || 'true' }}" in text
	assert 'echo "QUEUE_BACKPRESSURE_STATE_FILE=${RUNNER_TEMP}/queue-backpressure-state/state.json"' in text
	key = "key: queue-backpressure-state-v1-${{ github.run_id }}-${{ github.run_attempt }}"
	assert text.count(key) == 2
	assert "restore-keys: queue-backpressure-state-v1-\n" in text
	restore = text.index("- name: Restore queue backpressure marker")
	save = text.index("- name: Save queue backpressure marker")
	assert text.index("- name: Create runtime workspace") < restore < save < text.index("- name: Record poll run end")
	assert "uses: actions/cache/restore@v4" in text[restore:restore + 400]
	save_block = text[save:save + 400]
	assert "uses: actions/cache/save@v4" in save_block
	assert "if: always() && steps.queue_backpressure_marker.outputs.exists == 'true'" in save_block
	assert "continue-on-error: true" in save_block
