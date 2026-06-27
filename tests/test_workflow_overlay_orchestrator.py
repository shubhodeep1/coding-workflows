#!/usr/bin/env python3
"""Contract tests for orchestrator workflow overlay wiring."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TEMPLATES_DIR = REPO_ROOT / "workflow-templates"

LOADER_SNIPPET = (
	'python3 scripts/load_workflow_overlay.py \\\n'
	'            --repo-root "${GITHUB_WORKSPACE}"'
)
LOADER_SCHEMA_SNIPPET = '--schema-path "ai-memory/schemas/workflow_overlay.v1.json"'
LOADER_ENV_SNIPPET = '--github-env "${GITHUB_ENV}"'

WORKFLOW_EXPECTATIONS = {
	"orchestrate.yml": 'bash scripts/render_prompt.sh prompts/mode-orchestrate.txt',
	"orchestrate_poll.yml": "bash scripts/orchestrate_poll_process.sh",
	"orchestrate_clarify_respond.yml": 'bash scripts/render_prompt.sh prompts/mode-clarify-respond.txt',
}

ORCHESTRATE_PROMPT_ASSETS = (
	"_prelude_common.txt",
	"_prelude_output_contract.txt",
	"_templates/mode-orchestrate.txt",
)

TEMPLATE_EXPECTATIONS = {
	"ai-orchestrate.yml": {
		"uses": "uses: shubhodeep1/coding-workflows/.github/workflows/orchestrate.yml@stable",
		"snippets": ("project_description: ${{ inputs.project_description }}",),
	},
	"ai-orchestrate-poll.yml": {
		"uses": "uses: shubhodeep1/coding-workflows/.github/workflows/orchestrate_poll.yml@stable",
		"snippets": (),
	},
	"ai-orchestrate-clarify-respond.yml": {
		"uses": "uses: shubhodeep1/coding-workflows/.github/workflows/orchestrate_clarify_respond.yml@stable",
		"snippets": (),
	},
}


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def test_orchestrator_workflows_stage_overlay_loader_before_prompt_consumers() -> None:
	for workflow_name, downstream_snippet in WORKFLOW_EXPECTATIONS.items():
		workflow_text = _read(WORKFLOWS_DIR / workflow_name)
		assert "load_workflow_overlay.py" in workflow_text, workflow_name
		assert "workflow_overlay.v1.json" in workflow_text, workflow_name
		assert "WORKFLOW.md overlay is opt-in by file presence" in workflow_text, workflow_name
		assert LOADER_SNIPPET in workflow_text, workflow_name
		assert LOADER_SCHEMA_SNIPPET in workflow_text, workflow_name
		assert LOADER_ENV_SNIPPET in workflow_text, workflow_name
		assert downstream_snippet in workflow_text, workflow_name
		assert workflow_text.find(LOADER_SNIPPET) < workflow_text.find(downstream_snippet), workflow_name


def test_orchestrator_wrapper_templates_match_reusable_workflow_targets() -> None:
	for template_name, expectation in TEMPLATE_EXPECTATIONS.items():
		template_text = _read(TEMPLATES_DIR / template_name)
		assert expectation["uses"] in template_text, template_name
		assert "secrets: inherit" in template_text, template_name
		for snippet in expectation["snippets"]:
			assert snippet in template_text, template_name


def test_orchestrate_workflow_stages_prompt_assembly_assets() -> None:
	workflow_text = _read(WORKFLOWS_DIR / "orchestrate.yml")
	assert "for prompt_assembly_asset in " in workflow_text
	for prompt_asset in ORCHESTRATE_PROMPT_ASSETS:
		assert prompt_asset in workflow_text


def main() -> int:
	test_orchestrator_workflows_stage_overlay_loader_before_prompt_consumers()
	test_orchestrator_wrapper_templates_match_reusable_workflow_targets()
	test_orchestrate_workflow_stages_prompt_assembly_assets()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
