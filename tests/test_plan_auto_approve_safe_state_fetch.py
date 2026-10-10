#!/usr/bin/env python3
"""Contract tests for plan auto-approve issue-state fetch hardening."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
GUARD_SCRIPT = REPO_ROOT / "scripts" / "files_touched_scope_guard.py"
ORDINARY_PLAN = "Implementation Plan\n\n### 1. Files likely to change\n- `src/app.py`\n- `README.md`\n"
SECURITY_FINDING_PLAN = "Implementation Plan\n\n### 1. Files likely to change\n- `scripts/security_audit.sh`\n"
GRANTED_PLAN = (
	"Implementation Plan\n\n### 1. Files likely to change\n"
	"- `scripts/security_audit.sh`\n- `.github/workflows/security-audit.yml`\n- `README.md`\n"
)


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


def _auto_approve_script() -> str:
	workflow = yaml.safe_load(_workflow_text())
	step = next(step for step in workflow["jobs"]["plan"]["steps"] if step.get("name") == "Auto-approve clear plan")
	return step["run"].replace("${{ github.repository }}", "owner/repo")


def _run_auto_approve(issue, *, plan_text: str = ORDINARY_PLAN, fetch_fails: bool = False,
	gate_flag: str | None = None, login: str = "pipeline-bot") -> tuple[bool, str, str]:
	"""Run the shipped step against a fake gh; returns (posted, stdout, output file)."""
	with tempfile.TemporaryDirectory() as workdir:
		tmp_path = Path(workdir)
		(tmp_path / "scripts").mkdir()
		shutil.copy(GUARD_SCRIPT, tmp_path / "scripts" / "files_touched_scope_guard.py")
		(tmp_path / "scripts" / "gh_helpers.sh").write_text(
			'gh_retry() { "$@"; }\n'
			'_safe_gh_jq() { gh api "$@"; }\n'
			'gh() {\n'
			'  if [ "$2" = "repos/owner/repo/issues/123" ]; then\n'
			'    [ "${FETCH_FAIL}" = "false" ] || return 1\n'
			'    printf "%s" "${ISSUE_RESPONSE}"\n'
			'  elif [ "$2" = "user" ]; then\n'
			'    printf "%s\\n" "${PIPELINE_LOGIN}"\n'
			'  else\n'
			'    printf "%s\\n" "$*" >> "${POST_LOG}"\n'
			'  fi\n'
			'}\n'
			'sleep() { :; }\n',
			encoding="utf-8",
		)
		plan_file = tmp_path / "codex_output.txt"
		plan_file.write_text(plan_text, encoding="utf-8")
		output_path = tmp_path / "output"
		post_log = tmp_path / "posts"
		env = os.environ.copy()
		for name in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED",
			"ALLOW_WORKFLOW_EDITS", "PLAN_FILE", "GITHUB_STEP_SUMMARY"):
			env.pop(name, None)
		env.update({
			"GITHUB_OUTPUT": str(output_path),
			"ISSUE_NUMBER": "123",
			"ISSUE_RESPONSE": issue if isinstance(issue, str) else json.dumps(issue),
			"FETCH_FAIL": str(fetch_fails).lower(),
			"POST_LOG": str(post_log),
			"PIPELINE_LOGIN": login,
			"AUTO_IMPLEMENT_ON_CLEAR_PLAN": "true",
			"CODEX_OUTPUT_FILE": str(plan_file),
			"RUNNER_TEMP": str(tmp_path),
		})
		if gate_flag is not None:
			env["AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED"] = gate_flag
		result = subprocess.run(["bash", "-c", _auto_approve_script()], cwd=tmp_path, env=env, text=True,
			capture_output=True, check=True)
		posted = post_log.exists() and "/approved [auto-approved-by-plan]" in post_log.read_text(encoding="utf-8")
		assert post_log.exists() == posted, post_log.read_text(encoding="utf-8")
		return posted, result.stdout, output_path.read_text(encoding="utf-8")


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
	for issue, fetch_fails, expected_reason, approved in cases:
		posted, stdout, output = _run_auto_approve(issue, fetch_fails=fetch_fails)
		assert f"auto_approved={str(approved).lower()}" in output, (issue, stdout)
		assert posted == approved
		if not approved:
			assert f"reason={expected_reason} outcome=defer" in stdout


_PIPELINE_ISSUE = {"state": "open", "number": 123, "user": {"type": "User", "login": "pipeline-bot"},
	"author_association": "OWNER", "body": "Security finding. No files_touched block."}
_GRANTED_ISSUE = {"state": "open", "number": 123, "user": {"type": "User", "login": "maintainer"},
	"author_association": "OWNER",
	"body": "Fix it.\n\nfiles_touched:\n  - scripts/security_audit.sh\n  - .github/workflows/security-audit.yml\n"}


def test_ungranted_security_finding_plan_is_held() -> None:
	# Issue #6769 regression: a pipeline-authored security finding without
	# files_touched gets the open implement-time grant, but its plan must not
	# be auto-approved with the flag off (the default) or on.
	for flag, reason in ((None, "automation_path_plan_auto_approval_disabled"),
		("false", "automation_path_plan_auto_approval_disabled"), ("true", "open_grant_not_exact")):
		posted, stdout, output = _run_auto_approve(_PIPELINE_ISSUE, plan_text=SECURITY_FINDING_PLAN, gate_flag=flag)
		assert not posted, (flag, stdout)
		assert "auto_approved=false" in output
		assert f"auto_approve_hold_reason={reason}" in output
		assert (f"AI_PHASE_GATE_V1 phase=plan gate=auto_approve reason={reason} outcome=defer issue=123 "
			"paths=scripts/security_audit.sh") in stdout


def test_fully_granted_plan_is_approved_only_with_flag_on() -> None:
	posted, stdout, output = _run_auto_approve(_GRANTED_ISSUE, plan_text=GRANTED_PLAN, gate_flag="true")
	assert posted, stdout
	assert "auto_approved=true" in output
	posted, stdout, output = _run_auto_approve(_GRANTED_ISSUE, plan_text=GRANTED_PLAN, gate_flag="false")
	assert not posted
	assert "reason=automation_path_plan_auto_approval_disabled outcome=defer" in stdout


def test_partial_grant_reports_missing_paths() -> None:
	issue = dict(_GRANTED_ISSUE, body="files_touched:\n  - scripts/security_audit.sh\n")
	posted, stdout, _ = _run_auto_approve(issue, plan_text=GRANTED_PLAN, gate_flag="true")
	assert not posted
	assert "reason=path_not_granted outcome=defer issue=123 paths=.github/workflows/security-audit.yml" in stdout


def test_ordinary_plan_unchanged_and_missing_plan_holds() -> None:
	posted, _, _ = _run_auto_approve(_PIPELINE_ISSUE, plan_text=ORDINARY_PLAN)
	assert posted
	posted, stdout, _ = _run_auto_approve(_PIPELINE_ISSUE, plan_text="")
	assert not posted
	assert "reason=plan_gate_error outcome=defer" in stdout


def test_plan_gate_wiring_defaults_off() -> None:
	text = _workflow_text()
	assert "AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED: ${{ vars.AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED || 'false' }}" in text
	assert "clarify_isolated_run.sh files_touched_scope_guard.py; do" in text
	block = _step_block(text, "Auto-approve clear plan")
	assert "--plan-auto-approval-gate" in block
	assert 'plan_gate_reason="plan_gate_helper_unavailable"' in block
	assert "ALLOW_WORKFLOW_EDITS: ${{ vars.ALLOW_WORKFLOW_EDITS || 'true' }}" in block


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
