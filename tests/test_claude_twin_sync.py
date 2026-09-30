"""Contract for scripts/claude_twin_sync.py — the CLAUDE.md §28.C twin sync
that copies workflow-templates/.claude/** into .claude/** through one PR
(issue #4785) — and its wiring in .github/workflows/claude-twin-sync.yml."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("claude_twin_sync", ROOT / "scripts" / "claude_twin_sync.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

WORKFLOW = ROOT / ".github" / "workflows" / "claude-twin-sync.yml"
GIT_ENV = {
	"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
	"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
	"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> str:
	proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
	assert proc.returncode == 0, proc.stderr
	return proc.stdout.strip()


def write(repo: Path, rel: str, text: str) -> None:
	path = repo / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
	git(repo, "add", "-A")
	git(repo, "commit", "-q", "--allow-empty", "-m", message)
	return git(repo, "rev-parse", "HEAD")


def twin(rel: str) -> str:
	return f"{sync.TWIN_ROOT}/{rel}"


def claude(rel: str) -> str:
	return f"{sync.CLAUDE_ROOT}/{rel}"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
	"""A work repo whose twins and .claude/ start identical, with a bare origin."""
	origin = tmp_path / "origin.git"
	subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, env={**os.environ, **GIT_ENV})
	work = tmp_path / "work"
	work.mkdir()
	git(work, "init", "-q", "-b", "main")
	git(work, "remote", "add", "origin", str(origin))
	for rel, text in {
		"commands/a.md": "a v1\n",
		"scripts/s.py": "s v1\n",
		"hooks/h.py": "h v1\n",
		"settings.json": "{}\n",
		"commands/analyze-log.md": "consumer edition\n",
	}.items():
		write(work, twin(rel), text)
		write(work, claude(rel), "upstream edition\n" if rel == "commands/analyze-log.md" else text)
	commit(work, "base")
	git(work, "push", "-q", "origin", "main")
	return work


# --- paths -------------------------------------------------------------------


@pytest.mark.parametrize("rel,unsafe", [
	("commands/a.md", False),
	("hooks/deep/h.py", False),
	("", True),
	("/etc/passwd", True),
	("../x", True),
	("commands/../../x", True),
	("commands/./a.md", True),
	("commands//a.md", True),
	("commands\\a.md", True),
	("commands/a\n::warning::x.md", True),
	("commands/a\r.md", True),
	("commands/a\t.md", True),
	("commands/a\x7f.md", True),
])
def test_unsafe_path_reason(rel, unsafe):
	assert bool(sync.unsafe_path_reason(rel)) is unsafe


@pytest.mark.parametrize("rel,guard", [
	("hooks/pr_watch_guard.py", True),
	("hooks/session-start.sh", True),
	("settings.json", True),
	("settings.local.json", True),
	("commands/settings.json", False),
	("scripts/check_in_status.py", False),
	("commands/hooks.md", False),
])
def test_is_guard_path(rel, guard):
	assert sync.is_guard_path(rel) is guard


def test_upstream_only_paths_are_the_known_variants_and_the_pickup():
	assert sync.UPSTREAM_ONLY_PATHS == {
		"commands/analyze-log.md", "commands/claude-issue-pickup.md", "commands/deploy-activate.md",
		"commands/investigate-issue.md", "commands/validate-consumer-issue.md", "commands/verify-activation.md",
	}


def test_live_repo_twins_differ_only_in_upstream_only_files():
	"""Every non-excluded twin in this checkout is either identical or awaiting sync."""
	twins = {p.relative_to(ROOT / sync.TWIN_ROOT).as_posix() for p in (ROOT / sync.TWIN_ROOT).rglob("*") if p.is_file()}
	for rel in sorted(twins - sync.UPSTREAM_ONLY_PATHS):
		assert (ROOT / sync.CLAUDE_ROOT / rel).exists(), rel


# --- plan --------------------------------------------------------------------


def test_plan_identical_twins_is_empty(repo):
	plan = sync.plan_sync(str(repo), "HEAD")
	assert plan["copies"] == [] and plan["conflicts"] == [] and not plan["needs_owner"]
	assert plan["excluded"] == ["commands/analyze-log.md"]


def test_plan_copies_a_behind_file_and_a_new_file(repo):
	write(repo, twin("commands/a.md"), "a v2\n")
	write(repo, twin("commands/new.md"), "new\n")
	commit(repo, "twin ahead")
	plan = sync.plan_sync(str(repo), "HEAD")
	assert [(c["path"], c["reason"]) for c in plan["copies"]] == [("commands/a.md", "behind"), ("commands/new.md", "missing")]
	assert plan["conflicts"] == [] and not plan["guard"] and not plan["needs_owner"]


def test_plan_behind_by_several_versions_is_still_a_copy(repo):
	for version in ("v2", "v3", "v4"):
		write(repo, twin("scripts/s.py"), f"s {version}\n")
		commit(repo, version)
	plan = sync.plan_sync(str(repo), "HEAD")
	assert [c["path"] for c in plan["copies"]] == ["scripts/s.py"]


def test_plan_a_twin_version_first_made_by_a_merge_is_behind_not_a_conflict(repo):
	"""A merge that combines two twin edits produces a version no single-parent
	commit ever held. When `.claude/` was synced to it and the twin then moved
	on only through another merge (a project branch syncing the default
	branch in), the `.claude/` copy is behind, not a conflict."""
	lines = [f"line {n}\n" for n in range(1, 10)]
	rel = "commands/m.md"
	write(repo, twin(rel), "".join(lines))
	write(repo, claude(rel), "".join(lines))
	commit(repo, "multi-line twin")
	git(repo, "checkout", "-q", "-b", "project")
	write(repo, twin(rel), "".join(lines[:4] + ["project line\n"] + lines[4:]))
	commit(repo, "project edits the twin")
	git(repo, "checkout", "-q", "main")
	git(repo, "checkout", "-q", "-b", "side")
	write(repo, twin(rel), "".join(lines + ["side line\n"]))
	commit(repo, "side edits the twin")
	git(repo, "checkout", "-q", "main")
	write(repo, twin(rel), "".join(["main line\n"] + lines))
	commit(repo, "main edits the twin")
	git(repo, "merge", "-q", "--no-edit", "side")
	merged = (repo / twin(rel)).read_text(encoding="utf-8")
	assert merged == "".join(["main line\n"] + lines + ["side line\n"])
	write(repo, claude(rel), merged)
	commit(repo, "[claude-twin-sync] copy the merge-born twin")
	git(repo, "checkout", "-q", "project")
	git(repo, "merge", "-q", "--no-edit", "main")
	assert (repo / claude(rel)).read_text(encoding="utf-8") == merged
	assert (repo / twin(rel)).read_text(encoding="utf-8") != merged
	plan = sync.plan_sync(str(repo), "HEAD")
	assert plan["conflicts"] == []
	assert [(c["path"], c["reason"]) for c in plan["copies"]] == [(rel, "behind")]
	assert not plan["needs_owner"]


def test_plan_direct_edit_is_a_conflict_and_needs_the_owner(repo):
	write(repo, twin("commands/a.md"), "a v2\n")
	write(repo, claude("commands/a.md"), "operator edit\n")
	commit(repo, "diverged")
	plan = sync.plan_sync(str(repo), "HEAD")
	assert plan["copies"] == []
	assert [c["path"] for c in plan["conflicts"]] == ["commands/a.md"]
	assert plan["needs_owner"] and not plan["guard"]


def test_plan_guard_copy_needs_the_owner(repo):
	write(repo, twin("hooks/h.py"), "h v2\n")
	write(repo, twin("settings.json"), '{"a": 1}\n')
	commit(repo, "guards")
	plan = sync.plan_sync(str(repo), "HEAD")
	assert plan["guard_paths"] == ["hooks/h.py", "settings.json"]
	assert plan["guard"] and plan["needs_owner"]


def test_plan_symlinks_are_conflicts(repo):
	os.symlink("a.md", repo / twin("commands/link.md"))
	write(repo, twin("commands/b.md"), "b\n")
	os.symlink("a.md", repo / claude("commands/b.md"))
	commit(repo, "symlinks")
	plan = sync.plan_sync(str(repo), "HEAD")
	reasons = {c["path"]: c["reason"] for c in plan["conflicts"]}
	assert "not a regular file" in reasons["commands/link.md"]
	assert "not a regular file" in reasons["commands/b.md"]
	assert plan["copies"] == []


def test_plan_mode_change_is_a_copy(repo):
	(repo / twin("scripts/s.py")).chmod(0o755)
	commit(repo, "exec bit")
	plan = sync.plan_sync(str(repo), "HEAD")
	assert [(c["path"], c["reason"], c["mode"]) for c in plan["copies"]] == [("scripts/s.py", "mode", "100755")]


def test_plan_ignores_upstream_only_files(repo):
	write(repo, twin("commands/analyze-log.md"), "consumer edition v2\n")
	commit(repo, "variant")
	plan = sync.plan_sync(str(repo), "HEAD")
	assert plan["copies"] == [] and plan["conflicts"] == []


def test_plan_refuses_a_shallow_clone(repo, tmp_path):
	write(repo, twin("commands/a.md"), "a v2\n")
	commit(repo, "second")
	shallow = tmp_path / "shallow"
	subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], check=True, env={**os.environ, **GIT_ENV})
	with pytest.raises(sync.SyncError, match="shallow"):
		sync.plan_sync(str(shallow), "HEAD")


def test_build_tree_writes_copies_without_touching_the_work_tree(repo):
	write(repo, twin("commands/a.md"), "a v2\n")
	head = commit(repo, "twin ahead")
	plan = sync.plan_sync(str(repo), head)
	tree = sync.build_tree(str(repo), head, plan["copies"])
	assert git(repo, "show", f"{tree}:{claude('commands/a.md')}") == "a v2"
	assert (repo / claude("commands/a.md")).read_text() == "a v1\n"
	assert git(repo, "status", "--porcelain") == ""


def test_build_tree_refuses_an_unsafe_copy(repo):
	with pytest.raises(sync.SyncError, match="unsafe"):
		sync.build_tree(str(repo), "HEAD", [{"path": "../x", "mode": "100644", "blob": "a" * 40}])


# --- check -------------------------------------------------------------------


def test_check_allows_the_twin_to_be_ahead(repo):
	base = git(repo, "rev-parse", "HEAD")
	write(repo, twin("commands/a.md"), "a v2\n")
	head = commit(repo, "twin only")
	assert sync.check_not_ahead(str(repo), base, head)["ok"]


def test_check_allows_a_sync_to_the_twin(repo):
	write(repo, twin("commands/a.md"), "a v2\n")
	base = commit(repo, "twin only")
	write(repo, claude("commands/a.md"), "a v2\n")
	head = commit(repo, "sync")
	result = sync.check_not_ahead(str(repo), base, head)
	assert result["ok"] and result["checked"] == [claude("commands/a.md")]


def test_check_fails_when_claude_moves_past_its_twin(repo):
	base = git(repo, "rev-parse", "HEAD")
	write(repo, claude("hooks/h.py"), "loosened\n")
	head = commit(repo, "direct edit")
	result = sync.check_not_ahead(str(repo), base, head)
	assert not result["ok"]
	assert [v["path"] for v in result["violations"]] == [claude("hooks/h.py")]


def test_check_fails_for_a_claude_only_new_file_and_a_one_sided_delete(repo):
	base = git(repo, "rev-parse", "HEAD")
	write(repo, claude("commands/only-here.md"), "x\n")
	(repo / claude("scripts/s.py")).unlink()
	head = commit(repo, "ahead")
	assert {v["path"] for v in sync.check_not_ahead(str(repo), base, head)["violations"]} == {
		claude("commands/only-here.md"), claude("scripts/s.py"),
	}


def test_check_violation_messages_name_the_side_that_is_missing(repo):
	base = git(repo, "rev-parse", "HEAD")
	write(repo, claude("commands/only-here.md"), "x\n")
	(repo / claude("scripts/s.py")).unlink()
	write(repo, claude("commands/a.md"), "ahead\n")
	head = commit(repo, "ahead")
	reasons = {v["path"]: v["reason"] for v in sync.check_not_ahead(str(repo), base, head)["violations"]}
	assert reasons[claude("commands/only-here.md")].startswith("has no twin at ")
	assert "delete both copies together" in reasons[claude("commands/only-here.md")]
	assert reasons[claude("scripts/s.py")].startswith("deleted while ")
	assert "edit the twin" not in reasons[claude("scripts/s.py")]
	assert "edit the twin" in reasons[claude("commands/a.md")]


def test_check_allows_a_synced_delete_and_upstream_only_edits(repo):
	base = git(repo, "rev-parse", "HEAD")
	(repo / claude("scripts/s.py")).unlink()
	(repo / twin("scripts/s.py")).unlink()
	write(repo, claude("commands/analyze-log.md"), "upstream edition v2\n")
	write(repo, claude("commands/claude-issue-pickup.md"), "pickup\n")
	head = commit(repo, "ok")
	assert sync.check_not_ahead(str(repo), base, head)["ok"]


def test_check_cli_exit_codes(repo, capsys):
	base = git(repo, "rev-parse", "HEAD")
	write(repo, claude("commands/a.md"), "ahead\n")
	commit(repo, "ahead")
	assert sync.main(["check", "--repo-root", str(repo), "--base", base]) == 1
	assert "::error file=.claude/commands/a.md::" in capsys.readouterr().out
	assert sync.main(["check", "--repo-root", str(repo), "--base", sync.ZERO_SHA]) == 0
	assert sync.main(["check", "--repo-root", str(repo), "--base", "no-such-ref"]) == 2


def test_check_cli_cannot_inject_workflow_commands_through_a_path(repo, capsys):
	"""A `.claude/` file name with a newline must not start a workflow command line."""
	base = git(repo, "rev-parse", "HEAD")
	write(repo, claude("commands/x\n::warning title=INJECTED::pwned"), "x\n")
	commit(repo, "crafted name")
	assert sync.main(["check", "--repo-root", str(repo), "--base", base]) == 1
	captured = capsys.readouterr()
	lines = (captured.out + captured.err).splitlines()
	assert not [line for line in lines if line.startswith("::warning")], lines
	errors = [line for line in lines if line.startswith("::error ")]
	assert len(errors) == 1 and "%0A" in errors[0] and "%3A%3Awarning" in errors[0]


# --- check with guard provenance (stable, issue #5247) -----------------------


def _main_and_stable(repo: Path) -> tuple[str, str]:
	"""Move `main` to hook v2 in both copies; return (main tip, stable tip = the old main)."""
	stable = git(repo, "rev-parse", "HEAD")
	git(repo, "branch", "stable", stable)
	write(repo, claude("hooks/h.py"), "h v2\n")
	write(repo, twin("hooks/h.py"), "h v2\n")
	main = commit(repo, "hook v2 on main")
	git(repo, "checkout", "-q", "stable")
	return main, stable


def test_check_guard_provenance_allows_a_backport_of_main(repo):
	main, stable = _main_and_stable(repo)
	write(repo, claude("hooks/h.py"), "h v2\n")
	write(repo, twin("hooks/h.py"), "h v2\n")
	head = commit(repo, "backport")
	result = sync.check_not_ahead(str(repo), stable, head, main)
	assert result["ok"] and result["guard_provenance_ref"] == main


def test_check_guard_provenance_refuses_guard_content_main_does_not_carry(repo):
	main, stable = _main_and_stable(repo)
	write(repo, claude("hooks/h.py"), "loosened\n")
	write(repo, twin("hooks/h.py"), "loosened\n")
	write(repo, claude("settings.json"), '{"allow": ["*"]}\n')
	write(repo, twin("settings.json"), '{"allow": ["*"]}\n')
	head = commit(repo, "guard change straight into stable")
	assert sync.check_not_ahead(str(repo), stable, head)["ok"], "without the ref the twin rule alone holds"
	result = sync.check_not_ahead(str(repo), stable, head, main)
	assert not result["ok"]
	assert {v["path"] for v in result["violations"]} == {claude("hooks/h.py"), claude("settings.json")}
	assert all("not on the default branch" in v["reason"] for v in result["violations"])


def test_check_guard_provenance_refuses_a_guard_delete_main_keeps(repo):
	main, stable = _main_and_stable(repo)
	(repo / claude("hooks/h.py")).unlink()
	(repo / twin("hooks/h.py")).unlink()
	head = commit(repo, "delete the hook on stable")
	assert sync.check_not_ahead(str(repo), stable, head)["ok"]
	result = sync.check_not_ahead(str(repo), stable, head, main)
	assert [v["path"] for v in result["violations"]] == [claude("hooks/h.py")]


def test_check_guard_provenance_refuses_a_mode_change_main_does_not_carry(repo):
	main, stable = _main_and_stable(repo)
	write(repo, claude("hooks/h.py"), "h v2\n")
	write(repo, twin("hooks/h.py"), "h v2\n")
	os.chmod(repo / claude("hooks/h.py"), 0o755)
	head = commit(repo, "backport, but executable")
	result = sync.check_not_ahead(str(repo), stable, head, main)
	assert [v["path"] for v in result["violations"]] == [claude("hooks/h.py")]


def test_check_guard_provenance_ignores_non_guard_paths(repo):
	main, stable = _main_and_stable(repo)
	write(repo, claude("commands/a.md"), "stable-only fix\n")
	write(repo, twin("commands/a.md"), "stable-only fix\n")
	head = commit(repo, "command fix straight into stable")
	assert sync.check_not_ahead(str(repo), stable, head, main)["ok"]


def test_check_cli_guard_provenance_ref(repo, capsys):
	main, stable = _main_and_stable(repo)
	write(repo, claude("hooks/h.py"), "loosened\n")
	write(repo, twin("hooks/h.py"), "loosened\n")
	commit(repo, "guard change")
	args = ["check", "--repo-root", str(repo), "--base", stable]
	assert sync.main(args) == 0
	assert json.loads(capsys.readouterr().out.splitlines()[-1])["guard_provenance_ref"] is None
	assert sync.main([*args, "--guard-provenance-ref", main]) == 1
	assert "::error file=.claude/hooks/h.py::" in capsys.readouterr().out
	assert sync.main([*args, "--guard-provenance-ref", "no-such-ref"]) == 2


def test_log_escapes_line_breaks(capsys):
	sync.log("rejected path=a\n::warning::x\rb")
	err = capsys.readouterr().err
	assert err.count("\n") == 1 and "\r" not in err and "::warning" not in err.splitlines()[0][:10]


# --- merge rules -------------------------------------------------------------


def _sync_branch(repo: Path, files: dict[str, str]) -> tuple[str, str]:
	"""Main with the twin ahead, and a branch head that applies `files`."""
	write(repo, twin("commands/a.md"), "a v2\n")
	main = commit(repo, "twin ahead")
	git(repo, "checkout", "-q", "-b", "sync")
	for rel, text in files.items():
		write(repo, rel, text)
	head = commit(repo, "sync")
	git(repo, "checkout", "-q", "main")
	return main, head


def test_merge_check_accepts_a_pure_sync(repo):
	main, head = _sync_branch(repo, {claude("commands/a.md"): "a v2\n"})
	assert sync.merge_check(str(repo), main, head, [{"filename": claude("commands/a.md"), "status": "modified"}]) == {"ok": True, "reasons": []}


@pytest.mark.parametrize("files,pr_files,expected", [
	({claude("commands/a.md"): "a v3\n"}, [{"filename": claude("commands/a.md"), "status": "modified"}], "differs from"),
	({twin("commands/a.md"): "a v9\n", claude("commands/a.md"): "a v9\n"},
		[{"filename": twin("commands/a.md"), "status": "modified"}, {"filename": claude("commands/a.md"), "status": "modified"}], "outside .claude/"),
	({claude("hooks/h.py"): "h v1\n"}, [{"filename": claude("hooks/h.py"), "status": "modified"}], "guard path"),
	({}, [{"filename": claude("scripts/s.py"), "status": "removed"}], "status removed"),
	({}, [{"filename": claude("commands/analyze-log.md"), "status": "modified"}], "upstream-only"),
	({}, [], "changes no file"),
])
def test_merge_check_refuses_anything_but_a_pure_non_guard_sync(repo, files, pr_files, expected):
	main, head = _sync_branch(repo, files)
	verdict = sync.merge_check(str(repo), main, head, pr_files)
	assert not verdict["ok"]
	assert any(expected in reason for reason in verdict["reasons"]), verdict


def _run(name, status="completed", conclusion="success", run_id="1"):
	return {"name": name, "status": status, "conclusion": conclusion, "details_url": f"https://github.com/o/r/actions/runs/{run_id}/job/9"}


@pytest.mark.parametrize("runs,state,count,ok,why", [
	([_run("lint"), _run("gate", conclusion="skipped")], "", 0, True, "all checks passed"),
	([_run("gate")], "", 0, False, "lint has not passed"),
	([_run("lint"), _run("x", status="in_progress", conclusion=None)], "", 0, False, "in_progress"),
	([_run("lint"), _run("x", conclusion="failure")], "", 0, False, "concluded failure"),
	([_run("lint"), _run("sync", status="in_progress", conclusion=None, run_id="77")], "", 0, True, "all checks passed"),
	([_run("lint")], "pending", 1, False, "statuses are pending"),
	([_run("lint")], "success", 1, True, "all checks passed"),
])
def test_checks_green(runs, state, count, ok, why):
	green, reason = sync.checks_green(runs, state, count, "77")
	assert green is ok and why in reason


HEAD_SHA = "c" * 40


def _review(state, user="owner", assoc="OWNER", commit=HEAD_SHA, at="2026-09-28T10:00:00Z", rid=1):
	return {"id": rid, "state": state, "user": {"login": user}, "author_association": assoc, "commit_id": commit, "submitted_at": at}


@pytest.mark.parametrize("reviews,approved", [
	([], False),
	([_review("APPROVED")], True),
	([_review("APPROVED", user="Owner")], True),
	([_review("APPROVED", commit="d" * 40)], False),
	([_review("APPROVED", user="someone", assoc="COLLABORATOR")], False),
	([_review("APPROVED", assoc="COLLABORATOR")], False),
	([_review("APPROVED", rid=1), _review("CHANGES_REQUESTED", at="2026-09-28T11:00:00Z", rid=2)], False),
	([_review("CHANGES_REQUESTED", rid=1), _review("APPROVED", at="2026-09-28T11:00:00Z", rid=2)], True),
	([_review("APPROVED", rid=1), _review("COMMENTED", at="2026-09-28T11:00:00Z", rid=2)], True),
	([_review("DISMISSED")], False),
])
def test_owner_approved(reviews, approved):
	assert sync.owner_approved(reviews, "owner", HEAD_SHA) is approved


def test_render_body_carries_skip_ai_and_the_merge_rule():
	plan = {"ref": "a" * 40, "copies": [{"path": "hooks/h.py", "reason": "behind"}], "conflicts": [], "rejected": [],
		"guard_paths": ["hooks/h.py"], "needs_owner": True}
	body = sync.render_body(plan)
	assert "[skip ai]" in body and "Needs the repository owner" in body and "never approves or merges" in body
	assert not re.search(r"(?i)\b(fix(es|ed)?|close[sd]?|resolve[sd]?)\s+#\d+", body)
	plan.update({"guard_paths": [], "needs_owner": False})
	assert "the workflow merges this PR" in sync.render_body(plan)


# --- run (driver) ------------------------------------------------------------


class FakeGitHub(sync.GitHub):
	def __init__(self, repo_slug="o/r", prs=None, reviews=None, files=None, runs=None, status=None):
		super().__init__(repo_slug, "read-token", "write-token")
		self.prs = prs or []
		self.reviews = reviews or []
		self.files = files or []
		self.runs = runs or []
		self.status = status or {"state": "", "statuses": []}
		self.status_reads = 0
		self.writes: list[list[str]] = []

	def get(self, path):
		if "/pulls?state=open" in path:
			return self.prs if "page=1" in path else []
		if path.startswith(f"repos/{self.repo}/pulls/") and "/reviews" in path:
			return self.reviews if "page=1" in path else []
		if "/files" in path:
			return self.files if "page=1" in path else []
		if "/check-runs" in path:
			return {"check_runs": self.runs if "page=1" in path else []}
		if path.endswith("/status"):
			self.status_reads += 1
			return self.status
		raise AssertionError(path)

	def write(self, args):
		self.writes.append(args)
		if args[:4] == ["api", "-X", "POST", f"repos/{self.repo}/pulls"]:
			return json.dumps({"number": 41})
		return "{}"

	def wrote(self, needle: str) -> list[list[str]]:
		return [w for w in self.writes if any(needle in part for part in w)]

	def merges(self) -> list[list[str]]:
		return [w for w in self.writes if w[:2] == ["pr", "merge"]]


def _pr_from_origin(repo: Path, branch: str, number=40, labels=(), body=""):
	return {"number": number, "body": body, "labels": [{"name": n} for n in labels],
		"head": {"ref": branch, "sha": git(repo, "rev-parse", f"origin/{branch}"), "repo": {"full_name": "o/r"}}}


def _twin_ahead(repo: Path, rel="commands/a.md", text="a v2\n") -> str:
	write(repo, twin(rel), text)
	sha = commit(repo, f"twin {rel}")
	git(repo, "push", "-q", "origin", "main")
	return sha


def test_run_noop_when_twins_match(repo):
	gh = FakeGitHub()
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["action"] == "noop" and gh.writes == []


def test_run_closes_a_stale_sync_pr(repo):
	gh = FakeGitHub(prs=[{"number": 40, "head": {"ref": "claude/claude-twin-sync-x", "sha": "a" * 40, "repo": {"full_name": "o/r"}}}])
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["action"] == "closed_stale"
	assert ["api", "-X", "PATCH", "repos/o/r/pulls/40", "-f", "state=closed"] in gh.writes


def test_run_opens_a_non_guard_sync_pr_and_waits_for_checks(repo):
	main = _twin_ahead(repo)
	gh = FakeGitHub()
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["action"] == "opened" and summary["pr"] == 41
	branch = summary["branch"]
	assert branch == f"claude/claude-twin-sync-{main[:12]}"
	git(repo, "fetch", "-q", "origin")
	assert git(repo, "show", f"origin/{branch}:{claude('commands/a.md')}") == "a v2"
	assert git(repo, "rev-parse", f"origin/{branch}^") == main
	assert git(repo, "diff", "--name-only", main, f"origin/{branch}") == claude("commands/a.md")
	create = gh.wrote("repos/o/r/pulls")[0]
	assert "base=main" in create and any("[skip ai]" in part for part in create)
	assert any("state=success" in w for w in gh.wrote("/statuses/"))
	assert gh.wrote("labels") == [] and gh.merges() == []
	assert summary["merge"] == "waiting for checks on the new head" and summary["alert"] == ""


def test_run_merges_an_unchanged_non_guard_pr_once_checks_pass(repo):
	_twin_ahead(repo)
	first = FakeGitHub()
	branch = sync.run_sync(str(repo), first, "HEAD", "main", "owner", "77")["branch"]
	git(repo, "fetch", "-q", "origin")
	pr = _pr_from_origin(repo, branch, body=sync.render_body(sync.plan_sync(str(repo), "HEAD")))
	files = [{"filename": claude("commands/a.md"), "status": "modified"}]
	waiting = FakeGitHub(prs=[pr], files=files, runs=[_run("lint", status="in_progress", conclusion=None)])
	summary = sync.run_sync(str(repo), waiting, "HEAD", "main", "owner", "77")
	assert summary["action"] == "unchanged" and summary["merge"].startswith("waiting:") and waiting.merges() == []
	assert waiting.wrote("PATCH") == []
	green = FakeGitHub(prs=[pr], files=files, runs=[_run("lint")])
	summary = sync.run_sync(str(repo), green, "HEAD", "main", "owner", "77")
	assert summary["merge"] == "merged"
	assert ["pr", "merge", "40", "--repo", "o/r", "--squash", "--match-head-commit", pr["head"]["sha"]] in green.writes


def test_run_refuses_to_merge_a_tampered_sync_branch(repo):
	_twin_ahead(repo)
	branch = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")["branch"]
	git(repo, "fetch", "-q", "origin")
	pr = _pr_from_origin(repo, branch)
	files = [{"filename": claude("commands/a.md"), "status": "modified"}, {"filename": "scripts/evil.sh", "status": "added"}]
	gh = FakeGitHub(prs=[pr], files=files, runs=[_run("lint")])
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["merge"].startswith("refused:") and gh.merges() == []


def test_run_updates_an_open_pr_forward_when_main_moves(repo):
	_twin_ahead(repo)
	branch = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")["branch"]
	git(repo, "fetch", "-q", "origin")
	old_head = git(repo, "rev-parse", f"origin/{branch}")
	pr = _pr_from_origin(repo, branch)
	main = _twin_ahead(repo, "scripts/s.py", "s v2\n")
	gh = FakeGitHub(prs=[pr])
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["action"] == "updated" and summary["pr"] == 40
	assert gh.wrote("repos/o/r/pulls/40") and not [w for w in gh.writes if w[:4] == ["api", "-X", "POST", "repos/o/r/pulls"]]
	git(repo, "fetch", "-q", "origin")
	new_head = git(repo, "rev-parse", f"origin/{branch}")
	assert git(repo, "merge-base", "--is-ancestor", old_head, new_head) == ""
	assert git(repo, "merge-base", "--is-ancestor", main, new_head) == ""
	assert sorted(git(repo, "diff", "--name-only", main, new_head).splitlines()) == [claude("commands/a.md"), claude("scripts/s.py")]
	assert summary["merge"] == "waiting for checks on the new head"


def test_run_keeps_an_open_pr_when_main_moves_outside_claude_and_its_twins(repo):
	"""A main commit that leaves `.claude/` and the copies alone does not rebuild the
	sync PR: the head keeps its CI run (and an owner approval), and the PR merges."""
	_twin_ahead(repo)
	branch = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")["branch"]
	git(repo, "fetch", "-q", "origin")
	pr = _pr_from_origin(repo, branch, body=sync.render_body(sync.plan_sync(str(repo), "HEAD")))
	write(repo, "README.md", "unrelated\n")
	commit(repo, "unrelated main change")
	git(repo, "push", "-q", "origin", "main")
	files = [{"filename": claude("commands/a.md"), "status": "modified"}]
	gh = FakeGitHub(prs=[pr], files=files, runs=[_run("lint")])
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["action"] == "unchanged" and summary["head"] == pr["head"]["sha"]
	git(repo, "fetch", "-q", "origin")
	assert git(repo, "rev-parse", f"origin/{branch}") == pr["head"]["sha"]
	assert summary["merge"] == "merged"
	assert ["pr", "merge", "40", "--repo", "o/r", "--squash", "--match-head-commit", pr["head"]["sha"]] in gh.writes


def test_run_rebuilds_an_open_pr_that_carries_a_commit_outside_claude(repo, capsys):
	"""A foreign push onto the sync branch (here a README.md edit) is rebuilt away:
	the PR diff is only the copies again, and the next run leaves the head alone."""
	main = _twin_ahead(repo)
	branch = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")["branch"]
	git(repo, "fetch", "-q", "origin")
	git(repo, "checkout", "-q", "-b", "foreign", f"origin/{branch}")
	write(repo, "README.md", "foreign edit\n")
	foreign = commit(repo, "foreign push onto the sync branch")
	git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
	git(repo, "checkout", "-q", "main")
	pr = _pr_from_origin(repo, branch, body=sync.render_body(sync.plan_sync(str(repo), "HEAD")))
	assert pr["head"]["sha"] == foreign
	assert sync.changes_outside_claude(str(repo), main, foreign) == ["README.md"]
	files = [{"filename": claude("commands/a.md"), "status": "modified"}, {"filename": "README.md", "status": "added"}]
	gh = FakeGitHub(prs=[pr], files=files, runs=[_run("lint")])
	capsys.readouterr()
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["action"] == "updated" and summary["merge"] == "waiting for checks on the new head"
	assert f"CLAUDE_TWIN_SYNC rebuild pr=#{pr['number']} reason=head_changes_outside_claude count=1\n" in capsys.readouterr().err
	assert gh.merges() == []
	git(repo, "fetch", "-q", "origin")
	new_head = git(repo, "rev-parse", f"origin/{branch}")
	assert new_head == summary["head"] != foreign
	assert git(repo, "merge-base", "--is-ancestor", foreign, new_head) == ""
	assert git(repo, "diff", "--name-only", main, new_head) == claude("commands/a.md")
	assert sync.changes_outside_claude(str(repo), main, new_head) == []
	again = FakeGitHub(prs=[_pr_from_origin(repo, branch, body=pr["body"])], files=files[:1], runs=[_run("lint")])
	summary = sync.run_sync(str(repo), again, "HEAD", "main", "owner", "77")
	assert summary["action"] == "unchanged" and summary["head"] == new_head and summary["merge"] == "merged"


def test_changes_outside_claude_treats_unrelated_history_as_foreign(repo):
	git(repo, "checkout", "-q", "--orphan", "unrelated")
	write(repo, "other.txt", "x\n")
	unrelated = commit(repo, "unrelated root")
	git(repo, "checkout", "-q", "main")
	assert sync.changes_outside_claude(str(repo), "main", unrelated) is None


def test_run_rebuilds_an_open_pr_with_unrelated_history_and_logs_an_unknown_count(repo, capsys):
	"""A sync branch force-replaced by unrelated history is rebuilt, and the log
	says the path count is unknown instead of reporting one foreign path."""
	_twin_ahead(repo)
	branch = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")["branch"]
	git(repo, "checkout", "-q", "--orphan", "unrelated")
	write(repo, "other.txt", "x\n")
	unrelated = commit(repo, "unrelated root")
	git(repo, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")
	git(repo, "checkout", "-q", "-f", "main")
	pr = _pr_from_origin(repo, branch, body=sync.render_body(sync.plan_sync(str(repo), "HEAD")))
	assert pr["head"]["sha"] == unrelated
	capsys.readouterr()
	summary = sync.run_sync(str(repo), FakeGitHub(prs=[pr]), "HEAD", "main", "owner", "77")
	assert summary["action"] == "updated"
	err = capsys.readouterr().err
	assert f"CLAUDE_TWIN_SYNC rebuild pr=#{pr['number']} reason=head_changes_outside_claude count=unknown no_merge_base=true\n" in err
	assert "count=1" not in err


def test_run_rebuilds_an_open_pr_when_main_changes_claude(repo):
	"""A `.claude/` change on main that the head does not carry still rebuilds it."""
	_twin_ahead(repo)
	branch = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")["branch"]
	git(repo, "fetch", "-q", "origin")
	pr = _pr_from_origin(repo, branch)
	write(repo, claude("commands/b.md"), "b\n")
	write(repo, twin("commands/b.md"), "b\n")
	main = commit(repo, "new twinned command on main")
	git(repo, "push", "-q", "origin", "main")
	summary = sync.run_sync(str(repo), FakeGitHub(prs=[pr]), "HEAD", "main", "owner", "77")
	assert summary["action"] == "updated"
	git(repo, "fetch", "-q", "origin")
	new_head = git(repo, "rev-parse", f"origin/{branch}")
	assert git(repo, "merge-base", "--is-ancestor", main, new_head) == ""
	assert git(repo, "diff", "--name-only", main, new_head) == claude("commands/a.md")


def test_run_guard_pr_is_labelled_alerted_and_never_merged(repo):
	_twin_ahead(repo, "hooks/h.py", "h v2\n")
	gh = FakeGitHub()
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["needs_owner"] and summary["merge"] == "owner only"
	assert "needs the owner" in summary["alert"] and ".claude/hooks/h.py" in summary["alert"]
	assert gh.wrote(f"labels[]={sync.APPROVAL_LABEL}")
	assert any("state=pending" in w for w in gh.wrote("/statuses/"))
	# An owner approval on the current head flips the status, and still nothing merges.
	git(repo, "fetch", "-q", "origin")
	pr = _pr_from_origin(repo, summary["branch"], labels=[sync.APPROVAL_LABEL])
	approved = FakeGitHub(prs=[pr], reviews=[_review("APPROVED", commit=pr["head"]["sha"])], runs=[_run("lint")],
		files=[{"filename": claude("hooks/h.py"), "status": "modified"}])
	summary = sync.run_sync(str(repo), approved, "HEAD", "main", "owner", "77")
	assert summary["owner_approved"] and summary["alert"] == "" and summary["merge"] == "owner only"
	assert any("state=success" in w for w in approved.wrote("/statuses/"))
	assert approved.merges() == [] and approved.wrote("labels[]") == []
	assert not any(w for w in approved.writes if "APPROVE" in " ".join(w) or "/reviews" in " ".join(w))


def _approval_status(state, description):
	return {"state": state, "statuses": [{"context": sync.APPROVAL_STATUS_CONTEXT, "state": state, "description": description}]}


def test_run_posts_the_approval_status_only_when_it_changes(repo):
	"""§15 / GitHub's 1000-statuses-per-sha-and-context limit: an unchanged status is never re-posted."""
	_twin_ahead(repo, "hooks/h.py", "h v2\n")
	branch = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")["branch"]
	git(repo, "fetch", "-q", "origin")
	pr = _pr_from_origin(repo, branch, labels=[sync.APPROVAL_LABEL], body=sync.render_body(sync.plan_sync(str(repo), "HEAD")))
	waiting = FakeGitHub(prs=[pr], status=_approval_status("pending", "Owner review and merge required"))
	summary = sync.run_sync(str(repo), waiting, "HEAD", "main", "owner", "77")
	assert summary["action"] == "unchanged" and summary["merge"] == "owner only"
	assert waiting.writes == [] and waiting.status_reads == 1
	approved = FakeGitHub(prs=[pr], reviews=[_review("APPROVED", commit=pr["head"]["sha"])],
		status=_approval_status("pending", "Owner review and merge required"))
	sync.run_sync(str(repo), approved, "HEAD", "main", "owner", "77")
	assert [w for w in approved.writes if any("state=success" in part for part in w)] == approved.wrote("/statuses/")
	assert len(approved.wrote("/statuses/")) == 1


def test_run_merge_reuses_the_status_read_when_nothing_changed(repo):
	_twin_ahead(repo)
	branch = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")["branch"]
	git(repo, "fetch", "-q", "origin")
	pr = _pr_from_origin(repo, branch, body=sync.render_body(sync.plan_sync(str(repo), "HEAD")))
	files = [{"filename": claude("commands/a.md"), "status": "modified"}]
	green = FakeGitHub(prs=[pr], files=files, runs=[_run("lint")], status=_approval_status("success", "No hook or settings change"))
	summary = sync.run_sync(str(repo), green, "HEAD", "main", "owner", "77")
	assert summary["merge"] == "merged" and green.wrote("/statuses/") == [] and green.status_reads == 1
	# A status that had to be (re)posted is read again before the merge decision.
	stale = FakeGitHub(prs=[pr], files=files, runs=[_run("lint")], status=_approval_status("pending", "Owner review and merge required"))
	sync.run_sync(str(repo), stale, "HEAD", "main", "owner", "77")
	assert len(stale.wrote("/statuses/")) == 1 and stale.status_reads == 2 and stale.merges() == []


def test_run_conflict_only_opens_an_empty_marker_pr(repo):
	write(repo, twin("commands/a.md"), "a v2\n")
	write(repo, claude("commands/a.md"), "operator edit\n")
	main = commit(repo, "diverged")
	git(repo, "push", "-q", "origin", "main")
	gh = FakeGitHub()
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["conflicts"] == ["commands/a.md"] and summary["needs_owner"]
	git(repo, "fetch", "-q", "origin")
	assert git(repo, "diff", "--name-only", main, f"origin/{summary['branch']}") == ""
	assert git(repo, "show", f"origin/{summary['branch']}:{claude('commands/a.md')}") == "operator edit"
	assert gh.wrote(f"labels[]={sync.APPROVAL_LABEL}")


def test_run_closes_duplicate_sync_prs(repo):
	_twin_ahead(repo)
	branch = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")["branch"]
	git(repo, "fetch", "-q", "origin")
	gh = FakeGitHub(prs=[_pr_from_origin(repo, branch, number=45), _pr_from_origin(repo, branch, number=40)])
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["pr"] == 40
	assert ["api", "-X", "PATCH", "repos/o/r/pulls/45", "-f", "state=closed"] in gh.writes


def test_run_picks_a_free_branch_name(repo):
	main = _twin_ahead(repo)
	git(repo, "push", "-q", "origin", f"{main}:refs/heads/claude/claude-twin-sync-{main[:12]}")
	summary = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")
	assert summary["branch"] == f"claude/claude-twin-sync-{main[:12]}-2"


def test_run_replaces_a_sync_pr_whose_branch_was_deleted(repo):
	_twin_ahead(repo)
	gone = {"number": 40, "body": "", "labels": [],
		"head": {"ref": "claude/claude-twin-sync-deleted", "sha": "a" * 40, "repo": {"full_name": "o/r"}}}
	gh = FakeGitHub(prs=[gone])
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert ["api", "-X", "PATCH", "repos/o/r/pulls/40", "-f", "state=closed"] in gh.writes
	assert any("no longer exists" in part for w in gh.wrote("repos/o/r/issues/40/comments") for part in w)
	assert summary["action"] == "opened" and summary["pr"] == 41


def test_run_keeps_a_live_duplicate_when_the_oldest_branch_was_deleted(repo):
	_twin_ahead(repo)
	branch = sync.run_sync(str(repo), FakeGitHub(), "HEAD", "main", "owner", "77")["branch"]
	git(repo, "fetch", "-q", "origin")
	gone = {"number": 39, "body": "", "labels": [],
		"head": {"ref": "claude/claude-twin-sync-deleted", "sha": "a" * 40, "repo": {"full_name": "o/r"}}}
	gh = FakeGitHub(prs=[gone, _pr_from_origin(repo, branch, number=40)])
	summary = sync.run_sync(str(repo), gh, "HEAD", "main", "owner", "77")
	assert summary["pr"] == 40
	assert ["api", "-X", "PATCH", "repos/o/r/pulls/39", "-f", "state=closed"] in gh.writes
	assert ["api", "-X", "PATCH", "repos/o/r/pulls/40", "-f", "state=closed"] not in gh.writes


class _LabelGitHub(FakeGitHub):
	def __init__(self, create_error):
		super().__init__()
		self.create_error = create_error

	def write(self, args):
		if args[:4] == ["api", "-X", "POST", f"repos/{self.repo}/labels"]:
			self.writes.append(args)
			raise sync.SyncError(f"gh api -X POST failed: {self.create_error}")
		return super().write(args)


def test_set_label_tolerates_only_an_existing_label():
	exists = _LabelGitHub("gh: Validation Failed (HTTP 422)")
	sync.set_label(exists, 40, [], True)
	assert exists.wrote(f"labels[]={sync.APPROVAL_LABEL}")
	denied = _LabelGitHub("gh: Resource not accessible by integration (HTTP 403)")
	with pytest.raises(sync.SyncError, match="HTTP 403"):
		sync.set_label(denied, 40, [], True)
	assert denied.wrote(f"labels[]={sync.APPROVAL_LABEL}") == []


def test_twin_state_exempts_upstream_only_paths():
	import sys
	sys.path.insert(0, str(ROOT / "tests"))
	from claude_twin_state import claude_ahead_reason
	for rel in sorted(sync.UPSTREAM_ONLY_PATHS):
		assert claude_ahead_reason(rel) == ("ok", "")


def test_run_cli_requires_tokens(repo, monkeypatch):
	monkeypatch.delenv("CLAUDE_TWIN_SYNC_READ_TOKEN", raising=False)
	monkeypatch.delenv("CLAUDE_TWIN_SYNC_WRITE_TOKEN", raising=False)
	assert sync.main(["run", "--repo-root", str(repo), "--repo", "o/r", "--owner", "o"]) == 2


# --- workflow wiring ---------------------------------------------------------


def _workflow() -> dict:
	return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


# --- ci.yml sync-state step (issue #5247) ------------------------------------

CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CI_STEP_NAME = "Claude twin sync state (CLAUDE.md §28.C)"


def _ci_step() -> dict:
	jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
	steps = [step for job in jobs.values() for step in job.get("steps", []) if step.get("name") == CI_STEP_NAME]
	assert len(steps) == 1
	return steps[0]


def test_ci_step_env_is_event_data_and_the_workflow_token_only():
	step = _ci_step()
	assert step["env"] == {
		"EVENT_NAME": "${{ github.event_name }}",
		"PUSH_BEFORE": "${{ github.event.before }}",
		"REF_NAME": "${{ github.ref_name }}",
		"BASE_REF": "${{ github.base_ref }}",
		# The token only on push events: a PR's checkout is PR content.
		"GH_TOKEN": "${{ github.event_name == 'push' && github.token || '' }}",
	}
	assert "${{" not in step["run"]
	assert step["run"].count("gh api") == 1


@pytest.fixture
def ci(repo: Path, tmp_path: Path):
	"""Run the ci.yml step body in `repo`, with `stable` on origin and a stub `gh`."""
	subprocess.run(["git", "-C", str(repo.parent / "origin.git"), "config", "uploadpack.allowAnySHA1InWant", "true"], check=True)
	git(repo, "push", "-q", "origin", "main:refs/heads/stable")
	stub_bin = tmp_path / "bin"
	stub_bin.mkdir()
	gh_log = tmp_path / "gh-args.log"
	(stub_bin / "gh").write_text(
		"#!/usr/bin/env bash\n"
		f"printf '%s\\n' \"$*\" >> {gh_log}\n"
		"[ -n \"${STUB_GH_STATUS:-}\" ] || { echo 'stub gh: HTTP 502' >&2; exit 1; }\n"
		"printf '%s\\n' \"${STUB_GH_STATUS}\"\n",
		encoding="utf-8",
	)
	(stub_bin / "gh").chmod(0o755)

	def run(event: str, *, base_ref: str = "", ref_name: str = "", before: str = "", gh_status: str = "") -> subprocess.CompletedProcess:
		# The script sits untracked in the work tree, as in the CI checkout;
		# every commit the test makes is already in place.
		(repo / "scripts").mkdir(exist_ok=True)
		(repo / "scripts" / "claude_twin_sync.py").write_text((ROOT / "scripts" / "claude_twin_sync.py").read_text(encoding="utf-8"), encoding="utf-8")
		env = {
			**os.environ, **GIT_ENV,
			"PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
			"EVENT_NAME": event, "BASE_REF": base_ref, "REF_NAME": ref_name, "PUSH_BEFORE": before,
			"GH_TOKEN": "stub", "GITHUB_REPOSITORY": "owner/repo", "STUB_GH_STATUS": gh_status,
		}
		proc = subprocess.run(["bash", "-c", _ci_step()["run"]], cwd=repo, capture_output=True, text=True, env=env)
		proc.gh_calls = gh_log.read_text(encoding="utf-8").splitlines() if gh_log.exists() else []
		return proc

	return run


def _pr_merge(repo: Path, base: str, edits: dict[str, str]) -> str:
	"""Check out the merge commit a PR with `edits` onto `base` would produce (first parent = base tip)."""
	git(repo, "checkout", "-q", "-B", "feature", f"origin/{base}")
	for rel, text in edits.items():
		write(repo, rel, text)
	commit(repo, "pr change")
	git(repo, "checkout", "-q", "--detach", f"origin/{base}")
	git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature")
	return git(repo, "rev-parse", "HEAD^1")


def _move_origin_main(repo: Path, edits: dict[str, str]) -> str:
	git(repo, "checkout", "-q", "main")
	for rel, text in edits.items():
		write(repo, rel, text)
	sha = commit(repo, "main moves")
	git(repo, "push", "-q", "origin", "main")
	return sha


def test_ci_step_pr_into_stable_fails_on_a_hook_changed_without_its_twin(repo, ci):
	git(repo, "fetch", "-q", "origin")
	_pr_merge(repo, "stable", {claude("hooks/h.py"): "loosened\n"})
	proc = ci("pull_request", base_ref="stable", ref_name="42/merge")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "::error file=.claude/hooks/h.py::" in proc.stdout
	assert proc.gh_calls == []


def test_ci_step_pr_into_stable_fails_on_a_guard_change_main_does_not_carry(repo, ci):
	git(repo, "fetch", "-q", "origin")
	_pr_merge(repo, "stable", {claude("settings.json"): '{"allow": ["*"]}\n', twin("settings.json"): '{"allow": ["*"]}\n'})
	proc = ci("pull_request", base_ref="stable", ref_name="42/merge")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "not on the default branch" in proc.stdout


def test_ci_step_pr_into_stable_allows_a_backport_of_main(repo, ci):
	_move_origin_main(repo, {claude("hooks/h.py"): "h v2\n", twin("hooks/h.py"): "h v2\n"})
	git(repo, "fetch", "-q", "origin")
	_pr_merge(repo, "stable", {claude("hooks/h.py"): "h v2\n", twin("hooks/h.py"): "h v2\n"})
	proc = ci("pull_request", base_ref="stable", ref_name="42/merge")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert '"ok": true' in proc.stdout


def test_ci_step_pr_into_main_keeps_the_twin_rule_only(repo, ci):
	git(repo, "fetch", "-q", "origin")
	_pr_merge(repo, "main", {claude("hooks/h.py"): "h v2\n", twin("hooks/h.py"): "h v2\n"})
	proc = ci("pull_request", base_ref="main", ref_name="42/merge")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert '"guard_provenance_ref": null' in proc.stdout


def _stable_push(repo: Path, edits: dict[str, str]) -> str:
	"""Check out a new stable commit with `edits`; return the push's `before`."""
	git(repo, "fetch", "-q", "origin")
	before = git(repo, "rev-parse", "origin/stable")
	git(repo, "checkout", "-q", "--detach", before)
	for rel, text in edits.items():
		write(repo, rel, text)
	commit(repo, "stable push")
	return before


def test_ci_step_skips_a_promotion_push_to_stable(repo, ci):
	git(repo, "fetch", "-q", "origin")
	before = git(repo, "rev-parse", "origin/stable")
	# A range that would fail: .claude/ moved without its twin on main.
	head = _move_origin_main(repo, {claude("hooks/h.py"): "direct\n"})
	git(repo, "checkout", "-q", "--detach", head)
	proc = ci("push", ref_name="stable", before=before, gh_status="ahead")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "a promotion; skipping" in proc.stdout
	assert proc.gh_calls == [f"api repos/owner/repo/compare/{head}...main?per_page=1 --jq .status"]


def test_ci_step_checks_a_push_to_stable_that_is_not_on_main(repo, ci):
	before = _stable_push(repo, {claude("hooks/h.py"): "h v2\n", twin("hooks/h.py"): "h v2\n"})
	proc = ci("push", ref_name="stable", before=before, gh_status="diverged")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "compare status: diverged" in proc.stdout
	assert "not on the default branch" in proc.stdout


def test_ci_step_checks_a_push_to_stable_when_the_lookup_fails(repo, ci):
	before = _stable_push(repo, {claude("hooks/h.py"): "loosened\n"})
	proc = ci("push", ref_name="stable", before=before, gh_status="")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "compare status: lookup failed" in proc.stdout


def test_ci_step_allows_a_clean_push_to_stable(repo, ci):
	before = _stable_push(repo, {"CHANGELOG.md": "release notes\n"})
	proc = ci("push", ref_name="stable", before=before, gh_status="diverged")
	assert proc.returncode == 0, proc.stdout + proc.stderr


def test_ci_step_checks_a_push_that_creates_stable_against_main(repo, ci):
	_stable_push(repo, {claude("hooks/h.py"): "h v2\n", twin("hooks/h.py"): "h v2\n"})
	proc = ci("push", ref_name="stable", before=sync.ZERO_SHA, gh_status="diverged")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "push creates stable: no previous tip" in proc.stdout
	assert "not on the default branch" in proc.stdout


def test_ci_step_allows_a_clean_push_that_creates_stable(repo, ci):
	_stable_push(repo, {"CHANGELOG.md": "release notes\n"})
	proc = ci("push", ref_name="stable", before="", gh_status="diverged")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "push creates stable: no previous tip" in proc.stdout
	assert '"ok": true' in proc.stdout


def test_ci_step_skips_other_events(repo, ci):
	proc = ci("pull_request", base_ref="release/x", ref_name="42/merge")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "skipping" in proc.stdout and proc.gh_calls == []


def test_workflow_triggers():
	on = _workflow()["on"]
	assert on["push"]["branches"] == ["main"]
	assert on["push"]["paths"] == ["workflow-templates/.claude/**"]
	assert on["schedule"] and "workflow_dispatch" in on
	assert on["workflow_run"]["workflows"] == ["CI"]
	assert on["workflow_run"]["types"] == ["completed"]
	assert on["workflow_run"]["branches"] == ["claude/claude-twin-sync-*"]
	assert set(on["pull_request_review"]["types"]) == {"submitted", "dismissed"}


def test_workflow_is_least_privilege_and_pinned():
	text = WORKFLOW.read_text(encoding="utf-8")
	wf = _workflow()
	assert wf["permissions"] == {"contents": "read", "pull-requests": "read", "checks": "read", "statuses": "read"}
	for job in wf["jobs"].values():
		assert job["if"].startswith("github.repository == 'shubhodeep1/coding-workflows'")
		for step in job["steps"]:
			uses = step.get("uses")
			if uses:
				assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), uses
				assert step["with"]["persist-credentials"] is False
	for session_var in ("FUNTOKEN_IO_CF", "FT_GAMES_CF", "DIGITALOCEAN_ACCESS_TOKEN"):
		assert session_var not in text
	assert text.count("secrets.GH_PAT") == 1
	assert "--auto" not in text and "APPROVE" not in text
	assert "fetch-depth: 0" in text


def test_workflow_passes_gh_pat_only_to_the_sync_step():
	for job in _workflow()["jobs"].values():
		for step in job["steps"]:
			env = step.get("env") or {}
			if "CLAUDE_TWIN_SYNC_WRITE_TOKEN" in env:
				assert env["CLAUDE_TWIN_SYNC_WRITE_TOKEN"] == "${{ secrets.GH_PAT }}"
				assert env["CLAUDE_TWIN_SYNC_READ_TOKEN"] == "${{ github.token }}"
				assert "scripts/claude_twin_sync.py run" in step["run"]
			else:
				assert "GH_PAT" not in json.dumps(step)
		assert "GH_PAT" not in json.dumps(job.get("env") or {})


# --- docs --------------------------------------------------------------------


def _flat_doc(path: Path) -> str:
	return " ".join(path.read_text(encoding="utf-8").split())


def test_claude_md_documents_twin_first_and_the_sync():
	text = _flat_doc(ROOT / "CLAUDE.md")
	assert "unattended sessions **edit the twin, never `.claude/**`** (twin-first, issue #4785)" in text
	assert "`.github/workflows/claude-twin-sync.yml` (`scripts/claude_twin_sync.py`)" in text
	assert "merged only by the repository owner: the workflow never approves or merges it" in text
	assert "The one exception is a Claude twin sync PR (`claude/claude-twin-sync-*`, §28.C)" in text


@pytest.mark.parametrize("name", ["implement-plan-claude.md", "implement-issue-claude.md", "fix-claude-pr.md"])
def test_command_twins_say_edit_the_twin(name):
	text = _flat_doc(ROOT / sync.TWIN_ROOT / "commands" / name)
	assert "**Edit the twin, never `.claude/**`.**" in text
	assert "claude-twin-sync.yml" in text
	# The twins ship to consumers, which have no workflow-templates/.claude/ and keep the protected-path stop (G1).
	assert "`workflow-templates/.claude/` (coding-workflows)" in text
	assert "(a consumer" in text


def test_approval_label_is_in_the_label_contract():
	contract = json.loads((ROOT / ".github" / "ai" / "label_contract.v1.json").read_text(encoding="utf-8"))
	label = contract["labels"][sync.APPROVAL_LABEL]
	assert label == {"color": sync.APPROVAL_LABEL_COLOR, "description": sync.APPROVAL_LABEL_DESCRIPTION}
	assert len(sync.APPROVAL_LABEL_DESCRIPTION) <= 100
