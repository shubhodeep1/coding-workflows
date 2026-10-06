#!/usr/bin/env python3
"""Standalone issues answered by the clarify-respond worker (issue #6262 follow-up).

clarify.yml delegates a standalone issue's questions to
orchestrate_clarify_respond.yml, which answers them with the Claude
clarify-respond worker, falls back to the RECOMMENDED options when the worker
fails, and records every decision (and any placeholder setup item) in the
issue's auto-decisions comment.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
RESPOND = ROOT / ".github" / "workflows" / "orchestrate_clarify_respond.yml"
RELEASE_GATE = ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
AUTO_DECISIONS = ROOT / "scripts" / "auto_decisions.py"
RESPOND_PROMPTS = (ROOT / "prompts" / "mode-clarify-respond.txt", ROOT / "prompts" / "_templates" / "mode-clarify-respond.txt")
CLARIFY_PROMPTS = (ROOT / "prompts" / "mode-clarify.txt", ROOT / "prompts" / "_templates" / "mode-clarify.txt")
SPEC = importlib.util.spec_from_file_location("auto_decisions_standalone", AUTO_DECISIONS)
auto = importlib.util.module_from_spec(SPEC)
sys.modules["auto_decisions_standalone"] = auto
SPEC.loader.exec_module(auto)

QUESTIONS = """<!-- ai:clarification-questions -->
Clarification required

**Q1: Which callers should the fix cover?**

Choices:
- **A** — Only the SUMMARISER call
- **B** — Every read-profile role (RECOMMENDED)
- **C** — B plus write-profile roles

**Q2: Which identity attests a verdict?**

Choices:
- **A** — A dedicated bot identity; provision its credentials privately (RECOMMENDED)
- **B** — Disable verdict-triggered auto-merge

Reply with:
/answer
"""

WORKER_ANSWER = """DECISIONS:
Q1: C
Q2: A

RATIONALE:
Q1: Write-profile roles read the same files,
  so the engine fix covers them too.
Q2: The fixer identity ships as a placeholder.

SETUP REQUIRED:
- FIXER_APP_TOKEN (secret): a GitHub App token used only by the fixer; until set: verdict-triggered auto-merge is skipped.
"""


def _steps() -> dict[str, dict]:
	workflow = yaml.safe_load(RESPOND.read_text(encoding="utf-8"))
	return {step.get("name", ""): step for step in workflow["jobs"]["respond"]["steps"]}


def _substitute(script: str) -> str:
	for expression, value in {
		"${{ github.repository }}": "owner/repo",
		"${{ github.server_url }}": "https://github.com",
		"${{ github.run_id }}": "1",
	}.items():
		script = script.replace(expression, value)
	assert "${{" not in script, script
	return script


def _stub_gh(bin_dir: Path, log: Path, responses: dict[str, str]) -> None:
	"""A `gh` that logs its arguments and answers `gh api <path>` from `responses`."""
	bin_dir.mkdir(parents=True, exist_ok=True)
	table = bin_dir / "responses.json"
	table.write_text(json.dumps(responses), encoding="utf-8")
	gh = bin_dir / "gh"
	gh.write_text(
		"#!/usr/bin/env python3\n"
		"import json, sys\n"
		f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
		f"table = json.load(open({str(table)!r}))\n"
		"path = next((a for a in sys.argv[2:] if not a.startswith('-') and '=' not in a), '')\n"
		"if path in table:\n"
		"    print(table[path])\n"
		"else:\n"
		"    print('{}')\n",
		encoding="utf-8",
	)
	gh.chmod(gh.stat().st_mode | stat.S_IXUSR)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
	env = {
		"PATH": f"{tmp_path / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
		"PYTHONDONTWRITEBYTECODE": "1",
		"GITHUB_OUTPUT": str(tmp_path / "output"),
		"GITHUB_ENV": str(tmp_path / "env"),
		"RUNNER_TEMP": str(tmp_path),
		"GITHUB_RUN_ID": "1",
		"GITHUB_RUN_ATTEMPT": "1",
		"ISSUE_NUMBER": "6262",
		"ISSUE_URL": "https://github.com/owner/repo/issues/6262",
		"ISSUE_TITLE": "[security-audit] finding",
		"RUNTIME_DIR": str(tmp_path / "runtime"),
		"CODEX_OUTPUT_FILE": str(tmp_path / "runtime" / "codex_output.txt"),
	}
	for name in ("GITHUB_OUTPUT", "GITHUB_ENV"):
		Path(env[name]).touch()
	(tmp_path / "runtime").mkdir(exist_ok=True)
	env.update(extra)
	return env


def _outputs(path: Path) -> dict[str, str]:
	return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


# --- Gate -----------------------------------------------------------------


def _run_gate(tmp_path: Path, comment: str, body: str = "Fix it.", state: str = "open", labels: list[str] | None = None, title: str = "[security-audit] finding", user_type: str = "User", author_association: str | None = "OWNER", tracking_payload: dict | None = None, **flags: str) -> tuple[dict[str, str], str, str]:
	payload = {"number": 6262, "title": title, "body": body, "state": state, "labels": [{"name": name} for name in (labels or [])], "user": {"type": user_type}}
	if author_association is not None:
		payload["author_association"] = author_association
	responses = {"repos/owner/repo/issues/6262": json.dumps(payload)}
	if tracking_payload is not None:
		responses["repos/owner/repo/issues/123"] = json.dumps(tracking_payload)
	_stub_gh(tmp_path / "bin", tmp_path / "gh.log", responses)
	env = _env(
		tmp_path,
		CLARIFY_RESPOND_COMMENT_BODY=comment,
		STANDALONE_AUTO_DECIDE_ENABLED=flags.get("auto", "true"),
		STANDALONE_CLARIFY_RESPOND_ENABLED=flags.get("respond", "true"),
	)
	script = _substitute(_steps()["Check orchestrator metadata"]["run"])
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	return _outputs(tmp_path / "output"), (tmp_path / "env").read_text(encoding="utf-8"), result.stdout


@pytest.mark.parametrize(
	"case, kwargs, mode, reason",
	[
		("orchestrator", {"comment": "Clarification required", "body": "Managed by: AI Orchestrator"}, "orchestrator", None),
		("standalone", {"comment": QUESTIONS}, "standalone", "clarify_questions"),
		("auto-decide off", {"comment": QUESTIONS, "auto": "false"}, "skip", "disabled"),
		("worker off", {"comment": QUESTIONS, "respond": "false"}, "skip", "disabled"),
		("plan or human comment", {"comment": "Clarification required\n\nQ1: ..."}, "skip", "not_clarify_questions"),
		("after /reclarify", {"comment": QUESTIONS + "\n<!-- ai:clarification-human-answer -->\n"}, "skip", "human_answer_requested"),
		("closed", {"comment": QUESTIONS, "state": "closed"}, "skip", "issue_closed"),
		("label only", {"comment": QUESTIONS, "labels": ["ai:orchestrator-managed"]}, "skip", "orchestrator_label_without_metadata"),
	],
)
def test_gate_picks_the_mode(tmp_path: Path, case: str, kwargs: dict, mode: str, reason: str | None) -> None:
	outputs, _, stdout = _run_gate(tmp_path, **kwargs)
	assert outputs["mode"] == mode, case
	assert outputs["respond"] == ("true" if mode != "skip" else "false"), case
	assert outputs["is_orchestrator"] == ("true" if mode == "orchestrator" else "false"), case
	if reason:
		outcome = "run" if mode == "standalone" else "skip"
		assert f"AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=standalone reason={reason} outcome={outcome} issue=6262" in stdout, case


def test_gate_silences_release_gate_fixtures(tmp_path: Path) -> None:
	_, env, _ = _run_gate(tmp_path, QUESTIONS, title="[E2E Clarify Negative Test] under-specified task (run 1)", body="Make it better.")
	assert "ALERT_MSG_LEVEL=SILENT" in env


@pytest.mark.parametrize("user_type, author_association", [
	("User", "NONE"),
	("User", "CONTRIBUTOR"),
	("User", "FIRST_TIME_CONTRIBUTOR"),
	("Bot", "OWNER"),
	("User", None),
])
def test_gate_keeps_alerts_for_unverified_fixture_authors(tmp_path: Path, user_type: str, author_association: str | None) -> None:
	_, env, stdout = _run_gate(tmp_path, QUESTIONS, title="[E2E Clarify Negative Test] untrusted", user_type=user_type, author_association=author_association)
	assert "ALERT_MSG_LEVEL=SILENT" not in env
	assert "gate=smoke_alert_silence reason=unverified_fixture_provenance" in stdout


@pytest.mark.parametrize("child_labels, child_association, parent_labels, parent_association, silent", [
	(["ai:orchestrator-managed"], "OWNER", ["ai:orchestrator-tracking"], "MEMBER", True),
	([], "OWNER", ["ai:orchestrator-tracking"], "OWNER", False),
	(["ai:orchestrator-managed"], "NONE", ["ai:orchestrator-tracking"], "OWNER", False),
	(["ai:orchestrator-managed"], "OWNER", [], "OWNER", False),
	(["ai:orchestrator-managed"], "OWNER", ["ai:orchestrator-tracking"], "NONE", False),
])
def test_parent_fixture_silencing_requires_both_verified_issues(tmp_path: Path, child_labels: list[str], child_association: str, parent_labels: list[str], parent_association: str, silent: bool) -> None:
	parent = {"title": "[Orchestrator] E2E Smoke Test", "user": {"type": "User"}, "author_association": parent_association, "labels": [{"name": name} for name in parent_labels]}
	_, env, stdout = _run_gate(tmp_path, QUESTIONS, body="Tracking issue: #123\nManaged by: AI Orchestrator", labels=child_labels, author_association=child_association, tracking_payload=parent)
	assert ("ALERT_MSG_LEVEL=SILENT" in env) is silent
	if not silent:
		assert "gate=smoke_alert_silence reason=unverified_fixture_provenance" in stdout


def test_standalone_issue_cannot_borrow_a_fixture_parent(tmp_path: Path) -> None:
	parent = {"title": "[Orchestrator] E2E Smoke Test", "user": {"type": "User"}, "author_association": "OWNER", "labels": [{"name": "ai:orchestrator-tracking"}]}
	_, env, stdout = _run_gate(tmp_path, QUESTIONS, body="Tracking issue: #123", author_association="NONE", tracking_payload=parent)
	assert "ALERT_MSG_LEVEL=SILENT" not in env
	assert "gate=smoke_alert_silence reason=unverified_fixture_provenance" in stdout


def test_every_later_step_waits_for_the_gate() -> None:
	text = RESPOND.read_text(encoding="utf-8")
	assert "outputs.is_orchestrator ==" not in text
	for name, step in _steps().items():
		if name == "Check orchestrator metadata":
			continue
		condition = str(step.get("if", ""))
		assert "steps.check_orchestrator.outputs.respond == 'true'" in condition or "steps.check_orchestrator.outputs.mode == 'standalone'" in condition or "steps.ai_engine.outputs.engine == 'claude'" in condition, name


# --- Worker, facts and fallback wiring --------------------------------------


def test_facts_are_fetched_on_the_host_and_given_to_the_model() -> None:
	steps = _steps()
	names = list(steps)
	facts = steps["Fetch GitHub facts for the questions"]
	assert facts["continue-on-error"] is True
	assert "steps.semantic_cache_lookup.outputs.cache_hit != 'true'" in facts["if"]
	# A standalone question about live GitHub state must not replay a
	# semantic-cache answer produced before that state changed.
	for name in ("Semantic cache lookup", "Semantic cache store", "Restore semantic cache SQLite", "Save semantic cache SQLite"):
		assert "steps.check_orchestrator.outputs.mode != 'standalone'" in steps[name]["if"]
	assert names.index("Semantic cache lookup") < names.index("Fetch GitHub facts for the questions") < names.index("Remove git auth before Codex execution")
	assert "python3 scripts/clarify_github_facts.py" in facts["run"]
	assert "consumer-repos-file" not in facts["run"]
	assert "consumer_repos.json" not in facts["run"]
	run = steps["Build prompt and run Codex"]["run"]
	assert run.index('echo "=== CLARIFICATION QUESTIONS ==="') < run.index('echo "=== GITHUB FACTS ==="')
	stage = steps["Stage workflow support files"]["run"]
	for script in ("auto_decisions.py", "clarify_github_facts.py", "clarify_data_provision_guard.py"):
		assert f" {script}" in stage.split("; do", 1)[0]


def test_standalone_worker_failure_does_not_end_the_job() -> None:
	steps = _steps()
	assert steps["Build prompt and run Codex"]["continue-on-error"] == "${{ steps.check_orchestrator.outputs.mode == 'standalone' }}"
	assert "steps.run_codex.outcome == 'failure'" in steps["Standalone RECOMMENDED fallback"]["if"]
	assert "steps.standalone_fallback.outputs.ready == 'true'" in steps["Data-provision guard"]["if"]
	guard_run = steps["Data-provision guard"]["run"]
	assert '"${RUNTIME_DIR}/issue_body.txt" "${RUNTIME_DIR}/github_facts.txt"' in guard_run
	assert 'EVIDENCE_ARGS+=(--evidence-file "${evidence_file}")' in guard_run
	assert '"${EVIDENCE_ARGS[@]}"' in guard_run
	parse = steps["Parse and post answer"]["if"]
	assert "(steps.run_codex.outcome != 'failure' || steps.standalone_fallback.outputs.ready == 'true')" in parse
	names = list(steps)
	assert names.index("Standalone RECOMMENDED fallback") < names.index("Data-provision guard") < names.index("Parse and post answer") < names.index("Record standalone auto-decisions")
	assert names.index("Standalone RECOMMENDED fallback") < names.index("Answer completeness guard") < names.index("Data-provision guard")
	assert steps["Answer completeness guard"]["id"] == "answer_completeness"
	assert steps["Answer completeness guard"]["if"] == steps["Data-provision guard"]["if"].split(" && steps.answer_completeness", 1)[0]
	for name in ("Data-provision guard", "Parse and post answer"):
		assert "steps.answer_completeness.outputs.complete == 'true'" in steps[name]["if"]
	assert "steps.run_codex.outcome != 'failure'" in parse
	assert "steps.run_codex.outcome == 'success'" in steps["Answer completeness guard"]["if"]


def test_data_guard_step_fails_closed_without_guard_or_inputs(tmp_path: Path) -> None:
	env = _env(tmp_path)
	script = _steps()["Data-provision guard"]["run"]
	for setup in ("missing_guard", "missing_questions", "guard_error"):
		guard_path = tmp_path / "scripts" / "clarify_data_provision_guard.py"
		questions_path = tmp_path / "runtime" / "clarification_comment.txt"
		if setup != "missing_guard":
			guard_path.parent.mkdir(exist_ok=True)
			guard_path.write_text("raise RuntimeError('guard unavailable')\n", encoding="utf-8")
		if setup != "missing_questions":
			questions_path.write_text(QUESTIONS, encoding="utf-8")
		Path(env["CODEX_OUTPUT_FILE"]).write_text(WORKER_ANSWER, encoding="utf-8")
		result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
		assert result.returncode != 0, (setup, result.stdout, result.stderr)
		assert Path(env["CODEX_OUTPUT_FILE"]).read_text(encoding="utf-8") == WORKER_ANSWER
		guard_path.unlink(missing_ok=True)
		questions_path.unlink(missing_ok=True)


def _install_scripts(tmp_path: Path, sent: Path) -> None:
	scripts = tmp_path / "scripts"
	scripts.mkdir(exist_ok=True)
	shutil.copy(AUTO_DECISIONS, scripts / "auto_decisions.py")
	(scripts / "tg_helpers.sh").write_text(
		f'tg_send_phase_tracked() {{ printf "%s|%s\\n" "$4" "$3" >> "{sent}"; }}\n', encoding="utf-8"
	)


def _run_fallback(tmp_path: Path, questions: str) -> tuple[dict[str, str], str, str]:
	sent = tmp_path / "sent.txt"
	_install_scripts(tmp_path, sent)
	env = _env(tmp_path)
	(tmp_path / "runtime" / "clarification_comment.txt").write_text(questions, encoding="utf-8")
	Path(env["CODEX_OUTPUT_FILE"]).write_text("partial output from a failed attempt", encoding="utf-8")
	script = _substitute(_steps()["Standalone RECOMMENDED fallback"]["run"])
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	return _outputs(tmp_path / "output"), Path(env["CODEX_OUTPUT_FILE"]).read_text(encoding="utf-8"), (sent.read_text(encoding="utf-8") if sent.exists() else "") + result.stdout


def _run_completeness(tmp_path: Path, answer: str, questions: str = QUESTIONS, helper: bool = True) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str, str]:
	sent = tmp_path / "sent.txt"
	_install_scripts(tmp_path, sent)
	if not helper:
		(tmp_path / "scripts" / "auto_decisions.py").unlink()
	env = _env(tmp_path, CLARIFY_RESPOND_MODE="standalone")
	(tmp_path / "runtime" / "clarification_comment.txt").write_text(questions, encoding="utf-8")
	Path(env["CODEX_OUTPUT_FILE"]).write_text(answer, encoding="utf-8")
	script = _substitute(_steps()["Answer completeness guard"]["run"])
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	return result, _outputs(tmp_path / "output"), Path(env["CODEX_OUTPUT_FILE"]).read_text(encoding="utf-8"), sent.read_text(encoding="utf-8") if sent.exists() else ""


def test_completeness_step_fills_before_the_data_guard(tmp_path: Path) -> None:
	result, outputs, answers, sent = _run_completeness(tmp_path, "DECISIONS:\nQ1: C\n")
	assert result.returncode == 0 and outputs == {"complete": "true", "filled": "Q2"}
	assert auto.complete_answers(QUESTIONS, answers)["status"] == "complete"
	assert "Q2: A\n" in answers and "CRITICAL" not in sent
	assert "outcome=filled missing=Q2" in result.stdout


def test_completeness_step_blocks_and_pages_without_editing(tmp_path: Path) -> None:
	partial = "DECISIONS:\nQ1: C\n"
	result, outputs, answers, sent = _run_completeness(tmp_path, partial, QUESTIONS.replace(" (RECOMMENDED)", ""))
	assert result.returncode == 0 and outputs == {"complete": "false"}
	assert answers == partial and "CRITICAL|Clarification required" in sent
	assert "reason=incomplete_answer_undecided" in result.stdout


def test_completeness_step_fails_closed_when_helper_is_missing(tmp_path: Path) -> None:
	result, outputs, answers, _ = _run_completeness(tmp_path, WORKER_ANSWER, helper=False)
	assert result.returncode == 1 and outputs == {}
	assert answers == WORKER_ANSWER


def test_fallback_posts_recommended_options_without_paging(tmp_path: Path) -> None:
	outputs, answers, log = _run_fallback(tmp_path, QUESTIONS)
	assert outputs["ready"] == "true"
	# `jq -r` adds one newline, as in clarify.yml's own RECOMMENDED path.
	assert answers.rstrip("\n") == "Q1: B\nQ2: A"
	assert "STANDALONE_AUTO_DECIDE issue=6262 outcome=fallback reason=worker_failed decisions=2" in log
	assert "CRITICAL" not in log


def test_fallback_pages_when_a_question_has_no_recommendation(tmp_path: Path) -> None:
	outputs, _, log = _run_fallback(tmp_path, QUESTIONS.replace(" (RECOMMENDED)", "", 1))
	assert outputs["ready"] == "false"
	assert "reason=worker_failed_undecided" in log
	assert "CRITICAL|Clarification required for #6262: [security-audit] finding" in log


def test_fallback_guard_checks_later_recommended_answer(tmp_path: Path) -> None:
	questions = QUESTIONS.replace("A dedicated bot identity; provision its credentials privately", "Provide the PR URL for verification")
	questions = questions.replace("Disable verdict-triggered auto-merge", "Skip this verification")
	outputs, answers, _ = _run_fallback(tmp_path, questions)
	assert outputs["ready"] == "true"
	clarification_path = tmp_path / "runtime" / "clarification_comment.txt"
	answer_path = tmp_path / "runtime" / "codex_output.txt"
	assert answers.rstrip("\n") == "Q1: B\nQ2: A"
	result = subprocess.run(
		[sys.executable, str(ROOT / "scripts" / "clarify_data_provision_guard.py"),
		"--clarification-file", str(clarification_path), "--answers-file", str(answer_path)],
		capture_output=True, text=True, check=True,
	)
	assert "Q1: B\nQ2: ESCALATE" in result.stdout
	assert "Q2: escalated A" in result.stdout
	assert "ESCALATION:" in result.stdout


def _run_record(tmp_path: Path, comments: list[dict], answer: str = WORKER_ANSWER, **extra: str) -> tuple[list[list[str]], str]:
	_install_scripts(tmp_path, tmp_path / "sent.txt")
	_stub_gh(tmp_path / "bin", tmp_path / "gh.log", {})
	settings = {"CLARIFICATION_COMMENT_ID": "5986601001", "CLARIFY_RESPOND_FALLBACK_READY": "false", "CLARIFY_RESPOND_CACHE_HIT": "false", "CLARIFY_RESPOND_ENGINE": "claude"}
	settings.update(extra)
	env = _env(tmp_path, **settings)
	(tmp_path / "runtime" / "clarification_comment.txt").write_text(QUESTIONS, encoding="utf-8")
	(tmp_path / "runtime" / "issue_all_comments.json").write_text(json.dumps(comments), encoding="utf-8")
	Path(env["CODEX_OUTPUT_FILE"]).write_text(answer, encoding="utf-8")
	script = _substitute(_steps()["Record standalone auto-decisions"]["run"])
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	log = tmp_path / "gh.log"
	calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
	return calls, result.stdout


def _body(call: list[str]) -> str:
	return next(arg.split("=", 1)[1] for arg in call if arg.startswith("body="))


def test_worker_decisions_and_setup_items_are_recorded(tmp_path: Path) -> None:
	calls, stdout = _run_record(tmp_path, [])
	assert len(calls) == 1 and calls[0][:2] == ["api", "repos/owner/repo/issues/6262/comments"]
	body = _body(calls[0])
	assert body.startswith(auto.MARKER + "\n")
	assert "- **AD-1** (clarify comment 5986601001, clarify-respond on claude, Q1) Which callers should the fix cover? → **C**: Write-profile roles read the same files, so the engine fix covers them too." in body
	assert "→ **A**: The fixer identity ships as a placeholder." in body
	assert "### Setup required" in body
	assert "- **SETUP-1** FIXER_APP_TOKEN (secret): a GitHub App token used only by the fixer; until set: verdict-triggered auto-merge is skipped." in body
	assert "STANDALONE_AUTO_DECIDE issue=6262 outcome=answered decider=clarify-respond_on_claude decisions=2 ad_total=2 setup_total=1" in stdout


def test_filled_decisions_are_attributed_separately(tmp_path: Path) -> None:
	filled = auto.complete_answers(QUESTIONS, "DECISIONS:\nQ1: C\n")["answers"]
	calls, stdout = _run_record(tmp_path, [], filled, CLARIFY_RESPOND_FILLED_QIDS="Q2")
	body = _body(calls[0])
	assert "clarify comment 5986601001, clarify-respond on claude, Q1" in body
	assert "clarify comment 5986601001, RECOMMENDED fallback, Q2" in body
	assert "filled=1" in stdout


def test_a_later_cycle_updates_the_comment_in_place(tmp_path: Path) -> None:
	first = auto.render([], auto.from_answers(QUESTIONS, WORKER_ANSWER), "clarify comment 1, clarify-respond on claude")
	existing = {"id": 77, "body": first["body"], "author_association": "OWNER", "user": {"login": "owner"}, "created_at": "2026-10-05T00:00:00Z"}
	calls, stdout = _run_record(tmp_path, [existing], "Q1: B\nQ2: B\n", CLARIFY_RESPOND_FALLBACK_READY="true")
	assert calls[0][:4] == ["api", "-X", "PATCH", "repos/owner/repo/issues/comments/77"]
	body = _body(calls[0])
	assert "**AD-3** (clarify comment 5986601001, RECOMMENDED fallback, Q1)" in body
	# The setup item from the first cycle is kept, not duplicated.
	assert body.count("FIXER_APP_TOKEN") == 1
	assert "decider=RECOMMENDED_fallback" in stdout


# --- auto_decisions.py from-answers ----------------------------------------


def test_from_answers_reads_picks_rationale_and_setup() -> None:
	result = auto.from_answers(QUESTIONS, WORKER_ANSWER)
	assert result["answers"] == "Q1: C\nQ2: A\n" and result["undecided"] == []
	first = result["decisions"][0]
	assert first["pick"] == "C"
	assert first["why"] == "Write-profile roles read the same files, so the engine fix covers them too."
	assert [alt["letter"] for alt in first["alternatives"]] == ["A", "B"]
	assert result["setup"] == ["FIXER_APP_TOKEN (secret): a GitHub App token used only by the fixer; until set: verdict-triggered auto-merge is skipped."]
	assert auto.from_answers(QUESTIONS, WORKER_ANSWER.replace("Q1: Write-profile", "**Q1**: Write-profile"))["decisions"][0]["why"] == first["why"]


@pytest.mark.parametrize("setup_heading", ["**SETUP REQUIRED:**", "### SETUP REQUIRED:"])
def test_from_answers_reads_emphasized_section_headings(setup_heading: str) -> None:
	answer = WORKER_ANSWER.replace("RATIONALE:", "**RATIONALE:**").replace("SETUP REQUIRED:", setup_heading)
	result = auto.from_answers(QUESTIONS, answer)
	assert result["decisions"][0]["why"] == "Write-profile roles read the same files, so the engine fix covers them too."
	assert result["setup"] == ["FIXER_APP_TOKEN (secret): a GitHub App token used only by the fixer; until set: verdict-triggered auto-merge is skipped."]


def test_from_answers_handles_strategies_guard_overrides_and_missing_decisions() -> None:
	answer = (
		"DECISIONS:\nQ1: DERIVE_FROM_REPO\n\nRATIONALE:\nQ1: B\n\n"
		"EXECUTION PLAN (required for any non-letter decision):\nQ1:\n  strategy: derive_from_repo\n\n"
		"DATA-PROVISION GUARD OVERRIDES:\n  Q2: overrode A -> B (option requires external data)\n"
	)
	result = auto.from_answers(QUESTIONS, answer)
	assert result["answers"] == "Q1: DERIVE_FROM_REPO\n"
	assert result["decisions"][0]["why"] == "B"
	assert [alt["letter"] for alt in result["decisions"][0]["alternatives"]] == ["A", "B", "C"]
	assert result["undecided"] == ["Q2"] and result["setup"] == []
	# The RECOMMENDED fallback writes bare answer lines with no headings.
	bare = auto.from_answers(QUESTIONS, "Q1: B\nQ2: A\n")
	assert bare["answers"] == "Q1: B\nQ2: A\n"
	assert bare["decisions"][0]["why"] == "Every read-profile role"


def test_complete_answer_is_byte_identical_and_strategies_are_permitted() -> None:
	assert auto.complete_answers(QUESTIONS, WORKER_ANSWER) == {
		"status": "complete", "answers": WORKER_ANSWER, "filled": [],
		"missing": [], "invalid": [], "extra": [], "undecided": [],
	}
	for strategy in ("DERIVE_FROM_REPO", "SYNTHESIZE", "REFRAME", "ESCALATE"):
		answer = f"DECISIONS:\nQ1: {strategy}\nQ2: A\n"
		assert auto.complete_answers(QUESTIONS, answer)["answers"] == answer
	emphasized = "**DECISIONS:**\n**Q1**: **B**\n**Q2**: **A**\n"
	assert auto.complete_answers(QUESTIONS, emphasized)["answers"] == emphasized


def _run_poster(
	tmp_path: Path, comments: list[dict] | None = None, *,
	issue_response: str = '{"state":"open"}', comments_response: str | None = None,
	comments_fail: bool = False, answer: str = "Q1: B\nQ2: A\n", comment_id: str = "1",
	poster_memory_status: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(
		"#!/usr/bin/env python3\n"
		"import json, os, sys\n"
		"args = sys.argv[1:]\n"
		"with open(os.environ['POSTER_GH_LOG'], 'a') as log: log.write(json.dumps(args) + '\\n')\n"
		"if args[:2] == ['api', 'repos/owner/repo/issues/6262']:\n"
		"    print(os.environ['POSTER_ISSUE_RESPONSE'])\n"
		"elif '--slurp' in args:\n"
		"    if os.environ['POSTER_COMMENTS_FAIL'] == 'true': sys.exit(1)\n"
		"    print(os.environ['POSTER_COMMENTS_RESPONSE'])\n"
		"elif args[:2] == ['api', 'repos/owner/repo/issues/6262/comments']:\n"
		"    print('{\"id\":9}')\n"
		"else: sys.exit(1)\n",
		encoding="utf-8",
	)
	gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
	scripts = tmp_path / "scripts"
	scripts.mkdir(exist_ok=True)
	shutil.copy(ROOT / "scripts" / "orchestrate_parse_and_post_answer.sh", scripts)
	log = tmp_path / "gh.log"
	env = _env(
		tmp_path, GITHUB_REPOSITORY="owner/repo", GITHUB_ACTOR="bot",
		CLARIFICATION_COMMENT_ID=comment_id, POSTER_GH_LOG=str(log),
		POSTER_ISSUE_RESPONSE=issue_response,
		POSTER_COMMENTS_RESPONSE=comments_response if comments_response is not None else json.dumps([comments if comments is not None else [
			{"id": 1, "body": QUESTIONS, "author_association": "OWNER", "user": {"login": "owner", "type": "User"}},
		]]),
		POSTER_COMMENTS_FAIL="true" if comments_fail else "false",
		POSTER_MEMORY_STATUS=poster_memory_status,
	)
	Path(env["CODEX_OUTPUT_FILE"]).write_text(answer, encoding="utf-8")
	result = subprocess.run(
		["bash", str(scripts / "orchestrate_parse_and_post_answer.sh")], cwd=tmp_path,
		env=env, capture_output=True, text=True, check=False,
	)
	calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
	return result, calls, (tmp_path / "env").read_text(encoding="utf-8")


def _poster_comment(comment_id: int, body: str, association: str = "OWNER", login: str = "owner", commenter_type: str = "User") -> dict:
	return {"id": comment_id, "body": body, "author_association": association, "user": {"login": login, "type": commenter_type}}


def test_fresh_poster_posts_once(tmp_path: Path) -> None:
	result, calls, env = _run_poster(tmp_path, [_poster_comment(0, "/answer"), _poster_comment(1, QUESTIONS)])
	assert result.returncode == 0, result.stderr
	assert "Posted auto-answer on issue #6262" in result.stdout
	assert "SKIP_AUTO_ANSWER=false" in env
	assert calls[0][:2] == ["api", "repos/owner/repo/issues/6262"]
	assert "--slurp" in calls[1]
	assert len([call for call in calls if call[:2] == ["api", "repos/owner/repo/issues/6262/comments"]]) == 1
	assert calls[2][:2] == ["api", "repos/owner/repo/issues/6262/comments"]


@pytest.mark.parametrize("extra, reason", [
	(_poster_comment(2, "  /AnSwEr Q1: A"), "newer_answer"),
	(_poster_comment(2, "/answer [auto-answered-by-orchestrator]\nQ1: B", "NONE", "github-actions[bot]", "Bot"), "newer_answer"),
	(_poster_comment(2, "<!-- ai:clarification-questions -->\nQ1: ..."), "newer_clarification"),
	(_poster_comment(2, "Clarification required\nQ1: ...", "NONE", "github-actions[bot]", "Bot"), "newer_clarification"),
])
def test_poster_skips_superseded_thread(tmp_path: Path, extra: dict, reason: str) -> None:
	result, calls, env = _run_poster(tmp_path, [_poster_comment(1, QUESTIONS), extra])
	assert result.returncode == 0, result.stderr
	assert f"gate=answer_freshness reason={reason} outcome=skip" in result.stdout
	assert "SKIP_AUTO_ANSWER=true" in env and "LOOP_BLOCKED=false" in env
	assert all(call[:2] != ["api", "repos/owner/repo/issues/6262/comments"] for call in calls)


@pytest.mark.parametrize("extra", [
	_poster_comment(2, "/answer", "NONE", "outsider"),
	_poster_comment(2, "/answer [auto-answered-by-orchestrator]", "NONE", "other[bot]", "Bot"),
	_poster_comment(2, "/answer", "NONE", "github-actions[bot]", "Bot"),
	_poster_comment(2, "/answer", "OWNER", "other[bot]", "Bot"),
	_poster_comment(2, "<!-- ai:clarification-questions -->\nClarification required", "NONE", "other[bot]", "Bot"),
])
def test_untrusted_answer_cannot_suppress_poster(tmp_path: Path, extra: dict) -> None:
	result, calls, env = _run_poster(tmp_path, [_poster_comment(1, QUESTIONS), extra])
	assert result.returncode == 0 and "SKIP_AUTO_ANSWER=false" in env, result.stderr
	assert any(call[:2] == ["api", "repos/owner/repo/issues/6262/comments"] for call in calls)


def test_poster_reads_all_comment_pages(tmp_path: Path) -> None:
	pages = [[_poster_comment(1, QUESTIONS)], [_poster_comment(2, "/answer")]]
	result, calls, env = _run_poster(tmp_path, comments_response=json.dumps(pages))
	assert result.returncode == 0 and "reason=newer_answer" in result.stdout
	assert "SKIP_AUTO_ANSWER=true" in env
	assert all(call[:2] != ["api", "repos/owner/repo/issues/6262/comments"] for call in calls)


def test_failed_recheck_marks_only_that_claim_retryable(tmp_path: Path) -> None:
	scripts = tmp_path / "scripts"
	scripts.mkdir()
	(scripts / "memory_helpers.sh").write_text(
		'memory_ensure_branch() { :; }\n'
		'memory_processed_command_check() {\n'
		'  if [ -f "${POSTER_MEMORY_STATUS}" ]; then\n'
		'    printf \'{"exists":true,"entry":{"status":"%s"}}\\n\' "$(< "${POSTER_MEMORY_STATUS}")"\n'
		'  else printf \'{"exists":false}\\n\'; fi\n'
		'}\n'
		'memory_processed_command_claim() { printf "%s\\n" "$*" >> "${POSTER_MEMORY_STATUS}.log"; printf claimed > "${POSTER_MEMORY_STATUS}"; printf \'{"claimed":true}\\n\'; }\n'
		'memory_clarify_loop_guard() { printf \'{"result":{"blocked":false}}\\n\'; }\n'
		'memory_processed_command_complete() {\n'
		'  printf "%s\\n" "$*" >> "${POSTER_MEMORY_STATUS}.completions"\n'
		'  while [ "$#" -gt 0 ]; do\n'
		'    if [ "$1" = "--status" ]; then printf "%s" "$2" > "${POSTER_MEMORY_STATUS}"; break; fi\n'
		'    shift\n'
		'  done\n'
		'}\n', encoding="utf-8",
	)
	status_file = tmp_path / "poster_memory_status"
	failed, calls, _ = _run_poster(tmp_path, comments_fail=True, poster_memory_status=str(status_file))
	assert failed.returncode == 1 and status_file.read_text() == "recheck_unavailable"
	assert not any(call[:2] == ["api", "repos/owner/repo/issues/6262/comments"] for call in calls)
	retried, calls, _ = _run_poster(tmp_path, poster_memory_status=str(status_file))
	assert retried.returncode == 0 and "Posted auto-answer" in retried.stdout
	assert status_file.read_text() == "answered"
	assert len((tmp_path / "poster_memory_status.log").read_text().splitlines()) == 2
	assert "--retry-on-status recheck_unavailable" in (tmp_path / "poster_memory_status.log").read_text()
	completions = (tmp_path / "poster_memory_status.completions").read_text().splitlines()
	assert [json.loads(line.split("--metadata-json ", 1)[1])["clarify_comment_id"] for line in completions] == [1, 1]


@pytest.mark.parametrize("kwargs, reason, code", [
	({"comments": [_poster_comment(2, "hi")]}, "clarification_comment_missing", 0),
	({"issue_response": '{"state":"closed"}'}, "issue_closed", 0),
	({"issue_response": "not json"}, "recheck_unavailable", 1),
	({"comments_fail": True}, "recheck_unavailable", 1),
	({"comments_response": "{}"}, "recheck_unavailable", 1),
	({"comments_response": "[{\"id\":1}]"}, "recheck_unavailable", 1),
	({"comment_id": "1oops"}, "recheck_unavailable", 1),
])
def test_poster_fails_closed_or_skips_stale_thread(tmp_path: Path, kwargs: dict, reason: str, code: int) -> None:
	result, calls, env = _run_poster(tmp_path, **kwargs)
	assert result.returncode == code, result.stderr
	assert f"gate=answer_freshness reason={reason} outcome=skip" in result.stdout
	assert ("::error::" in result.stdout) == (code == 1)
	assert "SKIP_AUTO_ANSWER=true" in env
	assert all(call[:2] != ["api", "repos/owner/repo/issues/6262/comments"] for call in calls)


def test_escalation_cannot_override_newer_human_answer(tmp_path: Path) -> None:
	result, calls, env = _run_poster(tmp_path, [_poster_comment(1, QUESTIONS), _poster_comment(2, "/answer")], answer="Q1: ESCALATE\n")
	assert result.returncode == 0 and "reason=newer_answer" in result.stdout
	assert "SKIP_AUTO_ANSWER=true" in env
	assert not any(call[0] == "issue" or call[:2] == ["api", "repos/owner/repo/issues/6262/comments"] for call in calls)


def test_already_processed_escalation_does_not_post_or_block(tmp_path: Path) -> None:
	scripts = tmp_path / "scripts"
	scripts.mkdir()
	(scripts / "memory_helpers.sh").write_text(
		'memory_ensure_branch() { :; }\n'
		'memory_processed_command_check() { printf \'{"exists":true,"entry":{"status":"answered"}}\\n\'; }\n',
		encoding="utf-8",
	)
	result, calls, env = _run_poster(tmp_path, answer="Q1: ESCALATE\n")
	assert result.returncode == 0, result.stderr
	assert "reason=already_processed" in result.stdout
	assert "SKIP_AUTO_ANSWER=true" in env
	assert calls == []


def test_escalation_rechecks_immediately_before_comment_and_then_edits_labels(tmp_path: Path) -> None:
	result, calls, env = _run_poster(tmp_path, answer="Q1: ESCALATE\n")
	assert result.returncode == 0 and "SKIP_AUTO_ANSWER=true" in env, result.stderr
	assert len(calls) == 4
	assert calls[0][:2] == ["api", "repos/owner/repo/issues/6262"]
	assert "--slurp" in calls[1]
	assert calls[2][:2] == ["api", "repos/owner/repo/issues/6262/comments"]
	assert any(arg.startswith("body=Autonomous resolution") for arg in calls[2])
	assert calls[3][:3] == ["issue", "edit", "6262"]


@pytest.mark.parametrize("kwargs", [{"comments_fail": True}, {"issue_response": '{"state":"closed"}'}])
def test_escalation_recheck_blocks_both_post_and_label_edit(tmp_path: Path, kwargs: dict) -> None:
	result, calls, env = _run_poster(tmp_path, answer="Q1: ESCALATE\n", **kwargs)
	assert "SKIP_AUTO_ANSWER=true" in env
	assert all(call[0] != "issue" and call[:2] != ["api", "repos/owner/repo/issues/6262/comments"] for call in calls)
	assert result.returncode == (1 if kwargs.get("comments_fail") else 0)


@pytest.mark.parametrize("decision", ["Q1: ESCALATE", "**Q1**: **ESCALATE**", "Q0: ESCALATE"])
def test_complete_escalation_never_posts_an_answer(tmp_path: Path, decision: str) -> None:
	answer = f"DECISIONS:\n{decision}\nQ2: A\n"
	if decision != "Q0: ESCALATE":
		assert auto.complete_answers(QUESTIONS, answer)["answers"] == answer
	result, calls, env = _run_poster(tmp_path, answer=answer)
	assert result.returncode == 0, result.stderr
	assert all(not any(arg.startswith("body=/answer") for arg in call) for call in calls)
	assert "SKIP_AUTO_ANSWER=true" in env
	assert "reason=escalate_requested" in result.stdout


def test_incomplete_answer_keeps_valid_pick_and_removes_stale_rationale() -> None:
	answer = "DECISIONS:\nQ1: C\n\nRATIONALE:\nQ1: keep this\nQ2: stale\n  continuation\n\nSETUP REQUIRED:\n- TOKEN is needed\n"
	result = auto.complete_answers(QUESTIONS, answer)
	assert result["status"] == "filled" and result["filled"] == ["Q2"]
	assert result["missing"] == ["Q2"] and not result["undecided"]
	assert result["answers"].startswith("DECISIONS:\nQ1: C\nQ2: A\n")
	assert "Q1: keep this" in result["answers"] and "Q2: stale" not in result["answers"]
	assert "continuation" not in result["answers"] and "- TOKEN is needed" in result["answers"]
	assert auto.from_answers(QUESTIONS, result["answers"])["answers"] == "Q1: C\nQ2: A\n"
	assert auto.complete_answers(QUESTIONS, result["answers"])["status"] == "complete"


def test_filled_answer_remains_complete_after_data_provision_guard(tmp_path: Path) -> None:
	questions = "**Q1: Choose?**\n- A — go (RECOMMENDED)\n- B — wait\n**Q2: Choose?**\n- A — go (RECOMMENDED)\n- B — wait\n"
	clarification = tmp_path / "questions.txt"
	clarification.write_text(questions, encoding="utf-8")
	answer = tmp_path / "answer.txt"
	answer.write_text(auto.complete_answers(questions, "DECISIONS:\nQ1: B\n")["answers"], encoding="utf-8")
	guarded = subprocess.run(
		[sys.executable, str(ROOT / "scripts" / "clarify_data_provision_guard.py"),
		"--clarification-file", str(clarification), "--answers-file", str(answer)],
		capture_output=True, text=True, check=True,
	)
	assert auto.complete_answers(questions, guarded.stdout)["status"] == "complete"


@pytest.mark.parametrize("bad", ["Q1: Z", "Q1: A+Z", "q1: b", "Q1: B\nQ1: C", "Q1: B+B", "Q1: <!-- #123"])
def test_invalid_picks_are_replaced_with_recommended(bad: str) -> None:
	result = auto.complete_answers(QUESTIONS, bad + "\nQ2: A\n")
	assert result["status"] == "filled" and result["filled"] == ["Q1"]
	assert result["invalid"][0]["qid"] == "Q1"
	assert auto.from_answers(QUESTIONS, result["answers"])["answers"] == "Q1: B\nQ2: A\n"
	assert "<!--" not in result["answers"] and not re.search(r"#\d", result["answers"])


def test_extra_pick_is_removed_and_missing_recommendation_blocks() -> None:
	extra = auto.complete_answers(QUESTIONS, "Q1: B\nQ2: A\nQ9: A\n")
	assert extra["status"] == "filled" and extra["filled"] == [] and extra["extra"] == ["Q9"]
	assert "Q9:" not in extra["answers"]
	no_recommendation = QUESTIONS.replace(" (RECOMMENDED)", "")
	blocked = auto.complete_answers(no_recommendation, "Q1: B\n")
	assert blocked["status"] == "blocked" and blocked["undecided"] == ["Q2"]
	assert auto.complete_answers(QUESTIONS, "ESCALATION:\nQ1: B\n")["status"] == "blocked"
	assert auto.complete_answers("nothing here", "Q1: A\n")["status"] == "unparseable"


def test_complete_cli_and_per_decision_source(tmp_path: Path) -> None:
	questions = tmp_path / "q.txt"
	questions.write_text(QUESTIONS, encoding="utf-8")
	answer = tmp_path / "a.txt"
	answer.write_text("Q1: B\n", encoding="utf-8")
	cmd = [sys.executable, str(AUTO_DECISIONS), "complete", "--questions-file", str(questions), "--answers-file", str(answer)]
	result = subprocess.run(cmd, capture_output=True, text=True, check=False)
	assert result.returncode == 0 and json.loads(result.stdout)["status"] == "filled"
	assert subprocess.run(cmd[:-1] + [str(tmp_path / "missing")], capture_output=True, check=False).returncode == 2
	assert subprocess.run(cmd[:-2], capture_output=True, check=False).returncode == 1
	decisions = auto.from_answers(QUESTIONS, WORKER_ANSWER)
	decisions["decisions"][0]["source"] = "RECOMMENDED fallback"
	body = auto.render([], decisions, "worker")["body"]
	assert "(RECOMMENDED fallback, Q1)" in body and "(worker, Q2)" in body
	decisions["decisions"][0]["source"] = 1
	with pytest.raises(auto.UsageError):
		auto.render([], decisions, "worker")


@pytest.mark.parametrize("section_header", [
	"**RATIONALE:**", "## RATIONALE:", "EXECUTION PLAN (required for any non-letter decision):",
	"ESCALATION (required for any ESCALATE decision):", "SETUP REQUIRED (only when a decision relies on a placeholder):",
	"**DATA-PROVISION GUARD OVERRIDES:**",
])
def test_data_guard_ignores_lettered_lines_outside_decisions(section_header: str) -> None:
	guard_spec = importlib.util.spec_from_file_location("clarify_data_provision_guard", ROOT / "scripts" / "clarify_data_provision_guard.py")
	guard_module = importlib.util.module_from_spec(guard_spec)
	guard_spec.loader.exec_module(guard_module)
	assert guard_module._parse_answers(f"DECISIONS:\nQ1: A\n{section_header}\nQ1: B\n") == {"Q1": ["A"]}


def test_data_guard_rewrites_emphasized_decisions_before_posting(tmp_path: Path) -> None:
	questions = QUESTIONS.replace("A dedicated bot identity; provision its credentials privately", "Provide the PR URL for verification")
	questions = questions.replace("Disable verdict-triggered auto-merge", "Proceed without the URL using available information")
	clarification = tmp_path / "questions.txt"
	clarification.write_text(questions, encoding="utf-8")
	answers = tmp_path / "answer.txt"
	answers.write_text("**DECISIONS:**\n**Q1**: **B**\n**Q2**: **A**\n\n**RATIONALE:**\nQ2: This needs a URL.\n", encoding="utf-8")
	result = subprocess.run(
		[sys.executable, str(ROOT / "scripts" / "clarify_data_provision_guard.py"),
		"--clarification-file", str(clarification), "--answers-file", str(answers)],
		capture_output=True, text=True, check=True,
	)
	assert "Q2: B\n" in result.stdout and "Q2: overrode A -> B" in result.stdout
	assert auto.from_answers(questions, result.stdout)["answers"] == "Q1: B\nQ2: B\n"
	clarification.write_text(questions.replace("Proceed without the URL using available information", "Skip this verification"), encoding="utf-8")
	escalated = subprocess.run(
		[sys.executable, str(ROOT / "scripts" / "clarify_data_provision_guard.py"),
		"--clarification-file", str(clarification), "--answers-file", str(answers)],
		capture_output=True, text=True, check=True,
	)
	assert "Q2: ESCALATE" in escalated.stdout
	assert auto.from_answers(clarification.read_text(encoding="utf-8"), escalated.stdout)["answers"] == "Q1: B\nQ2: ESCALATE\n"


def test_setup_items_cannot_inject_a_comment_delimiter_or_reference_an_issue() -> None:
	answer = "DECISIONS:\nQ1: B\n\nSETUP REQUIRED:\n- TOKEN --> <!-- x --> fixes #12\n"
	body = auto.render([], auto.from_answers(QUESTIONS, answer), "c")["body"]
	assert body.count("<!--") == 1 and body.count("-->") == 1
	section = auto.pr_section([{"id": 1, "body": body, "author_association": "OWNER", "user": {"login": "o"}}])
	assert "### Setup required" in section and "SETUP-1" in section
	assert not re.search(r"#\d", section)


def test_from_answers_cli(tmp_path: Path) -> None:
	questions = tmp_path / "q.txt"
	questions.write_text(QUESTIONS, encoding="utf-8")
	answers = tmp_path / "a.txt"
	answers.write_text(WORKER_ANSWER, encoding="utf-8")
	result = subprocess.run([sys.executable, str(AUTO_DECISIONS), "from-answers", "--questions-file", str(questions), "--answers-file", str(answers)], capture_output=True, text=True, check=False)
	assert result.returncode == 0 and json.loads(result.stdout)["answers"] == "Q1: C\nQ2: A\n"
	missing = subprocess.run([sys.executable, str(AUTO_DECISIONS), "from-answers", "--questions-file", str(questions), "--answers-file", str(tmp_path / "none")], capture_output=True, text=True, check=False)
	assert missing.returncode == 2


# --- Prompt contracts --------------------------------------------------------


@pytest.mark.parametrize("prompt", RESPOND_PROMPTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_worker_prompt_carries_placeholder_and_no_intent_rules(prompt: Path) -> None:
	text = prompt.read_text(encoding="utf-8")
	assert "Placeholder rule (credentials and setup):" in text
	assert "UPPER_SNAKE_CASE secret or repository variable" in text
	assert "A missing credential\n  or setup step is never a reason to ESCALATE" in text
	assert "SETUP REQUIRED (only when a decision relies on a placeholder):" in text
	assert "GITHUB FACTS" in text
	# The release gate's negative clarify fixture must be escalated, never
	# answered: its body is exactly the example the no-intent rule names.
	assert "No-intent rule:" in text and '"Make it better."' in text
	assert "ESCALATE every question that asks what the work should be" in text


def test_release_gate_fixture_still_matches_the_no_intent_example() -> None:
	gate = RELEASE_GATE.read_text(encoding="utf-8")
	assert 'TITLE="[E2E Clarify Negative Test] under-specified task (run ${GITHUB_RUN_ID})"' in gate
	assert 'BODY="Make it better."' in gate


@pytest.mark.parametrize("prompt", CLARIFY_PROMPTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_clarify_prompt_never_blocks_on_credentials(prompt: Path) -> None:
	text = prompt.read_text(encoding="utf-8")
	assert "- Credentials and setup never block." in text
	assert "a private credential, a not-yet-existing commit SHA" not in text
	blocked = text[text.index("- If a required input is the content of an auth-walled"):]
	assert "emit exactly `BLOCKED: <short reason>`" in blocked.split("\n- ", 1)[0]


def test_runtime_clarify_prompt_keeps_placeholder_rule() -> None:
	text = (ROOT / ".github" / "workflows" / "clarify.yml").read_text(encoding="utf-8")
	inline = text.split("<<'PROMPT'", 1)[1].split("\n          PROMPT", 1)[0]
	assert "Credentials and setup never block" in inline
	assert "Missing critical information (credentials," not in inline
	assert "private credential, a not-yet-existing commit SHA" not in inline
	assert "Only when the task depends on the content of an auth-walled or" in inline
