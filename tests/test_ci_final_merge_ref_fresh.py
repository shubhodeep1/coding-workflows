"""Final-CI merge-ref freshness check (scripts/ci_final_merge_ref_fresh.sh).

A real local origin (bare repository) carries a base branch, a PR head and
`refs/pull/7/merge`; the job checkout is a depth-1 clone of that ref, as in
ci.yml. A fake `gh` answers `pulls/7` reads and, when FAKE_GH_REFRESH_TO is
set, moves the origin's merge ref on the first `mergeable` read, standing in
for GitHub's recompute.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ci_final_merge_ref_fresh.sh"

FAKE_GH = textwrap.dedent(
	r'''
	#!/usr/bin/env python3
	import json, os, subprocess, sys
	args = sys.argv[1:]
	with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
		log.write(json.dumps(args) + "\n")
	jq = args[args.index("--jq") + 1] if "--jq" in args else ""
	if os.environ.get("FAKE_GH_FAIL") == "1":
		sys.exit(1)
	if ".mergeable" in jq:
		target = os.environ.get("FAKE_GH_REFRESH_TO", "")
		if target:
			subprocess.run(["git", "-C", os.environ["FAKE_ORIGIN"], "update-ref", "refs/pull/7/merge", target], check=True)
		print(os.environ.get("FAKE_GH_MERGEABLE", "true"))
		sys.exit(0)
	if ".base.ref" in jq:
		print(os.environ.get("FAKE_GH_BASE_REF", "main"))
		sys.exit(0)
	sys.exit(1)
	'''
).lstrip()


def _git(cwd: Path, *args: str) -> str:
	env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
	return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def world(tmp_path: Path):
	origin = tmp_path / "origin.git"
	work = tmp_path / "work"
	_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
	_git(tmp_path, "init", "-q", "-b", "main", str(work))
	(work / "a.txt").write_text("1\n", encoding="utf-8")
	_git(work, "add", "a.txt")
	_git(work, "commit", "-q", "-m", "base 1")
	old_base = _git(work, "rev-parse", "HEAD")
	_git(work, "checkout", "-q", "-b", "head")
	(work / "b.txt").write_text("pr\n", encoding="utf-8")
	_git(work, "add", "b.txt")
	_git(work, "commit", "-q", "-m", "pr")
	head = _git(work, "rev-parse", "HEAD")
	_git(work, "checkout", "-q", "main")
	_git(work, "merge", "-q", "--no-ff", "-m", "old merge", head)
	old_merge = _git(work, "rev-parse", "HEAD")
	_git(work, "reset", "-q", "--hard", old_base)
	(work / "a.txt").write_text("2\n", encoding="utf-8")
	_git(work, "commit", "-q", "-am", "base 2 (the fix)")
	new_base = _git(work, "rev-parse", "HEAD")
	_git(work, "merge", "-q", "--no-ff", "-m", "new merge", head)
	new_merge = _git(work, "rev-parse", "HEAD")
	_git(work, "reset", "-q", "--hard", new_base)
	_git(work, "push", "-q", str(origin), "main", "head")
	_git(work, "push", "-q", str(origin), f"{old_merge}:refs/pull/7/merge", f"{new_merge}:refs/heads/keep-new-merge")

	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	log = tmp_path / "gh.log"

	def run(*, checkout: str = "refs/pull/7/merge", env: dict | None = None, args: tuple[str, ...] = ()):
		job = tmp_path / f"job-{len(list(tmp_path.glob('job-*')))}"
		_git(tmp_path, "init", "-q", str(job))
		_git(job, "remote", "add", "origin", f"file://{origin}")
		_git(job, "fetch", "-q", "--depth=1", "origin", f"+{checkout}:refs/remotes/checked-out")
		_git(job, "checkout", "-q", "--detach", "refs/remotes/checked-out")
		log.write_text("", encoding="utf-8")
		full_env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "CI_FINAL", "FINAL_CI"))}
		full_env.update({
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"FAKE_GH_LOG": str(log),
			"FAKE_ORIGIN": str(origin),
			"FINAL_CI_PR_NUMBER": "7",
			"GITHUB_SHA": head,
			"GITHUB_REPOSITORY": "o/r",
			"GH_TOKEN": "dummy",
			# Never reach the real API: a token-less read goes to a missing file.
			"CI_FINAL_MERGE_REF_API_URL": f"file://{tmp_path}/no-api",
			"CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS": "2",
			"CI_FINAL_MERGE_REF_REFRESH_SECONDS": "1",
		})
		full_env.update(env or {})
		result = subprocess.run(["bash", str(SCRIPT), *args], cwd=job, env=full_env, capture_output=True, text=True)
		calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
		return result, _git(job, "rev-parse", "HEAD"), calls

	return {"run": run, "head": head, "old_base": old_base, "new_base": new_base, "old_merge": old_merge, "new_merge": new_merge, "origin": origin}


def test_current_merge_ref_passes_without_refresh(world) -> None:
	_git(world["origin"], "update-ref", "refs/pull/7/merge", world["new_merge"])
	result, tested, calls = world["run"]()
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"outcome=fresh reason=base_tip base=main tested_base={world['new_base']}" in result.stdout
	assert tested == world["new_merge"]
	assert len(calls) == 1  # the base-branch read only


def test_stale_merge_ref_is_refreshed_and_checked_out(world) -> None:
	result, tested, calls = world["run"](env={"FAKE_GH_REFRESH_TO": world["new_merge"]})
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"outcome=refreshed reason=base_tip base=main tested_base={world['new_base']} attempt=1" in result.stdout
	assert tested == world["new_merge"]


def test_merge_ref_that_stays_stale_fails_the_job(world) -> None:
	result, tested, calls = world["run"]()
	assert result.returncode == 1
	assert "::error::CI_FINAL_MERGE_REF:" in result.stdout and "not a defect in the PR" in result.stdout
	assert f"outcome=stale reason=not_refreshed base=main tested_base={world['old_base']} tip={world['new_base']}" in result.stdout
	assert tested == world["old_merge"]
	assert sum(1 for c in calls if ".mergeable | tostring" in c) == 2


def test_conflict_fails_at_once(world) -> None:
	result, tested, calls = world["run"](env={"FAKE_GH_MERGEABLE": "false"})
	assert result.returncode == 1
	assert "outcome=conflict reason=not_mergeable" in result.stdout
	assert sum(1 for c in calls if ".mergeable | tostring" in c) == 1


@pytest.mark.parametrize(
	("env", "reason"),
	[
		({"FINAL_CI_PR_NUMBER": ""}, "reason=not_final_ci"),
		({"CI_FINAL_MERGE_REF_FRESH_ENABLED": "false"}, "reason=disabled"),
		({"GITHUB_SHA": "0" * 40}, "reason=not_a_merge_of_the_head"),
		({"FAKE_GH_FAIL": "1"}, "reason=base_ref_unreadable"),
		({"FAKE_GH_BASE_REF": "no-such-branch"}, "reason=tip_unreadable"),
	],
)
def test_fail_open_paths_keep_the_checkout(world, env: dict, reason: str) -> None:
	result, tested, _ = world["run"](env=env)
	assert result.returncode == 0, result.stdout + result.stderr
	assert reason in result.stdout
	assert tested == world["old_merge"]


def test_unfetchable_merge_ref_fails_open(world, tmp_path: Path) -> None:
	"""A checkout that cannot re-fetch (private repo, no persisted credentials) tests what it has."""
	hooks = tmp_path / "no-fetch"
	hooks.mkdir()
	(hooks / "git").write_text('#!/bin/sh\nif [ "$1" = fetch ]; then exit 128; fi\nexec /usr/bin/git "$@"\n', encoding="utf-8")
	(hooks / "git").chmod(0o755)
	result, tested, _ = world["run"](env={"PATH": f"{hooks}:{tmp_path / 'bin'}:{os.environ.get('PATH', '')}"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=skipped reason=fetch_unavailable" in result.stdout
	assert tested == world["old_merge"]


def test_tokenless_job_reads_the_public_api(world, tmp_path: Path) -> None:
	"""tests-sandbox-python passes no token; the PR comes from the public REST endpoint."""
	api = tmp_path / "api" / "repos" / "o" / "r" / "pulls"
	api.mkdir(parents=True)
	(api / "7").write_text(json.dumps({"base": {"ref": "main"}, "mergeable": True}), encoding="utf-8")
	_git(world["origin"], "update-ref", "refs/pull/7/merge", world["new_merge"])
	result, tested, calls = world["run"](env={"GH_TOKEN": "", "CI_FINAL_MERGE_REF_API_URL": f"file://{tmp_path / 'api'}"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=fresh reason=base_tip base=main" in result.stdout
	assert calls == []  # gh is never called without a token


def test_sandbox_job_step_passes_no_token() -> None:
	ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
	step = next(s for s in ci["jobs"]["tests-sandbox-python"]["steps"] if s.get("name") == "Verify the final-CI merge ref is current")
	assert "GH_TOKEN" not in step["env"] and "github.token" not in repr(step["env"])


def test_deeper_checkout_keeps_its_depth(world) -> None:
	result, tested, _ = world["run"](env={"FAKE_GH_REFRESH_TO": world["new_merge"]}, args=("--depth", "2"))
	assert result.returncode == 0, result.stdout + result.stderr
	assert tested == world["new_merge"]


def test_every_final_ci_checkout_is_followed_by_the_check() -> None:
	ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
	seen = 0
	for name, job in ci["jobs"].items():
		steps = job.get("steps") or []
		for i, step in enumerate(steps):
			ref = str((step.get("with") or {}).get("ref", ""))
			if "refs/pull/{0}/merge" not in ref:
				continue
			seen += 1
			check = steps[i + 1]
			assert check.get("name") == "Verify the final-CI merge ref is current", (name, i)
			assert check["run"].startswith("bash scripts/ci_final_merge_ref_fresh.sh --depth ")
			depth = str((step.get("with") or {}).get("fetch-depth", 1))
			assert check["run"].endswith(f"--depth {depth}"), (name, check["run"], depth)
			assert check.get("if") == (step.get("if") or "github.event_name == 'workflow_dispatch'"), name
			assert "inputs.pr_number" in check["env"]["FINAL_CI_PR_NUMBER"]
			assert check["env"]["CI_FINAL_MERGE_REF_FRESH_ENABLED"] == "${{ vars.CI_FINAL_MERGE_REF_FRESH_ENABLED || 'true' }}"
	assert seen == 10
