#!/usr/bin/env python3
"""Contract for `.claude/scripts/dispatch_workflow.py` (CLAUDE.md §23.I helpers).

Covers the allowlist, the dispatch body, finding the run the dispatch started
(never an older one), the timeout, argument validation, template parity, and
the allow rules in settings.json.
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
# The default-branch pin for internal-review.yml (issue #5375) is tested against
# the workflow-templates twin, which is edited first; test_template_parity keeps
# the live copy equal to it.
dw_twin = _load(TEMPLATE_SCRIPT_PATH, "dispatch_workflow_twin")


class FakeGitHub:
	"""Stands in for `gh api`: serves run lists in sequence and records the dispatch."""

	def __init__(self, run_lists, default_branch="main"):
		self.run_lists = list(run_lists)
		self.default_branch = default_branch
		self.reads: list[str] = []
		self.dispatches: list[tuple[str, str, str, dict]] = []

	def gh_api(self, path):
		self.reads.append(path)
		if "/actions/workflows/" in path:
			runs = self.run_lists.pop(0) if len(self.run_lists) > 1 else self.run_lists[0]
			return {"workflow_runs": runs}
		return {"default_branch": self.default_branch}

	def post_dispatch(self, repo, workflow, ref, inputs):
		self.dispatches.append((repo, workflow, ref, inputs))


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


def _run(run_id, status="queued"):
	return {"id": run_id, "html_url": f"https://github.com/o/r/actions/runs/{run_id}", "status": status, "created_at": "2026-09-27T03:00:00Z"}


def test_returns_the_new_run_not_the_previous_one(fake):
	github = fake([[_run(100, "completed")], [_run(100, "completed")], [_run(101), _run(100, "completed")]])
	code, result = dw.dispatch("o/r", "security-audit.yml", None, {"ref": "claude/x"}, 90, sleep=lambda _s: None)
	assert code == 0
	assert result["run_id"] == 101
	assert result["ref"] == "main"
	assert github.dispatches == [("o/r", "security-audit.yml", "main", {"ref": "claude/x"})]


def test_explicit_ref_skips_the_repository_read(fake):
	github = fake([[], [_run(7)]])
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

	def flaky_find(repo, workflow, known_ids):
		calls["n"] += 1
		raise dw.check_in_status.ReadError("proxy 502")

	monkeypatch.setattr(dw, "find_new_run", flaky_find)
	code, result = dw.dispatch("o/r", "security-audit.yml", "main", {}, 90, sleep=lambda _s: None)
	assert code == 2
	assert result["dispatched"] is True
	assert "proxy 502" in result["error"] and "before dispatching again" in result["error"]
	assert len(github.dispatches) == 1 and calls["n"] == 1


def test_read_failure_before_the_post_reports_dispatched_false(monkeypatch, capsys):
	def boom(path):
		raise dw.check_in_status.ReadError("proxy 403")

	posted = []
	monkeypatch.setattr(dw.check_in_status, "gh_api", boom)
	monkeypatch.setattr(dw, "_post_dispatch", lambda *args: posted.append(args))
	assert dw.main(["--repo", "o/r", "--workflow", "security-audit.yml", "--ref", "main"]) == 2
	assert json.loads(capsys.readouterr().out)["dispatched"] is False
	assert posted == []


def test_main_keeps_dispatched_true_on_a_failed_poll(fake, monkeypatch, capsys):
	fake([[_run(1)]])

	def flaky_find(repo, workflow, known_ids):
		raise dw.check_in_status.ReadError("proxy 502")

	monkeypatch.setattr(dw, "find_new_run", flaky_find)
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
	dw._post_dispatch("o/r", "security-audit.yml", "main", {"ref": "claude/x"})
	assert captured["argv"][:5] == ["gh", "api", "-X", "POST", "repos/o/r/actions/workflows/security-audit.yml/dispatches"]
	assert captured["body"] == {"ref": "main", "inputs": {"ref": "claude/x"}}


def test_main_reports_invalid_workflow_with_exit_1(capsys):
	assert dw.main(["--repo", "o/r", "--workflow", "release.yml"]) == 1
	assert json.loads(capsys.readouterr().out)["dispatched"] is False


@pytest.mark.parametrize("path,script_path", list(zip(SETTINGS_PATHS, [SCRIPT_PATH, TEMPLATE_SCRIPT_PATH])))
def test_allowlist_matches_the_gh_workflow_run_allow_rules(path, script_path):
	module = _load(script_path, f"dispatch_workflow_{script_path.parent.parent.parent.name}")
	allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
	allowlisted = {
		rule[len("Bash(gh workflow run ") : -len(" *)")]
		for rule in allow
		if rule.startswith("Bash(gh workflow run ") and rule.endswith(" *)")
	}
	# A default-branch-only workflow has no allow rule: `gh workflow run` takes
	# any `--ref`, so the helper is its only pre-approved path (issue #5375).
	assert module.DEFAULT_BRANCH_ONLY_WORKFLOWS <= module.DISPATCHABLE_WORKFLOWS
	assert allowlisted == set(module.DISPATCHABLE_WORKFLOWS - module.DEFAULT_BRANCH_ONLY_WORKFLOWS)
	assert not any("internal-review.yml" in rule for rule in allow)
	assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/dispatch_workflow.py *)" in allow


# ──────────────────────────────────────────────────────────────────
# internal-review.yml: default branch only, numeric pr_number (issue #5375)
# ──────────────────────────────────────────────────────────────────


def test_internal_review_dispatches_on_the_default_branch(fake_twin):
	github = fake_twin([[_run(1)], [_run(1), _run(2)]], default_branch="trunk")
	code, result = dw_twin.dispatch("o/r", "internal-review.yml", None, {"pr_number": "123"}, 90, sleep=lambda _s: None)
	assert code == 0 and result["run_id"] == 2 and result["ref"] == "trunk"
	assert github.dispatches == [("o/r", "internal-review.yml", "trunk", {"pr_number": "123"})]
	assert github.reads[0] == "repos/o/r"


def test_internal_review_accepts_the_default_branch_as_explicit_ref(fake_twin):
	github = fake_twin([[], [_run(7)]])
	code, result = dw_twin.dispatch("o/r", "internal-review.yml", "main", {"pr_number": "5"}, 90, sleep=lambda _s: None)
	assert code == 0 and result["ref"] == "main"
	# The default branch is always read from the API, even when --ref names it.
	assert github.reads[0] == "repos/o/r"
	assert github.dispatches == [("o/r", "internal-review.yml", "main", {"pr_number": "5"})]


@pytest.mark.parametrize("ref", ["claude/unreviewed-head", "refs/heads/main", "Main", "main ", "stable"])
def test_internal_review_refuses_any_other_ref_before_the_post(fake_twin, ref):
	github = fake_twin([[]])
	with pytest.raises(ValueError, match="only from the default branch"):
		dw_twin.dispatch("o/r", "internal-review.yml", ref, {"pr_number": "5"}, 90, sleep=lambda _s: None)
	assert github.dispatches == []
	assert github.reads == ["repos/o/r"]


@pytest.mark.parametrize(
	"inputs",
	[
		{},
		{"pr_number": ""},
		{"pr_number": "0"},
		{"pr_number": "007"},
		{"pr_number": "-5"},
		{"pr_number": "+5"},
		{"pr_number": "5 "},
		{"pr_number": "5\n"},
		{"pr_number": "12345678901"},
		{"pr_number": "5; rm -rf /"},
		{"pr_number": "${{ secrets.GH_PAT }}"},
		{"pr_number": "5", "allow_workflow_edits": "true"},
		{"pr_number": "5", "ref": "claude/x"},
	],
)
def test_internal_review_refuses_bad_inputs_before_any_call(fake_twin, inputs):
	github = fake_twin([[]])
	with pytest.raises(ValueError, match="pr_number"):
		dw_twin.dispatch("o/r", "internal-review.yml", None, inputs, 90, sleep=lambda _s: None)
	assert github.reads == [] and github.dispatches == []


def test_other_workflows_keep_an_explicit_ref(fake_twin):
	github = fake_twin([[], [_run(3)]])
	code, result = dw_twin.dispatch("o/r", "review_autofix.yml", "claude/x", {"pr_number": "5"}, 90, sleep=lambda _s: None)
	assert code == 0 and result["ref"] == "claude/x"
	assert not any(read == "repos/o/r" for read in github.reads)


def test_main_reports_a_refused_internal_review_ref_with_exit_1(fake_twin, capsys):
	github = fake_twin([[]])
	argv = ["--repo", "o/r", "--workflow", "internal-review.yml", "--ref", "claude/x", "--input", "pr_number=5"]
	assert dw_twin.main(argv) == 1
	result = json.loads(capsys.readouterr().out)
	assert result["dispatched"] is False and "default branch" in result["error"]
	assert github.dispatches == []


def test_internal_review_unreadable_default_branch_is_not_dispatched(fake_twin):
	github = fake_twin([[]], default_branch="")
	with pytest.raises(dw_twin.check_in_status.ReadError):
		dw_twin.dispatch("o/r", "internal-review.yml", None, {"pr_number": "5"}, 90, sleep=lambda _s: None)
	assert github.dispatches == []


def test_template_parity():
	assert TEMPLATE_SCRIPT_PATH.read_text(encoding="utf-8") == SCRIPT_PATH.read_text(encoding="utf-8")
