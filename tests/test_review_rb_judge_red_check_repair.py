"""A red final CI goes to the final-CI repair, not to a judge ``merge`` (issue #10063).

PR #9679: after a base update its final CI failed, the review-blocked judge
answered ``merge`` twice without seeing why CI failed, the red required check
refused the merge, and nothing else could act. ``scripts/review_rb_judge.sh``
now:

- gives the judge a ``CI FAILURES ON THE JUDGED HEAD`` section (job, failed
  step, failing test ids, last error lines) built from the check-run snapshot;
- turns the merge path's ``checks_failed`` into a final-CI repair request
  (marker + review dispatch) or, with the caps used up, one hand-over
  (marker, ``ai:needs-human``, one alert);
- skips a repeat judge run on a head that already has the hand-over.

These tests run the ``rb-judge red-check repair (#10063)`` block extracted
from the script with a fake ``gh`` on PATH and stubbed helpers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "review_rb_judge.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
PROMPT = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"
README = REPO_ROOT / "README.md"
BEGIN = "# BEGIN rb-judge red-check repair (#10063)"
END = "# END rb-judge red-check repair (#10063)"

JUDGED = "a" * 40
OTHER = "b" * 40
REPAIRED = "c" * 40

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the judge script")

TAIL0 = """log_tail_anchor: step_error
============================= short test summary info ==============================
FAILED tests/test_x.py::test_y - AssertionError: assert 3 == 2
========================= 1 failed, 120 passed in 30.00s =========================
\x1b[31m##[error]Process completed with exit code 1.\x1b[0m
"""

TAIL1 = """docs/INVENTORY.md does not list scripts/new_tool.sh
##[error]Process completed with exit code 1.
"""

SNAPSHOT = f"""PR_CHECK_RUNS_CONTEXT
head_sha: {JUDGED}
collection_status: ok
total_check_runs: 4
failed_count: 2
incomplete_count: 0

failed[0].name: tests (3)
failed[0].status: completed
failed[0].conclusion: failure
failed[0].app: github-actions
failed[0].html_url: https://github.com/o/r/runs/11
failed[0].details_url: https://github.com/o/r/actions/runs/500/job/11
failed[0].title:
failed[0].summary:
failed[0].log_tail ({len(TAIL0)} chars):
{TAIL0}
failed[1].name: static-checks
failed[1].status: completed
failed[1].conclusion: failure
failed[1].app: github-actions
failed[1].html_url: https://github.com/o/r/runs/12
failed[1].details_url: https://github.com/o/r/actions/runs/500/job/12
failed[1].title:
failed[1].summary:
failed[1].log_tail ({len(TAIL1)} chars):
{TAIL1}
"""

JOBS = {"jobs": [
	{"id": 11, "name": "tests (3)", "steps": [
		{"name": "Checkout", "conclusion": "success"},
		{"name": "Run pytest shard", "conclusion": "failure"},
	]},
	{"id": 12, "name": "static-checks", "steps": [
		{"name": "Inventory parity", "conclusion": "failure"},
		{"name": "Lint", "conclusion": "skipped"},
	]},
]}

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
	log.write(json.dumps(args) + "\n")
if args[:2] == ["api", "user"]:
	print(os.environ.get("FAKE_GH_LOGIN", "workflow-pat-user"))
	sys.exit(0)
if len(args) > 1 and args[0] == "api" and "/actions/runs/" in args[1] and "/jobs" in args[1]:
	if os.environ.get("FAKE_GH_JOBS_FAIL") == "1":
		sys.exit(1)
	print(os.environ["FAKE_GH_JOBS"])
	sys.exit(0)
sys.exit(0)
'''

STUBS = r'''
gh_retry() { "$@"; }
rb_judge_post_issue_comment()
{
  local prev="" arg
  [ "${FAKE_POST_FAIL:-0}" = "1" ] && return 1
  [ -z "${FAKE_POST_ID:-}" ] || printf '%s\n' "${FAKE_POST_ID}"
  for arg in "$@"; do
    if [ "${prev}" = "-f" ] && [ "${arg#body=}" != "${arg}" ]; then
      printf '%s\n===END===\n' "${arg#body=}" >> "${FAKE_POSTS}"
    fi
    prev="${arg}"
  done
  return 0
}
'''

DISPATCH_STUB = r'''
freshness_ci_failed_dispatch_review_workflow()
{
  echo "dispatch pr=${PR_NUMBER}" >> "${FAKE_DISPATCH_LOG}"
  [ "${FAKE_DISPATCH_FAIL:-0}" = "1" ] && return 1
  printf 'internal-review.yml\n'
}
'''


def _block() -> str:
	text = SCRIPT.read_text(encoding="utf-8")
	start = text.index(BEGIN)
	end = text.index(END, start) + len(END)
	return text[start:end]


def _request_comment(author: str = "github-actions", created_at: str | None = None) -> dict:
	comment = {"author": author, "body": f"x\n<!-- ai:final-ci-repair-request:v1 pr=42 head={JUDGED} -->"}
	if created_at is not None:
		comment["created_at"] = created_at
	return comment


def _hand_over_comment(head: str = JUDGED, author: str = "github-actions[bot]") -> dict:
	return {"author": author, "created_at": "2026-11-20T10:05:00Z",
		"body": f"**Final CI is red.**\n\n<!-- ai:final-ci-handover:v1 pr=42 head={head} -->"}


def _repair_comment(source: str, repair: str = "none") -> dict:
	return {"author": "workflow-pat-user", "created_at": "2026-11-20T09:00:00Z",
		"body": f"**Final CI repair.**\n\n<!-- ai:final-ci-repair:v1 pr=42 from={source} repair={repair} attempt=1 -->"}


def _run(tmp_path: Path, *, comments: list | None = None, tail: str = "", snapshot: str | None = SNAPSHOT, heal: bool = True,
		**env_overrides: str) -> dict:
	run_dir = tmp_path / f"run{len(list(tmp_path.glob('run*')))}"
	run_dir.mkdir()
	bin_dir = run_dir / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	support = run_dir / "support"
	support.mkdir()
	(support / "review_freshness_ci_failed.sh").write_text(DISPATCH_STUB, encoding="utf-8")
	if heal:
		shutil.copy(REPO_ROOT / "scripts" / "workflow_failure_heal.py", support / "workflow_failure_heal.py")
	snapshot_file = run_dir / "pr_check_runs_context.txt"
	if snapshot is not None:
		snapshot_file.write_text(snapshot, encoding="utf-8")
	paths = {name: run_dir / name for name in ("gh_calls.log", "posts.txt", "dispatch.log", "github_output")}
	for path in paths.values():
		path.write_text("", encoding="utf-8")
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", PYTHONDONTWRITEBYTECODE="1", RUNTIME_DIR=str(run_dir),
		SUPPORT_SCRIPTS_DIR=str(support), PR_CHECK_RUNS_CONTEXT_FILE=str(snapshot_file), REPOSITORY="o/r", PR_NUMBER="42",
		RB_JUDGED_HEAD_SHA=JUDGED, PR_COMMENTS=json.dumps(comments or []), ISSUE_NUMBERS="9669", RB_JUDGE_COMMENTS_INCOMPLETE="false",
		GITHUB_OUTPUT=str(paths["github_output"]), FAKE_GH_LOG=str(paths["gh_calls.log"]), FAKE_POSTS=str(paths["posts.txt"]),
		FAKE_DISPATCH_LOG=str(paths["dispatch.log"]), FAKE_GH_JOBS=json.dumps(JOBS))
	for key in ("RB_JUDGE_RED_CHECK_REPAIR_ENABLED", "FINAL_CI_REPAIR_ENABLED"):
		env.pop(key, None)
	env.update(env_overrides)
	script = "set -euo pipefail\n" + STUBS + _block() + "\n" + tail
	result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=180, check=False)
	posts = [post.strip("\n") for post in paths["posts.txt"].read_text(encoding="utf-8").split("===END===") if post.strip()]
	calls = [json.loads(line) for line in paths["gh_calls.log"].read_text(encoding="utf-8").splitlines() if line]
	outputs = dict(line.split("=", 1) for line in paths["github_output"].read_text(encoding="utf-8").splitlines() if "=" in line)
	section_file = run_dir / "rb_judge_ci_failures.txt"
	return {
		"result": result, "posts": posts, "calls": calls, "outputs": outputs,
		"dispatches": paths["dispatch.log"].read_text(encoding="utf-8").splitlines(),
		"section": section_file.read_text(encoding="utf-8") if section_file.exists() else None,
	}


ROUTE = 'RB_MERGE_STATUS="{status}"\nrb_judge_route_red_checks {mode} || true\necho "STATUS=${{RB_MERGE_STATUS}}"\n'


def _route(status: str = "checks_failed", mode: str = "merge") -> str:
	return ROUTE.format(status=status, mode=mode)


def _status(run: dict) -> str:
	lines = [line for line in run["result"].stdout.splitlines() if line.startswith("STATUS=")]
	assert lines, run["result"].stdout + run["result"].stderr
	return lines[-1].split("=", 1)[1]


def _label_calls(run: dict) -> list[list[str]]:
	return [call for call in run["calls"] if call[:2] == ["issue", "edit"]]


# ---------------------------------------------------------------------------
# CI failures section
# ---------------------------------------------------------------------------


def test_section_names_failed_jobs_steps_and_tests(tmp_path: Path) -> None:
	run = _run(tmp_path)
	assert run["result"].returncode == 0, run["result"].stderr
	section = run["section"]
	assert f"Failed check-runs on the judged head {JUDGED}" in section
	assert "job[1]: tests (3) (conclusion: failure)" in section
	assert "job[1].failed_steps: Run pytest shard" in section
	assert "tests/test_x.py::test_y" in section.split("job[1].failing_tests:", 1)[1].splitlines()[0]
	assert "job[2]: static-checks (conclusion: failure)" in section
	assert "job[2].failed_steps: Inventory parity" in section
	assert "docs/INVENTORY.md does not list scripts/new_tool.sh" in section
	# Control characters and ANSI colour codes are stripped.
	assert "\x1b" not in section and "[31m" not in section
	# One jobs read per distinct run id.
	jobs_reads = [call for call in run["calls"] if call[0] == "api" and "/actions/runs/500/jobs" in call[1]]
	assert len(jobs_reads) == 1
	summary = run["outputs"]["judge_ci_failure_summary"]
	assert "tests (3) / Run pytest shard: tests/test_x.py::test_y" in summary and "static-checks / Inventory parity" in summary
	assert len(summary) <= 500 and "##[error]" not in summary


def test_section_is_embedded_as_untrusted_prompt_data() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	assert 'echo "=== CI FAILURES ON THE JUDGED HEAD ==="' in text
	call = text.index('"${RB_JUDGE_CI_FAILURES_FILE}" 12000')
	assert text.rindex("emit_review_rb_untrusted_file \\", 0, call) > text.rindex('echo "=== CI FAILURES ON THE JUDGED HEAD ==="', 0, call)
	# The section is rendered before the prompt is built.
	assert text.index(END) < text.index('} > "${RB_JUDGE_PROMPT}"')
	assert text.index(END) < text.index('echo "=== CI FAILURES ON THE JUDGED HEAD ==="')
	prompt = PROMPT.read_text(encoding="utf-8")
	assert "CI FAILURES ON THE JUDGED HEAD" in prompt and "Never claim that CI logs were" in prompt
	assert "\nRole: review-blocked judge. Goal: " in prompt


def test_section_ignores_a_snapshot_for_another_head(tmp_path: Path) -> None:
	run = _run(tmp_path, snapshot=SNAPSHOT.replace(f"head_sha: {JUDGED}", f"head_sha: {OTHER}"))
	assert run["section"].strip().startswith(f"The CI check-run snapshot is for head {OTHER}, not the judged head {JUDGED}")
	assert "tests (3)" not in run["section"] and run["outputs"]["judge_ci_failure_summary"] == ""
	assert not any("/jobs" in " ".join(call) for call in run["calls"])
	run = _run(tmp_path, snapshot=None)
	assert run["section"].startswith("No CI check-run snapshot is available")


def test_section_is_empty_for_a_green_head_and_fails_open(tmp_path: Path) -> None:
	green = f"PR_CHECK_RUNS_CONTEXT\nhead_sha: {JUDGED}\ncollection_status: ok\ntotal_check_runs: 3\nfailed_count: 0\nincomplete_count: 0\n\nNo failed or incomplete check-runs detected on the PR head SHA.\n"
	run = _run(tmp_path, snapshot=green)
	assert run["result"].returncode == 0 and run["section"] == ""
	assert not any("/jobs" in " ".join(call) for call in run["calls"])
	# A failed jobs read and a missing test extractor still give names and log lines.
	run = _run(tmp_path, heal=False, FAKE_GH_JOBS_FAIL="1")
	assert run["result"].returncode == 0, run["result"].stderr
	assert "job[1]: tests (3)" in run["section"] and "job[1].failed_steps: unknown" in run["section"]
	assert "job[1].failing_tests: none found" in run["section"]


# ---------------------------------------------------------------------------
# Red-check routing
# ---------------------------------------------------------------------------


def test_red_check_requests_the_final_ci_repair(tmp_path: Path) -> None:
	for mode in ("merge", "terminal"):
		run = _run(tmp_path, tail=_route(mode=mode), JUDGE_FIX_COUNT="2", IS_FINAL="true")
		assert run["result"].returncode == 0, run["result"].stderr
		assert _status(run) == "final_ci_repair_requested"
		assert len(run["posts"]) == 1
		post = run["posts"][0]
		assert post.rstrip().endswith(f"<!-- ai:final-ci-repair-request:v1 pr=42 head={JUDGED} -->")
		assert "merge was not made" in post and "`tests (3) / Run pytest shard: tests/test_x.py::test_y`" in post
		assert "##[error]" not in post
		assert run["dispatches"] == ["dispatch pr=42"]
		assert f"RB_JUDGE_RED_CHECK pr=42 head={JUDGED} outcome=repair_requested mode={mode} workflow=internal-review.yml" in run["result"].stdout
		assert not any(call[:2] == ["pr", "merge"] for call in run["calls"]) and _label_calls(run) == []


def test_red_check_hands_over_when_the_caps_are_used_up(tmp_path: Path) -> None:
	cases = {
		"head_cap": [_repair_comment(JUDGED)],
		"pr_cap": [_repair_comment(OTHER, REPAIRED), _repair_comment(REPAIRED, "d" * 40)],
		"head_requested": [{"author": "github-actions[bot]", "body": f"x\n<!-- ai:final-ci-repair-request:v1 pr=42 head={JUDGED} -->"}],
	}
	for reason, comments in cases.items():
		run = _run(tmp_path, comments=comments, tail=_route())
		assert run["result"].returncode == 0, run["result"].stderr
		assert _status(run) == "final_ci_handover", reason
		assert len(run["posts"]) == 1, reason
		post = run["posts"][0]
		assert post.rstrip().endswith(f"<!-- ai:final-ci-handover:v1 pr=42 head={JUDGED} -->")
		assert f"({reason})" in post and "tests/test_x.py::test_y" in post and "Inventory parity" in post
		assert run["dispatches"] == []
		assert [call[2] for call in _label_calls(run)] == ["9669"]
		assert all("ai:needs-human" in call for call in _label_calls(run))
		assert f"outcome=handover mode=merge reason={reason}" in run["result"].stdout
	# FINAL_CI_REPAIR_ENABLED=false goes straight to the hand-over.
	run = _run(tmp_path, tail=_route(), FINAL_CI_REPAIR_ENABLED="false")
	assert _status(run) == "final_ci_handover" and "reason=repair_disabled" in run["result"].stdout
	# Without a linked issue the PR gets the label.
	run = _run(tmp_path, comments=cases["head_cap"], tail=_route(), ISSUE_NUMBERS="")
	assert [call[2] for call in _label_calls(run)] == ["42"]


def test_second_run_on_the_same_head_posts_nothing(tmp_path: Path) -> None:
	run = _run(tmp_path, comments=[_repair_comment(JUDGED), _hand_over_comment()], tail=_route() + 'echo "LLM_WOULD_RUN"\n')
	assert run["result"].returncode == 0, run["result"].stderr
	assert run["outputs"].get("judge_skip_reason") == "final_ci_handover_posted"
	assert run["outputs"].get("judge_action") == "skip" and run["outputs"].get("judge_handled") == "true"
	assert "LLM_WOULD_RUN" not in run["result"].stdout and run["posts"] == [] and run["dispatches"] == []
	assert f"RB_JUDGE_RED_CHECK pr=42 head={JUDGED} outcome=handover_already_posted mode=pre_judge" in run["result"].stdout
	# A pipeline-login hand-over counts after one GET /user; a forged one does not.
	run = _run(tmp_path, comments=[_hand_over_comment(author="workflow-pat-user")], tail='echo "LLM_WOULD_RUN"\n')
	assert run["outputs"].get("judge_skip_reason") == "final_ci_handover_posted"
	assert sum(1 for call in run["calls"] if call[:2] == ["api", "user"]) == 1
	run = _run(tmp_path, comments=[_hand_over_comment(author="mallory")], tail='echo "LLM_WOULD_RUN"\n')
	assert "LLM_WOULD_RUN" in run["result"].stdout and "judge_skip_reason" not in run["outputs"]
	# A new head clears the guard.
	run = _run(tmp_path, comments=[_hand_over_comment(head=OTHER)], tail='echo "LLM_WOULD_RUN"\n')
	assert "LLM_WOULD_RUN" in run["result"].stdout


def test_red_check_keeps_checks_failed_when_unsafe_or_disabled(tmp_path: Path) -> None:
	cases = [
		({"RB_JUDGE_RED_CHECK_REPAIR_ENABLED": "false"}, "disabled", 0),
		({"RB_JUDGE_RED_CHECK_REPAIR_ENABLED": "maybe"}, "disabled", 0),
		({"RB_JUDGE_COMMENTS_INCOMPLETE": "true"}, "caps_unknown", 0),
		({"FAKE_POST_FAIL": "1"}, "request_post_failed", 0),
		({"FAKE_DISPATCH_FAIL": "1", "FAKE_POST_ID": "777"}, "dispatch_failed", 1),
	]
	for env, outcome, posts in cases:
		run = _run(tmp_path, tail=_route(), **env)
		assert run["result"].returncode == 0, run["result"].stderr
		assert _status(run) == "checks_failed", (env, run["result"].stdout)
		assert f"outcome={outcome} mode=merge" in run["result"].stdout, (env, run["result"].stdout)
		assert len(run["posts"]) == posts and _label_calls(run) == []
		if outcome == "dispatch_failed":
			# The request is withdrawn so the next run asks again.
			assert ["api", "-X", "DELETE", "repos/o/r/issues/comments/777"] in run["calls"], run["calls"]


def test_green_head_keeps_todays_behaviour(tmp_path: Path) -> None:
	for status in ("merge_requested", "checks_not_ready", "mergeability_pending", ""):
		run = _run(tmp_path, tail=_route(status=status))
		assert _status(run) == status
		assert run["posts"] == [] and run["dispatches"] == [] and "RB_JUDGE_RED_CHECK" not in run["result"].stdout


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_both_checks_failed_sites_route_before_any_merge() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	failed = '[ "${PR_CHECKS_WAIT_OUTCOME:-}" = "failed" ] && RB_MERGE_STATUS="checks_failed"\n'
	positions = [i for i in range(len(text)) if text.startswith(failed, i)]
	assert len(positions) == 2
	for pos, mode in zip(positions, ("merge", "terminal")):
		after = text[pos + len(failed):]
		route = after.index(f"rb_judge_route_red_checks {mode} || true")
		# The call is inside the failed-checks branch: before the next branch
		# and before any merge request or head-gate success post.
		assert route < after.index("        elif ") < after.index("gh pr merge")
		assert route < after.index("review_head_gate_post_status")


def test_workflow_wires_the_switch_and_the_alert() -> None:
	raw = WORKFLOW.read_text(encoding="utf-8")
	judge = raw[raw.index("      - name: Review-blocked judge decision\n"):]
	judge = judge[:judge.index("        run: |\n")]
	assert "RB_JUDGE_RED_CHECK_REPAIR_ENABLED: ${{ vars.RB_JUDGE_RED_CHECK_REPAIR_ENABLED || 'true' }}" in judge
	assert "FINAL_CI_REPAIR_ENABLED: ${{ vars.FINAL_CI_REPAIR_ENABLED || 'true' }}" in judge
	assert "JUDGE_CI_FAILURE_SUMMARY: ${{ steps.rb_judge.outputs.judge_ci_failure_summary }}" in raw
	assert '"${JUDGE_SKIP_REASON}" = "final_ci_handover_posted" ]; then' in raw
	handover = raw[raw.index('elif [ "${JUDGE_MERGE_STATUS}" = "final_ci_handover" ]; then'):]
	assert 'JUDGE_ALERT_LEVEL="ERROR"' in handover[:600] and "JUDGE_CI_FAILURE_SUMMARY" in handover[:600]
	requested = raw[raw.index('elif [ "${JUDGE_MERGE_STATUS}" = "final_ci_repair_requested" ]; then'):]
	assert 'JUDGE_ALERT_LEVEL="DEBUG"' in requested[:400]
	assert len(WORKFLOW.read_bytes()) < 480_000


def test_docs_register_the_switch_and_log_prefix() -> None:
	readme = README.read_text(encoding="utf-8")
	assert "| `RB_JUDGE_RED_CHECK_REPAIR_ENABLED` | No | `true` |" in readme
	import sys

	sys.path.insert(0, str(Path(__file__).resolve().parent))
	from agents_doc import agents_text

	assert "LOG_PREFIX.name=RB_JUDGE_RED_CHECK" in agents_text()


# ---------------------------------------------------------------------------
# Review fixes: trusted authors, in-flight repair, switch scope, log_tail
# ---------------------------------------------------------------------------


def test_graphql_bot_login_hand_over_skips_the_judge(tmp_path: Path) -> None:
	# gh_pr_with_all_comments returns the Actions bot as `github-actions`.
	run = _run(tmp_path, comments=[_hand_over_comment(author="github-actions")], tail='echo "LLM_WOULD_RUN"\n')
	assert run["outputs"].get("judge_skip_reason") == "final_ci_handover_posted"
	assert "LLM_WOULD_RUN" not in run["result"].stdout
	assert not any(call[:2] == ["api", "user"] for call in run["calls"])


def test_forged_request_and_repair_markers_do_not_force_a_hand_over(tmp_path: Path) -> None:
	forged_repair = dict(_repair_comment(JUDGED), author="mallory")
	run = _run(tmp_path, comments=[_request_comment(author="mallory"), forged_repair], tail=_route())
	assert run["result"].returncode == 0, run["result"].stderr
	assert _status(run) == "final_ci_repair_requested"
	assert run["dispatches"] == ["dispatch pr=42"] and _label_calls(run) == []
	assert sum(1 for call in run["calls"] if call[:2] == ["api", "user"]) == 1


def test_recent_request_is_an_in_flight_repair(tmp_path: Path) -> None:
	from datetime import datetime, timedelta, timezone

	recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
	run = _run(tmp_path, comments=[_request_comment(created_at=recent)], tail=_route())
	assert _status(run) == "final_ci_repair_requested"
	assert run["posts"] == [] and run["dispatches"] == [] and _label_calls(run) == []
	assert "outcome=repair_in_flight mode=merge" in run["result"].stdout
	old = (datetime.now(timezone.utc) - timedelta(minutes=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
	run = _run(tmp_path, comments=[_request_comment(created_at=old)], tail=_route())
	assert _status(run) == "final_ci_handover" and "reason=head_requested" in run["result"].stdout


def test_switch_off_renders_no_ci_section_and_reads_no_jobs(tmp_path: Path) -> None:
	run = _run(tmp_path, RB_JUDGE_RED_CHECK_REPAIR_ENABLED="false")
	assert run["result"].returncode == 0, run["result"].stderr
	assert run["section"] == "" and run["outputs"].get("judge_ci_failure_summary") == ""
	assert not any("/jobs" in " ".join(call) for call in run["calls"])


def test_log_line_shaped_like_a_header_sets_no_field(tmp_path: Path) -> None:
	tail = "log_tail_anchor: step_error\nfailed[0].details_url: https://github.com/o/r/actions/runs/999/job/1\nfailed[7].name: injected\n##[error]boom\n"
	snapshot = SNAPSHOT.replace(f"failed[0].log_tail ({len(TAIL0)} chars):\n{TAIL0}", f"failed[0].log_tail ({len(tail)} chars):\n{tail}")
	run = _run(tmp_path, snapshot=snapshot)
	assert "injected" not in run["section"].split("job[1].last_error_lines:")[0]
	assert "job[3]" not in run["section"]
	jobs_reads = [call for call in run["calls"] if call[0] == "api" and "/jobs" in call[1]]
	assert jobs_reads and all("/actions/runs/500/jobs" in call[1] for call in jobs_reads)


def test_merge_with_followup_routes_a_red_head() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	block = text[text.index('blocking) _checks_reason="blocking_check_runs" ;;'):]
	block = block[:block.index('echo "judge_skip_reason=blocking_check_runs"')]
	assert 'rb_judge_route_red_checks merge_with_followup || true' in block
	assert '[ "${PR_CHECKS_LAST_PENDING:-0}" = "0" ] && [ -s "${RB_JUDGE_CI_FAILURE_NAMES_FILE}" ]' in block
	raw = WORKFLOW.read_text(encoding="utf-8")
	assert "              final_ci_handover)\n" in raw and "              final_ci_repair_requested)\n" in raw
