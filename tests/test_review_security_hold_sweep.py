from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "review_security_hold_sweep.py"
SPEC = importlib.util.spec_from_file_location("review_security_hold_sweep", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SWEEP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SWEEP)
HEAD = "a" * 40
NOW = dt.datetime(2026, 10, 6, 12, tzinfo=dt.timezone.utc)


def _comment(status: str, hours_old: int, *, login: str = "bot", cycle: int = 2) -> dict:
	return {
		"author": {"login": login},
		"body": f"result\n\n<!-- ai:single-issue-security-pass:v1 status={status} head={HEAD} cycle={cycle} -->",
		"createdAt": (NOW - dt.timedelta(hours=hours_old)).isoformat(),
	}


def _pr(*comments: dict) -> dict:
	return {"number": 42, "isDraft": False, "headRefOid": HEAD, "comments": {"nodes": list(comments)}}


def test_stale_candidate_requires_latest_trusted_current_head_findings() -> None:
	assert SWEEP.stale_candidate(_pr(_comment("findings", 25)), "bot", 24, NOW) == (42, HEAD, 2)
	assert SWEEP.stale_candidate(_pr(_comment("findings", 23)), "bot", 24, NOW) is None
	assert SWEEP.stale_candidate(_pr(_comment("findings", 25, login="other")), "bot", 24, NOW) is None
	assert SWEEP.stale_candidate(_pr(_comment("findings", 25), _comment("clean", 1)), "bot", 24, NOW) is None
	assert SWEEP.stale_candidate({"number": 42, "headRefOid": HEAD, "comments": "bad"}, "bot", 24, NOW) is None


def test_stale_candidate_dispatch_marker_deduplicates_head_and_cycle() -> None:
	dispatched = {
		"author": {"login": "bot"},
		"body": f"sent\n\n<!-- ai:security-followup-stale-dispatch:v1 head={HEAD} cycle=2 -->",
		"createdAt": NOW.isoformat(),
	}
	assert SWEEP.stale_candidate(_pr(_comment("findings", 25), dispatched), "bot", 24, NOW) is None
	dispatched["body"] = f"sent\n\n<!-- ai:security-followup-stale-dispatch:v1 head={HEAD} cycle=1 -->"
	assert SWEEP.stale_candidate(_pr(_comment("findings", 25), dispatched), "bot", 24, NOW) == (42, HEAD, 2)


def test_main_dispatches_judge_and_records_dedup_marker(monkeypatch) -> None:
	graphql_payload = {
		"data": {"repository": {
			"defaultBranchRef": {"name": "main"},
			"pullRequests": {"nodes": [_pr(_comment("findings", 25))], "pageInfo": {"hasNextPage": False}},
		}},
	}
	calls: list[list[str]] = []

	def fake_run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
		calls.append(arguments)
		stdout = "bot\n" if arguments[:2] == ["api", "user"] else "{}"
		return subprocess.CompletedProcess(arguments, 0, stdout, "")

	monkeypatch.setattr(SWEEP, "_run_gh", fake_run)
	monkeypatch.setattr(SWEEP, "_gh_json", lambda arguments: graphql_payload)
	monkeypatch.setattr(SWEEP.dt, "datetime", type("FixedDateTime", (dt.datetime,), {
		"now": classmethod(lambda cls, tz=None: NOW),
	}))
	monkeypatch.setenv("SINGLE_ISSUE_SECURITY_PASS_ENABLED", "true")
	monkeypatch.setenv("REPOSITORY", "o/r")
	monkeypatch.setenv("SECURITY_HOLD_REVIEW_WORKFLOW", "ai-review.yml")
	assert SWEEP.main() == 0
	assert any(call[:3] == ["workflow", "run", "ai-review.yml"] and "force_rb_judge=true" in call for call in calls)
	assert any(call[:2] == ["api", "repos/o/r/issues/42/comments"] and "ai:security-followup-stale-dispatch:v1" in call[-1] for call in calls)


def test_workflow_wiring_uses_existing_schedules() -> None:
	template = (ROOT / "workflow-templates" / "ai-review.yml").read_text(encoding="utf-8")
	source_sweep = (ROOT / ".github" / "workflows" / "review_autofix_sweep.yml").read_text(encoding="utf-8")
	assert 'cron: "17 * * * *"' in template
	assert "github.event_name != 'schedule'" in template
	assert "force_rb_judge: ${{ github.event_name == 'workflow_dispatch'" in template
	assert "review_security_hold_sweep.py" in template
	assert "review_security_hold_sweep.py" in source_sweep
	assert "SECURITY_HOLD_REVIEW_WORKFLOW: internal-review.yml" in source_sweep
	assert "actions: write" in template
	internal_review = (ROOT / ".github" / "workflows" / "internal-review.yml").read_text(encoding="utf-8")
	assert "force_rb_judge: ${{ github.event_name == 'workflow_dispatch' && github.event.inputs.force_rb_judge == 'true' }}" in internal_review
	yaml = pytest.importorskip("yaml")
	# A job-level permissions block replaces the workflow-level one.
	sweep_job = yaml.safe_load(source_sweep)["jobs"]["sweep"]
	assert sweep_job["permissions"]["issues"] == "write"
	assert "force_rb_judge" in yaml.safe_load(internal_review)[True]["workflow_dispatch"]["inputs"]


def test_findings_marker_beyond_comment_window_uses_full_history(monkeypatch) -> None:
	filler = [{"author": {"login": "bot"}, "body": "noise", "createdAt": NOW.isoformat()}]
	pr_payload = {**_pr(*filler), "comments": {"nodes": filler, "pageInfo": {"hasPreviousPage": True}}}
	graphql_payload = {
		"data": {"repository": {
			"defaultBranchRef": {"name": "main"},
			"pullRequests": {"nodes": [pr_payload], "pageInfo": {"hasNextPage": False}},
		}},
	}
	old = _comment("findings", 25)
	rest_pages = [[{"user": {"login": "bot"}, "body": old["body"], "created_at": old["createdAt"]}]]
	calls: list[list[str]] = []

	def fake_run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
		calls.append(arguments)
		stdout = "bot\n" if arguments[:2] == ["api", "user"] else "{}"
		return subprocess.CompletedProcess(arguments, 0, stdout, "")

	monkeypatch.setattr(SWEEP, "_run_gh", fake_run)
	monkeypatch.setattr(SWEEP, "_gh_json", lambda arguments: rest_pages if "--paginate" in arguments else graphql_payload)
	monkeypatch.setattr(SWEEP.dt, "datetime", type("FixedDateTime", (dt.datetime,), {
		"now": classmethod(lambda cls, tz=None: NOW),
	}))
	monkeypatch.setenv("SINGLE_ISSUE_SECURITY_PASS_ENABLED", "true")
	monkeypatch.setenv("REPOSITORY", "o/r")
	monkeypatch.setenv("SECURITY_HOLD_REVIEW_WORKFLOW", "ai-review.yml")
	assert SWEEP.main() == 0
	assert any(call[:3] == ["workflow", "run", "ai-review.yml"] for call in calls)


def test_recent_dispatch_marker_skips_full_history_read(monkeypatch) -> None:
	dispatched = {
		"author": {"login": "bot"},
		"body": f"sent\n\n<!-- ai:security-followup-stale-dispatch:v1 head={HEAD} cycle=2 -->",
		"createdAt": NOW.isoformat(),
	}
	pr_payload = {**_pr(dispatched), "comments": {"nodes": [dispatched], "pageInfo": {"hasPreviousPage": True}}}
	graphql_payload = {
		"data": {"repository": {
			"defaultBranchRef": {"name": "main"},
			"pullRequests": {"nodes": [pr_payload], "pageInfo": {"hasNextPage": False}},
		}},
	}
	json_calls: list[list[str]] = []
	calls: list[list[str]] = []

	def fake_json(arguments: list[str]):
		json_calls.append(arguments)
		return graphql_payload

	def fake_run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
		calls.append(arguments)
		stdout = "bot\n" if arguments[:2] == ["api", "user"] else "{}"
		return subprocess.CompletedProcess(arguments, 0, stdout, "")

	monkeypatch.setattr(SWEEP, "_run_gh", fake_run)
	monkeypatch.setattr(SWEEP, "_gh_json", fake_json)
	monkeypatch.setenv("SINGLE_ISSUE_SECURITY_PASS_ENABLED", "true")
	monkeypatch.setenv("REPOSITORY", "o/r")
	assert SWEEP.main() == 0
	assert not any("--paginate" in call for call in json_calls)
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_marker_post_is_retried_once(monkeypatch) -> None:
	graphql_payload = {
		"data": {"repository": {
			"defaultBranchRef": {"name": "main"},
			"pullRequests": {"nodes": [_pr(_comment("findings", 25))], "pageInfo": {"hasNextPage": False}},
		}},
	}
	marker_posts: list[list[str]] = []

	def fake_run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
		if arguments[:2] == ["api", "repos/o/r/issues/42/comments"]:
			marker_posts.append(arguments)
			return subprocess.CompletedProcess(arguments, 1 if len(marker_posts) == 1 else 0, "{}", "")
		stdout = "bot\n" if arguments[:2] == ["api", "user"] else "{}"
		return subprocess.CompletedProcess(arguments, 0, stdout, "")

	monkeypatch.setattr(SWEEP, "_run_gh", fake_run)
	monkeypatch.setattr(SWEEP, "_gh_json", lambda arguments: graphql_payload)
	monkeypatch.setattr(SWEEP.time, "sleep", lambda seconds: None)
	monkeypatch.setattr(SWEEP.dt, "datetime", type("FixedDateTime", (dt.datetime,), {
		"now": classmethod(lambda cls, tz=None: NOW),
	}))
	monkeypatch.setenv("SINGLE_ISSUE_SECURITY_PASS_ENABLED", "true")
	monkeypatch.setenv("REPOSITORY", "o/r")
	assert SWEEP.main() == 0
	assert len(marker_posts) == 2


def test_script_syntax() -> None:
	ast.parse(SCRIPT.read_text(encoding="utf-8"))
