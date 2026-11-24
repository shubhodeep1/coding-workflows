#!/usr/bin/env python3
"""Every test file must run in some workflow.

On 2026-10-10, 66 of the 315 `tests/test_*` files were referenced by no
workflow, so CI never ran them. Seven of them had gone stale against the
code they covered: the resolver had become sandbox-only, Codex had moved into
the isolated container (#6187), a staged block had grown, and the overlay
root had moved under RUNNER_TEMP. Two more spent about 35 seconds per test
fetching the ai-memory branch from origin. Nobody noticed, because nothing
ran them.

This guard fails when a `tests/test_*.sh` file is named in no
`.github/workflows/*.yml` file, or when the `tests-system-python` catch-all
that runs every `tests/test_*.py` file stops collecting the whole tree. A file
that cannot run in CI goes in UNWIRED_ALLOWLIST with the reason; a stale
allowlist entry fails too.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# "tests/<file>": "why it cannot run in CI". Keep it empty when possible.
# A `tests/test_*.py` entry is always stale while the catch-all below holds,
# so only `tests/test_*.sh` files can need one.
UNWIRED_ALLOWLIST: dict[str, str] = {}

# Former allowlist entry, kept as a record (stuck-states P9b, Refs #8291). The
# catch-all now runs this file directly, so it is no longer in the allowlist;
# its cases also still run through the file named in the reason:
#	"tests/test_clarify_blocked_verify.py": (
#		"ci.yml is outside the file grant of the issue that added it; its cases run in CI "
#		"through tests/test_plan_clarify_blocked_output.py, which collects them"
#	),

# The catch-all (merge-base sync churn, 2026-11-17). `tests-system-python` in
# ci.yml collects the whole `tests/` tree with pytest and runs every collected
# function, so a `tests/test_*.py` file runs in CI without being named in any
# workflow. Requiring a ci.yml edit per new test file made ci.yml the file
# nearly every implementation PR shared with `main`: 18 of 19 merge-base
# freshness updates sampled on 2026-11-16 overlapped on it. Shell tests
# (`tests/test_*.sh`) are not collected by pytest and must still be named.
CI_WORKFLOW = WORKFLOWS_DIR / "ci.yml"
PYPROJECT = ROOT / "pyproject.toml"
CATCH_ALL_JOB = "tests-system-python"
CATCH_ALL_COLLECT_COMMAND = "python3 -m pytest --collect-only -q -p no:cacheprovider tests "

# Former allowlist entry kept for the record (project #9116, P1). The
# catch-all above now runs this file directly, so it no longer needs an entry;
# its cases are also still collected through the failure-contract file:
#	"tests/test_workflow_log_analysis_report_pr.py": (
#		"ci.yml is outside the file grant of the issue that added it; its cases run in CI "
#		"through tests/test_workflow_log_analysis_failure_contract.py, which collects them"
#	),


def _strip_comment(line: str) -> str:
	"""Drop a full-line comment or a trailing ` #` comment (YAML or shell), so a path named only in a comment is not wired."""
	if line.lstrip().startswith("#"):
		return ""
	cut = line.find(" #")
	return line if cut < 0 else line[:cut]


def _workflow_text() -> str:
	"""All workflow text minus comments, so a path named only in a comment does not count as wired."""
	return "\n".join(
		_strip_comment(line)
		for path in sorted(WORKFLOWS_DIR.glob("*.yml"))
		for line in path.read_text(encoding="utf-8").splitlines()
	)


def _test_files() -> list[str]:
	return sorted(
		path.relative_to(ROOT).as_posix()
		for path in (ROOT / "tests").glob("test_*")
		if path.is_file() and path.suffix in (".py", ".sh")
	)


def _job_block_text(workflow_text: str, job_id: str) -> str:
	"""Text of the `jobs.<job_id>` block of a workflow, read without PyYAML.

	Fallback for containers without PyYAML: the block runs from the job's
	`  <job_id>:` line under top-level `jobs:` to the next line at the same or
	a lower indentation. Empty when the job is missing.
	"""
	lines = workflow_text.splitlines()
	in_jobs = False
	start = None
	job_indent = 0
	for index, line in enumerate(lines):
		stripped = line.strip()
		if not stripped or stripped.startswith("#"):
			continue
		indent = len(line) - len(line.lstrip(" "))
		if start is not None:
			if indent <= job_indent:
				return "\n".join(lines[start:index])
			continue
		if indent == 0:
			in_jobs = stripped.split("#", 1)[0].rstrip() == "jobs:"
			continue
		if in_jobs and stripped.split("#", 1)[0].rstrip() in (f"{job_id}:", f'"{job_id}":', f"'{job_id}':"):
			start = index + 1
			job_indent = indent
	return "\n".join(lines[start:]) if start is not None else ""


def _catch_all_collects_python_tests() -> bool:
	"""True while ci.yml's catch-all job collects all of tests/ and pytest collects every test_*.py there."""
	workflow_text = CI_WORKFLOW.read_text(encoding="utf-8")
	try:
		import yaml
	except ImportError:  # CI installs PyYAML; credential-free gate containers may not.
		yaml = None
	if yaml is not None:
		workflow = yaml.safe_load(workflow_text)
		job = (workflow.get("jobs") or {}).get(CATCH_ALL_JOB) or {}
		runs = [str(step.get("run") or "") for step in job.get("steps") or [] if isinstance(step, dict)]
	else:
		runs = [_job_block_text(workflow_text, CATCH_ALL_JOB)]
	collects_all = any(
		CATCH_ALL_COLLECT_COMMAND in "\n".join(_strip_comment(line) for line in run.splitlines())
		for run in runs
	)
	pyproject = PYPROJECT.read_text(encoding="utf-8")
	collects_test_py = 'testpaths = ["tests"]' in pyproject and 'python_files = ["test_*.py"]' in pyproject
	conftest = ROOT / "tests" / "conftest.py"
	ignores_files = conftest.is_file() and "collect_ignore" in conftest.read_text(encoding="utf-8")
	return collects_all and collects_test_py and not ignores_files


# A module the catch-all can run defines a pytest test function or a test
# class at top level (test methods live inside such a class). A test def
# nested in `main()` or under `if __name__ == "__main__"` is indented, so it
# does not count: such a script-style file collects nothing and still has to
# be named in a workflow.
_COLLECTABLE_TEST = re.compile(r"^(?:async\s+)?def\s+test\w*\s*\(|^class\s+(?:Test\w*|\w+\([^)]*TestCase[^)]*\))", re.MULTILINE)


def _defines_collectable_tests(relative: str) -> bool:
	try:
		text = (ROOT / relative).read_text(encoding="utf-8")
	except OSError:
		return False
	return bool(_COLLECTABLE_TEST.search(text))


def _is_wired(relative: str, workflow_text: str, catch_all: bool | None = None) -> bool:
	if relative in workflow_text:
		return True
	if catch_all is None:
		catch_all = _catch_all_collects_python_tests()
	return catch_all and relative.endswith(".py") and _defines_collectable_tests(relative)


def test_every_test_file_runs_in_a_workflow() -> None:
	workflow_text = _workflow_text()
	catch_all = _catch_all_collects_python_tests()
	unwired = [
		relative
		for relative in _test_files()
		if relative not in UNWIRED_ALLOWLIST and not _is_wired(relative, workflow_text, catch_all)
	]
	assert not unwired, (
		"These test files run in no workflow. A tests/test_*.sh file must be named in a CI step "
		"in .github/workflows/ci.yml (or go in UNWIRED_ALLOWLIST with the reason it cannot run "
		"there); a tests/test_*.py file runs through the tests-system-python catch-all, so this "
		"also fails when that catch-all stops collecting tests/:\n  " + "\n  ".join(unwired)
	)


def test_the_catch_all_collects_every_python_test_file() -> None:
	assert _catch_all_collects_python_tests(), (
		f"ci.yml job {CATCH_ALL_JOB} must run `{CATCH_ALL_COLLECT_COMMAND.strip()}` and pyproject.toml "
		"must collect tests/test_*.py, or new Python test files would run nowhere"
	)


def test_python_files_need_no_workflow_entry_but_shell_files_do() -> None:
	assert _is_wired("tests/test_ci_wires_every_test_file.py", "", catch_all=True)
	assert not _is_wired("tests/test_ci_wires_every_test_file.py", "", catch_all=False)
	assert not _is_wired("tests/test_not_on_disk.py", "", catch_all=True)
	assert not _is_wired("tests/test_not_named_anywhere.sh", "", catch_all=True)
	assert _is_wired("tests/test_named.sh", "run: bash tests/test_named.sh", catch_all=True)


def test_allowlist_entries_are_real_unwired_files_with_reasons() -> None:
	workflow_text = _workflow_text()
	existing = set(_test_files())
	for relative, reason in UNWIRED_ALLOWLIST.items():
		assert relative in existing, f"{relative} is allowlisted but does not exist"
		assert reason.strip(), f"{relative} is allowlisted without a reason"
		assert not _is_wired(relative, workflow_text), f"{relative} is allowlisted but a workflow runs it; drop the entry"


def test_the_guard_sees_the_test_tree() -> None:
	files = _test_files()
	assert len(files) > 100
	assert "tests/test_ci_wires_every_test_file.py" in files
	assert _is_wired("tests/test_ci_wires_every_test_file.py", _workflow_text())


def test_comments_do_not_count_as_wiring() -> None:
	assert _strip_comment("  # tests/test_x.py") == ""
	assert "tests/test_x.py" not in _strip_comment("  run: echo ok  # tests/test_x.py")
	assert _strip_comment("  python3 tests/test_x.py  # why") == "  python3 tests/test_x.py "


def test_script_style_files_still_need_a_workflow_entry(tmp_path) -> None:
	samples = {
		"def test_x():\n\tpass\n": True,
		"class TestThing:\n\tdef test_a(self):\n\t\tpass\n": True,
		"class Thing(unittest.TestCase):\n\tdef test_a(self):\n\t\tpass\n": True,
		"async def test_y():\n\tpass\n": True,
		"def main():\n\tassert 1\n\nif __name__ == '__main__':\n\tmain()\n": False,
		"def check_thing():\n\tpass\n": False,
		# Nested test defs run only when the script runs: pytest collects nothing.
		"def main():\n\tdef test_case():\n\t\tpass\n\ttest_case()\n": False,
		"if __name__ == '__main__':\n\tdef test_case():\n\t\tpass\n": False,
		"def main():\n    def test_case():\n        pass\n": False,
	}
	for text, expected in samples.items():
		assert bool(_COLLECTABLE_TEST.search(text)) is expected, text
	assert not _defines_collectable_tests("tests/test_session_start_extract_repo_slug.py")
