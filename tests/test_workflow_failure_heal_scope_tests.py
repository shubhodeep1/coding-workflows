#!/usr/bin/env python3
"""Heal scope widening from the run's own failing tests (operator decision Q37: A).

A CI heal's frozen scope was the workflow file plus tests/**, so the
implementer could only rewrite tests; heal PRs #6907 and #6916 flipped the
security-pass cap rule that #6906 had restored. The failing test names a run
reports now resolve, deterministically and only at the scope commit, to the
test file and the subject it covers. The diagnosis still cannot add a path.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import workflow_failure_heal as heal  # noqa: E402

HEAL_PY = REPO_ROOT / "scripts" / "workflow_failure_heal.py"
SELF_REPO = "shubhodeep1/coding-workflows"

SHARD_LOG = """
TEST_CASE_EVENT: {"elapsed_ms":19453,"event":"complete","status":"fail","test_name":"test_security_pass_cap_converts_low_keep_fixing_to_advisory"}
  FAIL  test_security_pass_cap_converts_low_keep_fixing_to_advisory:
TEST_CASE_EVENT: {"elapsed_ms":10,"event":"complete","status":"pass","test_name":"test_other_passes"}
  PASS  test_other_passes
32 passed, 1 failed, 33 total
FAILED tests/test_pr_checks_lib_required_filter.py::test_required_failing_check_blocks - AssertionError
::error::1 orchestrate-poll shard(s) failed.
"""


def test_extract_failing_tests_reads_shard_pytest_and_unittest_shapes() -> None:
	found = heal.extract_failing_tests(SHARD_LOG)
	assert found["names"] == ["test_security_pass_cap_converts_low_keep_fixing_to_advisory", "test_required_failing_check_blocks"]
	assert found["files"] == ["tests/test_pr_checks_lib_required_filter.py"]
	assert heal.extract_failing_tests("All green. The word test_name appears in prose, as does FAIL and tests/test_x.py")["names"] == []


def test_extract_failing_tests_handles_timestamps_classes_and_mid_line_prose() -> None:
	text = ("2026-10-09T10:00:00.1234567Z   FAIL  test_stamped_case:\n"
		"2026-10-09T10:00:01Z FAILED tests/test_klass.py::TestThing::test_method - AssertionError\n"
		"We FAILED. tests/test_prose.py was the cause\n")
	found = heal.extract_failing_tests(text)
	assert found["names"] == ["test_stamped_case", "test_method"]
	assert found["files"] == ["tests/test_klass.py"]


def test_extract_failing_tests_is_capped_and_deduped() -> None:
	text = "\n".join(f"  FAIL  test_case_{i}:" for i in range(40)) + "\n  FAIL  test_case_1:\n"
	found = heal.extract_failing_tests(text)
	assert len(found["names"]) == heal.FAILING_TEST_LIMIT
	assert found["names"][1] == "test_case_1" and found["names"].count("test_case_1") == 1


def test_scope_subjects_map_test_files_to_existing_subjects_only() -> None:
	existing = {"tests/test_orchestrate_poll_process.py", "scripts/orchestrate_poll_process.sh", "tests/test_pr_checks_lib_required_filter.py",
		"tests/test_review_merge_train.py", ".github/workflows/review-merge-train.yml", "tests/test_hidden.py", ".claude/hooks/hidden.py"}
	out = heal.heal_scope_test_subjects(
		["tests/test_orchestrate_poll_process.py", "tests/test_pr_checks_lib_required_filter.py", "tests/test_review_merge_train.py",
		 "tests/test_missing.py", "tests/test_hidden.py", "scripts/not_a_test.py", "../tests/test_x.py"],
		exists=lambda path: path in existing)
	assert out == ["tests/test_orchestrate_poll_process.py", "scripts/orchestrate_poll_process.sh",
		"tests/test_pr_checks_lib_required_filter.py", "tests/test_review_merge_train.py", ".github/workflows/review-merge-train.yml", "tests/test_hidden.py"]
	assert ".claude/hooks/hidden.py" not in out


def test_render_marker_appends_test_subjects_before_the_globs() -> None:
	marker = heal.render_heal_scope_marker(crash_file=None, workflow_paths=[".github/workflows/ci.yml"], changed_files=[], runs=[f"{SELF_REPO}:1"],
		exists=lambda path: True, test_subjects=["tests/test_orchestrate_poll_process.py", "scripts/orchestrate_poll_process.sh"])
	assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,tests/test_orchestrate_poll_process.py,scripts/orchestrate_poll_process.sh,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"
	verified = heal.verify_heal_scope(body=marker, author_login="bot", last_edited_at=None, labels=[heal.HEAL_LABEL], pipeline_login="bot")
	assert verified["status"] == "verified" and "scripts/orchestrate_poll_process.sh" in verified["paths"]


def _init_scope_repo(root: Path) -> str:
	subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
	(root / "tests").mkdir(exist_ok=True)
	(root / "scripts").mkdir(exist_ok=True)
	(root / ".github/workflows").mkdir(parents=True, exist_ok=True)
	(root / "tests/test_orchestrate_poll_process.py").write_text("def test_security_pass_cap_converts_low_keep_fixing_to_advisory() -> None:\n\tpass\n")
	(root / "scripts/orchestrate_poll_process.sh").write_text("#!/usr/bin/env bash\n")
	(root / ".github/workflows/ci.yml").write_text("name: CI\n")
	subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
	subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init"], check=True)
	return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def test_render_cli_resolves_failing_test_names_at_the_scope_commit() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir) / "repo"
		sha = _init_scope_repo(root)
		log_file = Path(tmpdir) / "run-1-job-2.txt"
		log_file.write_text(SHARD_LOG)
		failing = subprocess.run([sys.executable, str(HEAL_PY), "heal-scope", "failing-tests", "--log-files", str(log_file), str(Path(tmpdir) / "missing.txt")],
			capture_output=True, text=True, check=True).stdout
		failing_json = json.loads(failing)
		assert failing_json["names"][0] == "test_security_pass_cap_converts_low_keep_fixing_to_advisory"
		inputs = Path(tmpdir) / "scope_inputs.json"
		inputs.write_text(json.dumps({"crash_file": "", "runs": [f"{SELF_REPO}:1"], "workflow_paths": [".github/workflows/ci.yml"], "changed_files": [], "failing_tests": failing_json}))
		marker = subprocess.run([sys.executable, str(HEAL_PY), "heal-scope", "render", "--input-json", str(inputs), "--checkout", str(root), "--ref", sha],
			capture_output=True, text=True, check=True).stdout.strip()
		# The pytest-style file from the log does not exist at the scope commit and is dropped; the
		# name resolves through git grep to its file, whose stem maps to the poller script.
		assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,tests/test_orchestrate_poll_process.py,scripts/orchestrate_poll_process.sh,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"


def test_render_cli_drops_log_reported_files_that_do_not_define_a_failing_test() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir) / "repo"
		(root / "tests").mkdir(parents=True)
		(root / "tests/test_unrelated.py").write_text("def test_other() -> None:\n\tpass\n")
		(root / "tests/test_klass.py").write_text("class TestThing:\n\tdef test_method(self) -> None:\n\t\tpass\n")
		(root / "scripts").mkdir()
		(root / "scripts/unrelated.sh").write_text("#!/usr/bin/env bash\n")
		(root / "scripts/klass.py").write_text("\n")
		sha = _init_scope_repo(root)
		inputs = Path(tmpdir) / "scope_inputs.json"
		inputs.write_text(json.dumps({"crash_file": "", "runs": [f"{SELF_REPO}:1"], "workflow_paths": [".github/workflows/ci.yml"], "changed_files": [],
			"failing_tests": {"names": ["test_method"], "files": ["tests/test_unrelated.py", "tests/test_klass.py"]}}))
		marker = subprocess.run([sys.executable, str(HEAL_PY), "heal-scope", "render", "--input-json", str(inputs), "--checkout", str(root), "--ref", sha],
			capture_output=True, text=True, check=True).stdout.strip()
		assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,tests/test_klass.py,scripts/klass.py,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"


def test_render_cli_without_failing_tests_is_unchanged() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir) / "repo"
		sha = _init_scope_repo(root)
		inputs = Path(tmpdir) / "scope_inputs.json"
		inputs.write_text(json.dumps({"crash_file": "", "runs": [f"{SELF_REPO}:1"], "workflow_paths": [".github/workflows/ci.yml"], "changed_files": []}))
		marker = subprocess.run([sys.executable, str(HEAL_PY), "heal-scope", "render", "--input-json", str(inputs), "--checkout", str(root), "--ref", sha],
			capture_output=True, text=True, check=True).stdout.strip()
		assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"


def test_intake_freezes_failing_tests_before_the_diagnosis() -> None:
	text = (REPO_ROOT / "scripts" / "workflow_failure_heal_intake.sh").read_text(encoding="utf-8")
	extract_at = text.index('heal-scope failing-tests --log-files "${LOG_DIR}"/run-*-job-*.txt')
	assert text.index('SCOPE_INPUTS_FROZEN="${RUNTIME_DIR}/scope_inputs_frozen.json"') < extract_at < text.index("jq -n --arg crash")
	assert text.index("failing_tests: $failing_tests") < text.index("# --- Run the diagnosis model")
	assert "failing_tests: (.failing_tests // {names: [], files: []})" in text


def test_prompt_tells_the_healer_how_to_pick_a_side() -> None:
	for path in ("prompts/mode-workflow-failure-heal.txt", "prompts/_templates/mode-workflow-failure-heal.txt"):
		text = (REPO_ROOT / path).read_text(encoding="utf-8")
		assert "decide which side is" in text and "never from \"the test should match what" in text, path
