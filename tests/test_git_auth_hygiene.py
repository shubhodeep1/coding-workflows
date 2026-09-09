#!/usr/bin/env python3
"""Security contracts for model-visible Git metadata and terminal approvals."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = [
	"clarify.yml",
	"plan.yml",
	"implement.yml",
	"review_autofix.yml",
	"orchestrate.yml",
	"orchestrate_poll.yml",
	"orchestrate_clarify_respond.yml",
	"validate.yml",
	"security-audit.yml",
	"check_failure_triage.yml",
	"workflow-log-analysis.yml",
	"memory_maintenance.yml",
	"opencode-live-smoke.yml",
	"issue_pr_status.yml",
	"validation-improvements-intake.yml",
	"update_workflows.yml",
]
PRODUCTION_GIT_FILES = [
	"scripts/stage_workflow_support.sh",
	"scripts/memory_helpers.sh",
	"scripts/ai_memory_lib.py",
	"scripts/review_commit_changes.sh",
	"scripts/review_conflict_resolve.sh",
	"scripts/review_rb_judge.sh",
	*[f".github/workflows/{name}" for name in WORKFLOWS],
]


def test_every_planned_checkout_disables_persisted_credentials() -> None:
	for workflow_name in WORKFLOWS:
		text = (REPO_ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
		blocks = re.split(r"(?m)^\s*- name:", text)
		checkout_blocks = [block for block in blocks if "uses: actions/checkout@" in block]
		assert checkout_blocks, workflow_name
		for block in checkout_blocks:
			assert "persist-credentials: false" in block, workflow_name


def test_production_git_urls_do_not_embed_tokens() -> None:
	credential_url = re.compile(r"https://[^\s\"']*(?:GH_TOKEN|GH_PAT|x-access-token:)[^\s\"']*@")
	for relative_path in PRODUCTION_GIT_FILES:
		text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
		assert credential_url.search(text) is None, relative_path


def test_networked_git_steps_use_ephemeral_authentication() -> None:
	expected_steps = {
		"orchestrate.yml": ["Validate decomposition JSON"],
		"review_autofix.yml": [
			"Checkout PR head branch",
			"Count autofix iterations",
			"Generate diff context",
			"Pre-review deterministic merge-topology gate",
			"Detect merge conflicts",
			"Push all pending commits",
		],
		"update_workflows.yml": ["Commit and push updates"],
		"validation-improvements-intake.yml": ["Commit, push, and open draft PR"],
		"workflow-log-analysis.yml": [
			"Sync latest report from target branch",
			"Commit and push report",
			"Commit and push deep audit section",
			"Commit and push API redundancy section",
		],
	}
	for workflow_name, step_names in expected_steps.items():
		text = (REPO_ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
		for step_name in step_names:
			step_start = text.index(f"- name: {step_name}")
			step_end = text.find("\n      - name:", step_start + 1)
			step_block = text[step_start : step_end if step_end >= 0 else None]
			assert "GIT_CONFIG_VALUE_0" in step_block, f"{workflow_name}: {step_name}"
		if workflow_name == "review_autofix.yml":
			assert 'echo "GIT_CONFIG_VALUE_0=' not in text, "Git auth must not persist into model steps"
		if workflow_name == "workflow-log-analysis.yml":
			sync_step_matches = list(re.finditer(r"(?m)^      - name: Sync latest report from target branch$", text))
			assert len(sync_step_matches) == 2
			for sync_step_match in sync_step_matches:
				sync_step_end = text.find("\n      - name:", sync_step_match.end())
				sync_step_block = text[sync_step_match.start() : sync_step_end if sync_step_end >= 0 else None]
				assert "GIT_CONFIG_VALUE_0" in sync_step_block

	memory_helpers = (REPO_ROOT / "scripts" / "memory_helpers.sh").read_text(encoding="utf-8")
	ensure_branch_start = memory_helpers.index("\nmemory_ensure_branch()\n{")
	ensure_branch_end = memory_helpers.index("\nmemory_record_run_event()\n{", ensure_branch_start)
	ensure_branch_block = memory_helpers[ensure_branch_start:ensure_branch_end]
	assert 'origin_url="${BASH_REMATCH[1]}${BASH_REMATCH[2]}"' in ensure_branch_block
	assert '_memory_git ls-remote --heads origin "${branch}"' in memory_helpers
	assert '_memory_git push origin "${branch}"' in memory_helpers
	assert '_memory_git push origin "${memory_branch}"' in memory_helpers

	review_judge = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	warning_start = review_judge.index("review_rb_send_warning()")
	warning_end = review_judge.index("# Shared PR check-runs merge gate", warning_start)
	warning_block = review_judge[warning_start:warning_end]
	assert warning_block.index('cd "${rb_support_root}"') < warning_block.index('source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"')


def _approval_status(
	comments: list[dict[str, object]],
	request: dict[str, object],
	*,
	role: str = "maintain",
	repository: str = "owner/repo",
) -> dict[str, object]:
	command = (
		"source scripts/gh_helpers.sh; "
		"gh_retry() { if [ \"$MOCK_ROLE\" = __error__ ]; then return 1; fi; printf '%s\\n' \"$MOCK_ROLE\"; }; "
		"review_blocked_approval_status \"$COMMENTS\" \"$REQUEST\" \"$REPOSITORY\""
	)
	result = subprocess.run(
		["bash", "-c", command],
		cwd=REPO_ROOT,
		env={
			"PATH": "/usr/bin:/bin",
			"COMMENTS": json.dumps(comments, separators=(",", ":")),
			"REQUEST": json.dumps(request, separators=(",", ":")),
			"MOCK_ROLE": role,
			"REPOSITORY": repository,
		},
		check=True,
		text=True,
		capture_output=True,
	)
	return json.loads(result.stdout)


def _pending_request(comments: list[dict[str, object]], head_sha: str, producer_id: int = 41898282) -> str:
	command = (
		"source scripts/gh_helpers.sh; "
		"review_blocked_find_pending_request \"$COMMENTS\" 12 \"$HEAD_SHA\" \"$PRODUCER_ID\""
	)
	result = subprocess.run(
		["bash", "-c", command],
		cwd=REPO_ROOT,
		env={
			"PATH": "/usr/bin:/bin",
			"COMMENTS": json.dumps(comments, separators=(",", ":")),
			"HEAD_SHA": head_sha,
			"PRODUCER_ID": str(producer_id),
		},
		check=True,
		text=True,
		capture_output=True,
	)
	return result.stdout.strip()


def test_review_blocked_approval_requires_exact_command_and_maintainer_role() -> None:
	request = {
		"request_id": "review_blocked_approval_20260907010101_0123456789",
		"decision_digest": "a" * 64,
		"created_at": "2026-09-07T01:00:00Z",
	}
	command = f"/review-blocked-approve {request['request_id']} {request['decision_digest']}"
	base = {"id": 42, "author": "maintainer", "author_type": "User", "author_association": "MEMBER", "created_at": "2026-09-07T01:01:00Z"}
	assert _approval_status([{**base, "body": command}], request, role="maintain")["status"] == "approved"
	assert _approval_status([{**base, "body": command}], request, role="admin")["status"] == "approved"
	for insufficient_role in ("read", "triage", "write", "custom-security-reviewer", "", "__error__"):
		assert _approval_status([{**base, "body": command}], request, role=insufficient_role)["status"] == "pending"
	assert _approval_status([{**base, "body": command + " please"}], request)["status"] == "pending"
	assert _approval_status([{**base, "body": command, "author_association": "NONE"}], request)["status"] == "pending"
	assert _approval_status([{**base, "body": command, "author_type": "Bot"}], request)["status"] == "pending"
	assert _approval_status([{**base, "body": command, "created_at": "2026-09-07T00:59:00Z"}], request)["status"] == "pending"
	assert _approval_status([{**base, "body": command, "created_at": request["created_at"]}], request)["status"] == "approved"
	assert _approval_status([{**base, "body": command}], request, repository="")["status"] == "pending"


def test_editor_model_step_has_no_repository_credentials_and_rechecks_state() -> None:
	workflow_text = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	editor_start = workflow_text.index("- name: Apply fixes with editor model")
	editor_end = workflow_text.index("\n      - name:", editor_start + 1)
	editor_block = workflow_text[editor_start:editor_end]
	assert "GH_TOKEN:" not in editor_block
	assert "GH_PAT:" not in editor_block
	assert "REPOSITORY:" not in editor_block

	recheck_start = workflow_text.index("- name: Re-check PR state after editor", editor_end)
	commit_start = workflow_text.index("- name: Commit changes", recheck_start)
	assert recheck_start < commit_start
	recheck_end = workflow_text.index("\n      - name:", recheck_start + 1)
	recheck_block = workflow_text[recheck_start:recheck_end]
	assert "GH_TOKEN:" in recheck_block
	assert "SUPPORT_SCRIPTS_DIR" not in recheck_block
	assert 'post_editor_pr_state="$(gh api ' in recheck_block
	assert 'echo "PR_CLOSED=true" >> "$GITHUB_ENV"' in recheck_block


def test_editor_script_scrubs_repository_credentials_from_environment() -> None:
	# Exercise only the credential-scrub prologue: everything after the first
	# assignment to SUPPORT_SCRIPTS_DIR launches the real editor loop.
	script_text = (REPO_ROOT / "scripts" / "review_apply_fixes.sh").read_text(encoding="utf-8")
	prologue = script_text[: script_text.index("\nSUPPORT_SCRIPTS_DIR=")]
	assert "Refusing to launch review editor" not in prologue
	probe = prologue + "\nprintf 'PROLOGUE_REACHED\\n'\nenv\n"
	result = subprocess.run(
		["bash", "-c", probe],
		cwd=REPO_ROOT,
		env={
			"PATH": "/usr/bin:/bin",
			"GH_TOKEN": "test-only-placeholder",
			"GH_PAT": "test-only-placeholder",
			"GITHUB_TOKEN": "test-only-placeholder",
			"ORCHESTRATOR_STATE_AUTH_KEYRING": "test-only-placeholder",
			"TG_BOT_SECRET": "test-only-placeholder",
			"OPENROUTER_API_KEY": "model-key-stays",
		},
		text=True,
		capture_output=True,
	)
	assert result.returncode == 0, result.stderr
	assert "PROLOGUE_REACHED" in result.stdout
	for credential_name in ("GH_TOKEN", "GH_PAT", "GITHUB_TOKEN", "ORCHESTRATOR_STATE_AUTH_KEYRING", "TG_BOT_SECRET"):
		assert f"{credential_name}=" not in result.stdout
		assert f"::notice::Scrubbed {credential_name} from the review editor environment" in result.stderr
	assert "OPENROUTER_API_KEY=model-key-stays" in result.stdout
	assert "test-only-placeholder" not in result.stdout


def test_review_support_uses_only_the_immutable_workflow_definition_sha() -> None:
	workflow_text = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	resolve_start = workflow_text.index("- name: Resolve workflow support ref")
	resolve_end = workflow_text.index("\n      - name:", resolve_start + 1)
	resolve_block = workflow_text[resolve_start:resolve_end]
	assert "${{ job.workflow_repository }}" in resolve_block
	assert "${{ job.workflow_sha }}" in resolve_block
	assert "^[0-9a-fA-F]{40}$" in resolve_block
	assert "github.sha" not in resolve_block
	assert 'SCRIPT_REF=stable' not in resolve_block
	assert ".codex-workflow-src-main" not in workflow_text
	assert "Install project dependencies (best-effort)" not in workflow_text

	stage_text = (REPO_ROOT / "scripts" / "stage_workflow_support.sh").read_text(encoding="utf-8")
	review_stage = stage_text[: stage_text.index("\nWORKFLOW_SUPPORT_SOURCE_REPO_DEFAULT=")]
	assert ".codex-workflow-src-main" not in review_stage


def test_editor_launch_uses_unprivileged_empty_environment_and_protected_sentinel() -> None:
	editor_text = (REPO_ROOT / "scripts" / "review_apply_fixes.sh").read_text(encoding="utf-8")
	launch_start = editor_text.index("editor_opencode_cmd=(")
	launch_end = editor_text.index("\n  )", launch_start)
	launch_block = editor_text[launch_start:launch_end]
	assert 'sudo -n -u "${EDITOR_ISOLATION_USER}" --' in launch_block
	assert "env -i" in launch_block
	assert '"OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"' in launch_block
	for forbidden_name in ("GITHUB_ENV", "GITHUB_OUTPUT", "BASH_ENV", "GIT_DIR", "GH_TOKEN", "GH_PAT", "TG_BOT_SECRET"):
		assert forbidden_name not in launch_block
	assert "setup_editor_isolation" in editor_text
	assert "cleanup_editor_isolation" in editor_text
	assert 'chmod 0700 "${resolved_source}/.git" "${EDITOR_ISOLATION_COMMAND_DIR}"' in editor_text
	assert 'PR_CLOSED_SENTINEL_FILE must be the protected runtime sentinel' in editor_text
	assert "/tmp/pr_closed_sentinel_" not in editor_text

	reviewer_text = (REPO_ROOT / "scripts" / "review_run_reviewers.sh").read_text(encoding="utf-8")
	assert "/tmp/pr_closed_sentinel_" not in reviewer_text
	assert '"${PR_CLOSED_SENTINEL_FILE:-/dev/null}"' in reviewer_text


def _resolve_successor(search_payload: dict[str, object], intent: dict[str, object], *, api_failure: bool = False) -> dict[str, object]:
	command = (
		"source scripts/gh_helpers.sh; "
		+ ("gh_retry() { return 1; }; " if api_failure else "gh_retry() { printf '%s' \"$SEARCH_JSON\"; }; ")
		+ "review_blocked_resolve_successor_issue owner/repo \"$INTENT_JSON\" 41898282"
	)
	result = subprocess.run(
		["bash", "-c", command],
		cwd=REPO_ROOT,
		env={
			"PATH": "/usr/bin:/bin",
			"SEARCH_JSON": json.dumps(search_payload, separators=(",", ":")),
			"INTENT_JSON": json.dumps(intent, separators=(",", ":")),
		},
		check=True,
		text=True,
		capture_output=True,
	)
	return json.loads(result.stdout)


def test_successor_resolver_requires_creator_labels_and_canonical_payload() -> None:
	request = {
		"schema_version": "review_blocked_approval.v1",
		"request_id": "review_blocked_approval_20260907010101_0123456789",
		"pr_number": 12,
		"linked_issue_number": 34,
		"action": "close_and_reissue",
		"head_sha": "d" * 40,
	}
	title = "Authenticated replacement"
	body = "Replacement body"
	build_command = (
		"source scripts/gh_helpers.sh; "
		"review_blocked_build_successor_intent \"$REQUEST\" reissue \"$TITLE\" \"$BODY\" '[\"ai:clarification\"]' 41898282"
	)
	built = subprocess.run(
		["bash", "-c", build_command],
		cwd=REPO_ROOT,
		env={"PATH": "/usr/bin:/bin", "REQUEST": json.dumps(request), "TITLE": title, "BODY": body},
		check=True,
		text=True,
		capture_output=True,
	)
	intent = json.loads(built.stdout)
	marker = "<!-- REVIEW_BLOCKED_SUCCESSOR_V1\n" + json.dumps(intent, sort_keys=True, separators=(",", ":")) + "\nREVIEW_BLOCKED_SUCCESSOR_V1 -->"
	valid_item = {
		"state": "open",
		"user": {"id": 41898282},
		"title": title,
		"body": body + "\n\n" + marker,
		"html_url": "https://github.com/owner/repo/issues/77",
		"labels": [{"name": "ai:clarification"}],
	}
	assert _resolve_successor({"items": [valid_item]}, intent)["status"] == "valid"
	assert _resolve_successor({"items": [{**valid_item, "html_url": "http://ghe.example.com/owner/repo/issues/77"}]}, intent)["status"] == "valid"
	for invalid_item in (
		{**valid_item, "user": {"id": 1001}},
		{**valid_item, "labels": []},
		{**valid_item, "title": "Edited title"},
		{**valid_item, "body": "Edited body\n\n" + marker},
	):
		assert _resolve_successor({"items": [invalid_item]}, intent)["status"] == "not_found"
	assert _resolve_successor({"items": []}, intent, api_failure=True)["status"] == "inconclusive"


def test_approval_request_ids_use_the_canonical_generator() -> None:
	text = (REPO_ROOT / "scripts" / "gh_helpers.sh").read_text(encoding="utf-8")
	assert 'make_record_id("review_blocked_approval")' in text
	assert "REVIEW_BLOCKED_APPROVAL_V1" in text
	assert "is:issue is:open in:body review-blocked-approval-request:" in text
	assert 'collaborators/${approver}/permission' in text
	assert "--jq '.role_name // empty'" in text


def test_approval_pending_is_handled_without_critical_alerts() -> None:
	judge_text = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	approval_start = judge_text.index('RB_APPROVAL_REQUEST_ID=""')
	approval_end = judge_text.index("# Execute judge action", approval_start)
	approval_block = judge_text[approval_start:approval_end]
	assert approval_block.count('echo "judge_handled=true" >> "$GITHUB_OUTPUT"') == 2
	assert 'echo "judge_skip_reason=approval_request_created"' in approval_block
	assert 'echo "judge_skip_reason=approval_pending"' in approval_block
	assert 'while [ "${RB_MERGE_POLL_INDEX}" -lt "${RB_MERGE_POLL_ATTEMPTS}" ]' in judge_text
	assert 'if .mergeable == true then "true" elif .mergeable == false then "false" else empty end' in judge_text
	assert 'echo "judge_skip_reason=mergeability_pending"' in judge_text
	merge_head_refusal = judge_text[judge_text.index('if [ "${RB_MERGE_HEAD_SHA}" != "${POST_REVIEW_HEAD_SHA}" ]'):]
	merge_head_refusal = merge_head_refusal[:merge_head_refusal.index("elif [")]
	assert 'echo "judge_handled=true" >> "$GITHUB_OUTPUT"' in merge_head_refusal
	assert 'echo "judge_action=skip" >> "$GITHUB_OUTPUT"' in merge_head_refusal
	assert judge_text.count('echo "judge_handled=true" >> "$GITHUB_OUTPUT"') >= 7

	workflow_text = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	assert "approval_pending)" in workflow_text
	assert '[ "${JUDGE_SKIP_REASON}" = "approval_request_created" ]' in workflow_text
	assert "terminal recommendation needs trusted human approval" in workflow_text
	assert "suppressing duplicate alert" in workflow_text
	assert "Review-blocked refusal was already recorded" in workflow_text

	poller_text = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
	assert 'requires trusted human approval."$\'\\n\'' in poller_text
	assert 'review_blocked_approval_status "${PR_COMMENTS}" "${RB_APPROVAL_REQUEST}" "${GITHUB_REPOSITORY}"' in poller_text
	assert 'review_blocked_approval_status "${PR_COMMENTS}" "${RB_APPROVAL_REQUEST}" "${REPOSITORY}"' in judge_text


def test_outsider_cannot_forge_request_or_consumed_markers() -> None:
	head_sha = "b" * 40
	request = {
		"schema_version": "review_blocked_approval.v1",
		"request_id": "review_blocked_approval_20260907010101_0123456789",
		"pr_number": 12,
		"linked_issue_number": 34,
		"action": "merge",
		"head_sha": head_sha,
		"decision_digest": "c" * 64,
		"created_at": "2026-09-07T01:00:00Z",
		"decision": {"action": "merge"},
	}
	body = "<!-- REVIEW_BLOCKED_APPROVAL_V1\n" + json.dumps(request) + "\nREVIEW_BLOCKED_APPROVAL_V1 -->"
	outsider = {"body": body, "author": "outsider", "author_id": 1001, "author_type": "User", "author_association": "NONE"}
	assert _pending_request([outsider], head_sha) == ""
	trusted_human = {"body": body, "author": "maintainer", "author_id": 1002, "author_type": "User", "author_association": "OWNER"}
	assert _pending_request([trusted_human], head_sha) == ""
	producer = {"body": body, "author": "workflow-pat-owner", "author_id": 41898282, "author_type": "User", "author_association": "OWNER"}
	forged_consumed = {
		"body": "<!-- REVIEW_BLOCKED_APPROVAL_CONSUMED_V1\n"
		+ json.dumps({"request_id": request["request_id"]})
		+ "\nREVIEW_BLOCKED_APPROVAL_CONSUMED_V1 -->",
		"author": "outsider",
		"author_id": 1001,
		"author_type": "User",
		"author_association": "NONE",
	}
	assert json.loads(_pending_request([producer, forged_consumed], head_sha))["request_id"] == request["request_id"]
	trusted_human_consumed = {**forged_consumed, "author": "maintainer", "author_id": 1002, "author_association": "MEMBER"}
	assert json.loads(_pending_request([producer, trusted_human_consumed], head_sha))["request_id"] == request["request_id"]


def main() -> int:
	for name, value in sorted(globals().items()):
		if name.startswith("test_") and callable(value):
			value()
	print("OK: git authentication and review-blocked approval security contracts hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
