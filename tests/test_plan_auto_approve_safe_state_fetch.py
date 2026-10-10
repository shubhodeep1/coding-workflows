#!/usr/bin/env python3
"""Contract tests for plan auto-approve issue-state fetch hardening."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"


def _workflow_text() -> str:
	return PLAN_WF.read_text(encoding="utf-8")


def _step_block(text: str, step_name: str) -> str:
	marker = f"- name: {step_name}"
	start = text.find(marker)
	assert start != -1, f"Missing workflow step: {step_name}"
	next_step = text.find("\n      - name:", start + len(marker))
	if next_step == -1:
		return text[start:]
	return text[start:next_step]


def test_auto_approve_step_uses_safe_issue_state_fetch() -> None:
	block = _step_block(_workflow_text(), "Auto-approve clear plan")

	assert 'source scripts/gh_helpers.sh 2>/dev/null || true' in block
	assert 'type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }' in block
	assert 'type _safe_gh_jq >/dev/null 2>&1 || _safe_gh_jq() {' in block
	assert 'if ! _tmpf=$(mktemp "${TMPDIR:-/tmp}/_safe_gh_jq.XXXXXX" 2>/dev/null); then' in block
	assert 'echo "::error::_safe_gh_jq: failed to create temp file (mktemp failed); aborting without running: $*" >&2' in block
	assert 'if gh api "$@" > "${_tmpf}"; then' in block
	assert '_tmpf="$(mktemp "${TMPDIR:-/tmp}/_safe_gh_jq.XXXXXX" 2>/dev/null)" || return 1' not in block
	assert 'if gh api "$@" > "${_tmpf}" 2>/dev/null; then' not in block
	assert 'CURRENT_ISSUE_JSON="$(gh_retry _safe_gh_jq "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" 2>/dev/null)"' in block
	assert 'CURRENT_STATE="$(printf \'%s\' "${CURRENT_ISSUE_JSON}" | jq -er' in block
	assert '.user.type == "User"' in block
	assert '.author_association | IN("OWNER", "MEMBER", "COLLABORATOR")' in block
	assert '.user.type == "Bot" and .user.login == "github-actions[bot]"' in block
	assert '|| echo "open"' not in block
	assert (
		'CURRENT_STATE="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" '
		'--jq \'.state // "open"\' 2>/dev/null || echo "open")"'
	) not in block


def test_auto_approve_step_preserves_outputs_and_comment_payload() -> None:
	block = _step_block(_workflow_text(), "Auto-approve clear plan")

	assert 'echo "auto_approved=false" >> "$GITHUB_OUTPUT"' in block
	assert 'if [ "${CURRENT_STATE}" = "closed" ]; then' in block
	assert 'echo "auto_approved=true" >> "$GITHUB_OUTPUT"' in block
	assert 'gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" \\' in block
	assert "-f body=$'/approved [auto-approved-by-plan]\\n\\nAuto approval was posted because AUTO_IMPLEMENT_ON_CLEAR_PLAN is enabled.'" in block
	assert 'echo "AUTO_IMPLEMENT_ON_CLEAR_PLAN disabled; skipping auto-approval comment."' in block


def test_auto_approve_checks_live_author_before_posting() -> None:
	cases = [
		({"state": "open", "user": {"type": "User", "login": "maintainer"}, "author_association": "OWNER"}, False, None, True),
		({"state": "open", "user": {"type": "User"}, "author_association": "MEMBER"}, False, None, True),
		({"state": "open", "user": {"type": "User"}, "author_association": "COLLABORATOR"}, False, None, True),
		({"state": "open", "user": {"type": "Bot", "login": "github-actions[bot]"}, "author_association": "NONE"}, False, None, True),
		({"state": "closed", "user": {"type": "User"}, "author_association": "OWNER"}, False, "issue_closed", False),
		({"state": "open", "user": {"type": "User"}, "author_association": "NONE"}, False, "untrusted_issue_author", False),
		({"state": "open", "user": {"type": "Bot", "login": "other[bot]"}}, False, "untrusted_issue_author", False),
		({"state": "open", "user": {"type": "User", "login": "github-actions[bot]"}}, False, "untrusted_issue_author", False),
		({"state": "open", "user": {"type": "Bot", "login": "github-actions[bot]"}}, False, None, True),
		({"state": "open", "user": {}}, False, "untrusted_issue_author", False),
		({"user": {"type": "User"}, "author_association": "OWNER"}, False, "issue_state_unknown", False),
		("invalid-json", False, "issue_state_unknown", False),
		(None, True, "issue_lookup_failed", False),
	]
	workflow = yaml.safe_load(_workflow_text())
	step = next(step for step in workflow["jobs"]["plan"]["steps"] if step.get("name") == "Auto-approve clear plan")
	script = step["run"].replace("${{ github.repository }}", "owner/repo")
	for issue, fetch_fails, expected_reason, approved in cases:
		with tempfile.TemporaryDirectory() as workdir:
			tmp_path = Path(workdir)
			(tmp_path / "scripts").mkdir()
			(tmp_path / "scripts" / "gh_helpers.sh").write_text(
				'gh_retry() { "$@"; }\n'
				'_safe_gh_jq() { gh api "$@"; }\n'
				'gh() {\n'
				'  if [ "$2" = "repos/owner/repo/issues/123" ]; then\n'
				'    [ "${FETCH_FAIL}" = "false" ] || return 1\n'
				'    printf "%s" "${ISSUE_RESPONSE}"\n'
				'  else\n'
				'    printf "%s\\n" "$*" >> "${POST_LOG}"\n'
				'  fi\n'
				'}\n'
				'sleep() { :; }\n',
				encoding="utf-8",
			)
			output_path = tmp_path / "output"
			post_log = tmp_path / "posts"
			env = os.environ.copy()
			for name in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
				env.pop(name, None)
			env.update({
				"GITHUB_OUTPUT": str(output_path),
				"ISSUE_NUMBER": "123",
				"ISSUE_RESPONSE": issue if isinstance(issue, str) else json.dumps(issue),
				"FETCH_FAIL": str(fetch_fails).lower(),
				"POST_LOG": str(post_log),
				"AUTO_IMPLEMENT_ON_CLEAR_PLAN": "true",
			})
			result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, check=True)
			assert f"auto_approved={str(approved).lower()}" in output_path.read_text(encoding="utf-8"), (issue, result.stdout, result.stderr)
			assert post_log.exists() == approved
			if approved:
				assert "/approved [auto-approved-by-plan]" in post_log.read_text(encoding="utf-8")
			else:
				assert f"reason={expected_reason} outcome=defer" in result.stdout


POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
GUARD_SCRIPT = REPO_ROOT / "scripts" / "files_touched_scope_guard.py"
EXTRACTOR_BEGIN = "# BEGIN automation-path-plan-extractor v1"
EXTRACTOR_END = "# END automation-path-plan-extractor v1"


def _extractor_block(text: str) -> str:
	start = text.index(EXTRACTOR_BEGIN)
	end = text.index(EXTRACTOR_END, start) + len(EXTRACTOR_END)
	# Include the indentation before the BEGIN marker so dedent sees it.
	line_start = text.rfind("\n", 0, start) + 1
	return textwrap.dedent(text[line_start:end])


def _auto_approve_run_script() -> str:
	"""Return the step's run body without needing a YAML parser."""
	lines = _workflow_text().splitlines(keepends=True)
	start = next(i for i, line in enumerate(lines) if line.startswith("      - name: Auto-approve clear plan"))
	run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
	body = []
	for line in lines[run + 1:]:
		if line.startswith("      - name:"):
			break
		body.append(line[10:] if line.strip() else "\n")
	return "".join(body).replace("${{ github.repository }}", "owner/repo")


GATE_PLAN = """Implementation Plan

1. Files likely to change
- `.github/workflows/plan.yml` (granted). Gate at `scripts/orchestrate_poll_process.sh:14709`.
- `tests/test_plan_auto_approve_safe_state_fetch.py`
- Not changed: `scripts/files_touched_scope_guard.py`, `.github/workflows/ci.yml`.

2. Functions / modules
- mentions `scripts/elsewhere.sh` outside the Files section
"""

GATE_PLAN_NEGATIVE_ONLY = """Implementation Plan

1. Files likely to change
- `tests/test_x.py`
No changes are expected to:
- `scripts/foo.sh`

2. Functions / modules
"""

GATE_PLAN_NO_AUTOMATION = """Implementation Plan

Files likely to change:
- `tests/test_x.py`
- `README.md`
"""

_GATE_GH_STUB = (
	'gh_retry() { "$@"; }\n'
	'_safe_gh_jq() { gh api "$@"; }\n'
	'gh() {\n'
	'  if [ "$2" = "repos/owner/repo/issues/123" ]; then\n'
	'    printf "%s" "${ISSUE_RESPONSE}"\n'
	'  elif [ "$2" = "user" ]; then\n'
	'    printf "user\\n" >> "${CALL_LOG}"\n'
	'    printf "pipeline-bot\\n"\n'
	'  elif [ "$2" = "--paginate" ]; then\n'
	'    printf "comments\\n" >> "${CALL_LOG}"\n'
	'    printf "%s" "${COMMENTS_RESPONSE}"\n'
	'  else\n'
	'    printf "%s\\n<<END>>\\n" "$*" >> "${POST_LOG}"\n'
	'  fi\n'
	'}\n'
	'sleep() { :; }\n'
)


def _issue_with_grant(paths: list[str], association: str = "OWNER") -> dict:
	body = "Do the thing.\n\nfiles_touched:\n" + "".join(f"  - {path}\n" for path in paths)
	return {
		"number": 123, "state": "open", "body": body,
		"user": {"type": "User", "login": "maintainer"}, "author_association": association,
	}


def _run_gate_case(
	*, plan: str | None, issue: dict, flag: str | None, guard: bool = True, comments: list | None = None,
) -> dict:
	script = _auto_approve_run_script()
	with tempfile.TemporaryDirectory() as workdir:
		tmp_path = Path(workdir)
		(tmp_path / "scripts").mkdir()
		(tmp_path / "scripts" / "gh_helpers.sh").write_text(_GATE_GH_STUB, encoding="utf-8")
		if guard:
			support = tmp_path / ".codex-workflow-src" / "scripts"
			support.mkdir(parents=True)
			shutil.copy2(GUARD_SCRIPT, support / GUARD_SCRIPT.name)
		plan_file = tmp_path / "codex_output.txt"
		if plan is not None:
			plan_file.write_text(plan, encoding="utf-8")
		output_path = tmp_path / "output"
		post_log = tmp_path / "posts"
		call_log = tmp_path / "calls"
		env = os.environ.copy()
		for name in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED", "ALLOW_WORKFLOW_EDITS"):
			env.pop(name, None)
		env.update({
			"GITHUB_OUTPUT": str(output_path),
			"ISSUE_NUMBER": "123",
			"ISSUE_RESPONSE": json.dumps(issue),
			"COMMENTS_RESPONSE": json.dumps(comments or []),
			"POST_LOG": str(post_log),
			"CALL_LOG": str(call_log),
			"CODEX_OUTPUT_FILE": str(plan_file),
			"AUTO_IMPLEMENT_ON_CLEAR_PLAN": "true",
			"RUNNER_TEMP": str(tmp_path),
		})
		if flag is not None:
			env["AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED"] = flag
		result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, check=True)
		posts = post_log.read_text(encoding="utf-8") if post_log.exists() else ""
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"output": output_path.read_text(encoding="utf-8"),
			"posts": [entry for entry in posts.split("\n<<END>>\n") if entry.strip()],
			"calls": call_log.read_text(encoding="utf-8").split() if call_log.exists() else [],
		}


def _approved(result: dict) -> bool:
	return "auto_approved=true" in result["output"] and any("/approved [auto-approved-by-plan]" in post for post in result["posts"])


def test_automation_path_gate_is_off_by_default() -> None:
	issue = _issue_with_grant([])
	for flag in (None, "false", ""):
		result = _run_gate_case(plan=GATE_PLAN, issue=issue, flag=flag)
		assert _approved(result), result
		assert result["calls"] == []
		assert "automation_path_hold" not in result["output"]
	block = _step_block(_workflow_text(), "Auto-approve clear plan")
	assert "ALLOW_WORKFLOW_EDITS: ${{ vars.ALLOW_WORKFLOW_EDITS || 'true' }}" in block
	assert "AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED: ${{ vars.AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED || 'false' }}" in _workflow_text()


def test_automation_path_gate_approves_fully_granted_plan() -> None:
	issue = _issue_with_grant([".github/workflows/plan.yml", "scripts/orchestrate_poll_process.sh"])
	result = _run_gate_case(plan=GATE_PLAN, issue=issue, flag="true")
	assert _approved(result), result
	assert "user" in result["calls"]


def test_automation_path_gate_withholds_ungranted_plan() -> None:
	issue = _issue_with_grant([".github/workflows/plan.yml"])
	result = _run_gate_case(plan=GATE_PLAN, issue=issue, flag="true")
	assert not _approved(result)
	assert "auto_approved=false" in result["output"]
	assert "automation_path_hold=true" in result["output"]
	assert "reason=automation_path_ungranted outcome=defer issue=123 paths=1 detail=path_not_granted" in result["stdout"]
	assert len(result["posts"]) == 1
	post = result["posts"][0]
	assert "<!-- ai:automation-path-plan-hold:v1 issue=123 reason=automation_path_ungranted source=plan paths=" in post
	assert "`scripts/orchestrate_poll_process.sh`" in post
	assert "scripts/files_touched_scope_guard.py" not in post
	assert "scripts/elsewhere.sh" not in post
	assert "/approved [auto-approved-by-plan]" not in post


def test_automation_path_gate_skips_plans_without_automation_paths() -> None:
	issue = _issue_with_grant([])
	for plan in (GATE_PLAN_NO_AUTOMATION, GATE_PLAN_NEGATIVE_ONLY):
		result = _run_gate_case(plan=plan, issue=issue, flag="true")
		assert _approved(result), result
		assert result["calls"] == []


def test_automation_path_gate_fails_closed_when_check_unavailable() -> None:
	issue = _issue_with_grant([".github/workflows/plan.yml", "scripts/orchestrate_poll_process.sh"])
	for kwargs in (
		{"plan": GATE_PLAN, "guard": False},
		{"plan": "Implementation Plan\n\nNo file list here.\n"},
		{"plan": None},
	):
		result = _run_gate_case(issue=issue, flag="true", **kwargs)
		assert not _approved(result), (kwargs, result)
		assert "reason=automation_path_check_unavailable outcome=defer" in result["stdout"]
		assert len(result["posts"]) == 1
		assert "reason=automation_path_check_unavailable source=plan" in result["posts"][0]


def test_automation_path_gate_deduplicates_hold_comment() -> None:
	issue = _issue_with_grant([".github/workflows/plan.yml"])
	first = _run_gate_case(plan=GATE_PLAN, issue=issue, flag="true")
	marker = first["posts"][0].split("-f body=", 1)[1]
	comments = [
		{"id": 1, "created_at": "2026-10-09T00:00:00Z", "body": "Implementation Plan\n...", "author_association": "OWNER", "user": {"login": "maintainer"}},
		{"id": 2, "created_at": "2026-10-09T00:01:00Z", "body": marker, "author_association": "OWNER", "user": {"login": "maintainer"}},
	]
	again = _run_gate_case(plan=GATE_PLAN, issue=issue, flag="true", comments=comments)
	assert not _approved(again)
	assert again["posts"] == []
	untrusted = [comments[0], {**comments[1], "author_association": "NONE", "user": {"login": "stranger"}}]
	third = _run_gate_case(plan=GATE_PLAN, issue=issue, flag="true", comments=untrusted)
	assert len(third["posts"]) == 1


def test_automation_path_plan_extractor_is_identical_in_plan_and_poller() -> None:
	plan_block = _extractor_block(_workflow_text())
	poller_block = _extractor_block(POLLER_SCRIPT.read_text(encoding="utf-8"))
	assert plan_block == poller_block
	cases = {
		"# Files to change\n- `.github/x.yml`\n## Testing\n- `scripts/t.sh`\n": [".github/x.yml"],
		"1. Files likely to change\n- ./scripts/a.sh:12, `prompts/b.txt`.\n- `workflow-templates/*.yml`\n2. Functions\n": [
			"prompts/b.txt", "scripts/a.sh", "workflow-templates/*.yml",
		],
		"**Files changed**\nNothing else is modified:\n- `scripts/no.sh`\nDone with `scripts/yes.sh`\n": ["scripts/yes.sh"],
		"Files likely to change:\n- `tests/a.py`\n- a `scripts/` helper\n": ["scripts/"],
		"1. Files likely to change\n1. `tests/a.py`\n2. `scripts/b.sh`\n2. Functions\n- `scripts/c.sh`\n": ["scripts/b.sh"],
		"Files likely to change:\n- `tests/a.py`\n7. `Implementation-time estimate: 30 minutes`\n- `scripts/d.sh`\n": [],
	}
	with tempfile.TemporaryDirectory() as workdir:
		tmp_path = Path(workdir)
		plan_file = tmp_path / "plan.md"
		out_file = tmp_path / "out.txt"
		for plan, expected in cases.items():
			plan_file.write_text(plan, encoding="utf-8")
			out_file.unlink(missing_ok=True)
			result = subprocess.run(["python3", "-I", "-B", "-", str(plan_file), str(out_file)], input=plan_block, text=True, capture_output=True)
			assert result.returncode == 0, (plan, result.stderr)
			assert out_file.read_text(encoding="utf-8").split() == expected, plan
		plan_file.write_text("No files section at all mentions scripts/x.sh\n", encoding="utf-8")
		result = subprocess.run(["python3", "-I", "-B", "-", str(plan_file), str(out_file)], input=plan_block, text=True, capture_output=True)
		assert result.returncode == 3


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
