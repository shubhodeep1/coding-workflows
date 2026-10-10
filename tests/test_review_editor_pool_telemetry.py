"""The review editor replays Claude account-pool telemetry into the job log.

The editor's stderr reaches tmp_err through the heartbeat FIFO and is printed
only on failure, so a successful Claude editor run left no CLAUDE_POOL line in
the job log. scripts/review_apply_fixes.sh now replays those lines after every
attempt with a stage=editor prefix.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPLY_FIXES = ROOT / "scripts" / "review_apply_fixes.sh"
REPLAY = "{ grep -E '^(AI_ENGINE_[A-Z_]+|CLAUDE_POOL) ' \"${tmp_err}\" 2>/dev/null || true; } | sed 's/^/stage=editor /' >&2 || true"


def test_replay_runs_after_the_stderr_drain() -> None:
	text = APPLY_FIXES.read_text(encoding="utf-8")
	assert text.count(REPLAY) == 1
	drain_end = text.index('  _hb_fifo=""\n', text.index("_reap_editor_fifo_holders"))
	assert drain_end < text.index(REPLAY) < text.index('kill "${wd_pid}" 2>/dev/null || true; wait "${wd_pid}"', drain_end)


def test_replay_keeps_only_pool_and_engine_lines(tmp_path: Path) -> None:
	tmp_err = tmp_path / "err"
	tmp_err.write_text(
		"CODEX_HEARTBEAT: phase=review_apply_fixes\n"
		"CLAUDE_POOL run role=REVIEW_EDITOR account=A outcome=success reason=result_success exit_code=0\n"
		"model chatter CLAUDE_POOL not at line start\n"
		"AI_ENGINE_FALLBACK role=REVIEW_EDITOR reason=claude_unavailable action=opencode\n",
		encoding="utf-8",
	)
	result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + REPLAY], env={"tmp_err": str(tmp_err), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=True)
	assert result.stderr.splitlines() == [
		"stage=editor CLAUDE_POOL run role=REVIEW_EDITOR account=A outcome=success reason=result_success exit_code=0",
		"stage=editor AI_ENGINE_FALLBACK role=REVIEW_EDITOR reason=claude_unavailable action=opencode",
	]


def test_replay_tolerates_a_missing_capture(tmp_path: Path) -> None:
	result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + REPLAY], env={"tmp_err": str(tmp_path / "absent"), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=False)
	assert result.returncode == 0
	assert result.stderr == ""
