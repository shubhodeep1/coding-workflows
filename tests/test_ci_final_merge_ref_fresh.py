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
INTEGRATION = "orchestrator/project-8291"

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
	if ".head.sha" in jq:
		# The sync-only check's re-read of the live PR head.
		print(os.environ.get("FAKE_GH_HEAD_SHA") or os.environ.get("GITHUB_SHA", ""))
		sys.exit(0)
	if ".base.ref" in jq:
		# One field per line: base ref, default branch, changed_files.
		print(os.environ.get("FAKE_GH_BASE_REF", "main"))
		print(os.environ.get("FAKE_GH_DEFAULT_BRANCH", "main"))
		print(os.environ.get("FAKE_GH_CHANGED_FILES", "1"))
		sys.exit(0)
	path = args[1] if len(args) > 1 else ""
	source = ""
	page_slice = None
	if "/compare/" in path:
		default = os.environ.get("FAKE_GH_DEFAULT_BRANCH", "main")
		source = os.environ.get("FAKE_GH_COMPARE_DEFAULT" if path.endswith("..." + default) else "FAKE_GH_COMPARE_RANGE", "")
	elif "/pulls/7/files" in path:
		source = os.environ.get("FAKE_GH_PR_FILES", "")
		page = int(path.split("page=")[-1]) if "&page=" in path else 1
		page_slice = (100 * (page - 1), 100 * page)
	if source and os.path.isfile(source):
		with open(source, encoding="utf-8") as fh:
			body = json.load(fh)
		if page_slice is not None and isinstance(body, list):
			body = body[page_slice[0]:page_slice[1]]
		print(json.dumps(body))
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
	# An integration branch at the same tip as main (the #9602 shape).
	_git(work, "push", "-q", str(origin), f"{new_base}:refs/heads/{INTEGRATION}")

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
	assert not any("compare" in arg or "/files" in arg for call in calls for arg in call)


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


# --- Sync-only acceptance (#9688) --------------------------------------------
# The stale world: refs/pull/7/merge merges the head into old_base while the
# integration branch is at new_base. Compare and file listings are synthetic
# JSON served by the fake gh; their SHAs only have to be consistent.

MAIN_1 = "d1" * 20
MAIN_2 = "d2" * 20
SYNC_MERGE = "5a" * 20
RESOLVE = "5b" * 20
OUTSIDER = "f0" * 20


def _commit(sha: str, message: str, parents: list[str]) -> dict:
	return {"sha": sha, "parents": [{"sha": p} for p in parents], "commit": {"message": message}}


def _sync_range() -> list[dict]:
	"""The #9602 shape: main commits brought in by one sync merge."""
	return [
		_commit(MAIN_1, "fix: base fix", ["00" * 20]),
		_commit(MAIN_2, "feat: more main", [MAIN_1]),
		_commit(SYNC_MERGE, f"chore: sync main into {INTEGRATION}", ["ab" * 20, MAIN_2]),
	]


def _sync_env(
	tmp_path: Path,
	*,
	commits: list[dict] | None = None,
	files: list[dict] | None = None,
	status: str = "ahead",
	total: int | None = None,
	default_commits: list[dict] | None = None,
	pr_files: list[dict] | None = None,
	changed_files: int | None = None,
	missing: str = "",
) -> dict:
	commits = _sync_range() if commits is None else commits
	files = [{"filename": "a.txt"}] if files is None else files
	default_commits = [_commit(MAIN_1, "fix: base fix", ["00" * 20]), _commit(MAIN_2, "feat: more main", [MAIN_1])] if default_commits is None else default_commits
	pr_files = [{"filename": "b.txt"}] if pr_files is None else pr_files
	out = tmp_path / "sync-api"
	out.mkdir(exist_ok=True)
	docs = {
		"range": {"status": status, "total_commits": len(commits) if total is None else total, "commits": commits, "files": files},
		"default": {"status": "diverged", "total_commits": len(default_commits), "commits": default_commits},
		"files": pr_files,
	}
	for name, doc in docs.items():
		if name != missing:
			(out / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
	return {
		"FAKE_GH_BASE_REF": INTEGRATION,
		"FAKE_GH_COMPARE_RANGE": str(out / "range.json"),
		"FAKE_GH_COMPARE_DEFAULT": str(out / "default.json"),
		"FAKE_GH_PR_FILES": str(out / "files.json"),
		"FAKE_GH_CHANGED_FILES": str(len(pr_files) if changed_files is None else changed_files),
	}


def _api_paths(calls: list[list[str]], needle: str) -> list[str]:
	return [c[1] for c in calls if len(c) > 1 and needle in c[1]]


def test_sync_only_stale_merge_ref_is_accepted(world, tmp_path: Path) -> None:
	summary = tmp_path / "summary.md"
	env = _sync_env(tmp_path)
	env["GITHUB_STEP_SUMMARY"] = str(summary)
	result, tested, calls = world["run"](env=env)
	assert result.returncode == 0, result.stdout + result.stderr
	assert (
		f"outcome=stale_accepted reason=sync_only_no_overlap base={INTEGRATION} "
		f"tested_base={world['old_base']} tip={world['new_base']} commits=3 files=1"
	) in result.stdout
	assert "::error::" not in result.stdout
	assert tested == world["old_merge"]  # the checked-out merge is kept
	assert "outcome=stale_accepted" in summary.read_text(encoding="utf-8")
	assert len(summary.read_text(encoding="utf-8").splitlines()) == 1
	assert _api_paths(calls, "/compare/") == [
		f"repos/o/r/compare/{world['old_base']}...{world['new_base']}",
		f"repos/o/r/compare/{world['old_base']}...main",
	]
	assert _api_paths(calls, "/files") == ["repos/o/r/pulls/7/files?per_page=100&page=1"]
	# The live head is re-read once, after the file listing.
	head_reads = [i for i, c in enumerate(calls) if any(".head.sha" in arg for arg in c)]
	files_reads = [i for i, c in enumerate(calls) if len(c) > 1 and "/files" in c[1]]
	assert len(head_reads) == 1 and head_reads[0] > files_reads[-1]


def test_sync_only_head_moved_during_the_listing_fails_closed(world, tmp_path: Path) -> None:
	env = _sync_env(tmp_path)
	env["FAKE_GH_HEAD_SHA"] = "ee" * 20
	result, tested, _ = world["run"](env=env)
	assert result.returncode == 1, result.stdout + result.stderr
	assert "outcome=stale reason=not_refreshed" in result.stdout and "detail=api_error" in result.stdout
	assert "stale_accepted" not in result.stdout
	assert tested == world["old_merge"]


def test_sync_only_accepts_resolver_commits_and_pages_the_pr_files(world, tmp_path: Path) -> None:
	commits = _sync_range() + [_commit(RESOLVE, "[ai-merge-resolve] merge origin/main", [SYNC_MERGE])]
	pr_files = [{"filename": f"src/f{i}.py"} for i in range(150)]
	result, tested, calls = world["run"](env=_sync_env(tmp_path, commits=commits, pr_files=pr_files))
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=stale_accepted reason=sync_only_no_overlap" in result.stdout and "commits=4" in result.stdout
	assert _api_paths(calls, "/files") == [
		"repos/o/r/pulls/7/files?per_page=100&page=1",
		"repos/o/r/pulls/7/files?per_page=100&page=2",
	]
	assert tested == world["old_merge"]


@pytest.mark.parametrize(
	("case", "detail"),
	[
		({"files": [{"filename": "b.txt"}]}, "file_overlap"),
		({"files": [{"filename": "c.txt", "previous_filename": "b.txt"}]}, "file_overlap"),
		({"commits": _sync_range() + [_commit(OUTSIDER, "feat: pushed straight to the integration branch", [SYNC_MERGE])]}, "foreign_commit"),
		({"commits": _sync_range()[:2] + [_commit(SYNC_MERGE, "Merge branch 'main'", ["ab" * 20, MAIN_2])]}, "foreign_commit"),
		({"commits": _sync_range()[:2] + [_commit(SYNC_MERGE, f"chore: sync main into {INTEGRATION}", ["ab" * 20, OUTSIDER])]}, "foreign_commit"),
		({"status": "diverged"}, "not_ahead"),
		({"total": 251}, "truncated"),
		({"files": [{"filename": f"x{i}.txt"} for i in range(300)]}, "truncated"),
		({"changed_files": 3001}, "truncated"),
		({"changed_files": 2}, "truncated"),
		({"missing": "range"}, "api_error"),
		({"missing": "default"}, "api_error"),
		({"missing": "files"}, "api_error"),
	],
)
def test_sync_only_rejections_keep_the_stale_exit(world, tmp_path: Path, case: dict, detail: str) -> None:
	result, tested, calls = world["run"](env=_sync_env(tmp_path, **case))
	assert result.returncode == 1, result.stdout + result.stderr
	assert "::error::CI_FINAL_MERGE_REF:" in result.stdout and "not a defect in the PR" in result.stdout
	assert (
		f"outcome=stale reason=not_refreshed base={INTEGRATION} tested_base={world['old_base']} "
		f"tip={world['new_base']} detail={detail}"
	) in result.stdout
	assert "stale_accepted" not in result.stdout
	assert tested == world["old_merge"]
	if detail == "foreign_commit":
		assert _api_paths(calls, "/files") == []  # no file listing for a foreign commit
	if case.get("changed_files") == 3001:
		assert _api_paths(calls, "/files") == []


def test_sync_only_never_applies_to_a_default_branch_base(world, tmp_path: Path) -> None:
	env = _sync_env(tmp_path)
	env["FAKE_GH_BASE_REF"] = "main"
	result, tested, calls = world["run"](env=env)
	assert result.returncode == 1
	assert f"outcome=stale reason=not_refreshed base=main tested_base={world['old_base']} tip={world['new_base']} detail=foreign_commit" in result.stdout
	assert _api_paths(calls, "/compare/") == [] and _api_paths(calls, "/files") == []


def test_sync_only_unreadable_default_branch_fails_closed(world, tmp_path: Path) -> None:
	env = _sync_env(tmp_path)
	env["FAKE_GH_DEFAULT_BRANCH"] = ""
	result, _, calls = world["run"](env=env)
	assert result.returncode == 1
	assert "detail=api_error" in result.stdout
	assert _api_paths(calls, "/compare/") == []


def test_sync_only_switch_off_keeps_todays_exit(world, tmp_path: Path) -> None:
	env = _sync_env(tmp_path)
	env["CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED"] = "false"
	result, tested, calls = world["run"](env=env)
	assert result.returncode == 1
	line = next(out_line for out_line in result.stdout.splitlines() if out_line.startswith("CI_FINAL_MERGE_REF pr="))
	assert line.endswith(
		f"outcome=stale reason=not_refreshed base={INTEGRATION} tested_base={world['old_base']} tip={world['new_base']}"
	)
	assert "detail=" not in result.stdout
	assert _api_paths(calls, "/compare/") == [] and _api_paths(calls, "/files") == []
	assert tested == world["old_merge"]


def test_sync_only_tokenless_job_reads_the_public_api(world, tmp_path: Path) -> None:
	"""tests-sandbox-python passes no token: the extra reads go to the public endpoint too."""
	env = _sync_env(tmp_path)
	pull = tmp_path / "pull.json"
	pull.write_text(json.dumps({"base": {"ref": INTEGRATION, "repo": {"default_branch": "main"}}, "head": {"sha": world["head"]}, "changed_files": 1, "mergeable": True}), encoding="utf-8")
	curl_dir = tmp_path / "fake-curl"
	curl_dir.mkdir()
	# A stand-in curl: `pulls/7` comes from pull.json; every other path is
	# routed through the fake gh's compare/files answers.
	(curl_dir / "curl").write_text(textwrap.dedent(
		f'''\
		#!/usr/bin/env python3
		import subprocess, sys
		url = sys.argv[-1]
		prefix = "https://api.example.test/"
		if not url.startswith(prefix):
			sys.exit(22)
		path = url[len(prefix):]
		with open({str(tmp_path / "curl.log")!r}, "a", encoding="utf-8") as log:
			log.write(path + "\\n")
		if path == "repos/o/r/pulls/7":
			sys.stdout.write(open({str(pull)!r}, encoding="utf-8").read())
			sys.exit(0)
		sys.exit(subprocess.run(["gh", "api", path]).returncode)
		'''
	), encoding="utf-8")
	(curl_dir / "curl").chmod(0o755)
	env.update({
		"GH_TOKEN": "",
		"CI_FINAL_MERGE_REF_API_URL": "https://api.example.test",
		"PATH": f"{curl_dir}:{tmp_path / 'bin'}:{os.environ.get('PATH', '')}",
	})
	result, tested, _ = world["run"](env=env)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=stale_accepted reason=sync_only_no_overlap" in result.stdout
	paths = (tmp_path / "curl.log").read_text(encoding="utf-8").splitlines()
	assert f"repos/o/r/compare/{world['old_base']}...{world['new_base']}" in paths
	assert f"repos/o/r/compare/{world['old_base']}...main" in paths
	assert "repos/o/r/pulls/7/files?per_page=100&page=1" in paths
	assert tested == world["old_merge"]
