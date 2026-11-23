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
	# The consumer sweep job must not inherit the review job's write grants.
	consumer_job = yaml.safe_load(template)["jobs"]["security-hold-sweep"]
	assert consumer_job["permissions"] == {"actions": "write", "contents": "read", "pull-requests": "read", "issues": "write"}
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


# --- Issue #7209: github.token for operations, PAT only names the findings author ---

def _bot_comment(body: str, *, login: str = "github-actions", typename: str = "Bot") -> dict:
	return {"author": {"login": login, "__typename": typename}, "body": body, "createdAt": NOW.isoformat()}


def _dispatch_marker(cycle: int = 2) -> str:
	return f"sent\n\n<!-- ai:security-followup-stale-dispatch:v1 head={HEAD} cycle={cycle} -->"


def _graphql(*comments: dict) -> dict:
	return {"data": {"repository": {
		"defaultBranchRef": {"name": "main"},
		"pullRequests": {"nodes": [_pr(*comments)], "pageInfo": {"hasNextPage": False}},
	}}}


def _run_main(monkeypatch, graphql_payload, *, mode: str, pat: str | None, identity: str = "bot"):
	calls: list[list[str]] = []
	json_calls: list[list[str]] = []
	identity_tokens: list[str] = []
	environ_during_ops: list[str | None] = []

	def fake_run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
		calls.append(arguments)
		environ_during_ops.append(SWEEP.os.environ.get("SECURITY_HOLD_FINDING_AUTHOR_TOKEN"))
		stdout = "bot\n" if arguments[:2] == ["api", "user"] else "{}"
		return subprocess.CompletedProcess(arguments, 0, stdout, "")

	def fake_json(arguments: list[str]):
		json_calls.append(arguments)
		environ_during_ops.append(SWEEP.os.environ.get("SECURITY_HOLD_FINDING_AUTHOR_TOKEN"))
		return graphql_payload

	def fake_identity(token: str) -> str:
		identity_tokens.append(token)
		return identity

	monkeypatch.setattr(SWEEP, "_run_gh", fake_run)
	monkeypatch.setattr(SWEEP, "_gh_json", fake_json)
	monkeypatch.setattr(SWEEP, "_gh_identity", fake_identity)
	monkeypatch.setattr(SWEEP.dt, "datetime", type("FixedDateTime", (dt.datetime,), {
		"now": classmethod(lambda cls, tz=None: NOW),
	}))
	monkeypatch.setenv("SINGLE_ISSUE_SECURITY_PASS_ENABLED", "true")
	monkeypatch.setenv("REPOSITORY", "o/r")
	monkeypatch.setenv("SECURITY_HOLD_REVIEW_WORKFLOW", "ai-review.yml")
	monkeypatch.setenv("SECURITY_HOLD_SWEEP_TOKEN_MODE", mode)
	if pat is None:
		monkeypatch.delenv("SECURITY_HOLD_FINDING_AUTHOR_TOKEN", raising=False)
	else:
		monkeypatch.setenv("SECURITY_HOLD_FINDING_AUTHOR_TOKEN", pat)
	assert SWEEP.main() == 0
	return calls, json_calls, identity_tokens, environ_during_ops


def _dispatched(calls: list[list[str]]) -> bool:
	return any(call[:3] == ["workflow", "run", "ai-review.yml"] for call in calls)


def test_actions_token_mode_uses_pat_only_for_identity(monkeypatch) -> None:
	calls, json_calls, identity_tokens, environ_during_ops = _run_main(
		monkeypatch, _graphql(_comment("findings", 25)), mode="actions_token", pat="pat-secret")
	assert identity_tokens == ["pat-secret"]
	assert not any(call[:2] == ["api", "user"] for call in calls)
	assert "SECURITY_HOLD_FINDING_AUTHOR_TOKEN" not in SWEEP.os.environ
	assert environ_during_ops and all(value is None for value in environ_during_ops)
	assert _dispatched(calls)
	assert any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def test_actions_token_mode_fails_closed_without_finding_identity(monkeypatch, capsys) -> None:
	calls, json_calls, _, _ = _run_main(
		monkeypatch, _graphql(_comment("findings", 25)), mode="actions_token", pat="pat-secret", identity="")
	assert json_calls == [] and calls == []
	assert "reason=finding_identity_unavailable" in capsys.readouterr().out


def test_invalid_token_mode_skips_without_api_calls(monkeypatch, capsys) -> None:
	calls, json_calls, identity_tokens, _ = _run_main(
		monkeypatch, _graphql(_comment("findings", 25)), mode="bogus", pat="pat-secret")
	assert calls == [] and json_calls == [] and identity_tokens == []
	assert "reason=invalid_token_mode" in capsys.readouterr().out


def test_no_pat_trusts_bot_authored_findings_only_from_a_bot(monkeypatch) -> None:
	findings = _comment("findings", 25)
	bot_findings = {**findings, "author": {"login": "github-actions", "__typename": "Bot"}}
	calls, _, identity_tokens, _ = _run_main(monkeypatch, _graphql(bot_findings), mode="actions_token", pat="")
	assert identity_tokens == [] and _dispatched(calls)
	impostor = {**findings, "author": {"login": "github-actions", "__typename": "User"}}
	calls, _, _, _ = _run_main(monkeypatch, _graphql(impostor), mode="actions_token", pat="")
	assert not _dispatched(calls)


def test_with_pat_bot_authored_findings_are_rejected(monkeypatch) -> None:
	bot_findings = {**_comment("findings", 25), "author": {"login": "github-actions", "__typename": "Bot"}}
	calls, _, _, _ = _run_main(monkeypatch, _graphql(bot_findings), mode="actions_token", pat="pat-secret")
	assert not _dispatched(calls)


def test_sweep_marker_authors_are_verified_separately() -> None:
	markers = frozenset({"github-actions[bot]", "bot"})
	findings = _comment("findings", 25)
	# A Bot-authored dispatch marker (GraphQL or REST login) deduplicates.
	for login in ("github-actions", "github-actions[bot]"):
		assert SWEEP.stale_candidate(_pr(findings, _bot_comment(_dispatch_marker(), login=login)), "bot", 24, NOW, markers) is None
	# A user account named like the bot does not.
	assert SWEEP.stale_candidate(_pr(findings, _bot_comment(_dispatch_marker(), typename="User")), "bot", 24, NOW, markers) == (42, HEAD, 2)
	# A legacy PAT-authored marker still deduplicates.
	legacy = {"author": {"login": "bot"}, "body": _dispatch_marker(), "createdAt": NOW.isoformat()}
	assert SWEEP.stale_candidate(_pr(findings, legacy), "bot", 24, NOW, markers) is None
	# A Bot-authored marker does not deduplicate when the bot is not a marker author.
	assert SWEEP.stale_candidate(_pr(findings, _bot_comment(_dispatch_marker())), "bot", 24, NOW) == (42, HEAD, 2)
	# Dispatch markers never count as findings.
	assert SWEEP._has_current_head_result(_pr(_bot_comment(_dispatch_marker(), typename="User")), "bot", markers) is False
	assert SWEEP._has_current_head_result(_pr(_bot_comment(_dispatch_marker())), "bot", markers) is True


def test_rest_history_mapping_carries_author_type(monkeypatch) -> None:
	pages = [[{"user": {"login": "github-actions[bot]", "type": "Bot"}, "body": "x", "created_at": NOW.isoformat()}]]
	monkeypatch.setattr(SWEEP, "_gh_json", lambda arguments: pages)
	nodes = SWEEP._full_comment_nodes("o/r", 42)
	assert nodes == [{"author": {"login": "github-actions[bot]", "__typename": "Bot"}, "body": "x", "createdAt": NOW.isoformat()}]
	assert "author{login __typename}" in SWEEP.GRAPHQL_QUERY


def test_gh_identity_passes_token_only_to_its_subprocess(monkeypatch) -> None:
	seen: list[dict] = []

	def fake_subprocess_run(arguments, **kwargs):
		seen.append({"arguments": arguments, "token": kwargs["env"]["GH_TOKEN"]})
		return subprocess.CompletedProcess(arguments, 0, "pipeline-bot\n", "")

	monkeypatch.setattr(SWEEP.subprocess, "run", fake_subprocess_run)
	monkeypatch.setenv("GH_TOKEN", "actions-token")
	assert SWEEP._gh_identity("pat-secret") == "pipeline-bot"
	assert seen[0]["arguments"][:3] == ["gh", "api", "user"] and seen[0]["token"] == "pat-secret"
	assert SWEEP.os.environ["GH_TOKEN"] == "actions-token"
	monkeypatch.setattr(SWEEP.subprocess, "run", lambda arguments, **kwargs: subprocess.CompletedProcess(arguments, 0, "bad login!\n", ""))
	assert SWEEP._gh_identity("pat-secret") == ""
	assert SWEEP._gh_identity("") == ""


def test_consumer_sweep_job_uses_github_token_for_operations() -> None:
	template = (ROOT / "workflow-templates" / "ai-review.yml").read_text(encoding="utf-8")
	job_text = template.split("  security-hold-sweep:\n", 1)[1].split("\n  provider-outage-resume:\n", 1)[0]
	assert "GH_TOKEN: ${{ github.token }}" in job_text
	assert job_text.count("secrets.GH_PAT") == 1
	assert "SECURITY_HOLD_FINDING_AUTHOR_TOKEN: ${{ secrets.GH_PAT }}" in job_text
	assert "SECURITY_HOLD_SWEEP_TOKEN_MODE: actions_token" in job_text
	assert "persist-credentials: false" in job_text
	assert "path: .security-hold-support" in job_text
	yaml = pytest.importorskip("yaml")
	job = yaml.safe_load(template)["jobs"]["security-hold-sweep"]
	assert job["permissions"] == {"actions": "write", "contents": "read", "pull-requests": "read", "issues": "write"}
	assert job["steps"][0]["with"]["persist-credentials"] is False
	env = job["steps"][1]["env"]
	assert env["GH_TOKEN"] == "${{ github.token }}"
	assert env["SECURITY_HOLD_SWEEP_TOKEN_MODE"] == "actions_token"
