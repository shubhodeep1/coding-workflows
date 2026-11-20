#!/usr/bin/env python3
"""Generated chore PRs skip the reviewers and the security pass.

The nightly validation self-test status (`analysis/validation-selftest-status.json`,
branch `auto/validation-selftest-status-<run>-<attempt>`) and the workflow log
analysis report (`analysis/workflow-optimization-<date>.md`, branch
`auto/workflow-log-analysis-report-<run>-<attempt>`) reach the default branch
through a pull request only because that branch requires status checks. The
status file is `*.json`, so the protected-path rule kept a two-line generated
change on the full reviewer path (PR #9570: ten dispatched review rounds in 26
hours), and the report waited on security-audit cycles of a generated document
(PR #9636).

The gate decides by head ref, changed paths and author together, so a
hand-written PR with the same title, another author, another branch or one more
file is reviewed as before. The cases run the whole gate step against a fake
`gh`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = ROOT / "scripts" / "review_autofix_step_evaluate_gate.sh"
REVIEW_WF = ROOT / ".github" / "workflows" / "review_autofix.yml"
HEAD = "a" * 40
BASE = "b" * 40
PIPELINE = "workflow-pat-user"
STATUS_REF = "auto/validation-selftest-status-43791696091-1"
REPORT_REF = "auto/workflow-log-analysis-report-43990793355-1"
STATUS_FILE = {"filename": "analysis/validation-selftest-status.json", "status": "modified"}
REPORT_FILE = {"filename": "analysis/workflow-optimization-2026-11-17.md", "status": "added"}

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

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
		print(subprocess.run(["jq", "-r", "-c", jq], input=json.dumps(value), text=True, capture_output=True, check=True).stdout, end="")
		sys.exit(0)
	print(json.dumps(value))
	sys.exit(0)
if endpoint == "user":
	if fixture.get("login") is None:
		sys.exit(1)
	emit({"login": fixture["login"]})
if endpoint.endswith("/pulls/42"):
	emit(fixture["pr"])
if endpoint.endswith("/issues/42/comments"):
	emit(fixture.get("comments", [{"id": 1, "user": {"login": "alice", "type": "User"}, "created_at": "2026-11-16T12:00:00Z", "body": "note"}]))
if endpoint.endswith("/pulls/42/reviews"):
	emit([])
if endpoint.endswith("/pulls/42/files"):
	if fixture.get("files_fail"):
		sys.exit(1)
	emit(fixture["files"])
sys.exit(2)
'''


def _fixture(head_ref: str, files: list[dict], *, author: str | None = PIPELINE, login: str | None = PIPELINE,
		additions: int = 2, deletions: int = 2, pr: dict | None = None, **extra: object) -> dict:
	pull = {
		"state": "open", "merged": False,
		"head": {"ref": head_ref, "sha": HEAD, "repo": {"full_name": "o/r"}},
		"base": {"ref": "main", "sha": BASE},
		"updated_at": "2026-11-16T11:42:14Z", "labels": [],
		"additions": additions, "deletions": deletions, "changed_files": len(files),
		"mergeable": True, "mergeable_state": "clean",
		"title": "chore: update validation self-test status", "body": "",
		"user": {"login": author} if author is not None else None,
	}
	pull.update(pr or {})
	fixture: dict = {"pr": pull, "files": files, "login": login}
	fixture.update(extra)
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
		PR_NUMBER="42", PR_IS_DRAFT="false", PR_SKIP_AI="false", PR_TITLE=str(fixture["pr"]["title"]), PR_BODY="",
		EVENT_NAME="workflow_dispatch", EVENT_ACTION="", FINGERPRINT_CAP_SUPPORT_VERIFIED="false", DEFAULT_BRANCH="main",
		SINGLE_ISSUE_SECURITY_PASS_ENABLED="true", AUTOFIX_SKIP_RUNS_SECURITY_PASS="true", PYTHONDONTWRITEBYTECODE="1")
	for key in ("AUTOFIX_SKIP_GENERATED_CHORE", "AUTOFIX_SKIP_DOC_ONLY", "AUTOFIX_SKIP_CLEAN_SAME_HEAD", "FORCE_RB_JUDGE", "POST_SYNC_TEST_REPAIR",
			"GH_API_RETRY_HELPERS", "HEAD_GATE_HELPER_VERIFIED", "RETARGET_HELPER_VERIFIED", "AGENTS_MD_MATERIALITY_ENABLED",
			"AUTOFIX_SKIP_MAX_ADDITIONS", "AUTOFIX_SKIP_MAX_DELETIONS"):
		env.pop(key, None)
	env.update(env_overrides)
	result = subprocess.run(["bash", str(GATE_SCRIPT)], env=env, cwd=work, capture_output=True, text=True, check=False)
	outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
	return result, outputs, calls


def _assert_skipped(result: subprocess.CompletedProcess, outputs: dict[str, str], kind: str) -> None:
	assert result.returncode == 0, result.stderr
	assert outputs["should_run"] == "false", result.stdout
	assert outputs["deterministic_skip"] == "true" and outputs["det_skip_reason"] == "generated_chore", result.stdout
	assert outputs["skip_reason"] == "deterministic_skip_generated_chore"
	# No reviewers and no security audit for a generated file.
	assert outputs["det_skip_security_pass"] == "false"
	assert f"AUTOFIX_GATE_GENERATED_CHORE pr=42 kind={kind} outcome=skip_candidate" in result.stdout
	assert "AUTOFIX_GATE_DET_SKIP_SUPPRESSED" not in result.stdout


def _assert_reviewed(result: subprocess.CompletedProcess, outputs: dict[str, str]) -> None:
	assert result.returncode == 0, result.stderr
	assert outputs.get("det_skip_reason", "") != "generated_chore", result.stdout
	assert "outcome=skip_candidate" not in result.stdout


def test_validation_selftest_status_pr_skips_review_and_security_pass(tmp_path: Path) -> None:
	result, outputs, calls = _run_gate(tmp_path, _fixture(STATUS_REF, [STATUS_FILE]))
	_assert_skipped(result, outputs, "validation_selftest_status")
	# The exemption adds no API call: one PR read, one files read, one user read.
	assert sum(1 for call in calls if any(arg.endswith("/pulls/42") for arg in call)) == 1
	assert sum(1 for call in calls if any(arg.endswith("/pulls/42/files") for arg in call)) == 1
	assert sum(1 for call in calls if "user" in call) == 1


def test_status_pr_larger_than_the_small_diff_limit_still_skips(tmp_path: Path) -> None:
	fixture = _fixture(STATUS_REF, [{**STATUS_FILE, "status": "added"}], additions=400, deletions=0)
	result, outputs, _ = _run_gate(tmp_path, fixture)
	_assert_skipped(result, outputs, "validation_selftest_status")


def test_workflow_log_analysis_report_pr_skips_the_security_pass(tmp_path: Path) -> None:
	files = [
		REPORT_FILE,
		{"filename": "analysis/workflow-optimization-2026-11-16-2.md", "status": "modified"},
		{"filename": "analysis/workflow-optimization-2026-09-01.md", "status": "removed"},
	]
	fixture = _fixture(REPORT_REF, files, additions=784, deletions=120, pr={"title": "chore: add workflow log analysis report (workflow-optimization-2026-11-17.md)"})
	result, outputs, _ = _run_gate(tmp_path, fixture)
	_assert_skipped(result, outputs, "workflow_log_analysis_report")


@pytest.mark.parametrize("label, fixture", [
	# Same title and file, but not the generator's branch.
	("hand_branch", _fixture("claude/update-status", [STATUS_FILE])),
	("lookalike_branch", _fixture(STATUS_REF + "-x", [STATUS_FILE])),
	("branch_prefix", _fixture("x/" + STATUS_REF, [STATUS_FILE])),
	# The generator's branch, but another author.
	("other_author", _fixture(STATUS_REF, [STATUS_FILE], author="mallory")),
	("no_author", _fixture(STATUS_REF, [STATUS_FILE], author=None)),
	("case_differs", _fixture(STATUS_REF, [STATUS_FILE], author=PIPELINE.upper())),
	# One more file, another file, a rename, a deletion of the status file.
	("extra_file", _fixture(STATUS_REF, [STATUS_FILE, {"filename": "scripts/tool.sh", "status": "modified"}])),
	("other_json", _fixture(STATUS_REF, [{"filename": "analysis/other-status.json", "status": "modified"}])),
	("nested_path", _fixture(STATUS_REF, [{"filename": "x/analysis/validation-selftest-status.json", "status": "modified"}])),
	("renamed", _fixture(STATUS_REF, [{**STATUS_FILE, "status": "renamed", "previous_filename": "scripts/tool.sh"}])),
	("removed", _fixture(STATUS_REF, [{**STATUS_FILE, "status": "removed"}])),
	# The report branch may not carry the status file, nor the reverse.
	("status_on_report_branch", _fixture(REPORT_REF, [STATUS_FILE])),
	("report_on_status_branch", _fixture(STATUS_REF, [REPORT_FILE])),
	("report_plus_script", _fixture(REPORT_REF, [REPORT_FILE, {"filename": "scripts/tool.sh", "status": "added"}])),
	("report_lookalike", _fixture(REPORT_REF, [{"filename": "analysis/workflow-optimization-2026-11-17.md.sh", "status": "added"}])),
	("report_other_doc", _fixture(REPORT_REF, [{"filename": "analysis/recommendation-processing-report.md", "status": "modified"}])),
	# A fork head, or a PR into another branch.
	("fork_head", _fixture(STATUS_REF, [STATUS_FILE], pr={"head": {"ref": STATUS_REF, "sha": HEAD, "repo": {"full_name": "mallory/r"}}})),
	("other_base", _fixture(STATUS_REF, [STATUS_FILE], pr={"base": {"ref": "stable", "sha": BASE}})),
])
def test_anything_else_is_reviewed_as_before(tmp_path: Path, label: str, fixture: dict) -> None:
	result, outputs, _ = _run_gate(tmp_path, fixture)
	_assert_reviewed(result, outputs)
	if label in ("other_author", "no_author", "case_differs"):
		assert "AUTOFIX_GATE_GENERATED_CHORE pr=42 kind=validation_selftest_status outcome=review reason=author_not_pipeline" in result.stdout
	if label == "fork_head":
		# A fork head never reaches the skip evaluation at all.
		assert outputs["deterministic_skip"] == "false" and "AUTOFIX_GATE_GENERATED_CHORE" not in result.stdout
	elif fixture["files"] == [STATUS_FILE] and fixture["pr"]["base"]["ref"] == "main":
		# The status file alone keeps today's outcome: the protected path holds the small-diff skip.
		assert outputs["should_run"] == "true" and outputs["deterministic_skip"] == "false", (label, result.stdout)
		assert "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=protected_path pr=42 candidate=small_diff" in result.stdout, label


def test_unknown_pipeline_login_reviews(tmp_path: Path) -> None:
	result, outputs, _ = _run_gate(tmp_path, _fixture(STATUS_REF, [STATUS_FILE], login=None))
	_assert_reviewed(result, outputs)
	assert outputs["should_run"] == "true"
	assert "AUTOFIX_GATE_GENERATED_CHORE pr=42 kind=validation_selftest_status outcome=review reason=pipeline_login_unavailable" in result.stdout


def test_unreadable_file_list_reviews(tmp_path: Path) -> None:
	result, outputs, _ = _run_gate(tmp_path, _fixture(STATUS_REF, [STATUS_FILE], files_fail=True))
	_assert_reviewed(result, outputs)
	assert outputs["should_run"] == "true"
	assert "AUTOFIX_GATE_GENERATED_CHORE" not in result.stdout


@pytest.mark.parametrize("pr, env", [
	({}, {"AUTOFIX_SKIP_GENERATED_CHORE": "false"}),
	({"labels": [{"name": "force-review"}]}, {}),
	({"title": "chore: update validation self-test status [force-review]"}, {}),
	({"mergeable": False, "mergeable_state": "dirty"}, {}),
])
def test_switch_force_review_and_conflicts_still_send_it_to_review(tmp_path: Path, pr: dict, env: dict[str, str]) -> None:
	result, outputs, _ = _run_gate(tmp_path, _fixture(STATUS_REF, [STATUS_FILE], pr=pr), **env)
	assert result.returncode == 0, result.stderr
	assert outputs["should_run"] == "true" and outputs["deterministic_skip"] == "false", result.stdout


def test_security_pass_failed_label_keeps_the_review_path(tmp_path: Path) -> None:
	# A PR audited before the exemption existed keeps its failed-security hold.
	fixture = _fixture(REPORT_REF, [REPORT_FILE], additions=784, deletions=0, pr={"labels": [{"name": "ai:security-pass-failed"}]})
	result, outputs, _ = _run_gate(tmp_path, fixture)
	_assert_reviewed(result, outputs)
	assert outputs["should_run"] == "true" and outputs["deterministic_skip"] == "false", result.stdout
	assert "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=security_pass_failed pr=42 candidate=docs_only" in result.stdout


def test_skip_does_not_depend_on_the_doc_only_or_small_diff_settings(tmp_path: Path) -> None:
	fixture = _fixture(REPORT_REF, [REPORT_FILE], additions=784, deletions=0)
	result, outputs, _ = _run_gate(tmp_path, fixture, AUTOFIX_SKIP_DOC_ONLY="false")
	_assert_skipped(result, outputs, "workflow_log_analysis_report")


def test_ordinary_docs_only_pr_still_needs_the_security_pass(tmp_path: Path) -> None:
	# The exemption is for the generators only: a docs-only PR on any other
	# branch keeps the skip job's security pass.
	fixture = _fixture("claude/docs", [{"filename": "docs/notes.md", "status": "modified"}])
	result, outputs, _ = _run_gate(tmp_path, fixture)
	assert result.returncode == 0, result.stderr
	assert outputs["det_skip_reason"] == "docs_only" and outputs["det_skip_security_pass"] == "true", result.stdout


def test_review_workflow_passes_the_switch() -> None:
	text = REVIEW_WF.read_text(encoding="utf-8")
	assert "AUTOFIX_SKIP_GENERATED_CHORE: ${{ vars.AUTOFIX_SKIP_GENERATED_CHORE }}" in text
	assert '"${AUTOFIX_SKIP_GENERATED_CHORE:-true}" != "false"' in GATE_SCRIPT.read_text(encoding="utf-8")
