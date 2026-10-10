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
