#!/usr/bin/env python3
"""Contract for `.claude/scripts/dispatch_workflow.py` (CLAUDE.md §23.I helpers).

Covers the allowlist, the dispatch body, taking the run id GitHub returns for
the dispatch (issue #5016: never another session's run), the polling fallback
(never an older run, never a guess between several), the timeout, argument
validation, template parity, and the allow rules in settings.json.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / ".claude" / "scripts" / "dispatch_workflow.py"
TEMPLATE_SCRIPT_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "scripts" / "dispatch_workflow.py"
SETTINGS_PATHS = [REPO_ROOT / ".claude" / "settings.json", REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"]


def _load(path=SCRIPT_PATH, name="dispatch_workflow"):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


dw = _load()
# Issue #5841 changed the twin first (CLAUDE.md §28.C interim twin-first
# default); its tests load the twin, which `.claude/` matches after the
# `[claude-twin-sync]` copy (test_template_parity).
dw_twin = _load(TEMPLATE_SCRIPT_PATH, "dispatch_workflow_twin")


class FakeGitHub:
	"""Stands in for `gh api`: serves run lists in sequence and records the dispatch."""

	def __init__(self, run_lists, default_branch="main", response=None, run_reads=None, run_list_error=None):
		self.run_lists = list(run_lists)
		self.run_list_error = run_list_error
		self.default_branch = default_branch
		self.response = response
		self.run_reads = run_reads or {}
		self.reads: list[str] = []
		self.dispatches: list[tuple[str, str, str, dict]] = []

	def gh_api(self, path):
		self.reads.append(path)
		if "/actions/workflows/" in path:
			if self.run_list_error is not None:
				raise self.run_list_error
			runs = self.run_lists.pop(0) if len(self.run_lists) > 1 else self.run_lists[0]
			return {"workflow_runs": runs}
		if "/actions/runs/" in path:
			run = self.run_reads.get(int(path.rsplit("/", 1)[1]))
			if isinstance(run, Exception):
				raise run
			if run is None:
				raise dw.check_in_status.ReadError("HTTP 404")
			return run
		return {"default_branch": self.default_branch}

	def post_dispatch(self, repo, workflow, ref, inputs):
		self.dispatches.append((repo, workflow, ref, inputs))
		return self.response


@pytest.fixture
def fake(monkeypatch):
	def install(run_lists, **kwargs):
		github = FakeGitHub(run_lists, **kwargs)
		monkeypatch.setattr(dw.check_in_status, "gh_api", github.gh_api)
		monkeypatch.setattr(dw, "_post_dispatch", github.post_dispatch)
		return github

	return install


@pytest.fixture
def fake_twin(monkeypatch):
	def install(run_lists, **kwargs):
		github = FakeGitHub(run_lists, **kwargs)
		monkeypatch.setattr(dw_twin.check_in_status, "gh_api", github.gh_api)
		monkeypatch.setattr(dw_twin, "_post_dispatch", github.post_dispatch)
		return github

	return install


def _run(run_id, status="queued", head_branch="main"):
	return {
		"id": run_id,
		"html_url": f"https://github.com/o/r/actions/runs/{run_id}",
		"status": status,
		"created_at": "2026-09-27T03:00:00Z",
		"head_branch": head_branch,
	}


def test_returns_the_new_run_not_the_previous_one(fake):
	github = fake([[_run(100, "completed")], [_run(100, "completed")], [_run(101), _run(100, "completed")]])
	code, result = dw.dispatch("o/r", "security-audit.yml", None, {"ref": "claude/x"}, 90, sleep=lambda _s: None)
	assert code == 0
	assert result["run_id"] == 101
	assert result["matched_by"] == "new_run"
	assert result["ref"] == "main"
	assert github.dispatches == [("o/r", "security-audit.yml", "main", {"ref": "claude/x"})]


def _response(run_id):
	return {
		"workflow_run_id": run_id,
		"run_url": f"https://api.github.com/repos/o/r/actions/runs/{run_id}",
		"html_url": f"https://github.com/o/r/actions/runs/{run_id}",
	}


def test_uses_the_run_id_github_returned_even_when_a_newer_run_raced_in(fake):
	# Issue #5016: two sessions dispatched the same workflow 3 s apart and both
	# took the newest new run. The response id names this dispatch's run.
	sleeps: list[int] = []
	github = fake(
		[[_run(100, "completed")], [_run(202), _run(201), _run(100, "completed")]],
		response=_response(201),
		run_reads={201: _run(201, "queued")},
	)
	code, result = dw.dispatch("o/r", "security-audit.yml", "main", {"ref": "claude/a"}, 90, sleep=sleeps.append)
	assert code == 0
	assert result["run_id"] == 201
	assert result["matched_by"] == "dispatch_response"
	assert result["html_url"] == "https://github.com/o/r/actions/runs/201"
	assert result["status"] == "queued" and result["created_at"] == "2026-09-27T03:00:00Z"
	assert "ambiguous" not in result
	assert sleeps == []
	assert github.reads == ["repos/o/r/actions/workflows/security-audit.yml/runs?event=workflow_dispatch&per_page=20", "repos/o/r/actions/runs/201"]


def test_response_id_stands_when_the_run_read_fails(fake):
	fake([[]], response=_response(7), run_reads={7: dw.check_in_status.ReadError("proxy 502")})
	code, result = dw.dispatch("o/r", "internal-validate.yml", "main", {}, 90, sleep=lambda _s: None)
	assert code == 0
	assert result["run_id"] == 7 and result["matched_by"] == "dispatch_response"
	assert result["html_url"] == "https://github.com/o/r/actions/runs/7"
	assert result["status"] is None and result["created_at"] is None


@pytest.mark.parametrize("response", [None, {}, {"workflow_run_id": "7"}, {"workflow_run_id": 0}, {"workflow_run_id": True}])
def test_response_without_a_usable_run_id_falls_back_to_polling(fake, response):
	github = fake([[], [_run(9)]], response=response)
	code, result = dw.dispatch("o/r", "ai-validate.yml", "main", {}, 90, sleep=lambda _s: None)
	assert code == 0
	assert result["run_id"] == 9 and result["matched_by"] == "new_run"
	assert not any("/actions/runs/" in path for path in github.reads)


def test_fallback_never_guesses_between_several_new_runs(fake):
	github = fake([[_run(100, "completed")], [_run(202), _run(201), _run(100, "completed")]])
	code, result = dw.dispatch("o/r", "security-audit.yml", "main", {"ref": "claude/a"}, 90, sleep=lambda _s: None)
	assert code == 2
	assert result["dispatched"] is True
	assert result["ambiguous"] is True
	assert result["candidate_run_ids"] == [202, 201]
	assert "run_id" not in result
	assert "dispatch again" in result["error"]
	assert len(github.dispatches) == 1


# Issue #5841: a run another session started from a different ref runs that
# ref's copy of the workflow file, so the fallback must never take it as this
# dispatch's run, alone or as an ambiguous candidate.
def test_fallback_ignores_a_new_run_dispatched_from_another_ref(fake_twin):
	github = fake_twin([[_run(100, "completed")], [_run(301, head_branch="evil"), _run(100, "completed")], [_run(302), _run(301, head_branch="evil"), _run(100, "completed")]])
	sleeps: list[int] = []
	code, result = dw_twin.dispatch("o/r", "security-audit.yml", "main", {"ref": "claude/a"}, 90, sleep=sleeps.append)
	assert code == 0
	assert result["run_id"] == 302 and result["matched_by"] == "new_run"
	assert "ambiguous" not in result
	assert len(sleeps) == 2 and len(github.dispatches) == 1


def test_fallback_candidates_exclude_runs_from_another_ref(fake_twin):
	fake_twin([[], [_run(403), _run(402, head_branch="claude/other"), _run(401)]])
	code, result = dw_twin.dispatch("o/r", "internal-validate.yml", "main", {"target_ref": "claude/a"}, 90, sleep=lambda _s: None)
	assert code == 2
	assert result["ambiguous"] is True and result["candidate_run_ids"] == [403, 401]
	assert "dispatched from main" in result["error"] and "audited commit" in result["error"]
	# PR #5860 review round 1 (head 952aa92): the read-result stage may re-dispatch once after
	# every candidate completed without a match, so the helper bans a new
	# dispatch only while a candidate could still be this one.
	assert "do not dispatch again while any candidate could still be this dispatch's run" in result["error"]
	assert "never dispatch again" not in result["error"]


def test_fallback_times_out_when_only_other_refs_start_runs(fake_twin):
	fake_twin([[], [_run(501, head_branch="stable")]])
	sleeps: list[int] = []
	code, result = dw_twin.dispatch("o/r", "ai-validate.yml", "main", {}, 15, sleep=sleeps.append)
	assert code == 2
	assert result["dispatched"] is True and "run_id" not in result
	assert "no new ai-validate.yml run dispatched from main" in result["error"]
	assert sleeps == [dw_twin.POLL_INTERVAL_SECONDS] * 3


def test_fallback_drops_a_run_without_head_branch(fake_twin):
	run_without_branch = {"id": 601, "status": "queued"}
	fake_twin([[], [run_without_branch]])
	code, result = dw_twin.dispatch("o/r", "ai-validate.yml", "main", {}, 5, sleep=lambda _s: None)
	assert code == 2 and "run_id" not in result


def test_new_runs_keeps_every_new_run_without_a_dispatched_ref(fake_twin):
	# The keyword is optional, so callers of the old signature see every new run.
	fake_twin([[_run(3), _run(5, head_branch="stable"), _run(4)]])
	assert [run["id"] for run in dw_twin.new_runs("o/r", "security-audit.yml", {3})] == [5, 4]
	assert [run["id"] for run in dw_twin.new_runs("o/r", "security-audit.yml", {3}, dispatched_ref="main")] == [4]
	assert dw_twin.find_new_run("o/r", "security-audit.yml", {3})["id"] == 5


def test_explicit_ref_skips_the_repository_read(fake):
	github = fake([[], [_run(7, head_branch="stable")]])
	code, result = dw.dispatch("o/r", "internal-validate.yml", "stable", {}, 90, sleep=lambda _s: None)
	assert code == 0 and result["run_id"] == 7
	assert all("/actions/workflows/" in path for path in github.reads)
	assert github.dispatches[0][2] == "stable"


def test_times_out_without_a_new_run(fake):
	fake([[_run(5)]])
	sleeps: list[int] = []
	code, result = dw.dispatch("o/r", "ai-validate.yml", "main", {}, 15, sleep=sleeps.append)
	assert code == 2
	assert result["dispatched"] is True
	assert "no new ai-validate.yml run" in result["error"]
	assert sleeps == [dw.POLL_INTERVAL_SECONDS] * 3


def test_poll_failure_after_the_post_reports_dispatched_true(fake, monkeypatch):
	# The run exists once the POST succeeded: a failed read afterwards must never
	# look like a failed dispatch, or the caller dispatches a duplicate run.
	github = fake([[_run(1)]])
	calls = {"n": 0}

	def flaky_find(repo, workflow, known_ids, dispatched_ref=None):
		calls["n"] += 1
		raise dw.check_in_status.ReadError("proxy 502")

	monkeypatch.setattr(dw, "new_runs", flaky_find)
	code, result = dw.dispatch("o/r", "security-audit.yml", "main", {}, 90, sleep=lambda _s: None)
	assert code == 2
	assert result["dispatched"] is True
	assert "proxy 502" in result["error"] and "before dispatching again" in result["error"]
	assert len(github.dispatches) == 1 and calls["n"] == 1


def test_read_failure_before_the_post_reports_dispatched_false(monkeypatch, capsys):
	# Without --ref the default branch must be read first: a failed read means
	# nothing was sent, so the caller may dispatch again.
	def boom(path):
		raise dw.check_in_status.ReadError("proxy 403")

	posted = []
	monkeypatch.setattr(dw.check_in_status, "gh_api", boom)
	monkeypatch.setattr(dw, "_post_dispatch", lambda *args: posted.append(args))
	assert dw.main(["--repo", "o/r", "--workflow", "security-audit.yml"]) == 2
	assert json.loads(capsys.readouterr().out)["dispatched"] is False
	assert posted == []


def test_run_list_read_failure_does_not_cancel_a_dispatch_github_names(fake):
	# PR #5059 review round 1: the pre-dispatch run list only serves the
	# polling fallback, so its failure must not stop a dispatch whose run id
	# GitHub returns.
	sleeps: list[int] = []
	github = fake([[]], response=_response(31), run_reads={31: _run(31)}, run_list_error=dw.check_in_status.ReadError("proxy 502"))
	code, result = dw.dispatch("o/r", "security-audit.yml", "main", {"ref": "claude/a"}, 90, sleep=sleeps.append)
	assert code == 0
	assert result["run_id"] == 31 and result["matched_by"] == "dispatch_response"
	assert result["status"] == "queued"
	assert len(github.dispatches) == 1 and sleeps == []


def test_run_list_read_failure_without_a_response_id_never_guesses(fake, monkeypatch, capsys):
	# No run id and no pre-dispatch list: every recent run looks new, so the
	# helper reports the dispatch as sent and polls nothing.
	sleeps: list[int] = []
	monkeypatch.setattr(dw.time, "sleep", sleeps.append)
	github = fake([[_run(40)]], response=None, run_list_error=dw.check_in_status.ReadError("proxy 502"))
	assert dw.main(["--repo", "o/r", "--workflow", "internal-validate.yml", "--ref", "main"]) == 2
	result = json.loads(capsys.readouterr().out)
	assert result["dispatched"] is True
	assert "run_id" not in result and "matched_by" not in result
	assert "proxy 502" in result["error"] and "never dispatch again" in result["error"]
	assert len(github.dispatches) == 1 and sleeps == []
	assert github.reads == ["repos/o/r/actions/workflows/internal-validate.yml/runs?event=workflow_dispatch&per_page=20"]


def test_main_keeps_dispatched_true_on_a_failed_poll(fake, monkeypatch, capsys):
	fake([[_run(1)]])

	def flaky_find(repo, workflow, known_ids, dispatched_ref=None):
		raise dw.check_in_status.ReadError("proxy 502")

	monkeypatch.setattr(dw, "new_runs", flaky_find)
	monkeypatch.setattr(dw.time, "sleep", lambda _s: None)
	assert dw.main(["--repo", "o/r", "--workflow", "security-audit.yml", "--ref", "main"]) == 2
	assert json.loads(capsys.readouterr().out)["dispatched"] is True


def _failing_post(monkeypatch, *, returncode=1, stderr="", raises=None):
	"""Make `gh api -X POST` fail; return the list of payload paths it was handed."""
	seen = []

	class Proc:
		stdout = ""

	def fake_run(argv, **kwargs):
		seen.append(argv[argv.index("--input") + 1])
		if raises is not None:
			raise raises
		proc = Proc()
		proc.returncode = returncode
		proc.stderr = stderr
		return proc

	monkeypatch.setattr(dw.subprocess, "run", fake_run)
	return seen


@pytest.mark.parametrize(
	"failure",
	[
		{"raises": dw.subprocess.TimeoutExpired(["gh"], 60)},
		{"stderr": "gh: Server Error (HTTP 502)"},
		{"stderr": "Post \"https://api.github.com/...\": connection reset by peer"},
	],
)
def test_post_that_may_have_landed_reports_dispatched_true(monkeypatch, capsys, failure):
	# GitHub can accept the POST before a timeout, a 5xx or a dropped
	# connection, so the caller must look for the run, not dispatch again.
	monkeypatch.setattr(dw.check_in_status, "gh_api", lambda path: {"workflow_runs": [_run(1)]})
	seen = _failing_post(monkeypatch, **failure)
	assert dw.main(["--repo", "o/r", "--workflow", "security-audit.yml", "--ref", "main"]) == 2
	result = json.loads(capsys.readouterr().out)
	assert result["dispatched"] is True
	assert "may have reached GitHub" in result["error"] and "before dispatching again" in result["error"]
	assert len(seen) == 1 and not Path(seen[0]).exists()


@pytest.mark.parametrize("failure", [{"stderr": "gh: Unexpected inputs provided: [\"x\"] (HTTP 422)"}, {"raises": FileNotFoundError("gh")}])
def test_post_github_refused_or_never_sent_reports_dispatched_false(monkeypatch, capsys, failure):
	monkeypatch.setattr(dw.check_in_status, "gh_api", lambda path: {"workflow_runs": [_run(1)]})
	seen = _failing_post(monkeypatch, **failure)
	assert dw.main(["--repo", "o/r", "--workflow", "security-audit.yml", "--ref", "main"]) == 2
	assert json.loads(capsys.readouterr().out)["dispatched"] is False
	assert len(seen) == 1 and not Path(seen[0]).exists()


def test_payload_file_is_removed_when_serialisation_fails(monkeypatch):
	created = []
	real = dw.tempfile.NamedTemporaryFile

	def tracking(*args, **kwargs):
		handle = real(*args, **kwargs)
		created.append(handle.name)
		return handle

	monkeypatch.setattr(dw.tempfile, "NamedTemporaryFile", tracking)
	monkeypatch.setattr(dw.subprocess, "run", lambda *a, **k: pytest.fail("gh must not run"))
	with pytest.raises(dw.check_in_status.ReadError) as excinfo:
		dw._post_dispatch("o/r", "security-audit.yml", "main", {"key": object()})
	assert not isinstance(excinfo.value, dw.DispatchUnconfirmed)
	assert len(created) == 1 and not Path(created[0]).exists()


def test_payload_file_is_written_as_utf8():
	assert 'NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")' in SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("workflow", ["release.yml", "../security-audit.yml", "security-audit.yml/../x", ""])
def test_refuses_workflows_outside_the_allowlist(fake, workflow):
	github = fake([[]])
	with pytest.raises(ValueError):
		dw.dispatch("o/r", workflow, "main", {}, 90, sleep=lambda _s: None)
	assert github.reads == [] and github.dispatches == []


@pytest.mark.parametrize("repo", ["o", "o/r/x", "o r/x", ""])
def test_refuses_bad_repo(fake, repo):
	fake([[]])
	with pytest.raises(ValueError):
		dw.dispatch(repo, "security-audit.yml", "main", {}, 90, sleep=lambda _s: None)


def test_parse_inputs():
	assert dw.parse_inputs(["ref=claude/x", "pr_number=0", "empty="]) == {"ref": "claude/x", "pr_number": "0", "empty": ""}
	for bad in (["noequals"], ["=v"], ["bad key=v"], ["a=1", "a=2"]):
		with pytest.raises(ValueError):
			dw.parse_inputs(bad)


def test_post_dispatch_sends_ref_and_inputs_as_json(monkeypatch):
	captured = {}

	class Proc:
		returncode = 0
		stdout = ""
		stderr = ""

	def fake_run(argv, **kwargs):
		captured["argv"] = argv
		captured["body"] = json.loads(Path(argv[argv.index("--input") + 1]).read_text())
		return Proc()

	monkeypatch.setattr(dw.subprocess, "run", fake_run)
	assert dw._post_dispatch("o/r", "security-audit.yml", "main", {"ref": "claude/x"}) is None
	assert captured["argv"][:5] == ["gh", "api", "-X", "POST", "repos/o/r/actions/workflows/security-audit.yml/dispatches"]
	assert captured["body"] == {"ref": "main", "return_run_details": True, "inputs": {"ref": "claude/x"}}


@pytest.mark.parametrize(
	("stdout", "expected"),
	[
		('{"workflow_run_id": 5, "html_url": "u"}', {"workflow_run_id": 5, "html_url": "u"}),
		("", None),
		("not json", None),
		("[1, 2]", None),
	],
)
def test_post_dispatch_returns_the_response_object_or_none(monkeypatch, stdout, expected):
	class Proc:
		returncode = 0
		stderr = ""

	Proc.stdout = stdout
	monkeypatch.setattr(dw.subprocess, "run", lambda argv, **kwargs: Proc())
	assert dw._post_dispatch("o/r", "security-audit.yml", "main", {}) == expected


def test_find_new_run_still_returns_the_newest_new_run(fake):
	fake([[_run(3), _run(5), _run(4)]])
	assert dw.find_new_run("o/r", "security-audit.yml", {3})["id"] == 5
	assert dw.find_new_run("o/r", "security-audit.yml", {3, 4, 5}) is None


def test_main_reports_invalid_workflow_with_exit_1(capsys):
	assert dw.main(["--repo", "o/r", "--workflow", "release.yml"]) == 1
	assert json.loads(capsys.readouterr().out)["dispatched"] is False


@pytest.mark.parametrize("path", SETTINGS_PATHS)
def test_allowlist_matches_the_gh_workflow_run_allow_rules(path):
	allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
	allowlisted = {
		rule[len("Bash(gh workflow run ") : -len(" *)")]
		for rule in allow
		if rule.startswith("Bash(gh workflow run ") and rule.endswith(" *)")
	}
	assert allowlisted == set(dw.DISPATCHABLE_WORKFLOWS)
	assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/dispatch_workflow.py *)" in allow


def test_template_parity():
	assert TEMPLATE_SCRIPT_PATH.read_text(encoding="utf-8") == SCRIPT_PATH.read_text(encoding="utf-8")
