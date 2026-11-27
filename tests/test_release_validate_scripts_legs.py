"""Release gate `validate-scripts` runs its two long suites as parallel matrix legs (#10220).

The serial job used 58-93% of its 135-minute `timeout-minutes` one week after
the last raise (runs 45518188323, 45734337628, 45927857867, 46009933351). The
job now runs "Orchestrate poll process unit tests" in the `orchestrate-poll`
leg and every other check in the `unit-tests` leg, keeping its job id, step
names and the aggregate result `validate` waits on.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
	import yaml
except ImportError:  # CI installs PyYAML; credential-free gate containers may not.
	yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
LEGS = ["orchestrate-poll", "unit-tests"]

SETUP_STEPS = ["Checkout repository", "Setup Python", "Install Python test dependencies"]
POLL_STEP = "Orchestrate poll process unit tests"
UNIT_LEG_STEPS = [
	"YAML lint",
	"Shell script syntax check",
	"ShellCheck static analysis",
	"Python syntax check",
	"Python lint (ruff)",
	"Unit tests",
	"Claude asset tests (CLAUDE.md, .claude hooks, scripts, commands)",
	"Prompt files integrity",
	"Script-workflow cross-reference",
]
DIAG_STEP = "git diagnostic (checkout exit-128 probe)"
# The ordered step list before #10220; the split must not rename or reorder it.
EXPECTED_STEP_NAMES = [
	"Checkout repository",
	"Setup Python",
	"YAML lint",
	"Shell script syntax check",
	"ShellCheck static analysis",
	"Python syntax check",
	"Python lint (ruff)",
	"Install Python test dependencies",
	POLL_STEP,
	"Unit tests",
	"Claude asset tests (CLAUDE.md, .claude hooks, scripts, commands)",
	"Prompt files integrity",
	"Script-workflow cross-reference",
	DIAG_STEP,
]


def _scalar(raw: str):
	value = raw.strip()
	if value.startswith("[") and value.endswith("]"):
		return [_scalar(item) for item in value[1:-1].split(",") if item.strip()]
	if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
		return value[1:-1]
	if value in ("true", "false"):
		return value == "true"
	if re.fullmatch(r"-?\d+", value):
		return int(value)
	return value


def _indent(line: str) -> int:
	return len(line) - len(line.lstrip(" "))


def _mapping(lines: list[str], start: int, indent: int) -> tuple[dict, int]:
	"""Parse the block mapping at `indent` from lines[start:] (the subset this file's jobs use)."""
	out: dict = {}
	i = start
	while i < len(lines):
		line = lines[i]
		stripped = line.strip()
		if not stripped or stripped.startswith("#"):
			i += 1
			continue
		if _indent(line) < indent:
			break
		if _indent(line) > indent or stripped.startswith("- "):
			i += 1
			continue
		key, _, rest = stripped.partition(":")
		rest = rest.strip()
		i += 1
		if rest in (">-", ">", "|", "|-"):
			block = []
			while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > indent):
				block.append(lines[i].strip())
				i += 1
			out[key] = (" " if rest.startswith(">") else "\n").join(part for part in block if part)
		elif rest:
			out[key] = _scalar(rest)
		elif key == "steps":
			out[key], i = _steps_list(lines, i, indent + 2)
		else:
			out[key], i = _mapping(lines, i, indent + 2)
	return out, i


def _anchor_step(lines: list[str], anchor: str) -> dict:
	for index, line in enumerate(lines):
		if line.strip() == f"- &{anchor}":
			step, _ = _mapping(lines, index + 1, _indent(line) + 2)
			return step
	raise AssertionError(f"anchor {anchor} not found")


def _steps_list(lines: list[str], start: int, indent: int) -> tuple[list, int]:
	steps: list = []
	i = start
	while i < len(lines):
		line = lines[i]
		stripped = line.strip()
		if not stripped or stripped.startswith("#"):
			i += 1
			continue
		if _indent(line) < indent:
			break
		if _indent(line) == indent and stripped.startswith("- "):
			item = stripped[2:].strip()
			if item.startswith("*"):
				steps.append(_anchor_step(ALL_LINES_CACHE[0], item[1:]))
				i += 1
				continue
			step, i = _mapping(lines, i + 1, indent + 2)
			if item:
				key, _, rest = item.partition(":")
				step = {key: _scalar(rest), **step}
			steps.append(step)
			continue
		i += 1
	return steps, i


ALL_LINES_CACHE: list[list[str]] = []


def _jobs_without_yaml(text: str) -> dict:
	"""Text fallback when PyYAML is missing: parse only the jobs these tests read."""
	lines = text.splitlines()
	ALL_LINES_CACHE[:] = [lines]
	jobs: dict = {}
	for name in ("validate-scripts", "validate"):
		start = lines.index(f"  {name}:")
		jobs[name], _ = _mapping(lines, start + 1, 4)
	return jobs


def _jobs() -> dict:
	text = GATE.read_text(encoding="utf-8")
	if yaml is None:
		return _jobs_without_yaml(text)
	return yaml.safe_load(text)["jobs"]


def _job_text() -> str:
	text = GATE.read_text(encoding="utf-8")
	start = text.index("\n  validate-scripts:\n")
	end = text.index("\n  workflow-log-analysis-test:\n", start)
	return text[start:end]


def _steps() -> dict[str, dict]:
	return {step["name"]: step for step in _jobs()["validate-scripts"]["steps"]}


def test_job_is_a_two_leg_matrix_with_the_same_budget() -> None:
	job = _jobs()["validate-scripts"]
	assert job["needs"] == ["source", "resolve-version"]
	assert job["timeout-minutes"] == 135
	assert "name" not in job, "a job name would change the leg names budget watch and operator tooling match"
	strategy = job["strategy"]
	assert strategy["matrix"] == {"leg": LEGS}
	assert strategy["max-parallel"] == 2
	assert strategy["fail-fast"] is False


def test_step_names_and_order_are_unchanged() -> None:
	names = [step["name"] for step in _jobs()["validate-scripts"]["steps"]]
	assert names == EXPECTED_STEP_NAMES


def test_each_step_runs_in_the_intended_leg() -> None:
	steps = _steps()
	for name in SETUP_STEPS:
		assert "if" not in steps[name], name
	assert steps[POLL_STEP]["if"] == "matrix.leg == 'orchestrate-poll'"
	for name in UNIT_LEG_STEPS:
		assert steps[name]["if"] == "matrix.leg == 'unit-tests'", name
	assert steps[DIAG_STEP]["if"] == "always()"


def test_every_leg_condition_names_a_listed_leg() -> None:
	for name, step in _steps().items():
		condition = str(step.get("if", ""))
		for leg in re.findall(r"matrix\.leg == '([^']*)'", condition):
			assert leg in LEGS, (name, leg)
		if "matrix" in condition:
			assert re.fullmatch(r"matrix\.leg == '[^']*'", condition), (name, condition)
	# Every leg runs at least one test step, so neither leg is empty.
	conditions = [str(step.get("if", "")) for step in _steps().values()]
	for leg in LEGS:
		assert f"matrix.leg == '{leg}'" in conditions, leg


def test_validate_still_requires_validate_scripts_success() -> None:
	validate = _jobs()["validate"]
	assert "validate-scripts" in validate["needs"]
	assert "needs.validate-scripts.result == 'success'" in validate["if"]


def test_timeout_comment_keeps_history_and_records_the_split() -> None:
	text = _job_text()
	comment = text[: text.index("    timeout-minutes: 135\n")]
	for run_id in ("33073743283", "39526632477", "44689548109", "36374918973"):
		assert run_id in comment, run_id
	for run_id in ("45518188323", "45734337628", "45927857867", "46009933351"):
		assert run_id in comment, run_id
	assert "#10220" in comment
	assert "max-parallel: 2" in comment
	assert "scripts/ci_budget_watch.py" in comment


def test_workflow_stays_under_the_size_guard() -> None:
	assert GATE.stat().st_size < 480_000
