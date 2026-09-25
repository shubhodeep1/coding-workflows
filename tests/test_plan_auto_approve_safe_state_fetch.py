#!/usr/bin/env python3
"""Contract tests for plan auto-approve issue-state fetch hardening."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

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

	assert 'source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh"' in block
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
				# The step sources gh_helpers.sh from the immutable support
				# bundle; point it at the fake gh_helpers.sh staged above.
				"SUPPORT_SCRIPTS_DIR": str(tmp_path / "scripts"),
			})
			result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, check=True)
			assert f"auto_approved={str(approved).lower()}" in output_path.read_text(encoding="utf-8"), (issue, result.stdout, result.stderr)
			assert post_log.exists() == approved
			if approved:
				assert "/approved [auto-approved-by-plan]" in post_log.read_text(encoding="utf-8")
			else:
				assert f"reason={expected_reason} outcome=defer" in result.stdout


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
