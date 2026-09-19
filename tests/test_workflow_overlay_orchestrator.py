#!/usr/bin/env python3
"""Contract tests for orchestrator workflow overlay wiring."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TEMPLATES_DIR = REPO_ROOT / "workflow-templates"

LOADER_SNIPPET = (
	'python3 scripts/load_workflow_overlay.py \\\n'
	'            --repo-root "${GITHUB_WORKSPACE}"'
)
IMMUTABLE_LOADER_SNIPPET = (
	'python3 "${clarify_respond_immutable_support_root}/scripts/load_workflow_overlay.py" \\\n'
	'            --repo-root "${GITHUB_WORKSPACE}"'
)
ORCHESTRATE_LOADER_SNIPPET = 'python3 "${orchestrate_immutable_support_root}/scripts/load_workflow_overlay.py" \\'
POLL_LOADER_SNIPPET = 'python3 "${poll_immutable_support_root}/scripts/load_workflow_overlay.py" \\'
LOADER_SCHEMA_SNIPPET = '--schema-path "ai-memory/schemas/workflow_overlay.v1.json"'
LOADER_ENV_SNIPPET = '--github-env "${GITHUB_ENV}"'

WORKFLOW_EXPECTATIONS = {
	"orchestrate.yml": 'bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-orchestrate.txt"',
	"orchestrate_poll.yml": 'bash "${SUPPORT_SCRIPTS_DIR}/orchestrate_poll_process.sh"',
	"orchestrate_clarify_respond.yml": 'bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-clarify-respond.txt"',
}

ORCHESTRATE_PROMPT_ASSETS = (
	"_prelude_common.txt",
	"_prelude_role_persona.txt",
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
		loader_snippet = {
			"orchestrate.yml": ORCHESTRATE_LOADER_SNIPPET,
			"orchestrate_poll.yml": POLL_LOADER_SNIPPET,
			"orchestrate_clarify_respond.yml": IMMUTABLE_LOADER_SNIPPET,
		}[workflow_name]
		assert "load_workflow_overlay.py" in workflow_text, workflow_name
		assert "workflow_overlay.v1.json" in workflow_text, workflow_name
		assert "WORKFLOW.md overlay is opt-in by file presence" in workflow_text, workflow_name
		assert loader_snippet in workflow_text, workflow_name
		assert LOADER_SCHEMA_SNIPPET in workflow_text, workflow_name
		assert LOADER_ENV_SNIPPET in workflow_text, workflow_name
		assert "immutable-bundle" in workflow_text, workflow_name
		assert downstream_snippet in workflow_text, workflow_name
		assert workflow_text.find(loader_snippet) < workflow_text.find(downstream_snippet), workflow_name


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
	assert "gh_helpers.sh emit_event.sh emit_event.py" in workflow_text
	assert "openrouter_prompt_cache.py semantic_cache.py" in workflow_text
	assert "python3 -I -B -c" in workflow_text
	assert "sys.path.insert(0, '${SUPPORT_SCRIPTS_DIR}')" in workflow_text
	assert "sys.path.insert(0, 'scripts')" not in workflow_text
	assert 'repository_agents_file="AGENTS.md"' in workflow_text


def test_orchestrator_runtime_helpers_reject_checkout_relative_execution() -> None:
	for workflow_name in ("orchestrate.yml", "orchestrate_poll.yml"):
		workflow_text = _read(WORKFLOWS_DIR / workflow_name)
		assert re.search(r"^\s*(?:source|bash|python3)\s+scripts/", workflow_text, re.MULTILINE) is None, workflow_name
	poller_text = _read(REPO_ROOT / "scripts" / "orchestrate_poll_process.sh")
	assert 'ORCHESTRATE_POLL_SUPPORT_SCRIPTS_DIR="${SUPPORT_SCRIPTS_DIR:-' in poller_text
	assert 'source "${ORCHESTRATE_POLL_SUPPORT_SCRIPTS_DIR}/gh_helpers.sh"' in poller_text
	assert "sys.path.insert(0, 'scripts')" not in poller_text
	assert poller_text.count("python3 -I -B") >= 7
	assert 'os.environ["ORCHESTRATE_POLL_SUPPORT_SCRIPTS_DIR"]' in poller_text
	assert 'cat "${ORCHESTRATE_POLL_SUPPORT_ROOT_DIR}/unattended_system_instructions.md"' in poller_text
	assert "cat unattended_system_instructions.md" not in poller_text


def test_orchestrator_model_instructions_are_immutable() -> None:
	for workflow_name in ("orchestrate.yml", "orchestrate_poll.yml", "orchestrate_clarify_respond.yml"):
		workflow_text = _read(WORKFLOWS_DIR / workflow_name)
		for required_path in ("unattended_system_instructions.md", "ai_pipeline.md", "agents.md"):
			assert f'"{required_path}"' in workflow_text, workflow_name
		assert "cat unattended_system_instructions.md" not in workflow_text, workflow_name
		assert "cat ai_pipeline.md" not in workflow_text, workflow_name
	static_context_text = _read(REPO_ROOT / "scripts" / "build_static_context.sh")
	assert 'if [ -f AGENTS.md ]; then' in static_context_text
	assert 'repository_agents="AGENTS.md"' in static_context_text
	assert 'emit_untrusted_repository_file "${repository_agents}" "${repository_agents}"' in static_context_text


def main() -> int:
	test_orchestrator_workflows_stage_overlay_loader_before_prompt_consumers()
	test_orchestrator_wrapper_templates_match_reusable_workflow_targets()
	test_orchestrate_workflow_stages_prompt_assembly_assets()
	test_orchestrator_runtime_helpers_reject_checkout_relative_execution()
	test_orchestrator_model_instructions_are_immutable()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
