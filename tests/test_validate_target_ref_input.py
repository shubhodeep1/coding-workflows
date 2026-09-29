"""Contract for the optional `target_ref` validate input.

/implement-plan-claude validates its project branch before the branch merges
into the default branch, so the reusable workflow and both dispatch wrappers
accept an explicit branch. Empty keeps the tracking-issue / default-branch
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


def test_project_branch_base_requires_one_parent_pr_into_default(tmp_path: Path):
	invoke = _make_invoke(tmp_path)
	branch = "claude/implement-plan-issue-4687-bind-rejections"
	parent_branch = "claude/implement-plan-issue-4586-hold-reason"
	sha = "b" * 40
	pr = _pr(branch, parent_branch, sha)
	parent = _pr(parent_branch, "main", "c" * 40)

	assert invoke([pr], target=branch, parent=[parent]) == (0, f"sha={sha}\n")
	assert len(invoke.calls) == 2
	assert f"head=owner:{parent_branch}" in invoke.calls[1]
	assert "base=" not in invoke.calls[1]

	# No parent PR, two parent PRs, or the parent read failing.
	assert invoke([pr], target=branch, parent=[]) == (1, "")
	assert invoke([pr], target=branch, parent=[parent, parent]) == (1, "")
	assert invoke([pr], target=branch, parent=[parent], second_failure=True) == (1, "")
	# A parent PR into another branch: only one level of stacking is allowed.
	assert invoke([pr], target=branch, parent=[_pr(parent_branch, "claude/implement-plan-grandparent")]) == (1, "")
	assert invoke([pr], target=branch, parent=[_pr(parent_branch, "stable")]) == (1, "")
	# The parent PR must pass the same identity checks.
	for edit in (
		{"head": {**parent["head"], "repo": {"full_name": "fork/repo"}}},
		{"base": {**parent["base"], "repo": {"full_name": "fork/repo"}}},
		{"head": {**parent["head"], "ref": "claude/implement-plan-other"}},
		{"head": {**parent["head"], "sha": "short"}},
		{"head": {key: value for key, value in parent["head"].items() if key != "sha"}},
		{"state": "closed"},
		{"author_association": "CONTRIBUTOR"},
	):
		assert invoke([pr], target=branch, parent=[{**parent, **edit}]) == (1, ""), edit
	# A base the jq pattern accepts but git rejects stops before the parent read,
	# with its own error.
	for bad_base in ("claude/implement-plan-a..b", "claude/implement-plan-a.lock", "claude/implement-plan-a."):
		assert invoke([_pr(branch, bad_base, sha)], target=branch, parent=[parent]) == (1, ""), bad_base
		assert len(invoke.calls) == 1, bad_base
		assert "its PR base is not a valid branch name" in invoke.stdout, bad_base
		assert bad_base not in invoke.stdout, bad_base
	# The target PR itself still needs a trusted, same-repo binding.
	assert invoke([{**pr, "author_association": "NONE"}], target=branch, parent=[parent]) == (1, "")
	assert invoke([pr, pr], target=branch, parent=[parent]) == (1, "")


HEAL_CREATED_AT = "2026-09-27T17:05:53Z"


def _heal_issue(**overrides) -> dict:
	"""A heal issue as scripts/workflow_failure_heal_intake.sh files it (issue #4665)."""
	issue = {
		"number": 4665,
		"user": {"login": "owner", "type": "User"},
		"author_association": "OWNER",
		"created_at": HEAL_CREATED_AT,
		"labels": [{"name": "ai:claude"}, {"name": "ai:workflow-heal"}],
		"body": (
			"<!-- workflow-failure-heal:fp=63e3d23a -->\n"
			"<!-- workflow-failure-heal:gen=1 -->\n"
			"\n"
			"- **Target branch:** `stable`\n"
			"\n"
			"## Automated workflow failure heal (generation 1 of max 3)\n"
		),
	}
	issue.update(overrides)
	return issue


def _labeled(label: str, actor: str = "owner", at: str = "2026-09-27T17:05:55Z") -> dict:
	return {"event": "labeled", "label": {"name": label}, "actor": {"login": actor}, "created_at": at}


HEAL_EVENTS = [_labeled("ai:workflow-heal"), _labeled("ai:claude", at="2026-09-27T17:06:28Z")]


def test_stable_base_requires_verified_heal_issue(tmp_path: Path):
	invoke = _make_invoke(tmp_path)
	branch = "claude/implement-plan-issue-4665-reissue-new-output-paths"
	sha = "d" * 40
	pr = _pr(branch, "stable", sha)
	heal = _heal_issue()

	assert invoke([pr], target=branch, issue=heal, events=HEAL_EVENTS) == (0, f"sha={sha}\n")
	assert len(invoke.calls) == 3
	assert "repos/owner/repo/issues/4665" in invoke.calls[1]
	assert "/events" not in invoke.calls[1]
	assert invoke.calls[2].endswith("repos/owner/repo/issues/4665/events")
	assert "per_page=100" in invoke.calls[2]
	assert "--paginate --slurp" in invoke.calls[2]
	# Unrelated later labels, including by other accounts, do not matter.
	assert invoke([pr], target=branch, issue=heal, events=HEAL_EVENTS + [_labeled("ai:claude-blocked", actor="someone")])[0] == 0
	# github-actions[bot] is the other heal automation author.
	bot = {"login": "github-actions[bot]", "type": "Bot"}
	bot_pr = {**pr, "user": bot}
	bot_events = [_labeled("ai:workflow-heal", actor="github-actions[bot]")]
	assert invoke([bot_pr], target=branch, issue=_heal_issue(user=bot, author_association="NONE"), events=bot_events)[0] == 0
	# Label exactly at creation and at the 120 s edge are accepted.
	assert invoke([pr], target=branch, issue=heal, events=[_labeled("ai:workflow-heal", at=HEAL_CREATED_AT)])[0] == 0
	assert invoke([pr], target=branch, issue=heal, events=[_labeled("ai:workflow-heal", at="2026-09-27T17:07:53Z")])[0] == 0

	rejected_issues = {
		"label missing": _heal_issue(labels=[{"name": "ai:claude"}]),
		"no labels": {key: value for key, value in heal.items() if key != "labels"},
		"a pull request": _heal_issue(pull_request={"url": "x"}),
		"collaborator author": _heal_issue(author_association="COLLABORATOR"),
		"member author": _heal_issue(author_association="MEMBER"),
		"other bot author": _heal_issue(user={"login": "owner", "type": "Bot"}),
		"no user": {key: value for key, value in heal.items() if key != "user"},
		"author is not the PR author": _heal_issue(user={"login": "someone", "type": "User"}),
		"no fp marker": _heal_issue(body="- **Target branch:** `stable`\n"),
		"marker mid-line": _heal_issue(body="see <!-- workflow-failure-heal:fp=abc -->\n- **Target branch:** `stable`\n"),
		"other marker only": _heal_issue(body="<!-- check-failure-triage:fp=abc -->\n- **Target branch:** `stable`\n"),
		"no target branch": _heal_issue(body="<!-- workflow-failure-heal:fp=abc -->\n"),
		"target branch main": _heal_issue(body="<!-- workflow-failure-heal:fp=abc -->\n- **Target branch:** `main`\n"),
		"integration branch wins": _heal_issue(body=(
			"<!-- workflow-failure-heal:fp=abc -->\n- **Target branch:** `stable`\n"
			"- **Integration branch:** `orchestrator/project-1`\n"
		)),
		"null body": _heal_issue(body=None),
		"no created_at": {key: value for key, value in heal.items() if key != "created_at"},
		"unreadable created_at": _heal_issue(created_at="yesterday"),
		"not an object": [heal],
	}
	for name, issue in rejected_issues.items():
		assert invoke([pr], target=branch, issue=issue, events=HEAL_EVENTS) == (1, ""), name
		assert len(invoke.calls) == 2, name

	rejected_events = {
		"no labeled event": [_labeled("ai:claude")],
		"no events": [],
		"labelled by someone else": [_labeled("ai:workflow-heal", actor="someone")],
		"re-labelled by someone else": [_labeled("ai:workflow-heal"), _labeled("ai:workflow-heal", actor="someone", at="2026-09-28T01:00:00Z")],
		"labelled late": [_labeled("ai:workflow-heal", at="2026-09-27T17:07:54Z")],
		"labelled before creation": [_labeled("ai:workflow-heal", at="2026-09-27T17:05:52Z")],
		"unreadable event time": [_labeled("ai:workflow-heal", at="soon")],
		"no actor": [{"event": "labeled", "label": {"name": "ai:workflow-heal"}, "created_at": "2026-09-27T17:05:55Z"}],
		"not an array": {"events": HEAL_EVENTS},
	}
	for name, events in rejected_events.items():
		assert invoke([pr], target=branch, issue=heal, events=events) == (1, ""), name
		assert len(invoke.calls) == 3, name

	# Every events page is verified, so a long label history is read in full
	# rather than failing at 100 events.
	filler = [_labeled("ai:other", at="2026-09-27T18:00:00Z")] * 99
	assert invoke([pr], target=branch, issue=heal, event_pages=[HEAL_EVENTS[:1] + filler, HEAL_EVENTS[1:]])[0] == 0
	late_relabel = _labeled("ai:workflow-heal", actor="someone", at="2026-09-28T01:00:00Z")
	rejected_pages = {
		"re-labelled by someone else on a later page": [HEAL_EVENTS[:1] + filler, [late_relabel]],
		"labelled first on a later page, too late": [filler, [_labeled("ai:workflow-heal", at="2026-09-27T18:00:01Z")]],
		"pages not an array": {"pages": [HEAL_EVENTS]},
		"a bare event list, not the --slurp page wrapper": HEAL_EVENTS,
		"a page not an array": [HEAL_EVENTS, {"events": []}],
		"no pages": [],
	}
	for name, pages in rejected_pages.items():
		assert invoke([pr], target=branch, issue=heal, event_pages=pages) == (1, ""), name
		assert len(invoke.calls) == 3, name

	# Read failures fail closed.
	assert invoke([pr], target=branch, issue=heal, events=HEAL_EVENTS, second_failure=True) == (1, "")
	assert invoke([pr], target=branch, issue=heal, events=HEAL_EVENTS, third_failure=True) == (1, "")
	# The target PR author must be a plain login before anything else is read.
	# The 39-character cap counts a `[bot]` suffix too (an App slug is at most
	# 34 characters).
	for login in ("", "two words", "owner$(id)", "-owner", "owner-", "own--er", "o" * 40, "owner[bot]x", "o" * 35 + "[bot]", "o" * 39 + "[bot]", None):
		assert invoke([{**pr, "user": {"login": login, "type": "User"}}], target=branch, issue=heal, events=HEAL_EVENTS) == (1, ""), login
		assert len(invoke.calls) == 1, login
	assert invoke([{key: value for key, value in pr.items() if key != "user"}], target=branch, issue=heal, events=HEAL_EVENTS) == (1, "")
	# Logins that follow GitHub's rules pass the pattern: single inner hyphens,
	# the 39-character maximum, and a `[bot]` suffix within it.
	for login in ("o-w-n-e-r", "o" * 39, "o" * 34 + "[bot]"):
		user = {"login": login, "type": "User"}
		issue = _heal_issue(user=user)
		assert invoke([{**pr, "user": user}], target=branch, issue=issue, events=[_labeled("ai:workflow-heal", actor=login)])[0] == 0, login
	# A stable base is refused for a head that is not an issue project.
	other = "claude/implement-plan-some-project"
	assert invoke([_pr(other, "stable")], target=other, issue=heal, events=HEAL_EVENTS) == (1, "")
	assert invoke.calls and len(invoke.calls) == 1
	# Fork heads and untrusted authors are still refused.
	assert invoke([{**pr, "head": {**pr["head"], "repo": {"full_name": "fork/repo"}}}], target=branch, issue=heal, events=HEAL_EVENTS) == (1, "")
	assert invoke([{**pr, "author_association": "CONTRIBUTOR"}], target=branch, issue=heal, events=HEAL_EVENTS) == (1, "")


def test_stable_heal_issue_branch_line_forms(tmp_path: Path):
	invoke = _make_invoke(tmp_path)
	branch = "claude/implement-plan-issue-4665-reissue-new-output-paths"
	pr = _pr(branch, "stable")
	for line in (
		"- **Target branch:** `stable`",
		"Target branch: stable",
		"Target branch: `stable` (the release line)",
		"- **Integration branch:** `stable`",
		"Integration branch: stable\r",
	):
		issue = _heal_issue(body=f"<!-- workflow-failure-heal:fp=abc -->\r\n{line}\n")
		assert invoke([pr], target=branch, issue=issue, events=HEAL_EVENTS)[0] == 0, line
	for line in ("Target branch: stable-2", "Target branch: `stable2`", "Target branch: stable now"):
		issue = _heal_issue(body=f"<!-- workflow-failure-heal:fp=abc -->\n{line}\n")
		assert invoke([pr], target=branch, issue=issue, events=HEAL_EVENTS) == (1, ""), line


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
