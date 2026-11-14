#!/usr/bin/env python3
"""Runner image pin and nightly runner-image canary contract.

GitHub moved ``ubuntu-latest`` to the ``ubuntu-26.04`` image (Python 3.14) on
2026-11-10, and ``scripts/render_prompt.py`` then crashed on every clarify,
plan, review and judge run (#9200). A floating runner label lets the next image
change break the pipeline without warning, so every hosted job is pinned to
``ubuntu-26.04`` (stuck-backlog plan Phase 1). ``ci.yml`` is callable with a
``runner_image`` input, and the ``runner-image-canary`` job in
``nightly-validation-selftest.yml`` runs all of it on the floating
``ubuntu-latest`` label each night (Q12: A). A failing canary fails the Nightly
run, which the workflow failure heal intake already turns into a heal issue.

The three ``ubuntu-24.04`` jobs predate the pin and stay as they are.

To move the pin once the canary is green on a new image, update every
``runs-on``, the ``ci.yml`` fallback, ``PINNED_RUNNER_LABEL`` below and
``.github/actionlint.yaml`` together.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TEMPLATES_DIR = REPO_ROOT / "workflow-templates"
CI_WORKFLOW = WORKFLOWS_DIR / "ci.yml"
NIGHTLY_WORKFLOW = WORKFLOWS_DIR / "nightly-validation-selftest.yml"
INTAKE_WORKFLOW = WORKFLOWS_DIR / "workflow-failure-heal-intake.yml"
HEAL_SCRIPT = REPO_ROOT / "scripts" / "workflow_failure_heal.py"
ACTIONLINT_CONFIG = REPO_ROOT / ".github" / "actionlint.yaml"

PINNED_RUNNER_LABEL = "ubuntu-26.04"
FLOATING_RUNNER_LABEL = "ubuntu-latest"
CI_RUNS_ON_EXPRESSION = "${{ inputs.runner_image || '" + PINNED_RUNNER_LABEL + "' }}"
CANARY_JOB_ID = "runner-image-canary"
CANARY_USES = "./.github/workflows/ci.yml"
CANARY_SMOKE_STEP_NAME = "Runner-image canary system python smoke"
EXPLICIT_24_04_PIN_FILES = ("drift-audit.yml", "security-audit.yml", "review_autofix_sweep.yml")
RUNS_ON_FLOATING_LINE_RE = re.compile(r"^\s*runs-on:.*" + re.escape(FLOATING_RUNNER_LABEL))
PERMISSION_LEVELS = {"none": 0, "read": 1, "write": 2}
SECRET_EXPRESSION_RE = re.compile(r"\$\{\{[^}]*\bsecrets\.")
TAB_INDENT_RE = re.compile(r"^ *\t")


def _workflow_files() -> list[Path]:
	files: list[Path] = []
	for directory in (WORKFLOWS_DIR, TEMPLATES_DIR):
		files.extend(directory.glob("*.yml"))
		files.extend(directory.glob("*.yaml"))
	return sorted(files)


def _rel(path: Path) -> str:
	return path.relative_to(REPO_ROOT).as_posix()


def _load(path: Path) -> dict:
	doc = yaml.safe_load(path.read_text(encoding="utf-8"))
	assert isinstance(doc, dict), f"{_rel(path)} is not a YAML mapping"
	return doc


def _on(doc: dict) -> dict:
	"""PyYAML reads an unquoted `on:` key as True and a quoted `"on":` as a string."""
	value = doc[True] if True in doc else doc.get("on")
	return value if isinstance(value, dict) else {}


def _jobs(doc: dict) -> dict:
	jobs = doc.get("jobs")
	return jobs if isinstance(jobs, dict) else {}


def _runs_on_labels(value: object) -> list[str]:
	if value is None:
		return []
	if isinstance(value, str):
		return [value]
	if isinstance(value, list):
		return [str(item) for item in value]
	if isinstance(value, dict):
		labels: list[str] = []
		for key in ("labels", "group"):
			labels.extend(_runs_on_labels(value.get(key)))
		return labels
	return [str(value)]


def _heal_constant(name: str) -> tuple[str, ...]:
	"""Read a literal tuple from scripts/workflow_failure_heal.py without importing it."""
	tree = ast.parse(HEAL_SCRIPT.read_text(encoding="utf-8"))
	for node in tree.body:
		if isinstance(node, ast.Assign):
			targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
		elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
			targets = [node.target.id]
		else:
			continue
		if name in targets and node.value is not None:
			return tuple(ast.literal_eval(node.value))
	raise AssertionError(f"{name} not found in {_rel(HEAL_SCRIPT)}")


def test_no_floating_label_in_runs_on() -> None:
	offenders: list[str] = []
	for path in _workflow_files():
		for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
			if RUNS_ON_FLOATING_LINE_RE.match(line):
				offenders.append(f"{_rel(path)}:{line_no}")
		for job_id, job in _jobs(_load(path)).items():
			if not isinstance(job, dict):
				continue
			if any(FLOATING_RUNNER_LABEL in label for label in _runs_on_labels(job.get("runs-on"))):
				offenders.append(f"{_rel(path)} job={job_id}")
	assert not offenders, (
		f"runs-on must not use the floating {FLOATING_RUNNER_LABEL} label (pin {PINNED_RUNNER_LABEL}; "
		f"the nightly {CANARY_JOB_ID} job watches the floating image): " + ", ".join(offenders)
	)


def test_only_the_canary_passes_the_floating_label() -> None:
	found: list[str] = []
	for path in _workflow_files():
		for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
			if FLOATING_RUNNER_LABEL in line and not line.strip().startswith("#"):
				found.append(f"{_rel(path)}:{line_no}:{line.strip()}")
	assert len(found) == 1, (
		f"The only non-comment {FLOATING_RUNNER_LABEL} in workflows and templates must be the "
		f"{CANARY_JOB_ID} job's runner_image input; found: {found}"
	)
	assert found[0].startswith(_rel(NIGHTLY_WORKFLOW) + ":"), found
	assert found[0].endswith(f"runner_image: {FLOATING_RUNNER_LABEL}"), found
	canary = _jobs(_load(NIGHTLY_WORKFLOW)).get(CANARY_JOB_ID)
	assert isinstance(canary, dict), f"{_rel(NIGHTLY_WORKFLOW)} lost the {CANARY_JOB_ID} job"
	assert canary.get("with", {}).get("runner_image") == FLOATING_RUNNER_LABEL


def test_ci_runs_on_defaults_to_the_pin() -> None:
	doc = _load(CI_WORKFLOW)
	on = _on(doc)
	assert "push" in on and "pull_request" in on, "ci.yml must keep its push and pull_request triggers"
	workflow_call = on.get("workflow_call")
	assert isinstance(workflow_call, dict), "ci.yml must be callable (workflow_call) for the runner-image canary"
	runner_input = (workflow_call.get("inputs") or {}).get("runner_image")
	assert isinstance(runner_input, dict), "ci.yml workflow_call must declare the runner_image input"
	assert runner_input.get("type") == "string"
	assert runner_input.get("required") in (False, None)
	assert runner_input.get("default") == ""
	jobs = _jobs(doc)
	assert jobs, "ci.yml has no jobs"
	wrong = {job_id: job.get("runs-on") for job_id, job in jobs.items() if job.get("runs-on") != CI_RUNS_ON_EXPRESSION}
	assert not wrong, f"every ci.yml job must use runs-on: {CI_RUNS_ON_EXPRESSION}; found {wrong}"


def test_canary_smoke_runs_system_python_before_setup_python() -> None:
	"""The canary must exercise the image's own python3 (the #9200 failure), not only setup-python's."""
	steps = _jobs(_load(CI_WORKFLOW))["static-checks"]["steps"]
	names = [step.get("name") for step in steps]
	assert CANARY_SMOKE_STEP_NAME in names, f"ci.yml static-checks lost the {CANARY_SMOKE_STEP_NAME!r} step"
	assert "Setup Python" in names, "ci.yml static-checks lost its Setup Python step"
	assert names.index(CANARY_SMOKE_STEP_NAME) < names.index("Setup Python"), (
		f"{CANARY_SMOKE_STEP_NAME!r} must run before Setup Python so it uses the runner image's system python3"
	)
	smoke = steps[names.index(CANARY_SMOKE_STEP_NAME)]
	assert smoke.get("if") == "inputs.runner_image != ''", "the smoke step must run only under the canary call"
	assert "python3 scripts/render_prompt.py --help" in str(smoke.get("run", ""))


def test_canary_job_calls_ci_on_the_floating_label() -> None:
	text = NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
	tab_lines = [n for n, line in enumerate(text.splitlines(), start=1) if TAB_INDENT_RE.match(line)]
	assert not tab_lines, f"{_rel(NIGHTLY_WORKFLOW)} must use 2-space indentation (YAML forbids tabs): lines {tab_lines}"
	doc = _load(NIGHTLY_WORKFLOW)
	on = _on(doc)
	schedule = on.get("schedule")
	assert isinstance(schedule, list) and any(isinstance(item, dict) and item.get("cron") for item in schedule), (
		"the Nightly workflow must keep its schedule cron so the canary runs every night"
	)
	assert "workflow_dispatch" in on, "the Nightly workflow must keep workflow_dispatch for manual canary reruns"
	jobs = _jobs(doc)
	canary = jobs.get(CANARY_JOB_ID)
	assert isinstance(canary, dict), f"{_rel(NIGHTLY_WORKFLOW)} lost the {CANARY_JOB_ID} job"
	assert canary.get("uses") == CANARY_USES
	assert canary.get("with") == {"runner_image": FLOATING_RUNNER_LABEL}
	assert "secrets" not in canary, "the canary must forward no secrets to ci.yml"
	assert "needs" not in canary, "the canary must not depend on (or delay) the self-test jobs"
	canary_group = (canary.get("concurrency") or {}).get("group")
	assert canary_group, "the canary job needs its own concurrency group"
	for job_id, job in jobs.items():
		if job_id == CANARY_JOB_ID or not isinstance(job, dict):
			continue
		concurrency = job.get("concurrency")
		other_group = concurrency.get("group") if isinstance(concurrency, dict) else concurrency
		assert other_group != canary_group, f"the canary's concurrency group would cancel job {job_id}"


def test_canary_grants_exactly_what_ci_jobs_request() -> None:
	ci_doc = _load(CI_WORKFLOW)
	scopes = [ci_doc.get("permissions")] + [job.get("permissions") for job in _jobs(ci_doc).values()]
	union: dict[str, str] = {}
	for permissions in scopes:
		if permissions is None:
			continue
		assert isinstance(permissions, dict), f"ci.yml permissions must be a scope mapping, got {permissions!r}"
		for scope, level in permissions.items():
			assert level in PERMISSION_LEVELS, f"unexpected ci.yml permission level {scope}: {level!r}"
			if PERMISSION_LEVELS[level] > PERMISSION_LEVELS[union.get(scope, "none")]:
				union[scope] = level
	union = {scope: level for scope, level in union.items() if level != "none"}
	union.setdefault("contents", "read")
	canary = _jobs(_load(NIGHTLY_WORKFLOW))[CANARY_JOB_ID]
	assert canary.get("permissions") == union, (
		f"GitHub rejects the whole Nightly run at startup when a nested ci.yml job requests more than the "
		f"{CANARY_JOB_ID} job grants; its permissions must equal {union}, got {canary.get('permissions')}"
	)


def test_ci_side_effects_stay_push_only() -> None:
	doc = _load(CI_WORKFLOW)
	assert "concurrency" not in doc, (
		"ci.yml gained a workflow-level concurrency group; include github.event_name in it so the "
		"nightly runner-image canary call cannot cancel a push or pull_request CI run"
	)
	for job_id, job in _jobs(doc).items():
		assert "concurrency" not in job, (
			f"ci.yml job {job_id} gained a concurrency group; include github.event_name in it so the "
			"nightly runner-image canary call cannot cancel a push or pull_request CI run"
		)
		permissions = job.get("permissions") if isinstance(job.get("permissions"), dict) else {}
		writes = any(level == "write" for level in permissions.values())
		reads_secret = bool(SECRET_EXPRESSION_RE.search(json.dumps(job)))
		if writes or reads_secret:
			assert "github.event_name == 'push'" in str(job.get("if", "")), (
				f"ci.yml job {job_id} writes or reads a secret, so it must stay push-only "
				"(the runner-image canary calls ci.yml under schedule / workflow_dispatch)"
			)


def test_canary_failure_reaches_heal_intake() -> None:
	nightly_name = _load(NIGHTLY_WORKFLOW).get("name")
	assert nightly_name in _heal_constant("SCHEDULED_CHECK_WORKFLOW_NAMES")
	assert NIGHTLY_WORKFLOW.name in _heal_constant("SCHEDULED_CHECK_WORKFLOW_FILES")
	workflow_run = _on(_load(INTAKE_WORKFLOW)).get("workflow_run") or {}
	assert nightly_name in (workflow_run.get("workflows") or []), (
		"the heal intake must keep watching the Nightly workflow, which carries the runner-image canary"
	)


def test_explicit_24_04_pins_untouched() -> None:
	for name in EXPLICIT_24_04_PIN_FILES:
		path = WORKFLOWS_DIR / name
		assert path.is_file(), f"{_rel(path)} is missing"
		assert "runs-on: ubuntu-24.04" in path.read_text(encoding="utf-8"), f"{_rel(path)} lost its ubuntu-24.04 pin"


def test_actionlint_config_declares_the_pin() -> None:
	assert ACTIONLINT_CONFIG.is_file(), f"{_rel(ACTIONLINT_CONFIG)} is missing"
	config = yaml.safe_load(ACTIONLINT_CONFIG.read_text(encoding="utf-8"))
	assert config["self-hosted-runner"]["labels"] == [PINNED_RUNNER_LABEL]


def main() -> int:
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	for func in test_functions:
		func()
	print(f"OK: {len(test_functions)} runner image pin checks passed (pin {PINNED_RUNNER_LABEL})")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
