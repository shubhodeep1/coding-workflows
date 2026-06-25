#!/usr/bin/env python3
"""Focused contract checks for optional plan diagrams/failure-modes wiring."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_PROMPT = REPO_ROOT / "prompts" / "mode-plan.txt"
PLAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plan.yml"
PLAN_CONTRACT = REPO_ROOT / "prompts" / "contracts" / "mode-plan.yml"
AGENTS_MD = REPO_ROOT / "agents.md"
RENDER_PROMPT = REPO_ROOT / "scripts" / "render_prompt.py"


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _render_mode_plan(*extra_args: str) -> str:
	proc = subprocess.run(
		[sys.executable, str(RENDER_PROMPT), str(PLAN_PROMPT), *extra_args],
		cwd=str(REPO_ROOT),
		env=_base_env(),
		text=True,
		capture_output=True,
		timeout=60,
	)
	assert proc.returncode == 0, proc.stderr
	return proc.stdout


def test_mode_plan_diagram_contract_defaults_on() -> None:
	rendered = _render_mode_plan()

	assert "PLAN_DIAGRAMS_OPTIONAL=true" in rendered
	assert "11. Diagrams/failure-modes requirement gate (current render: `PLAN_DIAGRAMS_OPTIONAL=true`):" in rendered
	assert "between `API/interface changes.` and `Risks and edge cases.`" in rendered
	assert "`Data flow:` — include when the change introduces or modifies a multi-step" in rendered
	assert "flow. Use ASCII art or numbered prose, ≤ 20 lines." in rendered
	assert "`State machines:` — include when the change touches a state machine." in rendered
	assert "the states, transitions, and trigger for each transition." in rendered
	assert "`Failure modes:` — include for any change with non-trivial error paths." in rendered
	assert "Each entry should list trigger, observable symptom, and recovery path." in rendered
	assert "These fields are optional. Do not pad a trivial PR with diagrams or extra" in rendered
	assert "prose; simple plans should stay terse." in rendered
	assert "`State machines:` field is required, not optional." in rendered
	assert "`ai:clarification` → `ai:planning` → `ai:awaiting-approval` →" in rendered
	assert "`ai:implementing` → `ai:done` → `ai:ready-to-merge` → `ai:merged`" in rendered


def test_mode_plan_diagram_contract_forbids_sections_when_flag_disabled() -> None:
	rendered = _render_mode_plan("--var", "PLAN_DIAGRAMS_OPTIONAL=false")

	assert "PLAN_DIAGRAMS_OPTIONAL=false" in rendered
	assert "11. Diagrams/failure-modes requirement gate (current render: `PLAN_DIAGRAMS_OPTIONAL=false`):" in rendered
	assert "When the current render is" in rendered
	assert "do not emit `Data flow:`, `State machines:`, or `Failure modes:`." in rendered


def test_plan_workflow_and_contract_export_diagram_flag_with_live_prompt_parity() -> None:
	workflow = PLAN_WORKFLOW.read_text(encoding="utf-8")
	contract = PLAN_CONTRACT.read_text(encoding="utf-8")
	agents_md = AGENTS_MD.read_text(encoding="utf-8")

	assert "PLAN_DIAGRAMS_OPTIONAL: ${{ vars.PLAN_DIAGRAMS_OPTIONAL || 'true' }}" in workflow
	assert "11. Diagrams/failure-modes requirement gate (current render: `PLAN_DIAGRAMS_OPTIONAL={{PLAN_DIAGRAMS_OPTIONAL}}`):" in workflow
	assert "between `API/interface changes.` and `Risks and edge cases.`" in workflow
	assert "`Data flow:` — include when the change introduces or modifies a multi-step" in workflow
	assert "`State machines:` — include when the change touches a state machine." in workflow
	assert "`Failure modes:` — include for any change with non-trivial error paths." in workflow
	assert "These fields are optional. Do not pad a trivial PR with diagrams or extra" in workflow
	assert "prose; simple plans should stay terse." in workflow
	assert "`State machines:` field is required, not optional." in workflow
	assert "PLAN_DIAGRAMS_OPTIONAL: true" in contract
	assert "Plan prompt note: `PLAN_DIAGRAMS_OPTIONAL` defaults to `true`, so plan outputs" in agents_md
	assert "Trivial plans should omit them, and `State machines:` is" in agents_md
	assert "required only for changes touching the orchestrator phase machine" in agents_md


def main() -> int:
	test_mode_plan_diagram_contract_defaults_on()
	test_mode_plan_diagram_contract_forbids_sections_when_flag_disabled()
	test_plan_workflow_and_contract_export_diagram_flag_with_live_prompt_parity()
	print("OK: plan diagrams contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
