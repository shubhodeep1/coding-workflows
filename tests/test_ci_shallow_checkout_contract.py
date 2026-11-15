"""Shallow checkouts in the three slow CI jobs (cut-ci-runner-time Phase 1).

`tests-hooks-and-orchestrator`, `tests-release-and-log-analysis` and the
24-shard `tests-system-python` matrix used to clone full history on every
run. They now check out at depth 1, and a dedicated step right after the
checkout fetches only what the history readers need:

  * tests/claude_template_drift.py diffs `base...HEAD`, which needs the
    merge-base of the PR base and HEAD;
  * the Prepare steps' `sync_claude_live_copies.py sync --dry-run` runs a
    per-path `git log` and `merge-base --is-ancestor`, and falls back to
    `git merge-base HEAD origin/main` for a branch-creation or force push.

The step fetches a 200-commit window of HEAD and main plus the base (or push
before) commit, unshallows those two refs when the merge-base is deeper, and
fails the job with `::error::` when a needed commit cannot be fetched.

The static tests pin the layout; the behaviour tests run the shipped shell
against a local bare origin that mimics an actions/checkout depth-1 clone.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SHALLOW_JOBS = {
	"tests-hooks-and-orchestrator": "Fetch base commits for the shallow checkout (hooks and orchestrator)",
	"tests-release-and-log-analysis": "Fetch base commits for the shallow checkout (release and log analysis)",
	"tests-system-python": "Fetch base commits for the shallow checkout (system Python)",
}
FETCH_STEP_ENV = {
	"PR_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
	"PUSH_BEFORE_SHA": "${{ github.event.before }}",
}
ZERO_SHA = "0" * 40


def _jobs() -> dict:
	return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _fetch_body() -> str:
	jobs = _jobs()
	job_id, step_name = next(iter(SHALLOW_JOBS.items()))
	return next(step for step in jobs[job_id]["steps"] if step.get("name") == step_name)["run"]


@pytest.mark.parametrize("job_id", sorted(SHALLOW_JOBS))
def test_checkout_is_shallow(job_id: str) -> None:
	steps = _jobs()[job_id]["steps"]
	checkout = steps[0]
	assert checkout["name"] == "Checkout repository"
	assert str(checkout["uses"]).startswith("actions/checkout@")
	assert checkout["with"]["fetch-depth"] == 1
	for step in steps:
		assert str((step.get("with") or {}).get("fetch-depth", "")) != "0", f"{job_id}: {step.get('name')} clones full history"


@pytest.mark.parametrize("job_id", sorted(SHALLOW_JOBS))
def test_fetch_step_follows_checkout_before_any_base_reader(job_id: str) -> None:
	job = _jobs()[job_id]
	steps = job["steps"]
	fetch = steps[1]
	assert fetch["name"] == SHALLOW_JOBS[job_id]
	assert "if" not in fetch
	assert fetch["env"] == FETCH_STEP_ENV
	assert "${{" not in fetch["run"]
	for index, step in enumerate(steps):
		reads_base = str(step.get("name", "")).startswith("Prepare template-only PR live copies") or "CLAUDE_PARITY_PR_BASE_SHA" in (step.get("env") or {})
		if reads_base:
			assert index > 1, f"{job_id}: {step.get('name')} runs before the base fetch"


def test_fetch_bodies_are_identical_and_fetch_what_the_readers_need() -> None:
	jobs = _jobs()
	bodies = {next(step for step in jobs[job_id]["steps"] if step.get("name") == name)["run"] for job_id, name in SHALLOW_JOBS.items()}
	assert len(bodies) == 1
	body = bodies.pop()
	for fragment in (
		"set -euo pipefail",
		"--depth=200",
		"+refs/heads/main:refs/remotes/origin/main",
		'--depth=1 origin "${base}"',
		"git merge-base",
		"--unshallow",
		"::error::",
		"exit 1",
		"^[0-9a-f]{40}$",
	):
		assert fragment in body, fragment


def test_contract_is_wired_into_static_checks() -> None:
	run_text = "\n".join(step.get("run", "") for step in _jobs()["static-checks"]["steps"])
	assert "tests/test_ci_shallow_checkout_contract.py" in run_text


# --- behaviour: run the shipped shell against a local origin ---------------


def _git_env(tmp_path: Path) -> dict:
	env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "GITHUB_"))}
	for key in ("PR_BASE_SHA", "PUSH_BEFORE_SHA", "SHALLOW_FETCH_RETRY_DELAY_SECONDS", "BASH_ENV"):
		env.pop(key, None)
	config = tmp_path / "gitconfig"
	config.write_text("[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n", encoding="utf-8")
	env.update({"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"})
	return env


def _git(cwd: Path, env: dict, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=check)


def _commit(seed: Path, env: dict, path: str, text: str) -> str:
	(seed / path).parent.mkdir(parents=True, exist_ok=True)
	(seed / path).write_text(text, encoding="utf-8")
	_git(seed, env, "add", "-A")
	_git(seed, env, "commit", "-q", "-m", f"change {path}")
	return _git(seed, env, "rev-parse", "HEAD").stdout.strip()


def _empty_commits(seed: Path, env: dict, branch: str, parent: str, count: int) -> str:
	stream = []
	for index in range(count):
		stream.append(f"commit refs/heads/{branch}\ncommitter t <t@t> {1700000000 + index} +0000\ndata 3\nc{index % 10}\n")
		if index == 0:
			stream.append(f"from {parent}\n")
		stream.append("\n")
	subprocess.run(["git", "fast-import", "--quiet"], cwd=seed, env=env, input="".join(stream), text=True, check=True)
	return _git(seed, env, "rev-parse", f"refs/heads/{branch}").stdout.strip()


@pytest.fixture()
def origin(tmp_path: Path) -> dict:
	env = _git_env(tmp_path)
	seed = tmp_path / "seed"
	seed.mkdir()
	_git(seed, env, "init", "-q", "-b", "main")
	fork = _commit(seed, env, "workflow-templates/.claude/hooks/guard.py", "v1\n")
	for index in range(5):
		_commit(seed, env, f"docs/main{index}.md", "x\n")
	_git(seed, env, "checkout", "-q", "-b", "orchestrator/project-x", fork)
	for index in range(3):
		project = _commit(seed, env, f"docs/project{index}.md", "y\n")
	_git(seed, env, "checkout", "-q", "-b", "feature", project)
	feature = _commit(seed, env, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	_git(seed, env, "checkout", "-q", "-b", "pr-merge", project)
	_git(seed, env, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
	merge = _git(seed, env, "rev-parse", "HEAD").stdout.strip()
	deep_tip = _empty_commits(seed, env, "deep", fork, 250)
	_git(seed, env, "checkout", "-q", "main")
	main = _git(seed, env, "rev-parse", "HEAD").stdout.strip()
	bare = tmp_path / "origin.git"
	_git(tmp_path, env, "clone", "-q", "--bare", str(seed), str(bare))
	_git(bare, env, "config", "uploadpack.allowAnySHA1InWant", "true")
	return {"env": env, "bare": bare, "fork": fork, "project": project, "feature": feature, "merge": merge, "deep": deep_tip, "main": main}


def _checkout(tmp_path: Path, origin: dict, sha: str) -> Path:
	# Mimics actions/checkout with fetch-depth: 1.
	env = origin["env"]
	work = tmp_path / "work"
	work.mkdir()
	_git(work, env, "init", "-q")
	_git(work, env, "remote", "add", "origin", f"file://{origin['bare']}")
	_git(work, env, "fetch", "-q", "--no-tags", "--depth=1", "origin", sha)
	_git(work, env, "checkout", "-q", "--detach", sha)
	return work


def _run(work: Path, origin: dict, *, event: str, ref: str, sha: str, pr_base: str = "", before: str = "") -> subprocess.CompletedProcess[str]:
	env = dict(origin["env"])
	env.update({
		"GITHUB_EVENT_NAME": event,
		"GITHUB_REF": ref,
		"GITHUB_SHA": sha,
		"PR_BASE_SHA": pr_base,
		"PUSH_BEFORE_SHA": before,
		"SHALLOW_FETCH_RETRY_DELAY_SECONDS": "0",
	})
	return subprocess.run(["bash", "-c", _fetch_body()], cwd=work, env=env, capture_output=True, text=True, check=False)


def test_pull_request_gets_the_three_dot_diff(tmp_path: Path, origin: dict) -> None:
	work = _checkout(tmp_path, origin, origin["merge"])
	result = _run(work, origin, event="pull_request", ref="refs/pull/1/merge", sha=origin["merge"], pr_base=origin["project"])
	assert result.returncode == 0, result.stdout + result.stderr
	diff = _git(work, origin["env"], "diff", "--name-only", f"{origin['project']}...HEAD")
	assert diff.stdout.split() == ["workflow-templates/.claude/hooks/guard.py"]
	assert _git(work, origin["env"], "merge-base", "HEAD", "origin/main").stdout.strip() == origin["fork"]


def test_pull_request_with_missing_base_fails_closed(tmp_path: Path, origin: dict) -> None:
	work = _checkout(tmp_path, origin, origin["merge"])
	result = _run(work, origin, event="pull_request", ref="refs/pull/1/merge", sha=origin["merge"], pr_base="ab" * 20)
	assert result.returncode != 0
	assert "::error::Shallow checkout: the pull request base " + "ab" * 20 in result.stdout


def test_pull_request_with_malformed_base_fails_closed(tmp_path: Path, origin: dict) -> None:
	work = _checkout(tmp_path, origin, origin["merge"])
	result = _run(work, origin, event="pull_request", ref="refs/pull/1/merge", sha=origin["merge"], pr_base="--upload-pack=touch pwned")
	assert result.returncode != 0
	assert "::error::Shallow checkout: the pull request base invalid" in result.stdout
	assert not (work / "pwned").exists()


def test_branch_creation_push_resolves_merge_base_with_main(tmp_path: Path, origin: dict) -> None:
	work = _checkout(tmp_path, origin, origin["project"])
	result = _run(work, origin, event="push", ref="refs/heads/orchestrator/project-x", sha=origin["project"], before=ZERO_SHA)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "::warning::" in result.stdout
	assert _git(work, origin["env"], "merge-base", "HEAD", "origin/main").stdout.strip() == origin["fork"]


def test_force_push_with_unreachable_before_falls_back(tmp_path: Path, origin: dict) -> None:
	work = _checkout(tmp_path, origin, origin["project"])
	result = _run(work, origin, event="push", ref="refs/heads/orchestrator/project-x", sha=origin["project"], before="cd" * 20)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "::warning::Shallow checkout: push before=" + "cd" * 20 in result.stdout
	assert _git(work, origin["env"], "merge-base", "HEAD", "origin/main").stdout.strip() == origin["fork"]


def test_project_push_with_reachable_before_fetches_it(tmp_path: Path, origin: dict) -> None:
	work = _checkout(tmp_path, origin, origin["project"])
	result = _run(work, origin, event="push", ref="refs/heads/orchestrator/project-x", sha=origin["project"], before=origin["fork"])
	assert result.returncode == 0, result.stdout + result.stderr
	assert "::warning::" not in result.stdout
	assert _git(work, origin["env"], "cat-file", "-e", f"{origin['fork']}^{{commit}}", check=False).returncode == 0


def test_push_to_main_fetches_nothing(tmp_path: Path, origin: dict) -> None:
	work = _checkout(tmp_path, origin, origin["main"])
	result = _run(work, origin, event="push", ref="refs/heads/main", sha=origin["main"], before=origin["fork"])
	assert result.returncode == 0, result.stdout + result.stderr
	assert _git(work, origin["env"], "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
	assert _git(work, origin["env"], "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main", check=False).returncode != 0


def test_merge_base_deeper_than_window_unshallows(tmp_path: Path, origin: dict) -> None:
	work = _checkout(tmp_path, origin, origin["deep"])
	result = _run(work, origin, event="pull_request", ref="refs/pull/2/merge", sha=origin["deep"], pr_base=origin["fork"])
	assert result.returncode == 0, result.stdout + result.stderr
	assert "unshallowing" in result.stdout
	assert _git(work, origin["env"], "merge-base", origin["fork"], "HEAD").stdout.strip() == origin["fork"]
	assert _git(work, origin["env"], "diff", "--name-only", f"{origin['fork']}...HEAD").returncode == 0
