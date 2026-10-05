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


def _run_gate(tmp_path: Path, comment: str, body: str = "Fix it.", state: str = "open", labels: list[str] | None = None, title: str = "[security-audit] finding", **flags: str) -> tuple[dict[str, str], str, str]:
	payload = {"number": 6262, "title": title, "body": body, "state": state, "labels": [{"name": name} for name in (labels or [])]}
	_stub_gh(tmp_path / "bin", tmp_path / "gh.log", {"repos/owner/repo/issues/6262": json.dumps(payload)})
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
	assert names.index("Semantic cache lookup") < names.index("Fetch GitHub facts for the questions") < names.index("Remove git auth before Codex execution")
	assert "python3 scripts/clarify_github_facts.py" in facts["run"]
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
	parse = steps["Parse and post answer"]["if"]
	assert "(steps.run_codex.outcome != 'failure' || steps.standalone_fallback.outputs.ready == 'true')" in parse
	names = list(steps)
	assert names.index("Standalone RECOMMENDED fallback") < names.index("Data-provision guard") < names.index("Parse and post answer") < names.index("Record standalone auto-decisions")


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
