#!/usr/bin/env python3
"""A deliberate `BLOCKED:` implement verdict must park the issue in ai:blocked.

Before this, implement.yml's "Comment on issue failure" step returned every
failed run to ai:awaiting-approval, and the orchestrator poller's stall
recovery re-posted /approved on the same blocked plan every stall window
(tele-funtoken-msg-scoring#4395: four identical BLOCKED runs over ~15h).
The step's `run:` block is executed with a mocked `gh`, as in
test_implement_post_codex_recovery.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from tests.test_implement_post_codex_recovery import _extract_run_script, _render_github_expressions

REPO_ROOT = Path(__file__).resolve().parent.parent

MOCK_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
log = Path(os.environ["MOCK_GH_LOG"])
args = sys.argv[1:]
with log.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
if args[:1] == ["api"] and "--jq" in args:
    sys.stdout.write(json.dumps(os.environ.get("MOCK_ISSUE_LABELS", "[]")))
    sys.exit(0)
if os.environ.get("MOCK_FAIL_BLOCKED_EDIT") == "1" and args[:2] == ["issue", "edit"] and "ai:blocked" in args:
    sys.stderr.write("label edit failed\n")
    sys.exit(1)
sys.exit(0)
'''


def _run_failure_step(*, blocked_reason: str | None, labels: list[str], fail_blocked_edit: bool = False,
		runtime_out: Path | None = None) -> list[list[str]]:
	script = _render_github_expressions(_extract_run_script("Comment on issue failure"))
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(runtime_out) if runtime_out is not None else Path(tmp)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		(bin_dir / "gh").write_text(MOCK_GH, encoding="utf-8")
		(bin_dir / "gh").chmod(0o755)
		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir()
		if blocked_reason is not None:
			(runtime_dir / "codex_blocked.flag").write_text(blocked_reason + "\n", encoding="utf-8")
		log = tmp_path / "gh.log"
		log.write_text("", encoding="utf-8")
		env = os.environ.copy()
		for key in ("BASH_ENV", "ENV"):
			env.pop(key, None)
		env.update(
			{
				"PATH": f"{bin_dir}:{env['PATH']}",
				"MOCK_GH_LOG": str(log),
				"MOCK_ISSUE_LABELS": json.dumps(labels),
				"MOCK_FAIL_BLOCKED_EDIT": "1" if fail_blocked_edit else "0",
				"ISSUE_NUMBER": "4395",
				"ISSUE_URL": "https://github.com/owner/repo/issues/4395",
				"RUNTIME_DIR": str(runtime_dir),
				"IMPLEMENT_STAGED_SUPPORT_RUN_DIR": str(tmp_path / "missing-support-dir"),
				"ISSUE_META_FILE": "",
				"GH_TOKEN": "x",
				"PYTHONDONTWRITEBYTECODE": "1",
			}
		)
		script_path = tmp_path / "step.sh"
		script_path.write_text(script, encoding="utf-8")
		proc = subprocess.run(["bash", str(script_path)], cwd=str(tmp_path), env=env, text=True, capture_output=True, timeout=60)
		if fail_blocked_edit:
			assert proc.returncode != 0
		else:
			assert proc.returncode == 0, proc.stderr + proc.stdout
		return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _label_edits(calls: list[list[str]]) -> list[list[str]]:
	return [c for c in calls if c[:2] == ["issue", "edit"]]


def _comment_bodies(calls: list[list[str]]) -> list[str]:
	bodies = []
	for call in calls:
		if call[:1] == ["api"] and any(a.endswith("/comments") for a in call):
			bodies.append(next(a for a in call if a.startswith("body=")))
	return bodies


def test_blocked_verdict_moves_issue_to_ai_blocked_and_explains_resume() -> None:
	calls = _run_failure_step(
		blocked_reason="BLOCKED: Required production credentials and explicit mutation approvals are unavailable; the approved plan forbids repository changes.",
		labels=["ai:implementing", "ai:orchestrator-managed"],
	)
	edits = _label_edits(calls)
	assert len(edits) == 1
	edit = edits[0]
	assert "ai:blocked" in edit
	assert edit[edit.index("--add-label") + 1] == "ai:blocked"
	assert "ai:implementing" in edit and "ai:awaiting-approval" in edit
	assert not any("--add-label" in e and e[e.index("--add-label") + 1] == "ai:awaiting-approval" for e in edits)
	bodies = _comment_bodies(calls)
	assert len(bodies) == 1
	assert "Implementation blocked: human input required." in bodies[0]
	assert "Reason: BLOCKED: Required production credentials" in bodies[0]
	assert "stall recovery and re-issuance are paused" in bodies[0]
	assert "comment /answer to re-plan" in bodies[0]
	assert "AI implementation workflow failed" not in bodies[0]


def test_non_blocked_failure_keeps_awaiting_approval_path() -> None:
	calls = _run_failure_step(blocked_reason=None, labels=["ai:implementing"])
	edits = _label_edits(calls)
	assert len(edits) == 1
	assert edits[0][edits[0].index("--add-label") + 1] == "ai:awaiting-approval"
	bodies = _comment_bodies(calls)
	assert len(bodies) == 1
	assert "AI implementation workflow failed" in bodies[0]


def test_blocked_verdict_does_not_report_terminalization_when_latch_fails() -> None:
	calls = _run_failure_step(blocked_reason="BLOCKED: credentials unavailable", labels=["ai:implementing"], fail_blocked_edit=True)
	assert len(_label_edits(calls)) == 1
	assert _comment_bodies(calls) == []


def _runtime_after_failure_step(tmp_path: Path, **kwargs) -> Path:
	tmp_path.mkdir(parents=True, exist_ok=True)
	_run_failure_step(runtime_out=tmp_path, **kwargs)
	return tmp_path / "runtime"


def test_latch_flag_is_written_only_when_the_blocked_latch_is_set(tmp_path: Path) -> None:
	blocked = _runtime_after_failure_step(tmp_path / "a", blocked_reason="BLOCKED: npm registry unavailable", labels=["ai:implementing"])
	assert (blocked / "implement_blocked_latched.flag").is_file()
	failed = _runtime_after_failure_step(tmp_path / "b", blocked_reason="BLOCKED: x", labels=["ai:implementing"], fail_blocked_edit=True)
	assert not (failed / "implement_blocked_latched.flag").exists()
	plain = _runtime_after_failure_step(tmp_path / "c", blocked_reason=None, labels=["ai:implementing"])
	assert not (plain / "implement_blocked_latched.flag").exists()


FAKE_TG_HELPERS = '''tg_send_phase_failure() { printf '%s|%s|%s\\n' "$3" "$4" "$2" >> "$FAKE_TG_LOG"; }
'''


def _run_telegram_step(tmp_path: Path, *, blocked_reason: str | None, latched: bool) -> tuple[str, str, str]:
	script = _render_github_expressions(_extract_run_script("Telegram failure notification"))
	tmp_path.mkdir(parents=True, exist_ok=True)
	support = tmp_path / "support"
	support.mkdir()
	(support / "tg_helpers.sh").write_text(FAKE_TG_HELPERS, encoding="utf-8")
	runtime_dir = tmp_path / "runtime"
	runtime_dir.mkdir()
	if blocked_reason is not None:
		(runtime_dir / "codex_blocked.flag").write_text(blocked_reason + "\n", encoding="utf-8")
	if latched:
		(runtime_dir / "implement_blocked_latched.flag").write_text("", encoding="utf-8")
	log = tmp_path / "tg.log"
	env = os.environ.copy()
	for key in ("BASH_ENV", "ENV"):
		env.pop(key, None)
	env.update({
		"FAKE_TG_LOG": str(log),
		"ISSUE_NUMBER": "4770",
		"ISSUE_URL": "https://github.com/owner/repo/issues/4770",
		"RUNTIME_DIR": str(runtime_dir),
		"IMPLEMENT_STAGED_SUPPORT_RUN_DIR": str(support),
	})
	step = tmp_path / "step.sh"
	step.write_text(script, encoding="utf-8")
	proc = subprocess.run(["bash", str(step)], cwd=str(tmp_path), env=env, text=True, capture_output=True, timeout=60)
	assert proc.returncode == 0, proc.stderr + proc.stdout
	level, tracked, message = log.read_text(encoding="utf-8").split("|", 2)
	return level, tracked, message


def test_latched_blocked_verdict_pages_at_warning(tmp_path: Path) -> None:
	# tele-funtoken-msg-scoring#4770: sent at DEBUG, the alert was dropped
	# under the default ALERT_MSG_LEVEL=WARNING and nobody was told.
	level, tracked, message = _run_telegram_step(tmp_path, blocked_reason="BLOCKED: npm registry unavailable for npm ci", latched=True)
	assert level == "WARNING" and tracked == "4770"
	assert message.startswith("Implementation blocked on issue #4770: human input required")
	assert "Reason: BLOCKED: npm registry unavailable for npm ci" in message
	assert "Issue: https://github.com/owner/repo/issues/4770" in message
	assert "Run: https://github.com/owner/repo/actions/runs/777" in message


def test_unlatched_or_ordinary_failures_stay_at_debug(tmp_path: Path) -> None:
	level, _, message = _run_telegram_step(tmp_path / "a", blocked_reason="BLOCKED: x", latched=False)
	assert level == "DEBUG" and message.startswith("AI implementation workflow failed for issue #4770")
	level, _, message = _run_telegram_step(tmp_path / "b", blocked_reason=None, latched=True)
	assert level == "DEBUG" and message.startswith("AI implementation workflow failed for issue #4770")
