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
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

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
	# Test files are covered by tests/**, so only their subjects take explicit slots.
	marker = heal.render_heal_scope_marker(crash_file=None, workflow_paths=[".github/workflows/ci.yml"], changed_files=[], runs=[f"{SELF_REPO}:1"],
		exists=lambda path: True, test_subjects=["tests/test_orchestrate_poll_process.py", "scripts/orchestrate_poll_process.sh"])
	assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,scripts/orchestrate_poll_process.sh,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"
	verified = heal.verify_heal_scope(body=marker, author_login="bot", last_edited_at=None, labels=[heal.HEAL_LABEL], pipeline_login="bot")
	assert verified["status"] == "verified" and "scripts/orchestrate_poll_process.sh" in verified["paths"]


def test_render_marker_keeps_subjects_ahead_of_the_cap_and_warns_on_truncation(capsys) -> None:
	subjects = [f"scripts/s{i}.sh" for i in range(25)]
	marker = heal.render_heal_scope_marker(crash_file=None, workflow_paths=[".github/workflows/ci.yml"], changed_files=[], runs=[f"{SELF_REPO}:1"],
		exists=lambda path: True, test_subjects=[item for i, subject in enumerate(subjects) for item in (f"tests/test_s{i}.py", subject)])
	paths = marker.split("paths=", 1)[1].split(" ", 1)[0].split(",")
	assert paths[:-2] == [".github/workflows/ci.yml", *subjects[:19]]
	assert "WORKFLOW_HEAL warn heal_scope_truncated kept=20 dropped_candidates=6" in capsys.readouterr().err
	verified = heal.verify_heal_scope(body=marker, author_login="bot", last_edited_at=None, labels=[heal.HEAL_LABEL], pipeline_login="bot")
	assert verified["status"] == "verified"


def test_render_marker_keeps_subjects_when_changed_files_fill_the_cap() -> None:
	changed = [f"scripts/changed_{i}.py" for i in range(25)]
	marker = heal.render_heal_scope_marker(crash_file="scripts/crash.sh", workflow_paths=[".github/workflows/ci.yml"], changed_files=changed, runs=[f"{SELF_REPO}:1"],
		exists=lambda path: True, test_subjects=["tests/test_orchestrate_poll_process.py", "scripts/orchestrate_poll_process.sh"])
	paths = marker.split("paths=", 1)[1].split(" ", 1)[0].split(",")
	assert paths[:3] == ["scripts/crash.sh", ".github/workflows/ci.yml", "scripts/orchestrate_poll_process.sh"]
	assert len(paths) == 22 and paths[3:-2] == changed[:17]
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
		assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,scripts/orchestrate_poll_process.sh,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"


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
		assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,scripts/klass.py,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"


def test_render_cli_drops_a_name_defined_in_several_files_unless_the_log_names_one() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir) / "repo"
		(root / "tests").mkdir(parents=True)
		(root / "tests/test_planted.py").write_text("def test_security_pass_cap_converts_low_keep_fixing_to_advisory() -> None:\n\tpass\n")
		(root / "scripts").mkdir()
		(root / "scripts/planted.sh").write_text("#!/usr/bin/env bash\n")
		sha = _init_scope_repo(root)
		inputs = Path(tmpdir) / "scope_inputs.json"
		base = {"crash_file": "", "runs": [f"{SELF_REPO}:1"], "workflow_paths": [".github/workflows/ci.yml"], "changed_files": []}
		name = "test_security_pass_cap_converts_low_keep_fixing_to_advisory"
		render = [sys.executable, str(HEAL_PY), "heal-scope", "render", "--input-json", str(inputs), "--checkout", str(root), "--ref", sha]
		inputs.write_text(json.dumps({**base, "failing_tests": {"names": [name], "files": []}}))
		marker = subprocess.run(render, capture_output=True, text=True, check=True).stdout.strip()
		assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"
		# A file reported only for another failure does not bind the ambiguous name.
		inputs.write_text(json.dumps({**base, "failing_tests": {"names": [name], "files": ["tests/test_orchestrate_poll_process.py"],
			"pairs": ["tests/test_orchestrate_poll_process.py::test_other"]}}))
		marker = subprocess.run(render, capture_output=True, text=True, check=True).stdout.strip()
		assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"
		inputs.write_text(json.dumps({**base, "failing_tests": {"names": [name], "files": ["tests/test_orchestrate_poll_process.py"],
			"pairs": [f"tests/test_orchestrate_poll_process.py::{name}"]}}))
		marker = subprocess.run(render, capture_output=True, text=True, check=True).stdout.strip()
		assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,scripts/orchestrate_poll_process.sh,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"


def test_extract_failing_tests_pairs_files_with_names_and_strips_parameters() -> None:
	text = ("FAILED tests/test_a.py::test_other - AssertionError\n"
		"FAILED tests/test_b.py::TestK::test_shared[case-1] - AssertionError\n"
		'TEST_CASE_EVENT: {"status":"fail","test_name":"test_param[x]"}\n')
	found = heal.extract_failing_tests(text)
	assert found["pairs"] == ["tests/test_a.py::test_other", "tests/test_b.py::test_shared"]
	assert found["names"] == ["test_param", "test_other", "test_shared"]


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


def _run_scope_freeze(log_dir: Path, runtime_dir: Path, *, linked_env: bool = True) -> subprocess.CompletedProcess:
	"""Run the intake's real freeze block (helper through the chmod) in bash.

	linked_env=False leaves out the variables the linked-run helpers (#8360)
	read, to prove the block still runs when they are unset (#8377).
	"""
	if shutil.which("jq") is None:
		pytest.skip("jq is required to run the intake's scope-freeze block")
	text = (REPO_ROOT / "scripts" / "workflow_failure_heal_intake.sh").read_text(encoding="utf-8")
	start = text.index('SCOPE_INPUTS_FROZEN="${RUNTIME_DIR}/scope_inputs_frozen.json"')
	end_marker = 'log "warn scope_inputs_freeze_failed"; }'
	block = text[start:text.index(end_marker, start) + len(end_marker)]
	script = 'set -euo pipefail\nlog() { echo "WORKFLOW_HEAL $*" >&2; }\n' + block + "\n"
	env = {
		"PATH": "/usr/bin:/bin:/usr/local/bin",
		"PYTHONDONTWRITEBYTECODE": "1",
		"RUNTIME_DIR": str(runtime_dir),
		"LOG_DIR": str(log_dir),
		"HEAL_PY": str(HEAL_PY),
		"PAYLOAD_CRASH_FILE": "",
		"CRASH_OWNERSHIP": "none",
		"SOURCE_REPO": SELF_REPO,
	}
	if linked_env:
		env.update({"MAX_LINKED_RUNS": "0", "SOURCE_KIND": "", "SELF_REPO": SELF_REPO, "SCOPE_LINKED_WORKFLOWS_ENABLED": "true"})
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=True)


def test_scope_freeze_writes_empty_runs_when_the_report_names_no_run() -> None:
	# A report with no run (e.g. a label-only ai:destructive-blocked escalation)
	# leaves no *.verified file. jq printed `[]` and then exited 2 on the
	# unmatched glob, the `|| echo '[]'` fallback appended a second `[]`, and
	# --argjson rejected it, so the freeze always logged scope_inputs_freeze_failed.
	with tempfile.TemporaryDirectory() as tmpdir:
		log_dir, runtime_dir = Path(tmpdir, "logs"), Path(tmpdir, "runtime")
		log_dir.mkdir()
		runtime_dir.mkdir()
		result = _run_scope_freeze(log_dir, runtime_dir)
		assert "scope_inputs_freeze_failed" not in result.stderr
		assert "invalid JSON text" not in result.stderr
		frozen = json.loads((runtime_dir / "scope_inputs_frozen.json").read_text(encoding="utf-8"))
		assert frozen["runs"] == [] and frozen["self_workflows"] == [] and frozen["consumer_workflows"] == []
		assert frozen["changed_files"] == [] and frozen["crash_file"] == ""


def test_scope_freeze_collects_runs_and_paths_from_verified_records() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		log_dir, runtime_dir = Path(tmpdir, "logs"), Path(tmpdir, "runtime")
		log_dir.mkdir()
		runtime_dir.mkdir()
		(log_dir / "run-12.json.verified").write_text(json.dumps({"run_id": 12, "path": ".github/workflows/ci.yml", "referenced_paths": ["scripts/a.sh"]}))
		(log_dir / "run-7.json.verified").write_text(json.dumps({"run_id": 7, "path": "", "referenced_paths": ["scripts/a.sh", "scripts/b.sh"]}))
		result = _run_scope_freeze(log_dir, runtime_dir)
		assert "scope_inputs_freeze_failed" not in result.stderr
		frozen = json.loads((runtime_dir / "scope_inputs_frozen.json").read_text(encoding="utf-8"))
		assert frozen["runs"] == [f"{SELF_REPO}:12", f"{SELF_REPO}:7"]
		assert frozen["self_workflows"] == ["scripts/a.sh", "scripts/b.sh"]
		assert frozen["consumer_workflows"] == [".github/workflows/ci.yml"]
		assert "WORKFLOW_HEAL scope_linked outcome=skipped paths=0 runs=0" in result.stderr


def test_scope_freeze_survives_unset_linked_run_variables() -> None:
	# #8377: the linked-run helpers sit inside the freeze block; a bare ${VAR}
	# under set -u killed the shell even inside the `|| log` guard.
	with tempfile.TemporaryDirectory() as tmpdir:
		log_dir, runtime_dir = Path(tmpdir, "logs"), Path(tmpdir, "runtime")
		log_dir.mkdir()
		runtime_dir.mkdir()
		result = _run_scope_freeze(log_dir, runtime_dir, linked_env=False)
		assert result.returncode == 0
		assert "unbound variable" not in result.stderr
		assert "WORKFLOW_HEAL scope_linked outcome=skipped paths=0 runs=0" in result.stderr
		assert "scope_inputs_freeze_failed" not in result.stderr
		frozen = json.loads((runtime_dir / "scope_inputs_frozen.json").read_text(encoding="utf-8"))
		assert frozen["self_workflows"] == []


def test_prompt_tells_the_healer_how_to_pick_a_side() -> None:
	for path in ("prompts/mode-workflow-failure-heal.txt", "prompts/_templates/mode-workflow-failure-heal.txt"):
		text = (REPO_ROOT / path).read_text(encoding="utf-8")
		assert "decide which side is" in text and "never from \"the test should match what" in text, path


# Guard tests (Q39: C, item 11): a guard checks other files, so its own file
# name maps to no subject and the heal was confined to tests/**, where the
# guard cannot be fixed. The size guard maps to the workflow files at or
# above the guard at the scope commit, the split registries, and one
# step-script glob per oversized workflow.

def _init_guard_repo(root: Path, *, oversized: dict[str, int]) -> str:
	(root / "tests").mkdir(parents=True)
	(root / "tests/test_workflow_file_size_limit.py").write_text("def test_every_workflow_file_is_below_size_guard() -> None:\n\tpass\n")
	(root / "tests/test_ci_job_split_contract.py").write_text("def test_no_step_runs_in_more_than_one_job() -> None:\n\tpass\n")
	(root / ".github/workflows").mkdir(parents=True)
	for name, size in oversized.items():
		(root / ".github/workflows" / name).write_text("#" * (size - 1) + "\n")
	(root / ".github/workflows/small.yml").write_text("name: small\n")
	(root / "docs").mkdir()
	(root / "docs/INVENTORY.md").write_text("# Inventory\n")
	(root / "scripts").mkdir()
	(root / "scripts/stage_workflow_support.sh").write_text("#!/usr/bin/env bash\n")
	return _init_scope_repo(root)


def _render_guard(root: Path, sha: str, tmpdir: str, names: list[str]) -> str:
	inputs = Path(tmpdir) / "scope_inputs.json"
	inputs.write_text(json.dumps({"crash_file": "", "runs": [f"{SELF_REPO}:1"], "workflow_paths": [".github/workflows/ci.yml"], "changed_files": [],
		"failing_tests": {"names": names, "files": []}}))
	return subprocess.run([sys.executable, str(HEAL_PY), "heal-scope", "render", "--input-json", str(inputs), "--checkout", str(root), "--ref", sha],
		capture_output=True, text=True, check=True).stdout.strip()


def test_size_guard_failure_scopes_the_oversized_workflow_its_registries_and_step_scripts() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir) / "repo"
		sha = _init_guard_repo(root, oversized={"review_autofix.yml": heal.HEAL_SCOPE_WORKFLOW_SIZE_GUARD_BYTES, "issue-pr-status.yml": heal.HEAL_SCOPE_WORKFLOW_SIZE_GUARD_BYTES + 5})
		marker = _render_guard(root, sha, tmpdir, ["test_every_workflow_file_is_below_size_guard"])
		assert marker == (f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,.github/workflows/issue-pr-status.yml,"
			".github/workflows/review_autofix.yml,scripts/stage_workflow_support.sh,docs/INVENTORY.md,"
			"scripts/issue_pr_status_step_*.sh,scripts/review_autofix_step_*.sh,tests/**,changelog.d/*.md "
			f"runs={SELF_REPO}:1 -->")
		verified = heal.verify_heal_scope(body=marker, author_login="bot", last_edited_at=None, labels=[heal.HEAL_LABEL], pipeline_login="bot")
		assert verified["status"] == "verified"
		from files_touched_scope_guard import entry_matches
		assert any(entry_matches(entry, "scripts/review_autofix_step_move_big_body.sh") for entry in verified["paths"])
		assert not any(entry_matches(entry, "scripts/orchestrate_poll_process.sh") for entry in verified["paths"])
		assert not any(entry_matches(entry, "scripts/review_autofix_step_x.py") for entry in verified["paths"])


def test_size_guard_without_an_oversized_workflow_adds_nothing() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir) / "repo"
		sha = _init_guard_repo(root, oversized={"review_autofix.yml": heal.HEAL_SCOPE_WORKFLOW_SIZE_GUARD_BYTES - 1})
		marker = _render_guard(root, sha, tmpdir, ["test_every_workflow_file_is_below_size_guard"])
		assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"


def test_ci_contract_guards_scope_ci_yml() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir) / "repo"
		sha = _init_guard_repo(root, oversized={})
		inputs = Path(tmpdir) / "scope_inputs.json"
		inputs.write_text(json.dumps({"crash_file": "", "runs": [f"{SELF_REPO}:1"], "workflow_paths": [], "changed_files": [],
			"failing_tests": {"names": ["test_no_step_runs_in_more_than_one_job"], "files": []}}))
		marker = subprocess.run([sys.executable, str(HEAL_PY), "heal-scope", "render", "--input-json", str(inputs), "--checkout", str(root), "--ref", sha],
			capture_output=True, text=True, check=True).stdout.strip()
		assert marker == f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,tests/**,changelog.d/*.md runs={SELF_REPO}:1 -->"


def test_ci_poll_sharding_guard_scopes_ci_and_both_release_gates() -> None:
	wfs = (".github/workflows/ci.yml", ".github/workflows/mark-stable.yml", ".github/workflows/test-and-mark-stable.yml")
	out = heal.heal_scope_guard_subjects(["tests/test_ci_poll_test_sharding.py"], exists=lambda path: True, oversized_workflows=lambda: [])
	assert out == list(wfs)
	# Every subject must exist at the scope commit.
	assert heal.heal_scope_guard_subjects(["tests/test_ci_poll_test_sharding.py"], exists=lambda path: path == wfs[0], oversized_workflows=lambda: []) == [wfs[0]]


def test_ci_structure_guards_scope_ci_yml() -> None:
	for guard in ("tests/test_ci_job_split_contract.py", "tests/test_ci_inventory_parity_order_contract.py", "tests/test_ci_shared_shell_block_guard.py"):
		assert heal.heal_scope_guard_subjects([guard], exists=lambda path: True, oversized_workflows=lambda: []) == [".github/workflows/ci.yml"], guard


def test_oversized_workflows_read_the_checkout_without_a_ref() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir) / "repo"
		_init_guard_repo(root, oversized={"review_autofix.yml": heal.HEAL_SCOPE_WORKFLOW_SIZE_GUARD_BYTES})
		assert heal._heal_oversized_workflows(str(root), None) == [".github/workflows/review_autofix.yml"]
		assert heal._heal_oversized_workflows(str(root), "no-such-ref") == []


def test_step_script_glob_is_the_only_extra_glob_a_scope_accepts() -> None:
	assert heal.heal_scope_step_script_glob(".github/workflows/review_autofix.yml") == "scripts/review_autofix_step_*.sh"
	assert heal.heal_scope_step_script_glob(".github/workflows/Issue-PR-status.yaml") == "scripts/issue_pr_status_step_*.sh"
	for bad in ("", "review_autofix.yml", ".github/workflows/../x.yml", ".github/workflows/a/b.yml", "scripts/x.sh"):
		assert heal.heal_scope_step_script_glob(bad) == "", bad
	runs = f"runs={SELF_REPO}:1 -->"
	for bad_glob in ("scripts/*.sh", "scripts/*_step_*.sh", ".github/workflows/*.yml", "scripts/x_step_*.py", "scripts/a/b_step_*.sh", "scripts/x_step_**.sh"):
		body = f"<!-- ai:workflow-heal-scope:v1 paths=.github/workflows/ci.yml,{bad_glob},tests/**,changelog.d/*.md {runs}"
		assert heal.verify_heal_scope(body=body, author_login="bot", last_edited_at=None, labels=[heal.HEAL_LABEL], pipeline_login="bot")["status"] == "malformed", bad_glob
	# A step glob is accepted only as a guard subject, never from ownership inputs.
	marker = heal.render_heal_scope_marker(crash_file="scripts/x_step_*.sh", workflow_paths=[".github/workflows/ci.yml"], changed_files=["scripts/y_step_*.sh"],
		exists=lambda path: True, runs=[f"{SELF_REPO}:1"])
	assert "_step_*" not in marker


def test_size_guard_threshold_matches_the_guard_test() -> None:
	import importlib.util
	spec = importlib.util.spec_from_file_location("size_guard", REPO_ROOT / "tests" / "test_workflow_file_size_limit.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	assert heal.HEAL_SCOPE_WORKFLOW_SIZE_GUARD_BYTES == module.WORKFLOW_FILE_SIZE_GUARD_BYTES
	assert (REPO_ROOT / heal.HEAL_SCOPE_WORKFLOW_SIZE_GUARD_TEST).is_file()
	assert "def test_every_workflow_file_is_below_size_guard(" in (REPO_ROOT / heal.HEAL_SCOPE_WORKFLOW_SIZE_GUARD_TEST).read_text(encoding="utf-8")
	for registry in heal.HEAL_SCOPE_WORKFLOW_SPLIT_REGISTRIES:
		assert (REPO_ROOT / registry).is_file(), registry
	for test_file, subjects in heal.HEAL_SCOPE_GUARD_TEST_SUBJECTS.items():
		# A key that names no real test can never match a failing run.
		assert (REPO_ROOT / test_file).is_file(), test_file
		for subject in subjects:
			assert (REPO_ROOT / subject).is_file(), (test_file, subject)


SCRIPT_RUN_LOG = """2026-10-10T01:00:00.0000000Z FAIL: test_alpha (__main__.T.test_alpha)
2026-10-10T01:00:00.0000000Z ----------------------------------------------------------------------
2026-10-10T01:00:00.0000000Z Traceback (most recent call last):
2026-10-10T01:00:00.0000000Z   File "/home/runner/work/coding-workflows/coding-workflows/tests/test_ut.py", line 4, in test_alpha
2026-10-10T01:00:00.0000000Z ERROR: test_gamma (test_mod.TestK.test_gamma)
2026-10-10T01:00:01.0000000Z Traceback (most recent call last):
2026-10-10T01:00:01.0000000Z   File "/home/runner/work/coding-workflows/coding-workflows/tests/test_script.py", line 8, in <module>
2026-10-10T01:00:01.0000000Z   File "/home/runner/work/coding-workflows/coding-workflows/tests/test_script.py", line 2, in test_beta
2026-10-10T01:00:01.0000000Z AssertionError: x
"""


def test_extract_failing_tests_reads_script_run_tracebacks_and_unittest_headers() -> None:
	found = heal.extract_failing_tests(SCRIPT_RUN_LOG)
	assert found["names"] == ["test_alpha", "test_beta", "test_gamma"]
	assert found["files"] == ["tests/test_ut.py", "tests/test_script.py"]
	assert found["pairs"] == ["tests/test_ut.py::test_alpha", "tests/test_script.py::test_beta"]
	# Nested test directories are read too, like pytest's FAILED lines.
	assert heal.extract_failing_tests('  File "/w/repo/tests/integration/test_n.py", line 2, in test_nested\n')["pairs"] == ["tests/integration/test_n.py::test_nested"]
	# A helper frame or a quoted file name in prose is not a failing test.
	assert heal.extract_failing_tests('  File "/x/tests/test_a.py", line 3, in helper\nsee File "tests/test_b.py", line 1, in test_c here\n')["names"] == []


def test_real_size_guard_failure_output_resolves_to_its_subjects() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		root = Path(tmpdir) / "repo"
		sha = _init_guard_repo(root, oversized={"review_autofix.yml": heal.HEAL_SCOPE_WORKFLOW_SIZE_GUARD_BYTES})
		(root / "tests/test_workflow_file_size_limit.py").write_text((REPO_ROOT / "tests/test_workflow_file_size_limit.py").read_text(encoding="utf-8"))
		subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
		subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "guard"], check=True)
		sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
		# The way ci.yml runs it: as a script.
		run = subprocess.run([sys.executable, "tests/test_workflow_file_size_limit.py"], cwd=root, capture_output=True, text=True,
			env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
		assert run.returncode != 0
		found = heal.extract_failing_tests(run.stdout + run.stderr)
		assert "test_every_workflow_file_is_below_size_guard" in found["names"]
		marker = _render_guard(root, sha, tmpdir, found["names"])
		assert ",.github/workflows/review_autofix.yml,scripts/stage_workflow_support.sh,docs/INVENTORY.md,scripts/review_autofix_step_*.sh,tests/**," in marker


# ---------------------------------------------------------------------------
# Issue #8360: workflows and scripts a release run's linked runs used
# ---------------------------------------------------------------------------
# Heals #8287, #8241 and #8327 diagnosed a defect in a workflow that a release
# gate step only dispatched or polled, then sat at heal_scope_plan_out_of_scope
# because the frozen scope held only the failing release workflow. For a
# release or promotion `workflow_run` report of this repository, each
# same-repository linked run (#8248) now adds its own workflow file, the
# reusable workflows that file calls (one hop) and the scripts its failed or
# longest step runs, all read from the scope-commit tree. Workflow fixtures are
# written as JSON, which is valid YAML, so yaml.safe_load parses them as it
# parses the block style. The intake tests reuse the shared intake harness.

sys.path.insert(0, str(Path(__file__).resolve().parent))


class _LwIntakeHarness:
	"""Imports test_workflow_failure_heal on first use.

	That module imports PyYAML at import time; importing it lazily keeps this
	file collectable where PyYAML is not installed, so only the intake tests
	that use the harness depend on it.
	"""

	def __getattr__(self, name: str):
		import test_workflow_failure_heal as harness_module

		return getattr(harness_module, name)


base = _LwIntakeHarness()

# Same value as test_workflow_failure_heal.CONSUMER_REPO (a foreign repository).
LW_CONSUMER_REPO = "shubhodeep1/example-consumer"

try:
	import yaml as _lw_real_yaml  # noqa: F401
	LW_HAVE_YAML = True
except ImportError:  # CI installs PyYAML; offline fallback below
	LW_HAVE_YAML = False

# Offline stand-in for PyYAML: every workflow fixture here is JSON (valid
# YAML), so json.loads parses it the way yaml.safe_load does. Used only when
# PyYAML is missing; CI runs the real parser.
LW_YAML_STUB_SOURCE = (
	"import json\n\n\n"
	"class YAMLError(Exception):\n\tpass\n\n\n"
	"def safe_load(text):\n\treturn json.loads(text)\n"
)


@pytest.fixture
def _lw_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
	if LW_HAVE_YAML:
		return
	stub = types.ModuleType("yaml")
	exec(LW_YAML_STUB_SOURCE, stub.__dict__)  # noqa: S102 - fixed source above
	monkeypatch.setitem(sys.modules, "yaml", stub)
LW_INTAKE_SCRIPT = REPO_ROOT / "scripts" / "workflow_failure_heal_intake.sh"
LW_INTAKE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow-failure-heal-intake.yml"

LW_CANCEL_RUN_ID = 40814653574
LW_MEMORY_RUN_ID = 40752760153


def _lw_workflow(jobs: dict) -> str:
	return json.dumps({"name": "fixture", "on": {"workflow_call": {}}, "jobs": jobs}, indent=1) + "\n"


# Shapes of the real workflows the replays name (job ids, uses: lines and the
# step names are copied from the repository's files).
LW_INTERNAL_CANCEL = _lw_workflow({
	"cancel": {"uses": f"{SELF_REPO}/.github/workflows/cancel_on_pr_close.yml@promote-candidate", "secrets": "inherit"},
	"heal-pr-reconcile": {"runs-on": "ubuntu-latest", "steps": [{"name": "Reconcile heal PRs", "run": "bash scripts/workflow_failure_heal_pr_reconcile.sh"}]},
})
LW_CANCEL_ON_PR_CLOSE = _lw_workflow({
	"cancel": {"runs-on": "ubuntu-latest", "steps": [
		{"name": "Checkout", "uses": "actions/checkout@v5"},
		{"name": "Cancel queued runs for the closed PR", "run": "set -euo pipefail\nbash scripts/cancel_on_pr_close.sh \"${PR_NUMBER}\"\n"},
	]},
})
LW_INTERNAL_MEMORY = _lw_workflow({
	"memory": {"uses": f"{SELF_REPO}/.github/workflows/memory_maintenance.yml@promote-candidate", "secrets": "inherit"},
})
LW_MEMORY_MAINTENANCE = _lw_workflow({
	"maintain": {"runs-on": "ubuntu-latest", "steps": [
		{"name": "Resolve workflow support ref", "run": "echo ref"},
		{"name": "Checkout repository", "uses": "actions/checkout@v5"},
		{"name": "Extract repository learnings (fail-open)", "run": "python3 scripts/memory_maintenance_extract_learnings.py \\\n  --repo \"${GITHUB_REPOSITORY}\"\n"},
		{"name": "Run memory compaction", "run": "python3 scripts/ai_memory_compact.py"},
	]},
})
LW_RELEASE_WORKFLOW = _lw_workflow({"e2e": {"runs-on": "ubuntu-latest", "steps": [{"name": "Wait", "run": "bash scripts/stable_e2e_step_wait_review.sh"}]}})

LW_TREE = {
	".github/workflows/test-and-mark-stable.yml": LW_RELEASE_WORKFLOW,
	".github/workflows/internal-cancel-on-pr-close.yml": LW_INTERNAL_CANCEL,
	".github/workflows/cancel_on_pr_close.yml": LW_CANCEL_ON_PR_CLOSE,
	".github/workflows/internal-memory-maintenance.yml": LW_INTERNAL_MEMORY,
	".github/workflows/memory_maintenance.yml": LW_MEMORY_MAINTENANCE,
	"scripts/cancel_on_pr_close.sh": "#!/bin/bash\n",
	"scripts/memory_maintenance_extract_learnings.py": "print('x')\n",
	"scripts/ai_memory_compact.py": "print('y')\n",
	"scripts/workflow_failure_heal_pr_reconcile.sh": "#!/bin/bash\n",
}


def _lw_fakes(tree: dict[str, str] | None = None):
	files = dict(LW_TREE if tree is None else tree)
	return (lambda path: path in files), (lambda path: files.get(path))


def _lw_run(run_id: int, path: str, *, repo: str = SELF_REPO, head_repo: str | None = SELF_REPO, **extra) -> dict:
	run = {"id": run_id, "repository": {"full_name": repo}, "path": path, "name": "linked", "status": "completed",
		"conclusion": "failure", "created_at": "2026-10-28T10:00:00Z", "updated_at": "2026-10-28T10:40:00Z",
		"html_url": f"https://github.com/{repo}/actions/runs/{run_id}"}
	if head_repo is not None:
		run["head_repository"] = {"full_name": head_repo}
	run.update(extra)
	return run


def _lw_step(name: str, conclusion: str, start: str = "2026-10-28T10:01:00Z", end: str = "2026-10-28T10:02:00Z") -> dict:
	return {"name": name, "status": "completed", "conclusion": conclusion, "started_at": start, "completed_at": end}


def _lw_jobs(*steps: dict, job_name: str = "cancel / cancel") -> dict:
	return {"total_count": 1, "jobs": [{"id": 7700, "name": job_name, "status": "completed", "conclusion": "failure", "steps": list(steps)}]}


def _lw_paths(run: dict, jobs: dict, tree: dict[str, str] | None = None) -> dict:
	exists, read_file = _lw_fakes(tree)
	return heal.heal_scope_linked_workflow_paths(run, jobs, repo=SELF_REPO, exists=exists, read_file=read_file)


def _lw_reasons(result: dict) -> list[str]:
	return [entry["reason"] for entry in result["skipped"]]


# --- heal_scope_linked_workflow_paths ---------------------------------------


@pytest.mark.usefixtures("_lw_yaml")

def test_internal_wrapper_resolves_to_itself_plus_the_called_workflow_and_its_step_script() -> None:
	jobs = _lw_jobs(_lw_step("Checkout", "success"), _lw_step("Cancel queued runs for the closed PR", "failure"))
	result = _lw_paths(_lw_run(LW_CANCEL_RUN_ID, ".github/workflows/internal-cancel-on-pr-close.yml@refs/heads/promote-candidate"), jobs)
	assert result == {"paths": [
		".github/workflows/internal-cancel-on-pr-close.yml",
		".github/workflows/cancel_on_pr_close.yml",
		"scripts/cancel_on_pr_close.sh",
	], "skipped": []}


@pytest.mark.usefixtures("_lw_yaml")

def test_local_uses_form_is_accepted_and_the_repo_compares_case_insensitively() -> None:
	tree = dict(LW_TREE)
	tree[".github/workflows/wrapper.yml"] = _lw_workflow({"a": {"uses": "./.github/workflows/cancel_on_pr_close.yml"}})
	result = _lw_paths(_lw_run(1, ".github/workflows/wrapper.yml", repo=SELF_REPO.upper()), _lw_jobs(_lw_step("x", "success")), tree)
	assert result["paths"][:2] == [".github/workflows/wrapper.yml", ".github/workflows/cancel_on_pr_close.yml"]
	tree[".github/workflows/wrapper.yml"] = _lw_workflow({"a": {"uses": f"{SELF_REPO.upper()}/.github/workflows/cancel_on_pr_close.yml@main"}})
	assert ".github/workflows/cancel_on_pr_close.yml" in _lw_paths(_lw_run(1, ".github/workflows/wrapper.yml"), _lw_jobs(), tree)["paths"]


@pytest.mark.usefixtures("_lw_yaml")

def test_failed_step_names_the_extraction_script() -> None:
	jobs = _lw_jobs(_lw_step("Resolve workflow support ref", "success"), _lw_step("Extract repository learnings (fail-open)", "failure", end="2026-10-28T10:01:30Z"),
		_lw_step("Run memory compaction", "success", end="2026-10-28T10:30:00Z"), job_name="memory / maintain")
	result = _lw_paths(_lw_run(LW_MEMORY_RUN_ID, ".github/workflows/internal-memory-maintenance.yml"), jobs)
	assert result["paths"] == [
		".github/workflows/internal-memory-maintenance.yml",
		".github/workflows/memory_maintenance.yml",
		"scripts/memory_maintenance_extract_learnings.py",
	]
	assert "scripts/ai_memory_compact.py" not in result["paths"]


@pytest.mark.usefixtures("_lw_yaml")

def test_without_a_failed_step_the_longest_step_is_used() -> None:
	jobs = _lw_jobs(
		_lw_step("Extract repository learnings (fail-open)", "success", end="2026-10-28T10:01:30Z"),
		_lw_step("Run memory compaction", "cancelled", start="2026-10-28T10:02:00Z", end="2026-10-28T10:35:00Z"),
		_lw_step("Skipped", "skipped", start="2026-10-28T09:00:00Z", end="2026-10-28T12:00:00Z"),
	)
	result = _lw_paths(_lw_run(LW_MEMORY_RUN_ID, ".github/workflows/internal-memory-maintenance.yml"), jobs)
	assert result["paths"][-1] == "scripts/ai_memory_compact.py"
	assert "scripts/memory_maintenance_extract_learnings.py" not in result["paths"]


@pytest.mark.usefixtures("_lw_yaml")
@pytest.mark.parametrize(
	("run", "reason"),
	[
		(_lw_run(5, ".github/workflows/internal-cancel-on-pr-close.yml", repo="someone/else"), "other_repository"),
		(_lw_run(5, ".github/workflows/internal-cancel-on-pr-close.yml", head_repo="forker/coding-workflows"), "fork_head"),
		(_lw_run(5, ".github/workflows/nested/x.yml"), "path_malformed"),
		(_lw_run(5, ".github/workflows/../x.yml"), "path_malformed"),
		(_lw_run(5, ".github/workflows/x.txt"), "path_malformed"),
		(_lw_run(5, ".github/ai/x.yml"), "path_malformed"),
		(_lw_run(5, None), "path_malformed"),
		(_lw_run(5, ".github/workflows/missing.yml"), "file_missing"),
		({"id": "5", "repository": {"full_name": SELF_REPO}}, "run_malformed"),
		({"id": 5, "repository": {}}, "run_malformed"),
		("not a run", "run_malformed"),
	],
)
def test_identity_and_path_rejections_add_nothing(run, reason: str) -> None:
	result = _lw_paths(run, _lw_jobs(_lw_step("x", "failure")))
	assert result["paths"] == []
	assert _lw_reasons(result) == [reason]


def test_unsafe_own_path_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(heal, "_safe_heal_path", lambda path: False)
	result = _lw_paths(_lw_run(5, ".github/workflows/internal-cancel-on-pr-close.yml"), _lw_jobs())
	assert result == {"paths": [], "skipped": [{"run_id": "5", "reason": "path_unsafe"}]}


@pytest.mark.usefixtures("_lw_yaml")

def test_foreign_uses_missing_callee_missing_script_and_unmatched_step() -> None:
	tree = dict(LW_TREE)
	tree[".github/workflows/wrapper.yml"] = _lw_workflow({
		"action": {"uses": "actions/checkout@v5"},
		"other": {"uses": "someone/else/.github/workflows/x.yml@main"},
		"traversal": {"uses": "./.github/workflows/..yml"},
		"gone": {"uses": "./.github/workflows/gone.yml"},
		"run": {"runs-on": "ubuntu-latest", "steps": [{"name": "Gate", "run": "bash scripts/missing.sh && bash scripts/cancel_on_pr_close.sh"}]},
	})
	result = _lw_paths(_lw_run(9, ".github/workflows/wrapper.yml"), _lw_jobs(_lw_step("Gate", "timed_out")), tree)
	assert result["paths"] == [".github/workflows/wrapper.yml", "scripts/cancel_on_pr_close.sh"]
	assert _lw_reasons(result) == ["uses_foreign", "uses_foreign", "uses_foreign", "file_missing", "script_missing"]
	unmatched = _lw_paths(_lw_run(9, ".github/workflows/wrapper.yml"), _lw_jobs(_lw_step("Run bash scripts/cancel_on_pr_close.sh", "failure")), tree)
	assert "step_unmatched" in _lw_reasons(unmatched) and "scripts/cancel_on_pr_close.sh" not in unmatched["paths"]


@pytest.mark.usefixtures("_lw_yaml")

def test_only_one_hop_is_followed() -> None:
	tree = dict(LW_TREE)
	tree[".github/workflows/wrapper.yml"] = _lw_workflow({"a": {"uses": "./.github/workflows/middle.yml"}})
	tree[".github/workflows/middle.yml"] = _lw_workflow({"b": {"uses": "./.github/workflows/cancel_on_pr_close.yml"}})
	result = _lw_paths(_lw_run(9, ".github/workflows/wrapper.yml"), _lw_jobs(_lw_step("x", "failure")), tree)
	assert result["paths"] == [".github/workflows/wrapper.yml", ".github/workflows/middle.yml"]


@pytest.mark.usefixtures("_lw_yaml")

def test_unreadable_or_oversized_yaml_keeps_only_the_own_workflow() -> None:
	for text in ("{not: [valid", "just a string\n", None, "#" * (heal.HEAL_SCOPE_LINKED_YAML_MAX_BYTES + 1)):
		tree = {".github/workflows/w.yml": text}

		def exists(path, tree=tree):
			return path in tree
		result = heal.heal_scope_linked_workflow_paths(_lw_run(9, ".github/workflows/w.yml"), _lw_jobs(), repo=SELF_REPO, exists=exists, read_file=tree.get)
		assert result == {"paths": [".github/workflows/w.yml"], "skipped": [{"run_id": "9", "reason": "yaml_unreadable"}]}


def test_yaml_unavailable_adds_only_the_own_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setitem(sys.modules, "yaml", None)
	result = _lw_paths(_lw_run(LW_CANCEL_RUN_ID, ".github/workflows/internal-cancel-on-pr-close.yml"), _lw_jobs(_lw_step("Cancel queued runs for the closed PR", "failure")))
	assert result == {"paths": [".github/workflows/internal-cancel-on-pr-close.yml"], "skipped": [{"run_id": str(LW_CANCEL_RUN_ID), "reason": "yaml_unavailable"}]}


@pytest.mark.usefixtures("_lw_yaml")

def test_scripts_per_step_are_capped() -> None:
	tree = dict(LW_TREE)
	body = " ".join(f"bash scripts/s{index}.sh;" for index in range(24))
	tree[".github/workflows/w.yml"] = _lw_workflow({"j": {"runs-on": "ubuntu-latest", "steps": [{"name": "Many", "run": body}]}})
	tree.update({f"scripts/s{index}.sh": "x" for index in range(24)})
	result = _lw_paths(_lw_run(9, ".github/workflows/w.yml"), _lw_jobs(_lw_step("Many", "failure")), tree)
	# The per-step cap counts existing scripts of this step only, not the run's own workflow.
	assert result["paths"][1:] == [f"scripts/s{index}.sh" for index in range(heal.HEAL_SCOPE_LINKED_SCRIPTS_PER_STEP)]
	assert _lw_reasons(result) == ["cap_reached"]
	tree.pop("scripts/s0.sh")
	result = _lw_paths(_lw_run(9, ".github/workflows/w.yml"), _lw_jobs(_lw_step("Many", "failure")), tree)
	assert result["paths"][1:] == [f"scripts/s{index}.sh" for index in range(1, heal.HEAL_SCOPE_LINKED_SCRIPTS_PER_STEP + 1)]
	assert _lw_reasons(result) == ["script_missing", "cap_reached"]


@pytest.mark.usefixtures("_lw_yaml")

def test_merged_runs_dedupe_keep_order_and_cap(tmp_path: Path) -> None:
	tree = dict(LW_TREE)
	pairs = []
	for index in range(8):
		name = f".github/workflows/w{index}.yml"
		tree[name] = _lw_workflow({"j": {"runs-on": "ubuntu-latest", "steps": [{"name": "Gate", "run": f"bash scripts/g{index}.sh"}]}})
		tree[f"scripts/g{index}.sh"] = "x"
		run_file, jobs_file = tmp_path / f"run-{index}.json", tmp_path / f"run-{index}-jobs.json"
		run_file.write_text(json.dumps(_lw_run(100 + index, name)))
		jobs_file.write_text(json.dumps(_lw_jobs(_lw_step("Gate", "failure"))))
		pairs.append((str(run_file), str(jobs_file)))
	pairs.insert(1, pairs[0])  # the same run twice adds nothing new
	exists, read_file = _lw_fakes(tree)
	result = heal.heal_scope_linked_paths_for_runs(pairs, repo=SELF_REPO, exists=exists, read_file=read_file)
	assert result["paths"] == [path for index in range(6) for path in (f".github/workflows/w{index}.yml", f"scripts/g{index}.sh")]
	assert len(result["paths"]) == heal.HEAL_SCOPE_LINKED_PATHS_MAX
	assert _lw_reasons(result).count("cap_reached") == 4
	assert result["outcome"] == "added" and result["runs"] == 9
	broken = heal.heal_scope_linked_paths_for_runs([(str(tmp_path / "absent.json"), "")], repo=SELF_REPO, exists=exists, read_file=read_file)
	assert broken == {"outcome": "none", "paths": [], "skipped": [{"run_id": "?", "reason": "cache_unreadable"}], "runs": 1}


def _lw_freeze_filter() -> str:
	"""The intake's scope-freeze jq program, as shipped."""
	text = LW_INTAKE_SCRIPT.read_text(encoding="utf-8")
	start = text.index("'($self_workflows + $engine_paths) as $base") + 1
	end = text.index("}' > \"${SCOPE_INPUTS_FROZEN}\"", start) + 1
	return text[start:end]


@pytest.mark.parametrize(("crash", "engine_paths"), [("", []), ("scripts/crash.sh", []), ("", ["scripts/ai_engine.sh", "scripts/claude_engine.py"])])
def test_linked_paths_at_the_cap_leave_the_subject_reserve_in_the_marker(crash: str, engine_paths: list[str]) -> None:
	# #6907/#6916: the source workflow, engine and crash paths sit ahead of the
	# subject reserve too, so the freeze trims the linked paths to the room left.
	if shutil.which("jq") is None:
		pytest.skip("jq is required to run the intake's scope-freeze filter")
	assert heal.HEAL_SCOPE_LINKED_PATHS_MAX + heal.HEAL_SCOPE_SUBJECT_RESERVE == 20
	source = ".github/workflows/test-and-mark-stable.yml"
	linked = [f"scripts/linked{index}.sh" for index in range(heal.HEAL_SCOPE_LINKED_PATHS_MAX)]
	subjects = [f"scripts/subject{index}.sh" for index in range(heal.HEAL_SCOPE_SUBJECT_RESERVE)]
	args = {"failing_tests": {"names": [], "files": []}, "engine_runs": [], "engine_paths": engine_paths, "linked_paths": linked,
		"runs": [f"{SELF_REPO}:500"], "self_workflows": [source], "consumer_workflows": [], "changed": []}
	cmd = ["jq", "-n", "--arg", "crash", crash]
	for key, value in args.items():
		cmd += ["--argjson", key, json.dumps(value)]
	frozen = json.loads(subprocess.run([*cmd, _lw_freeze_filter()], capture_output=True, text=True, check=True).stdout)
	assert frozen["self_workflows"][:1 + len(engine_paths)] == [source, *engine_paths]
	assert len(frozen["self_workflows"]) + (1 if crash else 0) == heal.HEAL_SCOPE_LINKED_PATHS_MAX
	marker = heal.render_heal_scope_marker(crash_file=crash or None, workflow_paths=frozen["self_workflows"],
		changed_files=[], exists=lambda path: True, runs=frozen["runs"], test_subjects=subjects)
	assert marker
	for path in subjects:
		assert path in marker
	# Without linked paths the freeze output is unchanged.
	empty = dict(args, linked_paths=[])
	cmd = ["jq", "-n", "--arg", "crash", crash]
	for key, value in empty.items():
		cmd += ["--argjson", key, json.dumps(value)]
	frozen_empty = json.loads(subprocess.run([*cmd, _lw_freeze_filter()], capture_output=True, text=True, check=True).stdout)
	assert frozen_empty["self_workflows"] == [source, *engine_paths]


# --- heal_scope_linked_workflows_gate --------------------------------------


def _lw_gate(**overrides) -> str:
	kwargs = {"enabled": "true", "source_kind": "workflow_run", "source_repo": SELF_REPO, "self_repo": SELF_REPO,
		"source_workflow_paths": [".github/workflows/test-and-mark-stable.yml"]}
	kwargs.update(overrides)
	return heal.heal_scope_linked_workflows_gate(**kwargs)


@pytest.mark.parametrize("workflow_file", heal.RELEASE_WORKFLOW_FILES)
def test_gate_accepts_every_release_workflow(workflow_file: str) -> None:
	assert _lw_gate(source_workflow_paths=[f".github/workflows/{workflow_file}@refs/heads/main"]) == "eligible"


def test_gate_rejects_other_sources_and_honours_the_flag() -> None:
	assert _lw_gate(source_workflow_paths=[".github/workflows/ci.yml"]) == "ineligible"
	assert _lw_gate(source_workflow_paths=[]) == "ineligible"
	assert _lw_gate(source_kind="autofix_failure") == "ineligible"
	assert _lw_gate(source_repo=LW_CONSUMER_REPO) == "ineligible"
	assert _lw_gate(source_repo=SELF_REPO.upper()) == "eligible"
	assert _lw_gate(enabled="false") == "disabled"
	assert _lw_gate(enabled=" False ") == "disabled"
	assert _lw_gate(enabled="") == "eligible"
	assert _lw_gate(enabled=None) == "eligible"


# --- CLI --------------------------------------------------------------------


def _lw_git(cwd: Path, *args: str) -> str:
	return subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


def _lw_origin(root: Path, tree: dict[str, str] | None = None) -> tuple[Path, str]:
	origin = root / "origin"
	origin.mkdir(parents=True)
	_lw_git(origin, "init", "-q", "-b", "main")
	_lw_git(origin, "config", "user.name", "test")
	_lw_git(origin, "config", "user.email", "test@example.invalid")
	for path, text in (LW_TREE if tree is None else tree).items():
		target = origin / path
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(text, encoding="utf-8")
	_lw_git(origin, "add", "-A")
	_lw_git(origin, "commit", "-qm", "scope tree")
	return origin, _lw_git(origin, "rev-parse", "HEAD")


def _lw_cli(checkout: Path, ref: str, pairs: list[tuple[Path, Path]], *extra: str) -> subprocess.CompletedProcess[str]:
	cmd = [sys.executable, str(HEAL_PY), "heal-scope", "linked-workflow-paths", "--checkout", str(checkout), "--ref", ref,
		"--repo", SELF_REPO, "--self-repo", SELF_REPO, "--source-kind", "workflow_run", "--source-repo", SELF_REPO,
		"--source-workflow-paths", ".github/workflows/test-and-mark-stable.yml", *extra]
	for run_file, jobs_file in pairs:
		cmd += ["--run-json", str(run_file), "--jobs-json", str(jobs_file)]
	env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
	if not LW_HAVE_YAML:
		stub_dir = checkout.parent / "lw-yaml-stub"
		stub_dir.mkdir(exist_ok=True)
		(stub_dir / "yaml.py").write_text(LW_YAML_STUB_SOURCE, encoding="utf-8")
		env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(stub_dir), env.get("PYTHONPATH", "")]))
	return subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def test_cli_reads_the_scope_commit_and_fails_open(tmp_path: Path) -> None:
	origin, sha = _lw_origin(tmp_path)
	run_file, jobs_file = tmp_path / "run.json", tmp_path / "jobs.json"
	run_file.write_text(json.dumps(_lw_run(LW_CANCEL_RUN_ID, ".github/workflows/internal-cancel-on-pr-close.yml")))
	jobs_file.write_text(json.dumps(_lw_jobs(_lw_step("Cancel queued runs for the closed PR", "failure"))))
	result = _lw_cli(origin, sha, [(run_file, jobs_file)])
	assert result.returncode == 0, result.stderr
	out = json.loads(result.stdout)
	assert out["outcome"] == "added" and out["runs"] == 1
	assert out["paths"] == [".github/workflows/internal-cancel-on-pr-close.yml", ".github/workflows/cancel_on_pr_close.yml", "scripts/cancel_on_pr_close.sh"]
	corrupt = tmp_path / "corrupt.json"
	corrupt.write_text("{not json")
	result = _lw_cli(origin, sha, [(corrupt, jobs_file)])
	assert result.returncode == 0
	assert json.loads(result.stdout) == {"outcome": "none", "paths": [], "skipped": [{"run_id": "?", "reason": "cache_unreadable"}], "runs": 1}
	# Disabled, an ineligible source or a non-SHA ref read nothing.
	assert json.loads(_lw_cli(origin, sha, [(run_file, jobs_file)], "--enabled", "false").stdout) == {"outcome": "disabled", "paths": [], "skipped": [], "runs": 0}
	assert json.loads(_lw_cli(origin, "main", [(run_file, jobs_file)]).stdout)["outcome"] == "skipped"


# --- Intake -----------------------------------------------------------------


def _lw_release_payload(sha: str, name: str = "Test & Mark Stable Release") -> dict:
	return heal.build_workflow_run_payload(
		repo=SELF_REPO,
		workflow_run={"id": 500, "name": name, "conclusion": "failure", "head_sha": sha, "head_branch": "main",
			"html_url": f"https://github.com/{SELF_REPO}/actions/runs/500", "display_title": name},
	)


def _lw_release_state(sha: str, linked_id: int, linked_path: str, linked_steps: list[dict], *, source_path: str = ".github/workflows/test-and-mark-stable.yml", **source_extra) -> dict:
	parent_log = (
		"2026-10-28T10:00:00.000Z ##[group]Run e2e\n"
		f"2026-10-28T10:00:01.000Z Watching run https://github.com/{SELF_REPO}/actions/runs/{linked_id}\n"
		f"2026-10-28T10:00:02.000Z ::error::Linked run failed (watched runs: https://github.com/{SELF_REPO}/actions/runs/{linked_id})\n"
		"2026-10-28T10:00:03.000Z ##[error]Process completed with exit code 1.\n"
	)
	source = {"id": 500, "repository": {"full_name": SELF_REPO}, "head_sha": sha, "html_url": f"https://github.com/{SELF_REPO}/actions/runs/500",
		"status": "completed", "conclusion": "failure", "path": source_path, "pull_requests": [], **source_extra}
	return base._intake_state(
		run_details={"500": source, str(linked_id): _lw_run(linked_id, linked_path)},
		jobs={
			"500": [{"id": 9001, "name": "e2e-smoke-test", "workflow_name": "Test & Mark Stable Release", "conclusion": "failure",
				"steps": [{"name": "Wait for the linked run", "conclusion": "failure"}]}],
			str(linked_id): [{"id": 7700, "name": "linked", "status": "completed", "conclusion": "failure", "steps": linked_steps}],
		},
		job_logs={"9001": parent_log, "7700": "2026-10-28T10:01:10.0000000Z boom\n"},
	)


def _lw_git_setup(origin: Path):
	def setup(tmp: Path, work: Path) -> None:
		_lw_git(work, "init", "-q", "-b", "main")
		_lw_git(work, "remote", "add", "origin", str(origin))
		_lw_git(work, "fetch", "-q", "origin")
	return setup


def _lw_scope_line(state: dict) -> str:
	body = state.get("composed_issue", {}).get("body", "")
	return next((line for line in body.splitlines() if "ai:workflow-heal-scope:v1" in line), "")


def _lw_intake(sha: str, origin: Path | None, state: dict, runtime: Path, *, name: str = "Test & Mark Stable Release", **env: str):
	extra = {"RUNTIME_DIR": str(runtime), **env}
	return base._run_intake(_lw_release_payload(sha, name), state, diagnosis=base.DIAG_WORKFLOW_DEFECT, extra_env=extra,
		setup_git=_lw_git_setup(origin) if origin is not None else None)


def test_intake_replays_cancel_on_pr_close_run_40814653574(tmp_path: Path) -> None:
	origin, sha = _lw_origin(tmp_path / "src")
	state = _lw_release_state(sha, LW_CANCEL_RUN_ID, ".github/workflows/internal-cancel-on-pr-close.yml", [_lw_step("Cancel queued runs for the closed PR", "failure")])
	result, after, _prompt = _lw_intake(sha, origin, state, tmp_path / "runtime")
	assert result.returncode == 0, result.stderr + result.stdout
	assert "WORKFLOW_HEAL scope_linked outcome=added paths=3 runs=1" in result.stdout
	frozen = json.loads((tmp_path / "runtime" / "scope_inputs_frozen.json").read_text())
	assert frozen["self_workflows"] == [".github/workflows/test-and-mark-stable.yml", ".github/workflows/internal-cancel-on-pr-close.yml",
		".github/workflows/cancel_on_pr_close.yml", "scripts/cancel_on_pr_close.sh"]
	marker = _lw_scope_line(after)
	assert ".github/workflows/cancel_on_pr_close.yml" in marker and ".github/workflows/test-and-mark-stable.yml" in marker
	assert len(base._linked_calls(after, str(LW_CANCEL_RUN_ID), "7700")) == 3


def test_intake_replays_memory_maintenance_run_40752760153(tmp_path: Path) -> None:
	origin, sha = _lw_origin(tmp_path / "src")
	steps = [_lw_step("Resolve workflow support ref", "success"), _lw_step("Extract repository learnings (fail-open)", "failure")]
	state = _lw_release_state(sha, LW_MEMORY_RUN_ID, ".github/workflows/internal-memory-maintenance.yml", steps)
	result, after, _prompt = _lw_intake(sha, origin, state, tmp_path / "runtime")
	assert result.returncode == 0, result.stderr + result.stdout
	marker = _lw_scope_line(after)
	assert ".github/workflows/memory_maintenance.yml" in marker
	assert "scripts/memory_maintenance_extract_learnings.py" in marker


def test_intake_flag_off_matches_the_previous_scope_byte_for_byte(tmp_path: Path) -> None:
	origin, sha = _lw_origin(tmp_path / "src")
	state = _lw_release_state(sha, LW_CANCEL_RUN_ID, ".github/workflows/internal-cancel-on-pr-close.yml", [_lw_step("Cancel queued runs for the closed PR", "failure")])
	runs = {}
	for label, env in (("off", {"WORKFLOW_HEAL_SCOPE_LINKED_WORKFLOWS_ENABLED": "false"}), ("none", {"WORKFLOW_HEAL_MAX_LINKED_RUNS": "0"}), ("on", {})):
		runtime = tmp_path / f"runtime-{label}"
		result, after, _prompt = _lw_intake(sha, origin, state, runtime, **env)
		assert result.returncode == 0, result.stderr + result.stdout
		runs[label] = (result, after, (runtime / "scope_inputs_frozen.json").read_text(encoding="utf-8"))
	assert "WORKFLOW_HEAL scope_linked outcome=disabled paths=0 runs=1" in runs["off"][0].stdout
	assert runs["off"][2] == runs["none"][2]
	assert _lw_scope_line(runs["off"][1]) == _lw_scope_line(runs["none"][1])
	assert _lw_scope_line(runs["on"][1]) != _lw_scope_line(runs["off"][1])
	# The fingerprint never sees linked data.
	fps = {heal.parse_heal_markers(after["composed_issue"]["body"])["fp"] for _result, after, _frozen in runs.values()}
	assert len(fps) == 1
	# API budget: one run, one jobs and one job-log read per linked run, flag on or off.
	assert len(base._linked_calls(runs["on"][1], str(LW_CANCEL_RUN_ID), "7700")) == 3
	assert len(base._linked_calls(runs["off"][1], str(LW_CANCEL_RUN_ID), "7700")) == 3


def test_intake_non_release_sources_get_no_linked_paths(tmp_path: Path) -> None:
	# A consumer report: the existing #8248 guard plus the skipped outcome.
	result, _after, _prompt = base._run_intake(base._consumer_payload(), base._linked_state(base.LINKED_PARENT_LOG), diagnosis=base.DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "WORKFLOW_HEAL scope_linked outcome=skipped paths=0 runs=1" in result.stdout
	# A self-repo workflow_run whose verified run is CI on main, not a release workflow.
	origin, sha = _lw_origin(tmp_path / "src", {**LW_TREE, ".github/workflows/ci.yml": LW_RELEASE_WORKFLOW})
	state = _lw_release_state(sha, LW_CANCEL_RUN_ID, ".github/workflows/internal-cancel-on-pr-close.yml", [_lw_step("Cancel queued runs for the closed PR", "failure")],
		source_path=".github/workflows/ci.yml", event="push", head_branch="main")
	result, _after, _prompt = _lw_intake(sha, origin, state, tmp_path / "runtime", name="CI", WORKFLOW_HEAL_DEFAULT_BRANCH="main")
	assert result.returncode == 0, result.stderr + result.stdout
	if (tmp_path / "runtime" / "scope_inputs_frozen.json").exists():
		frozen = json.loads((tmp_path / "runtime" / "scope_inputs_frozen.json").read_text())
		assert "cancel_on_pr_close" not in json.dumps(frozen)
	assert "scope_linked outcome=added" not in result.stdout
	assert all("outcome=skipped paths=0" in line for line in result.stdout.splitlines() if "WORKFLOW_HEAL scope_linked " in line)


def test_intake_unresolvable_head_sha_adds_nothing_and_still_composes(tmp_path: Path) -> None:
	sha = "b" * 40
	state = _lw_release_state(sha, LW_CANCEL_RUN_ID, ".github/workflows/internal-cancel-on-pr-close.yml", [_lw_step("Cancel queued runs for the closed PR", "failure")])
	result, after, _prompt = _lw_intake(sha, None, state, tmp_path / "runtime")
	assert result.returncode == 0, result.stderr + result.stdout
	assert "WORKFLOW_HEAL scope_linked outcome=skipped paths=0 runs=1" in result.stdout
	frozen = json.loads((tmp_path / "runtime" / "scope_inputs_frozen.json").read_text())
	assert frozen["self_workflows"] == [".github/workflows/test-and-mark-stable.yml"]
	assert after.get("composed_issue", {}).get("body")


# --- Wiring -----------------------------------------------------------------


def test_intake_layout_fetches_before_the_freeze_and_renders_after() -> None:
	text = LW_INTAKE_SCRIPT.read_text(encoding="utf-8")
	assert 'SCOPE_LINKED_WORKFLOWS_ENABLED="${WORKFLOW_HEAL_SCOPE_LINKED_WORKFLOWS_ENABLED:-true}"' in text
	freeze = text.index('jq -n --arg crash "${PAYLOAD_CRASH_FILE}"')
	assert text.index('_heal_fetch_linked_runs || log "warn linked_runs_failed"') < text.index("\n_heal_scope_linked_paths ||") < freeze
	assert freeze < text.index('_heal_collect_linked_runs || log "warn linked_runs_failed"')
	assert '--argjson linked_paths "${LINKED_SCOPE_PATHS_JSON:-[]}"' in text
	assert "self_workflows: ($base + (($linked_paths - $base)[:$linked_room]))" in text
	# The run and jobs REST reads happen only in the fetch half.
	collect = text[text.index("_heal_collect_linked_runs()\n"):text.index('_heal_collect_linked_runs || log "warn linked_runs_failed"')]
	assert "gh_api_json_to_file" not in collect and "fetched_ids.txt" in collect
	fetch = text[text.index("_heal_fetch_linked_runs()\n"):text.index('_heal_fetch_linked_runs || log "warn linked_runs_failed"')]
	assert fetch.count("gh_api_json_to_file") == 2 and "/logs" not in fetch


def test_workflow_sets_the_flag_with_a_true_default() -> None:
	text = LW_INTAKE_WORKFLOW.read_text(encoding="utf-8")
	line = "      WORKFLOW_HEAL_SCOPE_LINKED_WORKFLOWS_ENABLED: ${{ vars.WORKFLOW_HEAL_SCOPE_LINKED_WORKFLOWS_ENABLED || 'true' }}\n"
	assert text.count(line) == 2  # intake and recovery-scan jobs
