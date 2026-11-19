#!/usr/bin/env python3
"""Contract tests for the red-base plan auto-approval hold (Refs #9421, Phase 2).

scripts/plan_base_ci_gate.sh is sourced by plan.yml's "Auto-approve clear
plan" step right before the `/approved [auto-approved-by-plan]` post. While
the default branch's newest decisive push CI run failed and a trusted open
ai:workflow-heal issue tracks that workflow, the approval is held
(`auto_approve_hold_reason=base_ci_red`). Every read fails open.
"""

from __future__ import annotations

import functools
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


@functools.lru_cache(maxsize=1)
def _workflow_parsed() -> dict:
	# plan.yml is ~247 KB; parse it once per module (no caller mutates the steps).
	return yaml.safe_load(_workflow_text())


def _step(name: str) -> dict:
	workflow = _workflow_parsed()
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
	copy_helper: bool = True, symlink_helper: bool = False, staged: bool = True,
	extra_env: dict | None = None) -> dict:
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
			"PLAN_FILES_TOUCHED_GATE_ENABLED", "PLAN_BASE_CI_GATE_STAGED"):
			env.pop(name, None)
		if staged:
			env["PLAN_BASE_CI_GATE_STAGED"] = "true"
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
	# staged=False: a regular checkout copy (e.g. restored by the merged-PR
	# reroute's forced checkout) that the staging step did not install.
	for kwargs in ({"copy_helper": False}, {"symlink_helper": True}, {"staged": False}):
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
	assert 'if [ "${PLAN_BASE_CI_GATE_STAGED:-false}" = "true" ]' in run
	assert 'echo "auto_approve_hold_reason=base_ci_red" >> "$GITHUB_OUTPUT"' in run


def test_helper_is_staged_from_the_workflow_source() -> None:
	"""Consumer checkouts do not ship the helper, so the staging step must install
	the pipeline copy (and drop a checkout copy when none can be staged)."""
	run = _step("Stage workflow support files")["run"]
	loop_start = run.index("for f in plan_base_ci_gate.sh; do")
	loop = run[loop_start:run.index("\ndone", loop_start)]
	assert 'src=".codex-workflow-src/scripts/${f}"' in loop
	assert 'src=".codex-workflow-src-main/scripts/${f}"' in loop
	assert 'rm -f "scripts/${f}"' in loop
	assert 'install -m 0755 "${src}" "scripts/${f}"' in loop
	assert '_fetched_scripts+=("${f}")' in loop
	assert run.index("for f in plan_base_ci_gate.sh; do") < run.index('if [ "${{ github.repository }}" != "${wf_source}" ]; then')


def _run_staging_loop(*, src: str | None, main: str | None, checkout: str | None) -> dict:
	"""Execute the shipped staging loop in a temp workspace."""
	run = _step("Stage workflow support files")["run"]
	loop_start = run.index("for f in plan_base_ci_gate.sh; do")
	loop = run[loop_start:run.index("\ndone", loop_start) + len("\ndone")]
	with tempfile.TemporaryDirectory() as workdir:
		tmp = Path(workdir)
		(tmp / "scripts").mkdir()
		for root, content in ((".codex-workflow-src", src), (".codex-workflow-src-main", main), (None, checkout)):
			if content is None:
				continue
			target = (tmp / root / "scripts" if root else tmp / "scripts")
			target.mkdir(parents=True, exist_ok=True)
			(target / "plan_base_ci_gate.sh").write_text(content, encoding="utf-8")
		env = dict(os.environ, GITHUB_ENV=str(tmp / "github_env"), SCRIPT_REF="stable")
		env.pop("BASH_ENV", None)
		result = subprocess.run(["bash", "-c", "set -euo pipefail\n_fetched_scripts=()\n" + loop
			+ '\nprintf "fetched=%s\\n" "${_fetched_scripts[*]:-}"'], cwd=tmp, env=env, text=True,
			capture_output=True, check=True)
		helper = tmp / "scripts" / "plan_base_ci_gate.sh"
		github_env = tmp / "github_env"
		return {
			"stdout": result.stdout,
			"helper": helper.read_text(encoding="utf-8") if helper.exists() else None,
			"github_env": github_env.read_text(encoding="utf-8") if github_env.exists() else "",
		}


def test_staging_loop_installs_the_pipeline_copy_or_removes_the_checkout_copy() -> None:
	missing = _run_staging_loop(src=None, main=None, checkout="caller copy")
	assert missing["helper"] is None
	assert "PLAN_BASE_CI_GATE_STAGED=false" in missing["github_env"]
	assert "fetched=\n" in missing["stdout"]
	pinned = _run_staging_loop(src="pinned copy", main="main copy", checkout="caller copy")
	assert pinned["helper"] == "pinned copy"
	assert "PLAN_BASE_CI_GATE_STAGED=true" in pinned["github_env"]
	assert "fetched=plan_base_ci_gate.sh" in pinned["stdout"]
	fallback = _run_staging_loop(src=None, main="main copy", checkout=None)
	assert fallback["helper"] == "main copy"
	assert "PLAN_BASE_CI_GATE_STAGED=true" in fallback["github_env"]


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



# --- poller stall-recovery gate (Refs #9421, Phase 2 step 4; issue #9766) ---


POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
POLLER_GATE_SIGNATURES = ("_red_base_gate_load_helper()\n{", "_red_base_gate_default_branch()\n{", "_plan_red_base_gate()\n{")
POLLER_DETAILS = {
	"501": {"labels": [{"name": "ai:orchestrator-managed"}, {"name": "ai:awaiting-approval"}]},
	"502": {"labels": ["ai:awaiting-approval"]},
	"503": {"labels": [{"name": "ai:workflow-heal"}]},
	"504": {"labels": [{"name": "ai:awaiting-approval"}]},
	"505": {"labels": []},
}
POLLER_GATE_FN = ('gate() { if _plan_red_base_gate "$1" "${DETAILS}" "${2:-}" ai:awaiting-approval; then '
	'echo "rc[$1]=0"; else echo "rc[$1]=1"; fi; }\n')


def _extract_bash_function(script: str, signature: str) -> str:
	start = script.index(signature)
	end = script.index("\n}\n", start) + len("\n}\n")
	return script[start:end]


def _run_poller_gate(snippet: str, *, runs: dict | None = None, heal: list | None = None, helper_mode: str = "copy",
	staged: bool = True, runs_fail: bool = False, heal_fail: bool = False, env: dict | None = None) -> dict:
	"""Run the poller's red-base gate functions against the fake gh."""
	poller = POLLER.read_text(encoding="utf-8")
	functions = "\n".join(_extract_bash_function(poller, signature) for signature in POLLER_GATE_SIGNATURES)
	with tempfile.TemporaryDirectory() as workdir:
		tmp = Path(workdir)
		scripts = tmp / "scripts"
		scripts.mkdir()
		(scripts / "poller_fns.sh").write_text('declare -gA RED_BASE_HEALTH_CACHE=()\n_RED_BASE_GATE_HELPER_STATE=""\n'
			+ functions, encoding="utf-8")
		helper = scripts / "plan_base_ci_gate.sh"
		if helper_mode == "copy":
			shutil.copy(HELPER, helper)
		elif helper_mode == "symlink":
			helper.symlink_to(HELPER)
		elif helper_mode == "nofunc":
			helper.write_text("_red_base_hold_flag_on() { return 0; }\n", encoding="utf-8")
		call_log = tmp / "calls"
		script = ("set -euo pipefail\n" + GH_STUB + f'source "{scripts / "poller_fns.sh"}"\n' + POLLER_GATE_FN + snippet
			+ '\necho "cache=${#RED_BASE_HEALTH_CACHE[@]}"\n')
		run_env = os.environ.copy()
		for name in ("BASH_ENV", "ENV", "RED_BASE_APPROVAL_HOLD_ENABLED", "RED_BASE_HOLD_WORKFLOWS", "PLAN_BASE_CI_GATE_STAGED"):
			run_env.pop(name, None)
		run_env.update({
			"CALL_LOG": str(call_log),
			"POST_LOG": str(tmp / "posts"),
			"RUNS_RESPONSE": json.dumps(runs if runs is not None else RED_RUNS),
			"RUNS_FAIL": str(runs_fail).lower(),
			"HEAL_RESPONSE": json.dumps(heal if heal is not None else [_heal_issue()]),
			"HEAL_FAIL": str(heal_fail).lower(),
			"DETAILS": json.dumps(POLLER_DETAILS),
			"GITHUB_REPOSITORY": "owner/repo",
			"CWS_DEFAULT_BRANCH": "main",
		})
		if staged:
			run_env["PLAN_BASE_CI_GATE_STAGED"] = "true"
		if env:
			run_env.update(env)
		result = subprocess.run(["bash", "-c", script], env=run_env, text=True, capture_output=True, check=True)
		calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"calls": calls,
			"runs_calls": [call for call in calls if "/actions/workflows/" in call],
			"heal_calls": [call for call in calls if "labels=ai:workflow-heal" in call],
		}


def test_poller_red_base_gate_holds_on_red() -> None:
	managed = _run_poller_gate("gate 501 orchestrator/project-1", heal=[_heal_issue(branch="orchestrator/project-1")])
	assert "rc[501]=1" in managed["stdout"], managed
	assert ("RED_BASE_APPROVAL_HOLD outcome=held issue=501 heal=777 workflow=.github/workflows/ci.yml run=9002 "
		"status=red reason=base_ci_red") in managed["stdout"]
	assert ("AI_PHASE_GATE_V1 phase=stall_recovery gate=auto_approve reason=base_ci_red outcome=hold issue=501 "
		"heal=777 branch=orchestrator/project-1") in managed["stdout"]
	assert "STALL_SKIP issue=501 reason=base_ci_red_hold phase=ai:awaiting-approval action=none" in managed["stdout"]
	assert managed["runs_calls"] == ["api repos/owner/repo/actions/workflows/ci.yml/runs?branch=orchestrator%2Fproject-1"
		"&event=push&status=completed&per_page=10"]
	# An unmanaged issue tracks the default branch even when a branch is passed.
	unmanaged = _run_poller_gate("gate 502 orchestrator/project-1")
	assert "rc[502]=1" in unmanaged["stdout"], unmanaged
	assert "STALL_SKIP issue=502 reason=base_ci_red_hold" in unmanaged["stdout"]
	assert unmanaged["runs_calls"] == ["api repos/owner/repo/actions/workflows/ci.yml/runs?branch=main"
		"&event=push&status=completed&per_page=10"]
	# No comment read or post from the poller gate.
	assert all("/comments" not in call for call in managed["calls"] + unmanaged["calls"])


def test_poller_red_base_gate_passes_on_green() -> None:
	result = _run_poller_gate("gate 502", runs=GREEN_RUNS)
	assert "rc[502]=0" in result["stdout"], result
	assert ("RED_BASE_APPROVAL_HOLD outcome=pass issue=502 heal=none workflow=none run=none status=green "
		"reason=base_green") in result["stdout"]
	assert "STALL_SKIP" not in result["stdout"]
	assert "AI_PHASE_GATE_V1" not in result["stdout"]
	assert result["heal_calls"] == []


def test_poller_red_base_gate_kill_switch() -> None:
	for value in ("false", "0", "off"):
		result = _run_poller_gate("gate 502\ngate 504", env={"RED_BASE_APPROVAL_HOLD_ENABLED": value})
		assert "rc[502]=0" in result["stdout"] and "rc[504]=0" in result["stdout"], (value, result)
		assert "RED_BASE_APPROVAL_HOLD outcome=skipped issue=502" in result["stdout"]
		assert "reason=disabled" in result["stdout"]
		assert result["calls"] == []
		assert "cache=0" in result["stdout"]
	heal = _run_poller_gate("gate 503\ngate 503")
	assert heal["stdout"].count("rc[503]=0") == 2, heal
	assert "reason=heal_issue_exempt" in heal["stdout"]
	assert "STALL_SKIP" not in heal["stdout"]
	assert heal["runs_calls"] == [] and "cache=0" in heal["stdout"]


def test_poller_red_base_gate_read_failure_fails_open() -> None:
	runs_failed = _run_poller_gate("gate 502", runs_fail=True)
	assert "rc[502]=0" in runs_failed["stdout"], runs_failed
	assert "status=unknown reason=runs_lookup_failed" in runs_failed["stdout"]
	assert runs_failed["heal_calls"] == []
	heal_failed = _run_poller_gate("gate 502", heal_fail=True)
	assert "rc[502]=0" in heal_failed["stdout"], heal_failed
	assert "status=unknown reason=heal_lookup_failed" in heal_failed["stdout"]
	assert "STALL_SKIP" not in heal_failed["stdout"]
	for kwargs in ({"helper_mode": "missing"}, {"helper_mode": "symlink"}, {"staged": False}, {"helper_mode": "nofunc"}):
		result = _run_poller_gate("gate 502\ngate 504", **kwargs)
		assert "rc[502]=0" in result["stdout"] and "rc[504]=0" in result["stdout"], (kwargs, result)
		assert "RED_BASE_APPROVAL_HOLD" not in result["stdout"]
		assert result["stdout"].count("::notice::red-base approval hold helper unavailable") == 1
		assert result["calls"] == []


def test_poller_red_base_gate_cache_reused_within_tick() -> None:
	heal = [_heal_issue(), _heal_issue(number=778, branch="orchestrator/project-1")]
	result = _run_poller_gate("gate 502\ngate 504\ngate 505\ngate 501 orchestrator/project-1", heal=heal)
	for issue in ("502", "504", "505"):
		assert f"rc[{issue}]=1" in result["stdout"], result
		assert f"RED_BASE_APPROVAL_HOLD outcome=held issue={issue} heal=777" in result["stdout"]
		assert f"STALL_SKIP issue={issue} reason=base_ci_red_hold" in result["stdout"]
	assert "RED_BASE_APPROVAL_HOLD outcome=held issue=501 heal=778" in result["stdout"]
	assert [call.split("branch=")[1].split("&")[0] for call in result["runs_calls"]] == ["main", "orchestrator%2Fproject-1"]
	assert len(result["heal_calls"]) == 2
	assert "cache=2" in result["stdout"]
	failed = _run_poller_gate("gate 502\ngate 504", runs_fail=True)
	assert len(failed["runs_calls"]) == 1, failed
	assert failed["stdout"].count("reason=runs_lookup_failed") == 2
	assert "rc[502]=0" in failed["stdout"] and "rc[504]=0" in failed["stdout"]


def test_poller_red_base_gate_wiring() -> None:
	poller = POLLER.read_text(encoding="utf-8")
	assert "declare -gA RED_BASE_HEALTH_CACHE=()" in poller
	gate = _extract_bash_function(poller, "_plan_auto_approval_gate()\n{")
	assert 'local integration_branch="${5:-}"' in gate
	red_check = ('if ! _plan_red_base_gate "${issue_num}" "${details_map_json}" "${integration_branch}" "${phase}"; then\n'
		"    return 1\n  fi")
	assert red_check in gate
	assert gate.index(red_check) < gate.index("unblock_trusted_login")
	assert '"${_managed_staged_support_comments}" "${phase}" "${_managed_gate_integration_branch}"; then' in poller
	assert '"${_managed_rti_gate_comments}" "${phase}" "${_managed_rti_gate_integration_branch}"; then' in poller
	assert poller.count("jq -r '.integration_branch // \"\"' \"${STATE_FILE:-}\"") == 2
	assert poller.count('_plan_auto_approval_gate "${issue_num}"') == 4
	for default in ('RED_BASE_APPROVAL_HOLD_ENABLED="${RED_BASE_APPROVAL_HOLD_ENABLED-true}"',
		'RED_BASE_HOLD_WORKFLOWS="${RED_BASE_HOLD_WORKFLOWS:-.github/workflows/ci.yml}"',
		'PLAN_BASE_CI_GATE_STAGED="${PLAN_BASE_CI_GATE_STAGED:-false}"'):
		assert default in poller, default
	helper_check = _extract_bash_function(poller, "_red_base_gate_load_helper()\n{")
	assert '[ "${PLAN_BASE_CI_GATE_STAGED:-false}" = "true" ]' in helper_check
	assert '[ ! -L "${helper_path}" ]' in helper_check
	workflow = POLL_WF.read_text(encoding="utf-8")
	loop_start = workflow.index("for f in plan_base_ci_gate.sh; do")
	loop = workflow[loop_start:workflow.index("\n          done", loop_start)]
	assert 'rm -f "scripts/${f}"' in loop
	assert 'echo "PLAN_BASE_CI_GATE_STAGED=false" >> "$GITHUB_ENV"' in loop
	assert 'echo "PLAN_BASE_CI_GATE_STAGED=true" >> "$GITHUB_ENV"' in loop
	assert 'install -m 0755 "${src}" "scripts/${f}"' in loop
	assert workflow.count("RED_BASE_APPROVAL_HOLD_ENABLED: ${{ vars.RED_BASE_APPROVAL_HOLD_ENABLED || 'true' }}") == 5
	assert workflow.count("RED_BASE_HOLD_WORKFLOWS: ${{ vars.RED_BASE_HOLD_WORKFLOWS || '.github/workflows/ci.yml' }}") == 5

if __name__ == "__main__":
	import pytest

	raise SystemExit(pytest.main([__file__, "-q"]))
