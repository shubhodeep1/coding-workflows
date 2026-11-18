#!/usr/bin/env python3
"""Engine label plumbing (plan Phase 6, decision D2).

`ai:engine-claude` is propagated for the separate role cutovers;
`ai:codex` wins when both are present. The orchestrator
applies the label from its `engine` input at creation, the poller copies the
tracking issue's label to every issue and PR it creates, implement copies the
issue's label to its PR, and scripts/claude_engine.py can read work-item labels
when a role supplies them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE = ROOT / ".github" / "workflows" / "orchestrate.yml"
POLLER = ROOT / "scripts" / "orchestrate_poll_process.sh"
IMPLEMENT = ROOT / ".github" / "workflows" / "implement.yml"


def _steps(path: Path, job: str) -> dict[str, dict]:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	return {step.get("name", ""): step for step in workflow["jobs"][job]["steps"]}


def test_label_is_in_the_contract_and_the_catalog() -> None:
	contract = json.loads((ROOT / ".github" / "ai" / "label_contract.v1.json").read_text(encoding="utf-8"))
	assert "ai:engine-claude" in contract["labels"]
	assert "ai:codex" in contract["labels"]
	assert "ai:engine-claude" not in contract.get("retired_labels", [])
	helpers = (ROOT / "scripts" / "label_helpers.sh").read_text(encoding="utf-8")
	assert helpers.count('["ai:engine-claude"]=') == 2


def test_orchestrate_input_is_declared_and_forwarded() -> None:
	workflow = yaml.safe_load(ORCHESTRATE.read_text(encoding="utf-8"))
	triggers = workflow.get("on", workflow.get(True))
	engine = triggers["workflow_call"]["inputs"]["engine"]
	assert engine["required"] is False and engine["default"] == ""
	for caller in (ROOT / ".github" / "workflows" / "internal-orchestrate.yml", ROOT / "workflow-templates" / "ai-orchestrate.yml"):
		data = yaml.safe_load(caller.read_text(encoding="utf-8"))
		on = data.get("on", data.get(True))
		assert on["workflow_dispatch"]["inputs"]["engine"]["default"] == "", caller.name
		assert data["jobs"]["orchestrate"]["with"]["engine"] == "${{ inputs.engine }}", caller.name


def _run_label_step(tmp_path: Path, engine: str) -> tuple[subprocess.CompletedProcess, str, list[str]]:
	step = _steps(ORCHESTRATE, "orchestrate")["Ensure orchestrator labels exist"]
	(tmp_path / "scripts").mkdir(exist_ok=True)
	calls = tmp_path / "calls.txt"
	(tmp_path / "scripts" / "label_helpers.sh").write_text(
		f'ensure_label_exists() {{ printf "%s\\n" "$1" >> "{calls}"; }}\n', encoding="utf-8"
	)
	github_env = tmp_path / "github_env"
	github_env.write_text("", encoding="utf-8")
	script = step["run"].replace("${{ github.repository }}", "o/r")
	env = dict(os.environ, ENGINE_INPUT=engine, GITHUB_ENV=str(github_env))
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, cwd=tmp_path, check=False)
	labels = calls.read_text(encoding="utf-8").split() if calls.exists() else []
	return result, github_env.read_text(encoding="utf-8"), labels


@pytest.mark.parametrize(
	"engine, label",
	[("", ""), ("claude", "ai:engine-claude"), (" Claude ", "ai:engine-claude"), ("codex", "ai:codex")],
)
def test_orchestrate_maps_the_engine_input_to_a_label(tmp_path: Path, engine: str, label: str) -> None:
	result, github_env, ensured = _run_label_step(tmp_path, engine)
	assert result.returncode == 0, result.stderr
	assert f"PROJECT_ENGINE_LABEL={label}\n" in github_env
	assert (label in ensured) is bool(label)
	assert "ai:orchestrator-tracking" in ensured


def test_orchestrate_rejects_an_unknown_engine(tmp_path: Path) -> None:
	result, github_env, _ = _run_label_step(tmp_path, "gpt")
	assert result.returncode == 1
	assert "engine must be empty, claude or codex" in result.stdout
	assert "PROJECT_ENGINE_LABEL" not in github_env


def test_orchestrate_rejects_invalid_engine_before_decomposition() -> None:
	steps = _steps(ORCHESTRATE, "orchestrate")
	assert list(steps).index("Log trigger context") < list(steps).index("Ensure orchestrator labels exist")
	script = steps["Log trigger context"]["run"].replace("${{ github.run_id }}", "1")
	for engine_value, accepted in (("gpt", False), (" Claude ", True), ("", True)):
		result = subprocess.run(
			["bash", "-c", script], capture_output=True, text=True, check=False,
			env=dict(os.environ, ENGINE_INPUT=engine_value, PROJECT_DESCRIPTION="test"),
		)
		assert (result.returncode == 0) is accepted
	assert list(steps).index("Log trigger context") < list(steps).index("Run Codex (decomposer)")


def test_orchestrate_applies_the_label_at_creation() -> None:
	steps = _steps(ORCHESTRATE, "orchestrate")
	tracking = steps["Create tracking issue"]["run"]
	assert 'tracking_create_args+=(--label "${PROJECT_ENGINE_LABEL}")' in tracking
	assert '"${tracking_create_args[@]}")"' in tracking
	children = steps["Create Wave 1 issues only"]["run"]
	assert 'child_create_args+=(--label "${PROJECT_ENGINE_LABEL}")' in children
	assert '"${child_create_args[@]}")"' in children


def test_every_poller_issue_and_pr_creation_inherits_the_label() -> None:
	text = POLLER.read_text(encoding="utf-8")
	lines = text.splitlines()
	creations = [
		i for i, line in enumerate(lines)
		if re.search(r"\bgh (issue|pr) create\b", line) and not line.lstrip().startswith("#")
	]
	assert len(creations) >= 15
	for i in creations:
		assert '"${_engine_label_args[@]}"' in lines[i], lines[i]
		assert lines[i - 1].strip().startswith("mapfile -t _engine_label_args < <(engine_label_create_args"), lines[i - 1]


def _helper() -> str:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index("engine_label_create_args()\n{")
	end = text.index("\n}\n", start) + 3
	return text[start:end]


@pytest.mark.parametrize(
	"tracking, arg, expected",
	[
		('["ai:orchestrator-tracking","ai:engine-claude"]', None, ["--label", "ai:engine-claude"]),
		('["ai:engine-claude","ai:codex"]', None, ["--label", "ai:codex"]),
		("[]", None, []),
		("not json", None, []),
		("[]", '[{"name":"ai:engine-claude"}]', ["--label", "ai:engine-claude"]),
		('["ai:engine-claude"]', "[]", []),
	],
)
def test_the_poller_helper(tracking: str, arg: str | None, expected: list[str]) -> None:
	call = "engine_label_create_args" + (f" '{arg}'" if arg is not None else "")
	script = f"set -euo pipefail\n{_helper()}\nmapfile -t a < <({call})\nprintf '%s\\n' \"${{a[@]}}\"\n"
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=dict(os.environ, TRACKING_LABELS=tracking), check=True)
	assert [line for line in result.stdout.splitlines() if line] == expected


def test_the_poller_helper_tolerates_an_unset_tracking_label_list() -> None:
	script = f"set -euo pipefail\nunset TRACKING_LABELS\n{_helper()}\nmapfile -t a < <(engine_label_create_args)\necho \"n=${{#a[@]}}\"\n"
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
	assert result.stdout.strip() == "n=0"


def test_implement_copies_the_issue_label_to_its_pr() -> None:
	step = _steps(IMPLEMENT, "implement")["Create Pull Request"]["run"]
	assert '"ai:codex" elif index("ai:engine-claude") then "ai:engine-claude"' in step
	assert 'PR_CREATE_ARGS+=(--label "${PR_ENGINE_LABEL}")' in step
	assert step.index('PR_CREATE_ARGS+=(--label "${PR_ENGINE_LABEL}")') < step.index('gh pr create "${PR_CREATE_ARGS[@]}"')
	assert 'gh_retry gh pr edit "${EXISTING_PR}"' in step
	assert step.index('gh_retry gh pr edit "${EXISTING_PR}"') < step.index('echo "pr_url=${EXISTING_PR}" >> "$GITHUB_OUTPUT"')


def test_implement_plan_claude_dispatches_with_the_engine_input() -> None:
	for base in (ROOT / ".claude", ROOT / "workflow-templates" / ".claude"):
		text = (base / "commands" / "implement-plan-claude.md").read_text(encoding="utf-8")
		assert "-f engine=claude" in text
		assert "has no `engine` input yet" in text
		assert "role cutovers" in text
		assert "every role of this project on the Claude engine" not in text
