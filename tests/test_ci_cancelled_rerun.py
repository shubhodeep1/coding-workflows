"""Contract for scripts/ci_cancelled_rerun.py — the review sweep's once-per-head
re-run of a PR's cancelled `CI` run (issue #4713) — and its wiring in
.github/workflows/review_autofix_sweep.yml and .github/workflows/ci.yml."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ci_cancelled_rerun", ROOT / "scripts" / "ci_cancelled_rerun.py")
rerunner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rerunner)

REPO = "o/r"
HEAD = "a" * 40
OLD_HEAD = "b" * 40
SWEEP_WF = ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
CI_WF = ROOT / ".github" / "workflows" / "ci.yml"
RUNS_PATH = f"repos/{REPO}/actions/workflows/ci.yml/runs?per_page=100"


def _pr(number=1, head=HEAD, ref="claude/x", repo=REPO, title="t", body=""):
	return {"number": number, "head_ref": ref, "head_repo": repo, "head_sha": head, "title": title, "body": body}


def _run(run_id, head=HEAD, status="completed", conclusion="cancelled", attempt=1, created="2026-09-28T01:00:00Z"):
	return {"id": run_id, "head_sha": head, "status": status, "conclusion": conclusion, "run_attempt": attempt, "created_at": created}


class FakeApi:
	def __init__(self, runs=None, get_error=None, refuse=()):
		self.runs = runs or []
		self.get_error = get_error
		self.refuse = set(refuse)
		self.gets: list[str] = []
		self.posts: list[str] = []

	def get(self, path):
		self.gets.append(path)
		if self.get_error:
			raise rerunner.ApiError(self.get_error)
		return {"total_count": len(self.runs), "workflow_runs": self.runs}

	def post(self, path):
		self.posts.append(path)
		if any(path.endswith(suffix) for suffix in self.refuse):
			raise rerunner.ApiError(f"refused {path}")


def _process(prs, api, **kwargs):
	kwargs.setdefault("enabled", True)
	return rerunner.process(REPO, prs, get=api.get, post=api.post, **kwargs)


def test_cancelled_on_attempt_one_is_rerun_once(capsys):
	api = FakeApi([_run(11)])
	summary = _process([_pr()], api)
	assert api.gets == [RUNS_PATH]
	assert api.posts == [f"repos/{REPO}/actions/runs/11/rerun-failed-jobs"]
	assert summary == {"candidates": 1, "rerun": 1, "skipped": 0, "errors": 0}
	assert f"CI_CANCELLED_RERUN pr=1 head={HEAD[:12]} run=11 action=rerun reason=cancelled endpoint=rerun-failed-jobs" in capsys.readouterr().out


def test_second_attempt_is_skipped(capsys):
	api = FakeApi([_run(11, attempt=2)])
	summary = _process([_pr()], api)
	assert api.posts == []
	assert summary["skipped"] == 1 and summary["rerun"] == 0
	assert "action=skip reason=already_rerun attempt=2" in capsys.readouterr().out


def test_failure_is_never_rerun(capsys):
	api = FakeApi([_run(11, conclusion="failure")])
	_process([_pr()], api)
	assert api.posts == []
	assert "action=skip reason=conclusion_failure" in capsys.readouterr().out


@pytest.mark.parametrize("conclusion", ["success", "timed_out", "action_required", "skipped", "neutral"])
def test_other_conclusions_are_skipped(conclusion):
	api = FakeApi([_run(11, conclusion=conclusion)])
	_process([_pr()], api)
	assert api.posts == []


def test_startup_failure_is_rerun():
	api = FakeApi([_run(11, conclusion="startup_failure")])
	summary = _process([_pr()], api)
	assert api.posts == [f"repos/{REPO}/actions/runs/11/rerun-failed-jobs"]
	assert summary["rerun"] == 1


def test_startup_failure_falls_back_to_full_rerun_when_failed_jobs_refused(capsys):
	api = FakeApi([_run(11, conclusion="startup_failure")], refuse=("/rerun-failed-jobs",))
	summary = _process([_pr()], api)
	assert api.posts == [f"repos/{REPO}/actions/runs/11/rerun-failed-jobs", f"repos/{REPO}/actions/runs/11/rerun"]
	assert summary["rerun"] == 1 and summary["errors"] == 0
	assert "reason=startup_failure endpoint=rerun" in capsys.readouterr().out


def test_cancelled_refusal_does_not_fall_back_and_fails_open(capsys):
	api = FakeApi([_run(11)], refuse=("/rerun-failed-jobs",))
	summary = _process([_pr()], api)
	assert api.posts == [f"repos/{REPO}/actions/runs/11/rerun-failed-jobs"]
	assert summary["errors"] == 1 and summary["rerun"] == 0
	assert "::warning::CI_CANCELLED_RERUN pr=1" in capsys.readouterr().out


def test_superseded_head_is_skipped(capsys):
	"""A run cancelled on an older head (a newer push superseded it) is never matched."""
	api = FakeApi([_run(11, head=OLD_HEAD)])
	_process([_pr(head=HEAD)], api)
	assert api.posts == []
	assert "action=skip reason=no_ci_run" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["queued", "in_progress", "pending", "waiting", "requested"])
def test_active_run_on_head_is_skipped(status, capsys):
	api = FakeApi([_run(12, status=status, conclusion=None, created="2026-09-28T02:00:00Z"), _run(11)])
	_process([_pr()], api)
	assert api.posts == []
	assert "run=12 action=skip reason=active_run" in capsys.readouterr().out


def test_newest_run_on_head_decides():
	api = FakeApi([_run(12, conclusion="success", created="2026-09-28T02:00:00Z"), _run(11, created="2026-09-28T01:00:00Z")])
	_process([_pr()], api)
	assert api.posts == []
	api = FakeApi([_run(12, created="2026-09-28T02:00:00Z"), _run(11, conclusion="success", created="2026-09-28T01:00:00Z")])
	_process([_pr()], api)
	assert api.posts == [f"repos/{REPO}/actions/runs/12/rerun-failed-jobs"]


def test_switch_off_skips_without_api_calls(capsys):
	api = FakeApi([_run(11)])
	summary = _process([_pr()], api, enabled=False)
	assert api.gets == [] and api.posts == []
	assert summary["rerun"] == 0
	assert "action=skip reason=disabled" in capsys.readouterr().out


@pytest.mark.parametrize("value,expected", [
	("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True), (" true ", True),
	("false", False), ("0", False), ("off", False), ("", False), (None, False), ("ture", False),
])
def test_switch_parsing(value, expected):
	assert rerunner.is_enabled(value) is expected


def test_dry_run_reports_without_rerun(capsys):
	api = FakeApi([_run(11)])
	_process([_pr()], api, dry_run=True)
	assert api.posts == []
	assert "action=skip reason=dry_run would_rerun=cancelled" in capsys.readouterr().out


def test_local_filters_skip_without_listing(capsys):
	prs = [
		_pr(1, ref="feature/x"),
		_pr(2, title="x [skip ai]"),
		_pr(3, repo="fork/r"),
		_pr(4, head=""),
	]
	api = FakeApi([_run(11)])
	summary = _process(prs, api, head_ref_filter="claude/")
	out = capsys.readouterr().out
	assert api.gets == [] and api.posts == []
	assert summary == {"candidates": 4, "rerun": 0, "skipped": 4, "errors": 0}
	for reason in ("head_ref_filter", "skip_ai_marker", "fork_head", "no_head_sha"):
		assert f"reason={reason}" in out


def test_one_listing_serves_every_pr():
	api = FakeApi([_run(11, head=HEAD), _run(21, head=OLD_HEAD)])
	summary = _process([_pr(1, head=HEAD), _pr(2, head=OLD_HEAD), _pr(3, head="c" * 40)], api)
	assert api.gets == [RUNS_PATH]
	assert sorted(api.posts) == sorted([f"repos/{REPO}/actions/runs/11/rerun-failed-jobs", f"repos/{REPO}/actions/runs/21/rerun-failed-jobs"])
	assert summary == {"candidates": 3, "rerun": 2, "skipped": 1, "errors": 0}


def test_listing_failure_fails_open(capsys):
	api = FakeApi(get_error="HTTP 502")
	summary = _process([_pr()], api)
	assert api.posts == []
	assert summary["errors"] == 1
	assert "reason=runs_list_failed" in capsys.readouterr().out


def test_missing_snapshot_skips(capsys, tmp_path):
	assert rerunner.load_snapshot(str(tmp_path / "missing.json")) is None
	bad = tmp_path / "bad.json"
	bad.write_text("{not json")
	assert rerunner.load_snapshot(str(bad)) is None
	api = FakeApi([_run(11)])
	summary = _process(None, api)
	assert api.gets == [] and summary["rerun"] == 0
	assert "reason=no_pr_snapshot" in capsys.readouterr().out


def test_main_reads_snapshot_and_env(monkeypatch, tmp_path, capsys):
	snapshot = tmp_path / "prs.json"
	snapshot.write_text(json.dumps([_pr()]))
	calls = FakeApi([_run(11)])
	monkeypatch.setattr(rerunner, "api_get", calls.get)
	monkeypatch.setattr(rerunner, "api_post", calls.post)
	monkeypatch.setenv("REPOSITORY", REPO)
	monkeypatch.setenv("CI_CANCELLED_AUTO_RERUN_ENABLED", "true")
	monkeypatch.delenv("DRY_RUN", raising=False)
	monkeypatch.delenv("HEAD_REF_FILTER", raising=False)
	assert rerunner.main(["--prs", str(snapshot)]) == 0
	assert calls.posts == [f"repos/{REPO}/actions/runs/11/rerun-failed-jobs"]
	assert "CI_CANCELLED_RERUN_END candidates=1 rerun=1 skipped=0 errors=0" in capsys.readouterr().out
	monkeypatch.setenv("CI_CANCELLED_AUTO_RERUN_ENABLED", "false")
	calls.posts.clear()
	calls.gets.clear()
	assert rerunner.main(["--prs", str(snapshot)]) == 0
	assert calls.gets == [] and calls.posts == []


def _sweep_job():
	return yaml.safe_load(SWEEP_WF.read_text(encoding="utf-8"))["jobs"]["sweep"]


def test_sweep_job_runs_the_rerun_step_after_dispatch():
	steps = _sweep_job()["steps"]
	names = [step.get("name") for step in steps]
	enumerate_index = names.index("Enumerate open PRs and dispatch internal-review.yml")
	checkout_index = names.index("Check out the cancelled-CI re-run helper")
	rerun_index = names.index("Re-run cancelled CI once per PR head")
	assert enumerate_index < checkout_index < rerun_index
	checkout = steps[checkout_index]
	assert checkout["uses"].startswith("actions/checkout@")
	assert checkout["with"]["persist-credentials"] is False
	assert checkout["if"] == "${{ !cancelled() }}"
	step = steps[rerun_index]
	assert step["if"] == "${{ !cancelled() }}"
	assert step["env"]["CI_CANCELLED_AUTO_RERUN_ENABLED"] == "${{ vars.CI_CANCELLED_AUTO_RERUN_ENABLED || 'true' }}"
	assert step["env"]["GH_TOKEN"] == steps[enumerate_index]["env"]["GH_TOKEN"]
	assert step["env"]["HEAD_REF_FILTER"] == "${{ inputs.head_ref_filter }}"
	assert step["env"]["DRY_RUN"] == "${{ inputs.dry_run }}"
	assert "scripts/ci_cancelled_rerun.py" in step["run"]
	assert '--prs "${RUNNER_TEMP}/review-autofix-sweep-prs.json"' in step["run"]


def test_sweep_job_keeps_actions_write_and_skips_the_catch_all_tick():
	workflow = yaml.safe_load(SWEEP_WF.read_text(encoding="utf-8"))
	assert workflow["permissions"]["actions"] == "write"
	assert "permissions" not in _sweep_job()
	assert _sweep_job()["if"] == "github.event.schedule != '17 * * * *'"


def test_enumerate_step_writes_snapshot_with_head_sha_before_zero_candidate_exit():
	step = next(s for s in _sweep_job()["steps"] if s.get("name") == "Enumerate open PRs and dispatch internal-review.yml")
	script = step["run"]
	assert "head_sha: (.head.sha // \"\")," in script
	# The dispatch loop stopped reading `head_repo` in #4634 (issue #4618);
	# the re-run's same-repository filter still needs it, or every PR is
	# skipped as `fork_head`.
	assert "head_repo: (.head.repo.full_name // \"\")," in script
	write = script.find('> "${RUNNER_TEMP}/review-autofix-sweep-prs.json"')
	guard = script.find('if [ "${total}" -eq 0 ]; then')
	assert 0 < write < guard


def test_ci_runs_this_test_file():
	assert "tests/test_ci_cancelled_rerun.py" in CI_WF.read_text(encoding="utf-8")


def test_log_prefixes_are_registered_in_both_agents_md_forms():
	agents_text = (ROOT / "agents.md").read_text(encoding="utf-8")
	for prefix in ("CI_CANCELLED_RERUN", "CI_CANCELLED_RERUN_END", "AUTOFIX_SWEEP_PR_SNAPSHOT_WRITE_FAILED"):
		assert f"- `{prefix}`" in agents_text
		assert f"LOG_PREFIX.name={prefix}" in agents_text
