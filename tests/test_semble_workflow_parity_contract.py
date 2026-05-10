#!/usr/bin/env python3
"""Contract tests for Semble parity in targeted reusable workflows."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

TARGET_WORKFLOWS = {
	"clarify.yml",
	"plan.yml",
	"orchestrate.yml",
	"orchestrate_clarify_respond.yml",
}

REQUIRED_SNIPPETS = (
	"SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}",
	"install_semble.sh",
	"uses: astral-sh/setup-uv@v3",
	"- name: Install semble",
	"bash scripts/install_semble.sh",
	"- name: Build semble index",
	"SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index",
	"SEMBLE_INDEX_AVAILABLE=false",
	"SEMBLE_INDEX_AVAILABLE=true",
	"semble index . --out \"${SEMBLE_INDEX_PATH}\"",
	"env.SEMBLE_ENABLED == 'true'",
)

DISALLOWED_SNIPPETS = (
	"semble query",
	"source scripts/semble_helpers.sh",
)


def _workflow_text(filename: str) -> str:
	return (WORKFLOWS_DIR / filename).read_text(encoding="utf-8")


def test_target_workflows_define_semble_parity_contract() -> None:
	for workflow_name in sorted(TARGET_WORKFLOWS):
		wf = _workflow_text(workflow_name)
		for snippet in REQUIRED_SNIPPETS:
			assert snippet in wf, f"{workflow_name} missing Semble parity snippet: {snippet}"


def test_target_workflows_order_semble_steps_before_codex_config() -> None:
	for workflow_name in sorted(TARGET_WORKFLOWS):
		wf = _workflow_text(workflow_name)
		stage_idx = wf.find("- name: Stage workflow support files")
		runtime_idx = wf.find("SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index")
		setup_idx = wf.find("- name: Setup uv", max(stage_idx, runtime_idx) + 1)
		install_idx = wf.find("- name: Install semble", setup_idx + 1)
		build_idx = wf.find("- name: Build semble index", install_idx + 1)
		config_idx = wf.find("- name: Create Codex config", build_idx + 1)

		assert stage_idx != -1, f"{workflow_name} missing support-file staging step"
		assert runtime_idx != -1, f"{workflow_name} missing runtime Semble index path contract"
		assert setup_idx != -1, f"{workflow_name} missing Setup uv step"
		assert install_idx != -1, f"{workflow_name} missing Install semble step"
		assert build_idx != -1, f"{workflow_name} missing Build semble index step"
		assert config_idx != -1, f"{workflow_name} missing Create Codex config step"

		assert stage_idx < setup_idx, f"{workflow_name} must stage install_semble.sh before Semble setup"
		assert runtime_idx < setup_idx, f"{workflow_name} must define Semble runtime env before setup"
		assert setup_idx < install_idx < build_idx < config_idx, (
			f"{workflow_name} must run Setup uv, Install semble, and Build semble index before Codex config"
		)


def test_target_workflows_do_not_add_query_calls_yet() -> None:
	for workflow_name in sorted(TARGET_WORKFLOWS):
		wf = _workflow_text(workflow_name)
		for snippet in DISALLOWED_SNIPPETS:
			assert snippet not in wf, f"{workflow_name} should not add Semble query wiring yet: {snippet}"


def main() -> int:
	test_target_workflows_define_semble_parity_contract()
	test_target_workflows_order_semble_steps_before_codex_config()
	test_target_workflows_do_not_add_query_calls_yet()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
