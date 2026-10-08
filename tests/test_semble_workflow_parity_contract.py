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
	"SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'true' }}",
	"install_semble.sh",
	"build_semble_wrapper.sh",
	"SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index",
	"SEMBLE_INDEX_AVAILABLE=false",
)

DISALLOWED_SNIPPETS = (
	"semble query",
	"source scripts/semble_helpers.sh",
	# `semble index . --out ...` was the broken pre-extraction call; never
	# add it back without also reverting the build_semble_wrapper.sh swap.
	"semble index . --out",
)

AI_MEMORY_SCHEMA_SNIPPETS = (
	"validation_history.v1.json",
	"operator_bypass_audit.v1.json",
	"revalidate_events.v1.json",
)


def _workflow_text(filename: str) -> str:
	return (WORKFLOWS_DIR / filename).read_text(encoding="utf-8")


def test_target_workflows_keep_fail_open_semble_runtime_defaults() -> None:
	for workflow_name in sorted(TARGET_WORKFLOWS):
		wf = _workflow_text(workflow_name)
		for snippet in REQUIRED_SNIPPETS:
			assert snippet in wf, f"{workflow_name} missing Semble parity snippet: {snippet}"


def test_target_workflows_do_not_bootstrap_semble() -> None:
	for workflow_name in sorted(TARGET_WORKFLOWS):
		wf = _workflow_text(workflow_name)
		for snippet in ("- name: Install semble", "- name: Build semble index", "bash scripts/install_semble.sh", "bash scripts/build_semble_wrapper.sh", "uses: astral-sh/setup-uv@v7"):
			assert snippet not in wf, f"{workflow_name} still bootstraps Semble: {snippet}"


def test_target_workflows_do_not_add_query_calls_yet() -> None:
	for workflow_name in sorted(TARGET_WORKFLOWS):
		wf = _workflow_text(workflow_name)
		for snippet in DISALLOWED_SNIPPETS:
			assert snippet not in wf, f"{workflow_name} should not add Semble query wiring yet: {snippet}"


def test_target_workflows_stage_revalidate_lifecycle_ai_memory_schemas() -> None:
	for workflow_name in sorted(TARGET_WORKFLOWS):
		wf = _workflow_text(workflow_name)
		for snippet in AI_MEMORY_SCHEMA_SNIPPETS:
			assert snippet in wf, f"{workflow_name} missing ai-memory schema bootstrap snippet: {snippet}"


def main() -> int:
	test_target_workflows_keep_fail_open_semble_runtime_defaults()
	test_target_workflows_do_not_bootstrap_semble()
	test_target_workflows_do_not_add_query_calls_yet()
	test_target_workflows_stage_revalidate_lifecycle_ai_memory_schemas()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
