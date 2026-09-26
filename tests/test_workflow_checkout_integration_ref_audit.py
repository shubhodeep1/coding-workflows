#!/usr/bin/env python3
"""Audit actions/checkout@v5 workflows for integration-ref safety contracts."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
REVIEWED_CODEX_ACTION_SHA = "f03b8d657a63d64b87324e8a1047a8b90d3221e0"
REVIEWED_SETUP_NODE_ACTION_SHA = "249970729cb0ef3589644e2896645e5dc5ba9c38"
REVIEWED_SETUP_PYTHON_ACTION_SHA = "ece7cb06caefa5fff74198d8649806c4678c61a1"
REVIEWED_SETUP_UV_ACTION_SHA = "37802adc94f370d6bfd71619e3f0bf239e1f3b78"
REVIEWED_FREE_DISK_SPACE_ACTION_SHA = "54081f138730dfa15788a46383842cd2f914a1be"

REQUIRED_RESOLVER_WORKFLOWS = {
	"plan.yml",
	"clarify.yml",
	"orchestrate_clarify_respond.yml",
	"implement.yml",
	"validate.yml",
}

# Explicit allow-list exceptions for checkout@v5 workflows that are not
# orchestrator-managed issue execution paths.
ALLOWLIST_EXCEPTIONS = {
	"audit_consumer_drift.yml": "Consumer-wrapper drift audit is scheduled/manual repository maintenance, not an orchestrator issue-phase checkout path.",
	"cancel_on_pr_close.yml": "PR-close cleanup cancels branch runs and has no orchestrator issue-phase checkout.",
	"ci.yml": "PR CI validation has no orchestrator issue metadata.",
	"claude-issue-intake.yml": "Claude issue intake is repository_dispatch automation that fires the dispatcher routine from the default branch; it runs no orchestrator issue phase and reads no issue metadata into a checkout ref.",
	"integration-pr-readiness.yml": "Integration-PR readiness check runs on pull_request refs and posts commit status metadata, not orchestrator issue-phase checkout.",
	"issue_pr_status.yml": "Issue/PR status utility workflow does not execute orchestrator issue phases.",
	"lint-plan-archival.yml": "Plan-archival lint validates pull_request body/diff state rather than orchestrator issue-phase integration refs.",
	"lint-pr-body-auto-close.yml": "PR-body auto-close lint validates pull_request metadata rather than orchestrator issue-phase checkout.",
	"mark-stable.yml": "Release promotion workflow operates on repo refs, not tracking-issue metadata.",
	"memory_maintenance.yml": "Maintenance workflow has no issue-comment or tracking-issue context.",
	"orchestrate.yml": "Project bootstrap workflow has no integration-branch metadata at checkout time.",
	"orchestrate_poll.yml": "Poller handles multiple tracking issues per run; a single checkout integration ref is undefined.",
	"review_autofix.yml": "PR review/autofix operates on PR refs rather than orchestrator integration metadata.",
	"security-audit.yml": "Scheduled/manual default-branch security audit is a source-repo maintenance workflow, not an orchestrator issue-phase checkout path.",
	"sync_ai_labels.yml": "Repository label-sync maintenance manages ai:* labels and does not execute orchestrator issue phases.",
	"test-and-mark-stable.yml": "Release test workflow checks specific refs/tags and is outside orchestrator phase execution.",
	"comprehensive-test-and-release.yml": "Comprehensive release conductor dispatches downstream workflows and does not execute tracking-issue integration-ref checkout.",
	"drift-audit.yml": "Scheduled maintenance workflow audits review-autofix logs and is not an orchestrator issue-phase checkout path.",
	"check_failure_triage.yml": "Check-run triage operates on failing PR-check metadata and PR head refs, not orchestrator issue-phase integration refs.",
	"update_workflows.yml": "Workflow-template sync job is repository maintenance, not issue-phase execution.",
	"validation-improvements-intake.yml": "Validation prompt intake workflow is repository_dispatch PR automation.",
	"validation-refresh.yml": "Validation refresh workflow iterates consumer repos and is not an orchestrator issue-phase checkout path.",
	"workflow-log-analysis.yml": "Workflow-log analyzer inspects run artifacts, not orchestrator issue branches.",
	"nightly-validation-selftest.yml": "Nightly fixture self-test runs on schedule/workflow_dispatch without orchestrator issue metadata.",
	"workspace-cache-maintenance.yml": "Scheduled workspace-cache pruning operates on repository cache metadata, not orchestrator issue-phase integration refs.",
	"forward-merge-stable-to-main.yml": "Stable→main forward-merge workflow operates on repo refs (stable, main), not tracking-issue metadata.",
	"promote-main-to-stable.yml": "Main→stable promotion workflow operates on repo refs (main, stable), not tracking-issue metadata.",
	"auto-release-stable.yml": "Scheduled stable-branch release check operates on repo refs (stable branch vs stable tag), not tracking-issue metadata.",
	"workflow_failure_heal.yml": "Escalation reporter checks out the repo only to read consumer wrapper release pins and dispatches upstream; it executes no orchestrator issue phase.",
	"workflow-failure-heal-intake.yml": "Heal intake is repository_dispatch / workflow_run issue-filing automation on the default branch, not an orchestrator issue-phase checkout path.",
	"internal-cancel-on-pr-close.yml": "Heal PR reconcile checks out main on pull_request close to merge or close heal PRs of the closed PR; it executes no orchestrator issue phase.",
}


def _workflow_text(filename: str) -> str:
	return (WORKFLOWS_DIR / filename).read_text(encoding="utf-8")


def _checkout_workflow_files() -> set[str]:
	files: set[str] = set()
	for path in WORKFLOWS_DIR.glob("*.yml"):
		if "uses: actions/checkout@v5" in path.read_text(encoding="utf-8"):
			files.add(path.name)
	return files


def test_checkout_workflows_are_all_classified() -> None:
	checkout_files = _checkout_workflow_files()
	classified = REQUIRED_RESOLVER_WORKFLOWS | set(ALLOWLIST_EXCEPTIONS.keys())
	missing = sorted(checkout_files - classified)
	extra = sorted(classified - checkout_files)
	assert not missing, f"Unclassified checkout@v5 workflows: {missing}"
	assert not extra, f"Classified workflows without checkout@v5: {extra}"


def test_allowlist_entries_have_rationale() -> None:
	for workflow_name, rationale in ALLOWLIST_EXCEPTIONS.items():
		assert rationale.strip(), f"Missing allow-list rationale for {workflow_name}"


def test_remote_codex_runtime_actions_are_immutable() -> None:
	paths = [REPO_ROOT / ".github" / "actions" / "setup-runtime" / "action.yml", *WORKFLOWS_DIR.glob("*.yml")]
	runtime_action_text = paths[0].read_text(encoding="utf-8")
	assert f"actions/setup-node@{REVIEWED_SETUP_NODE_ACTION_SHA}" in runtime_action_text
	assert f"actions/setup-python@{REVIEWED_SETUP_PYTHON_ACTION_SHA}" in runtime_action_text
	for path in paths:
		text = path.read_text(encoding="utf-8")
		assert "install-codex@stable" not in text, path
		assert "setup-runtime@stable" not in text, path
		assert "Intentionally use the released action ref as the rollout boundary" not in text, path
		for line in text.splitlines():
			if "uses: actions/setup-node@" in line:
				assert line.rstrip().endswith(f"actions/setup-node@{REVIEWED_SETUP_NODE_ACTION_SHA}"), (path, line)
			if "uses: actions/setup-python@" in line:
				assert line.rstrip().endswith(f"actions/setup-python@{REVIEWED_SETUP_PYTHON_ACTION_SHA}"), (path, line)
			if "uses: astral-sh/setup-uv@" in line:
				assert line.rstrip().endswith(f"astral-sh/setup-uv@{REVIEWED_SETUP_UV_ACTION_SHA}"), (path, line)
			if "uses: jlumbroso/free-disk-space@" in line:
				assert line.rstrip().endswith(
					f"jlumbroso/free-disk-space@{REVIEWED_FREE_DISK_SPACE_ACTION_SHA}"
				), (path, line)
			if "shubhodeep1/coding-workflows/.github/actions/install-codex@" in line:
				assert line.rstrip().endswith(f"install-codex@{REVIEWED_CODEX_ACTION_SHA}"), (path, line)
		for cache_block in text.split("- name: Cache Codex CLI")[1:]:
			cache_key_line = next(line.strip() for line in cache_block.splitlines() if line.strip().startswith("key:"))
			assert REVIEWED_CODEX_ACTION_SHA in cache_key_line, (path, cache_key_line)


def test_required_workflows_enforce_integration_ref_contract() -> None:
	resolver_step = "- name: Resolve integration ref"
	resolver_id = "id: refctx"
	canonical_stage = "resolver_stage_root=\"${RUNNER_TEMP}/resolve-integration-ref-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}\""
	canonical_exec = "bash \"${resolver_script}\""
	base_disallowed_inline_markers = (
		"contents/scripts/resolve_integration_ref.sh?ref=${resolver_ref}",
		"base64 --decode >",
		"sed -nE 's/^- Integration branch:",
		"grep -Eq '^orchestrator/project-[0-9]+$'",
		"/git/ref/heads/",
		'resolver_ref="stable"',
		"resolver_stage_fallback",
		"--branch main",
	)

	# Keep the GitHub ref-lookup ban shared so future baseline-resolution logic
	# cannot slip into another required workflow without updating this audit.
	for workflow_name in sorted(REQUIRED_RESOLVER_WORKFLOWS):
		wf = _workflow_text(workflow_name)
		disallowed_inline_markers = base_disallowed_inline_markers
		if workflow_name == "implement.yml":
			checkout_ref = "ref: ${{ steps.checkout_ref.outputs.ref || steps.refctx.outputs.ref || github.event.repository.default_branch }}"
			resolved_ref_env = "IMPLEMENT_RESOLVED_FALLBACK_REF: ${{ steps.checkout_ref.outputs.ref || steps.refctx.outputs.ref || github.event.repository.default_branch }}"
			resolved_ref_log = 'echo "Resolved fallback ref: ${IMPLEMENT_RESOLVED_FALLBACK_REF}"'
			unsafe_resolved_ref_log = "echo \"Resolved fallback ref: ${{ steps.checkout_ref.outputs.ref || steps.refctx.outputs.ref || github.event.repository.default_branch }}\""
			resolved_base_env = "IMPLEMENT_LOG_PR_BASE_REF: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}"
			resolved_base_log = 'echo "PR base ref: ${IMPLEMENT_LOG_PR_BASE_REF}"'
			unsafe_resolved_base_log = "echo \"PR base ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}\""
			checkout_resolver_step = "- name: Resolve checkout ref"
			checkout_resolver_id = "id: checkout_ref"
		else:
			# validate.yml authorizes an explicit `target_ref` against a
			# project PR and pins its head SHA, which wins over the tracking
			# issue's integration branch.
			ref_expression = (
				"steps.authorized_target.outputs.sha || steps.refctx.outputs.ref || github.event.repository.default_branch"
				if workflow_name == "validate.yml"
				else "steps.refctx.outputs.ref || github.event.repository.default_branch"
			)
			checkout_ref = f"ref: ${{{{ {ref_expression} }}}}"
			resolved_ref_name = {
				"clarify.yml": "CLARIFY_RESOLVED_REF",
				"orchestrate_clarify_respond.yml": "ORCHESTRATE_CLARIFY_RESOLVED_REF",
				"plan.yml": "PLAN_RESOLVED_REF",
				"validate.yml": "VALIDATE_RESOLVED_REF",
			}[workflow_name]
			resolved_ref_env = f"{resolved_ref_name}: ${{{{ {ref_expression} }}}}"
			resolved_ref_log = f'echo "Resolved ref: ${{{resolved_ref_name}}}"'
			unsafe_resolved_ref_log = "echo \"Resolved ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}\""
			resolved_base_env = ""
			resolved_base_log = ""
			unsafe_resolved_base_log = ""
			checkout_resolver_step = ""
			checkout_resolver_id = ""

		assert resolver_step in wf, f"{workflow_name} missing integration ref resolver step"
		assert resolver_id in wf, f"{workflow_name} missing resolver step id refctx"
		assert checkout_ref in wf, f"{workflow_name} checkout is missing refctx/default branch ref"
		assert resolved_ref_env in wf, f"{workflow_name} missing env-bound resolved-ref log input"
		assert resolved_ref_log in wf, f"{workflow_name} missing resolved-ref log output"
		assert unsafe_resolved_ref_log not in wf, f"{workflow_name} interpolates resolver output into shell source"
		if resolved_base_log:
			assert resolved_base_env in wf, f"{workflow_name} missing env-bound base-ref log input"
			assert resolved_base_log in wf, f"{workflow_name} missing base-ref log output"
			assert unsafe_resolved_base_log not in wf, f"{workflow_name} interpolates base-ref output into shell source"
			assert checkout_resolver_step in wf, f"{workflow_name} missing checkout override resolver step"
			assert checkout_resolver_id in wf, f"{workflow_name} missing checkout override resolver id"
		assert "git rev-parse HEAD" in wf, f"{workflow_name} missing HEAD commit log"
		assert "git symbolic-ref --short HEAD" in wf, f"{workflow_name} missing branch/detached log"
		assert canonical_stage in wf, f"{workflow_name} missing canonical resolver staging"
		assert canonical_exec in wf, f"{workflow_name} missing canonical resolver invocation"

		for marker in disallowed_inline_markers:
			assert marker not in wf, f"{workflow_name} still contains inline resolver marker: {marker}"

		resolver_idx = wf.find(resolver_step)
		checkout_resolver_idx = wf.find(checkout_resolver_step) if checkout_resolver_step else -1
		checkout_ref_idx = wf.find(checkout_ref)
		resolver_block = wf[resolver_idx:checkout_ref_idx]
		assert "WORKFLOW_DEFINITION_REPOSITORY: ${{ fromJSON(toJSON(job)).workflow_repository }}" in resolver_block
		assert "WORKFLOW_DEFINITION_SHA: ${{ fromJSON(toJSON(job)).workflow_sha }}" in resolver_block
		assert 'resolver_ref="${WORKFLOW_DEFINITION_SHA,,}"' in resolver_block
		assert 'git -C "${resolver_stage_primary}" init --quiet' in resolver_block
		assert 'fetch --quiet --depth 1 origin "${resolver_ref}"' in resolver_block
		assert "Failed to stage canonical integration resolver at immutable ref ${resolver_ref}." in resolver_block
		assert "Canonical integration resolver script not found in staged source." in resolver_block
		assert "Canonical integration resolver failed." in resolver_block
		assert "Failed to stage canonical integration resolver at immutable ref ${resolver_ref}; falling back to default branch." not in resolver_block
		assert "Canonical integration resolver script not found in staged source; falling back to default branch." not in resolver_block
		assert "Canonical integration resolver failed; falling back to default branch." not in resolver_block
		assert resolver_block.count('echo "ref=" >> "$GITHUB_OUTPUT"') == 1
		assert resolver_idx != -1 and checkout_ref_idx != -1 and resolver_idx < checkout_ref_idx, (
			f"{workflow_name} must resolve integration ref before refctx-bound checkout"
		)
		if checkout_resolver_step:
			assert checkout_resolver_idx != -1 and resolver_idx < checkout_resolver_idx < checkout_ref_idx, (
				f"{workflow_name} must resolve the baseline checkout override after refctx and before checkout"
			)


def main() -> int:
	test_checkout_workflows_are_all_classified()
	test_allowlist_entries_have_rationale()
	test_remote_codex_runtime_actions_are_immutable()
	test_required_workflows_enforce_integration_ref_contract()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
