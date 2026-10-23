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
HEAL_SCRIPT = REPO_ROOT / "scripts" / "workflow_failure_heal.py"
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
	assert 'CURRENT_ISSUE_JSON="$(gh_retry _safe_gh_jq "repos/${PLAN_STEP_REPO}/issues/${ISSUE_NUMBER}" 2>/dev/null)"' in block
	assert 'CURRENT_STATE="$(printf \'%s\' "${CURRENT_ISSUE_JSON}" | jq -er' in block
	assert '.user.type == "User"' in block
	assert '.author_association | IN("OWNER", "MEMBER", "COLLABORATOR")' in block
	assert '.user.type == "Bot" and .user.login == "github-actions[bot]"' in block
	assert '|| echo "open"' not in block
	assert (
		'CURRENT_STATE="$(gh_retry gh api "repos/${PLAN_STEP_REPO}/issues/${ISSUE_NUMBER}" '
		'--jq \'.state // "open"\' 2>/dev/null || echo "open")"'
	) not in block


def test_auto_approve_step_preserves_outputs_and_comment_payload() -> None:
	block = _step_block(_workflow_text(), "Auto-approve clear plan")

	assert 'echo "auto_approved=false" >> "$GITHUB_OUTPUT"' in block
	assert 'if [ "${CURRENT_STATE}" = "closed" ]; then' in block
	assert 'echo "auto_approved=true" >> "$GITHUB_OUTPUT"' in block
	assert 'gh_retry gh api "repos/${PLAN_STEP_REPO}/issues/${ISSUE_NUMBER}/comments" \\' in block
	assert "-f body=$'/approved [auto-approved-by-plan]\\n\\nAuto approval was posted because AUTO_IMPLEMENT_ON_CLEAR_PLAN is enabled.'" in block
	assert 'echo "AUTO_IMPLEMENT_ON_CLEAR_PLAN disabled; skipping auto-approval comment."' in block


def _auto_approve_script() -> str:
	workflow = yaml.safe_load(_workflow_text())
	step = next(step for step in workflow["jobs"]["plan"]["steps"] if step.get("name") == "Auto-approve clear plan")
	# The repository reaches the body through the step's PLAN_STEP_REPO env
	# (no expression in the run: body); _run_auto_approve sets it.
	assert "${{" not in step["run"]
	return step["run"]


def _run_auto_approve(issue, *, plan_text: str = ORDINARY_PLAN, fetch_fails: bool = False,
	gate_flag: str | None = None, login: str = "pipeline-bot", comments: list | None = None,
	posts_out: list | None = None, patches_out: list | None = None, patch_fails: bool = False,
	graphql: dict | None = None, graphql_fails: bool = False, heal_fix_flag: str | None = None,
	graphql_calls_out: list | None = None, split_stub_rc: int | None = None,
	split_env_out: dict | None = None, extra_env: dict | None = None) -> tuple[bool, str, str]:
	"""Run the shipped step against a fake gh; returns (posted, stdout, output file).

	Every other API write (the hold comment) is appended to ``posts_out``;
	issue-body PATCH calls (the Q108 pipeline plan grant) to ``patches_out``.
	With ``split_stub_rc``, a stub scripts/plan_split_dispatch.sh exits with
	that code and its environment lands in ``split_env_out``.
	"""
	with tempfile.TemporaryDirectory() as workdir:
		tmp_path = Path(workdir)
		(tmp_path / "scripts").mkdir()
		shutil.copy(GUARD_SCRIPT, tmp_path / "scripts" / "files_touched_scope_guard.py")
		shutil.copy(HEAL_SCRIPT, tmp_path / "scripts" / "workflow_failure_heal.py")
		(tmp_path / "scripts" / "gh_helpers.sh").write_text(
			'gh_retry() { "$@"; }\n'
			'_safe_gh_jq() { gh api "$@"; }\n'
			'gh() {\n'
			'  if [ "$2" = "repos/owner/repo/issues/123" ]; then\n'
			'    [ "${FETCH_FAIL}" = "false" ] || return 1\n'
			'    printf "%s" "${ISSUE_RESPONSE}"\n'
			'  elif [ "$2" = "user" ]; then\n'
			'    printf "%s\\n" "${PIPELINE_LOGIN}"\n'
			'  elif [ "$2" = "graphql" ]; then\n'
			'    [ "${GRAPHQL_FAIL}" = "false" ] || return 1\n'
			'    printf "%s\\n" "$*" >> "${GRAPHQL_LOG}"\n'
			'    printf "%s" "${GRAPHQL_RESPONSE}"\n'
			'  elif [ "$2" = "-X" ] && [ "$3" = "PATCH" ]; then\n'
			'    [ "${PATCH_FAIL}" = "false" ] || return 1\n'
			'    printf "%s\\n<<END>>\\n" "$*" >> "${PATCH_LOG}"\n'
			'  elif [ "$2" = "--paginate" ]; then\n'
			'    printf "%s" "${COMMENTS_RESPONSE}"\n'
			'  else\n'
			'    printf "%s\\n<<END>>\\n" "$*" >> "${POST_LOG}"\n'
			'  fi\n'
			'}\n'
			'sleep() { :; }\n',
			encoding="utf-8",
		)
		split_env_file = tmp_path / "split_env.json"
		if split_stub_rc is not None:
			(tmp_path / "scripts" / "plan_split_dispatch.sh").write_text(
				"python3 -c 'import json, os, sys; json.dump({k: os.environ.get(k, \"\") for k in "
				"(\"REPO\", \"ISSUE_NUMBER\", \"PLAN_PATH_COUNT\", \"PLAN_SPLIT_TO_PROJECT_ENABLED\")} "
				"| {\"plan\": open(os.environ[\"PLAN_FILE\"]).read(), \"issue\": json.load(open(os.environ[\"ISSUE_JSON_FILE\"]))}, "
				f"open(sys.argv[1], \"w\"))' {split_env_file}\nexit {split_stub_rc}\n",
				encoding="utf-8",
			)
		plan_file = tmp_path / "codex_output.txt"
		plan_file.write_text(plan_text, encoding="utf-8")
		output_path = tmp_path / "output"
		post_log = tmp_path / "posts"
		patch_log = tmp_path / "patches"
		graphql_log = tmp_path / "graphql"
		env = os.environ.copy()
		for name in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED",
			"ALLOW_WORKFLOW_EDITS", "PLAN_FILE", "GITHUB_STEP_SUMMARY", "HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED",
			"ENFORCE_FILES_TOUCHED", "ALLOW_OUT_OF_SCOPE_FILES", "PLAN_FILES_TOUCHED_GATE_ENABLED"):
			env.pop(name, None)
		env.update({
			"GITHUB_OUTPUT": str(output_path),
			"PLAN_STEP_REPO": "owner/repo",
			"ISSUE_NUMBER": "123",
			"ISSUE_RESPONSE": issue if isinstance(issue, str) else json.dumps(issue),
			"FETCH_FAIL": str(fetch_fails).lower(),
			"POST_LOG": str(post_log),
			"PATCH_LOG": str(patch_log),
			"PATCH_FAIL": str(patch_fails).lower(),
			"GRAPHQL_LOG": str(graphql_log),
			"GRAPHQL_FAIL": str(graphql_fails).lower(),
			"GRAPHQL_RESPONSE": json.dumps(graphql or {}),
			"COMMENTS_RESPONSE": json.dumps(comments or []),
			"PIPELINE_LOGIN": login,
			"AUTO_IMPLEMENT_ON_CLEAR_PLAN": "true",
			"CODEX_OUTPUT_FILE": str(plan_file),
			"RUNNER_TEMP": str(tmp_path),
		})
		if gate_flag is not None:
			env["AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED"] = gate_flag
		if heal_fix_flag is not None:
			env["HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED"] = heal_fix_flag
		if extra_env:
			env.update(extra_env)
		result = subprocess.run(["bash", "-c", _auto_approve_script()], cwd=tmp_path, env=env, text=True,
			capture_output=True, check=True)
		posts = [entry for entry in (post_log.read_text(encoding="utf-8") if post_log.exists() else "").split("\n<<END>>\n")
			if entry.strip()]
		approvals = [entry for entry in posts if "/approved [auto-approved-by-plan]" in entry]
		posted = bool(approvals)
		others = [entry for entry in posts if entry not in approvals]
		# The only other write the step may make is the hold comment, and
		# never alongside an approval.
		assert all("<!-- ai:automation-path-plan-hold:v1 " in entry for entry in others), others
		assert not (posted and others), posts
		patches = [entry for entry in (patch_log.read_text(encoding="utf-8") if patch_log.exists() else "").split("\n<<END>>\n")
			if entry.strip()]
		# A body write only ever precedes an approval (or a failed one, which holds).
		assert not patches or posted or patch_fails, (patches, posts)
		if posts_out is not None:
			posts_out.extend(others)
		if patches_out is not None:
			patches_out.extend(patches)
		if graphql_calls_out is not None and graphql_log.exists():
			graphql_calls_out.extend(graphql_log.read_text(encoding="utf-8").splitlines())
		if split_env_out is not None and split_env_file.exists():
			split_env_out.update(json.loads(split_env_file.read_text(encoding="utf-8")))
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
	"body": ("Fix it.\n\nfiles_touched:\n  - scripts/security_audit.sh\n  - .github/workflows/security-audit.yml\n"
		"  - README.md\n")}


def test_ungranted_security_finding_plan_is_held_with_flag_off() -> None:
	# Issue #6769: with the flag off, a pipeline-authored security finding
	# without files_touched is never auto-approved and nothing is written.
	patches: list[str] = []
	posted, stdout, output = _run_auto_approve(_PIPELINE_ISSUE, plan_text=SECURITY_FINDING_PLAN, gate_flag="false",
		patches_out=patches)
	assert not posted, stdout
	assert patches == []
	assert "auto_approve_hold_reason=automation_path_plan_auto_approval_disabled" in output
	assert ("AI_PHASE_GATE_V1 phase=plan gate=auto_approve reason=automation_path_plan_auto_approval_disabled "
		"outcome=defer issue=123 paths=scripts/security_audit.sh") in stdout


def test_pipeline_plan_grant_is_written_before_approval() -> None:
	# Q108: the open pipeline-author grant never approves on its own. With the
	# flag on (unset and empty included) the step writes an exact
	# files_touched block from the plan into the issue body, then approves.
	for flag in (None, "", "true"):
		patches: list[str] = []
		posted, stdout, output = _run_auto_approve(_PIPELINE_ISSUE, plan_text=SECURITY_FINDING_PLAN, gate_flag=flag,
			patches_out=patches)
		assert posted, (flag, stdout)
		assert "auto_approved=true" in output
		assert "PIPELINE_PLAN_GRANT issue=123 source=plan outcome=written" in stdout
		assert len(patches) == 1
		assert patches[0].startswith("api -X PATCH repos/owner/repo/issues/123 -f body=Security finding. No files_touched block.\n\n---\n")
		assert patches[0].endswith("<!-- ai:pipeline-plan-grant:v1 -->\nfiles_touched:\n  - scripts/security_audit.sh\n"
			"  - tests/**\n  - changelog.d/*\n  - agents.d/*")


def test_pipeline_plan_grant_write_failure_holds() -> None:
	posts: list[str] = []
	posted, stdout, output = _run_auto_approve(_PIPELINE_ISSUE, plan_text=SECURITY_FINDING_PLAN, gate_flag="true",
		patch_fails=True, posts_out=posts)
	assert not posted, stdout
	assert "PIPELINE_PLAN_GRANT issue=123 source=plan outcome=failed reason=issue_body_write_failed" in stdout
	assert "auto_approve_hold_reason=pipeline_plan_grant_write_failed" in output
	assert len(posts) == 1 and "could not run" in posts[0]


def test_pipeline_plan_grant_never_written_for_a_member_issue() -> None:
	# A maintainer-written issue without files_touched has no grant at all;
	# the pipeline never writes one for it.
	issue = dict(_GRANTED_ISSUE, body="Fix it. No allowlist.")
	patches: list[str] = []
	posted, stdout, _ = _run_auto_approve(issue, plan_text=SECURITY_FINDING_PLAN, gate_flag="true", patches_out=patches)
	assert not posted
	assert patches == []
	assert "reason=no_allowlist outcome=defer" in stdout


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


# --- Plan files outside files_touched (consumer #4795) ----------------------
# A plan that names a file outside the issue's files_touched list is held,
# because the implement-time files_touched guard would refuse the commit.

_SCOPED_ISSUE = {"state": "open", "number": 123, "user": {"type": "User", "login": "maintainer"},
	"author_association": "OWNER",
	"body": "Fix the poller.\n\nfiles_touched:\n  - backend/poll_telegram_mongo.py\n  - tests/test_poll_telegram_mongo.py\n"}
WIDENED_PLAN = (
	"Implementation Plan\n\n## Files likely to change\n- `backend/withdrawal_flow.py`\n"
	"- `backend/poll_telegram_mongo.py`\n- `tests/test_poll_telegram_mongo.py`\n- `package-lock.json`\n"
)


def test_plan_outside_files_touched_is_held_with_a_comment() -> None:
	posts: list[str] = []
	patches: list[str] = []
	posted, stdout, output = _run_auto_approve(_SCOPED_ISSUE, plan_text=WIDENED_PLAN, posts_out=posts,
		patches_out=patches)
	assert not posted, stdout
	assert patches == []
	assert "auto_approve_hold_reason=plan_files_outside_files_touched" in output
	# Only the out-of-scope file is named; the lockfile is allowed as at implement.
	assert ("AI_PHASE_GATE_V1 phase=plan gate=auto_approve reason=plan_files_outside_files_touched "
		"outcome=defer issue=123 paths=backend/withdrawal_flow.py") in stdout
	assert len(posts) == 1
	hold = posts[0]
	assert "the plan changes files outside the `files_touched` list of this issue" in hold
	assert "- `backend/withdrawal_flow.py`" in hold
	assert "`/approved` alone does not help" in hold
	assert "reason=plan_files_outside_files_touched source=plan paths=" in hold


def test_plan_inside_files_touched_is_approved() -> None:
	issue = dict(_SCOPED_ISSUE, body=_SCOPED_ISSUE["body"] + "  - backend/withdrawal_flow.py\n")
	posted, stdout, _ = _run_auto_approve(issue, plan_text=WIDENED_PLAN)
	assert posted, stdout


def test_plan_files_touched_check_follows_the_implement_switches() -> None:
	# Off whenever implement would not block: enforcement off, out-of-scope
	# files allowed, or the plan-time check switched off on its own.
	for extra in ({"ENFORCE_FILES_TOUCHED": "false"}, {"ALLOW_OUT_OF_SCOPE_FILES": "true"},
		{"PLAN_FILES_TOUCHED_GATE_ENABLED": "false"}):
		posted, stdout, _ = _run_auto_approve(_SCOPED_ISSUE, plan_text=WIDENED_PLAN, extra_env=extra)
		assert posted, (extra, stdout)
	posted, _, _ = _run_auto_approve(_SCOPED_ISSUE, plan_text=WIDENED_PLAN,
		extra_env={"ENFORCE_FILES_TOUCHED": "true", "ALLOW_OUT_OF_SCOPE_FILES": "false"})
	assert not posted


def test_plan_files_touched_switches_are_wired() -> None:
	text = _workflow_text()
	assert "ENFORCE_FILES_TOUCHED: ${{ vars.ENFORCE_FILES_TOUCHED || 'true' }}" in text
	assert "ALLOW_OUT_OF_SCOPE_FILES: ${{ vars.ALLOW_OUT_OF_SCOPE_FILES || 'false' }}" in text
	assert "PLAN_FILES_TOUCHED_GATE_ENABLED: ${{ vars.PLAN_FILES_TOUCHED_GATE_ENABLED || 'true' }}" in text
	poll = (REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
	gate_steps = poll.count("AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED: ${{ vars.AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED || 'true' }}")
	assert gate_steps >= 1
	for line in ("ENFORCE_FILES_TOUCHED: ${{ vars.ENFORCE_FILES_TOUCHED || 'true' }}",
		"ALLOW_OUT_OF_SCOPE_FILES: ${{ vars.ALLOW_OUT_OF_SCOPE_FILES || 'false' }}",
		"PLAN_FILES_TOUCHED_GATE_ENABLED: ${{ vars.PLAN_FILES_TOUCHED_GATE_ENABLED || 'true' }}"):
		assert poll.count(line) == gate_steps, line


def test_plan_gate_wiring_defaults_on() -> None:
	text = _workflow_text()
	assert "AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED: ${{ vars.AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED || 'true' }}" in text
	assert "clarify_isolated_run.sh files_touched_scope_guard.py plan_split_dispatch.sh; do" in text
	block = _step_block(text, "Auto-approve clear plan")
	assert "--plan-auto-approval-gate" in block
	assert 'plan_gate_reason="plan_gate_helper_unavailable"' in block
	assert "ALLOW_WORKFLOW_EDITS: ${{ vars.ALLOW_WORKFLOW_EDITS || 'true' }}" in block


def test_hold_posts_one_deduplicated_comment_naming_missing_paths() -> None:
	posts: list[str] = []
	posted, _, output = _run_auto_approve(_PIPELINE_ISSUE, plan_text=SECURITY_FINDING_PLAN, gate_flag="false", posts_out=posts)
	assert not posted
	assert "automation_path_hold=true" in output
	assert len(posts) == 1
	hold = posts[0]
	assert "automatic approval of such plans is off" in hold
	assert "- `scripts/security_audit.sh`" in hold
	assert ("<!-- ai:automation-path-plan-hold:v1 issue=123 "
		"reason=automation_path_plan_auto_approval_disabled source=plan paths=") in hold
	marker = hold.split("-f body=", 1)[1]
	comments = [
		{"id": 1, "created_at": "2026-10-09T00:00:00Z", "body": "Implementation Plan\n...",
			"author_association": "OWNER", "user": {"login": "maintainer"}},
		{"id": 2, "created_at": "2026-10-09T00:01:00Z", "body": marker,
			"author_association": "OWNER", "user": {"login": "maintainer"}},
	]
	again: list[str] = []
	posted, _, _ = _run_auto_approve(_PIPELINE_ISSUE, plan_text=SECURITY_FINDING_PLAN, comments=comments, gate_flag="false", posts_out=again)
	assert not posted and again == []
	untrusted = [comments[0], {**comments[1], "author_association": "NONE", "user": {"login": "stranger"}}]
	third: list[str] = []
	_run_auto_approve(_PIPELINE_ISSUE, plan_text=SECURITY_FINDING_PLAN, comments=untrusted, gate_flag="false", posts_out=third)
	assert len(third) == 1


def test_hold_comment_for_partial_grant_and_unavailable_check() -> None:
	issue = dict(_GRANTED_ISSUE, body="files_touched:\n  - scripts/security_audit.sh\n")
	posts: list[str] = []
	_run_auto_approve(issue, plan_text=GRANTED_PLAN, gate_flag="true", posts_out=posts)
	assert len(posts) == 1
	assert "without a trusted exact grant" in posts[0]
	assert "- `.github/workflows/security-audit.yml`" in posts[0]
	assert "- `scripts/security_audit.sh`" not in posts[0]
	posts = []
	_run_auto_approve(_PIPELINE_ISSUE, plan_text="", posts_out=posts)
	assert len(posts) == 1
	assert "could not run" in posts[0]
	assert "reason=plan_gate_error source=plan" in posts[0]


def test_old_gate_helpers_are_gone() -> None:
	# Issue #6769 replaced the #6838 gate; its extractor must not linger.
	text = _workflow_text()
	assert "automation_path_plan_gate()" not in text
	assert "automation-path-plan-extractor" not in text
	poller = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
	assert "_automation_path_plan_auto_approval_gate" not in poller
	assert "automation-path-plan-extractor" not in poller

_LARGE_PLAN = "Implementation Plan\n\n### 1. Files likely to change\n" + "".join(f"- `scripts/f{i}.sh`\n" for i in range(51))


def test_too_large_plan_is_handed_to_the_split_script() -> None:
	# Q151: a plan over the 50-file grant starts an orchestrator project
	# instead of waiting for /approved; nothing is approved or held.
	seen: dict = {}
	posts: list[str] = []
	posted, stdout, output = _run_auto_approve(_PIPELINE_ISSUE, plan_text=_LARGE_PLAN, split_stub_rc=0,
		split_env_out=seen, posts_out=posts)
	assert not posted and posts == []
	assert "plan_split=true" in output and "auto_approve_hold_reason" not in output
	assert "reason=plan_too_large_for_grant outcome=split issue=123" in stdout
	assert seen["REPO"] == "owner/repo" and seen["ISSUE_NUMBER"] == "123" and seen["PLAN_PATH_COUNT"] == "51"
	assert seen["plan"] == _LARGE_PLAN and seen["issue"]["number"] == 123


def test_too_large_plan_holds_when_the_split_does_not_start() -> None:
	# The script exits 3 whenever it cannot start the project (wrapper off,
	# old wrapper, dispatch error, ...): the plan is held exactly as before.
	posts: list[str] = []
	posted, stdout, output = _run_auto_approve(_PIPELINE_ISSUE, plan_text=_LARGE_PLAN, split_stub_rc=3, posts_out=posts)
	assert not posted and "plan_split=true" not in output
	assert "auto_approve_hold_reason=plan_too_large_for_grant" in output
	assert "reason=plan_too_large_for_grant outcome=defer" in stdout
	assert len(posts) == 1 and "reason=plan_too_large_for_grant source=plan" in posts[0]
	# Without the script (a support checkout that predates it) it holds too.
	posted, _, output = _run_auto_approve(_PIPELINE_ISSUE, plan_text=_LARGE_PLAN)
	assert not posted and "auto_approve_hold_reason=plan_too_large_for_grant" in output


def test_other_holds_never_reach_the_split_script() -> None:
	seen: dict = {}
	posted, _, output = _run_auto_approve(_PIPELINE_ISSUE, plan_text=SECURITY_FINDING_PLAN, gate_flag="false",
		split_stub_rc=0, split_env_out=seen)
	assert not posted and seen == {} and "plan_split=true" not in output


def test_split_plan_sends_no_awaiting_approval_alert() -> None:
	text = _workflow_text()
	block = _step_block(text, "Telegram plan notification")
	assert "steps.auto_approve.outputs.plan_split != 'true'" in block
	assert "PLAN_SPLIT_TO_PROJECT_ENABLED: ${{ vars.PLAN_SPLIT_TO_PROJECT_ENABLED || 'true' }}" in _step_block(text, "Auto-approve clear plan")


# --- #7444: workflow-heal issues are never body-patched by the plan gate ---

HEAL_MARKER = ("<!-- ai:workflow-heal-scope:v1 paths=scripts/security_audit.sh,.github/workflows/security-audit.yml,"
	"tests/**,changelog.d/*.md runs=owner/repo:500 -->")
HEAL_BODY = "Heal this failure.\n" + HEAL_MARKER + "\n"
HEAL_PLAN = ("Implementation Plan\n\n### 1. Files likely to change\n"
	"- `scripts/security_audit.sh`\n- `.github/workflows/security-audit.yml`\n- `tests/test_security_audit.py`\n")
_HEAL_ISSUE = {"state": "open", "number": 123, "user": {"type": "User", "login": "pipeline-bot"},
	"author_association": "OWNER", "body": HEAL_BODY, "labels": [{"name": "ai:workflow-heal"}]}


def _heal_graphql(*, edited: bool = False) -> dict:
	return {"data": {"repository": {"issue": {"body": HEAL_BODY, "lastEditedAt": "2026-10-15T00:00:00Z" if edited else None,
		"author": {"login": "pipeline-bot"}, "labels": {"nodes": [{"name": "ai:workflow-heal"}]}}}}}


def test_heal_issue_in_scope_plan_is_approved_without_a_body_write() -> None:
	patches: list[str] = []
	calls: list[str] = []
	posted, stdout, output = _run_auto_approve(_HEAL_ISSUE, plan_text=HEAL_PLAN, gate_flag="true",
		graphql=_heal_graphql(), patches_out=patches, graphql_calls_out=calls)
	assert posted, stdout
	assert "auto_approved=true" in output
	assert patches == []
	assert "PIPELINE_PLAN_GRANT issue=123 source=plan outcome=skipped reason=heal_issue" in stdout
	assert "outcome=written" not in stdout
	assert len(calls) == 1 and "lastEditedAt" in calls[0]


def test_heal_issue_out_of_scope_plan_holds_without_a_body_write() -> None:
	plan = HEAL_PLAN + "- `scripts/other.sh`\n"
	patches: list[str] = []
	posts: list[str] = []
	posted, stdout, output = _run_auto_approve(_HEAL_ISSUE, plan_text=plan, gate_flag="true",
		graphql=_heal_graphql(), patches_out=patches, posts_out=posts)
	assert not posted, stdout
	assert patches == []
	assert "auto_approve_hold_reason=heal_scope_plan_out_of_scope" in output
	assert "outcome=defer issue=123 paths=scripts/other.sh" in stdout
	assert len(posts) == 1 and "verified workflow-heal scope" in posts[0]


def test_edited_heal_issue_holds_as_unverified() -> None:
	patches: list[str] = []
	posted, stdout, output = _run_auto_approve(_HEAL_ISSUE, plan_text=HEAL_PLAN, gate_flag="true",
		graphql=_heal_graphql(edited=True), patches_out=patches)
	assert not posted, stdout
	assert patches == []
	assert "auto_approve_hold_reason=heal_scope_unverified" in output


def test_heal_issue_read_failure_holds_as_check_unavailable() -> None:
	posts: list[str] = []
	posted, stdout, output = _run_auto_approve(_HEAL_ISSUE, plan_text=HEAL_PLAN, gate_flag="true",
		graphql_fails=True, posts_out=posts)
	assert not posted, stdout
	assert "auto_approve_hold_reason=heal_scope_check_unavailable" in output
	assert len(posts) == 1 and "could not run" in posts[0]


def test_heal_fix_flag_off_keeps_the_q108_write() -> None:
	issue = dict(_HEAL_ISSUE, body="Heal this failure. No files_touched block.")
	patches: list[str] = []
	calls: list[str] = []
	posted, stdout, _ = _run_auto_approve(issue, plan_text=SECURITY_FINDING_PLAN, gate_flag="true",
		heal_fix_flag="false", patches_out=patches, graphql_calls_out=calls)
	assert posted, stdout
	assert "PIPELINE_PLAN_GRANT issue=123 source=plan outcome=written" in stdout
	assert len(patches) == 1
	assert calls == []


def test_heal_fix_flag_wiring() -> None:
	block = _step_block(_workflow_text(), "Auto-approve clear plan")
	assert "HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED: ${{ vars.HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED || '' }}" in block
	assert "--heal-scope-file" in block
	implement = (REPO_ROOT / ".github" / "workflows" / "implement.yml").read_text(encoding="utf-8")
	preflight = _step_block(implement, "Heal route preflight")
	assert "HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED: ${{ vars.HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED || '' }}" in preflight


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
