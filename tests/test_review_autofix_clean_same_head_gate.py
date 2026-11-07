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
	assert 'or ($b | contains("<!-- review-autofix-clean:v1 ")) or ($b | contains("<!-- ai:freshness-ci-failed:v1 "))) |' in text
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
	fragment = FRAGMENT.read_text(encoding="utf-8")
	assert '<!-- agents: section="Stable log prefixes (contractual)" -->' in fragment
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
		value = json.loads(subprocess.run(["jq", "-c", jq], input=json.dumps(value), text=True, capture_output=True, check=True).stdout)
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
	for key in ("AUTOFIX_SKIP_CLEAN_SAME_HEAD", "FORCE_RB_JUDGE", "GH_API_RETRY_HELPERS", "HEAD_GATE_HELPER_VERIFIED", "RETARGET_HELPER_VERIFIED"):
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


def test_gate_freshness_ci_failed_marker_rearms(tmp_path: Path) -> None:
	fixture = _gate_fixture()
	fixture["comments"].append({"id": 4, "user": {"login": PIPELINE, "type": "User"}, "created_at": "2026-11-05T00:00:00Z",
		"body": f"CI failed after the update.\n<!-- ai:freshness-ci-failed:v1 pr=42 head={HEAD} -->"})
	result, outputs, _ = _run_gate(tmp_path, fixture)
	assert outputs["should_run"] == "true"
	assert "outcome=no_skip reason=freshness_ci_failed" in result.stdout


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


def _run_writer(tmp_path: Path, summary: str, *, committed: str = "- none\n", **env_overrides: str) -> tuple[subprocess.CompletedProcess, str]:
	support_dir = tmp_path / "support"
	support_dir.mkdir(exist_ok=True)
	(support_dir / "gh_helpers.sh").write_text(FAKE_GH_FUNC, encoding="utf-8")
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
