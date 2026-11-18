#!/usr/bin/env python3
"""Contract tests for the red-base plan auto-approval hold (Refs #9421, Phase 2).

scripts/plan_base_ci_gate.sh is sourced by plan.yml's "Auto-approve clear
plan" step right before the `/approved [auto-approved-by-plan]` post. While
the default branch's newest decisive push CI run failed and a trusted open
ai:workflow-heal issue tracks that workflow, the approval is held
(`auto_approve_hold_reason=base_ci_red`). Every read fails open.
"""

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
HELPER = REPO_ROOT / "scripts" / "plan_base_ci_gate.sh"
GUARD_SCRIPT = REPO_ROOT / "scripts" / "files_touched_scope_guard.py"
HEAL_SCRIPT = REPO_ROOT / "scripts" / "workflow_failure_heal.py"
PARTIAL_DELIVERY_SCRIPT = REPO_ROOT / "scripts" / "partial_delivery.py"
ORDINARY_PLAN = "Implementation Plan\n\n### 1. Files likely to change\n- `src/app.py`\n- `README.md`\n"
APPROVAL_POST = ("api repos/owner/repo/issues/123/comments -f body=/approved [auto-approved-by-plan]"
	"\n\nAuto approval was posted because AUTO_IMPLEMENT_ON_CLEAR_PLAN is enabled.")
HOLD_MARKER = "<!-- ai:red-base-approval-hold:v1 heal=777 -->"
ISSUE = {"state": "open", "number": 123, "user": {"type": "User", "login": "maintainer"},
	"author_association": "OWNER", "body": "Do the thing.", "labels": [{"name": "ai:awaiting-approval"}]}

GH_STUB = (
	'gh_retry() { "$@"; }\n'
	'_safe_gh_jq() { gh api "$@"; }\n'
	'gh() {\n'
	'  printf "%s\\n" "$*" >> "${CALL_LOG}"\n'
	'  case "$2" in\n'
	'    repos/owner/repo/issues/123)\n'
	'      printf "%s" "${ISSUE_RESPONSE}" ;;\n'
	'    repos/owner/repo/actions/workflows/*)\n'
	'      [ "${RUNS_FAIL:-false}" = "false" ] || return 1\n'
	'      printf "%s" "${RUNS_RESPONSE}" ;;\n'
	'    user)\n'
	'      printf "pipeline-bot\\n" ;;\n'
	'    --paginate)\n'
	'      printf "%s" "${COMMENTS_RESPONSE:-[]}" ;;\n'
	'    -X)\n'
	'      if [ "$3" = "GET" ] && [ "$4" = "repos/owner/repo/issues" ]; then\n'
	'        [ "${HEAL_FAIL:-false}" = "false" ] || return 1\n'
	'        printf "%s" "${HEAL_RESPONSE:-[]}"\n'
	'      else\n'
	'        printf "%s\\n<<END>>\\n" "$*" >> "${POST_LOG}"\n'
	'      fi ;;\n'
	'    *)\n'
	'      printf "%s\\n<<END>>\\n" "$*" >> "${POST_LOG}" ;;\n'
	'  esac\n'
	'}\n'
	'sleep() { :; }\n'
)


def _workflow_text() -> str:
	return PLAN_WF.read_text(encoding="utf-8")


def _step(name: str) -> dict:
	workflow = yaml.safe_load(_workflow_text())
	return next(step for step in workflow["jobs"]["plan"]["steps"] if step.get("name") == name)


def _heal_body(workflow: str = ".github/workflows/ci.yml", branch: str = "main", repo: str = "owner/repo") -> str:
	return "\n".join([
		"Workflow failure heal",
		"",
		"<!-- workflow-failure-heal:fp=abc -->",
		f"<!-- workflow-failure-heal:source={repo}#run -->",
		f"<!-- workflow-failure-heal:workflow={workflow} -->",
		"",
		"",
		f"- **Target branch:** `{branch}`",
		"",
	])


def _heal_issue(number: int = 777, *, association: str = "OWNER", **kwargs) -> dict:
	return {"number": number, "author_association": association, "body": _heal_body(**kwargs),
		"labels": [{"name": "ai:workflow-heal"}]}


def _runs(*runs: tuple[int, str, str]) -> dict:
	return {"workflow_runs": [{"id": run_id, "conclusion": conclusion, "created_at": created}
		for run_id, conclusion, created in runs]}


RED_RUNS = _runs((9001, "success", "2026-11-17T01:00:00Z"), (9002, "failure", "2026-11-18T01:00:00Z"))
GREEN_RUNS = _runs((9001, "failure", "2026-11-17T01:00:00Z"), (9002, "success", "2026-11-18T01:00:00Z"))


def _run_step(*, runs: dict | None = None, runs_fail: bool = False, heal: list | None = None,
	heal_fail: bool = False, comments: list | None = None, issue: dict | None = None,
	copy_helper: bool = True, symlink_helper: bool = False, extra_env: dict | None = None) -> dict:
	"""Run the shipped auto_approve step against a fake gh."""
	with tempfile.TemporaryDirectory() as workdir:
		tmp = Path(workdir)
		scripts = tmp / "scripts"
		scripts.mkdir()
		shutil.copy(GUARD_SCRIPT, scripts / "files_touched_scope_guard.py")
		shutil.copy(HEAL_SCRIPT, scripts / "workflow_failure_heal.py")
		shutil.copy(PARTIAL_DELIVERY_SCRIPT, scripts / "partial_delivery.py")
		if symlink_helper:
			(scripts / "plan_base_ci_gate.sh").symlink_to(HELPER)
		elif copy_helper:
			shutil.copy(HELPER, scripts / "plan_base_ci_gate.sh")
		(scripts / "gh_helpers.sh").write_text(GH_STUB, encoding="utf-8")
		plan_file = tmp / "codex_output.txt"
		plan_file.write_text(ORDINARY_PLAN, encoding="utf-8")
		output = tmp / "output"
		summary = tmp / "summary"
		post_log = tmp / "posts"
		call_log = tmp / "calls"
		env = os.environ.copy()
		for name in ("BASH_ENV", "ENV", "AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED", "ALLOW_WORKFLOW_EDITS",
			"HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED", "HEAL_ROUTE", "PLAN_COMMENT_ID", "RED_BASE_APPROVAL_HOLD_ENABLED",
			"RED_BASE_HOLD_WORKFLOWS", "ENFORCE_FILES_TOUCHED", "ALLOW_OUT_OF_SCOPE_FILES",
			"PLAN_FILES_TOUCHED_GATE_ENABLED"):
			env.pop(name, None)
		env.update({
			"GITHUB_OUTPUT": str(output),
			"GITHUB_STEP_SUMMARY": str(summary),
			"PLAN_STEP_REPO": "owner/repo",
			"PLAN_DEFAULT_BRANCH": "main",
			"ISSUE_NUMBER": "123",
			"ISSUE_RESPONSE": json.dumps(issue or ISSUE),
			"RUNS_RESPONSE": json.dumps(runs if runs is not None else GREEN_RUNS),
			"RUNS_FAIL": str(runs_fail).lower(),
			"HEAL_RESPONSE": json.dumps(heal if heal is not None else []),
			"HEAL_FAIL": str(heal_fail).lower(),
			"COMMENTS_RESPONSE": json.dumps(comments or []),
			"POST_LOG": str(post_log),
			"CALL_LOG": str(call_log),
			"AUTO_IMPLEMENT_ON_CLEAR_PLAN": "true",
			"CODEX_OUTPUT_FILE": str(plan_file),
			"RUNNER_TEMP": str(tmp),
			"HEAL_COUNTERFACTUAL_GATE_ENABLED": "false",
		})
		if extra_env:
			env.update(extra_env)
		result = subprocess.run(["bash", "-c", _step("Auto-approve clear plan")["run"]], cwd=tmp, env=env,
			text=True, capture_output=True, check=True)
		posts = [entry for entry in (post_log.read_text(encoding="utf-8") if post_log.exists() else "").split("\n<<END>>\n")
			if entry.strip()]
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"output": output.read_text(encoding="utf-8"),
			"summary": summary.read_text(encoding="utf-8") if summary.exists() else "",
			"posts": posts,
			"approvals": [entry for entry in posts if "/approved [auto-approved-by-plan]" in entry],
			"calls": call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else [],
		}


def _runs_calls(result: dict) -> list[str]:
	return [call for call in result["calls"] if "/actions/workflows/" in call]


def _heal_calls(result: dict) -> list[str]:
	return [call for call in result["calls"] if "labels=ai:workflow-heal" in call]


# --- step-level behaviour --------------------------------------------------


def test_red_base_holds_the_approval() -> None:
	result = _run_step(runs=RED_RUNS, heal=[_heal_issue()])
	assert result["approvals"] == [], result
	assert "auto_approved=true" not in result["output"]
	assert "auto_approved=false" in result["output"]
	assert "auto_approve_hold_reason=base_ci_red" in result["output"]
	assert ("AI_PHASE_GATE_V1 phase=plan gate=auto_approve reason=base_ci_red outcome=defer issue=123"
		in result["stdout"])
	assert ("RED_BASE_APPROVAL_HOLD outcome=held issue=123 heal=777 workflow=.github/workflows/ci.yml run=9002 "
		"status=red reason=base_ci_red") in result["stdout"]
	assert len(result["posts"]) == 1
	assert result["posts"][0].rstrip().endswith(HOLD_MARKER)
	assert "**Automatic approval held: CI on `main` is red.**" in result["posts"][0]
	assert "/approved" in result["posts"][0] and "[auto-approved-by-plan]" not in result["posts"][0]
	assert "### Automatic plan approval held (base CI red)" in result["summary"]
	runs_calls = _runs_calls(result)
	assert runs_calls == ["api repos/owner/repo/actions/workflows/ci.yml/runs?branch=main&event=push&status=completed&per_page=10"]
	assert len(_heal_calls(result)) == 1


def test_existing_hold_comment_newer_than_plan_is_not_reposted() -> None:
	comments = [
		{"id": 1, "created_at": "2026-11-18T01:00:00Z", "author_association": "OWNER", "user": {"login": "maintainer"},
			"body": "Implementation Plan\n\nstuff"},
		{"id": 2, "created_at": "2026-11-18T02:00:00Z", "author_association": "OWNER", "user": {"login": "maintainer"},
			"body": "held\n\n" + HOLD_MARKER},
	]
	result = _run_step(runs=RED_RUNS, heal=[_heal_issue()], comments=comments)
	assert result["posts"] == [], result
	assert "reason=base_ci_red outcome=defer issue=123" in result["stdout"]
	assert "auto_approve_hold_reason=base_ci_red" in result["output"]


def test_hold_comment_older_than_plan_or_untrusted_is_posted_again() -> None:
	older = [
		{"id": 1, "created_at": "2026-11-18T01:00:00Z", "author_association": "OWNER", "user": {"login": "maintainer"},
			"body": "held\n\n" + HOLD_MARKER},
		{"id": 2, "created_at": "2026-11-18T02:00:00Z", "author_association": "OWNER", "user": {"login": "maintainer"},
			"body": "Implementation Plan\n\nstuff"},
	]
	untrusted = [{"id": 3, "created_at": "2026-11-18T03:00:00Z", "author_association": "NONE",
		"user": {"login": "stranger"}, "body": HOLD_MARKER}]
	for comments in (older, untrusted):
		result = _run_step(runs=RED_RUNS, heal=[_heal_issue()], comments=comments)
		assert len(result["posts"]) == 1 and HOLD_MARKER in result["posts"][0], result
		assert result["approvals"] == []


def test_green_base_approves_unchanged_without_heal_read() -> None:
	result = _run_step(runs=GREEN_RUNS, heal=[_heal_issue()])
	assert result["approvals"] == [APPROVAL_POST]
	assert result["posts"] == [APPROVAL_POST]
	assert "auto_approved=true" in result["output"]
	assert "base_ci_red" not in result["output"]
	assert "RED_BASE_APPROVAL_HOLD outcome=pass issue=123 heal=none workflow=none run=none status=green reason=base_green" in result["stdout"]
	assert _heal_calls(result) == []


def test_newest_cancelled_run_does_not_hide_a_red_base() -> None:
	runs = _runs((9002, "failure", "2026-11-18T01:00:00Z"), (9003, "cancelled", "2026-11-18T02:00:00Z"))
	result = _run_step(runs=runs, heal=[_heal_issue()])
	assert result["approvals"] == []
	assert "run=9002 status=red reason=base_ci_red" in result["stdout"]


def test_untracked_failure_approves() -> None:
	for heal in ([_heal_issue(association="NONE")], [_heal_issue(branch="develop")],
		[_heal_issue(workflow=".github/workflows/other.yml")], [_heal_issue(repo="evil/repo")], []):
		result = _run_step(runs=RED_RUNS, heal=heal)
		assert result["approvals"] == [APPROVAL_POST], (heal, result)
		assert "status=pending reason=failure_untracked" in result["stdout"]


def test_heal_pull_request_is_ignored() -> None:
	heal = _heal_issue()
	heal["pull_request"] = {"url": "x"}
	result = _run_step(runs=RED_RUNS, heal=[heal])
	assert result["approvals"] == [APPROVAL_POST]


def test_heal_issue_is_exempt() -> None:
	issue = dict(ISSUE, labels=[{"name": "ai:workflow-heal"}])
	result = _run_step(runs=RED_RUNS, heal=[_heal_issue()], issue=issue,
		extra_env={"HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED": "false"})
	assert "RED_BASE_APPROVAL_HOLD outcome=skipped" in result["stdout"]
	assert "reason=heal_issue_exempt" in result["stdout"]
	assert "base_ci_red" not in result["output"]
	assert _runs_calls(result) == []


def test_kill_switch_off_or_invalid_restores_legacy_behaviour() -> None:
	legacy = _run_step(runs=RED_RUNS, heal=[_heal_issue()], copy_helper=False)
	assert legacy["approvals"] == [APPROVAL_POST]
	for value in ("false", "FALSE", " off ", "0", "no", "maybe"):
		result = _run_step(runs=RED_RUNS, heal=[_heal_issue()], extra_env={"RED_BASE_APPROVAL_HOLD_ENABLED": value})
		assert result["approvals"] == legacy["approvals"], (value, result)
		assert result["posts"] == legacy["posts"]
		assert "auto_approved=true" in result["output"]
		assert "RED_BASE_APPROVAL_HOLD outcome=skipped" in result["stdout"] and "reason=disabled" in result["stdout"]
		assert _runs_calls(result) == []
	invalid = _run_step(runs=RED_RUNS, heal=[_heal_issue()], extra_env={"RED_BASE_APPROVAL_HOLD_ENABLED": "maybe"})
	assert "::warning::RED_BASE_APPROVAL_HOLD_ENABLED has an invalid value" in invalid["stdout"]


def test_flag_on_values_hold() -> None:
	for value in ("true", "TRUE", "1", "yes", "on", ""):
		result = _run_step(runs=RED_RUNS, heal=[_heal_issue()], extra_env={"RED_BASE_APPROVAL_HOLD_ENABLED": value})
		assert result["approvals"] == [], value


def test_unreadable_status_fails_open() -> None:
	runs_failed = _run_step(runs_fail=True, heal=[_heal_issue()])
	assert runs_failed["approvals"] == [APPROVAL_POST]
	assert "status=unknown reason=runs_lookup_failed" in runs_failed["stdout"]
	assert _heal_calls(runs_failed) == []
	bad_json = _run_step(runs={"message": "Not Found"}, heal=[_heal_issue()])
	assert bad_json["approvals"] == [APPROVAL_POST]
	assert "status=unknown reason=runs_lookup_failed" in bad_json["stdout"]
	heal_failed = _run_step(runs=RED_RUNS, heal_fail=True)
	assert heal_failed["approvals"] == [APPROVAL_POST]
	assert "status=unknown reason=heal_lookup_failed" in heal_failed["stdout"]
	no_runs = _run_step(runs=_runs((1, "cancelled", "2026-11-18T01:00:00Z")))
	assert no_runs["approvals"] == [APPROVAL_POST]
	assert "status=pending reason=no_completed_run" in no_runs["stdout"]


def test_missing_or_symlinked_helper_skips_the_check() -> None:
	for kwargs in ({"copy_helper": False}, {"symlink_helper": True}):
		result = _run_step(runs=RED_RUNS, heal=[_heal_issue()], **kwargs)
		assert result["approvals"] == [APPROVAL_POST], kwargs
		assert "RED_BASE_APPROVAL_HOLD" not in result["stdout"]
		assert _runs_calls(result) == [] and _heal_calls(result) == []


# --- workflow text ----------------------------------------------------------


def test_auto_approve_step_wiring() -> None:
	step = _step("Auto-approve clear plan")
	env = step["env"]
	assert env["RED_BASE_APPROVAL_HOLD_ENABLED"] == "${{ vars.RED_BASE_APPROVAL_HOLD_ENABLED || 'true' }}"
	assert env["RED_BASE_HOLD_WORKFLOWS"] == "${{ vars.RED_BASE_HOLD_WORKFLOWS || '.github/workflows/ci.yml' }}"
	assert env["PLAN_DEFAULT_BRANCH"] == "${{ github.event.repository.default_branch }}"
	run = step["run"]
	assert "${{" not in run
	hold = run.index("red_base_approval_hold_check \"${PLAN_STEP_REPO}\"")
	assert run.index("if [ -n \"${heal_to_marker}\" ]; then") < hold < run.index("sleep 3")
	assert "[ ! -L scripts/plan_base_ci_gate.sh ]" in run
	assert 'echo "auto_approve_hold_reason=base_ci_red" >> "$GITHUB_OUTPUT"' in run


def test_telegram_uses_debug_only_for_base_ci_red() -> None:
	step = _step("Telegram plan notification")
	assert step["env"]["PLAN_AUTO_APPROVE_HOLD_REASON"] == "${{ steps.auto_approve.outputs.auto_approve_hold_reason || '' }}"
	run = step["run"]
	branch = run.index('elif [ "${PLAN_AUTO_APPROVE_HOLD_REASON:-}" = "base_ci_red" ]; then')
	debug = run.index('tg_send_phase_tracked "${ISSUE_NUMBER}" "plan" "${MSG}" "DEBUG"')
	critical = run.index('tg_send_phase_tracked "${ISSUE_NUMBER}" "plan" "${MSG}" "CRITICAL"')
	assert branch < debug < critical


# --- direct helper unit tests ----------------------------------------------


def _run_helper(snippet: str, *, env: dict | None = None, runs: dict | None = None, heal: list | None = None) -> subprocess.CompletedProcess:
	with tempfile.TemporaryDirectory() as workdir:
		tmp = Path(workdir)
		call_log = tmp / "calls"
		script = (
			GH_STUB
			+ f'source "{HELPER}"\n'
			+ snippet
			+ '\necho "CALLS_BEGIN"; cat "${CALL_LOG}" 2>/dev/null || true\n'
		)
		run_env = os.environ.copy()
		for name in ("BASH_ENV", "ENV", "RED_BASE_APPROVAL_HOLD_ENABLED", "RED_BASE_HOLD_WORKFLOWS"):
			run_env.pop(name, None)
		run_env.update({
			"CALL_LOG": str(call_log),
			"POST_LOG": str(tmp / "posts"),
			"RUNS_RESPONSE": json.dumps(runs if runs is not None else RED_RUNS),
			"HEAL_RESPONSE": json.dumps(heal if heal is not None else [_heal_issue()]),
		})
		if env:
			run_env.update(env)
		return subprocess.run(["bash", "-c", script], env=run_env, text=True, capture_output=True, check=False)


CHECK_CALL = ('red_base_approval_hold_check owner/repo main 123 "[]" false; '
	'echo "rc=$? status=${RED_BASE_HOLD_STATUS} red=${RED_BASE_HOLD_RED} heal=${RED_BASE_HOLD_HEAL}"')


def test_helper_direct_execution_prints_usage() -> None:
	result = subprocess.run(["bash", str(HELPER)], text=True, capture_output=True, check=False)
	assert result.returncode == 2
	assert "usage:" in result.stderr


def test_helper_does_not_change_set_options() -> None:
	result = subprocess.run(["bash", "-c", f'before="$-"; source "{HELPER}"; [ "$-" = "${{before}}" ] && echo same'],
		text=True, capture_output=True, check=True)
	assert result.stdout.strip() == "same"


def test_helper_status_values() -> None:
	red = _run_helper(CHECK_CALL)
	assert "rc=0 status=red red=true heal=777" in red.stdout
	green = _run_helper(CHECK_CALL, runs=GREEN_RUNS)
	assert "rc=1 status=green red=false heal=none" in green.stdout
	pending = _run_helper(CHECK_CALL, runs=_runs())
	assert "rc=1 status=pending" in pending.stdout
	unknown = _run_helper(CHECK_CALL, env={"RUNS_FAIL": "true"})
	assert "rc=1 status=unknown" in unknown.stdout


def test_helper_branch_slash_is_encoded() -> None:
	result = _run_helper('red_base_approval_hold_check owner/repo release/v1 123 "[]" false; echo "rc=$?"',
		heal=[_heal_issue(branch="release/v1")])
	assert "rc=0" in result.stdout
	assert "repos/owner/repo/actions/workflows/ci.yml/runs?branch=release%2Fv1&event=push" in result.stdout


def test_helper_rejects_invalid_inputs() -> None:
	for args in ('"own er/repo" main 123', 'owner/repo "ma in" 123', "owner/repo ../main 123", "owner/repo main 0",
		"owner/repo main abc"):
		result = _run_helper(f'red_base_approval_hold_check {args} "[]" false; echo "rc=$?"')
		assert "rc=1" in result.stdout, args
		assert "reason=input_invalid" in result.stdout
		assert "actions/workflows" not in result.stdout.split("CALLS_BEGIN")[1]


def test_helper_workflow_list_validation() -> None:
	bad = _run_helper(CHECK_CALL, env={"RED_BASE_HOLD_WORKFLOWS": "ci.yml, .github/workflows/../x.yml, scripts/a.yml"})
	assert "reason=no_workflows" in bad.stdout and "rc=1" in bad.stdout
	assert "::warning::RED_BASE_HOLD_WORKFLOWS" in bad.stderr
	many = ",".join(f".github/workflows/w{i}.yml" for i in range(7)) + ",.github/workflows/w0.yml"
	capped = _run_helper(CHECK_CALL, env={"RED_BASE_HOLD_WORKFLOWS": many}, runs=GREEN_RUNS)
	calls = [line for line in capped.stdout.split("CALLS_BEGIN")[1].splitlines() if "actions/workflows" in line]
	assert len(calls) == 5
	assert [call.split("/actions/workflows/")[1].split("/")[0] for call in calls] == [f"w{i}.yml" for i in range(5)]
	assert "duplicate" in capped.stderr
	default = _run_helper(CHECK_CALL, env={"RED_BASE_HOLD_WORKFLOWS": " , "})
	assert "workflows/ci.yml/runs" in default.stdout


def test_helper_second_failing_workflow_can_hold() -> None:
	result = _run_helper(CHECK_CALL, env={"RED_BASE_HOLD_WORKFLOWS": ".github/workflows/ci.yml,.github/workflows/e2e.yml"},
		heal=[_heal_issue(number=800, workflow=".github/workflows/e2e.yml")])
	assert "rc=0 status=red red=true heal=800" in result.stdout
	assert "workflow=.github/workflows/e2e.yml" in result.stdout
	heal_reads = [line for line in result.stdout.split("CALLS_BEGIN")[1].splitlines() if "labels=ai:workflow-heal" in line]
	assert len(heal_reads) == 1


def test_helper_oldest_matching_heal_issue_wins() -> None:
	result = _run_helper(CHECK_CALL, heal=[_heal_issue(number=500), _heal_issue(number=900)])
	assert "heal=500" in result.stdout


def test_helper_heal_route_exempt() -> None:
	result = _run_helper('red_base_approval_hold_check owner/repo main 123 "[]" true; echo "rc=$?"')
	assert "rc=1" in result.stdout and "reason=heal_issue_exempt" in result.stdout
	assert "actions/workflows" not in result.stdout.split("CALLS_BEGIN")[1]


def test_helper_comment_rejects_invalid_state() -> None:
	result = _run_helper('RED_BASE_HOLD_HEAL=none; red_base_approval_hold_comment owner/repo 123 main; echo "rc=$?"')
	assert "rc=1" in result.stdout
	assert "invalid input" in result.stdout


if __name__ == "__main__":
	import pytest

	raise SystemExit(pytest.main([__file__, "-q"]))
