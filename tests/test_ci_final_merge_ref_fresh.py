"""Final-CI merge-ref freshness check (scripts/ci_final_merge_ref_fresh.sh).

A real local origin (bare repository) carries a base branch, a PR head and
`refs/pull/7/merge`; the job checkout is a depth-1 clone of that ref, as in
ci.yml. A fake `gh` answers `pulls/7` reads and, when FAKE_GH_REFRESH_TO is
set, moves the origin's merge ref on the first `mergeable` read, standing in
for GitHub's recompute. It answers `actions/runs/<id>` with
FAKE_GH_CREATED_AT, the run's creation time the script pins the base to.
The sync-only check's `compare/...` and `pulls/7/files?...` reads are answered
from JSON fixtures under FAKE_GH_FIXTURES (see `_fixture_name`), filtered with
the real `jq`; a missing fixture is an API error.

Commit dates are fixed: base 1 at 00:00, the PR head at 00:30, base 2 (the
fix) at 02:00 on 2026-01-01. The default run was created at 03:00, so its
pinned base is base 2, the live tip.
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
	path = args[1] if len(args) > 1 else ""
	if "/compare/" in path or "/pulls/7/files" in path:
		name = path.split("repos/o/r/", 1)[-1].replace("/", "_").replace("?", "_").replace("&", "_") + ".json"
		fixture = os.path.join(os.environ.get("FAKE_GH_FIXTURES", "/nonexistent"), name)
		if not os.path.exists(fixture):
			sys.exit(1)
		move_ref, move_to = os.environ.get("FAKE_GH_FILES_MOVE_REF", ""), os.environ.get("FAKE_GH_FILES_MOVE_TO", "")
		if "/pulls/7/files" in path and move_ref and move_to:
			# The base moves while the sync-only check reads the PR's files.
			subprocess.run(["git", "-C", os.environ["FAKE_ORIGIN"], "update-ref", move_ref, move_to], check=True)
		out = subprocess.run(["jq", "-r", jq, fixture], capture_output=True, text=True)
		sys.stdout.write(out.stdout)
		sys.exit(out.returncode)
	if ".created_at" in jq:
		created = os.environ.get("FAKE_GH_CREATED_AT", "")
		if not created:
			sys.exit(1)
		print(created)
		sys.exit(0)
	if ".mergeable" in jq:
		target = os.environ.get("FAKE_GH_REFRESH_TO", "")
		if target:
			subprocess.run(["git", "-C", os.environ["FAKE_ORIGIN"], "update-ref", "refs/pull/7/merge", target], check=True)
		move_main = os.environ.get("FAKE_GH_MOVE_MAIN_TO", "")
		if move_main:
			subprocess.run(["git", "-C", os.environ["FAKE_ORIGIN"], "update-ref", "refs/heads/main", move_main], check=True)
		if os.environ.get("FAKE_GH_DELETE_MAIN") == "1":
			# The tip re-read (`git ls-remote`) then finds no branch.
			subprocess.run(["git", "-C", os.environ["FAKE_ORIGIN"], "update-ref", "-d", "refs/heads/main"], check=True)
		seq = os.environ.get("FAKE_GH_MERGEABLE_SEQ", "")
		if seq:
			counter = os.environ["FAKE_GH_LOG"] + ".mergeable"
			n = int(open(counter).read()) if os.path.exists(counter) else 0
			open(counter, "w").write(str(n + 1))
			answers = seq.split(",")
			print(answers[min(n, len(answers) - 1)])
			sys.exit(0)
		print(os.environ.get("FAKE_GH_MERGEABLE", "true"))
		sys.exit(0)
	if ".changed_files" in jq:
		print(os.environ.get("FAKE_GH_BASE_REF", "main") + "\t" + os.environ.get("FAKE_GH_CHANGED_FILES", "1"))
		sys.exit(0)
	if ".base.ref" in jq:
		print(os.environ.get("FAKE_GH_BASE_REF", "main"))
		sys.exit(0)
	sys.exit(1)
	'''
).lstrip()

DAY = "2026-01-01T"


def _git(cwd: Path, *args: str, date: str = "") -> str:
	env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
	if date:
		env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{DAY}{date}+0000"
	return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def world(tmp_path: Path):
	origin = tmp_path / "origin.git"
	work = tmp_path / "work"
	_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
	_git(tmp_path, "init", "-q", "-b", "main", str(work))
	(work / "a.txt").write_text("1\n", encoding="utf-8")
	_git(work, "add", "a.txt")
	_git(work, "commit", "-q", "-m", "base 1", date="00:00:00")
	old_base = _git(work, "rev-parse", "HEAD")
	_git(work, "checkout", "-q", "-b", "head")
	(work / "b.txt").write_text("pr\n", encoding="utf-8")
	_git(work, "add", "b.txt")
	_git(work, "commit", "-q", "-m", "pr", date="00:30:00")
	head = _git(work, "rev-parse", "HEAD")
	# A second head that edits the line base 2 changes: merges cleanly into
	# base 1, conflicts with base 2.
	_git(work, "checkout", "-q", "-b", "conflicting", old_base)
	(work / "a.txt").write_text("pr\n", encoding="utf-8")
	_git(work, "commit", "-q", "-am", "conflicting pr", date="00:40:00")
	conflicting = _git(work, "rev-parse", "HEAD")
	_git(work, "checkout", "-q", "--detach", old_base)
	_git(work, "merge", "-q", "--no-ff", "-m", "conflicting old merge", conflicting, date="00:41:00")
	conflicting_old_merge = _git(work, "rev-parse", "HEAD")
	_git(work, "checkout", "-q", "main")
	_git(work, "merge", "-q", "--no-ff", "-m", "old merge", head, date="00:31:00")
	old_merge = _git(work, "rev-parse", "HEAD")
	_git(work, "reset", "-q", "--hard", old_base)
	(work / "a.txt").write_text("2\n", encoding="utf-8")
	_git(work, "commit", "-q", "-am", "base 2 (the fix)", date="02:00:00")
	new_base = _git(work, "rev-parse", "HEAD")
	_git(work, "merge", "-q", "--no-ff", "-m", "new merge", head, date="02:01:00")
	new_merge = _git(work, "rev-parse", "HEAD")
	_git(work, "reset", "-q", "--hard", new_base)
	# Base 3 lands at 05:00, after the default run was created; tests push it.
	(work / "c.txt").write_text("3\n", encoding="utf-8")
	_git(work, "add", "c.txt")
	_git(work, "commit", "-q", "-m", "base 3 (after the run was created)", date="05:00:00")
	later_base = _git(work, "rev-parse", "HEAD")
	_git(work, "merge", "-q", "--no-ff", "-m", "later merge", head, date="05:01:00")
	later_merge = _git(work, "rev-parse", "HEAD")
	_git(work, "reset", "-q", "--hard", new_base)
	_git(work, "push", "-q", str(origin), "main", "head", "conflicting")
	_git(
		work, "push", "-q", str(origin),
		f"{old_merge}:refs/pull/7/merge",
		f"{new_merge}:refs/heads/keep-new-merge",
		f"{later_merge}:refs/heads/keep-later-merge",
		f"{conflicting_old_merge}:refs/heads/keep-conflicting-merge",
	)

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
		github_env = job.parent / f"{job.name}.env"
		github_env.write_text("", encoding="utf-8")
		full_env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "CI_FINAL", "FINAL_CI", "GIT_"))}
		full_env.update({
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"FAKE_GH_LOG": str(log),
			"FAKE_ORIGIN": str(origin),
			"FAKE_GH_CREATED_AT": f"{DAY}03:00:00Z",
			"FINAL_CI_PR_NUMBER": "7",
			"GITHUB_SHA": head,
			"GITHUB_REPOSITORY": "o/r",
			"GITHUB_RUN_ID": "42",
			"GITHUB_ENV": str(github_env),
			"GH_TOKEN": "dummy",
			# Never reach the real API: a token-less read goes to a missing file.
			"CI_FINAL_MERGE_REF_API_URL": f"file://{tmp_path}/no-api",
			"CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS": "2",
			"CI_FINAL_MERGE_REF_REFRESH_SECONDS": "1",
		})
		full_env.update(env or {})
		result = subprocess.run(["bash", str(SCRIPT), *args], cwd=job, env=full_env, capture_output=True, text=True)
		calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
		result.job = job  # type: ignore[attr-defined]
		result.exported = github_env.read_text(encoding="utf-8").splitlines()  # type: ignore[attr-defined]
		return result, _git(job, "rev-parse", "HEAD"), calls

	return {
		"run": run, "head": head, "old_base": old_base, "new_base": new_base, "old_merge": old_merge, "new_merge": new_merge,
		"later_base": later_base, "later_merge": later_merge, "conflicting": conflicting,
		"conflicting_old_merge": conflicting_old_merge, "origin": origin, "work": work,
	}


def _tree(repo: Path, rev: str) -> str:
	return _git(repo, "rev-parse", f"{rev}^{{tree}}")


def _assert_local_merge_of(result, tested: str, world: dict, base: str, merge_with_same_tree: str) -> None:
	job = result.job
	assert _git(job, "rev-parse", f"{tested}^1") == base
	assert _git(job, "rev-parse", f"{tested}^2") == world["head"]
	assert _tree(job, tested) == _tree(world["work"], merge_with_same_tree)
	assert _git(job, "status", "--porcelain") == ""
	assert f"CI_FINAL_MERGE_REF_LOCAL_MERGE={tested}" in result.exported
	assert f"merge={tested} tree={_tree(job, tested)}" in result.stdout


def test_current_merge_ref_passes_without_refresh(world) -> None:
	_git(world["origin"], "update-ref", "refs/pull/7/merge", world["new_merge"])
	result, tested, calls = world["run"]()
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"outcome=fresh reason=base_tip base=main tested_base={world['new_base']}" in result.stdout
	assert f"pin={world['new_base']}" in result.stdout
	assert tested == world["new_merge"]
	assert len(calls) == 2  # the base-branch read and the run read
	assert result.exported == []


def test_stale_merge_ref_is_refreshed_and_checked_out(world) -> None:
	result, tested, calls = world["run"](env={"FAKE_GH_REFRESH_TO": world["new_merge"]})
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"outcome=refreshed reason=base_tip base=main tested_base={world['new_base']} attempt=1" in result.stdout
	assert tested == world["new_merge"]


def test_merge_ref_that_stays_stale_is_merged_locally(world) -> None:
	result, tested, calls = world["run"]()
	assert result.returncode == 0, result.stdout + result.stderr
	assert "::error::" not in result.stdout
	assert f"outcome=merged_locally reason=not_refreshed base=main tested_base={world['new_base']} tip={world['new_base']}" in result.stdout
	assert tested not in (world["old_merge"], world["new_merge"])
	_assert_local_merge_of(result, tested, world, world["new_base"], world["new_merge"])
	assert sum(1 for c in calls if ".mergeable | tostring" in c) == 2


def test_every_job_builds_the_same_local_merge(world) -> None:
	first, tested_first, _ = world["run"]()
	second, tested_second, _ = world["run"](args=("--depth", "3"))
	assert first.returncode == 0 and second.returncode == 0, first.stdout + second.stdout
	assert tested_first == tested_second


def test_conflict_fails_at_once(world) -> None:
	result, tested, calls = world["run"](env={"FAKE_GH_MERGEABLE": "false"})
	assert result.returncode == 1
	assert "outcome=conflict reason=not_mergeable" in result.stdout
	assert sum(1 for c in calls if ".mergeable | tostring" in c) == 1


def test_local_merge_conflict_fails_the_job(world) -> None:
	"""GitHub still says mergeable (stale answer); the local merge finds the conflict."""
	_git(world["origin"], "update-ref", "refs/pull/7/merge", world["conflicting_old_merge"])
	result, tested, _ = world["run"](env={"GITHUB_SHA": world["conflicting"]})
	assert result.returncode == 1
	assert "::error::CI_FINAL_MERGE_REF: PR #7" in result.stdout and "does not merge cleanly" in result.stdout
	assert f"outcome=conflict reason=merge_conflict base=main tested_base={world['new_base']} tip={world['new_base']}" in result.stdout
	assert tested == world["conflicting_old_merge"]
	assert result.exported == []


def test_merge_driver_in_gitattributes_cannot_hide_a_conflict(world, tmp_path: Path) -> None:
	"""A union driver would merge both sides of a.txt; the local merge ignores attributes and fails."""
	clone = tmp_path / "attrs"
	_git(tmp_path, "clone", "-q", str(world["origin"]), str(clone))
	_git(clone, "checkout", "-q", "--detach", world["conflicting"])
	(clone / ".gitattributes").write_text("a.txt merge=union\n", encoding="utf-8")
	_git(clone, "add", ".gitattributes")
	_git(clone, "commit", "-q", "-m", "union driver", date="00:50:00")
	union_head = _git(clone, "rev-parse", "HEAD")
	_git(clone, "checkout", "-q", "--detach", world["old_base"])
	_git(clone, "merge", "-q", "--no-ff", "-m", "union old merge", union_head, date="00:51:00")
	union_old_merge = _git(clone, "rev-parse", "HEAD")
	_git(clone, "push", "-q", "origin", f"{union_head}:refs/heads/union", f"+{union_old_merge}:refs/pull/7/merge")
	result, tested, _ = world["run"](env={"GITHUB_SHA": union_head})
	assert result.returncode == 1, result.stdout + result.stderr
	assert "outcome=conflict reason=merge_conflict" in result.stdout
	assert tested == union_old_merge


def test_github_conflict_past_the_pin_defers_to_the_local_merge(world) -> None:
	"""main moves to base 3 during the attempts and GitHub judges that tip; the merge into the pin is clean."""
	result, tested, calls = world["run"](env={"FAKE_GH_MOVE_MAIN_TO": world["later_base"], "FAKE_GH_MERGEABLE_SEQ": "true,false"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "past this run's base" in result.stdout
	assert f"outcome=merged_locally reason=not_refreshed base=main tested_base={world['new_base']} tip={world['later_base']}" in result.stdout
	_assert_local_merge_of(result, tested, world, world["new_base"], world["new_merge"])
	assert sum(1 for c in calls if ".mergeable | tostring" in c) == 2


def test_github_conflict_on_the_first_attempt_after_the_tip_moved_defers_to_the_local_merge(world) -> None:
	"""main moves past the pin between the pre-loop tip read and GitHub's first answer: the tip is re-read before judging."""
	result, tested, calls = world["run"](env={"FAKE_GH_MOVE_MAIN_TO": world["later_base"], "FAKE_GH_MERGEABLE": "false"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=conflict reason=not_mergeable" not in result.stdout
	assert "past this run's base" in result.stdout and "did not recompute" not in result.stdout
	assert f"outcome=merged_locally reason=not_refreshed base=main tested_base={world['new_base']} tip={world['later_base']}" in result.stdout
	_assert_local_merge_of(result, tested, world, world["new_base"], world["new_merge"])
	assert sum(1 for c in calls if ".mergeable | tostring" in c) == 1


def test_github_conflict_with_an_unreadable_tip_defers_to_the_local_merge(world) -> None:
	"""GitHub says not mergeable and the tip re-read fails: the local merge into the pin decides, not a not_mergeable failure."""
	result, tested, calls = world["run"](env={"FAKE_GH_DELETE_MAIN": "1", "FAKE_GH_MERGEABLE": "false"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=conflict reason=not_mergeable" not in result.stdout
	assert "could not be re-read" in result.stdout and "did not recompute" not in result.stdout
	assert f"outcome=merged_locally reason=not_refreshed base=main tested_base={world['new_base']} tip={world['new_base']}" in result.stdout
	_assert_local_merge_of(result, tested, world, world["new_base"], world["new_merge"])
	assert sum(1 for c in calls if ".mergeable | tostring" in c) == 1


def test_event_payload_pushed_at_pins_without_an_api_call(world, tmp_path: Path) -> None:
	_git(world["origin"], "update-ref", "refs/heads/main", world["later_base"])
	for value in (f"{DAY}03:00:00Z", 1767236400):  # 03:00 UTC as ISO and as a push-event epoch
		event = tmp_path / "event.json"
		event.write_text(json.dumps({"repository": {"pushed_at": value}}), encoding="utf-8")
		result, tested, calls = world["run"](env={"GITHUB_EVENT_PATH": str(event), "FAKE_GH_CREATED_AT": ""})
		assert result.returncode == 0, result.stdout + result.stderr
		assert f"pin={world['new_base']} pin_source=event_pushed_at" in result.stdout
		assert not any(".created_at" in " ".join(c) for c in calls)
		_assert_local_merge_of(result, tested, world, world["new_base"], world["new_merge"])


def test_head_that_moved_during_the_refresh_fails_open(world) -> None:
	"""The refreshed merge ref merges another head: the run is superseded, as before."""
	result, tested, _ = world["run"](env={"FAKE_GH_REFRESH_TO": world["conflicting_old_merge"]})
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"outcome=skipped reason=head_moved base=main merged_head={world['conflicting']}" in result.stdout
	assert tested == world["old_merge"]
	assert result.exported == []


def test_base_that_moved_after_the_run_was_created_stays_pinned(world) -> None:
	"""GitHub refreshed to base 3, pushed after the run was created: the job tests base 2, like its earlier jobs."""
	_git(world["origin"], "update-ref", "refs/heads/main", world["later_base"])
	_git(world["origin"], "update-ref", "refs/pull/7/merge", world["later_merge"])
	result, tested, calls = world["run"]()
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"outcome=merged_locally reason=pinned_base base=main tested_base={world['new_base']} tip={world['later_base']}" in result.stdout
	assert f"pin={world['new_base']}" in result.stdout
	_assert_local_merge_of(result, tested, world, world["new_base"], world["new_merge"])
	assert not any(".mergeable | tostring" in c for c in calls)  # GitHub's ref can never match the pin


def test_pinned_merge_ref_is_fresh_after_the_base_moved(world) -> None:
	_git(world["origin"], "update-ref", "refs/heads/main", world["later_base"])
	_git(world["origin"], "update-ref", "refs/pull/7/merge", world["new_merge"])
	result, tested, _ = world["run"]()
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"outcome=fresh reason=pinned_base base=main tested_base={world['new_base']} tip={world['later_base']}" in result.stdout
	assert tested == world["new_merge"]


def test_jobs_before_and_after_a_base_move_test_the_same_merge(world) -> None:
	early, tested_early, _ = world["run"]()
	_git(world["origin"], "update-ref", "refs/heads/main", world["later_base"])
	late, tested_late, _ = world["run"]()
	assert early.returncode == 0 and late.returncode == 0, early.stdout + late.stdout
	assert tested_early == tested_late


def test_run_created_unreadable_falls_back_to_the_live_tip(world) -> None:
	_git(world["origin"], "update-ref", "refs/heads/main", world["later_base"])
	result, tested, _ = world["run"](env={"FAKE_GH_CREATED_AT": ""})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "pin=none pin_reason=run_created_unreadable" in result.stdout
	assert f"outcome=merged_locally reason=not_refreshed base=main tested_base={world['later_base']}" in result.stdout
	_assert_local_merge_of(result, tested, world, world["later_base"], world["later_merge"])


def test_pin_beyond_the_merge_depth_falls_back_to_the_live_tip(world) -> None:
	"""The run predates every commit in the fetched window of the base."""
	_git(world["origin"], "update-ref", "refs/heads/main", world["later_base"])
	result, tested, _ = world["run"](env={"FAKE_GH_CREATED_AT": "2025-12-31T00:00:00Z", "CI_FINAL_MERGE_REF_MERGE_DEPTH": "1"})
	assert "pin=none pin_reason=beyond_depth" in result.stdout


def test_merge_base_deeper_than_the_fetch_fails_as_stale(world) -> None:
	result, tested, _ = world["run"](env={"CI_FINAL_MERGE_REF_MERGE_DEPTH": "1"})
	assert result.returncode == 1
	assert "::error::CI_FINAL_MERGE_REF:" in result.stdout and "not a defect in the PR" in result.stdout
	assert f"outcome=stale reason=merge_base_too_deep base=main tested_base={world['old_base']} tip={world['new_base']}" in result.stdout
	assert tested == world["old_merge"]
	assert result.exported == []


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
	"""tests-sandbox-python passes no token; the PR and the run come from the public REST endpoint."""
	api = tmp_path / "api" / "repos" / "o" / "r"
	(api / "pulls").mkdir(parents=True)
	(api / "actions" / "runs").mkdir(parents=True)
	(api / "pulls" / "7").write_text(json.dumps({"base": {"ref": "main"}, "mergeable": True}), encoding="utf-8")
	(api / "actions" / "runs" / "42").write_text(json.dumps({"created_at": f"{DAY}03:00:00Z"}), encoding="utf-8")
	_git(world["origin"], "update-ref", "refs/pull/7/merge", world["new_merge"])
	result, tested, calls = world["run"](env={"GH_TOKEN": "", "CI_FINAL_MERGE_REF_API_URL": f"file://{tmp_path / 'api'}"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=fresh reason=base_tip base=main" in result.stdout
	assert f"pin={world['new_base']}" in result.stdout
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
			assert check["env"]["CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED"] == (
				"${{ vars.CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED || 'true' }}"
			), name
	# Issue #10156 added tests-main-delta (push-only; its check never runs).
	assert seen == 11


def test_steps_that_fetch_the_merge_commit_fetch_a_local_merges_parents() -> None:
	"""origin does not have a locally built merge commit, so a fetch of HEAD would fail."""
	ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
	bodies = [
		step["run"]
		for job in ci["jobs"].values()
		for step in job.get("steps") or []
		if 'origin "${head_commit}"' in str(step.get("run", "")) or '"${head_fetch[@]}"' in str(step.get("run", ""))
	]
	assert len(bodies) == 4
	for body in bodies:
		assert 'origin "${head_commit}"' not in body
		assert "CI_FINAL_MERGE_REF_LOCAL_MERGE" in body


# --- Sync-only acceptance at the stale exit (issue #10177, Phase 10 / D10) ---
#
# Integration world on top of `world`: `orchestrator/project-1` at I0 (02:10),
# the PR head H from I0 touching p.txt (02:20), `refs/pull/7/merge` =
# merge(I0, H). The default branch gets M1 touching m.txt (02:30), and the
# integration tip S is the sync merge (I0, M1) (02:40). The run was created at
# 03:00, so the pin is S. CI_FINAL_MERGE_REF_MERGE_DEPTH=1 makes the local
# merge fail as merge_base_too_deep, which reaches the stale exit.

INTEGRATION = "orchestrator/project-1"


def _fixture_name(endpoint: str) -> str:
	return endpoint.replace("/", "_").replace("?", "_").replace("&", "_") + ".json"


def _commit(sha: str, parents: list[str], message: str) -> dict:
	return {"sha": sha, "parents": [{"sha": p} for p in parents], "commit": {"message": message}}


@pytest.fixture()
def integration(world, tmp_path: Path):
	def build(default: str = "main") -> dict:
		work, origin = world["work"], world["origin"]
		_git(work, "checkout", "-q", "--detach", world["new_base"])
		(work / "i.txt").write_text("i\n", encoding="utf-8")
		_git(work, "add", "i.txt")
		_git(work, "commit", "-q", "-m", "integration start", date="02:10:00")
		i0 = _git(work, "rev-parse", "HEAD")
		(work / "p.txt").write_text("pr\n", encoding="utf-8")
		_git(work, "add", "p.txt")
		_git(work, "commit", "-q", "-m", "integration pr", date="02:20:00")
		pr_head = _git(work, "rev-parse", "HEAD")
		_git(work, "checkout", "-q", "--detach", i0)
		_git(work, "merge", "-q", "--no-ff", "-m", "integration merge ref", pr_head, date="02:21:00")
		merge_ref = _git(work, "rev-parse", "HEAD")
		_git(work, "checkout", "-q", "--detach", world["new_base"])
		(work / "m.txt").write_text("m\n", encoding="utf-8")
		_git(work, "add", "m.txt")
		_git(work, "commit", "-q", "-m", "main 1", date="02:30:00")
		m1 = _git(work, "rev-parse", "HEAD")
		# A merge of the PR head into M1: a refreshed ref whose first parent is not the pin.
		_git(work, "merge", "-q", "--no-ff", "-m", "merge into m1", pr_head, date="02:35:00")
		m1_merge = _git(work, "rev-parse", "HEAD")
		_git(work, "checkout", "-q", "--detach", i0)
		_git(work, "merge", "-q", "--no-ff", "-m", f"chore: sync {default} into {INTEGRATION}", m1, date="02:40:00")
		sync = _git(work, "rev-parse", "HEAD")
		_git(work, "checkout", "-q", "main")
		_git(
			work, "push", "-q", "--force", str(origin),
			f"{m1}:refs/heads/{default}",
			f"{sync}:refs/heads/{INTEGRATION}",
			f"{pr_head}:refs/heads/integration-pr",
			f"{m1_merge}:refs/heads/keep-m1-merge",
			f"+{merge_ref}:refs/pull/7/merge",
		)
		if default != "main":
			_git(origin, "symbolic-ref", "HEAD", f"refs/heads/{default}")
		fixtures = tmp_path / "fixtures"
		fixtures.mkdir(exist_ok=True)
		summary = tmp_path / "step_summary.md"
		summary.write_text("", encoding="utf-8")
		state = {
			"default": default, "i0": i0, "pr_head": pr_head, "merge_ref": merge_ref, "m1": m1, "m1_merge": m1_merge,
			"sync": sync, "fixtures": fixtures, "summary": summary,
		}

		def fixture(endpoint: str, payload) -> None:
			(fixtures / _fixture_name(endpoint)).write_text(json.dumps(payload), encoding="utf-8")

		def standard(*, range_commits=None, range_files=None, default_commits=None, pr_files=None, status="ahead"):
			commits = range_commits if range_commits is not None else [
				_commit(m1, [world["new_base"]], "main 1"),
				_commit(sync, [i0, m1], f"chore: sync {default} into {INTEGRATION}\n\nbody"),
			]
			fixture(f"compare/{i0}...{sync}", {
				"status": status, "total_commits": len(commits), "commits": commits,
				"files": range_files if range_files is not None else [{"filename": "m.txt", "status": "added"}],
			})
			dflt = default_commits if default_commits is not None else [_commit(m1, [world["new_base"]], "main 1")]
			fixture(f"compare/{i0}...{default}", {"status": "diverged", "total_commits": len(dflt), "commits": dflt})
			fixture("pulls/7/files?per_page=100&page=1", pr_files if pr_files is not None else [{"filename": "p.txt", "status": "added"}])

		def run(env: dict | None = None):
			full = {
				"FAKE_GH_BASE_REF": INTEGRATION, "FAKE_GH_FIXTURES": str(fixtures), "FAKE_GH_CHANGED_FILES": "1",
				"GITHUB_SHA": pr_head, "CI_FINAL_MERGE_REF_MERGE_DEPTH": "1", "GITHUB_STEP_SUMMARY": str(summary),
			}
			full.update(env or {})
			return world["run"](env=full)

		state.update({"fixture": fixture, "standard": standard, "run": run})
		return state

	return build


def _stale_detail(result, detail: str) -> None:
	assert result.returncode == 1, result.stdout + result.stderr
	assert "::error::CI_FINAL_MERGE_REF:" in result.stdout
	line = next(ln for ln in result.stdout.splitlines() if "outcome=stale " in ln)
	assert line.endswith(f" detail={detail}"), line
	assert "outcome=stale_accepted" not in result.stdout


def test_sync_only_stale_merge_ref_is_accepted(integration) -> None:
	it = integration()
	it["standard"]()
	result, tested, calls = it["run"]()
	assert result.returncode == 0, result.stdout + result.stderr
	assert "::error::" not in result.stdout
	assert (
		f"outcome=stale_accepted reason=sync_only_no_overlap base={INTEGRATION} tested_base={it['i0']} "
		f"tip={it['sync']} commits=2 files=1"
	) in result.stdout
	assert tested == it["merge_ref"]
	summary = it["summary"].read_text(encoding="utf-8").splitlines()
	assert len(summary) == 1 and it["i0"] in summary[0] and it["sync"] in summary[0]
	assert result.exported == []
	assert sum(1 for c in calls if "/compare/" in c[1]) == 2
	assert sum(1 for c in calls if "/pulls/7/files" in c[1]) == 1


def test_default_branch_pr_stale_merge_ref_still_fails(world) -> None:
	result, tested, calls = world["run"](env={"CI_FINAL_MERGE_REF_MERGE_DEPTH": "1"})
	_stale_detail(result, "default_base")
	assert f"outcome=stale reason=merge_base_too_deep base=main tested_base={world['old_base']}" in result.stdout
	assert tested == world["old_merge"]
	assert not any("/compare/" in c[1] or "/files" in c[1] for c in calls)


def test_sync_only_switch_off_keeps_the_stale_exit(integration) -> None:
	it = integration()
	it["standard"]()
	result, tested, calls = it["run"](env={"CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED": "false"})
	assert result.returncode == 1
	expected = (
		f"CI_FINAL_MERGE_REF pr=7 head={it['pr_head']} outcome=stale reason=merge_base_too_deep base={INTEGRATION} "
		f"tested_base={it['i0']} tip={it['sync']} pin={it['sync']} pin_source=run_created_at"
	)
	assert expected in result.stdout.splitlines(), result.stdout
	assert "detail=" not in result.stdout
	assert not any("/compare/" in c[1] for c in calls)
	assert it["summary"].read_text(encoding="utf-8") == ""


def test_sync_touching_a_pr_file_fails_as_overlap(integration) -> None:
	it = integration()
	it["standard"](range_files=[{"filename": "m.txt", "status": "added"}, {"filename": "p.txt", "status": "modified"}])
	result, _, _ = it["run"]()
	_stale_detail(result, "file_overlap")


def test_sync_renaming_a_pr_file_fails_as_overlap(integration) -> None:
	it = integration()
	it["standard"](range_files=[{"filename": "q.txt", "status": "renamed", "previous_filename": "p.txt"}])
	result, _, _ = it["run"]()
	_stale_detail(result, "file_overlap")


def test_pr_renamed_file_counts_its_old_path(integration) -> None:
	it = integration()
	it["standard"](pr_files=[{"filename": "p2.txt", "status": "renamed", "previous_filename": "m.txt"}])
	result, _, _ = it["run"]()
	_stale_detail(result, "file_overlap")


def test_non_sync_commit_fails_as_foreign(integration) -> None:
	it = integration()
	it["standard"](range_commits=[
		_commit(it["m1"], ["0" * 40], "main 1"),
		_commit("a" * 40, [it["i0"]], "feat: someone pushed to the integration branch"),
	])
	result, _, _ = it["run"]()
	_stale_detail(result, "foreign_commit")


def test_sync_subject_with_a_foreign_second_parent_fails_as_foreign(integration) -> None:
	it = integration()
	it["standard"](range_commits=[
		_commit(it["sync"], [it["i0"], "e" * 40], f"chore: sync main into {INTEGRATION}"),
	])
	result, _, _ = it["run"]()
	_stale_detail(result, "foreign_commit")


def test_diverged_range_fails_as_not_ahead(integration) -> None:
	it = integration()
	it["standard"](status="diverged")
	result, _, _ = it["run"]()
	_stale_detail(result, "not_ahead")


@pytest.mark.parametrize("shape", ["total_over_250", "short_commit_list", "300_files"])
def test_truncated_range_compare_fails(integration, shape: str) -> None:
	it = integration()
	it["standard"]()
	endpoint = f"compare/{it['i0']}...{it['sync']}"
	path = it["fixtures"] / _fixture_name(endpoint)
	payload = json.loads(path.read_text(encoding="utf-8"))
	if shape == "total_over_250":
		payload["total_commits"] = 251
	elif shape == "short_commit_list":
		payload["total_commits"] = 3
	else:
		payload["files"] = [{"filename": f"f{i}.txt", "status": "added"} for i in range(300)]
	it["fixture"](endpoint, payload)
	result, _, _ = it["run"]()
	_stale_detail(result, "truncated")


def test_truncated_default_branch_compare_fails(integration) -> None:
	it = integration()
	it["standard"]()
	it["fixture"](f"compare/{it['i0']}...main", {"status": "diverged", "total_commits": 3, "commits": [_commit(it["m1"], ["0" * 40], "main 1")]})
	result, _, _ = it["run"]()
	_stale_detail(result, "truncated")


def test_default_branch_other_than_main_is_read_from_the_symref(integration) -> None:
	it = integration(default="trunk")
	it["standard"]()
	result, _, calls = it["run"]()
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=stale_accepted reason=sync_only_no_overlap" in result.stdout
	assert any(c[1].endswith(f"compare/{it['i0']}...trunk") for c in calls)


def test_non_merge_resolver_commit_is_accepted(integration) -> None:
	it = integration()
	it["standard"](range_commits=[
		_commit(it["m1"], ["0" * 40], "main 1"),
		_commit(it["sync"], [it["i0"], it["m1"]], f"chore: sync main into {INTEGRATION}"),
		_commit("d" * 40, [it["sync"]], "[ai-merge-resolve] repair post-sync test failures"),
	])
	result, _, _ = it["run"]()
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=stale_accepted reason=sync_only_no_overlap" in result.stdout and "commits=3 files=1" in result.stdout


def test_too_many_pr_files_fails_without_a_listing(integration) -> None:
	it = integration()
	it["standard"]()
	result, _, calls = it["run"](env={"FAKE_GH_CHANGED_FILES": "3001"})
	_stale_detail(result, "too_many_files")
	assert not any("/pulls/7/files" in c[1] for c in calls)


def test_short_pr_files_listing_fails_as_truncated(integration) -> None:
	it = integration()
	it["standard"]()
	result, _, _ = it["run"](env={"FAKE_GH_CHANGED_FILES": "2"})
	_stale_detail(result, "truncated")


def test_missing_api_response_fails_closed(integration) -> None:
	it = integration()  # no fixtures: the first compare cannot be read
	result, tested, _ = it["run"]()
	_stale_detail(result, "api_error")
	assert f"outcome=stale reason=merge_base_too_deep base={INTEGRATION}" in result.stdout
	assert tested == it["merge_ref"]


def test_tokenless_tree_fails_closed_at_the_files_listing(integration, tmp_path: Path) -> None:
	it = integration()
	api = tmp_path / "api" / "repos" / "o" / "r"
	(api / "pulls").mkdir(parents=True)
	(api / "actions" / "runs").mkdir(parents=True)
	(api / "compare").mkdir(parents=True)
	(api / "pulls" / "7").write_text(json.dumps({"base": {"ref": INTEGRATION}, "changed_files": 1, "mergeable": True}), encoding="utf-8")
	(api / "actions" / "runs" / "42").write_text(json.dumps({"created_at": f"{DAY}03:00:00Z"}), encoding="utf-8")
	it["standard"]()
	for endpoint in (f"compare/{it['i0']}...{it['sync']}", f"compare/{it['i0']}...main"):
		(api / endpoint).write_text((it["fixtures"] / _fixture_name(endpoint)).read_text(encoding="utf-8"), encoding="utf-8")
	result, _, calls = it["run"](env={"GH_TOKEN": "", "CI_FINAL_MERGE_REF_API_URL": f"file://{tmp_path / 'api'}"})
	_stale_detail(result, "api_error")
	assert calls == []


def test_tested_base_is_the_original_checkouts_first_parent(integration) -> None:
	"""A refresh moves the merge ref to merge(M1, H) without a checkout: the original first parent is judged."""
	it = integration()
	it["standard"]()
	result, tested, _ = it["run"](env={"FAKE_GH_REFRESH_TO": it["m1_merge"]})
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"outcome=stale_accepted reason=sync_only_no_overlap base={INTEGRATION} tested_base={it['i0']} tip={it['sync']}" in result.stdout
	assert tested == it["merge_ref"]


def test_base_tip_moving_during_the_check_fails_as_truncated(integration) -> None:
	it = integration()
	it["standard"]()
	result, _, _ = it["run"](env={
		"FAKE_GH_FILES_MOVE_REF": f"refs/heads/{INTEGRATION}", "FAKE_GH_FILES_MOVE_TO": it["m1_merge"],
	})
	_stale_detail(result, "truncated")
	assert it["summary"].read_text(encoding="utf-8") == ""


def test_fresh_path_makes_no_sync_only_calls(world) -> None:
	_git(world["origin"], "update-ref", "refs/pull/7/merge", world["new_merge"])
	result, _, calls = world["run"]()
	assert result.returncode == 0
	assert len(calls) == 2
	assert not any("/compare/" in c[1] or "/files" in c[1] for c in calls)
