#!/usr/bin/env python3
"""Tests for the daily consumer health check (scripts/consumer_health_check.{py,sh})."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_PY = REPO_ROOT / "scripts" / "consumer_health_check.py"
CHECK_SH = REPO_ROOT / "scripts" / "consumer_health_check.sh"
POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
SELF_REPO = "shubhodeep1/coding-workflows"
PIPELINE = "pipeline-bot"
NOW = "2026-10-15T12:00:00Z"
TEMPLATES = sorted(p.name for p in (REPO_ROOT / "workflow-templates").glob("*.yml"))


def _module():
	spec = importlib.util.spec_from_file_location("consumer_health_check", CHECK_PY)
	assert spec is not None and spec.loader is not None
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


def _wf(name: str, state: str = "active", wid: int = 1) -> dict:
	return {"id": wid, "path": f".github/workflows/{name}", "state": state}


def _healthy(poll_age_hours: float = 1.0) -> dict:
	created = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc).timestamp() - poll_age_hours * 3600
	return {
		"workflows": {"total_count": 3, "workflows": [_wf("ai-orchestrate-poll.yml", wid=11), _wf("ai-review.yml", wid=12), _wf("ci.yml", "disabled_manually", 13)]},
		"poll_runs": {"total_count": 1, "workflow_runs": [{"created_at": datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}]},
		"tracking": [{"number": 5, "comments": 40}],
	}


def _write_snapshot(root: Path, repo: str, data: dict | None, error: str | None = None) -> None:
	snap = root / repo.replace("/", "__")
	snap.mkdir(parents=True)
	if error is not None:
		(snap / "error").write_text(error, encoding="utf-8")
		return
	assert data is not None
	for key in ("workflows", "poll_runs", "tracking"):
		if key in data:
			(snap / f"{key}.json").write_text(json.dumps(data[key]), encoding="utf-8")


def _evaluate(tmp: Path, snapshots: dict[str, dict | str], open_issues: list | None = None) -> dict:
	mod = _module()
	root = tmp / "snap"
	for repo, data in snapshots.items():
		if isinstance(data, str):
			_write_snapshot(root, repo, None, error=data)
		else:
			_write_snapshot(root, repo, data)
	return mod.evaluate(
		list(snapshots), root, open_issues or [], pipeline_login=PIPELINE, templates=set(TEMPLATES),
		now=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc), silent_hours=6, comment_limit=2000,
	)


def _issue(number: int, repo: str, problem: str, author: str = PIPELINE) -> dict:
	return {"number": number, "user": {"login": author}, "body": f"<!-- ai:consumer-health:v1 repo={repo} problem={problem} -->\n## x"}


def test_a_healthy_consumer_has_no_problem(tmp_path: Path) -> None:
	# ci.yml is the consumer's own workflow, not a wrapper: its state is ignored.
	result = _evaluate(tmp_path, {"o/a": _healthy()})
	assert result == {"problems": [], "enable": [], "open": [], "close": [], "repos": 1}


def test_inactivity_disabled_wrappers_are_reenabled_and_manual_ones_reported(tmp_path: Path) -> None:
	data = _healthy()
	data["workflows"]["workflows"] += [_wf("ai-plan.yml", "disabled_inactivity", 21), _wf("ai-implement.yml", "disabled_manually", 22), _wf("ai-clarify.yml", "disabled_fork", 23)]
	result = _evaluate(tmp_path, {"o/a": data})
	assert result["enable"] == [{"repo": "o/a", "workflow_id": 21, "path": ".github/workflows/ai-plan.yml"}]
	assert result["problems"] == [{"repo": "o/a", "problem": "workflows_disabled", "detail": "ai-clarify.yml (disabled_fork), ai-implement.yml (disabled_manually)"}]
	assert result["open"] == result["problems"]


def test_a_silent_poller_is_reported_only_while_its_wrapper_is_active(tmp_path: Path) -> None:
	result = _evaluate(tmp_path / "1", {"o/a": _healthy(poll_age_hours=7)})
	assert [(p["problem"], p["detail"]) for p in result["problems"]] == [("poller_silent", "last run started 2026-10-15T05:00Z")]
	never = _healthy()
	never["poll_runs"] = {"total_count": 0, "workflow_runs": []}
	result = _evaluate(tmp_path / "2", {"o/a": never})
	assert [(p["problem"], p["detail"]) for p in result["problems"]] == [("poller_silent", "last run started never")]
	# A run created long ago but started recently is not silent; one queued
	# since a fresh created_at but started (run_started_at) 7 hours ago is.
	late = _healthy(poll_age_hours=7)
	late["poll_runs"]["workflow_runs"][0]["run_started_at"] = "2026-10-15T11:00:00Z"
	assert _evaluate(tmp_path / "4", {"o/a": late})["problems"] == []
	stuck = _healthy()
	stuck["poll_runs"]["workflow_runs"][0]["run_started_at"] = "2026-10-15T05:00:00Z"
	assert [p["problem"] for p in _evaluate(tmp_path / "5", {"o/a": stuck})["problems"]] == ["poller_silent"]
	# A poll wrapper switched off is reported as disabled, never also as silent.
	off = _healthy(poll_age_hours=50)
	off["workflows"]["workflows"][0]["state"] = "disabled_manually"
	result = _evaluate(tmp_path / "3", {"o/a": off})
	assert [p["problem"] for p in result["problems"]] == ["workflows_disabled"]


def test_tracking_issues_at_the_limit_are_reported(tmp_path: Path) -> None:
	data = _healthy()
	data["tracking"] = [{"number": 3928, "comments": 2500}, {"number": 4038, "comments": 2000}, {"number": 4281, "comments": 1999}, {"number": 9, "comments": 3000, "pull_request": {}}]
	result = _evaluate(tmp_path, {"o/a": data})
	assert result["problems"] == [{"repo": "o/a", "problem": "tracking_issue_comments", "detail": "#3928 (2500 comments), #4038 (2000 comments)"}]


def test_an_unreadable_consumer_is_a_problem_and_keeps_its_other_issues_open(tmp_path: Path) -> None:
	open_issues = [_issue(70, "o/a", "poller_silent"), _issue(71, "o/b", "workflows_disabled")]
	result = _evaluate(tmp_path, {"o/a": "forbidden", "o/b": _healthy()}, open_issues)
	assert result["problems"] == [{"repo": "o/a", "problem": "unreadable", "detail": "forbidden"}]
	# o/a could not be read, so its poller issue says nothing new and stays open.
	assert result["close"] == [{"number": 71, "repo": "o/b", "problem": "workflows_disabled"}]
	missing = _evaluate(tmp_path / "m", {"o/a": _healthy() | {"tracking": None}})
	assert [(p["problem"], p["detail"]) for p in missing["problems"]] == [("unreadable", "tracking_unparsable")]


def test_open_issues_dedupe_and_only_pipeline_issues_are_closed(tmp_path: Path) -> None:
	data = _healthy(poll_age_hours=9)
	open_issues = [
		_issue(80, "o/a", "poller_silent"),
		_issue(81, "o/a", "unreadable"),
		_issue(82, "o/a", "tracking_issue_comments", author="someone-else"),
		{"number": 83, "user": {"login": PIPELINE}, "body": "mentions <!-- ai:consumer-health:v1 repo=o/a problem=unreadable --> later"},
		_issue(84, "o/gone", "poller_silent"),
	]
	result = _evaluate(tmp_path, {"o/a": data}, open_issues)
	assert result["open"] == []  # #80 already tracks the silent poller
	# Cleared on a readable repo, and a repo no longer listed: closed. Issues not
	# filed by the pipeline, or without the marker first, are never touched.
	assert result["close"] == [{"number": 81, "repo": "o/a", "problem": "unreadable"}, {"number": 84, "repo": "o/gone", "problem": "poller_silent"}]


# --- the shell entry point, against a fake `gh` -----------------------------

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def done(out="", rc=0, err=""):
	json.dump(state, open(state_path, "w"))
	if out:
		sys.stdout.write(out if out.endswith("\n") else out + "\n")
	if err:
		sys.stderr.write(err + "\n")
	sys.exit(rc)
if args[:2] == ["label", "create"]:
	done()
if args[0] != "api":
	done(rc=2, err="unexpected gh call")
rest = args[1:]
method = "GET"
if "-X" in rest:
	method = rest[rest.index("-X") + 1]
paths = [a for a in rest if a.startswith("repos/") or a == "user"]
path = paths[0] if paths else ""
fields = {}
for i, a in enumerate(rest):
	if a == "-f":
		k, _, v = rest[i + 1].partition("=")
		fields[k] = v
if path == "user":
	done(state["login"])
if method == "GET" and path.startswith("repos/%s/issues?" % state["self"]):
	if state.get("fail_open_issues"):
		done(rc=1, err="HTTP 502: Bad Gateway")
	done(json.dumps(state["open_issues"]))
for repo, data in state["consumers"].items():
	if path == "repos/%s/actions/workflows?per_page=100" % repo:
		if "error" in data:
			done(rc=1, err=data["error"])
		done(json.dumps(data["workflows"]))
	if path.startswith("repos/%s/actions/workflows/" % repo) and path.endswith("/runs?per_page=1"):
		done(json.dumps(data["poll_runs"]))
	if path.startswith("repos/%s/issues?state=open" % repo):
		done(json.dumps(data["tracking"]))
	if method == "PUT" and path.startswith("repos/%s/actions/workflows/" % repo):
		state.setdefault("enabled", []).append(path)
		done()
if method == "POST" and path == "repos/%s/issues" % state["self"]:
	if state.get("fail_create"):
		done(rc=1, err="HTTP 502: Bad Gateway")
	state.setdefault("created", []).append(fields)
	done(str(900 + len(state["created"])))
if method == "POST" and path.endswith("/comments"):
	state.setdefault("comments", []).append(path)
	done("{}")
if method == "PATCH":
	state.setdefault("closed", []).append(path)
	done("{}")
done(rc=1, err="HTTP 404: Not Found (unexpected %s %s)" % (method, path))
'''


def _run_sh(tmp: Path, consumers: dict, open_issues: list, env_extra: dict | None = None, *, fail_open_issues: bool = False, fail_create: bool = False) -> tuple[subprocess.CompletedProcess, dict]:
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	state_path = tmp / "gh_state.json"
	state_path.write_text(json.dumps({"login": PIPELINE, "self": SELF_REPO, "open_issues": open_issues, "consumers": consumers, "fail_open_issues": fail_open_issues, "fail_create": fail_create}), encoding="utf-8")
	consumers_file = tmp / "consumers.json"
	consumers_file.write_text(json.dumps(list(consumers)), encoding="utf-8")
	templates_file = tmp / "templates.txt"
	templates_file.write_text("\n".join(TEMPLATES) + "\n", encoding="utf-8")
	env = {
		"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp), "FAKE_GH_STATE": str(state_path),
		"GITHUB_REPOSITORY": SELF_REPO, "GH_TOKEN": "x", "RUNNER_TEMP": str(tmp),
		"CONSUMER_HEALTH_CONSUMERS_FILE": str(consumers_file), "CONSUMER_HEALTH_TEMPLATES_FILE": str(templates_file),
		"CONSUMER_HEALTH_NOW": NOW, "GH_RETRY_MAX_ATTEMPTS": "1", "PYTHONDONTWRITEBYTECODE": "1",
	}
	env.update(env_extra or {})
	result = subprocess.run(["bash", str(CHECK_SH)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
	return result, json.loads(state_path.read_text(encoding="utf-8"))


def test_the_script_opens_enables_and_closes(tmp_path: Path) -> None:
	paused = _healthy()
	paused["workflows"]["workflows"] += [_wf("ai-plan.yml", "disabled_inactivity", 21), _wf("ai-implement.yml", "disabled_manually", 22)]
	result, state = _run_sh(tmp_path, {"o/a": paused, "o/b": {"error": "HTTP 404: Not Found"}, "o/c": _healthy()}, [_issue(70, "o/c", "poller_silent")])
	assert result.returncode == 0, result.stdout + result.stderr
	assert state["enabled"] == ["repos/o/a/actions/workflows/21/enable"]
	assert [(c["title"], c["labels[]"]) for c in state["created"]] == [
		("Consumer check: o/a: AI workflows are switched off", "ai:consumer-health"),
		("Consumer check: o/b: cannot be read", "ai:consumer-health"),
	]
	assert state["created"][0]["body"].startswith("<!-- ai:consumer-health:v1 repo=o/a problem=workflows_disabled -->\n")
	assert "ai-implement.yml (disabled_manually)" in state["created"][0]["body"]
	assert "not_found_or_no_access" in state["created"][1]["body"]
	assert state["comments"] == [f"repos/{SELF_REPO}/issues/70/comments"]
	assert state["closed"] == [f"repos/{SELF_REPO}/issues/70"]
	assert "CONSUMER_HEALTH outcome=done repos=3 problems=2 opened=2 closed=1 enabled=1" in result.stdout
	# The poll-run read happens only for an active poll wrapper; o/b's failed
	# workflow read stops its other reads.
	reads = [c for c in state["calls"] if c[0] == "api" and "-X" not in c]
	assert not [c for c in reads if any("repos/o/b/issues" in a for a in c)]


def test_the_script_is_off_when_switched_off(tmp_path: Path) -> None:
	for value, reason in (("false", "disabled"), ("FALSE", "disabled"), ("nope", "invalid_switch")):
		sub = tmp_path / value
		sub.mkdir()
		result, state = _run_sh(sub, {"o/a": _healthy()}, [], {"CONSUMER_HEALTH_CHECK_ENABLED": value})
		assert result.returncode == 0
		assert f"CONSUMER_HEALTH skip reason={reason}" in result.stdout
		assert "calls" not in state


def test_a_failed_read_of_this_repos_issues_fails_the_day(tmp_path: Path) -> None:
	# Without the open-issue list every problem would be filed again, so the
	# run fails and the next tick retries the day.
	result, state = _run_sh(tmp_path, {"o/a": _healthy(poll_age_hours=9)}, [], fail_open_issues=True)
	assert result.returncode == 1
	assert "CONSUMER_HEALTH outcome=failed reason=open_issues_unavailable" in result.stdout
	assert "created" not in state and "closed" not in state


FAKE_CURL = """#!/usr/bin/env bash
echo sent >> "${FAKE_CURL_LOG}"
[ "${FAKE_CURL_FAIL:-}" = "1" ] && exit 7
printf '{"ok":true,"result":{"message_id":7}}'
"""


def test_the_failure_alert_is_recorded_only_after_telegram_delivers_it(tmp_path: Path) -> None:
	for case, curl_fail, alerted_before, expect_sent, expect_recorded in (
		("delivered", "", "false", True, True),
		("send_failed", "1", "false", True, False),
		("already_alerted", "", "true", False, False),
	):
		sub = tmp_path / case
		sub.mkdir()
		fake_bin = sub / "fakecurl"
		fake_bin.mkdir()
		(fake_bin / "curl").write_text(FAKE_CURL, encoding="utf-8")
		(fake_bin / "curl").chmod(0o755)
		curl_log = sub / "curl.log"
		output = sub / "github_output"
		output.write_text("", encoding="utf-8")
		env = {
			"PATH": f"{sub / 'bin'}:{fake_bin}:{os.environ['PATH']}", "FAKE_CURL_LOG": str(curl_log), "FAKE_CURL_FAIL": curl_fail,
			"TG_BOT_SECRET": "x", "TG_ADMIN_CHAT_ID": "1", "ALERT_MSG_LEVEL": "WARNING",
			"GITHUB_OUTPUT": str(output), "CONSUMER_HEALTH_FAILURE_ALERTED": alerted_before,
		}
		result, state = _run_sh(sub, {"o/a": _healthy(poll_age_hours=9)}, [], env, fail_create=True)
		assert result.returncode == 1, case
		assert "write_failures=1" in result.stdout, case
		assert curl_log.exists() == expect_sent, case
		assert ("failure_alerted=true" in output.read_text(encoding="utf-8")) == expect_recorded, case


def test_the_poller_runs_the_check_once_a_day_in_this_repo_only() -> None:
	text = POLL_WORKFLOW.read_text(encoding="utf-8")
	step = text[text.index("- name: Run daily consumer health check"):]
	step = step[:step.index("\n      - name:", 1)]
	assert "github.repository == 'shubhodeep1/coding-workflows'" in step
	assert "steps.consumer_health_day.outputs.cache-hit != 'true'" in step
	assert "CONSUMER_HEALTH_CHECK_ENABLED: ${{ vars.CONSUMER_HEALTH_CHECK_ENABLED || 'true' }}" in step
	assert "CONSUMER_HEALTH_POLLER_SILENT_HOURS: ${{ vars.CONSUMER_HEALTH_POLLER_SILENT_HOURS || '6' }}" in step
	assert "CONSUMER_HEALTH_TRACKING_COMMENT_LIMIT: ${{ vars.CONSUMER_HEALTH_TRACKING_COMMENT_LIMIT || '2000' }}" in step
	assert "continue-on-error: true" in step
	assert "bash scripts/consumer_health_check.sh" in step
	date_step = text[text.index("- name: Pick today's consumer health check key"):]
	date_step = date_step[:date_step.index("\n      - name:", 1)]
	assert "continue-on-error: true" in date_step
	# A failed date step must not save a dateless key that skips every later run.
	assert "steps.consumer_health_date.outputs.day != ''" in step
	lookup = text[text.index("- name: Look up today's consumer health check"):]
	lookup = lookup[:lookup.index("\n      - name:", 1)]
	assert "key: consumer-health-check-${{ steps.consumer_health_date.outputs.day }}" in lookup
	assert "lookup-only: true" in lookup
	assert "steps.consumer_health_date.outputs.day != ''" in lookup
	save = text[text.index("- name: Record today's consumer health check"):]
	save = save[:save.index("\n      - name:", 1)]
	assert "steps.consumer_health.outcome == 'success'" in save
	assert "steps.consumer_health_date.outputs.day != ''" in save
	assert "key: consumer-health-check-${{ steps.consumer_health_date.outputs.day }}" in save
	assert "CONSUMER_HEALTH_FAILURE_ALERTED: ${{ steps.consumer_health_alerted.outputs.cache-hit == 'true' && 'true' || 'false' }}" in step
	alert = text[text.index("- name: Record today's consumer health failure alert"):]
	alert = alert[:alert.index("\n      - name:", 1)]
	assert "steps.consumer_health.outputs.failure_alerted == 'true'" in alert
	assert "key: consumer-health-alert-${{ steps.consumer_health_date.outputs.day }}" in alert
	# The template list comes from workflow-templates/, the same set this test uses.
	assert "workflow-templates" in step and TEMPLATES and "ai-orchestrate-poll.yml" in TEMPLATES
