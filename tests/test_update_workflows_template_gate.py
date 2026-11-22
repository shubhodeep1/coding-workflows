#!/usr/bin/env python3
"""Template gate for the consumer updater (issue #7004, orchestrator project #6902).

`workflow-templates/ai-update-workflows.yml` is the caller every consumer
installs; `.github/workflows/update_workflows.yml` is the reusable workflow it
calls. The two ship together, so this gate pins the contract between them:

- the template grants every permission the reusable updater's jobs need;
- every input and secret the template passes exists in `workflow_call` (a
  missing input fails every run at startup with zero jobs) and the template
  passes no input the release that is currently `@stable` lacks;
- the `verify` job runs before auto-merge and is the only place that enables
  it;
- the `verify` job never runs code from the pull-request head.

Assumption: the plan (docs/plans/safeguarded-consumer-updater-plan.md) could
not be read, so the job id `verify`, the status context
`ai-update-workflows/verify` and the log prefix UPDATER_PR_VERIFY are the
provisional names from the issue.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
TEMPLATE = REPO_ROOT / "workflow-templates" / "ai-update-workflows.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
STATUS_CONTEXT = "ai-update-workflows/verify"
PUSHED_SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = "octo/consumer"

# What the updater's jobs need from the caller's GITHUB_TOKEN. `verify` has no
# job-level permissions block (a reusable job asking for more than the caller
# grants fails the whole run at startup), so its needs are declared here:
# contents (checkout, compare), statuses (the verify commit status). The
# pull-request path itself uses GH_PAT; pull-requests: write is granted for it
# as the issue requires.
REQUIRED_CALLER_PERMISSIONS = {"contents": "write", "pull-requests": "write", "statuses": "write"}
LEVELS = {"none": 0, "read": 1, "write": 2}
# Python modules the verify job runs from the attested release checkout.
VERIFY_EXECUTED_SCRIPTS = {
	"verify_release_manifest.py",
	"apply_audit_gate_assets.py",
	"assemble_changelog.py",
	"assemble_agents.py",
}
BOOTSTRAP_SCRIPTS = {
	"scripts/verify_release_manifest.py",
	"scripts/release_manifest.py",
	"scripts/workflow_wrapper_refs.py",
	"scripts/apply_audit_gate_assets.py",
	"scripts/assemble_changelog.py",
	"scripts/assemble_agents.py",
}


def _load(path: Path) -> dict:
	return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
	# PyYAML reads the bare `on` key as boolean True.
	if "on" in doc:
		return doc["on"]
	return doc[True]


def _workflow() -> dict:
	return _load(WORKFLOW)


def _template() -> dict:
	return _load(TEMPLATE)


def _verify_job() -> dict:
	return _workflow()["jobs"]["verify"]


def _verify_step(step_id: str) -> dict:
	matches = [step for step in _verify_job()["steps"] if step.get("id") == step_id]
	assert len(matches) == 1, step_id
	return matches[0]


def _caller_job() -> dict:
	jobs = _template()["jobs"]
	assert list(jobs) == ["update"]
	return jobs["update"]


# ── Permissions ──────────────────────────────────────────────────────────


def test_template_grants_every_permission_the_updater_needs() -> None:
	granted = _template()["permissions"]
	needed = dict(REQUIRED_CALLER_PERMISSIONS)
	for job_name, job in _workflow()["jobs"].items():
		job_permissions = job.get("permissions") or {}
		assert isinstance(job_permissions, dict), job_name
		for scope, level in job_permissions.items():
			if LEVELS.get(level, 0) > LEVELS.get(needed.get(scope, "none"), 0):
				needed[scope] = level
	for scope, level in needed.items():
		assert LEVELS[granted.get(scope, "none")] >= LEVELS[level], scope


def test_verify_job_asks_for_no_job_level_permissions() -> None:
	# Inheriting the caller's grant keeps an older caller wrapper (contents:
	# write only) from failing the whole updater at startup; without
	# statuses: write each status post retries once with GH_PAT, and the job
	# fails closed only when both tokens are refused.
	assert "permissions" not in _verify_job()
	assert "permissions" not in _workflow()


# ── Inputs and secrets ───────────────────────────────────────────────────


def test_template_inputs_and_secrets_exist_in_workflow_call() -> None:
	call = _on(_workflow())["workflow_call"]
	inputs = call.get("inputs") or {}
	secrets = call.get("secrets") or {}
	caller = _caller_job()
	passed = caller.get("with") or {}
	for name in passed:
		assert name in inputs, name
	for name, spec in inputs.items():
		if spec.get("required"):
			assert name in passed, name
	# The currently released @stable updater only knows this input.
	assert set(passed) <= {"allow_workflow_edits"}
	caller_secrets = caller.get("secrets")
	if caller_secrets != "inherit":
		assert isinstance(caller_secrets, dict)
		for name in caller_secrets:
			assert name in secrets, name


def test_template_stays_backward_compatible() -> None:
	doc = _template()
	assert doc["name"] == "AI Update Workflows"
	on = _on(doc)
	assert set(on) == {"schedule", "repository_dispatch", "workflow_dispatch"}
	assert on["schedule"] == [{"cron": "0 4 * * *"}]
	assert on["repository_dispatch"] == {"types": ["coding-workflows-stable-released"]}
	caller = _caller_job()
	assert caller["uses"] == "shubhodeep1/coding-workflows/.github/workflows/update_workflows.yml@stable"
	assert caller["with"] == {"allow_workflow_edits": "${{ vars.ALLOW_WORKFLOW_EDITS != 'false' }}"}
	# Least privilege: only the secrets the updater declares. The released
	# v1.38.3 updater declares both (GH_PAT required, TG_BOT_SECRET optional),
	# so a template synced ahead of the next release still starts.
	assert caller["secrets"] == {"GH_PAT": "${{ secrets.GH_PAT }}", "TG_BOT_SECRET": "${{ secrets.TG_BOT_SECRET }}"}
	# Consumer kill switch (plan Q49): unset means on, 'false' stops the job.
	assert caller["if"] == "${{ vars.AI_WORKFLOW_AUTO_UPDATE_ENABLED != 'false' }}"
	text = TEMPLATE.read_text(encoding="utf-8")
	assert "commits directly to the default branch" not in text
	assert "UPDATER_PR_DELIVERY_ENABLED" in text
	assert STATUS_CONTEXT in text


def test_template_header_documents_the_update_hold() -> None:
	# Issue #8117: a consumer audit read the default-on gate as a reverted
	# security hold. The gate is deliberate (unset means on); the header must
	# say how to hold updates, and the gate it documents must stay in place.
	header = TEMPLATE.read_text(encoding="utf-8").split("name: AI Update Workflows", 1)[0]
	lines = [line.lstrip("#").strip() for line in header.splitlines()]
	assert "AI_WORKFLOW_AUTO_UPDATE_ENABLED" in " ".join(lines)
	start = next(i for i, line in enumerate(lines) if "AI_WORKFLOW_AUTO_UPDATE_ENABLED" in line)
	while start > 0 and lines[start - 1]:
		start -= 1
	end = start
	while end < len(lines) and lines[end]:
		end += 1
	paragraph = " ".join(lines[start:end])
	assert "'false'" in paragraph
	assert "runs by default" in paragraph
	assert "cannot erase the hold" in paragraph
	assert _caller_job()["if"] == "${{ vars.AI_WORKFLOW_AUTO_UPDATE_ENABLED != 'false' }}"


def test_template_renders_to_an_immutable_ref() -> None:
	sys.path.insert(0, str(REPO_ROOT / "scripts"))
	try:
		import workflow_wrapper_refs  # noqa: PLC0415
	finally:
		sys.path.pop(0)
	rendered = workflow_wrapper_refs.pin_reusable_workflow_refs(TEMPLATE.read_text(encoding="utf-8"), PUSHED_SHA)
	assert f"update_workflows.yml@{PUSHED_SHA} # stable" in rendered
	assert "@stable\n" not in rendered


# ── Merge gate ───────────────────────────────────────────────────────────


def test_verify_job_runs_after_the_update_job_for_opened_prs_only() -> None:
	job = _verify_job()
	needs = job["needs"]
	assert needs == "update-wrappers" or "update-wrappers" in needs
	condition = job["if"]
	assert "!cancelled()" in condition
	assert "needs.update-wrappers.outputs.delivery == 'pr'" in condition
	assert "needs.update-wrappers.outputs.pr_action == 'created'" in condition
	assert "needs.update-wrappers.outputs.pr_action == 'refreshed'" in condition
	outputs = _workflow()["jobs"]["update-wrappers"]["outputs"]
	for name in ("delivery", "pr_action", "pr_number", "pushed_sha", "base", "upstream_sha", "verified"):
		assert name in outputs, name


def test_auto_merge_is_enabled_only_by_the_verify_job() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	commit_push = [step for step in _workflow()["jobs"]["update-wrappers"]["steps"] if step.get("id") == "commit_push"][0]
	# The ai-sync/stable delivery (#7128) merges its own verified PR, bound to
	# the head it pushed; the auto/update-workflows-* flow below the
	# ai-sync section stays verify-job-only.
	commit_run = commit_push["run"]
	sync_start = commit_run.index('SYNC_BRANCH="ai-sync/stable"')
	sync_end = commit_run.index("# ── Pull-request delivery (UPDATER_PR_DELIVERY_ENABLED=true)")
	sync_section = commit_run[sync_start:sync_end]
	assert sync_section.count("gh pr merge") == 2
	assert sync_section.count('--match-head-commit "${MERGE_HEAD_SHA}"') == 2
	assert text.count("gh pr merge") - sync_section.count("gh pr merge") == 1
	run = _verify_step("verify_publish")["run"]
	success = run.index("-f state=success")
	merge = run.index('gh pr merge "${PR_NUMBER}"')
	assert success < merge
	assert run.index('if [ "${VERIFIED:-}" = "true" ]; then') < merge
	assert '--auto --squash --match-head-commit "${PUSHED_SHA}"' in run
	assert "gh pr merge" not in commit_run[:sync_start] + commit_run[sync_end:]
	assert 'AUTO_MERGE="pending_verify"' in commit_push["run"]
	steps = [step.get("id") for step in _verify_job()["steps"]]
	for earlier in ("verify_inputs", "verify_release", "verify_topology", "verify_reproduce", "verify_tree"):
		assert steps.index(earlier) < steps.index("verify_publish"), earlier


def test_verify_status_context_is_fixed() -> None:
	assert _verify_job()["env"]["VERIFY_STATUS_CONTEXT"] == STATUS_CONTEXT
	failure = [step for step in _verify_job()["steps"] if step.get("name") == "Mark verify failure"]
	assert len(failure) == 1
	assert "always()" in failure[0]["if"]
	assert "steps.verify_inputs.outputs.sha_ok == 'true'" in failure[0]["if"]
	assert "steps.verify_publish.outcome != 'success'" in failure[0]["if"]


# ── No pull-request-head code ────────────────────────────────────────────


PR_TREE_EXEC_RE = re.compile(r"(?:\b(?:bash|sh|python3?|source|npm|make|node)\b|(?<![\w/])\.)\s+[\"']?\$\{GITHUB_WORKSPACE\}/\.updater-pr-tree|cd\s+[\"']?\$\{?(?:PR_TREE|GITHUB_WORKSPACE)")


def test_pr_head_is_checked_out_as_data_only() -> None:
	steps = _verify_job()["steps"]
	checkouts = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")]
	assert len(checkouts) == 1
	with_ = checkouts[0]["with"]
	assert with_["persist-credentials"] is False
	assert with_["ref"] == "${{ needs.update-wrappers.outputs.pushed_sha }}"
	assert with_["path"] == ".updater-pr-tree"
	assert with_["lfs"] is False
	for step in steps:
		assert "working-directory" not in step, step.get("name")
		uses = str(step.get("uses", ""))
		assert not uses.startswith("."), step.get("name")
		run = step.get("run", "")
		assert not PR_TREE_EXEC_RE.search(run), step.get("name")


def test_verify_job_runs_only_attested_release_python() -> None:
	invocations = []
	for step in _verify_job()["steps"]:
		run = step.get("run", "")
		for line in run.splitlines():
			if re.search(r"\bpython3?\s+[-\"$]", line) and not line.strip().startswith("#"):
				invocations.append(line)
	assert invocations
	for line in invocations:
		assert "python3 -I -B " in line, line
		match = re.search(r'python3 -I -B "\$\{RELEASE_ROOT\}/scripts/([A-Za-z0-9_]+\.py)"', line)
		assert match, line
		assert match.group(1) in VERIFY_EXECUTED_SCRIPTS, line
	release = _verify_step("verify_release")["run"]
	loop = re.search(r"for bootstrap_rel in ([^;]+); do", release)
	assert loop
	assert set(loop.group(1).split()) == BOOTSTRAP_SCRIPTS
	for name in VERIFY_EXECUTED_SCRIPTS:
		assert f"scripts/{name}" in BOOTSTRAP_SCRIPTS
	assert release.index("gh attestation verify") < release.index("python3 -I -B")


def test_gh_pat_never_reaches_a_remote_url() -> None:
	release = _verify_step("verify_release")["run"]
	assert "x-access-token:${GH_TOKEN}" not in release
	assert "credential.helper=${CRED_HELPER}" in release
	text = WORKFLOW.read_text(encoding="utf-8")
	verify_text = text[text.index("\n  verify:\n"):]
	assert "@github.com" not in verify_text


def _attestation_flags(run: str) -> list[str]:
	start = run.index("gh attestation verify")
	block = run[start:run.index("; then", start)]
	return [line.strip().rstrip("\\").strip() for line in block.splitlines()[1:]]


def test_attestation_policy_matches_the_update_job() -> None:
	update_steps = _workflow()["jobs"]["update-wrappers"]["steps"]
	original = [step for step in update_steps if step.get("id") == "verify_release_manifest"][0]["run"]
	again = _verify_step("verify_release")["run"]
	flags = _attestation_flags(again)
	assert flags == _attestation_flags(original)
	assert "--deny-self-hosted-runners" in flags
	assert any(flag.startswith("--cert-identity-regex") for flag in flags)
	assert any(flag.startswith("--predicate-type https://slsa.dev/provenance/v1") for flag in flags)


def test_review_auto_merge_skips_updater_prs() -> None:
	# Ordinary review must never enable auto-merge on an updater PR; only the
	# verify job does, after its PR-tree check.
	script = (REPO_ROOT / "scripts" / "review_enable_auto_merge.sh").read_text(encoding="utf-8")
	guard = script.index("^auto/update-workflows-[0-9a-f]{12}(-r[2-5])?$")
	assert "reason=updater_pr_verify_job_owns_merge" in script[guard:guard + 400]
	assert guard < script.index("gh_retry gh pr merge")


def test_deterministic_skip_merge_skips_updater_prs() -> None:
	text = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	guard = text.index('elif [[ "${PR_HEAD_REF}" =~ ^auto/update-workflows-[0-9a-f]{12}(-r[2-5])?$ ]]; then')
	assert "reason=updater_pr_verify_job_owns_merge" in text[guard:guard + 600]
	assert guard < text.index("gh_retry gh pr merge", guard)


def test_review_blocked_judge_never_merges_updater_prs() -> None:
	script = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	guard = script.index('=~ ^auto/update-workflows-[0-9a-f]{12}(-r[2-5])?$ ]]')
	assert "judge_skip_reason=updater_pr_verify_job_owns_merge" in script[guard:guard + 600]
	assert script.index('RB_MERGE_ACTION="false"') < guard < script.index("rb_security_merge_gate; then")
	assert guard < script.index('if gh pr merge "${PR_NUMBER}"')
	# Every action is held (fix pushes, close_and_reissue), not only merges.
	condition = script[script.rindex("if ", 0, guard):guard]
	assert "RB_MERGE_ACTION" not in condition
	assert guard < script.index('git push origin "HEAD:${TARGET_BRANCH}"')


def test_review_editor_never_commits_to_updater_branches() -> None:
	script = (REPO_ROOT / "scripts" / "review_commit_changes.sh").read_text(encoding="utf-8")
	guard = script.index('[[ "${TARGET_BRANCH:-}" =~ ^auto/update-workflows-[0-9a-f]{12}(-r[2-5])?$ ]]')
	assert "reason=updater_pr_verify_job_owns_branch" in script[guard:guard + 400]
	assert "AUTOFIX_UPDATER_COMMIT_WITHHELD=true" in script[guard:guard + 1200]
	assert guard < script.index("git commit")
	workflow = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	assert "env.AUTOFIX_UPDATER_COMMIT_WITHHELD != 'true'" in workflow


def test_review_never_pushes_or_resolves_on_updater_branches() -> None:
	text = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	push = text.index("- name: Push all pending commits")
	guard = text.index('[[ "${TARGET_BRANCH:-}" =~ ^auto/update-workflows-[0-9a-f]{12}(-r[2-5])?$ ]]', push)
	assert "AUTOFIX_PUSH_SKIPPED" in text[guard:guard + 300]
	assert guard < text.index("git push", push)
	detect = (REPO_ROOT / "scripts" / "review_autofix_step_detect_merge_conflicts.sh").read_text(encoding="utf-8")
	assert "AUTOFIX_MERGE_CONFLICT_SKIPPED" in detect


def test_telegram_helpers_come_from_the_attested_checkout() -> None:
	steps = _workflow()["jobs"]["update-wrappers"]["steps"]
	notify = [step for step in steps if step.get("name") == "Send Telegram notification"][0]
	assert notify["env"]["UPSTREAM_DIR"] == "${{ steps.fetch.outputs.upstream_dir }}"
	assert "contents/scripts/tg_helpers.sh" not in notify["run"]
	assert '"${UPSTREAM_DIR:-/nonexistent}/../scripts/tg_helpers.sh"' in notify["run"]


def test_verify_job_reproduces_agents_fragment_assembly() -> None:
	run = _verify_step("verify_reproduce")["run"]
	assert '"${RELEASE_ROOT}/scripts/assemble_agents.py" assemble' in run
	assert '--repo-root "${BASE_TREE}"' in run


def test_template_gate_is_registered_in_ci() -> None:
	assert "tests/test_update_workflows_template_gate.py" in CI.read_text(encoding="utf-8")


# ── Behavioural: publish step against a fake gh ──────────────────────────


FAKE_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
printf 'token=%s\n' "${GH_TOKEN:-}" >> "${FAKE_GH_LOG}"
if [ "$1" = "api" ] && [ "${GH_TOKEN:-}" = "${FAKE_STATUS_REFUSED_TOKEN:-none}" ]; then exit 1; fi
if [ "$1" = "api" ]; then exit "${FAKE_STATUS_RC:-0}"; fi
if [ "$1 $2" = "pr merge" ]; then exit "${FAKE_MERGE_RC:-0}"; fi
exit 1
"""


def _run_publish(tmp_path: Path, verified: str, status_rc: int = 0, merge_rc: int = 0,
	refused_token: str = "none") -> tuple[subprocess.CompletedProcess, list[str], dict]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
	log = tmp_path / "gh.log"
	output = tmp_path / "out"
	output.write_text("", encoding="utf-8")
	script = _verify_step("verify_publish")["run"]
	assert "${{" not in script
	env = {
		**{k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "FAKE_"))},
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"GITHUB_OUTPUT": str(output),
		"GITHUB_REPOSITORY": REPO,
		"GITHUB_RUN_ID": "1",
		"PR_NUMBER": "77",
		"PUSHED_SHA": PUSHED_SHA,
		"VERIFIED": verified,
		"VERIFY_STATUS_CONTEXT": STATUS_CONTEXT,
		"STATUS_TOKEN": "job-token",
		"MERGE_TOKEN": "pat-token",
		"FAKE_GH_LOG": str(log),
		"FAKE_STATUS_RC": str(status_rc),
		"FAKE_MERGE_RC": str(merge_rc),
		"FAKE_STATUS_REFUSED_TOKEN": refused_token,
	}
	result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
	calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
	outputs = dict(line.partition("=")[::2] for line in output.read_text(encoding="utf-8").splitlines() if "=" in line)
	return result, calls, outputs


def test_verified_release_gets_one_bound_merge(tmp_path: Path) -> None:
	result, calls, outputs = _run_publish(tmp_path, "true")
	assert result.returncode == 0, result.stderr
	status_index = next(i for i, call in enumerate(calls) if call.startswith("api -X POST"))
	assert f"repos/{REPO}/statuses/{PUSHED_SHA}" in calls[status_index]
	assert "state=success" in calls[status_index]
	assert f"context={STATUS_CONTEXT}" in calls[status_index]
	assert calls[status_index + 1] == "token=job-token"
	merges = [i for i, call in enumerate(calls) if call.startswith("pr merge")]
	assert len(merges) == 1
	assert calls[merges[0]] == f"pr merge 77 --repo {REPO} --auto --squash --match-head-commit {PUSHED_SHA}"
	assert calls[merges[0] + 1] == "token=pat-token"
	assert status_index < merges[0]
	assert outputs["auto_merge"] == "enabled"
	assert "UPDATER_PR_VERIFY outcome=passed reason=ok" in result.stdout


@pytest.mark.parametrize("verified", ["false", "skipped", ""])
def test_unverified_release_is_not_merged(tmp_path: Path, verified: str) -> None:
	result, calls, outputs = _run_publish(tmp_path, verified)
	assert result.returncode == 0, result.stderr
	assert not any(call.startswith("pr merge") for call in calls)
	assert outputs["auto_merge"] == "skipped_unverified"


def test_status_post_failure_blocks_the_merge(tmp_path: Path) -> None:
	result, calls, outputs = _run_publish(tmp_path, "true", status_rc=1)
	assert result.returncode != 0
	assert not any(call.startswith("pr merge") for call in calls)
	assert "auto_merge" not in outputs
	assert "reason=status_post_failed" in result.stdout
	assert sum(call.startswith("api -X POST") for call in calls) == 2


def test_status_post_retries_with_pat_when_job_token_is_refused(tmp_path: Path) -> None:
	# A caller wrapper rendered before it granted `statuses: write`: the PR
	# that fixes the wrapper must not wait for a human.
	result, calls, outputs = _run_publish(tmp_path, "true", refused_token="job-token")
	assert result.returncode == 0, result.stderr
	posts = [i for i, call in enumerate(calls) if call.startswith("api -X POST")]
	assert [calls[i + 1] for i in posts] == ["token=job-token", "token=pat-token"]
	assert "::warning::UPDATER_PR_VERIFY posted the status with GH_PAT" in result.stdout
	assert outputs["auto_merge"] == "enabled"


def _run_pending(tmp_path: Path, status_rc: int = 0, refused_token: str = "none") -> tuple[subprocess.CompletedProcess, list[str]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
	log = tmp_path / "gh.log"
	output = tmp_path / "out"
	output.write_text("", encoding="utf-8")
	step = _verify_step("verify_inputs")
	assert step["env"]["MERGE_TOKEN"] == "${{ secrets.GH_PAT }}"
	script = step["run"]
	assert "${{" not in script
	env = {
		**{k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "FAKE_"))},
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"GITHUB_OUTPUT": str(output),
		"GITHUB_REPOSITORY": REPO,
		"GITHUB_RUN_ID": "1",
		"PR_NUMBER": "77",
		"PUSHED_SHA": PUSHED_SHA,
		"UPSTREAM_SHA": PUSHED_SHA,
		"BASE": "main",
		"VERIFIED": "true",
		"VERIFY_STATUS_CONTEXT": STATUS_CONTEXT,
		"GH_TOKEN": "job-token",
		"MERGE_TOKEN": "pat-token",
		"FAKE_GH_LOG": str(log),
		"FAKE_STATUS_RC": str(status_rc),
		"FAKE_STATUS_REFUSED_TOKEN": refused_token,
	}
	result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
	calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
	return result, calls


def test_pending_status_retries_with_pat_when_job_token_is_refused(tmp_path: Path) -> None:
	# The pending post runs first in the job: without the same fallback an
	# older wrapper would fail here and never reach the publish step.
	result, calls = _run_pending(tmp_path, refused_token="job-token")
	assert result.returncode == 0, result.stdout + result.stderr
	posts = [i for i, call in enumerate(calls) if call.startswith("api -X POST")]
	assert [calls[i + 1] for i in posts] == ["token=job-token", "token=pat-token"]
	assert "state=pending" in calls[posts[0]]
	assert "::warning::UPDATER_PR_VERIFY posted the status with GH_PAT" in result.stdout


def test_pending_status_fails_closed_when_both_tokens_are_refused(tmp_path: Path) -> None:
	result, calls = _run_pending(tmp_path, status_rc=1)
	assert result.returncode != 0
	assert sum(call.startswith("api -X POST") for call in calls) == 2
	assert "reason=status_post_failed stage=status" in result.stdout


def test_merge_failure_is_reported_not_retried(tmp_path: Path) -> None:
	result, calls, outputs = _run_publish(tmp_path, "true", merge_rc=1)
	assert result.returncode == 0, result.stderr
	assert sum(call.startswith("pr merge") for call in calls) == 1
	assert outputs["auto_merge"] == "failed"


# ── Merge-blocked alarm (#9483) ──────────────────────────────────────────


MERGE_BLOCKED_IF = "${{ !cancelled() && steps.verify_publish.outcome == 'success' && steps.verify_publish.outputs.auto_merge == 'failed' }}"


def test_merge_blocked_step_fails_the_job_after_mark_verify_failure() -> None:
	steps = _verify_job()["steps"]
	names = [step.get("name") for step in steps]
	step = _verify_step("verify_merge_blocked")
	assert step["name"] == "Fail when auto-merge could not be enabled"
	assert step["if"] == MERGE_BLOCKED_IF
	assert names.index("Mark verify failure") < names.index(step["name"]) < names.index("Verify summary")
	assert "continue-on-error" not in step
	run = step["run"]
	assert "${{" not in run
	assert "::error::UPDATER_PR_VERIFY outcome=merge_blocked reason=auto_merge_failed pr=${PR_NUMBER} head=${PUSHED_SHA}" in run
	# #9483 fix 1 removed the read:org dependency; do not send operators there.
	assert "read:org" not in run
	assert "curl -sS --fail -X POST" in run
	# A hung Telegram endpoint must not hold the failure until the job timeout.
	assert "--max-time 30" in run
	assert step["env"]["ALERT_MSG_LEVEL"].endswith("|| vars.ALERT_MSG_LEVEL || 'WARNING' }}")
	assert run.rstrip().endswith("exit 1")
	# The verify status is never touched: a human can still merge the PR.
	assert "statuses" not in run
	assert "gh " not in run
	assert "Telegram not configured — skipping merge-blocked alert." in run
	assert step["env"]["TG_BOT_SECRET"] == "${{ secrets.TG_BOT_SECRET }}"
	# "Mark verify failure" keeps its condition, so it never runs when the
	# publish step (and its success status) succeeded.
	failure = [s for s in steps if s.get("name") == "Mark verify failure"][0]
	assert failure["if"] == "${{ always() && steps.verify_inputs.outputs.sha_ok == 'true' && steps.verify_publish.outcome != 'success' }}"


FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_CURL_LOG}"
exit "${FAKE_CURL_RC:-0}"
"""


def _run_merge_blocked(tmp_path: Path, level: str, secret: str, curl_rc: int = 0) -> tuple[subprocess.CompletedProcess, list[str]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	curl = bin_dir / "curl"
	curl.write_text(FAKE_CURL, encoding="utf-8")
	curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
	log = tmp_path / "curl.log"
	env = {
		**{k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "FAKE_", "TG_"))},
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"PR_NUMBER": "77",
		"PUSHED_SHA": PUSHED_SHA,
		"ALERT_MSG_LEVEL": level,
		"TG_BOT_SECRET": secret,
		"TG_ADMIN_CHAT_ID": "123" if secret else "",
		"SYNC_REPO": REPO,
		"SYNC_RUN_URL": "https://github.com/octo/consumer/actions/runs/1",
		"FAKE_CURL_LOG": str(log),
		"FAKE_CURL_RC": str(curl_rc),
	}
	result = subprocess.run(["bash", "-c", _verify_step("verify_merge_blocked")["run"]], env=env, capture_output=True, text=True)
	calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
	return result, calls


def test_merge_blocked_step_alerts_and_exits_nonzero(tmp_path: Path) -> None:
	result, calls = _run_merge_blocked(tmp_path, "WARNING", "bot-secret")
	assert result.returncode == 1
	assert f"::error::UPDATER_PR_VERIFY outcome=merge_blocked reason=auto_merge_failed pr=77 head={PUSHED_SHA}" in result.stdout
	sent = "\n".join(calls)
	assert sent.count("api.telegram.org/bot") == 1
	assert "chat_id=123" in sent
	assert "updater PR #77 in octo/consumer" in sent


def test_merge_blocked_step_alerts_when_no_level_is_set(tmp_path: Path) -> None:
	# An unset or empty level means WARNING for this ERROR-class alert.
	result, calls = _run_merge_blocked(tmp_path, "", "bot-secret")
	assert result.returncode == 1
	assert "\n".join(calls).count("api.telegram.org/bot") == 1


def test_merge_blocked_step_still_fails_without_telegram(tmp_path: Path) -> None:
	result, calls = _run_merge_blocked(tmp_path, "WARNING", "")
	assert result.returncode == 1
	assert "Telegram not configured" in result.stdout
	assert calls == []


def test_merge_blocked_step_honours_silent_alert_level(tmp_path: Path) -> None:
	result, calls = _run_merge_blocked(tmp_path, "SILENT", "bot-secret")
	assert result.returncode == 1
	assert "suppresses the merge-blocked alert" in result.stdout
	assert calls == []


def test_merge_blocked_step_warns_when_telegram_rejects_the_alert(tmp_path: Path) -> None:
	# curl --fail exits 22 on an HTTP 4xx/5xx reply; the warning must show
	# and the step must still fail the job.
	result, calls = _run_merge_blocked(tmp_path, "WARNING", "bot-secret", curl_rc=22)
	assert result.returncode == 1
	assert "::warning::Telegram send failed" in result.stdout
	assert "\n".join(calls).count("api.telegram.org/bot") == 1


# ── Head gate re-check after verify (#9483 fix 3) ────────────────────────


UPDATER_BRANCH = "auto/update-workflows-0123456789ab"
HEAD_GATE_IF = "${{ !cancelled() && steps.verify_publish.outcome == 'success' }}"
FAKE_GATE_GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
token = os.environ.get("GH_TOKEN", "")
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps({"args": args, "token": token}) + "\\n")
if args[:3] == ["api", "-X", "POST"]:
    sys.exit(1 if token in os.environ.get("FAKE_REFUSED_TOKENS", "none").split(",") else 0)
if len(args) >= 2 and args[0] == "api" and "/commits/" in args[1] and "/status" in args[1]:
    seq = json.loads(os.environ["FAKE_STATUS_SEQ"])
    counter = os.environ["FAKE_GH_LOG"] + ".n"
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    item = seq[min(n, len(seq) - 1)]
    if item is None:
        sys.exit(1)
    print(json.dumps(item))
    sys.exit(0)
if len(args) >= 2 and args[0] == "api" and args[1] == "repos/octo/consumer/pulls/77":
    print(os.environ["FAKE_PR_JSON"])
    sys.exit(0)
sys.exit(1)
'''


def _gate_statuses(verify: str | None = "success", integrity: str | None = "success", total: int | None = None) -> dict:
	statuses = []
	if verify is not None:
		statuses.append({"context": STATUS_CONTEXT, "state": verify})
	if integrity is not None:
		statuses.append({"context": "coding-workflows/workflow-integrity", "state": integrity})
	return {"state": "pending", "total_count": len(statuses) if total is None else total, "statuses": statuses}


def _gate_pr(**head: object) -> dict:
	return {
		"state": "open",
		"head": {"ref": UPDATER_BRANCH, "sha": PUSHED_SHA, "repo": {"full_name": REPO}, **head},
		"base": {"ref": "main", "repo": {"full_name": REPO}},
	}


def _run_head_gate(tmp_path: Path, seq: list | None = None, pr: dict | None = None,
	**env_overrides: str) -> tuple[subprocess.CompletedProcess, list[dict]]:
	if not shutil.which("jq"):
		pytest.skip("jq is required by the head-gate step")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(parents=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GATE_GH, encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
	log = tmp_path / "gh.log"
	env = {
		**{k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "FAKE_", "REVIEW_HEAD_GATE_", "UPDATER_HEAD_GATE_"))},
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"GITHUB_REPOSITORY": REPO,
		"GITHUB_RUN_ID": "1",
		"PR_NUMBER": "77",
		"PUSHED_SHA": PUSHED_SHA,
		"VERIFY_STATUS_CONTEXT": STATUS_CONTEXT,
		"STATUS_TOKEN": "job-token",
		"MERGE_TOKEN": "pat-token",
		"UPDATER_HEAD_GATE_POLL_ATTEMPTS": "3",
		"UPDATER_HEAD_GATE_POLL_SECONDS": "0",
		"FAKE_GH_LOG": str(log),
		"FAKE_STATUS_SEQ": json.dumps([_gate_statuses()] if seq is None else seq),
		"FAKE_PR_JSON": json.dumps(_gate_pr() if pr is None else pr),
		**env_overrides,
	}
	result = subprocess.run(["bash", "-c", _verify_step("verify_head_gate")["run"]], env=env, capture_output=True, text=True)
	calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
	return result, calls


def _gate_posts(calls: list[dict]) -> list[dict]:
	return [call for call in calls if call["args"][:3] == ["api", "-X", "POST"]]


def test_head_gate_step_follows_publish_and_never_merges() -> None:
	steps = _verify_job()["steps"]
	ids = [step.get("id") for step in steps]
	names = [step.get("name") for step in steps]
	step = _verify_step("verify_head_gate")
	assert step["if"] == HEAD_GATE_IF
	assert ids.index("verify_publish") < ids.index("verify_head_gate") < names.index("Mark verify failure")
	assert step["env"]["REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED"] == "${{ vars.REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED || 'true' }}"
	assert step["env"]["MERGE_TOKEN"] == "${{ secrets.GH_PAT }}"
	run = step["run"]
	assert "${{" not in run
	assert "gh pr merge" not in run
	assert "context=ai-review/head-gate" in run
	assert run.rstrip().endswith("exit 0")


def test_head_gate_posts_success_when_verify_and_integrity_are_green(tmp_path: Path) -> None:
	result, calls = _run_head_gate(tmp_path)
	assert result.returncode == 0, result.stderr
	posts = _gate_posts(calls)
	assert len(posts) == 1
	assert posts[0]["args"][3] == f"repos/{REPO}/statuses/{PUSHED_SHA}"
	assert "state=success" in posts[0]["args"] and "context=ai-review/head-gate" in posts[0]["args"]
	assert posts[0]["token"] == "pat-token"
	assert f"REVIEW_HEAD_GATE mode=updater_exempt source=verify_job pr=77 head={PUSHED_SHA} outcome=posted reason=verified" in result.stdout


def test_head_gate_waits_for_a_late_integrity_status(tmp_path: Path) -> None:
	seq = [_gate_statuses(integrity=None), _gate_statuses(integrity="pending"), _gate_statuses()]
	result, calls = _run_head_gate(tmp_path, seq)
	assert result.returncode == 0, result.stderr
	assert sum("/status" in call["args"][1] for call in calls if call["args"][1] != "-X") == 3
	assert len(_gate_posts(calls)) == 1


@pytest.mark.parametrize("seq,pr,reason,reads", [
	([_gate_statuses(integrity="failure")], None, "integrity_not_success", 1),
	([_gate_statuses(verify="failure")], None, "verify_not_success", 1),
	([_gate_statuses(integrity=None)], None, "integrity_missing", 3),
	([_gate_statuses(integrity=None, total=150)], None, "status_page_incomplete", 1),
	([None], None, "status_read_failed", 3),
	([{"statuses": "nope"}], None, "invalid_response", 1),
	(None, _gate_pr(sha="b" * 40), "head_moved", 1),
	(None, _gate_pr(repo={"full_name": "evil/consumer"}), "fork_head", 1),
	(None, _gate_pr(ref="ai/issue-1"), "not_updater_branch", 1),
	(None, {**_gate_pr(), "state": "closed"}, "pr_not_open", 1),
])
def test_head_gate_fails_closed(tmp_path: Path, seq: list | None, pr: dict | None, reason: str, reads: int) -> None:
	result, calls = _run_head_gate(tmp_path, seq, pr)
	assert result.returncode == 0, result.stderr
	assert not _gate_posts(calls)
	assert f"outcome=skipped reason={reason}" in result.stdout
	assert sum(call["args"][1].endswith("/status?per_page=100") for call in calls) == reads
	# Only an expired wait window (still missing or pending) raises the warning.
	assert ("left pending" in result.stdout) == (reason in {"integrity_missing", "status_read_failed"})


@pytest.mark.parametrize("env,reason", [
	({"REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED": "false"}, "disabled"),
	({"REVIEW_HEAD_GATE_STATUS_ENABLED": "false"}, "status_disabled"),
	({"REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED": "FALSE"}, "disabled"),
	({"PR_NUMBER": "abc"}, "invalid_input"),
])
def test_head_gate_switches_make_no_api_call(tmp_path: Path, env: dict, reason: str) -> None:
	result, calls = _run_head_gate(tmp_path, **env)
	assert result.returncode == 0, result.stderr
	assert calls == []
	assert f"outcome=skipped reason={reason}" in result.stdout


def test_head_gate_falls_back_to_the_job_token_and_never_fails_the_job(tmp_path: Path) -> None:
	result, calls = _run_head_gate(tmp_path, FAKE_REFUSED_TOKENS="pat-token")
	assert result.returncode == 0, result.stderr
	assert [post["token"] for post in _gate_posts(calls)] == ["pat-token", "job-token"]
	assert "outcome=posted reason=verified" in result.stdout
	result, calls = _run_head_gate(tmp_path / "both", FAKE_REFUSED_TOKENS="pat-token,job-token")
	assert result.returncode == 0, result.stderr
	assert len(_gate_posts(calls)) == 2
	assert "outcome=skipped reason=post_failed" in result.stdout


def test_head_gate_accepts_resend_branch(tmp_path: Path) -> None:
	# The verify step's own regex copy must accept -r2..-r5 resend branches
	# like REVIEW_HEAD_GATE_UPDATER_BRANCH_RE, or their gate stays pending (#9483).
	result, calls = _run_head_gate(tmp_path, pr=_gate_pr(ref=UPDATER_BRANCH + "-r3"))
	assert result.returncode == 0, result.stderr
	assert len(_gate_posts(calls)) == 1
	assert "outcome=posted reason=verified" in result.stdout
	result, calls = _run_head_gate(tmp_path / "r6", pr=_gate_pr(ref=UPDATER_BRANCH + "-r6"))
	assert result.returncode == 0, result.stderr
	assert not _gate_posts(calls)
	assert "outcome=skipped reason=not_updater_branch" in result.stdout


def test_head_gate_step_constants_match_the_review_helper() -> None:
	# verify_head_gate copies the branch regex and status contexts of
	# scripts/review_head_gate.sh; a rename on one side must fail CI rather
	# than leave updater PRs pending again (#9483).
	helper = (REPO_ROOT / "scripts" / "review_head_gate.sh").read_text(encoding="utf-8")
	consts = dict(re.findall(r"^(REVIEW_HEAD_GATE_UPDATER_[A-Z_]+)=['\"]([^'\"]+)['\"]$", helper, re.MULTILINE))
	run = _verify_step("verify_head_gate")["run"]
	assert f"--arg re '{consts['REVIEW_HEAD_GATE_UPDATER_BRANCH_RE']}'" in run
	assert f'--arg i "{consts["REVIEW_HEAD_GATE_UPDATER_INTEGRITY_CONTEXT"]}"' in run
	assert '--arg v "${VERIFY_STATUS_CONTEXT}"' in run
	assert _verify_job()["env"]["VERIFY_STATUS_CONTEXT"] == consts["REVIEW_HEAD_GATE_UPDATER_VERIFY_CONTEXT"]
