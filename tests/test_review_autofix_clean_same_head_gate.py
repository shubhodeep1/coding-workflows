"""Clean same-head skip in the review gate (issue #8952).

A Claude pool audit (2026-11-05..07) found seven e2e smoke PRs re-reviewed
68-113 times each on unchanged heads: every round ended with "Change status:
not-edited", and no head-keyed marker recorded that the head was already
reviewed clean. The editor-summary step now ends a clean summary with a
``review-autofix-clean:v1`` marker, and the gate skips another full review of
the same head, base and review-support SHA.

These tests pin:
  1. the wiring in ``.github/workflows/review_autofix.yml`` and the two step
     scripts;
  2. the embedded Python parser, run directly against comment fixtures;
  3. the whole gate step, run with a fake ``gh`` on PATH;
  4. the marker writer, run with a fake ``gh`` shell function.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import expanded_review_autofix_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
GATE_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_evaluate_gate.sh"
SUMMARY_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_post_editor_summary.sh"
README = REPO_ROOT / "README.md"
FRAGMENT = REPO_ROOT / "agents.d" / "8952-clean-same-head-skip.md"

HEAD = "1" * 40
OTHER_HEAD = "2" * 40
BASE = "3" * 40
OTHER_BASE = "4" * 40
SUPPORT = "5" * 40
OTHER_SUPPORT = "6" * 40
PIPELINE = "workflow-pat-user"
MARKER_TIME = "2026-11-06T10:00:00Z"


def _clean_marker(head: str = HEAD, base: str = BASE, support: str = SUPPORT, run: str = "777") -> str:
	return f"<!-- review-autofix-clean:v1 head={head} base={base} support={support} run={run} -->"


def _summary_comment(marker: str | None = None, *, comment_id: int = 10, author: str = PIPELINE, created_at: str = MARKER_TIME, body: str | None = None) -> dict[str, object]:
	if body is None:
		body = "AI autofix editor summary\n\nChange status:\n- not-edited\n\n" + (marker if marker is not None else _clean_marker())
	return {"id": comment_id, "author_login": author, "author_type": "User", "created_at": created_at, "body": body}


def _parser_script() -> str:
	lines = GATE_SCRIPT.read_text(encoding="utf-8").splitlines()
	for idx, line in enumerate(lines):
		if "clean_decision=" in line and "python3 - <<'PY'" in line:
			body: list[str] = []
			for inner in lines[idx + 1:]:
				if inner.strip() == "PY":
					return textwrap.dedent("\n".join(body))
				body.append(inner)
	raise AssertionError("clean same-head parser heredoc not found")


def _run_parser(tmp_path: Path, comments: object, *, head: str = HEAD, base: str = BASE, support: str = SUPPORT) -> tuple[int, dict[str, str]]:
	comments_file = tmp_path / "comments.json"
	comments_file.write_text(comments if isinstance(comments, str) else json.dumps(comments), encoding="utf-8")
	env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CLEAN_MARKER_COMMENTS_FILE=str(comments_file), GATE_HEAD_SHA=head,
		GATE_BASE_SHA=base, GATE_SUPPORT_SHA=support, GATE_PR_NUMBER="42", GATE_MARKER_AUTHOR_LOGIN=PIPELINE)
	result = subprocess.run([sys.executable, "-"], input=_parser_script(), env=env, text=True, capture_output=True, check=False)
	return result.returncode, dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _step_text(text: str, name: str) -> str:
	start = text.index(f"- name: {name}\n")
	end = text.find("\n      - name: ", start + 1)
	return text[start:end if end > 0 else len(text)]


def test_workflow_wires_the_switch_base_sha_and_writer_env() -> None:
	raw = WORKFLOW.read_text(encoding="utf-8")
	gate_job = raw[raw.index("\n  gate:\n"):raw.index("\n    steps:\n", raw.index("\n  gate:\n"))]
	assert "      base_sha: ${{ steps.evaluate.outputs.base_sha }}\n" in gate_job
	evaluate = _step_text(raw, "Evaluate review gate")
	assert "          AUTOFIX_SKIP_CLEAN_SAME_HEAD: ${{ vars.AUTOFIX_SKIP_CLEAN_SAME_HEAD }}\n" in evaluate
	terminal = evaluate.index("AUTOFIX_SKIP_TERMINAL_SAME_HEAD: ${{ vars.AUTOFIX_SKIP_TERMINAL_SAME_HEAD }}")
	assert evaluate.index("AUTOFIX_SKIP_CLEAN_SAME_HEAD: ${{ vars.AUTOFIX_SKIP_CLEAN_SAME_HEAD }}") - terminal < 400
	summary = _step_text(raw, "Post editor summary comment")
	assert "          AUTOFIX_CLEAN_MARKER_BASE_SHA: ${{ needs.gate.outputs.base_sha }}\n" in summary
	# The editor summary's head and support SHAs come from the codex-agent job env.
	assert "      AUTOFIX_FAILURE_HEAD_SHA: ${{ needs.gate.outputs.head_sha }}\n" in raw
	assert len(WORKFLOW.read_bytes()) < 480_000
	# The expanded workflow (step scripts inlined) still carries the gate body.
	assert "# ----- Clean same-head skip (issue #8952) -----" in expanded_review_autofix_text()


def test_gate_script_wiring() -> None:
	text = GATE_SCRIPT.read_text(encoding="utf-8")
	assert 'base_ref: (.base.ref // ""), base_sha: (.base.sha // ""), head_repo:' in text
	assert '"${AUTOFIX_SKIP_CLEAN_SAME_HEAD:-true}" = "false"' in text
	assert 'echo "base_sha=${pr_base_sha_gate}" >> "${GITHUB_OUTPUT}"' in text
	assert 'or ($b | contains("<!-- review-autofix-clean:v1 ")) or ($b | contains("<!-- ai:single-issue-security-pass:v1 ")) or ($b | contains("<!-- ai:freshness-ci-failed:v1 ")) or ($b | contains("<!-- ai:final-ci-failed:v1 ")) or ($b | contains("<!-- ai:final-ci-repair:v1 "))) |' in text
	assert 'body: "", activity_only: true}))]' in text
	# Order: terminal same-head skip, clean same-head skip, fingerprint cap.
	block_start = text.index("# ----- Clean same-head skip (issue #8952) -----")
	block_end = text.index("# ----- Identical-failure fingerprint cap -----")
	assert text.index('SKIP_REASON="terminal_same_head"') < block_start < block_end
	block = text[block_start:block_end]
	# force-review does not bypass the clean skip (issue #8952, Q8: A).
	code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
	assert "force-review" not in code and "pr_labels" not in code and "PR_TITLE" not in code
	assert 'echo "AUTOFIX_GATE_SKIP reason=clean_same_head pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} marker_run=${clean_marker_run} marker_comment_id=${clean_marker_comment_id}"' in block
	assert 'SKIP_REASON="clean_same_head"' in block and 'GATE_SKIP_RECORDED="true"' in block
	# The reviews read runs only for a candidate.
	reviews = block.index("/pulls/${PR_NUMBER}/reviews")
	assert block.rindex('if [ "${clean_outcome}" = "candidate" ]; then', 0, reviews) > 0
	assert code.count("/reviews") == 1
	# The retarget clears the base SHA.
	retarget = text[text.index('pr_base_retargeted="true"'):]
	assert 'pr_base_sha_gate=""' in retarget[:400]


def test_summary_script_appends_the_marker_last() -> None:
	text = SUMMARY_SCRIPT.read_text(encoding="utf-8")
	assert '-f body="$(cat "${PR_EDITOR_COMMENT_FILE}")${sandbox_excluded_marker_suffix}${clean_marker_suffix}"' in text
	assert "AUTOFIX_CLEAN_MARKER_BASE_SHA:-" in text


def test_docs_register_the_switch_and_log_prefixes() -> None:
	readme = README.read_text(encoding="utf-8")
	assert re.search(r"^\| `AUTOFIX_SKIP_CLEAN_SAME_HEAD` \| No \| `true` \| review_autofix \|", readme, re.MULTILINE)
	# Read the rendered agents text so the check survives the release fold,
	# which moves the fragment into agents.md and deletes it.
	sys.path.insert(0, str(Path(__file__).resolve().parent))
	from agents_doc import agents_text

	fragment = agents_text()
	for prefix in ("AUTOFIX_GATE_CLEAN_SAME_HEAD_CHECK", "AUTOFIX_CLEAN_SAME_HEAD_MARKER"):
		assert f"LOG_PREFIX.name={prefix}" in fragment


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_returns_candidate_for_a_matching_trusted_marker(tmp_path: Path) -> None:
	rc, decision = _run_parser(tmp_path, [_summary_comment()])
	assert rc == 0
	assert decision["decision"] == "candidate" and decision["reason"] == "none"
	assert decision["marker_run"] == "777" and decision["marker_comment_id"] == "10"
	assert decision["marker_created_at"] == MARKER_TIME and decision["matching_markers"] == "1"


def test_parser_support_none_matches_only_an_empty_support_sha(tmp_path: Path) -> None:
	comments = [_summary_comment(_clean_marker(support="none"))]
	assert _run_parser(tmp_path, comments, support="")[1]["decision"] == "candidate"
	assert _run_parser(tmp_path, comments)[1]["reason"] == "support_changed"
	assert _run_parser(tmp_path, comments, support="garbage")[1]["reason"] == "support_changed"


def test_parser_no_skip_reasons(tmp_path: Path) -> None:
	human = {"id": 11, "author_login": "alice", "author_type": "User", "created_at": "2026-11-06T11:00:00Z", "body": "", "activity_only": True}
	cases = {
		"no_marker": ([_summary_comment(_clean_marker(head=OTHER_HEAD))], {}),
		"base_moved": ([_summary_comment()], {"base": OTHER_BASE}),
		"support_changed": ([_summary_comment()], {"support": OTHER_SUPPORT}),
		"freshness_ci_failed": ([
			{"id": 5, "author_login": PIPELINE, "author_type": "User", "created_at": "2026-11-05T00:00:00Z",
				"body": f"CI failed\n<!-- ai:freshness-ci-failed:v1 pr=42 head={HEAD} -->"},
			_summary_comment()], {}),
		"newer_failure_marker": ([_summary_comment(), {"id": 12, "author_login": PIPELINE, "author_type": "User",
			"created_at": "2026-11-06T12:00:00Z", "body": "**AI review/autofix encountered a post-editor failure**"}], {}),
		"newer_human_comment": ([_summary_comment(), human], {}),
	}
	for reason, (comments, overrides) in cases.items():
		rc, decision = _run_parser(tmp_path, comments, **overrides)
		assert rc == 0, reason
		assert decision["decision"] == "no_skip" and decision["reason"] == reason, (reason, decision)


def test_parser_partial_and_failure_markers_newer_than_the_marker_rearm(tmp_path: Path) -> None:
	for body in (f"x\n<!-- review-autofix-failure:v1 head={HEAD} reason=r fp={'a' * 64} degraded=0 run=9 -->",
			f"<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->\n```text\nhead_sha={HEAD}\n```"):
		comments = [_summary_comment(), {"id": 13, "author_login": PIPELINE, "author_type": "User", "created_at": "2026-11-06T13:00:00Z", "body": body}]
		assert _run_parser(tmp_path, comments)[1]["reason"] == "newer_failure_marker"
	# An older failure is superseded by the clean marker.
	comments = [{"id": 3, "author_login": PIPELINE, "author_type": "User", "created_at": "2026-11-05T00:00:00Z", "body": "**AI review/autofix failed**"}, _summary_comment()]
	assert _run_parser(tmp_path, comments)[1]["decision"] == "candidate"


def test_parser_newer_security_pass_result_rearms(tmp_path: Path) -> None:
	for status in ("clean", "failed", "findings"):
		comments = [_summary_comment(), {"id": 14, "author_login": PIPELINE, "author_type": "User", "created_at": "2026-11-06T13:00:00Z",
			"body": f"Single-issue security pass: {status}\n\n<!-- ai:single-issue-security-pass:v1 status={status} head={HEAD} cycle=1 -->"}]
		assert _run_parser(tmp_path, comments)[1]["reason"] == "newer_security_pass_result", status
	# A pending marker (posted by the same run), another head or an older result does not re-arm.
	for body, created_at in ((f"x\n<!-- ai:single-issue-security-pass:v1 status=pending head={HEAD} cycle=1 -->", "2026-11-06T13:00:00Z"),
			(f"x\n<!-- ai:single-issue-security-pass:v1 status=clean head={OTHER_HEAD} cycle=1 -->", "2026-11-06T13:00:00Z"),
			(f"x\n<!-- ai:single-issue-security-pass:v1 status=clean head={HEAD} cycle=1 -->", "2026-11-05T13:00:00Z")):
		comments = [_summary_comment(), {"id": 15, "author_login": PIPELINE, "author_type": "User", "created_at": created_at, "body": body}]
		assert _run_parser(tmp_path, comments)[1]["decision"] == "candidate", body


def test_parser_missing_comment_time_fails_open(tmp_path: Path) -> None:
	human = {"id": 16, "author_login": "alice", "author_type": "User", "body": "", "activity_only": True}
	for created_at in (None, "", "2026-11"):
		record = dict(human)
		if created_at is not None:
			record["created_at"] = created_at
		decision = _run_parser(tmp_path, [_summary_comment(), record])[1]
		assert decision["decision"] == "no_skip" and decision["reason"] == "comment_time_unavailable", created_at


def test_parser_ignores_newer_bot_and_pipeline_activity(tmp_path: Path) -> None:
	comments = [
		_summary_comment(),
		{"id": 20, "author_login": "github-actions[bot]", "author_type": "Bot", "created_at": "2026-11-06T12:00:00Z", "body": "AI review/autofix failed"},
		{"id": 21, "author_login": PIPELINE.upper(), "author_type": "User", "created_at": "2026-11-06T12:00:00Z", "body": "", "activity_only": True},
	]
	assert _run_parser(tmp_path, comments)[1]["decision"] == "candidate"


def test_parser_rejects_untrusted_malformed_and_misplaced_markers(tmp_path: Path) -> None:
	rc, decision = _run_parser(tmp_path, [_summary_comment(author="mallory")])
	assert decision["reason"] == "no_marker" and decision["untrusted_markers"] == "1"
	bad_bodies = [
		"AI autofix editor summary\n" + _clean_marker() + "\nmore text after the marker",
		"AI autofix editor summary\n" + _clean_marker(run="abc"),
		"AI autofix editor summary\n" + _clean_marker().replace("head=", "head=X"),
		"Some other comment\n" + _clean_marker(),
	]
	for body in bad_bodies:
		rc, decision = _run_parser(tmp_path, [_summary_comment(body=body)])
		assert rc == 0 and decision["decision"] == "no_skip" and decision["reason"] == "no_marker", body
		assert decision["malformed_markers"] == "1", body


def test_parser_uses_the_newest_marker_for_the_head(tmp_path: Path) -> None:
	comments = [
		_summary_comment(_clean_marker(base=OTHER_BASE, run="1"), comment_id=1, created_at="2026-11-05T00:00:00Z"),
		_summary_comment(_clean_marker(run="2"), comment_id=2, created_at="2026-11-06T00:00:00Z"),
	]
	decision = _run_parser(tmp_path, comments)[1]
	assert decision["decision"] == "candidate" and decision["marker_run"] == "2"


def test_parser_exits_2_on_garbage(tmp_path: Path) -> None:
	assert _run_parser(tmp_path, "not json")[0] == 2
	assert _run_parser(tmp_path, {"not": "a list"})[0] == 2


def _final_ci_comment(*, created_at: str, author: str = "github-actions[bot]", author_type: str = "Bot", head: str = HEAD, pr: str = "42", comment_id: int = 30) -> dict[str, object]:
	return {"id": comment_id, "author_login": author, "author_type": author_type, "created_at": created_at,
		"body": f"**The final CI run failed on head `{head[:12]}`.**\n\n<!-- ai:final-ci-failed:v1 pr={pr} head={head} -->"}


def test_parser_final_ci_failed_marker_at_or_after_the_clean_marker_rearms(tmp_path: Path) -> None:
	# Final-CI hand-back (final-ci-failure-ci-fix plan, Phase 1).
	for created_at in (MARKER_TIME, "2026-11-06T12:00:00Z"):
		for author, author_type in (("github-actions[bot]", "Bot"), (PIPELINE, "User")):
			comments = [_summary_comment(), _final_ci_comment(created_at=created_at, author=author, author_type=author_type)]
			decision = _run_parser(tmp_path, comments)[1]
			assert decision["decision"] == "no_skip" and decision["reason"] == "final_ci_failed", (created_at, author, decision)


def test_parser_final_ci_failed_marker_that_cannot_rearm(tmp_path: Path) -> None:
	cases = [
		# Older than the clean marker: a review already ran after the failure.
		_final_ci_comment(created_at="2026-11-05T00:00:00Z"),
		# Untrusted authors.
		_final_ci_comment(created_at="2026-11-06T12:00:00Z", author="mallory", author_type="Bot"),
		_final_ci_comment(created_at="2026-11-06T12:00:00Z", author="github-actions[bot]", author_type="User"),
		# Another head or PR.
		_final_ci_comment(created_at="2026-11-06T12:00:00Z", head=OTHER_HEAD),
		_final_ci_comment(created_at="2026-11-06T12:00:00Z", pr="43"),
	]
	for comment in cases:
		decision = _run_parser(tmp_path, [_summary_comment(), comment])[1]
		# A newer non-Bot comment still re-arms through newer_human_comment.
		expected = "newer_human_comment" if comment["author_type"] == "User" else "none"
		assert decision["reason"] == expected, (comment, decision)


# ---------------------------------------------------------------------------
# Whole gate step with a fake gh
# ---------------------------------------------------------------------------

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
fixture = json.load(open(os.environ["FAKE_GH_FIXTURE"]))
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
	log.write(json.dumps(args) + "\n")
endpoint = next((arg for arg in args[1:] if arg.startswith("repos/") or arg == "user"), "")
jq = args[args.index("--jq") + 1] if "--jq" in args else None
def emit(value):
	if jq:
		# Like gh --jq: a string result prints raw, anything else as JSON.
		print(subprocess.run(["jq", "-r", "-c", jq], input=json.dumps(value), text=True, capture_output=True, check=True).stdout, end="")
		sys.exit(0)
	print(json.dumps(value))
	sys.exit(0)
if endpoint == "user":
	print(fixture.get("login", "workflow-pat-user"))
	sys.exit(0)
if endpoint.endswith("/pulls/42"):
	emit(fixture["pr"])
if endpoint.endswith("/issues/42/comments"):
	emit(fixture.get("comments", []))
if endpoint.endswith("/pulls/42/reviews"):
	if fixture.get("reviews_fail"):
		sys.exit(1)
	emit(fixture.get("reviews", []))
if "/commits/" in endpoint and "/statuses" not in endpoint and "commit" in fixture:
	emit(fixture["commit"])
if "/commits/" in endpoint and "/statuses" in endpoint:
	if fixture.get("statuses_fail"):
		sys.exit(1)
	emit(fixture.get("statuses", []))
if endpoint.endswith("/pulls/42/files"):
	emit([{"filename": "scripts/tool.sh", "status": "modified"}])
sys.exit(2)
'''


def _gate_fixture(**overrides: object) -> dict:
	pr = {"state": "open", "merged": False, "head": {"ref": "ai/issue-42", "sha": HEAD, "repo": {"full_name": "o/r"}},
		"base": {"ref": "main", "sha": BASE}, "updated_at": "2026-11-06T09:00:00Z", "labels": [],
		"additions": 400, "deletions": 50, "changed_files": 1, "mergeable": True, "mergeable_state": "clean",
		"title": "Test", "body": ""}
	pr.update(overrides.pop("pr", {}))
	raw_comments = [{"id": 10, "user": {"login": PIPELINE, "type": "User"}, "created_at": MARKER_TIME, "author_association": "OWNER",
		"body": "AI autofix editor summary\n\nChange status:\n- not-edited\n\n" + _clean_marker()}]
	fixture = {"pr": pr, "comments": raw_comments, "reviews": []}
	fixture.update(overrides)
	return fixture


def _run_gate(tmp_path: Path, fixture: dict, **env_overrides: str) -> tuple[subprocess.CompletedProcess, dict[str, str], list[list[str]]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	fixture_file = tmp_path / "fixture.json"
	fixture_file.write_text(json.dumps(fixture), encoding="utf-8")
	log = tmp_path / "calls.log"
	log.write_text("", encoding="utf-8")
	output_file = tmp_path / "output.txt"
	output_file.write_text("", encoding="utf-8")
	work = tmp_path / "work"
	work.mkdir(exist_ok=True)
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_GH_FIXTURE=str(fixture_file), FAKE_GH_LOG=str(log),
		GITHUB_OUTPUT=str(output_file), GITHUB_ENV=str(tmp_path / "github_env"), RUNNER_TEMP=str(tmp_path), REPOSITORY="o/r",
		PR_NUMBER="42", PR_IS_DRAFT="false", PR_SKIP_AI="false", PR_TITLE="Test", PR_BODY="", EVENT_NAME="workflow_dispatch",
		EVENT_ACTION="", FINGERPRINT_CAP_SUPPORT_VERIFIED="false", REVIEW_SUPPORT_SHA=SUPPORT, PYTHONDONTWRITEBYTECODE="1")
	for key in ("AUTOFIX_SKIP_CLEAN_SAME_HEAD", "FORCE_RB_JUDGE", "POST_SYNC_TEST_REPAIR", "GH_API_RETRY_HELPERS", "HEAD_GATE_HELPER_VERIFIED", "RETARGET_HELPER_VERIFIED"):
		env.pop(key, None)
	env.update(env_overrides)
	result = subprocess.run(["bash", str(GATE_SCRIPT)], env=env, cwd=work, capture_output=True, text=True, check=False)
	outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
	return result, outputs, calls


def _reviews_calls(calls: list[list[str]]) -> int:
	return sum(1 for call in calls if any(arg.endswith("/pulls/42/reviews") for arg in call))


def test_gate_skips_a_clean_same_head(tmp_path: Path) -> None:
	result, outputs, calls = _run_gate(tmp_path, _gate_fixture())
	assert result.returncode == 0, result.stderr
	assert outputs["should_run"] == "false" and outputs["skip_reason"] == "clean_same_head"
	assert outputs["deterministic_skip"] == "false" and outputs["base_sha"] == BASE
	assert f"AUTOFIX_GATE_SKIP reason=clean_same_head pr=42 head_sha={HEAD} marker_run=777 marker_comment_id=10" in result.stdout
	assert result.stdout.count("AUTOFIX_GATE_SKIP ") == 1
	assert _reviews_calls(calls) == 1
	assert sum(1 for call in calls if any(arg.endswith("/issues/42/comments") for arg in call)) == 1
	assert not any("POST" in call for call in calls)


def test_gate_force_review_label_does_not_bypass_an_existing_clean_marker(tmp_path: Path) -> None:
	fixture = _gate_fixture(pr={"labels": [{"name": "force-review"}]})
	result, outputs, _ = _run_gate(tmp_path, fixture)
	assert outputs["skip_reason"] == "clean_same_head", result.stdout
	fixture["comments"] = []
	result, outputs, calls = _run_gate(tmp_path, fixture)
	assert outputs["should_run"] == "true", result.stdout
	assert "AUTOFIX_GATE_CLEAN_SAME_HEAD_CHECK outcome=no_skip reason=no_marker" in result.stdout
	assert _reviews_calls(calls) == 0


def test_gate_runs_when_preconditions_fail(tmp_path: Path) -> None:
	cases = [
		("disabled", _gate_fixture(), {"AUTOFIX_SKIP_CLEAN_SAME_HEAD": "false"}),
		("force_rb_judge", _gate_fixture(), {"FORCE_RB_JUDGE": "true"}),
		("post_sync_test_repair", _gate_fixture(), {"POST_SYNC_TEST_REPAIR": "true"}),
		("reopened", _gate_fixture(), {"EVENT_NAME": "pull_request", "EVENT_ACTION": "reopened"}),
		("mergeable_not_true", _gate_fixture(pr={"mergeable": False, "mergeable_state": "dirty"}), {}),
		("mergeable_not_true", _gate_fixture(pr={"mergeable": None}), {}),
		("base_sha_unavailable", _gate_fixture(pr={"base": {"ref": "main"}}), {}),
	]
	for reason, fixture, env in cases:
		result, outputs, calls = _run_gate(tmp_path, fixture, **env)
		assert result.returncode == 0, result.stderr
		assert outputs["should_run"] == "true" and outputs["skip_reason"] != "clean_same_head", (reason, result.stdout)
		assert f"AUTOFIX_GATE_CLEAN_SAME_HEAD_CHECK outcome=no_skip reason={reason} pr=42" in result.stdout, (reason, result.stdout)
		assert _reviews_calls(calls) == 0


def test_gate_runs_on_head_base_support_or_newer_activity(tmp_path: Path) -> None:
	human = {"id": 11, "user": {"login": "alice", "type": "User"}, "created_at": "2026-11-06T11:00:00Z", "body": "please look again"}
	cases = [
		("no_marker", _gate_fixture(pr={"head": {"ref": "ai/issue-42", "sha": OTHER_HEAD, "repo": {"full_name": "o/r"}}}), {}),
		("base_moved", _gate_fixture(pr={"base": {"ref": "main", "sha": OTHER_BASE}}), {}),
		("support_changed", _gate_fixture(), {"REVIEW_SUPPORT_SHA": OTHER_SUPPORT}),
		("newer_human_comment", _gate_fixture(), {}),
		("newer_review", _gate_fixture(reviews=[{"id": 1, "user": {"login": "alice", "type": "User"}, "submitted_at": "2026-11-06T11:00:00Z", "state": "COMMENTED"}]), {}),
	]
	cases[3][1]["comments"].append(human)
	for reason, fixture, env in cases:
		result, outputs, _ = _run_gate(tmp_path, fixture, **env)
		assert result.returncode == 0, result.stderr
		assert outputs["should_run"] == "true", (reason, result.stdout)
		assert f"AUTOFIX_GATE_CLEAN_SAME_HEAD_CHECK outcome=no_skip reason={reason} pr=42" in result.stdout, (reason, result.stdout)
	# Pipeline, Bot and pending reviews do not re-arm.
	fixture = _gate_fixture(reviews=[
		{"id": 2, "user": {"login": PIPELINE, "type": "User"}, "submitted_at": "2026-11-06T11:00:00Z", "state": "COMMENTED"},
		{"id": 3, "user": {"login": "x[bot]", "type": "Bot"}, "submitted_at": "2026-11-06T11:00:00Z", "state": "COMMENTED"},
		{"id": 4, "user": {"login": "alice", "type": "User"}, "state": "PENDING"},
		{"id": 5, "user": {"login": "alice", "type": "User"}, "submitted_at": "2026-11-05T11:00:00Z", "state": "APPROVED"},
	])
	assert _run_gate(tmp_path, fixture)[1]["skip_reason"] == "clean_same_head"


def test_gate_fails_open_on_lookup_errors(tmp_path: Path) -> None:
	result, outputs, _ = _run_gate(tmp_path, _gate_fixture(reviews_fail=True))
	assert outputs["should_run"] == "true"
	assert "AUTOFIX_GATE_CLEAN_SAME_HEAD_CHECK outcome=error reason=reviews_api_error pr=42" in result.stdout
	result, outputs, _ = _run_gate(tmp_path, _gate_fixture(login=""))
	assert outputs["should_run"] == "true"
	assert "AUTOFIX_GATE_CLEAN_SAME_HEAD_CHECK outcome=error reason=marker_author_unavailable pr=42" in result.stdout


def test_gate_ignores_an_untrusted_clean_marker(tmp_path: Path) -> None:
	fixture = _gate_fixture()
	fixture["comments"][0]["user"]["login"] = "mallory"
	result, outputs, calls = _run_gate(tmp_path, fixture)
	assert outputs["should_run"] == "true"
	assert "outcome=no_skip reason=no_marker" in result.stdout and "untrusted_markers=1" in result.stdout
	assert _reviews_calls(calls) == 0


def test_gate_keeps_security_pass_results_and_rearms(tmp_path: Path) -> None:
	fixture = _gate_fixture()
	fixture["comments"].append({"id": 5, "user": {"login": PIPELINE, "type": "User"}, "created_at": "2026-11-06T12:00:00Z",
		"body": f"Single-issue security pass: clean\n\n<!-- ai:single-issue-security-pass:v1 status=clean head={HEAD} cycle=1 -->"})
	result, outputs, calls = _run_gate(tmp_path, fixture)
	assert outputs["should_run"] == "true"
	assert "outcome=no_skip reason=newer_security_pass_result" in result.stdout
	assert _reviews_calls(calls) == 0


def test_gate_freshness_ci_failed_marker_rearms(tmp_path: Path) -> None:
	fixture = _gate_fixture()
	fixture["comments"].append({"id": 4, "user": {"login": PIPELINE, "type": "User"}, "created_at": "2026-11-05T00:00:00Z",
		"body": f"CI failed after the update.\n<!-- ai:freshness-ci-failed:v1 pr=42 head={HEAD} -->"})
	result, outputs, _ = _run_gate(tmp_path, fixture)
	assert outputs["should_run"] == "true"
	assert "outcome=no_skip reason=freshness_ci_failed" in result.stdout


def test_gate_final_ci_failed_marker_rearms(tmp_path: Path) -> None:
	fixture = _gate_fixture()
	fixture["comments"].append({"id": 40, "user": {"login": "github-actions[bot]", "type": "Bot"}, "created_at": "2026-11-06T12:00:00Z",
		"author_association": "NONE", "body": f"**The final CI run failed on head `x`.**\n\n<!-- ai:final-ci-failed:v1 pr=42 head={HEAD} -->"})
	result, outputs, calls = _run_gate(tmp_path, fixture)
	assert outputs["should_run"] == "true", result.stdout + result.stderr
	assert "AUTOFIX_GATE_CLEAN_SAME_HEAD_CHECK outcome=no_skip reason=final_ci_failed pr=42" in result.stdout
	assert _reviews_calls(calls) == 0


# ---------------------------------------------------------------------------
# Marker writer
# ---------------------------------------------------------------------------

FAKE_GH_FUNC = """gh() {
  local prev="" arg
  for arg in "$@"; do
    if [ "${prev}" = "-f" ] && [ "${arg#body=}" != "${arg}" ]; then
      printf '%s' "${arg#body=}" > "${FAKE_GH_BODY_FILE}"
    fi
    prev="${arg}"
  done
  return 0
}
gh_retry() { "$@"; }
"""


def _editor_summary(status: str = "not-edited", ignored: str = "- none") -> str:
	return (
		"Changes made:\n- none\n\n"
		f"Change status:\n- {status}\n\n"
		"Already satisfied (suggested but already present):\n- none\n\n"
		f"Ignored suggestions (with short reason):\n{ignored}\n\n"
		"Reviewer files processed:\n- previous_reviews/review_1.md — used\n\n"
		"Regression fingerprint:\n- none\n\n"
		"Runtime failure path:\n- none\n"
	)


def _run_writer(tmp_path: Path, summary: str, *, committed: str = "- none\n", gh_func: str = FAKE_GH_FUNC, **env_overrides: str) -> tuple[subprocess.CompletedProcess, str]:
	support_dir = tmp_path / "support"
	support_dir.mkdir(exist_ok=True)
	(support_dir / "gh_helpers.sh").write_text(gh_func, encoding="utf-8")
	summary_file = tmp_path / "editor_summary.txt"
	summary_file.write_text(summary, encoding="utf-8")
	committed_file = tmp_path / "committed.txt"
	committed_file.write_text(committed, encoding="utf-8")
	body_file = tmp_path / "body.txt"
	if body_file.exists():
		body_file.unlink()
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1",
		"FAKE_GH_BODY_FILE": str(body_file), "SUPPORT_SCRIPTS_DIR": str(support_dir), "RUNTIME_DIR": str(tmp_path),
		"EDITOR_SUMMARY_FILE": str(summary_file), "COMMITTED_FILES_FILE": str(committed_file),
		"PR_EDITOR_COMMENT_FILE": str(tmp_path / "comment.txt"), "GITHUB_ENV": str(tmp_path / "github_env"),
		"PR_NUMBER": "42", "AUTOFIX_STEP_REPOSITORY": "o/r", "AUTOFIX_STEP_SERVER_URL": "https://github.com",
		"AUTOFIX_STEP_RUN_ID": "777", "GITHUB_RUN_ID": "777", "AUTOFIX_FAILURE_HEAD_SHA": HEAD,
		"AUTOFIX_CLEAN_MARKER_BASE_SHA": BASE, "REVIEW_SUPPORT_SHA": SUPPORT,
		"AUTOFIX_FAILURE_HEAL_PY": str(REPO_ROOT / "scripts" / "workflow_failure_heal.py"),
	}
	env.update(env_overrides)
	result = subprocess.run(["bash", str(SUMMARY_SCRIPT)], env=env, capture_output=True, text=True, timeout=120, check=False)
	return result, body_file.read_text(encoding="utf-8") if body_file.exists() else ""


def test_writer_appends_the_marker_on_a_clean_review(tmp_path: Path) -> None:
	result, body = _run_writer(tmp_path, _editor_summary())
	assert result.returncode == 0, result.stderr
	assert body.startswith("AI autofix editor summary")
	assert [line for line in body.splitlines() if line.strip()][-1] == _clean_marker()
	assert f"AUTOFIX_CLEAN_SAME_HEAD_MARKER outcome=written reason=none pr=42 head={HEAD}" in result.stdout
	# The gate parser accepts what the writer posts.
	comments = [{"id": 1, "author_login": PIPELINE, "author_type": "User", "created_at": MARKER_TIME, "body": body}]
	assert _run_parser(tmp_path, comments)[1]["decision"] == "candidate"


def test_writer_support_none_without_a_support_sha(tmp_path: Path) -> None:
	_, body = _run_writer(tmp_path, _editor_summary(), REVIEW_SUPPORT_SHA="")
	assert body.rstrip().endswith(_clean_marker(support="none"))


def test_writer_skips_the_marker_off_the_clean_path(tmp_path: Path) -> None:
	cases = [
		("not_not_edited", _editor_summary(status="edited"), {}, "- none\n"),
		("committed", _editor_summary(), {}, "- scripts/tool.sh\n"),
		("committed", _editor_summary(), {"DID_COMMIT": "true"}, "- none\n"),
		("sandbox_excluded", _editor_summary(ignored='- fix in .claude/docs/x.md — "sandbox-excluded path"'), {}, "- none\n"),
		("sandbox_excluded", _editor_summary(ignored='- fix in .claude/docs/x.md — "sandbox-excluded path"'), {"AUTOFIX_FAILURE_HEAL_PY": ""}, "- none\n"),
		("head_unavailable", _editor_summary(), {"AUTOFIX_FAILURE_HEAD_SHA": "abc"}, "- none\n"),
		("base_unavailable", _editor_summary(), {"AUTOFIX_CLEAN_MARKER_BASE_SHA": ""}, "- none\n"),
		("run_unavailable", _editor_summary(), {"AUTOFIX_STEP_RUN_ID": "x"}, "- none\n"),
	]
	for reason, summary, env, committed in cases:
		result, body = _run_writer(tmp_path, summary, committed=committed, **env)
		assert result.returncode == 0, (reason, result.stderr)
		assert "review-autofix-clean:v1" not in body, reason
		assert f"AUTOFIX_CLEAN_SAME_HEAD_MARKER outcome=not_written reason={reason} pr=42" in result.stdout, (reason, result.stdout)


def test_writer_skips_the_marker_for_the_fallback_summary(tmp_path: Path) -> None:
	result, body = _run_writer(tmp_path, "", committed="- none\n")
	assert result.returncode == 0, result.stderr
	assert "review-autofix-clean:v1" not in body
	assert "AUTOFIX_CLEAN_SAME_HEAD_MARKER outcome=not_written reason=fallback" in result.stdout


# ---------------------------------------------------------------------------
# Stale ai:review-blocked clear after a clean review (issue #9369 / #9716)
# ---------------------------------------------------------------------------

FAKE_GH_STALE_FUNC = """gh() {
  local prev="" arg
  printf '%s ' "${@//$'\\n'/ }" >> "${FAKE_GH_LOG}"
  printf '\\n' >> "${FAKE_GH_LOG}"
  for arg in "$@"; do
    if [ "${prev}" = "-f" ] && [ "${arg#body=}" != "${arg}" ]; then
      printf '%s' "${arg#body=}" > "${FAKE_GH_BODY_FILE}"
    fi
    prev="${arg}"
  done
  if [ "${1:-}" = "api" ] && [ "${2:-}" = "user" ]; then
    [ -n "${FAKE_GH_USER_LOGIN:-}" ] || return 1
    printf '%s\\n' "${FAKE_GH_USER_LOGIN}"
    return 0
  fi
  if [ "${2:-}" = "-X" ] && [ "${3:-}" = "DELETE" ]; then
    case "${FAKE_GH_DELETE_MODE:-ok}" in
      ok) return 0 ;;
      404) echo "gh: Not Found (HTTP 404)" >&2; return 1 ;;
      notfound) echo "gh: not found (AI_COMMENT_SIG shim)" >&2; return 127 ;;
      *) printf '%s\\n' "::error::gh command failed after 5 attempts: api -X DELETE" "::warning::  stderr: gh: Server Error (HTTP 500)" >&2; return 1 ;;
    esac
  fi
  return 0
}
gh_retry() { "$@"; }
"""

STALE_LINE = "REVIEW_BLOCKED_STALE_CLEAR "
DELETE_URL = "repos/o/r/issues/42/labels/ai%3Areview-blocked"


def _cap_comment(head: str = OTHER_HEAD, support: str | None = SUPPORT, *, comment_id: int = 20, author: str = PIPELINE,
		pr_label: bool = True, inline: bool = False) -> dict[str, object]:
	support_field = f" support={support}" if support else ""
	marker = f"<!-- review-autofix-failure-cap:v1 head={head} fp={'b' * 16} reason=reviewers_failed count=3{support_field} -->"
	labeled = "this pull request (no linked issue found)" if pr_label else "linked issue(s) #7"
	body = f"**AI review/autofix stopped: identical failure repeated**\n\nLabeled `ai:review-blocked`: {labeled}.\n\n"
	body += f"Quoted from an older run: {marker} (see above)" if inline else marker
	return {"id": comment_id, "user": {"login": author, "type": "User"}, "body": body}


def _run_stale_clear(tmp_path: Path, comments: list[dict[str, object]], *, labels: tuple[str, ...] = ("ai:review-blocked",),
		user_login: str = PIPELINE, delete_mode: str = "ok", **env_overrides: str) -> tuple[subprocess.CompletedProcess, list[str], list[str]]:
	payload = tmp_path / "pr_payload.json"
	payload.write_text(json.dumps({"number": 42, "labels": [{"name": name} for name in labels]}), encoding="utf-8")
	comments_file = tmp_path / "pr_issue_comments.json"
	comments_file.write_text(json.dumps(comments), encoding="utf-8")
	log = tmp_path / "gh_calls.log"
	log.write_text("", encoding="utf-8")
	env = {"PR_PAYLOAD_FILE": str(payload), "PR_ISSUE_COMMENTS_FILE": str(comments_file), "FAKE_GH_LOG": str(log),
		"FAKE_GH_USER_LOGIN": user_login, "FAKE_GH_DELETE_MODE": delete_mode}
	env.update(env_overrides)
	result, _ = _run_writer(tmp_path, _editor_summary(), gh_func=FAKE_GH_STALE_FUNC, **env)
	stale_lines = [line for line in result.stdout.splitlines() if line.startswith(STALE_LINE)]
	calls = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
	return result, stale_lines, calls


def _stale_line(outcome: str, reason: str, *, cap_head: str = "none", cap_support: str = "none", clean_support: str = SUPPORT) -> str:
	return (f"{STALE_LINE}pr=42 outcome={outcome} reason={reason} cap_head={cap_head} cap_support={cap_support} "
		f"clean_head={HEAD} clean_support={clean_support}")


def _user_calls(calls: list[str]) -> int:
	return sum(1 for call in calls if call.startswith("api user "))


def _delete_calls(calls: list[str]) -> int:
	return sum(1 for call in calls if call.startswith("api -X DELETE ") and DELETE_URL in call)


def test_stale_clear_removes_label_after_head_change(tmp_path: Path) -> None:
	result, lines, calls = _run_stale_clear(tmp_path, [_cap_comment()])
	assert result.returncode == 0, result.stderr
	assert lines == [_stale_line("removed", "cap_head_mismatch", cap_head=OTHER_HEAD, cap_support=SUPPORT)], result.stdout
	assert _user_calls(calls) == 1 and _delete_calls(calls) == 1, calls
	# The clean marker still ends the posted comment.
	assert "AUTOFIX_CLEAN_SAME_HEAD_MARKER outcome=written" in result.stdout
	# A trusted author differing only in case still matches.
	_, lines, calls = _run_stale_clear(tmp_path, [_cap_comment(author=PIPELINE.upper())])
	assert lines[0].split(" ")[2] == "outcome=removed" and _delete_calls(calls) == 1


def test_stale_clear_removes_label_on_support_mismatch(tmp_path: Path) -> None:
	result, lines, calls = _run_stale_clear(tmp_path, [_cap_comment(head=HEAD, support=OTHER_SUPPORT)])
	assert result.returncode == 0, result.stderr
	assert lines == [_stale_line("removed", "cap_support_mismatch", cap_head=HEAD, cap_support=OTHER_SUPPORT)], result.stdout
	assert _user_calls(calls) == 1 and _delete_calls(calls) == 1


def test_stale_clear_removes_label_when_cap_support_missing(tmp_path: Path) -> None:
	result, lines, calls = _run_stale_clear(tmp_path, [_cap_comment(head=HEAD, support=None)])
	assert result.returncode == 0, result.stderr
	assert lines == [_stale_line("removed", "cap_support_mismatch", cap_head=HEAD)], result.stdout
	assert _delete_calls(calls) == 1
	# With no review-support SHA the clean marker says support=none: heads only.
	result, lines, calls = _run_stale_clear(tmp_path, [_cap_comment(head=HEAD, support=None)], REVIEW_SUPPORT_SHA="")
	assert lines == [_stale_line("kept", "same_head_same_support", cap_head=HEAD, clean_support="none")], result.stdout
	assert _user_calls(calls) == 0 and _delete_calls(calls) == 0


def test_stale_clear_keeps_label_same_head_same_support(tmp_path: Path) -> None:
	result, lines, calls = _run_stale_clear(tmp_path, [_cap_comment(head=HEAD, support=SUPPORT)])
	assert result.returncode == 0, result.stderr
	assert lines == [_stale_line("kept", "same_head_same_support", cap_head=HEAD, cap_support=SUPPORT)], result.stdout
	assert _user_calls(calls) == 0 and _delete_calls(calls) == 0


def test_stale_clear_keeps_label_without_cap_marker(tmp_path: Path) -> None:
	result, lines, calls = _run_stale_clear(tmp_path, [_summary_comment(comment_id=5)])
	assert result.returncode == 0, result.stderr
	assert lines == [_stale_line("kept", "no_cap_marker")], result.stdout
	assert _user_calls(calls) == 0 and _delete_calls(calls) == 0
	# A marker quoted inline, not on its own line, does not count.
	_, lines, calls = _run_stale_clear(tmp_path, [_cap_comment(inline=True)])
	assert lines == [_stale_line("kept", "no_cap_marker")]
	assert _user_calls(calls) == 0 and _delete_calls(calls) == 0


def test_stale_clear_keeps_label_for_untrusted_cap_author(tmp_path: Path) -> None:
	result, lines, calls = _run_stale_clear(tmp_path, [_cap_comment(author="intruder")])
	assert result.returncode == 0, result.stderr
	assert lines == [_stale_line("kept", "untrusted_cap_author", cap_head=OTHER_HEAD, cap_support=SUPPORT)], result.stdout
	assert _user_calls(calls) == 1 and _delete_calls(calls) == 0
	# The newest trusted marker wins: a newer marker from another author does
	# not shadow the older trusted stale one.
	_, lines, calls = _run_stale_clear(tmp_path, [_cap_comment(comment_id=20), _cap_comment(comment_id=30, author="intruder")])
	assert lines == [_stale_line("removed", "cap_head_mismatch", cap_head=OTHER_HEAD, cap_support=SUPPORT)]
	assert _user_calls(calls) == 1 and _delete_calls(calls) == 1
	# A newer stale forged marker cannot remove a label the trusted marker holds.
	_, lines, calls = _run_stale_clear(tmp_path, [_cap_comment(head=HEAD, comment_id=20), _cap_comment(comment_id=30, author="intruder")])
	assert lines == [_stale_line("kept", "same_head_same_support", cap_head=HEAD, cap_support=SUPPORT)]
	assert _user_calls(calls) == 1 and _delete_calls(calls) == 0
	# Without a trusted login nothing is removed.
	_, lines, calls = _run_stale_clear(tmp_path, [_cap_comment()], user_login="")
	assert lines[0].split(" ")[2:4] == ["outcome=kept", "reason=marker_author_unavailable"] and _delete_calls(calls) == 0


def test_stale_clear_delete_failure_logs_error_and_exits_zero(tmp_path: Path) -> None:
	result, lines, calls = _run_stale_clear(tmp_path, [_cap_comment()], delete_mode="fail")
	assert result.returncode == 0, result.stderr
	assert lines == [_stale_line("error", "delete_failed", cap_head=OTHER_HEAD, cap_support=SUPPORT)], result.stdout
	assert "::warning::Could not remove stale ai:review-blocked from PR #42: gh: Server Error (HTTP 500)" in result.stdout
	assert _delete_calls(calls) == 1
	assert "EDITOR_SUMMARY_POSTED=true" in (tmp_path / "github_env").read_text(encoding="utf-8")
	# A 404 means the label is already gone.
	result, lines, _ = _run_stale_clear(tmp_path, [_cap_comment()], delete_mode="404")
	assert result.returncode == 0 and lines == [_stale_line("removed", "cap_head_mismatch", cap_head=OTHER_HEAD, cap_support=SUPPORT)]
	# Other "not found" text without HTTP 404 is a failure, not "already gone".
	result, lines, _ = _run_stale_clear(tmp_path, [_cap_comment()], delete_mode="notfound")
	assert result.returncode == 0 and lines == [_stale_line("error", "delete_failed", cap_head=OTHER_HEAD, cap_support=SUPPORT)]


def test_stale_clear_keeps_label_when_absent_or_from_another_path(tmp_path: Path) -> None:
	result, lines, calls = _run_stale_clear(tmp_path, [_cap_comment()], labels=("bug",))
	assert result.returncode == 0, result.stderr
	assert lines == [_stale_line("kept", "label_absent")], result.stdout
	assert calls == [call for call in calls if "/comments" in call]
	# A cap that labelled linked issues did not put the label on this PR.
	_, lines, calls = _run_stale_clear(tmp_path, [_cap_comment(pr_label=False)])
	assert lines == [_stale_line("kept", "cap_not_pr_label", cap_head=OTHER_HEAD, cap_support=SUPPORT)]
	assert _user_calls(calls) == 0 and _delete_calls(calls) == 0


def test_stale_clear_runs_only_after_a_clean_marker(tmp_path: Path) -> None:
	result, lines, calls = _run_stale_clear(tmp_path, [_cap_comment()], DID_COMMIT="true")
	assert result.returncode == 0, result.stderr
	assert "AUTOFIX_CLEAN_SAME_HEAD_MARKER outcome=not_written reason=committed" in result.stdout
	assert lines == [] and _user_calls(calls) == 0 and _delete_calls(calls) == 0


def test_stale_clear_falls_back_to_one_read_without_a_snapshot(tmp_path: Path) -> None:
	result, lines, calls = _run_stale_clear(tmp_path, [_cap_comment()], PR_PAYLOAD_FILE=str(tmp_path / "missing.json"))
	assert result.returncode == 0, result.stderr
	# The stub's PR read returns no labels.
	assert lines == [_stale_line("kept", "label_absent")], result.stdout
	assert sum(1 for call in calls if call.startswith("api repos/o/r/pulls/42 ")) == 1


# ---------------------------------------------------------------------------
# Final-CI repair gate output (final-ci-failure-ci-fix plan, Phase 2)
# ---------------------------------------------------------------------------

def _repair_fixture(*extra_comments: dict, head_ref: str = "ai/issue-42", statuses: list | None = None) -> dict:
	fixture = _gate_fixture(pr={"head": {"ref": head_ref, "sha": HEAD, "repo": {"full_name": "o/r"}}})
	fixture["comments"].append({"id": 40, "user": {"login": "github-actions[bot]", "type": "Bot"}, "created_at": "2026-11-06T12:00:00Z",
		"author_association": "NONE", "body": f"**The final CI run failed.**\n\n<!-- ai:final-ci-failed:v1 pr=42 head={HEAD} -->"})
	fixture["comments"].extend(extra_comments)
	fixture["statuses"] = statuses if statuses is not None else [
		{"context": "ai-review/head-gate", "state": "success", "creator": {"login": PIPELINE}},
		{"context": "ai-review/head-gate", "state": "pending", "creator": {"login": PIPELINE}},
	]
	return fixture


def _repair_marker(source: str, comment_id: int, author: str = PIPELINE, attempt: int = 1, repair: str = "9" * 40) -> dict:
	return {"id": comment_id, "user": {"login": author, "type": "User"}, "created_at": "2026-11-06T13:00:00Z", "author_association": "OWNER",
		"body": f"**Final CI repair.**\n\n<!-- ai:final-ci-repair:v1 pr=42 from={source} repair={repair} attempt={attempt} -->"}


def _statuses_calls(calls: list[list[str]]) -> int:
	return sum(1 for call in calls if any("/statuses" in arg for arg in call))


def test_gate_selects_the_final_ci_repair(tmp_path: Path) -> None:
	result, outputs, calls = _run_gate(tmp_path, _repair_fixture())
	assert outputs["should_run"] == "true", result.stdout + result.stderr
	assert outputs["final_ci_repair"] == "true" and outputs["final_ci_repair_attempt"] == "1"
	assert f"FINAL_CI_REPAIR pr=42 head={HEAD} outcome=selected attempt=1" in result.stdout
	assert _statuses_calls(calls) == 1
	# A repair of another head counts toward the PR cap and sets the ordinal.
	result, outputs, _ = _run_gate(tmp_path, _repair_fixture(_repair_marker(OTHER_HEAD, 50)))
	assert outputs["final_ci_repair"] == "true" and outputs["final_ci_repair_attempt"] == "2"
	# An untrusted repair marker does not count.
	result, outputs, _ = _run_gate(tmp_path, _repair_fixture(_repair_marker(HEAD, 51, author="mallory")))
	assert outputs["final_ci_repair"] == "true" and outputs["final_ci_repair_attempt"] == "1"


def test_gate_final_ci_repair_caps(tmp_path: Path) -> None:
	result, outputs, calls = _run_gate(tmp_path, _repair_fixture(_repair_marker(HEAD, 50)))
	assert outputs["final_ci_repair"] == "false"
	assert f"FINAL_CI_REPAIR pr=42 head={HEAD} outcome=skipped reason=head_cap repairs=1" in result.stdout
	assert _statuses_calls(calls) == 0
	result, outputs, _ = _run_gate(tmp_path, _repair_fixture(_repair_marker(OTHER_HEAD, 50), _repair_marker(BASE, 51, attempt=2)))
	assert outputs["final_ci_repair"] == "false"
	assert "outcome=skipped reason=pr_cap repairs=2" in result.stdout


def test_gate_final_ci_repair_needs_every_condition(tmp_path: Path) -> None:
	newer_failure = {"id": 60, "user": {"login": PIPELINE, "type": "User"}, "created_at": "2026-11-06T14:00:00Z", "author_association": "OWNER",
		"body": f"**AI review/autofix failed**\n<!-- review-autofix-failure:v1 head={HEAD} reason=r fp={'a' * 64} degraded=0 run=9 -->"}
	cases = [
		(_repair_fixture(head_ref="orchestrator/project-9"), {}, "reason=orchestrator_head", 0),
		(_repair_fixture(newer_failure), {}, "reason=already_reviewed", 0),
		(_repair_fixture(statuses=[{"context": "ai-review/head-gate", "state": "pending", "creator": {"login": PIPELINE}}]), {},
			"reason=head_gate_not_success state=pending", 1),
		(_repair_fixture(statuses=[{"context": "ai-review/head-gate", "state": "success", "creator": {"login": "mallory"}}]), {},
			"reason=head_gate_not_success state=success", 1),
		(_repair_fixture(statuses=[]), {}, "reason=head_gate_not_success state=none", 1),
		(dict(_repair_fixture(), statuses_fail=True), {}, "reason=head_gate_not_success state=unreadable", 1),
		(_repair_fixture(), {"FINAL_CI_REPAIR_ENABLED": "false"}, None, 0),
		(_repair_fixture(), {"FINAL_CI_REPAIR_ENABLED": "maybe"}, "FINAL_CI_REPAIR_ENABLED has an unrecognised value", 0),
	]
	for fixture, env, expected, statuses in cases:
		result, outputs, calls = _run_gate(tmp_path, fixture, **env)
		assert outputs["should_run"] == "true", (expected, result.stdout + result.stderr)
		assert outputs["final_ci_repair"] == "false", expected
		if expected:
			assert expected in result.stdout, (expected, result.stdout)
		else:
			assert "FINAL_CI_REPAIR " not in result.stdout
		assert _statuses_calls(calls) == statuses, expected


def test_gate_without_a_final_ci_marker_makes_no_repair_read(tmp_path: Path) -> None:
	fixture = _gate_fixture()
	fixture["comments"].append({"id": 70, "user": {"login": "alice", "type": "User"}, "created_at": "2026-11-06T12:00:00Z",
		"author_association": "OWNER", "body": "please look again"})
	result, outputs, calls = _run_gate(tmp_path, fixture)
	assert outputs["should_run"] == "true"
	assert outputs["final_ci_repair"] == "false" and outputs["final_ci_repair_attempt"] == ""
	assert "FINAL_CI_REPAIR " not in result.stdout and _statuses_calls(calls) == 0


def test_gate_final_ci_repair_counts_fallbacks_per_head_only(tmp_path: Path) -> None:
	# A fallback (repair=none) on this head uses up its one attempt.
	result, outputs, _ = _run_gate(tmp_path, _repair_fixture(_repair_marker(HEAD, 50, repair="none")))
	assert outputs["final_ci_repair"] == "false" and "reason=head_cap repairs=0" in result.stdout
	# Fallbacks on other heads, and a duplicated marker, do not use up the PR's two repairs.
	result, outputs, _ = _run_gate(tmp_path, _repair_fixture(_repair_marker(OTHER_HEAD, 50, repair="none"),
		_repair_marker(BASE, 51), _repair_marker(BASE, 52)))
	assert outputs["final_ci_repair"] == "true" and outputs["final_ci_repair_attempt"] == "2", result.stdout


def test_gate_final_ci_repair_ignores_a_provider_pause_notice(tmp_path: Path) -> None:
	pause = {"id": 61, "user": {"login": PIPELINE, "type": "User"}, "created_at": "2026-11-06T14:00:00Z", "author_association": "OWNER",
		"body": "**AI review/autofix paused — model provider unavailable**"}
	result, outputs, _ = _run_gate(tmp_path, _repair_fixture(pause))
	assert outputs["final_ci_repair"] == "true", result.stdout


# Issue #10023: a final CI attempt that turned green after the hand-back.
_HEAD_GATE_SUCCESS = {"context": "ai-review/head-gate", "state": "success", "creator": {"login": PIPELINE}}


def _ci_final(state: str, created_at: str, creator: str = "github-actions[bot]") -> dict:
	return {"context": "ci/final", "state": state, "created_at": created_at, "creator": {"login": creator}}


def test_gate_final_ci_repair_skips_a_head_whose_final_ci_turned_green(tmp_path: Path) -> None:
	# GitHub lists statuses newest first; the marker is at 2026-11-06T12:00:00Z.
	statuses = [_ci_final("success", "2026-11-06T13:00:00Z"), _HEAD_GATE_SUCCESS, _ci_final("failure", "2026-11-06T11:59:00Z")]
	result, outputs, calls = _run_gate(tmp_path, _repair_fixture(statuses=statuses))
	assert f"FINAL_CI_REPAIR pr=42 head={HEAD} outcome=skipped reason=final_ci_now_green" in result.stdout, result.stdout + result.stderr
	assert outputs["final_ci_repair"] == "false" and outputs["final_ci_repair_attempt"] == ""
	assert outputs["should_run"] == "false" and outputs["skip_reason"] == "final_ci_now_green"
	assert "outcome=selected" not in result.stdout
	assert _statuses_calls(calls) == 1


@pytest.mark.parametrize("ci_final", [
	[_ci_final("failure", "2026-11-06T13:00:00Z"), _ci_final("success", "2026-11-06T12:30:00Z")],
	[_ci_final("success", "2026-11-06T11:00:00Z")],
	[_ci_final("success", "2026-11-06T13:00:00Z", creator="mallory")],
	[_ci_final("success", "not-a-time")],
	[],
], ids=["newest_failure", "older_than_marker", "untrusted_creator", "unparseable_time", "no_ci_final"])
def test_gate_final_ci_repair_still_selected_when_final_ci_is_not_newer_green(tmp_path: Path, ci_final: list) -> None:
	result, outputs, calls = _run_gate(tmp_path, _repair_fixture(statuses=[*ci_final, _HEAD_GATE_SUCCESS]))
	assert outputs["should_run"] == "true", result.stdout + result.stderr
	assert outputs["final_ci_repair"] == "true" and outputs["final_ci_repair_attempt"] == "1"
	assert f"FINAL_CI_REPAIR pr=42 head={HEAD} outcome=selected attempt=1" in result.stdout
	assert "final_ci_now_green" not in result.stdout
	assert _statuses_calls(calls) == 1


def test_gate_final_ci_now_green_needs_a_pipeline_head_gate_success(tmp_path: Path) -> None:
	# An unapproved head keeps the full review even when ci/final is green.
	statuses = [_ci_final("success", "2026-11-06T13:00:00Z"), {"context": "ai-review/head-gate", "state": "pending", "creator": {"login": PIPELINE}}]
	result, outputs, _ = _run_gate(tmp_path, _repair_fixture(statuses=statuses))
	assert outputs["should_run"] == "true" and outputs["final_ci_repair"] == "false"
	assert "reason=head_gate_not_success state=pending" in result.stdout
	assert "final_ci_now_green" not in result.stdout


# ---------------------------------------------------------------------------
# Final-CI repair delta review output (final-ci-failure-ci-fix plan, Phase 3)
# ---------------------------------------------------------------------------

def _delta_fixture(*, parents: list[str] | None = None, committer: str = "codex-bot", repair: str = HEAD) -> dict:
	fixture = _gate_fixture()
	fixture["comments"] = [{"id": 80, "user": {"login": PIPELINE, "type": "User"}, "created_at": "2026-11-06T13:00:00Z", "author_association": "OWNER",
		"body": f"**Final CI repair.** x\n\n<!-- ai:final-ci-repair:v1 pr=42 from={OTHER_HEAD} repair={repair} attempt=1 -->"}]
	fixture["commit"] = {"sha": HEAD, "parents": [{"sha": sha} for sha in (parents if parents is not None else [OTHER_HEAD])],
		"commit": {"committer": {"name": committer, "email": f"{committer}@users.noreply.invalid"}}}
	fixture["statuses"] = [{"context": "ai-review/head-gate", "state": "success", "creator": {"login": PIPELINE}}]
	return fixture


def _stage_head_gate_src(tmp_path: Path) -> None:
	src = tmp_path / "work" / ".codex-head-gate-src" / "scripts"
	src.mkdir(parents=True, exist_ok=True)
	for name in ("review_head_gate.sh", "gh_helpers.sh"):
		(src / name).write_text((REPO_ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8")


def _commit_calls(calls: list[list[str]]) -> int:
	return sum(1 for call in calls if any("/commits/" in arg and "/statuses" not in arg for arg in call))


def test_gate_outputs_the_delta_base_for_a_verified_repair_head(tmp_path: Path) -> None:
	_stage_head_gate_src(tmp_path)
	result, outputs, calls = _run_gate(tmp_path, _delta_fixture(), HEAD_GATE_HELPER_VERIFIED="true", GH_RETRY_MAX_ATTEMPTS="1")
	assert outputs["should_run"] == "true", result.stdout + result.stderr
	assert outputs["review_diff_base_sha"] == OTHER_HEAD, result.stdout + result.stderr
	assert f"REVIEW_HEAD_GATE mode=final_ci_repair pr=42 head={HEAD} outcome=delta reason=final_ci_repair from={OTHER_HEAD}" in result.stdout
	assert _commit_calls(calls) == 1 and _statuses_calls(calls) == 1


def test_gate_keeps_the_full_review_when_the_delta_check_fails(tmp_path: Path) -> None:
	_stage_head_gate_src(tmp_path)
	cases = [
		(_delta_fixture(parents=[OTHER_HEAD, BASE]), {"HEAD_GATE_HELPER_VERIFIED": "true"}, "reason=head_is_not_the_repair", 1),
		(_delta_fixture(committer="mallory"), {"HEAD_GATE_HELPER_VERIFIED": "true"}, "reason=committer_not_pipeline", 1),
		(_delta_fixture(repair=BASE), {"HEAD_GATE_HELPER_VERIFIED": "true"}, None, 0),
		(_delta_fixture(), {}, None, 0),
		(_delta_fixture(), {"HEAD_GATE_HELPER_VERIFIED": "true", "FINAL_CI_REPAIR_DELTA_REVIEW_ENABLED": "false"}, None, 0),
		(_delta_fixture(), {"HEAD_GATE_HELPER_VERIFIED": "true", "FINAL_CI_REPAIR_DELTA_REVIEW_ENABLED": "on"},
			"FINAL_CI_REPAIR_DELTA_REVIEW_ENABLED has an unrecognised value", 0),
	]
	for fixture, env, expected, commit_reads in cases:
		result, outputs, calls = _run_gate(tmp_path, fixture, GH_RETRY_MAX_ATTEMPTS="1", **env)
		assert outputs["should_run"] == "true", (expected, result.stdout + result.stderr)
		assert outputs["review_diff_base_sha"] == "", (expected, result.stdout)
		if expected:
			assert expected in result.stdout, (expected, result.stdout)
		assert _commit_calls(calls) == commit_reads, expected
