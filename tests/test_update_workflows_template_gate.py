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

import os
import re
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
}
BOOTSTRAP_SCRIPTS = {
	"scripts/verify_release_manifest.py",
	"scripts/release_manifest.py",
	"scripts/workflow_wrapper_refs.py",
	"scripts/apply_audit_gate_assets.py",
	"scripts/assemble_changelog.py",
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
	# statuses: write its status post fails and the job fails closed.
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
	assert caller["secrets"] == "inherit"
	text = TEMPLATE.read_text(encoding="utf-8")
	assert "commits directly to the default branch" not in text
	assert "UPDATER_PR_DELIVERY_ENABLED" in text
	assert STATUS_CONTEXT in text


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
	assert text.count("gh pr merge") == 1
	run = _verify_step("verify_publish")["run"]
	success = run.index("-f state=success")
	merge = run.index('gh pr merge "${PR_NUMBER}"')
	assert success < merge
	assert run.index('if [ "${VERIFIED:-}" = "true" ]; then') < merge
	assert '--auto --squash --match-head-commit "${PUSHED_SHA}"' in run
	commit_push = [step for step in _workflow()["jobs"]["update-wrappers"]["steps"] if step.get("id") == "commit_push"][0]
	assert "gh pr merge" not in commit_push["run"]
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


def test_template_gate_is_registered_in_ci() -> None:
	assert "tests/test_update_workflows_template_gate.py" in CI.read_text(encoding="utf-8")


# ── Behavioural: publish step against a fake gh ──────────────────────────


FAKE_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
printf 'token=%s\n' "${GH_TOKEN:-}" >> "${FAKE_GH_LOG}"
if [ "$1" = "api" ]; then exit "${FAKE_STATUS_RC:-0}"; fi
if [ "$1 $2" = "pr merge" ]; then exit "${FAKE_MERGE_RC:-0}"; fi
exit 1
"""


def _run_publish(tmp_path: Path, verified: str, status_rc: int = 0, merge_rc: int = 0) -> tuple[subprocess.CompletedProcess, list[str], dict]:
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


def test_merge_failure_is_reported_not_retried(tmp_path: Path) -> None:
	result, calls, outputs = _run_publish(tmp_path, "true", merge_rc=1)
	assert result.returncode == 0, result.stderr
	assert sum(call.startswith("pr merge") for call in calls) == 1
	assert outputs["auto_merge"] == "failed"
