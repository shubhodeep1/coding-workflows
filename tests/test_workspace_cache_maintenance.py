#!/usr/bin/env python3
"""Contract tests for review workspace reuse and workspace-cache maintenance."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
STAGE_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
MAINTENANCE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workspace-cache-maintenance.yml"
REVIEW_TEMPLATE = REPO_ROOT / "workflow-templates" / "ai-review.yml"
REMOVAL_REGISTRY = REPO_ROOT / "docs" / "scripts-pending-removal.md"


def _workflow_text(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _workflow_doc(path: Path) -> dict[str, object]:
	doc = yaml.safe_load(path.read_text(encoding="utf-8"))
	if not isinstance(doc, dict):
		raise AssertionError(f"Workflow did not parse into a mapping: {path}")
	return doc


def _step(path: Path, step_name: str) -> dict[str, object]:
	jobs = _workflow_doc(path).get("jobs")
	if not isinstance(jobs, dict):
		raise AssertionError(f"Workflow jobs mapping missing in {path}")
	for job in jobs.values():
		if not isinstance(job, dict):
			continue
		steps = job.get("steps")
		if not isinstance(steps, list):
			continue
		for step in steps:
			if isinstance(step, dict) and str(step.get("name", "")).strip() == step_name:
				return step
	raise AssertionError(f"Step not found in {path}: {step_name}")


def _step_run_text(path: Path, step_name: str) -> str:
	run = _step(path, step_name).get("run")
	if not isinstance(run, str):
		raise AssertionError(f"Step does not define a run block: {step_name}")
	return run


def test_workspace_cache_maintenance_workflow_has_required_triggers_and_concurrency() -> None:
	workflow = _workflow_text(MAINTENANCE_WORKFLOW)
	assert 'name: Workspace Cache Maintenance' in workflow
	assert 'schedule:' in workflow
	assert 'cron: "43 3 * * *"' in workflow
	assert 'workflow_dispatch: {}' in workflow
	assert 'concurrency:' in workflow
	assert 'group: workspace-cache-maintenance-${{ github.repository }}' in workflow
	assert 'cancel-in-progress: false' in workflow


def test_workspace_cache_maintenance_workflow_uses_gh_pat_cache_surfaces_and_summary() -> None:
	workflow = _workflow_text(MAINTENANCE_WORKFLOW)
	assert 'GH_TOKEN: ${{ secrets.GH_PAT }}' in workflow
	assert 'gh cache list' in workflow
	assert '--key workspace-v1-' in workflow
	assert 'gh cache delete' in workflow
	assert 'KEEP_LATEST_PER_FAMILY="3"' in workflow
	assert 'issueOrPullRequest' in workflow
	assert 'GITHUB_STEP_SUMMARY' in workflow
	assert 'Workspace Cache Maintenance Summary' in workflow
	assert 'Issue/PR lookup warnings (retention-only fallback)' in workflow
	assert 'Issue/PR lookup errors' in workflow
	assert 'if delete_failures or lookup_errors:' in workflow


def test_review_autofix_stages_and_activates_workspace_reuse_before_reviewers() -> None:
	workflow = _workflow_text(REVIEW_WORKFLOW)
	stage_block = STAGE_HELPER.read_text(encoding="utf-8")
	metadata_block = _step_run_text(REVIEW_WORKFLOW, 'Initialize workspace metadata')
	activate_block = _step_run_text(REVIEW_WORKFLOW, 'Activate workspace shell context')
	cache_step = _step(REVIEW_WORKFLOW, 'Restore reusable workspace cache')

	assert 'workspace_init.sh' in stage_block
	assert 'workspace_reuse_enabled="${WORKSPACE_REUSE_ENABLED:-false}"' in metadata_block
	assert 'if [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] && [ "${PR_NUMBER}" -gt 0 ]; then' in metadata_block
	assert 'workspace_reuse_enabled="false"' in metadata_block
	assert 'WORKSPACE_REQUIRE_STABLE_IDENTIFIER_FOR_REUSE="true"' in metadata_block
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/workspace_init.sh" metadata' in metadata_block
	assert cache_step.get('uses') == 'actions/cache@v5'
	assert cache_step.get('with', {}).get('path') == '${{ steps.workspace_meta.outputs.workspace_path }}'
	assert cache_step.get('with', {}).get('restore-keys') == (
		'${{ steps.workspace_meta.outputs.workspace_cache_restore_prefix_exact }}\n'
		'${{ steps.workspace_meta.outputs.workspace_cache_restore_prefix_issue }}\n'
	)
	assert 'cd "${WORKSPACE_PATH}"' in activate_block
	assert 'echo "BASH_ENV=${workspace_shell_env}"' in activate_block
	assert 'echo "GIT_WORK_TREE=${WORKSPACE_PATH}"' in activate_block
	assert workflow.index('- name: Activate workspace shell context') < workflow.index('- name: Restore review-issue ledger')
	assert workflow.index('- name: Activate workspace shell context') < workflow.index('- name: Run reviewer models')


def test_review_wrapper_template_documents_workspace_reuse_contract() -> None:
	workflow = _workflow_text(REVIEW_TEMPLATE)
	assert 'uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable' in workflow
	assert 'vars.WORKSPACE_REUSE_ENABLED' in workflow


def test_workspace_init_keeps_reuse_enabled_for_explicit_review_identifier() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		output_path = Path(tmpdir) / 'github_output'
		env_path = Path(tmpdir) / 'github_env'
		env = os.environ.copy()
		env.update(
			{
				'GITHUB_OUTPUT': str(output_path),
				'GITHUB_ENV': str(env_path),
				'GITHUB_RUN_ID': '1',
				'GITHUB_RUN_ATTEMPT': '1',
				'GITHUB_WORKSPACE': str(REPO_ROOT),
				'RUNNER_TEMP': tmpdir,
				'WORKSPACE_REUSE_ENABLED': 'true',
				'WORKSPACE_REQUIRE_STABLE_IDENTIFIER_FOR_REUSE': 'true',
				'WORKSPACE_ISSUE_IDENTIFIER': '123',
				'WORKSPACE_SOURCE_PATH': str(REPO_ROOT),
			}
		)
		subprocess.run(
			['bash', str(REPO_ROOT / 'scripts' / 'workspace_init.sh'), 'metadata'],
			check=True,
			cwd=REPO_ROOT,
			env=env,
			capture_output=True,
			text=True,
		)
		output = output_path.read_text(encoding='utf-8')
		assert 'workspace_identifier_source=explicit' in output
		assert 'workspace_reuse_enabled=true' in output


def test_review_autofix_retargets_review_runtime_cache_into_workspace() -> None:
	restore_step = _step(REVIEW_WORKFLOW, 'Restore review-issue ledger')
	save_step = _step(REVIEW_WORKFLOW, 'Save review-issue ledger')

	for step in (restore_step, save_step):
		with_block = step.get('with', {})
		assert with_block.get('path') == (
			'${{ steps.workspace_state.outputs.workspace_path }}/.ai/review_issue_ledger/\n'
			'${{ steps.workspace_state.outputs.workspace_path }}/.ai/review_runtime/\n'
			'${{ steps.workspace_state.outputs.workspace_path }}/${{ env.REVIEW_LEDGER_PATH }}\n'
		)


def test_removal_registry_documents_workspace_cache_maintenance_workflow() -> None:
	registry = _workflow_text(REMOVAL_REGISTRY)
	assert '### `.github/workflows/workspace-cache-maintenance.yml`' in registry
	assert '- **Type:** long-running' in registry
	assert '- **Removal trigger:** permanent — review annually' in registry
	assert 'gh workflow view workspace-cache-maintenance.yml -R shubhodeep1/coding-workflows' in registry
	assert 'gh run list --workflow workspace-cache-maintenance.yml --limit 5 -R shubhodeep1/coding-workflows' in registry


def main() -> int:
	test_workspace_cache_maintenance_workflow_has_required_triggers_and_concurrency()
	test_workspace_cache_maintenance_workflow_uses_gh_pat_cache_surfaces_and_summary()
	test_review_autofix_stages_and_activates_workspace_reuse_before_reviewers()
	test_review_wrapper_template_documents_workspace_reuse_contract()
	test_workspace_init_keeps_reuse_enabled_for_explicit_review_identifier()
	test_review_autofix_retargets_review_runtime_cache_into_workspace()
	test_removal_registry_documents_workspace_cache_maintenance_workflow()
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
