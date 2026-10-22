#!/usr/bin/env python3
"""A deliberate `BLOCKED:` implement verdict must park the issue in ai:blocked.

Before this, implement.yml's "Comment on issue failure" step returned every
failed run to ai:awaiting-approval, and the orchestrator poller's stall
recovery re-posted /approved on the same blocked plan every stall window
(tele-funtoken-msg-scoring#4395: four identical BLOCKED runs over ~15h).
The step's `run:` block is executed with a mocked `gh`, as in
test_implement_post_codex_recovery.py.

The same step reads the issue's labels live (BUG-002,
workflow-optimization-2026-09-23): the cached ISSUE_META_FILE snapshot is
taken before the ai:awaiting-approval -> ai:implementing swap, so it cannot
decide whether to restore ai:awaiting-approval, and a failed read must not
abort the step before the failure comment is posted.
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
    if os.environ.get("MOCK_FAIL_LABEL_READ") == "1":
        sys.stderr.write("HTTP 502: Bad Gateway\n")
        sys.exit(1)
    raw = os.environ.get("MOCK_ISSUE_LABELS_RAW")
    sys.stdout.write(raw if raw is not None else os.environ.get("MOCK_ISSUE_LABELS", "[]"))
    sys.exit(0)
if os.environ.get("MOCK_FAIL_BLOCKED_EDIT") == "1" and args[:2] == ["issue", "edit"] and "ai:blocked" in args:
    sys.stderr.write("label edit failed\n")
    sys.exit(1)
sys.exit(0)
'''


def _run_failure_step_full(
	*,
	blocked_reason: str | None,
	labels: list[str],
	fail_blocked_edit: bool = False,
	meta_labels: list[str] | None = None,
	fail_label_read: bool = False,
	labels_raw: str | None = None,
) -> tuple[list[list[str]], str]:
	script = _render_github_expressions(_extract_run_script("Comment on issue failure"))
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		(bin_dir / "gh").write_text(MOCK_GH, encoding="utf-8")
		(bin_dir / "gh").chmod(0o755)
		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir()
		if blocked_reason is not None:
			(runtime_dir / "codex_blocked.flag").write_text(blocked_reason + "\n", encoding="utf-8")
		issue_meta_file = ""
		if meta_labels is not None:
			meta_path = runtime_dir / "issue_meta.json"
			meta_path.write_text(
				json.dumps({"number": 4395, "labels": [{"name": name} for name in meta_labels]}),
				encoding="utf-8",
			)
			issue_meta_file = str(meta_path)
		log = tmp_path / "gh.log"
		log.write_text("", encoding="utf-8")
		env = os.environ.copy()
		for key in ("BASH_ENV", "ENV", "MOCK_ISSUE_LABELS_RAW"):
			env.pop(key, None)
		env.update(
			{
				"PATH": f"{bin_dir}:{env['PATH']}",
				"MOCK_GH_LOG": str(log),
				"MOCK_ISSUE_LABELS": json.dumps(labels),
				"MOCK_FAIL_BLOCKED_EDIT": "1" if fail_blocked_edit else "0",
				"MOCK_FAIL_LABEL_READ": "1" if fail_label_read else "0",
				"ISSUE_NUMBER": "4395",
				"ISSUE_URL": "https://github.com/owner/repo/issues/4395",
				"RUNTIME_DIR": str(runtime_dir),
				"IMPLEMENT_STAGED_SUPPORT_RUN_DIR": str(tmp_path / "missing-support-dir"),
				"ISSUE_META_FILE": issue_meta_file,
				"GH_TOKEN": "x",
				"PYTHONDONTWRITEBYTECODE": "1",
			}
		)
		if labels_raw is not None:
			env["MOCK_ISSUE_LABELS_RAW"] = labels_raw
		script_path = tmp_path / "step.sh"
		script_path.write_text(script, encoding="utf-8")
		proc = subprocess.run(["bash", str(script_path)], cwd=str(tmp_path), env=env, text=True, capture_output=True, timeout=60)
		if fail_blocked_edit:
			assert proc.returncode != 0
		else:
			assert proc.returncode == 0, proc.stderr + proc.stdout
		calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
		return calls, proc.stdout


def _run_failure_step(*, blocked_reason: str | None, labels: list[str], fail_blocked_edit: bool = False) -> list[list[str]]:
	calls, _ = _run_failure_step_full(blocked_reason=blocked_reason, labels=labels, fail_blocked_edit=fail_blocked_edit)
	return calls


def _label_edits(calls: list[list[str]]) -> list[list[str]]:
	return [c for c in calls if c[:2] == ["issue", "edit"]]


def _label_reads(calls: list[list[str]]) -> list[list[str]]:
	return [c for c in calls if c[:1] == ["api"] and "--jq" in c]


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


def test_failure_step_does_not_read_labels_from_issue_meta_file() -> None:
	script = _extract_run_script("Comment on issue failure")
	assert "ISSUE_META_FILE" not in script
	assert "ISSUE_LABELS_LIVE_READ" in script
	assert "jq -e 'type == \"array\"'" in script


def test_stale_cached_snapshot_still_restores_awaiting_approval() -> None:
	calls, _ = _run_failure_step_full(
		blocked_reason=None,
		labels=["ai:implementing"],
		meta_labels=["ai:awaiting-approval"],
	)
	assert len(_label_reads(calls)) == 1
	edits = _label_edits(calls)
	assert len(edits) == 1
	edit = edits[0]
	assert edit[edit.index("--add-label") + 1] == "ai:awaiting-approval"
	assert edit[edit.index("--remove-label") + 1] == "ai:implementing"
	bodies = _comment_bodies(calls)
	assert len(bodies) == 1
	assert "AI implementation workflow failed" in bodies[0]


def _assert_labels_unchanged_with_warning(calls: list[list[str]], stdout: str) -> None:
	assert _label_edits(calls) == []
	assert not any(c[:2] == ["label", "create"] and "ai:awaiting-approval" in c for c in calls)
	assert "::warning::Could not read the labels of issue #4395; labels were left unchanged" in stdout
	bodies = _comment_bodies(calls)
	assert len(bodies) == 1
	assert "AI implementation workflow failed" in bodies[0]


def test_failed_live_label_read_leaves_labels_and_still_comments() -> None:
	calls, stdout = _run_failure_step_full(
		blocked_reason=None,
		labels=["ai:implementing"],
		meta_labels=["ai:implementing"],
		fail_label_read=True,
	)
	_assert_labels_unchanged_with_warning(calls, stdout)


def test_non_array_label_read_is_treated_as_unavailable() -> None:
	calls, stdout = _run_failure_step_full(
		blocked_reason=None,
		labels=["ai:implementing"],
		labels_raw='{"labels":"ai:implementing"}',
	)
	_assert_labels_unchanged_with_warning(calls, stdout)


def test_blocked_verdict_skips_live_label_read() -> None:
	calls, stdout = _run_failure_step_full(
		blocked_reason="BLOCKED: credentials unavailable",
		labels=["ai:implementing"],
	)
	assert _label_reads(calls) == []
	edits = _label_edits(calls)
	assert len(edits) == 1
	assert edits[0][edits[0].index("--add-label") + 1] == "ai:blocked"
	assert "IMPLEMENT_BLOCKED_TERMINALIZED issue=4395 label=ai:blocked" in stdout
	assert "::warning::" not in stdout
