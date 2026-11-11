#!/usr/bin/env python3
"""Contract tests: a review-blocked judge crash is not "autofix exhausted"
(issue #9249).

When ``scripts/review_rb_judge.sh`` crashes (``rb_judge_status=failed``) or
reports ``llm_failed*`` / ``json_parse_failed*`` before autofix reached
``MAX_AUTOFIX_ITERATIONS``:

- the "Telegram review-blocked judge decision" step sends a judge-failed
  message with the autofix count and the run URL, never "autofix exhausted";
- the "Post review-blocked comment on PR (autofix exhaustion)" step
  (``scripts/review_autofix_step_post_review_blocked_comment.sh``) appends a
  ``rb-judge-crash:v1`` marker and skips a duplicate POST when a trusted
  comment already carries the same head and reason, so the alert step logs
  ``RB_JUDGE_CRASH_ALERT ... repeat=true`` and sends nothing;
- genuine exhaustion keeps its exact messages, and every read failure fails
  open (comment and alert are sent).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
COMMENT_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_post_review_blocked_comment.sh"
TG_HELPERS = REPO_ROOT / "scripts" / "tg_helpers.sh"

HEAD_A = "a" * 40
HEAD_B = "b" * 40

COMMENT_STEP_NAME = "      - name: Post review-blocked comment on PR (autofix exhaustion)"
ALERT_STEP_NAME = "      - name: Telegram review-blocked judge decision"


def _require_jq() -> None:
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _step(workflow: str, name: str) -> str:
	start = workflow.index(name)
	end = workflow.index("\n      - name: ", start + len(name))
	return workflow[start:end]


def _marker(head: str, reason: str) -> str:
	return f"<!-- rb-judge-crash:v1 head={head} reason={reason} -->"


# ---------------------------------------------------------------------------
# (a) Workflow text
# ---------------------------------------------------------------------------


def test_comment_step_has_id_and_crash_inputs():
	step = _step(_workflow_text(), COMMENT_STEP_NAME)
	assert "        id: rb_blocked_comment\n" in step
	assert "RB_JUDGE_STATUS: ${{ steps.rb_judge.outputs.rb_judge_status }}" in step
	assert "AUTOFIX_ITERATION: ${{ steps.retrigger_guard.outputs.autofix_iteration }}" in step
	assert "review_autofix_step_post_review_blocked_comment.sh" in step


def test_rb_blocked_comment_id_is_unique():
	assert _workflow_text().count("id: rb_blocked_comment\n") == 1


def test_alert_step_receives_crash_inputs():
	step = _step(_workflow_text(), ALERT_STEP_NAME)
	for line in (
		"RB_JUDGE_STATUS: ${{ steps.rb_judge.outputs.rb_judge_status }}",
		"RB_JUDGE_EXIT_CODE: ${{ steps.rb_judge.outputs.rb_judge_exit_code }}",
		"AUTOFIX_ITERATION: ${{ steps.retrigger_guard.outputs.autofix_iteration }}",
		"RB_BLOCKED_REPEAT: ${{ steps.rb_blocked_comment.outputs.rb_judge_crash_repeat }}",
	):
		assert line in step, line
	assert "RB_JUDGE_CRASH_ALERT pr=" in step


def test_rb_judge_emits_exit_code_next_to_failed_status():
	workflow = _workflow_text()
	anchor = 'echo "rb_judge_status=failed" >> "$GITHUB_OUTPUT"\n'
	start = workflow.index(anchor)
	assert 'echo "rb_judge_exit_code=${_judge_exit}" >> "$GITHUB_OUTPUT"' in workflow[start:start + 300]


def test_comment_step_runs_before_alert_step():
	workflow = _workflow_text()
	assert workflow.index(COMMENT_STEP_NAME) < workflow.index(ALERT_STEP_NAME)


def test_exhaustion_messages_unchanged():
	step = _step(_workflow_text(), ALERT_STEP_NAME)
	for text in (
		"PR review blocked — autofix exhausted (%s iterations), judge disabled\\nNeeds human intervention",
		"PR review blocked — autofix exhausted (%s iterations), branch not writable\\nNeeds human intervention",
		"PR review blocked — autofix exhausted (%s iterations), judge LLM failed, deterministic fallback found critical issues\\nNeeds human intervention",
		"PR review blocked — autofix exhausted (%s iterations), judge LLM failed\\nNeeds human intervention",
		"PR review blocked — autofix exhausted (%s iterations)\\nNeeds human intervention",
	):
		assert text in step, text


# ---------------------------------------------------------------------------
# (b) The comment step script
# ---------------------------------------------------------------------------

GH_HELPERS_STUB = r'''
curl_gh_api()
{
	local url="${!#}"
	printf '%s\n' "${url}" >> "${CURL_LOG}"
	[ "${CURL_FAIL:-false}" != "true" ] || return 1
	case "${url}" in
		*/user) cat "${FIX_DIR}/user.json" ;;
		*/comments\?*) cat "${FIX_DIR}/comments.json" ;;
		*/issues/*) cat "${FIX_DIR}/issue.json" ;;
		*) return 1 ;;
	esac
}
'''

GH_STUB = r'''#!/usr/bin/env bash
{ printf '%s' "$*" | tr '\n' '~'; printf '\n'; } >> "${GH_LOG}"
printf '{}\n'
'''


def _run_comment_script(
	env: dict[str, str],
	*,
	comments: list[dict] | None = None,
	curl_fail: bool = False,
	with_tg_helpers: bool = True,
) -> dict:
	"""Source the comment step script with stubbed gh / curl_gh_api; return
	the gh calls, the curl URLs, the step output and stdout/stderr."""
	_require_jq()
	comments = comments or []
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		support = root / "support"
		bindir = root / "bin"
		fix = root / "fix"
		for directory in (support, bindir, fix):
			directory.mkdir()
		(support / "gh_helpers.sh").write_text(GH_HELPERS_STUB, encoding="utf-8")
		if with_tg_helpers:
			shutil.copy(TG_HELPERS, support / "tg_helpers.sh")
		gh = bindir / "gh"
		gh.write_text(GH_STUB, encoding="utf-8")
		gh.chmod(0o755)
		(fix / "user.json").write_text(json.dumps({"login": "Bot"}), encoding="utf-8")
		(fix / "issue.json").write_text(json.dumps({"comments": len(comments)}), encoding="utf-8")
		(fix / "comments.json").write_text(json.dumps(comments), encoding="utf-8")
		gh_log = root / "gh.log"
		curl_log = root / "curl.log"
		output = root / "output"
		output.write_text("", encoding="utf-8")
		run_env = {
			"PATH": f"{bindir}:/usr/bin:/bin",
			"HOME": tmp,
			"SUPPORT_SCRIPTS_DIR": str(support),
			"FIX_DIR": str(fix),
			"GH_LOG": str(gh_log),
			"CURL_LOG": str(curl_log),
			"CURL_FAIL": "true" if curl_fail else "false",
			"GITHUB_OUTPUT": str(output),
			"GITHUB_REPOSITORY": "o/r",
			"GH_TOKEN": "t",
			"AUTOFIX_STEP_REPOSITORY": "o/r",
			"AUTOFIX_STEP_SERVER_URL": "https://github.com",
			"AUTOFIX_STEP_RUN_ID": "99",
			"PR_NUMBER": "7",
			"MAX_AUTOFIX_ITERATIONS": "5",
			"AUTOFIX_ITERATION": "0",
			"AUTOFIX_FAILURE_HEAD_SHA": HEAD_A,
			"JUDGE_SKIP_REASON": "",
			"RB_JUDGE_STATUS": "failed",
		}
		run_env.update(env)
		result = subprocess.run(
			["bash", "-c", f"source {str(COMMENT_SCRIPT)!r}"],
			env=run_env,
			cwd=tmp,
			capture_output=True,
			text=True,
			check=False,
		)
		assert result.returncode == 0, result.stderr
		calls = gh_log.read_text(encoding="utf-8").splitlines() if gh_log.exists() else []
		urls = curl_log.read_text(encoding="utf-8").splitlines() if curl_log.exists() else []
		return {
			"comment_posts": [c for c in calls if "/issues/7/comments" in c],
			"label_posts": [c for c in calls if "/issues/7/labels" in c],
			"urls": urls,
			"output": output.read_text(encoding="utf-8"),
			"stdout": result.stdout,
			"stderr": result.stderr,
		}


def _trusted_comment(body: str, login: str = "bot", user_type: str = "User") -> dict:
	return {"id": 11, "user": {"login": login, "type": user_type}, "body": body}


def test_crash_below_max_posts_marker_and_repeat_false():
	out = _run_comment_script({"AUTOFIX_ITERATION": "0"})
	assert len(out["comment_posts"]) == 1
	assert _marker(HEAD_A, "judge_crashed") in out["comment_posts"][0]
	assert "rb_judge_crash_repeat=false" in out["output"]
	assert any(url.endswith("/issues/7") for url in out["urls"])


def test_trusted_matching_marker_skips_post_and_keeps_label_handover():
	comments = [_trusted_comment("blocked\n\n" + _marker(HEAD_A, "llm_failed"))]
	out = _run_comment_script(
		{"RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "llm_failed", "AUTOFIX_ITERATION": "2"},
		comments=comments,
	)
	assert out["comment_posts"] == []
	assert "rb_judge_crash_repeat=true" in out["output"]
	assert len(out["label_posts"]) == 1 and "ai:needs-human" in out["label_posts"][0]


def test_untrusted_author_marker_still_posts():
	comments = [_trusted_comment(_marker(HEAD_A, "judge_crashed"), login="mallory")]
	out = _run_comment_script({}, comments=comments)
	assert len(out["comment_posts"]) == 1
	assert "rb_judge_crash_repeat=false" in out["output"]


def test_different_head_or_reason_posts():
	for body in (_marker(HEAD_B, "judge_crashed"), _marker(HEAD_A, "llm_failed")):
		out = _run_comment_script({}, comments=[_trusted_comment(body)])
		assert len(out["comment_posts"]) == 1, body
		assert "rb_judge_crash_repeat=false" in out["output"]


def test_read_failure_fails_open():
	out = _run_comment_script({}, curl_fail=True)
	assert len(out["comment_posts"]) == 1
	assert "rb_judge_crash_repeat=false" in out["output"]
	assert "rb-judge-crash dedupe read failed for PR #7; posting" in out["stdout"] + out["stderr"]


def test_missing_tg_helpers_fails_open():
	out = _run_comment_script({}, with_tg_helpers=False)
	assert len(out["comment_posts"]) == 1
	assert _marker(HEAD_A, "judge_crashed") in out["comment_posts"][0]
	assert "rb_judge_crash_repeat=false" in out["output"]
	assert "::warning::rb-judge-crash dedupe unavailable" in out["stdout"]


def test_non_crash_runs_add_no_marker_and_no_read():
	for env in (
		{"AUTOFIX_ITERATION": "5"},
		{"AUTOFIX_ITERATION": "7", "RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "llm_failed"},
		{"AUTOFIX_ITERATION": "", "RB_JUDGE_STATUS": "failed"},
		{"RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "merge_conflict"},
		{"AUTOFIX_FAILURE_HEAD_SHA": "not-a-sha"},
	):
		out = _run_comment_script(env, comments=[_trusted_comment(_marker(HEAD_A, "judge_crashed"))])
		assert len(out["comment_posts"]) == 1, env
		assert "rb-judge-crash:v1" not in out["comment_posts"][0], env
		assert out["urls"] == [], env
		assert "rb_judge_crash_repeat=false" in out["output"], env


# ---------------------------------------------------------------------------
# (c) The alert step body
# ---------------------------------------------------------------------------


def _judge_alert(env: dict[str, str]) -> tuple[list[tuple[str, str]], str]:
	"""Run the "Telegram review-blocked judge decision" step body; return
	the (level, message) sends and stdout."""
	step = _step(_workflow_text(), ALERT_STEP_NAME)
	body = step[step.index("        run: |\n") + len("        run: |\n"):]
	body = "\n".join(line[10:] for line in body.splitlines())
	body = re.sub(r"\$\{\{[^}]*\}\}", "x", body)
	with tempfile.TemporaryDirectory() as tmp:
		log = Path(tmp) / "tg.log"
		(Path(tmp) / "tg_helpers.sh").write_text(
			'tg_send_tracked() { printf \'%s\\t%s\\n\' "$3" "${2//$\'\\n\'/ }" >> "${TG_LOG}"; }\n', encoding="utf-8"
		)
		run_env = {
			"PATH": "/usr/bin:/bin",
			"SUPPORT_SCRIPTS_DIR": tmp,
			"TG_LOG": str(log),
			"PR_NUMBER": "7",
			"MAX_AUTOFIX_ITERATIONS": "5",
			"AUTOFIX_FAILURE_HEAD_SHA": HEAD_A,
			"JUDGE_HANDLED": "",
			"JUDGE_ACTION": "",
			"JUDGE_SKIP_REASON": "",
		}
		run_env.update(env)
		result = subprocess.run(["bash", "-c", body], env=run_env, capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
		sends = []
		if log.exists():
			for line in log.read_text(encoding="utf-8").splitlines():
				level, message = line.split("\t", 1)
				sends.append((level, message))
		return sends, result.stdout


def test_alert_crash_sends_judge_failed_text():
	sends, stdout = _judge_alert({"RB_JUDGE_STATUS": "failed", "RB_JUDGE_EXIT_CODE": "1", "AUTOFIX_ITERATION": "0"})
	assert len(sends) == 1
	level, message = sends[0]
	assert level == "CRITICAL"
	assert "review-blocked judge failed (exit 1; 0 autofix commits, max 5)" in message
	assert "autofix exhausted" not in message
	assert "Run: " in message
	assert f"RB_JUDGE_CRASH_ALERT pr=7 head={HEAD_A} reason=judge_crashed repeat=false level=CRITICAL" in stdout


def test_alert_llm_failed_below_max_sends_crash_text():
	sends, stdout = _judge_alert({"RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "llm_failed", "AUTOFIX_ITERATION": "3"})
	assert len(sends) == 1 and sends[0][0] == "CRITICAL"
	assert "review-blocked judge failed (llm_failed; 3 autofix commits, max 5)" in sends[0][1]
	assert "autofix exhausted" not in sends[0][1]
	assert "reason=llm_failed repeat=false" in stdout


def test_alert_repeat_sends_nothing_and_logs():
	sends, stdout = _judge_alert({"RB_JUDGE_STATUS": "failed", "RB_JUDGE_EXIT_CODE": "1", "AUTOFIX_ITERATION": "0", "RB_BLOCKED_REPEAT": "true"})
	assert sends == []
	assert f"RB_JUDGE_CRASH_ALERT pr=7 head={HEAD_A} reason=judge_crashed repeat=true level=SUPPRESSED" in stdout


def test_alert_exhaustion_text_unchanged():
	for env in (
		{"RB_JUDGE_STATUS": "failed", "AUTOFIX_ITERATION": "5"},
		{"RB_JUDGE_STATUS": "ok", "AUTOFIX_ITERATION": "6"},
		{"RB_JUDGE_STATUS": "failed", "AUTOFIX_ITERATION": ""},
	):
		sends, stdout = _judge_alert(env)
		assert len(sends) == 1 and sends[0][0] == "CRITICAL", env
		assert sends[0][1].startswith("PR review blocked — autofix exhausted (5 iterations) Needs human intervention"), env
		assert "RB_JUDGE_CRASH_ALERT" not in stdout, env
	sends, _ = _judge_alert({"RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "llm_failed", "AUTOFIX_ITERATION": "5"})
	assert "autofix exhausted (5 iterations), judge LLM failed" in sends[0][1]


def test_alert_repeat_flag_never_silences_non_crash():
	sends, _ = _judge_alert({"RB_JUDGE_STATUS": "ok", "AUTOFIX_ITERATION": "5", "RB_BLOCKED_REPEAT": "true"})
	assert len(sends) == 1 and "autofix exhausted" in sends[0][1]
	sends, _ = _judge_alert({"RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "sync_merge_failed", "AUTOFIX_ITERATION": "1", "RB_BLOCKED_REPEAT": "true"})
	assert len(sends) == 1 and sends[0][0] == "CRITICAL"


def test_alert_handled_judge_is_not_a_crash():
	sends, stdout = _judge_alert({"JUDGE_HANDLED": "true", "JUDGE_ACTION": "fix", "RB_JUDGE_STATUS": "failed", "AUTOFIX_ITERATION": "0", "RB_BLOCKED_REPEAT": "true"})
	assert sends == [("DEBUG", sends[0][1])] and "pushed fix commit" in sends[0][1]
	assert "RB_JUDGE_CRASH_ALERT" not in stdout


# ---------------------------------------------------------------------------
# (d) Parity between the comment script and the alert step
# ---------------------------------------------------------------------------


def test_crash_condition_parity():
	cases = (
		{"RB_JUDGE_STATUS": "failed", "JUDGE_SKIP_REASON": "", "AUTOFIX_ITERATION": "0"},
		{"RB_JUDGE_STATUS": "failed", "JUDGE_SKIP_REASON": "llm_failed", "AUTOFIX_ITERATION": "4"},
		{"RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "llm_failed_critical_issues", "AUTOFIX_ITERATION": "1"},
		{"RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "json_parse_failed", "AUTOFIX_ITERATION": "2"},
		{"RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "llm_failed-X", "AUTOFIX_ITERATION": "2"},
		{"RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "llm_failed", "AUTOFIX_ITERATION": "5"},
		{"RB_JUDGE_STATUS": "failed", "JUDGE_SKIP_REASON": "", "AUTOFIX_ITERATION": "x"},
		{"RB_JUDGE_STATUS": "ok", "JUDGE_SKIP_REASON": "disabled", "AUTOFIX_ITERATION": "0"},
	)
	for env in cases:
		comment = _run_comment_script(env)
		match = re.search(r"rb-judge-crash:v1 head=[0-9a-f]{40} reason=([a-z0-9_]+) -->", comment["comment_posts"][0])
		comment_reason = match.group(1) if match else None
		_, stdout = _judge_alert(env)
		alert_match = re.search(r"RB_JUDGE_CRASH_ALERT .* reason=(\S+) repeat=", stdout)
		alert_reason = alert_match.group(1) if alert_match else None
		assert comment_reason == alert_reason, (env, comment_reason, alert_reason)


if __name__ == "__main__":
	import pytest

	raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
