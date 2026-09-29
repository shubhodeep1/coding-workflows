"""Contract for the intentional skip-AI marker rule and the gate's skip notice (issue #4985).

One rule, three copies: `has_skip_ai_marker` in `.claude/scripts/check_in_status.py`
(also used by `scripts/claude_pr_sweep.py`), and the `SKIP_AI_BODY_AWK` program in
the review gate (`.github/workflows/review_autofix.yml`) and in
`.github/workflows/review_autofix_sweep.yml`. Every copy must answer the same case
table. The gate's end-of-step skip log and its one-comment-per-head notice for
`claude/*` PRs are executed in bash against a fake `gh`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
GATE_WF = ROOT / ".github" / "workflows" / "review_autofix.yml"
SWEEP_WF = ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
_spec = importlib.util.spec_from_file_location("check_in_status", ROOT / ".claude" / "scripts" / "check_in_status.py")
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)

HEAD = "d" * 40
AWK_LINE_RE = re.compile(r"^[ ]*SKIP_AI_BODY_AWK='([^']*)'$", re.MULTILINE)

# (title, body, skipped)
CASES = [
	("Docs [skip ai]", "", True),
	("[skip ai] validation prompt self-heal", "body", True),
	("Phase 1", "[skip ai]", True),
	("Phase 1", "Intro\n\n[skip ai]\n", True),
	("Phase 1", "   [skip ai]  \t", True),
	("Phase 1", "Intro\r\n[skip ai]\r\nmore", True),
	("Phase 1", "```\ncode\n```\n[skip ai]", True),
	("Phase 1", "AD-3: `[skip ai]` plus a head-ref skip", False),
	("Phase 1", "`[skip ai]`", False),
	("Phase 1", "Add [skip ai] to the title to opt out.", False),
	("Phase 1", "```\n[skip ai]\n```", False),
	("Phase 1", "~~~md\n[skip ai]\n~~~", False),
	("Phase 1", "    [skip ai]", False),
	("Phase 1", "\t[skip ai]", False),
	("Phase 1", "- [skip ai]", False),
	("Phase 1", "### [skip ai]", False),
	("Phase 1", "[skip ai] please", False),
	("Phase 1", "[SKIP AI]", False),
	("Phase 1", "", False),
	("", "", False),
]


def _awk_programs():
	programs = {}
	for path in (GATE_WF, SWEEP_WF):
		found = AWK_LINE_RE.findall(path.read_text(encoding="utf-8"))
		assert len(found) == 1, f"{path.name} must define SKIP_AI_BODY_AWK exactly once"
		programs[path.name] = found[0]
	return programs


def test_both_workflows_carry_the_same_awk_program():
	programs = _awk_programs()
	assert len(set(programs.values())) == 1, programs


@pytest.mark.parametrize("title, body, skipped", CASES)
def test_python_rule(title, body, skipped):
	assert checker.has_skip_ai_marker(title, body) is skipped


@pytest.mark.parametrize("title, body, skipped", CASES)
def test_workflow_rule_matches_the_python_rule(title, body, skipped):
	program = next(iter(_awk_programs().values()))
	# The exact condition both workflows use: title glob, then the body through awk.
	script = 'if [[ "$T" == *"[skip ai]"* ]] || printf \'%s\\n\' "$B" | awk "$P"; then echo skip; else echo run; fi'
	result = subprocess.run(["bash", "-c", "set -euo pipefail; " + script], capture_output=True, text=True,
		env={"PATH": os.environ["PATH"], "T": title, "B": body, "P": program}, check=True)
	assert result.stdout.strip() == ("skip" if skipped else "run")


def test_non_string_input_counts_as_empty():
	assert checker.has_skip_ai_marker(None, None) is False
	assert checker.has_skip_ai_marker(5, ["[skip ai]"]) is False


def test_the_old_substring_checks_are_gone():
	gate = GATE_WF.read_text(encoding="utf-8")
	sweep = SWEEP_WF.read_text(encoding="utf-8")
	assert 'printf "%s" "${PR_TITLE} ${PR_BODY}" | grep -Fq "[skip ai]"' not in gate
	assert '[[ "${PR_TITLE}" == *"[skip ai]"* ]]' in gate
	assert '|| printf \'%s\\n\' "${PR_BODY}" | awk "${SKIP_AI_BODY_AWK}"; then' in gate
	assert "grep -Fq '[skip ai]'" not in sweep
	assert '[[ "${pr_title}" == *"[skip ai]"* ]]' in sweep
	assert '|| printf \'%s\\n\' "${pr_body}" | awk "${SKIP_AI_BODY_AWK}"; then' in sweep
	sweep_script = (ROOT / "scripts" / "claude_pr_sweep.py").read_text(encoding="utf-8")
	assert 'if check_in_status.has_skip_ai_marker(pr.get("title"), pr.get("body")):' in sweep_script
	# §6: the reason names stay.
	assert 'SKIP_REASON="skip_ai_marker"' in gate and 'SKIP_REASON="draft_or_skip_ai"' in gate
	assert "reason=skip_ai_marker" in sweep and "reason=skip_ai_marker" in sweep_script


# --- the gate's skip log and notice -----------------------------------------------

def _gate_script() -> str:
	workflow = yaml.safe_load(GATE_WF.read_text(encoding="utf-8"))
	steps = workflow["jobs"]["gate"]["steps"]
	return next(step["run"] for step in steps if step.get("name") == "Evaluate review gate")


def _notice_block() -> str:
	script = _gate_script()
	start = script.index("# Say why (issue #4985")
	end = script.index('echo "should_run=${SHOULD_RUN}" >> "${GITHUB_OUTPUT}"')
	return script[start:end]


def _fetch_helper() -> str:
	script = _gate_script()
	start = script.index('gate_marker_fetch_state="pending"')
	end = script.index("# True (exit 0) when a workflow-issued Claude hand-off")
	return script[start:end]


def test_every_skip_is_logged_before_the_outputs():
	block = _notice_block()
	assert 'if [ "${SHOULD_RUN}" != "true" ]; then\n  echo "AUTOFIX_GATE_SKIP reason=${SKIP_REASON:-unknown} pr=${PR_NUMBER:-none} head_sha=${pr_head_sha_gate:-${PR_HEAD_SHA:-unknown}}"' in block
	script = _gate_script()
	assert script.index("# Say why (issue #4985") > script.index('[ "${CLAUDE_BRANCH_REVIEW}" = "true" ] && [ "${DETERMINISTIC_SKIP}" = "true" ]')


def test_the_notice_marker_is_the_one_the_checker_reads():
	block = _notice_block()
	marker = 'echo "<!-- ai:claude-fixer-review-skipped:v1 reason=${SKIP_REASON} head=${pr_head_sha_gate} -->"'
	assert marker in block
	rendered = f"<!-- ai:claude-fixer-review-skipped:v1 reason=skip_ai_marker head={HEAD} -->"
	assert checker.REVIEW_SKIPPED_MARKER_RE.fullmatch(rendered)
	# The existing gate comment fetch returns it (its filter keeps `<!-- ai:claude-fixer-` comments).
	assert 'contains("<!-- ai:claude-fixer-")' in _fetch_helper()
	assert "exit 1" not in block and "ai:review-skipped\"" not in block


def _fake_gh(tmp_path: Path, comments: list, fail_post: bool = False) -> Path:
	(tmp_path / "comments.json").write_text(json.dumps(comments))
	gh = tmp_path / "bin" / "gh"
	gh.parent.mkdir()
	gh.write_text(textwrap.dedent(f"""\
		#!/usr/bin/env bash
		echo "$*" >> "{tmp_path}/gh_calls.log"
		if [ "$2" = "user" ]; then echo "workflow-bot"; exit 0; fi
		if [ "$2" = "-X" ] && [ "$3" = "POST" ]; then
			{"exit 1" if fail_post else ""}
			while [ $# -gt 0 ]; do if [ "$1" = "--input" ]; then cp "$2" "{tmp_path}/posted.json"; fi; shift; done
			exit 0
		fi
		filter=""
		while [ $# -gt 0 ]; do if [ "$1" = "--jq" ]; then filter="$2"; fi; shift; done
		jq "$filter" "{tmp_path}/comments.json"
		"""))
	gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
	return gh.parent


def _run_block(tmp_path, comments=(), fail_post=False, **env):
	bin_dir = _fake_gh(tmp_path, list(comments), fail_post)
	script = "set -euo pipefail\n" + _fetch_helper() + "\n" + _notice_block() + "\necho GATE_DONE\n"
	base_env = {
		"PATH": f"{bin_dir}:{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path), "REPOSITORY": "o/r",
		"GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": "99", "PR_NUMBER": "7", "PR_IS_DRAFT": "false",
		"SHOULD_RUN": "false", "SKIP_REASON": "skip_ai_marker", "pr_state": "open", "pr_head_sha_gate": HEAD,
		"pr_head_ref": "claude/implement-plan-demo-phase-1",
	}
	base_env.update(env)
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=base_env)
	posted_path = tmp_path / "posted.json"
	posted = json.loads(posted_path.read_text())["body"] if posted_path.exists() else None
	return result, posted


def test_a_marker_skip_on_a_claude_pr_posts_one_notice(tmp_path):
	result, posted = _run_block(tmp_path)
	assert result.returncode == 0 and "GATE_DONE" in result.stdout, result.stderr
	assert f"AUTOFIX_GATE_SKIP reason=skip_ai_marker pr=7 head_sha={HEAD}" in result.stdout
	assert f"AUTOFIX_GATE_SKIP_NOTICE pr=7 head_sha={HEAD} reason=skip_ai_marker posted=true" in result.stdout
	assert posted.startswith("## Review skipped: `skip_ai_marker`\n")
	assert "https://github.com/o/r/actions/runs/99" in posted and "pr_number=7" in posted
	assert posted.rstrip("\n").endswith(f"<!-- ai:claude-fixer-review-skipped:v1 reason=skip_ai_marker head={HEAD} -->")
	assert checker._head_has_review_trace([{"user": {"login": "workflow-bot"}, "body": posted}], HEAD, "workflow-bot")


def test_the_notice_is_posted_once_per_head(tmp_path):
	existing = {"id": 1, "user": {"login": "workflow-bot", "type": "User"}, "author_association": "OWNER",
		"created_at": "t", "body": f"x\n<!-- ai:claude-fixer-review-skipped:v1 reason=skip_ai_marker head={HEAD} -->"}
	result, posted = _run_block(tmp_path, [existing])
	assert result.returncode == 0 and posted is None
	assert "posted=false detail=already_posted" in result.stdout


def test_a_notice_edited_to_crlf_still_counts_as_posted(tmp_path):
	# Review round 1 on #5056: a comment edited in the web UI is stored with
	# CRLF; the jq dedupe must strip `\r` like `_head_has_review_trace` does.
	existing = {"id": 1, "user": {"login": "workflow-bot", "type": "User"}, "author_association": "OWNER",
		"created_at": "t", "body": f"x\r\n<!-- ai:claude-fixer-review-skipped:v1 reason=skip_ai_marker head={HEAD} -->\r\n"}
	assert checker._head_has_review_trace([existing], HEAD, "workflow-bot")
	result, posted = _run_block(tmp_path, [existing])
	assert result.returncode == 0 and posted is None
	assert "posted=false detail=already_posted" in result.stdout


def test_a_notice_by_another_account_does_not_count(tmp_path):
	forged = {"id": 1, "user": {"login": "someone", "type": "User"}, "author_association": "NONE",
		"created_at": "t", "body": f"<!-- ai:claude-fixer-review-skipped:v1 reason=skip_ai_marker head={HEAD} -->"}
	result, posted = _run_block(tmp_path, [forged])
	assert result.returncode == 0 and posted is not None


@pytest.mark.parametrize("env", [
	{"pr_head_ref": "ai/issue-9"},
	{"SKIP_REASON": "draft_or_skip_ai", "PR_IS_DRAFT": "true"},
	{"SKIP_REASON": "pr_closed", "pr_state": "closed"},
	{"SKIP_REASON": "terminal_same_head"},
	{"SKIP_REASON": "deterministic_skip_docs_only"},
])
def test_no_notice_outside_the_two_reasons_on_open_claude_prs(tmp_path, env):
	result, posted = _run_block(tmp_path, **env)
	assert result.returncode == 0 and posted is None
	assert "AUTOFIX_GATE_SKIP reason=" in result.stdout
	assert not (tmp_path / "gh_calls.log").exists()


def test_a_pr_skip_ai_flag_on_a_non_draft_posts_the_notice(tmp_path):
	result, posted = _run_block(tmp_path, SKIP_REASON="draft_or_skip_ai")
	assert result.returncode == 0 and "`pr_skip_ai=true`" in posted


def test_a_failed_post_only_warns(tmp_path):
	result, posted = _run_block(tmp_path, fail_post=True)
	assert result.returncode == 0 and "GATE_DONE" in result.stdout
	assert "::warning::AUTOFIX_GATE_SKIP_NOTICE" in result.stdout and "detail=post_failed" in result.stdout


def test_a_run_that_reviews_logs_nothing(tmp_path):
	result, posted = _run_block(tmp_path, SHOULD_RUN="true", SKIP_REASON="")
	assert result.returncode == 0 and "AUTOFIX_GATE_SKIP" not in result.stdout and posted is None
