#!/usr/bin/env python3
"""Heal-route counterfactual check and plan auto-approval gate (issue #8249).

Covers the shared parser (scripts/partial_delivery.py heal-counterfactual),
the heal-only prompt block run_plan_codex.sh appends, the heal intake's
"Would the suggested fix have passed?" subsection, plan.yml's
"Auto-approve clear plan" step and the poller wiring.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
PARTIAL_DELIVERY = SCRIPTS / "partial_delivery.py"
RUN_PLAN = SCRIPTS / "run_plan_codex.sh"
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
POLLER = SCRIPTS / "orchestrate_poll_process.sh"
CF_PROMPT = REPO_ROOT / "prompts" / "mode-plan-heal-counterfactual.txt"
CF_TEMPLATE = REPO_ROOT / "prompts" / "_templates" / "mode-plan-heal-counterfactual.txt"
HEAL_PROMPTS = (REPO_ROOT / "prompts" / "mode-workflow-failure-heal.txt",
	REPO_ROOT / "prompts" / "_templates" / "mode-workflow-failure-heal.txt")
FIXTURE_8241 = REPO_ROOT / "tests" / "fixtures" / "heal_counterfactual" / "issue_8241_plan.md"
FIXTURE_8327 = REPO_ROOT / "tests" / "fixtures" / "heal_counterfactual" / "issue_8327_plan.md"
# The trailing note the #8327 planner wrote inside its "## Deferred scope" section.
NOTE_8327 = "\nThese items are delivered through a separate issue that is not a heal issue,"
DEFERRED_MISSING_HINT = "The section must appear once and hold only `- ` bullets: a prose line or note inside it makes it unreadable."


def _load_partial_delivery():
	spec = importlib.util.spec_from_file_location("partial_delivery_under_test", PARTIAL_DELIVERY)
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	return module


PD = _load_partial_delivery()


def _plan(line: str | None, *, deferred: list[str] | None = None, extra: str = "") -> str:
	parts = ["Implementation Plan", "", "## Files likely to change", "- `scripts/a.sh`", ""]
	if deferred is not None:
		parts += ["## Deferred scope"] + [f"- {item}" for item in deferred] + [""]
	parts += ["## Counterfactual check", "", "- job-a: walked the evidence.", ""]
	if line is not None:
		parts.append(line)
	parts += [extra, "", "STATUS: CLEAR", "PLAN_SELF_CHECK: PASS", ""]
	return "\n".join(parts)


def _check(text: str, flag: str | None = "true") -> dict:
	return PD.parse_heal_counterfactual(text, flag)


# --- parser -----------------------------------------------------------------


def test_pass_approves() -> None:
	result = _check(_plan("HEAL_COUNTERFACTUAL: PASS"))
	assert result["decision"] == "approve" and result["result"] == "PASS", result
	assert result["gate"] == "on" and result["flag_invalid"] is False


def test_pass_with_text_is_malformed() -> None:
	result = _check(_plan("HEAL_COUNTERFACTUAL: PASS: probably"))
	assert result["decision"] == "hold" and result["result"] == "missing" and result["reason"] == "malformed_line", result


def test_fail_and_unverified_hold() -> None:
	fail = _check(_plan("HEAL_COUNTERFACTUAL: FAIL: still times out"))
	assert (fail["decision"], fail["result"], fail["reason"]) == ("hold", "FAIL", "fail"), fail
	unverified = _check(_plan("HEAL_COUNTERFACTUAL: UNVERIFIED: missing step timeline"))
	assert (unverified["decision"], unverified["result"], unverified["reason"]) == ("hold", "UNVERIFIED", "unverified")


def test_fail_or_unverified_without_text_is_malformed() -> None:
	for line in ("HEAL_COUNTERFACTUAL: FAIL:", "HEAL_COUNTERFACTUAL: UNVERIFIED", "HEAL_COUNTERFACTUAL: FAIL:   "):
		result = _check(_plan(line))
		assert (result["decision"], result["reason"]) == ("hold", "malformed_line"), (line, result)


def test_unknown_verb_and_lowercase_value_are_malformed() -> None:
	for line in ("HEAL_COUNTERFACTUAL: MAYBE", "HEAL_COUNTERFACTUAL: pass", "HEAL_COUNTERFACTUAL:"):
		result = _check(_plan(line))
		assert (result["decision"], result["reason"]) == ("hold", "malformed_line"), (line, result)


def test_missing_line_holds() -> None:
	result = _check(_plan(None))
	assert (result["decision"], result["result"], result["reason"]) == ("hold", "missing", "no_line"), result


def test_duplicate_lines_hold() -> None:
	for second in ("HEAL_COUNTERFACTUAL: PASS", "HEAL_COUNTERFACTUAL: FAIL: no"):
		result = _check(_plan("HEAL_COUNTERFACTUAL: PASS", extra=second))
		assert (result["decision"], result["result"], result["reason"]) == ("hold", "missing", "duplicate_line"), result


def test_fenced_and_mid_line_and_lowercase_token_are_ignored() -> None:
	for extra in ("```\nHEAL_COUNTERFACTUAL: PASS\n```", "~~~text\nHEAL_COUNTERFACTUAL: PASS\n~~~",
		"The line HEAL_COUNTERFACTUAL: PASS goes last.", "heal_counterfactual: PASS"):
		result = _check(_plan(None, extra=extra))
		assert (result["decision"], result["reason"]) == ("hold", "no_line"), (extra, result)
	# A fenced copy beside the real line is not a duplicate.
	result = _check(_plan("HEAL_COUNTERFACTUAL: PASS", extra="```\nHEAL_COUNTERFACTUAL: FAIL: x\n```"))
	assert result["decision"] == "approve", result
	# Only a bare marker closes a fence: "```text" inside an open fence stays fenced.
	nested = "## Counterfactual check\n\n```\nexample:\n```text\nHEAL_COUNTERFACTUAL: PASS\n```\n\nHEAL_COUNTERFACTUAL: FAIL: x\n"
	result = _check(nested)
	assert (result["decision"], result["result"]) == ("hold", "FAIL"), result


def test_indented_code_block_marker_is_ignored() -> None:
	# Review finding: a marker indented by 4+ spaces or a tab is an indented
	# code block, not the plan's line.
	for extra in ("    HEAL_COUNTERFACTUAL: PASS", "\tHEAL_COUNTERFACTUAL: PASS"):
		result = _check(_plan(None, extra=extra))
		assert (result["decision"], result["reason"]) == ("hold", "no_line"), (extra, result)
	result = _check(_plan("HEAL_COUNTERFACTUAL: FAIL: no", extra="    HEAL_COUNTERFACTUAL: PASS"))
	assert (result["decision"], result["result"]) == ("hold", "FAIL"), result
	assert _check(_plan("   HEAL_COUNTERFACTUAL: PASS"))["decision"] == "approve"


def test_partial_approves_when_every_job_is_deferred() -> None:
	text = _plan("HEAL_COUNTERFACTUAL: PARTIAL: E2E-Smoke-Test, lint",
		deferred=["The e2e-smoke-test job still times out in the pre-push gate", "Fix the lint job separately"])
	result = _check(text)
	assert (result["decision"], result["result"], result["reason"]) == ("approve", "PARTIAL", "partial_jobs_deferred"), result
	assert result["jobs"] == ["E2E-Smoke-Test", "lint"]


def test_partial_holds_with_unmatched_jobs() -> None:
	text = _plan("HEAL_COUNTERFACTUAL: PARTIAL: e2e-smoke-test, validate-scripts",
		deferred=["The e2e-smoke-test job still times out"])
	result = _check(text)
	assert (result["decision"], result["result"], result["reason"]) == ("hold", "FAIL", "partial_jobs_not_deferred"), result
	assert result["unmatched_jobs"] == ["validate-scripts"]


def test_partial_holds_without_a_readable_deferred_scope() -> None:
	assert _check(_plan("HEAL_COUNTERFACTUAL: PARTIAL: job-a"))["reason"] == "deferred_scope_missing"
	malformed = _plan("HEAL_COUNTERFACTUAL: PARTIAL: job-a", deferred=[]).replace("## Deferred scope\n", "## Deferred scope\nprose\n")
	assert _check(malformed)["reason"] == "deferred_scope_missing"


def test_partial_holds_for_an_invalid_job_list() -> None:
	deferred = ["job-a", "job-b"]
	for line in ("HEAL_COUNTERFACTUAL: PARTIAL:", "HEAL_COUNTERFACTUAL: PARTIAL", "HEAL_COUNTERFACTUAL: PARTIAL: job-a,,job-b",
		"HEAL_COUNTERFACTUAL: PARTIAL: " + ", ".join(f"job-{i}" for i in range(21)),
		"HEAL_COUNTERFACTUAL: PARTIAL: " + "x" * 201):
		result = _check(_plan(line, deferred=deferred))
		assert (result["decision"], result["result"], result["reason"]) == ("hold", "FAIL", "partial_job_list_invalid"), (line, result)


def test_line_outside_the_counterfactual_section_holds() -> None:
	# Review finding: the marker counts only inside "## Counterfactual check".
	for text in ("## Risks\n\nHEAL_COUNTERFACTUAL: PASS\n",
		"## Counterfactual check\n\n- walked it.\n\n## Risks\n\nHEAL_COUNTERFACTUAL: PASS\n"):
		result = _check(text)
		assert (result["decision"], result["reason"]) == ("hold", "outside_counterfactual_section"), (text, result)
	nested = "## Counterfactual check\n\n### job-a\n\nwalked.\n\nHEAL_COUNTERFACTUAL: PASS\n"
	assert _check(nested)["decision"] == "approve"
	# Only a level-2 heading opens the section.
	for level in ("###", "######"):
		deeper = f"{level} Counterfactual check\n\nHEAL_COUNTERFACTUAL: PASS\n"
		result = _check(deeper)
		assert (result["decision"], result["reason"]) == ("hold", "outside_counterfactual_section"), (level, result)


def test_indented_heading_closes_the_counterfactual_section() -> None:
	# Review finding: a heading indented by 1-3 spaces is still a heading and
	# ends the "## Counterfactual check" section.
	for indent in (" ", "  ", "   "):
		text = f"## Counterfactual check\n\n- walked it.\n\n{indent}## Risks\n\nHEAL_COUNTERFACTUAL: PASS\n"
		result = _check(text)
		assert (result["decision"], result["reason"]) == ("hold", "outside_counterfactual_section"), (indent, result)
	assert _check("  ## Counterfactual check\n\nHEAL_COUNTERFACTUAL: PASS\n")["decision"] == "approve"
	# A 4-space indent is a code block, not a heading, and does not close it.
	code = "## Counterfactual check\n\n    ## Risks\n\nHEAL_COUNTERFACTUAL: PASS\n"
	assert _check(code)["decision"] == "approve"


def test_partial_job_names_match_whole_tokens_only() -> None:
	text = _plan("HEAL_COUNTERFACTUAL: PARTIAL: build", deferred=["The build-image job still times out"])
	result = _check(text)
	assert (result["decision"], result["reason"]) == ("hold", "partial_jobs_not_deferred"), result
	assert result["unmatched_jobs"] == ["build"]
	assert _check(_plan("HEAL_COUNTERFACTUAL: PARTIAL: build", deferred=["The build job still fails"]))["decision"] == "approve"


def test_gate_flag_handling() -> None:
	fail_plan = _plan("HEAL_COUNTERFACTUAL: FAIL: no")
	off = _check(fail_plan, "false")
	assert (off["gate"], off["decision"], off["reason"], off["result"]) == ("off", "approve", "gate_disabled", "FAIL"), off
	for flag in (None, "", "true", " TRUE "):
		assert _check(fail_plan, flag)["decision"] == "hold", flag
	invalid = _check(fail_plan, "banana")
	assert (invalid["gate"], invalid["flag_invalid"], invalid["decision"]) == ("on", True, "hold"), invalid


def test_display_line_is_sanitized() -> None:
	result = _check(_plan("HEAL_COUNTERFACTUAL: FAIL: see #123 @user `x` <!-- y -->"))
	line = result["line"]
	assert "<!--" not in line and "`" not in line, line
	assert "#123" not in line and "@user" not in line, line
	long_line = _check(_plan("HEAL_COUNTERFACTUAL: FAIL: " + "z" * 600))["line"]
	assert len(long_line) <= PD.HEAL_CF_MAX_LINE_CHARS


def test_issue_8241_fixture_is_held() -> None:
	text = FIXTURE_8241.read_text(encoding="utf-8")
	result = _check(text)
	assert (result["decision"], result["result"]) == ("hold", "FAIL"), result
	# The plan the fixture reconstructs still ends CLEAR / PASS: the gate, not the
	# self-check, is what withholds approval.
	assert "STATUS: CLEAR" in text and "PLAN_SELF_CHECK: PASS" in text


def test_issue_8327_fixture_trailing_note_is_held() -> None:
	"""Issue #8327: a prose note after the bullets made the section unreadable."""
	text = FIXTURE_8327.read_text(encoding="utf-8")
	result = _check(text)
	assert (result["decision"], result["result"], result["reason"]) == ("hold", "FAIL", "deferred_scope_missing"), result
	assert result["jobs"] == ["e2e-smoke-test"]
	assert PD.parse_plan(text)["reason"] == "prose_in_block"
	# The same plan without the note is a fully deferred PARTIAL: the note alone held it.
	start = text.index(NOTE_8327)
	end = text.index("\n\n", start + 1)
	bullets_only = text[:start] + text[end:]
	result = _check(bullets_only)
	assert (result["decision"], result["result"], result["reason"]) == ("approve", "PARTIAL", "partial_jobs_deferred"), result
	assert len(PD.parse_plan(bullets_only)["items"]) == 3


def test_cli_exit_codes() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		plan = Path(tmp) / "plan.md"
		for line in ("HEAL_COUNTERFACTUAL: PASS", "HEAL_COUNTERFACTUAL: nonsense", None):
			plan.write_text(_plan(line), encoding="utf-8")
			proc = subprocess.run([sys.executable, "-I", "-B", str(PARTIAL_DELIVERY), "heal-counterfactual",
				"--plan-file", str(plan), "--gate-flag", "banana"], capture_output=True, text=True, check=False)
			assert proc.returncode == 0, proc.stderr
			assert json.loads(proc.stdout)["flag_invalid"] is True
		proc = subprocess.run([sys.executable, "-I", "-B", str(PARTIAL_DELIVERY), "heal-counterfactual",
			"--plan-file", str(Path(tmp) / "missing.md")], capture_output=True, text=True, check=False)
		assert proc.returncode == 2


# --- prompts ----------------------------------------------------------------


def test_counterfactual_prompt_contract() -> None:
	text = CF_PROMPT.read_text(encoding="utf-8")
	assert text.startswith("# tier: DEFAULT\n")
	assert CF_TEMPLATE.read_bytes() == CF_PROMPT.read_bytes()
	for needle in ("## Counterfactual check", "HEAL_COUNTERFACTUAL: PASS", "HEAL_COUNTERFACTUAL: PARTIAL:",
		"HEAL_COUNTERFACTUAL: FAIL:", "HEAL_COUNTERFACTUAL: UNVERIFIED:", "## Deferred scope",
		"## Would the suggested fix have passed?", "re-derive"):
		assert needle in text, needle
	assert "{{" not in text
	# Issue #8327: the planner must know that a note inside the section holds the plan.
	assert "section holds only `- ` bullets: a prose line or note inside it makes it" in text


def test_heal_intake_prompt_has_the_would_it_pass_subsection() -> None:
	bodies = [path.read_text(encoding="utf-8") for path in HEAL_PROMPTS]
	assert bodies[0] == bodies[1]
	text = bodies[0]
	assert text.index("## Suggested fix") < text.index("## Would the suggested fix have passed?") < text.index("## Affected files")
	assert "`<job>: unknown`" in text and "Not applicable: <classification>" in text


def _heal_route_block() -> str:
	text = RUN_PLAN.read_text(encoding="utf-8")
	start = text.index('if [ "${HEAL_ROUTE:-false}" = true ]; then\n\t[ -s ')
	end = text.index("\nfi\n", start) + len("\nfi\n")
	return text[start:end]


def test_run_plan_appends_the_block_only_on_the_heal_route() -> None:
	block = _heal_route_block()
	assert "prompts/mode-plan-heal-counterfactual.txt" in block
	assert RUN_PLAN.read_text(encoding="utf-8").count("mode-plan-heal-counterfactual.txt") == 1
	for heal_route, prompt_present in (("true", True), ("false", True), ("true", False)):
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			(root / "scripts").mkdir()
			(root / "scripts" / "render_prompt.sh").write_text('cat "$1"\n', encoding="utf-8")
			if prompt_present:
				(root / "prompts").mkdir()
				shutil.copy(CF_PROMPT, root / "prompts" / CF_PROMPT.name)
			(root / "evidence.md").write_text("EVIDENCE\n", encoding="utf-8")
			(root / "prompt.txt").write_text("BASE\n", encoding="utf-8")
			env = {"PATH": os.environ.get("PATH", ""), "HEAL_ROUTE": heal_route, "RUNTIME_DIR": str(root),
				"HEAL_EVIDENCE_FILE": str(root / "evidence.md"), "CODEX_PROMPT_FILE": str(root / "prompt.txt")}
			proc = subprocess.run(["bash", "-c", "set -euo pipefail\n" + block], cwd=root, env=env,
				capture_output=True, text=True, check=False)
			assert proc.returncode == 0, proc.stderr
			prompt = (root / "prompt.txt").read_text(encoding="utf-8")
			if heal_route == "false":
				assert prompt == "BASE\n"
			elif prompt_present:
				assert "EVIDENCE" in prompt and "HEAL COUNTERFACTUAL CHECK" in prompt
				assert prompt.index("EVIDENCE") < prompt.index("HEAL COUNTERFACTUAL CHECK")
			else:
				assert "HEAL COUNTERFACTUAL CHECK" not in prompt
				assert "::warning::HEAL_COUNTERFACTUAL prompt unavailable" in proc.stdout


# --- plan.yml "Auto-approve clear plan" -------------------------------------


def _step_run(step_name: str) -> str:
	"""The step's run: body (no YAML parser needed)."""
	lines = PLAN_WF.read_text(encoding="utf-8").split("\n")
	start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}")
	run_index = next(i for i in range(start + 1, len(lines)) if re.match(r"^\s+run: \|\s*$", lines[i]))
	indent = None
	body: list[str] = []
	for line in lines[run_index + 1:]:
		if not line.strip():
			body.append("")
			continue
		current = len(line) - len(line.lstrip(" "))
		if indent is None:
			indent = current
		if current < indent:
			break
		body.append(line[indent:])
	return "\n".join(body)


def _step_env(step_name: str) -> str:
	text = PLAN_WF.read_text(encoding="utf-8")
	start = text.index(f"- name: {step_name}")
	return text[start:text.index("run: |", start)]


_HEAL_ISSUE = {"state": "open", "number": 123, "user": {"type": "User", "login": "pipeline-bot"},
	"author_association": "OWNER", "body": "Heal this failure.", "labels": [{"name": "ai:workflow-heal"}]}
_ORDINARY_ISSUE = {"state": "open", "number": 123, "user": {"type": "User", "login": "maintainer"},
	"author_association": "OWNER", "body": "Fix it.", "labels": []}
_ORDINARY_PLAN = "Implementation Plan\n\n### 1. Files likely to change\n- `src/app.py`\n"
_FAKE_GH = '''gh_retry() { "$@"; }
_safe_gh_jq() { gh api "$@"; }
gh() {
  if [ "$2" = "repos/owner/repo/issues/123" ]; then
    printf '%s' "${ISSUE_RESPONSE}"
  elif [ "$2" = "user" ]; then
    printf '%s\\n' pipeline-bot
  elif [ "$2" = "graphql" ]; then
    return 1
  elif [ "$2" = "--paginate" ]; then
    printf '%s' "${COMMENTS_RESPONSE}"
  else
    printf '%s\\n<<END>>\\n' "$*" >> "${POST_LOG}"
  fi
}
sleep() { :; }
'''


def _jq_available() -> bool:
	return shutil.which("jq") is not None


def _run_step(issue: dict, plan_text: str, *, flag: str | None = None, comments: list | None = None,
	heal_route: str = "false", copy_parser: bool = True) -> dict:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "scripts").mkdir()
		for name in ("files_touched_scope_guard.py", "workflow_failure_heal.py"):
			shutil.copy(SCRIPTS / name, root / "scripts" / name)
		if copy_parser:
			shutil.copy(PARTIAL_DELIVERY, root / "scripts" / "partial_delivery.py")
		(root / "scripts" / "gh_helpers.sh").write_text(_FAKE_GH, encoding="utf-8")
		(root / "plan.txt").write_text(plan_text, encoding="utf-8")
		env = {name: value for name, value in os.environ.items()
			if name not in ("BASH_ENV", "ENV", "HEAL_COUNTERFACTUAL_GATE_ENABLED", "AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED",
				"HEAL_ROUTE", "PLAN_COMMENT_ID", "GITHUB_STEP_SUMMARY", "HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED")}
		env.update({"GITHUB_OUTPUT": str(root / "output"), "PLAN_STEP_REPO": "owner/repo", "ISSUE_NUMBER": "123",
			"ISSUE_RESPONSE": json.dumps(issue), "COMMENTS_RESPONSE": json.dumps(comments or []),
			"POST_LOG": str(root / "posts"), "AUTO_IMPLEMENT_ON_CLEAR_PLAN": "true", "CODEX_OUTPUT_FILE": str(root / "plan.txt"),
			"RUNNER_TEMP": str(root), "PLAN_COMMENT_ID": "555", "HEAL_ROUTE": heal_route})
		if flag is not None:
			env["HEAL_COUNTERFACTUAL_GATE_ENABLED"] = flag
		proc = subprocess.run(["bash", "-c", _step_run("Auto-approve clear plan")], cwd=root, env=env, text=True,
			capture_output=True, check=False)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		posts_text = (root / "posts").read_text(encoding="utf-8") if (root / "posts").exists() else ""
		posts = [entry for entry in posts_text.split("\n<<END>>\n") if entry.strip()]
		return {"stdout": proc.stdout, "output": (root / "output").read_text(encoding="utf-8"),
			"approvals": [p for p in posts if "/approved [auto-approved-by-plan]" in p],
			"holds": [p for p in posts if "<!-- ai:heal-counterfactual-hold:v1 " in p]}


def _skip_without_jq() -> bool:
	"""The step needs jq; skip (pytest) or return early (direct run) without it."""
	if _jq_available():
		return False
	pytest = sys.modules.get("pytest")
	if pytest is not None:
		pytest.skip("jq binary not available")
	print("SKIP: jq not available")
	return True


def test_plan_step_holds_the_8241_fixture() -> None:
	if _skip_without_jq():
		return
	run = _run_step(_HEAL_ISSUE, FIXTURE_8241.read_text(encoding="utf-8"))
	assert run["approvals"] == [], run["stdout"]
	assert "auto_approve_hold_reason=heal_counterfactual_hold" in run["output"]
	assert ("AI_PHASE_GATE_V1 phase=plan gate=heal_counterfactual outcome=hold result=FAIL reason=fail issue=123") in run["stdout"]
	assert len(run["holds"]) == 1
	hold = run["holds"][0]
	assert "<!-- ai:heal-counterfactual-hold:v1 issue=123 plan_comment=555 result=FAIL source=plan -->" in hold
	assert "Counterfactual line: `HEAL_COUNTERFACTUAL: FAIL:" in hold
	assert "`/approved`, or `/reclarify`" in hold


def test_plan_step_hold_explains_an_unreadable_deferred_scope() -> None:
	if _skip_without_jq():
		return
	run = _run_step(_HEAL_ISSUE, FIXTURE_8327.read_text(encoding="utf-8"))
	assert run["approvals"] == [], run["stdout"]
	assert ("AI_PHASE_GATE_V1 phase=plan gate=heal_counterfactual outcome=hold result=FAIL "
		"reason=deferred_scope_missing issue=123") in run["stdout"]
	assert len(run["holds"]) == 1
	assert "Reason: `deferred_scope_missing`" in run["holds"][0]
	assert DEFERRED_MISSING_HINT in run["holds"][0]


def test_deferred_scope_missing_hint_matches_in_both_callers() -> None:
	for path in (PLAN_WF, POLLER):
		assert path.read_text(encoding="utf-8").count(DEFERRED_MISSING_HINT) == 1, path


def test_plan_step_hold_comment_is_deduplicated() -> None:
	if _skip_without_jq():
		return
	marker = {"user": {"login": "pipeline-bot"}, "author_association": "OWNER",
		"body": "held\n<!-- ai:heal-counterfactual-hold:v1 issue=123 plan_comment=555 result=FAIL source=poller -->"}
	run = _run_step(_HEAL_ISSUE, FIXTURE_8241.read_text(encoding="utf-8"), comments=[marker])
	assert run["approvals"] == [] and run["holds"] == [], run["stdout"]
	# A marker from an outsider does not suppress the comment.
	outsider = dict(marker, user={"login": "stranger"}, author_association="NONE")
	run = _run_step(_HEAL_ISSUE, FIXTURE_8241.read_text(encoding="utf-8"), comments=[outsider])
	assert len(run["holds"]) == 1


def test_plan_step_gate_off_and_invalid_flag() -> None:
	if _skip_without_jq():
		return
	fail_plan = FIXTURE_8241.read_text(encoding="utf-8")
	off = _run_step(_HEAL_ISSUE, fail_plan, flag="false")
	assert "gate=heal_counterfactual outcome=approve result=FAIL reason=gate_disabled issue=123" in off["stdout"]
	assert "auto_approve_hold_reason=heal_counterfactual_hold" not in off["output"]
	assert off["holds"] == []
	invalid = _run_step(_HEAL_ISSUE, fail_plan, flag="banana")
	assert "::warning::HEAL_COUNTERFACTUAL_GATE_ENABLED has invalid value; treating as true" in invalid["stdout"]
	assert "auto_approve_hold_reason=heal_counterfactual_hold" in invalid["output"]


def test_plan_step_pass_reaches_the_heal_scope_gate() -> None:
	if _skip_without_jq():
		return
	run = _run_step(_HEAL_ISSUE, _plan("HEAL_COUNTERFACTUAL: PASS"))
	assert "gate=heal_counterfactual outcome=approve result=PASS reason=pass issue=123" in run["stdout"]
	# The existing workflow-heal scope gate still decides (its read fails here).
	assert "auto_approve_hold_reason=heal_scope_check_unavailable" in run["output"]
	assert run["holds"] == []


def test_plan_step_missing_parser_holds() -> None:
	if _skip_without_jq():
		return
	run = _run_step(_HEAL_ISSUE, _plan("HEAL_COUNTERFACTUAL: PASS"), copy_parser=False)
	assert "reason=heal_counterfactual_check_unavailable" in run["stdout"]
	assert "auto_approve_hold_reason=heal_counterfactual_hold" in run["output"]
	invalid = _run_step(_HEAL_ISSUE, _plan("HEAL_COUNTERFACTUAL: PASS"), copy_parser=False, flag="banana")
	assert "::warning::HEAL_COUNTERFACTUAL_GATE_ENABLED has invalid value; treating as true" in invalid["stdout"]
	assert "auto_approve_hold_reason=heal_counterfactual_hold" in invalid["output"]
	off = _run_step(_HEAL_ISSUE, _plan("HEAL_COUNTERFACTUAL: PASS"), copy_parser=False, flag="false")
	assert "reason=gate_disabled issue=123" in off["stdout"], off["stdout"]
	assert "auto_approve_hold_reason=heal_counterfactual_hold" not in off["output"]


def test_plan_step_heal_route_env_gates_an_unlabelled_issue() -> None:
	if _skip_without_jq():
		return
	run = _run_step(_ORDINARY_ISSUE, _ORDINARY_PLAN, heal_route="true")
	assert "gate=heal_counterfactual outcome=hold result=missing reason=no_line issue=123" in run["stdout"]
	assert run["approvals"] == []


def test_plan_step_gate_off_releases_an_unknown_route() -> None:
	if _skip_without_jq():
		return
	# A non-string issue body makes heal-route fail, so the route is unknown.
	broken = dict(_ORDINARY_ISSUE, body=123)
	held = _run_step(broken, _ORDINARY_PLAN)
	assert "reason=heal_route_unknown" in held["stdout"], held["stdout"]
	assert "auto_approve_hold_reason=heal_counterfactual_hold" in held["output"]
	# An invalid flag on an unknown route is held and logged as invalid.
	invalid = _run_step(broken, _ORDINARY_PLAN, flag="banana")
	assert "reason=heal_route_unknown" in invalid["stdout"], invalid["stdout"]
	assert "::warning::HEAL_COUNTERFACTUAL_GATE_ENABLED has invalid value; treating as true" in invalid["stdout"]
	# An unmatched quote in the flag does not abort the step.
	quoted = _run_step(broken, _ORDINARY_PLAN, flag="fa'lse")
	assert "auto_approve_hold_reason=heal_counterfactual_hold" in quoted["output"]
	off = _run_step(broken, _ORDINARY_PLAN, flag="FALSE")
	assert "reason=heal_route_unknown" not in off["stdout"]
	assert "auto_approve_hold_reason=heal_counterfactual_hold" not in off["output"]


def test_plan_step_leaves_non_heal_issues_alone() -> None:
	if _skip_without_jq():
		return
	run = _run_step(_ORDINARY_ISSUE, _ORDINARY_PLAN)
	assert "gate=heal_counterfactual" not in run["stdout"]
	assert len(run["approvals"]) == 1, run["stdout"]


# --- wiring -----------------------------------------------------------------


def test_plan_workflow_wiring() -> None:
	text = PLAN_WF.read_text(encoding="utf-8")
	assert "HEAL_COUNTERFACTUAL_GATE_ENABLED: ${{ vars.HEAL_COUNTERFACTUAL_GATE_ENABLED || 'true' }}" in _step_env("Auto-approve clear plan")
	assert " pr_dependency.py partial_delivery.py assemble_prompt.sh " in text
	assert "prompts/mode-plan-heal-counterfactual.txt" in text
	post = _step_run("Post implementation plan")
	assert 'PLAN_COMMENT_ID="$(gh_retry gh api' in post and "--jq '.id'" in post
	approve = _step_run("Auto-approve clear plan")
	# The counterfactual gate runs before the automation-path gate.
	assert approve.index("partial_delivery.py heal-counterfactual") < approve.index('plan_gate_file="${CODEX_OUTPUT_FILE:-}"')


def test_poller_wiring() -> None:
	poller = POLLER.read_text(encoding="utf-8")
	assert 'HEAL_COUNTERFACTUAL_GATE_ENABLED="${HEAL_COUNTERFACTUAL_GATE_ENABLED:-true}"' in poller
	gate = poller.split("\n_plan_auto_approval_gate()\n", 1)[1].split("\n}\n", 1)[0]
	assert gate.index("_plan_heal_counterfactual_check") < gate.index("--plan-auto-approval-gate")
	assert "reason=heal_counterfactual_hold phase=${phase} action=none" in gate
	assert poller.count('_plan_auto_approval_gate "${issue_num}"') == 4
	workflow = POLL_WF.read_text(encoding="utf-8")
	assert " pr_dependency.py partial_delivery.py pr_checks_lib.sh " in workflow
	assert workflow.count("HEAL_COUNTERFACTUAL_GATE_ENABLED: ${{ vars.HEAL_COUNTERFACTUAL_GATE_ENABLED || 'true' }}") == 5


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
