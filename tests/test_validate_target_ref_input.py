"""Contract for the optional `target_ref` validate input.

An explicit branch whose single open PR goes into the default branch can be
validated before it merges, so the reusable workflow and both dispatch
wrappers accept it. Empty keeps the tracking-issue / default-branch
resolution unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parent.parent
REUSABLE = ROOT / ".github" / "workflows" / "validate.yml"
WRAPPERS = (
	ROOT / ".github" / "workflows" / "internal-validate.yml",
	ROOT / "workflow-templates" / "ai-validate.yml",
)


def _load(path: Path) -> dict:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	# YAML 1.1 reads an unquoted `on:` key as boolean True.
	if True in workflow:
		workflow["on"] = workflow.pop(True)
	return workflow


def test_reusable_validate_declares_optional_target_ref():
	target_ref = _load(REUSABLE)["on"]["workflow_call"]["inputs"]["target_ref"]
	assert target_ref["required"] is False
	assert target_ref["default"] == ""
	assert target_ref["type"] == "string"


def test_checkout_prefers_target_ref_then_integration_ref_then_default():
	workflow = _load(REUSABLE)
	steps = workflow["jobs"]["validate"]["steps"]
	checkout = next(step for step in steps if step["name"] == "Checkout repository")
	verify = next(step for step in steps if step["name"] == "Verify authorized checkout")
	expression = "${{ steps.authorized_target.outputs.sha || steps.refctx.outputs.ref || github.event.repository.default_branch }}"
	assert checkout["with"]["ref"] == expression
	assert checkout["with"]["persist-credentials"] is False
	assert "git rev-parse HEAD" in verify["run"]
	assert steps.index(verify) < next(i for i, step in enumerate(steps) if step["name"] == "Initialize workspace metadata")
	assert "git remote set-url origin" not in REUSABLE.read_text(encoding="utf-8")
	assert "VALIDATE_RESOLVED_REF: " + expression in REUSABLE.read_text(encoding="utf-8")


def _authorize_step() -> dict:
	return next(step for step in _load(REUSABLE)["jobs"]["validate"]["steps"] if step["name"] == "Authorize explicit validation target")


def _gh_stub(bin_dir: Path) -> None:
	"""Stub `gh` that answers by request and logs one line per call.

	The target listing (head=<owner>:$VALIDATE_TARGET_REF) prints PULLS_JSON,
	any other pull listing prints PARENT_JSON, an issue-events read prints
	EVENTS_JSON (the slurped pages), and an issue read prints ISSUE_JSON. FAIL_API=yes fails every
	call; FAIL_SECOND=yes fails every call after the first; FAIL_THIRD=yes
	fails every call after the second.
	"""
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(
		"#!/usr/bin/env bash\n"
		"printf '%s\\n' \"$*\" >> \"${GH_CALLS}\"\n"
		"[ \"${FAIL_API:-}\" != yes ] || exit 1\n"
		"calls=\"$(wc -l < \"${GH_CALLS}\")\"\n"
		"[ \"${FAIL_SECOND:-}\" != yes ] || [ \"${calls}\" -le 1 ] || exit 1\n"
		"[ \"${FAIL_THIRD:-}\" != yes ] || [ \"${calls}\" -le 2 ] || exit 1\n"
		"case \" $* \" in\n"
		"  *\" repos/${GITHUB_REPOSITORY}/issues/\"*\"/events \"*) printf '%s' \"${EVENTS_JSON}\" ;;\n"
		"  *\" repos/${GITHUB_REPOSITORY}/issues/\"*) printf '%s' \"${ISSUE_JSON}\" ;;\n"
		"  *\" head=${GITHUB_REPOSITORY%%/*}:${VALIDATE_TARGET_REF} \"*) printf '%s' \"${PULLS_JSON}\" ;;\n"
		"  *) printf '%s' \"${PARENT_JSON}\" ;;\n"
		"esac\n",
		encoding="utf-8",
	)
	gh.chmod(0o755)


def _pr(head: str, base: str, sha: str = "a" * 40) -> dict:
	return {
		"state": "open",
		"author_association": "OWNER",
		"user": {"login": "owner", "type": "User"},
		"head": {"ref": head, "sha": sha, "repo": {"full_name": "owner/repo"}},
		"base": {"ref": base, "repo": {"full_name": "owner/repo"}},
	}


def _make_invoke(tmp_path: Path):
	step = _authorize_step()
	bin_dir = tmp_path / "bin"
	_gh_stub(bin_dir)

	def invoke(pulls, target="claude/implement-plan-example", api_failure=False, parent=None, issue=None, second_failure=False, events=None, third_failure=False, event_pages=None):
		output = tmp_path / "output"
		output.write_text("", encoding="utf-8")
		calls = tmp_path / "calls"
		calls.write_text("", encoding="utf-8")
		env = os.environ.copy()
		env.update({
			"PATH": f"{bin_dir}:{env['PATH']}", "GITHUB_REPOSITORY": "owner/repo",
			"GITHUB_OUTPUT": str(output), "GH_TOKEN": "placeholder", "GH_CALLS": str(calls),
			"VALIDATE_DEFAULT_BRANCH": "main", "VALIDATE_TARGET_REF": target,
			"PULLS_JSON": json.dumps([pulls]), "FAIL_API": "yes" if api_failure else "no",
			"PARENT_JSON": json.dumps([parent if parent is not None else []]),
			"ISSUE_JSON": json.dumps(issue if issue is not None else {}),
			"FAIL_SECOND": "yes" if second_failure else "no",
			"EVENTS_JSON": json.dumps(event_pages if event_pages is not None else [events if events is not None else []]),
			"FAIL_THIRD": "yes" if third_failure else "no",
		})
		result = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True)
		invoke.calls = [line for line in calls.read_text(encoding="utf-8").splitlines() if line]
		invoke.stdout = result.stdout
		return result.returncode, output.read_text(encoding="utf-8")

	invoke.calls = []
	invoke.stdout = ""
	return invoke


def test_explicit_target_authorization_cases(tmp_path: Path):
	invoke = _make_invoke(tmp_path)
	branch = "claude/implement-plan-example"
	sha = "a" * 40
	pr = _pr(branch, "main", sha)

	assert invoke([pr]) == (0, f"sha={sha}\n")
	assert invoke([], target="") == (0, "")
	assert invoke([pr], target="other-branch")[0] != 0
	assert invoke([pr], api_failure=True)[0] != 0
	assert invoke([])[0] != 0
	assert invoke([pr, pr])[0] != 0
	for edit in (
		{"head": {**pr["head"], "sha": "short"}},
		{"head": {**pr["head"], "repo": {"full_name": "fork/repo"}}},
		{"base": {**pr["base"], "repo": {"full_name": "fork/repo"}}},
		{"state": "closed"},
		{"author_association": "CONTRIBUTOR"},
	):
		assert invoke([{**pr, **edit}])[0] != 0


def test_target_listing_has_no_base_filter(tmp_path: Path):
	invoke = _make_invoke(tmp_path)
	pr = _pr("claude/implement-plan-example", "main")
	assert invoke([pr])[0] == 0
	assert len(invoke.calls) == 1
	assert "head=owner:claude/implement-plan-example" in invoke.calls[0]
	assert "base=" not in invoke.calls[0]
	assert "-f base=" not in _authorize_step()["run"]


def test_stacked_and_stable_bases_are_refused(tmp_path: Path):
	"""The project-branch and stable bases (#4734 / #4791) were removed with
	the retired /implement-plan-claude chain: only a PR into the default
	branch authorizes an explicit target, with one listing call."""
	invoke = _make_invoke(tmp_path)
	branch = "claude/implement-plan-issue-4665-reissue-new-output-paths"
	for base in ("claude/implement-plan-issue-4586-hold-reason", "stable"):
		assert invoke([_pr(branch, base)], target=branch) == (1, ""), base
		assert len(invoke.calls) == 1, base


def test_other_bases_are_refused(tmp_path: Path):
	invoke = _make_invoke(tmp_path)
	branch = "claude/implement-plan-example"
	for base in ("develop", "claude/other-branch", "release/1.0"):
		assert invoke([_pr(branch, base)], target=branch) == (1, ""), base
		assert len(invoke.calls) == 1, base


def test_validation_hooks_use_verified_helper():
	steps = _load(REUSABLE)["jobs"]["validate"]["steps"]
	fetch = next(step for step in steps if step["name"] == "Fetch workflow support files")["run"]
	assert 'helper_ref="main"' in fetch
	assert 'VALIDATE_HOOK_HELPER=' in fetch
	assert 'WORKFLOW_SUPPORT_REF="${support_sha}"' in fetch
	assert 'helper_path="scripts/stage_workflow_support.sh"' not in fetch
	for name in ("after_create", "before_run", "after_run", "before_remove"):
		step = next(step for step in steps if step["name"] == f"Run workspace {name} hook")
		assert f'bash "${{VALIDATE_HOOK_HELPER}}" validate {name}' in step["run"]
	staging = (ROOT / "scripts" / "stage_workflow_support.sh").read_text(encoding="utf-8")
	assert 'if [ "${IS_SELF_REPO}" = "true" ] && [ -z "${VALIDATE_AUTHORIZED_TARGET_SHA:-}" ]; then' in staging
	assert 'require_remote="true"' in staging
	assert 'allow_main_fallback="false"' in staging


def test_explicit_target_checkout_rejects_moved_head(tmp_path: Path):
	step = next(step for step in _load(REUSABLE)["jobs"]["validate"]["steps"] if step["name"] == "Verify authorized checkout")
	subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
	(tmp_path / "file.txt").write_text("content", encoding="utf-8")
	subprocess.run(["git", "add", "file.txt"], cwd=tmp_path, check=True)
	subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "test"], cwd=tmp_path, check=True)
	sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
	for authorized_sha, expected_rc in ((sha, 0), ("b" * 40, 1)):
		env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE")}
		env["VALIDATE_AUTHORIZED_SHA"] = authorized_sha
		assert subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True).returncode == expected_rc


def test_wrappers_forward_target_ref():
	for wrapper in WRAPPERS:
		workflow = _load(wrapper)
		dispatch_input = workflow["on"]["workflow_dispatch"]["inputs"]["target_ref"]
		assert dispatch_input["default"] == "", wrapper
		assert dispatch_input["required"] is False, wrapper
		assert workflow["jobs"]["validate"]["with"]["target_ref"] == "${{ inputs.target_ref || '' }}", wrapper


def test_consumer_wrapper_has_no_pr_number_input():
	# /implement-plan-claude passes -f pr_number=0 only to internal-validate.yml;
	# the consumer wrapper rejects unknown dispatch inputs.
	assert "pr_number" not in _load(WRAPPERS[1])["on"]["workflow_dispatch"]["inputs"]
