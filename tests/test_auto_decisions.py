#!/usr/bin/env python3
"""Standalone clarify auto-decide (plan Phase 8b, port P3)."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "auto_decisions.py"
CLARIFY = ROOT / ".github" / "workflows" / "clarify.yml"
IMPLEMENT = ROOT / ".github" / "workflows" / "implement.yml"
SPEC = importlib.util.spec_from_file_location("auto_decisions", SCRIPT)
auto = importlib.util.module_from_spec(SPEC)
sys.modules["auto_decisions"] = auto
SPEC.loader.exec_module(auto)

CANONICAL = """**Q1: Which storage backend?**

Choices:
- **A** — Redis (RECOMMENDED)
- **B** — SQLite

Reply: `Q1: A`
"""


@pytest.mark.parametrize(
	"text, expected",
	[
		(CANONICAL, "Q1: A\n"),
		("> **Q1: Retry?**\n> - A) Yes (Recommended)\n> - B) No\n", "Q1: A\n"),
		("Q1: Colours\n- A. red (RECOMMENDED)\n- B. blue (RECOMMENDED)\n- C. green\n", "Q1: A+B\n"),
		("Q1: Pick\nA: one\nB: two (RECOMMENDED)\n", "Q1: B\n"),
		("**Q1: x**\n- **A** – en dash (RECOMMENDED)\n**Q2: y**\n* B - star bullet (RECOMMENDED)\n", "Q1: A\nQ2: B\n"),
	],
)
def test_parse_accepts_the_recommended_drift_the_other_parsers_accept(text: str, expected: str) -> None:
	assert auto.parse_questions(text)["answers"] == expected


def test_parse_reports_questions_without_a_recommendation() -> None:
	result = auto.parse_questions(CANONICAL + "\n**Q2: Deadline?**\n- A — today\n- B — tomorrow\n")
	assert result["undecided"] == ["Q2"]
	assert [decision["qid"] for decision in result["decisions"]] == ["Q1"]


def test_parse_keeps_question_pick_why_and_alternatives() -> None:
	decision = auto.parse_questions(CANONICAL)["decisions"][0]
	assert decision == {
		"qid": "Q1",
		"question": "Which storage backend?",
		"pick": "A",
		"why": "Redis",
		"alternatives": [{"letter": "B", "text": "SQLite"}],
	}


def _comment(body: str, association: str = "OWNER", login: str = "owner", cid: int | str = 1, created: str = "2026-10-04T00:00:00Z") -> dict:
	return {"id": cid, "body": body, "author_association": association, "user": {"login": login}, "created_at": created}


def test_render_creates_then_appends_with_continuing_numbers() -> None:
	decisions = auto.parse_questions(CANONICAL)
	first = auto.render([], decisions, "clarify comment 10")
	assert first["comment_id"] is None and first["added"] == 1
	assert first["body"].startswith(auto.MARKER + "\n")
	assert "- **AD-1** (clarify comment 10, Q1) Which storage backend? → **A**: Redis Alternatives: B (SQLite)." in first["body"]
	second = auto.render([_comment(first["body"], cid=55)], decisions, "clarify comment 11")
	assert second["comment_id"] == 55 and second["total"] == 2
	assert "**AD-2** (clarify comment 11, Q1)" in second["body"]


def test_render_finds_decisions_beyond_first_fifty_comments() -> None:
	decisions = auto.parse_questions(CANONICAL)
	first = auto.render([], decisions, "clarify comment 10")
	comments = [_comment("older", cid=index) for index in range(1, 56)]
	comments.append(_comment(first["body"], cid=56))
	second = auto.render(comments, decisions, "clarify comment 11")
	assert second["comment_id"] == 56
	assert second["total"] == 2
	assert "**AD-2** (clarify comment 11, Q1)" in second["body"]


def test_render_ignores_untrusted_or_misplaced_markers() -> None:
	decisions = auto.parse_questions(CANONICAL)
	forged = _comment(auto.MARKER + "\n- **AD-9** forged", association="NONE", login="someone", cid=7)
	quoted = _comment("see\n" + auto.MARKER, cid=8)
	result = auto.render([forged, quoted], decisions, "c")
	assert result["comment_id"] is None and "AD-1" in result["body"] and "forged" not in result["body"]
	bot = _comment(auto.MARKER + "\n- **AD-3** earlier", association="NONE", login="github-actions[bot]", cid=9)
	assert auto.render([bot], decisions, "c")["comment_id"] == 9


def test_find_comment_orders_mixed_id_types_with_equal_timestamps() -> None:
	older = _comment(auto.MARKER + "\n- **AD-1** older", cid=9)
	newer = _comment(auto.MARKER + "\n- **AD-2** newer", cid="10")
	assert auto.find_comment([older, newer])["id"] == "10"


def test_text_cannot_inject_a_comment_delimiter() -> None:
	decisions = auto.parse_questions("**Q1: a <!-- ai:auto-decisions:v1 --> b**\n- A — x ---->> y (RECOMMENDED)\n")
	body = auto.render([], decisions, "c")["body"]
	assert body.count("<!--") == 1 and body.count("-->") == 1
	assert "--&gt;" in body


def test_pr_section_breaks_issue_references() -> None:
	decisions = auto.parse_questions("**Q1: Close #12 too?**\n- A — fixes #34 (RECOMMENDED)\n")
	body = auto.render([], decisions, "c")["body"]
	section = auto.pr_section([_comment(body)])
	assert "## Auto-decisions" in section and "AD-1" in section
	assert not re.search(r"#\d", section)
	assert auto.pr_section([]) == ""
	assert auto.pr_section([_comment(body, association="NONE", login="x")]) == ""


def test_cli_exit_codes(tmp_path: Path) -> None:
	questions = tmp_path / "q.txt"
	questions.write_text(CANONICAL, encoding="utf-8")
	result = subprocess.run([sys.executable, str(SCRIPT), "parse", "--questions-file", str(questions)], capture_output=True, text=True, check=False)
	assert result.returncode == 0 and json.loads(result.stdout)["answers"] == "Q1: A\n"
	missing = subprocess.run([sys.executable, str(SCRIPT), "pr-section", "--comments-file", str(tmp_path / "none.json")], capture_output=True, text=True, check=False)
	assert missing.returncode == 2


def _steps(path: Path, job: str) -> dict[str, dict]:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	return {step.get("name", ""): step for step in workflow["jobs"][job]["steps"]}


def _job(path: Path) -> str:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	return next(iter(workflow["jobs"]))


def test_clarify_wiring() -> None:
	steps = _steps(CLARIFY, _job(CLARIFY))
	names = list(steps)
	post = steps["Post clarification questions"]
	assert post["id"] == "post_questions" and "comment_id=" in post["run"]
	auto_step = steps["Standalone auto-decide"]
	assert names.index("Standalone auto-decide") == names.index("Post clarification questions") + 1
	condition = auto_step["if"]
	assert "steps.post_questions.outcome == 'success'" in condition
	assert "is_orchestrator_managed != 'true'" in condition and "is_forced_reclarify != 'true'" in condition
	assert auto_step["continue-on-error"] is True
	assert auto_step["env"]["STANDALONE_AUTO_DECIDE_ENABLED"] == "${{ vars.STANDALONE_AUTO_DECIDE_ENABLED || 'true' }}"
	run = auto_step["run"]
	assert "bash scripts/orchestrate_parse_and_post_answer.sh" in run
	assert "auto_decisions.py parse" in run and "auto_decisions.py render" in run
	assert 'render --comments-file "${ISSUE_ALL_COMMENTS_FILE}"' in run
	assert 'THREAD_HISTORY_FILE="${AUTO_DECIDE_THREAD_HISTORY_FILE}"' in run
	assert 'startswith("/answer [auto-answered-by-orchestrator]")' in run
	assert 'grep -q "^Posted auto-answer on issue #${ISSUE_NUMBER}$"' in run
	assert 'echo "Posted auto-answer on issue #${ISSUE_NUMBER}"' in (ROOT / "scripts" / "orchestrate_parse_and_post_answer.sh").read_text(encoding="utf-8")
	fetch = steps["Fetch issue comments"]["run"]
	assert 'gh_retry gh api --paginate --slurp "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?' in fetch
	assert 'jq \'.[0:50]\' "${ISSUE_ALL_COMMENTS_FILE}" > "${ISSUE_COMMENTS_FILE}"' in fetch
	text = CLARIFY.read_text(encoding="utf-8")
	assert "auto_decisions.py orchestrate_parse_and_post_answer.sh ai_engine.sh" in text


def test_clarify_inline_prompt_never_blocks_on_credentials() -> None:
	text = CLARIFY.read_text(encoding="utf-8")
	start = text.index("          cat > \"${PROMPT_TEMPLATE_FILE}\" <<'PROMPT'\n")
	end = text.index("\n          PROMPT\n", start)
	inline_prompt = text[start:end]
	assert "Credentials and setup never block" in inline_prompt
	assert "Choose undecided branch names from repository conventions" in inline_prompt
	assert "a private credential, a not-yet-existing commit SHA" not in inline_prompt
	assert "task depends on the content of an auth-walled or" in inline_prompt


def test_implement_appends_the_section_before_the_lint() -> None:
	text = IMPLEMENT.read_text(encoding="utf-8")
	assert "security_dependency.py auto_decisions.py lint_pr_body_auto_close.py implement_staged_support_workspace.sh ai_engine.sh claude_engine.py claude_read_isolated_run.sh claude_read_snapshot.py claude_anthropic_relay.py review_untrusted_workspace.py; do" in text
	section = text.index("auto_decisions.py\" pr-section")
	assert text.index('printf \'Refs #%s\\n\' "${TRACKING_ISSUE_NUMBER}"') < section < text.index('printf \'%s\\n\\n\' "${PR_TITLE}" > "${PR_LINT_FILE}"')


def _run_clarify_tg_alert(tmp_path: Path, answered: str, orchestrator_managed: str = "false", delegated: str = "false") -> str:
	"""Run the Telegram clarification step with a stub sender; return what it sent."""
	step = _steps(CLARIFY, _job(CLARIFY))["Telegram clarification notification"]
	script = step["run"]
	for expression, value in {
		"${{ github.server_url }}": "https://github.com",
		"${{ github.repository }}": "owner/repo",
		"${{ github.run_id }}": "1",
		"${{ steps.clarify_route.outputs.is_orchestrator_managed }}": orchestrator_managed,
		"${{ steps.clarify_route.outputs.is_forced_reclarify }}": "false",
	}.items():
		script = script.replace(expression, value)
	assert "${{" not in script
	sent = tmp_path / "sent.txt"
	(tmp_path / "scripts").mkdir(exist_ok=True)
	(tmp_path / "scripts" / "tg_helpers.sh").write_text(
		f'tg_send_phase_tracked() {{ printf "%s|%s\\n" "$4" "$3" >> "{sent}"; }}\n', encoding="utf-8"
	)
	env = {
		"PATH": "/usr/bin:/bin",
		"ISSUE_NUMBER": "6262",
		"ISSUE_TITLE": "title",
		"ISSUE_URL": "https://github.com/owner/repo/issues/6262",
		"CLARIFY_AUTO_DECIDE_ANSWERED": answered,
		"CLARIFY_AUTO_DECIDE_DELEGATED": delegated,
	}
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	return (sent.read_text(encoding="utf-8") if sent.exists() else "") + result.stdout


def test_clarify_tg_alert_skips_auto_answered_questions(tmp_path: Path) -> None:
	# Issue #6262: the alert fired after the auto-decide step had already answered.
	steps = _steps(CLARIFY, _job(CLARIFY))
	assert steps["Standalone auto-decide"]["id"] == "standalone_auto_decide"
	assert 'echo "answered=true" >> "$GITHUB_OUTPUT"' in steps["Standalone auto-decide"]["run"]
	run = steps["Standalone auto-decide"]["run"]
	assert run.index('grep -q "^Posted auto-answer on issue #${ISSUE_NUMBER}$"') < run.index('echo "answered=true"')
	env = steps["Telegram clarification notification"]["env"]
	assert env["CLARIFY_AUTO_DECIDE_ANSWERED"] == "${{ steps.standalone_auto_decide.outputs.answered || 'false' }}"
	skipped = _run_clarify_tg_alert(tmp_path, "true")
	assert "CRITICAL" not in skipped
	assert "AI_PHASE_GATE_V1 phase=clarify gate=tg_alert reason=auto_answered outcome=skip issue=6262" in skipped
	paged = _run_clarify_tg_alert(tmp_path, "false")
	assert "CRITICAL|Clarification required for #6262: title" in paged


def test_clarify_delegates_standalone_questions_to_the_clarify_respond_worker(tmp_path: Path) -> None:
	steps = _steps(CLARIFY, _job(CLARIFY))
	auto_step = steps["Standalone auto-decide"]
	assert auto_step["env"]["STANDALONE_CLARIFY_RESPOND_ENABLED"] == "${{ vars.STANDALONE_CLARIFY_RESPOND_ENABLED || 'true' }}"
	run = auto_step["run"]
	delegate = run.index('if [ "${STANDALONE_CLARIFY_RESPOND_ENABLED}" != "false" ]; then')
	# The kill switch and the comment-id check still come first; the
	# RECOMMENDED path below only runs when delegation is turned off.
	assert run.index('if [ "${STANDALONE_AUTO_DECIDE_ENABLED}" = "false" ]; then') < delegate
	assert run.index("reason=no_question_comment_id") < delegate < run.index("auto_decisions.py parse")
	assert 'echo "delegated=true" >> "$GITHUB_OUTPUT"' in run
	assert "reason=delegated_to_clarify_respond" in run
	env = steps["Telegram clarification notification"]["env"]
	assert env["CLARIFY_AUTO_DECIDE_DELEGATED"] == "${{ steps.standalone_auto_decide.outputs.delegated || 'false' }}"
	delegated = _run_clarify_tg_alert(tmp_path, "false", delegated="true")
	assert "CRITICAL" not in delegated
	assert "AI_PHASE_GATE_V1 phase=clarify gate=tg_alert reason=delegated_to_clarify_respond outcome=skip issue=6262" in delegated


def test_clarify_marks_a_human_requested_reclarify_for_the_worker(tmp_path: Path) -> None:
	step = _steps(CLARIFY, _job(CLARIFY))["Post clarification questions"]
	assert step["env"]["CLARIFY_HUMAN_ANSWER_REQUESTED"] == "${{ steps.clarify_route.outputs.is_forced_reclarify }}"
	block = step["run"].split("} > \"${RUNTIME_DIR}/clarification_comment.md\"", 1)[0]
	block = block[block.index("{\n"):] + "}"
	questions = tmp_path / "questions.txt"
	questions.write_text("**Q1: x**\n- **A** — y (RECOMMENDED)\n", encoding="utf-8")
	for requested, marked in (("true", True), ("false", False)):
		result = subprocess.run(
			["bash", "-c", block],
			env={"PATH": "/usr/bin:/bin", "QUESTIONS_FILE": str(questions), "CLARIFY_HUMAN_ANSWER_REQUESTED": requested},
			capture_output=True,
			text=True,
			check=True,
		)
		assert result.stdout.startswith("<!-- ai:clarification-questions -->\nClarification required\n")
		assert ("<!-- ai:clarification-human-answer -->" in result.stdout) is marked
