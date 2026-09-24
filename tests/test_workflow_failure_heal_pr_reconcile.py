#!/usr/bin/env python3
"""scripts/workflow_failure_heal_pr_reconcile.sh: heal PRs of a closed source PR.

PR #4349 (the heal fix for PR #4323's review/autofix failure) stayed open,
merge-queued, on the branch of PR #4323 after PR #4323 closed unmerged. The
script closes such heal PRs, or, when the source PR merged, moves only the
heal commits onto the source base and re-points the heal PR.

Each case runs the script against a real bare git origin (the source PR head
under refs/pull/<n>/head, a squash-merged base) and a fake `gh` that serves the
heal issue / PR listings from a JSON state file and records every write.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "workflow_failure_heal_pr_reconcile.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "internal-cancel-on-pr-close.yml"
REPO = "shubhodeep1/coding-workflows"
BASE = "orchestrator/project-4139"
SOURCE_BRANCH = "ai/issue-4314"
SOURCE_PR = 4323
HEAL_ISSUE = 4342
HEAL_PR = 4349
HEAL_BRANCH = f"ai/issue-{HEAL_ISSUE}"

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
if args[:1] != ["api"]:
	state_path.write_text(json.dumps(state)); sys.exit(1)
method, path, fields, jq_expr = "GET", "", {}, None
i = 1
while i < len(args):
	arg = args[i]
	if arg == "--method":
		method = args[i + 1]; i += 2; continue
	if arg in ("-f", "-F"):
		key, _, value = args[i + 1].partition("="); fields[key] = value; i += 2; continue
	if arg == "--jq":
		jq_expr = args[i + 1]; i += 2; continue
	if arg == "--paginate":
		i += 1; continue
	path = arg; i += 1
out = None
if method == "GET" and path == f"repos/{state['repo']}/issues":
	out = [issue for issue in state["issues"] if fields.get("labels") in issue["labels"]]
elif method == "GET" and path == f"repos/{state['repo']}/pulls":
	owner = state["repo"].split("/")[0]
	out = [pr for pr in state["pulls"] if pr["state"] == "open" and f"{owner}:{pr['head']['ref']}" == fields.get("head")]
elif method == "PATCH" and path.startswith(f"repos/{state['repo']}/pulls/"):
	number = int(path.rsplit("/", 1)[1])
	for pr in state["pulls"]:
		if pr["number"] == number:
			if "state" in fields: pr["state"] = fields["state"]
			if "base" in fields: pr["base"]["ref"] = fields["base"]
	state.setdefault("writes", []).append({"method": method, "path": path, "fields": fields}); out = {}
elif method in ("POST", "PATCH", "DELETE"):
	state.setdefault("writes", []).append({"method": method, "path": path, "fields": fields}); out = {}
state_path.write_text(json.dumps(state))
if out is None:
	sys.exit(1)
text = json.dumps(out)
if jq_expr:
	text = subprocess.run(["jq", "-c", jq_expr], input=text, capture_output=True, text=True, check=True).stdout
sys.stdout.write(text)
'''


def _git(cwd: Path, *args: str) -> str:
	env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
	return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


def _commit(work: Path, name: str, content: str, message: str) -> str:
	(work / name).write_text(content, encoding="utf-8")
	_git(work, "add", name)
	_git(work, "commit", "-q", "-m", message)
	return _git(work, "rev-parse", "HEAD")


def _stage(tmp: Path, *, heal_touches_source: bool = False, heal_base: str = SOURCE_BRANCH) -> dict:
	"""Origin with base, source PR head (refs/pull), a heal branch cut from an older source head, a squash merge."""
	origin = tmp / "origin.git"
	_git(tmp, "init", "-q", "--bare", str(origin))
	seed = tmp / "seed"
	_git(tmp, "init", "-q", "-b", BASE, str(seed))
	_commit(seed, "base.txt", "base\n", "base")
	_git(seed, "remote", "add", "origin", str(origin))
	_git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{BASE}")
	_git(seed, "checkout", "-q", "-b", SOURCE_BRANCH)
	_commit(seed, "source.txt", "v1\n", "source 1")
	# The heal branch forks from the source head of that time ...
	_git(seed, "checkout", "-q", "-b", HEAL_BRANCH)
	if heal_touches_source:
		_commit(seed, "source.txt", "healed\n", "heal fix")
	else:
		_commit(seed, "heal.txt", "fix\n", "heal fix")
	heal_sha = _git(seed, "rev-parse", "HEAD")
	# ... and the source PR moves on afterwards (e16fdfd -> 07e76f9 on #4323).
	_git(seed, "checkout", "-q", SOURCE_BRANCH)
	source_sha = _commit(seed, "source.txt", "v2\n", "source 2")
	_git(seed, "push", "-q", "origin", f"{HEAL_BRANCH}:refs/heads/{HEAL_BRANCH}", f"{SOURCE_BRANCH}:refs/heads/{SOURCE_BRANCH}", f"{source_sha}:refs/pull/{SOURCE_PR}/head")
	work = tmp / "work"
	_git(tmp, "clone", "-q", "-b", BASE, str(origin), str(work))
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	state = {
		"repo": REPO,
		"issues": [
			{"number": HEAL_ISSUE, "labels": "ai:workflow-heal", "body": f"<!-- workflow-failure-heal:source={REPO}#{SOURCE_PR} -->\nHeal body"},
			# Another source PR's heal issue: never touched.
			{"number": 4400, "labels": "ai:workflow-heal", "body": f"<!-- workflow-failure-heal:source={REPO}#{SOURCE_PR}1 -->"},
		],
		"pulls": [
			{"number": HEAL_PR, "state": "open", "head": {"ref": HEAL_BRANCH, "sha": heal_sha}, "base": {"ref": heal_base}, "labels": [{"name": "ai:merge-queued"}]},
		],
	}
	state_file = tmp / "state.json"
	state_file.write_text(json.dumps(state), encoding="utf-8")
	return {"origin": origin, "seed": seed, "work": work, "bin": bin_dir, "state_file": state_file, "heal_sha": heal_sha, "source_sha": source_sha}


def _squash_merge_source(stage: dict) -> str:
	seed = stage["seed"]
	_git(seed, "checkout", "-q", BASE)
	_git(seed, "merge", "-q", "--squash", SOURCE_BRANCH)
	_git(seed, "commit", "-q", "-m", f"AI implementation for issue #4314 (#{SOURCE_PR})")
	_git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{BASE}")
	return _git(seed, "rev-parse", "HEAD")


def _run(stage: dict, *, merged: bool, **extra_env: str) -> tuple[subprocess.CompletedProcess, dict]:
	env = {
		"PATH": f"{stage['bin']}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
		"HOME": str(stage["work"].parent),
		"FAKE_GH_STATE": str(stage["state_file"]),
		"GH_TOKEN": "synthetic",
		"REPOSITORY": REPO,
		"SOURCE_PR_NUMBER": str(SOURCE_PR),
		"SOURCE_PR_MERGED": "true" if merged else "false",
		"SOURCE_HEAD_REF": SOURCE_BRANCH,
		"SOURCE_HEAD_SHA": stage["source_sha"].upper(),
		"SOURCE_HEAD_REPO": REPO,
		"SOURCE_BASE_REF": BASE,
		"RUNNER_TEMP": str(stage["work"].parent / "runner-temp"),
		"PYTHONDONTWRITEBYTECODE": "1",
		**extra_env,
	}
	result = subprocess.run(["bash", str(SCRIPT)], cwd=stage["work"], env=env, capture_output=True, text=True, check=False)
	return result, json.loads(stage["state_file"].read_text(encoding="utf-8"))


def _writes(state: dict, method: str, suffix: str) -> list[dict]:
	return [w for w in state.get("writes", []) if w["method"] == method and w["path"].endswith(suffix)]


def _origin_ref(stage: dict, ref: str) -> str:
	return _git(stage["origin"], "rev-parse", ref)


def test_unmerged_source_closes_heal_pr_and_issue_without_touching_the_branch() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		stage = _stage(Path(tmp_name))
		result, state = _run(stage, merged=False)
		assert result.returncode == 0, result.stderr
		assert f"WORKFLOW_HEAL_PR_RECONCILE closed heal_pr={HEAL_PR} heal_issue={HEAL_ISSUE} source_pr={SOURCE_PR} reason=source_closed_unmerged" in result.stdout
		assert _writes(state, "PATCH", f"pulls/{HEAL_PR}")[0]["fields"] == {"state": "closed"}
		assert _writes(state, "DELETE", f"issues/{HEAL_PR}/labels/ai:merge-queued")
		assert _writes(state, "PATCH", f"issues/{HEAL_ISSUE}")[0]["fields"] == {"state": "closed", "state_reason": "not_planned"}
		pr_comment = _writes(state, "POST", f"issues/{HEAL_PR}/comments")[0]["fields"]["body"]
		assert f"#{SOURCE_PR} closed without merging" in pr_comment and "not re-pointed" in pr_comment
		# Never re-pointed, never pushed; the other source's heal issue is untouched.
		assert not any("base" in w["fields"] for w in state["writes"])
		assert _origin_ref(stage, f"refs/heads/{HEAL_BRANCH}") == stage["heal_sha"]
		assert not any("4400" in w["path"] for w in state["writes"])


def test_merged_source_moves_only_the_heal_commits_onto_the_base() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		stage = _stage(Path(tmp_name))
		base_sha = _squash_merge_source(stage)
		result, state = _run(stage, merged=True)
		assert result.returncode == 0, result.stderr
		assert f"retargeted heal_pr={HEAL_PR} heal_branch={HEAL_BRANCH} source_pr={SOURCE_PR} base={BASE}" in result.stdout, result.stdout
		new_head = _origin_ref(stage, f"refs/heads/{HEAL_BRANCH}")
		assert new_head != stage["heal_sha"]
		# Exactly the heal commit on top of the squash-merged base.
		assert _git(stage["origin"], "rev-parse", f"{new_head}^") == base_sha
		assert _git(stage["origin"], "log", "--format=%s", f"{base_sha}..{new_head}") == "heal fix"
		assert _git(stage["origin"], "show", f"{new_head}:source.txt") == "v2"
		assert _writes(state, "PATCH", f"pulls/{HEAL_PR}")[0]["fields"] == {"base": BASE}
		assert f"merged into `{BASE}`" in _writes(state, "POST", f"issues/{HEAL_PR}/comments")[0]["fields"]["body"]
		assert not _writes(state, "PATCH", f"issues/{HEAL_ISSUE}")


def test_merged_source_already_repointed_by_github_is_rebased_without_a_base_patch() -> None:
	# GitHub re-points PRs to the merged PR's base when it deletes the head branch.
	with tempfile.TemporaryDirectory() as tmp_name:
		stage = _stage(Path(tmp_name), heal_base=BASE)
		base_sha = _squash_merge_source(stage)
		result, state = _run(stage, merged=True)
		assert "retargeted" in result.stdout, result.stdout
		assert _git(stage["origin"], "rev-parse", f"refs/heads/{HEAL_BRANCH}^") == base_sha
		assert not _writes(state, "PATCH", f"pulls/{HEAL_PR}")


def test_merged_source_with_conflicting_heal_commit_closes_heal_pr() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		stage = _stage(Path(tmp_name), heal_touches_source=True)
		_squash_merge_source(stage)
		result, state = _run(stage, merged=True)
		assert f"closed heal_pr={HEAL_PR} heal_issue={HEAL_ISSUE} source_pr={SOURCE_PR} reason=source_merged_rebase_conflict" in result.stdout, result.stdout
		assert _origin_ref(stage, f"refs/heads/{HEAL_BRANCH}") == stage["heal_sha"]
		assert _writes(state, "PATCH", f"pulls/{HEAL_PR}")[0]["fields"] == {"state": "closed"}
		assert _writes(state, "PATCH", f"issues/{HEAL_ISSUE}")[0]["fields"] == {"state": "closed", "state_reason": "not_planned"}


def test_heal_pr_on_an_unrelated_base_and_gates_are_left_alone() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		stage = _stage(Path(tmp_name), heal_base="stable")
		result, state = _run(stage, merged=False)
		assert f"skip reason=unrelated_base heal_pr={HEAL_PR} base=stable" in result.stdout
		assert not state.get("writes")
	with tempfile.TemporaryDirectory() as tmp_name:
		stage = _stage(Path(tmp_name))
		for extra, expected in (
			({"WORKFLOW_HEAL_PR_RECONCILE_ENABLED": "false"}, "skip reason=disabled"),
			({"SOURCE_HEAD_REPO": "someone/fork"}, "skip reason=fork_head"),
			({"SOURCE_HEAD_REF": "../escape"}, "skip reason=missing_context"),
			({"SOURCE_PR_NUMBER": "99"}, "noop reason=no_heal_issue pr=99"),
		):
			result, state = _run(stage, merged=False, **extra)
			assert result.returncode == 0 and expected in result.stdout, (extra, result.stdout)
			assert not state.get("writes"), extra


def test_workflow_runs_reconcile_on_pr_close_only() -> None:
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	job = workflow["jobs"]["heal-pr-reconcile"]
	assert job["if"] == "github.event_name == 'pull_request' && github.event.action == 'closed'"
	assert job["env"]["SOURCE_PR_MERGED"] == "${{ github.event.pull_request.merged }}"
	assert job["env"]["WORKFLOW_HEAL_PR_RECONCILE_ENABLED"] == "${{ vars.WORKFLOW_HEAL_PR_RECONCILE_ENABLED || 'true' }}"
	steps = job["steps"]
	assert steps[0]["with"]["ref"] == "main" and steps[0]["with"]["fetch-depth"] == 0
	assert steps[1]["continue-on-error"] is True
	assert "scripts/workflow_failure_heal_pr_reconcile.sh" in steps[1]["run"]
	# The existing cancel job is unchanged.
	assert workflow["jobs"]["cancel"]["uses"] == "shubhodeep1/coding-workflows/.github/workflows/cancel_on_pr_close.yml@main"
