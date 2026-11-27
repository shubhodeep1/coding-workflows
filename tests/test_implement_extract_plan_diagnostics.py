#!/usr/bin/env python3
"""implement.yml "Extract implementation plan" says why no plan was accepted (#9724).

When no trusted plan comment precedes the triggering /approved, the step
still prints its message and exits 1, and now also prints one
`::error::IMPLEMENT_PLAN_NOT_FOUND reason=<token> ...` line plus one
`::warning::IMPLEMENT_PLAN_CANDIDATE ...` line per plan-like comment. Comment bodies are never
printed, and every comment-derived value is reduced to a safe charset.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

try:
	import yaml
except ModuleNotFoundError:
	# The credential-free pre-push gate image has no PyYAML; _step_run then
	# reads the step's `run: |` block from the workflow text instead.
	yaml = None

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "implement.yml"
STEP_NAME = "Extract implementation plan"
APPROVAL_ID = 7000
PLAN_MARKER = "<!-- ai:plan-proposal:v1 -->"
SENTINEL = "SENTINEL_PLAN_BODY_9724"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")


def _indent_of(line: str) -> int:
	return len(line) - len(line.lstrip(" "))


def _step_runs_from_text(text: str, step_name: str) -> list[str]:
	lines = text.splitlines()
	runs: list[str] = []
	for i, line in enumerate(lines):
		if line.strip() != f"- name: {step_name}":
			continue
		item_indent = _indent_of(line)
		key_indent = item_indent + 2
		j = i + 1
		while j < len(lines) and (not lines[j].strip() or _indent_of(lines[j]) > item_indent):
			if _indent_of(lines[j]) == key_indent and lines[j].strip().startswith("run: |"):
				body: list[str] = []
				k = j + 1
				while k < len(lines) and (not lines[k].strip() or _indent_of(lines[k]) > key_indent):
					body.append(lines[k])
					k += 1
				while body and not body[-1].strip():
					body.pop()
				runs.append(textwrap.dedent("\n".join(body)) + "\n")
				break
			j += 1
		else:
			runs.append("")
	return runs


def _step_run() -> str:
	text = WORKFLOW.read_text(encoding="utf-8")
	if yaml is None:
		runs = _step_runs_from_text(text, STEP_NAME)
	else:
		data = yaml.safe_load(text)
		runs = [
			step.get("run", "")
			for job in (data.get("jobs") or {}).values()
			for step in (job.get("steps") or [])
			if step.get("name") == STEP_NAME
		]
	assert len(runs) == 1, f"expected exactly one step named {STEP_NAME!r}, found {len(runs)}"
	assert "${{" not in runs[0]
	return runs[0]


def _comment(cid: int, ts: str, body: str, login: str = "shubhodeep1", utype: str = "User", assoc: str = "OWNER") -> dict:
	return {
		"id": cid,
		"created_at": ts,
		"body": body,
		"user": {"login": login, "type": utype},
		"author_association": assoc,
	}


def _approval(ts: str = "2026-11-18T11:40:00Z", body: str = "/approved [auto-approved-by-plan]") -> dict:
	return _comment(APPROVAL_ID, ts, body)


def _plan(cid: int, ts: str, **kwargs) -> dict:
	body = f"Implementation Plan\n\n{SENTINEL}\n\n{PLAN_MARKER}"
	return _comment(cid, ts, body, **kwargs)


def _run(comments: list[dict], tmp_path: Path) -> tuple[subprocess.CompletedProcess, Path]:
	comments_file = tmp_path / "comments.json"
	comments_file.write_text(json.dumps(comments), encoding="utf-8")
	plan_file = tmp_path / "plan.txt"
	env = dict(os.environ)
	env.update(
		{
			"APPROVAL_COMMENT_ID": str(APPROVAL_ID),
			"ISSUE_COMMENTS_FILE": str(comments_file),
			"PLAN_FILE": str(plan_file),
		}
	)
	result = subprocess.run(
		["bash", "-c", _step_run()],
		env=env,
		capture_output=True,
		text=True,
		timeout=60,
		check=False,
	)
	return result, plan_file


def _error_lines(out: str) -> list[str]:
	return [line for line in out.splitlines() if line.startswith("::error::IMPLEMENT_PLAN_NOT_FOUND ")]


def _candidate_lines(out: str) -> list[str]:
	return [line for line in out.splitlines() if line.startswith("::warning::IMPLEMENT_PLAN_CANDIDATE ")]


def _assert_failed(result: subprocess.CompletedProcess, reason: str) -> str:
	assert result.returncode == 1, result.stdout + result.stderr
	assert "Unable to locate approved Implementation Plan in issue comments." in result.stdout
	errors = _error_lines(result.stdout)
	assert len(errors) == 1, result.stdout
	assert f" reason={reason} " in errors[0]
	assert f" approval_id={APPROVAL_ID} " in errors[0]
	assert SENTINEL not in result.stdout + result.stderr
	return errors[0]


def test_trusted_plan_before_approval_is_accepted(tmp_path: Path) -> None:
	result, plan_file = _run([_plan(10, "2026-11-18T11:08:00Z"), _approval()], tmp_path)
	assert result.returncode == 0, result.stdout + result.stderr
	assert plan_file.read_text(encoding="utf-8").startswith("Implementation Plan")
	assert "IMPLEMENT_PLAN_NOT_FOUND" not in result.stdout


def test_issue_9718_replay_reports_no_plan_comment(tmp_path: Path) -> None:
	comments = [
		_comment(1, "2026-11-18T10:20:00Z", "The task appears clear."),
		_comment(2, "2026-11-18T10:20:01Z", "/answer [auto-answered-by-clarify]"),
		_comment(3, "2026-11-18T10:21:00Z", "### AI attempt journal\n<!-- AI_ATTEMPT_JOURNAL_V1 {} AI_ATTEMPT_JOURNAL_V1 -->"),
		_comment(4, "2026-11-18T10:27:00Z", "<!-- AI_STANDALONE_STALL_STATE_V1 {} AI_STANDALONE_STALL_STATE_V1 -->"),
		_comment(5, "2026-11-18T11:34:00Z", "<!-- AI_STANDALONE_STALL_STATE_V1 {} AI_STANDALONE_STALL_STATE_V1 -->"),
		_approval(),
	]
	result, _ = _run(comments, tmp_path)
	error = _assert_failed(result, "no_plan_comment")
	assert "approval_created_at=2026-11-18T11:40:00Z" in error
	assert "comments_total=6" in error
	assert error.endswith("plan_candidates=0")
	assert _candidate_lines(result.stdout) == []


def test_plan_after_approval(tmp_path: Path) -> None:
	result, _ = _run([_approval(), _plan(20, "2026-11-18T12:00:00Z")], tmp_path)
	_assert_failed(result, "plan_after_approval")
	candidates = _candidate_lines(result.stdout)
	assert len(candidates) == 1
	assert "id=20 " in candidates[0]
	assert "after_approval=true" in candidates[0]


def test_untrusted_plan_author(tmp_path: Path) -> None:
	result, _ = _run([_plan(30, "2026-11-18T11:00:00Z", login="stranger", assoc="NONE"), _approval()], tmp_path)
	_assert_failed(result, "plan_author_untrusted")
	candidates = _candidate_lines(result.stdout)
	assert len(candidates) == 1
	assert "login=stranger type=User association=NONE" in candidates[0]
	assert "trusted=false" in candidates[0]


def test_approval_not_found(tmp_path: Path) -> None:
	result, _ = _run([_plan(40, "2026-11-18T11:00:00Z"), _approval(body="looks fine")], tmp_path)
	error = _assert_failed(result, "approval_not_found")
	assert "approval_created_at=none" in error
	assert "after_approval=unknown" in _candidate_lines(result.stdout)[0]


def test_marker_only_comment_reports_edited_plan(tmp_path: Path) -> None:
	edited = _comment(50, "2026-11-18T11:08:00Z", f"Edited away.\n{SENTINEL}\n{PLAN_MARKER}")
	result, _ = _run([edited, _approval()], tmp_path)
	_assert_failed(result, "no_plan_comment")
	candidates = _candidate_lines(result.stdout)
	assert len(candidates) == 1
	assert "heading=false marker=true" in candidates[0]


def test_heading_split_across_lines_reports_body_mismatch(tmp_path: Path) -> None:
	# jq matches "implementation\s+plan" across a newline; the grep check does not.
	split = _comment(60, "2026-11-18T11:08:00Z", f"Implementation\nplan\n{SENTINEL}")
	result, _ = _run([split, _approval()], tmp_path)
	_assert_failed(result, "plan_body_mismatch")


def test_crafted_login_cannot_inject_workflow_commands(tmp_path: Path) -> None:
	evil = _plan(70, "2026-11-18T11:00:00Z", login="x\n::set-env name=EVIL::1%0A::add-mask::y", assoc="NONE")
	result, _ = _run([evil, _approval()], tmp_path)
	_assert_failed(result, "plan_author_untrusted")
	for line in result.stdout.splitlines():
		assert not line.startswith("::set-env")
		assert not line.startswith("::add-mask")
	candidates = _candidate_lines(result.stdout)
	assert len(candidates) == 1
	assert "%" not in candidates[0]


def test_every_candidate_gets_a_line(tmp_path: Path) -> None:
	# Issue #9724 asks for the metadata of every comment matching the plan heading.
	comments = [_plan(100 + i, f"2026-11-18T12:{i:02d}:00Z") for i in range(25)]
	comments.append(_approval())
	result, _ = _run(comments, tmp_path)
	error = _assert_failed(result, "plan_after_approval")
	assert "plan_candidates=25" in error
	candidates = _candidate_lines(result.stdout)
	assert len(candidates) == 25
	assert "id=100 " in candidates[0]
	assert "id=124 " in candidates[-1]
	# The error line comes after every candidate line, so the heal log
	# filter's tail-keeping byte cut keeps the reason token.
	lines = result.stdout.splitlines()
	assert lines.index(error) > lines.index(candidates[-1])


def test_malformed_comments_file_still_exits_one(tmp_path: Path) -> None:
	comments_file = tmp_path / "comments.json"
	comments_file.write_text("not json", encoding="utf-8")
	env = dict(os.environ)
	env.update(
		{
			"APPROVAL_COMMENT_ID": str(APPROVAL_ID),
			"ISSUE_COMMENTS_FILE": str(comments_file),
			"PLAN_FILE": str(tmp_path / "plan.txt"),
		}
	)
	result = subprocess.run(["bash", "-c", _step_run()], env=env, capture_output=True, text=True, timeout=60, check=False)
	assert result.returncode != 0


def test_selection_guard_is_unchanged() -> None:
	run = _step_run()
	assert 'test("(?im)^\\\\s*implementation\\\\s+plan\\\\b")' in run
	assert '(.created_at <= $approval.created_at)' in run
	assert '["github-actions[bot]", "codex-bot[bot]"] | index($bot_login)' in run
	assert '["OWNER", "MEMBER", "COLLABORATOR"] | index($assoc)' in run
	assert "grep -qiE '^[[:space:]]*implementation[[:space:]]+plan\\b' \"${PLAN_FILE}\"" in run
	assert run.rstrip().endswith("exit 1\nfi")
