"""End-to-end replay of the 2026-09-30 OpenRouter credits outage (issue #5773).

The fixture holds the real in-window comments, labelled events and failed
check runs of PR #5324, #5178 and #5215. Every "AI review/autofix failed"
run is replayed through the real `review_autofix.yml` failure-path step
scripts ("Assemble failure evidence", "Post review-blocked comment on PR
(workflow failure)") with the real reviewer-log excerpt of run 36748847333.
The replay must show: no fingerprint cap, no ai:review-blocked label, no
Claude-fixer hand-off, and one alert; then, after a mocked recovery, the
re-dispatches and the clean-up.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "provider_outage"
REPO = "shubhodeep1/coding-workflows"
WORKFLOW_LOGIN = "shubhodeep1"
FIXTURE = json.loads((FIXTURES / "2026-09-30-outage.json").read_text(encoding="utf-8"))
EXCERPT = (FIXTURES / "run-36748847333-reviewer-excerpt.log").read_text(encoding="utf-8")
FAILED_TITLE = "**AI review/autofix failed — needs human intervention**"
REVIEW_WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8"))


def _load(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


heal = _load("workflow_failure_heal", ROOT / "scripts" / "workflow_failure_heal.py")
outage = _load("provider_outage", ROOT / "scripts" / "provider_outage.py")
# The `.claude/` change ships twin-first (issue #5773, Q40): test the twin.
checker = _load("check_in_status_twin", ROOT / "workflow-templates" / ".claude" / "scripts" / "check_in_status.py")


def _step(name: str) -> dict:
	for job in REVIEW_WORKFLOW["jobs"].values():
		for step in job.get("steps") or []:
			if step.get("name") == name:
				return step
	raise AssertionError(f"step {name!r} not found")


def _script(step: dict, run_id: str) -> str:
	return (step["run"].replace("${{ github.server_url }}", "https://github.com")
		.replace("${{ github.repository }}", REPO).replace("${{ github.run_id }}", run_id))


GH_STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["api"] and "/jobs" in args[1]:
	print("Run reviewer models")
	sys.exit(0)
if args[:1] == ["api"] and args[1].endswith("/comments"):
	body = args[args.index("-f") + 1].split("=", 1)[1]
	with open(os.environ["STUB_COMMENTS"], "a", encoding="utf-8") as handle:
		handle.write(json.dumps({"path": args[1], "body": body}) + "\n")
	print("{}")
	sys.exit(0)
sys.exit(1)
'''


def _replay_failure(tmp: Path, pr: dict, run_id: str) -> tuple[dict[str, str], str]:
	"""Run the two failure-path steps for one failed review run; return (GITHUB_ENV, comment body)."""
	work = tmp / f"run-{run_id}"
	runtime = work / "runtime"
	reviews = work / "previous_reviews"
	bin_dir = work / "bin"
	for path in (runtime, reviews, bin_dir):
		path.mkdir(parents=True)
	(reviews / "pass1_openai_gpt-6-luna.log").write_text(EXCERPT, encoding="utf-8")
	for slot in ("openai_gpt-6-luna", "z-ai_glm-5.2", "qwen_qwen3.7-plus"):
		(reviews / f"status_pass1_{slot}.txt").write_text("failed\n", encoding="utf-8")
	(bin_dir / "gh").write_text(GH_STUB, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	env_file = work / "github_env"
	env_file.write_text("", encoding="utf-8")
	comments_file = work / "comments.jsonl"
	env = {
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"GITHUB_ENV": str(env_file),
		"GITHUB_REPOSITORY": REPO,
		"GITHUB_RUN_ID": run_id,
		"GITHUB_RUN_ATTEMPT": "1",
		"RUNNER_NAME": "runner",
		"RUNTIME_DIR": str(runtime),
		"PREVIOUS_REVIEWS_DIR": str(reviews),
		"AUTOFIX_FAILURE_HEAL_PY": str(ROOT / "scripts" / "workflow_failure_heal.py"),
		"AUTOFIX_FAILURE_HEAD_SHA": pr["head"]["sha"],
		"PR_NUMBER": str(pr["number"]),
		"SUPPORT_SCRIPTS_DIR": str(work / "support"),
		"STUB_COMMENTS": str(comments_file),
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	assemble = subprocess.run(["bash", "-c", _script(_step("Assemble failure evidence"), run_id)],
		env=env, capture_output=True, text=True, check=False)
	assert assemble.returncode == 0, assemble.stderr + assemble.stdout
	exported = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	post = subprocess.run(["bash", "-c", _script(_step("Post review-blocked comment on PR (workflow failure)"), run_id)],
		env={**env, **exported}, capture_output=True, text=True, check=False)
	assert post.returncode == 0, post.stderr + post.stdout
	posted = [json.loads(line) for line in comments_file.read_text(encoding="utf-8").splitlines()]
	assert len(posted) == 1 and posted[0]["path"] == f"repos/{REPO}/issues/{pr['number']}/comments"
	return exported, posted[0]["body"]


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
	"""Per PR: the in-window comments as the new failure path would have left them."""
	tmp = tmp_path_factory.mktemp("replay")
	result = {}
	for pr in FIXTURE["prs"]:
		comments, exports = [], []
		for comment in pr["comments"]:
			if not comment["body"].startswith(FAILED_TITLE):
				continue
			run_id = re.search(r" run=([0-9]+) -->", comment["body"]).group(1)
			exported, body = _replay_failure(tmp, pr, run_id)
			exports.append(exported)
			comments.append({**comment, "body": body})
		result[pr["number"]] = {"pr": pr, "comments": comments, "exports": exports}
	return result


def test_every_replayed_run_is_a_provider_outage(replayed):
	for number, data in replayed.items():
		assert len(data["exports"]) == 3, number
		for exported in data["exports"]:
			assert exported["AUTOFIX_FAILURE_REASON"] == "provider_unavailable"
			assert exported["AUTOFIX_PROVIDER_UNAVAILABLE"] == "true"
			assert exported["AUTOFIX_PROVIDER_OUTAGE"] == "provider=openrouter status=402 kind=credits key=OPENROUTER_API_KEY"
		for comment in data["comments"]:
			assert comment["body"].startswith("**AI review/autofix paused: model provider unavailable**")
			assert "reason=provider_unavailable" in comment["body"]
			assert "Linked issues have been labeled" not in comment["body"]


def test_no_fingerprint_cap(replayed):
	for data in replayed.values():
		count = heal.count_identical_failures(data["comments"], head_sha=data["pr"]["head"]["sha"], author_login=WORKFLOW_LOGIN)
		assert count["count"] == 0
		# Before the change the same three runs tripped the cap (default 3).
		original = [comment for comment in data["pr"]["comments"] if comment["body"].startswith(FAILED_TITLE)]
		assert heal.count_identical_failures(original, head_sha=data["pr"]["head"]["sha"], author_login=WORKFLOW_LOGIN)["count"] == 3


def test_no_label_and_no_per_pr_alert(replayed):
	guard = "env.AUTOFIX_PROVIDER_UNAVAILABLE != 'true'"
	for name in ("Mark linked issues review-blocked (workflow failure)",
		"Force orchestrate poll after workflow failure review-blocked label", "Telegram failure"):
		assert guard in _step(name)["if"], name
	assert all(exported["AUTOFIX_PROVIDER_UNAVAILABLE"] == "true" for data in replayed.values() for exported in data["exports"])


def test_no_claude_fixer_hand_off(replayed, monkeypatch):
	# The workflow's hand-off step never runs on a failed reviewer step.
	assert "failure()" not in _step("Hand review round to Claude session (Claude-fixer mode)")["if"]
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", WORKFLOW_LOGIN)
	now = dt.datetime(2026, 9, 30, 23, 0, tzinfo=dt.timezone.utc)
	for number, data in replayed.items():
		pr = data["pr"]
		pull = {"merged": False, "state": "open", "labels": [], "mergeable_state": "clean", "updated_at": "2026-09-30T21:00:00Z",
			"head": {"sha": pr["head"]["sha"], "ref": pr["head"]["ref"]}, "user": pr["user"],
			"base": {"repo": {"default_branch": "main"}}}
		comments = [{**comment, "user": {"login": comment["user"]["login"], "type": "User"}} for comment in data["comments"]]
		monkeypatch.setattr(checker, "gh_api", lambda path, pull=pull: pull)
		monkeypatch.setattr(checker, "gh_api_list", lambda path, comments=comments: comments)
		monkeypatch.setattr(checker, "_gh_api_paginated_object", lambda path, key, pr=pr: {"check_runs": pr["failed_check_runs"]})
		verdict = checker.check_pr_hand_back(REPO, number, 6.0, 0.0, now)
		assert verdict["state"] == "provider-unavailable" and verdict["done"] is False, number
		assert checker.route_verdict(verdict, "hand_back") == {"action": "wait"}
		assert verdict["hand_backs"] == 0
		# The /implement-plan-claude project checker does not see it as stuck either.
		plain = checker.check_pr(REPO, number, False, 6.0, now)
		assert plain["state"] == "provider-unavailable" and plain["done"] is False, number


class _FakeGitHub:
	"""Same surface as provider_outage.GitHub, backed by dicts."""

	def __init__(self, lists, objects):
		self.lists, self.objects, self.calls, self.next_issue = lists, objects, [], 5800

	def get(self, path):
		self.calls.append(("GET", path))
		return self.objects.get(path)

	def get_list(self, path, list_key=None):
		self.calls.append(("LIST", path))
		return list(self.lists.get(path, []))

	def send(self, method, path, body=None):
		self.calls.append((method, path, body))
		markers = f"repos/{REPO}/issues?labels={outage.OUTAGE_LABEL}&state=open"
		if method == "POST" and path == f"repos/{REPO}/issues":
			issue = {"number": self.next_issue, "state": "open", "user": {"login": WORKFLOW_LOGIN}, "body": body["body"],
				"labels": [{"name": outage.OUTAGE_LABEL}], "created_at": "2026-09-30T17:18:57Z", "html_url": "u"}
			self.next_issue += 1
			self.lists.setdefault(markers, []).append(issue)
			return issue
		if method == "PATCH":
			self.lists[markers] = [issue for issue in self.lists.get(markers, []) if not path.endswith(f"/{issue['number']}")]
		return None


def test_one_alert_then_recovery_redispatches_and_cleans_up(replayed):
	gh = _FakeGitHub({}, {"user": {"login": WORKFLOW_LOGIN}})
	now = dt.datetime(2026, 9, 30, 23, 0, tzinfo=dt.timezone.utc)
	alerts = []
	for data in replayed.values():
		for exported in data["exports"]:
			fields = heal.parse_provider_outage_line("provider_outage " + exported["AUTOFIX_PROVIDER_OUTAGE"])
			result = outage.record(gh, REPO, provider=fields["provider"], status=fields["status"], kind=fields["kind"],
				key=fields["key"], source=f"{REPO}#{data['pr']['number']}", now=now)
			if result["alert"]:
				alerts.append(result["alert_text"])
	assert len(alerts) == 1 and "openrouter HTTP 402 (credits) on key OPENROUTER_API_KEY" in alerts[0]
	assert len([call for call in gh.calls if call[:2] == ("POST", f"repos/{REPO}/issues")]) == 1

	# Recovery: the probe answers 200. The holds a fixer posted before this
	# change, and #5215's in-window cap label, show the clean-up rules.
	prs = []
	for number, data in replayed.items():
		pr = data["pr"]
		labels = [{"name": "ai:review-blocked"}] if any(event["event"] == "labeled" for event in pr["events"]) else []
		prs.append({"number": number, "draft": False, "updated_at": "2026-09-30T21:00:00Z", "title": pr["title"], "body": pr["body"],
			"head": pr["head"], "user": pr["user"], "labels": labels, "base": {"repo": {"default_branch": "main"}}})
		holds = [comment for comment in pr["comments"] if "kind=hold" in comment["body"]]
		gh.lists[f"repos/{REPO}/issues/{number}/comments"] = sorted(data["comments"] + holds, key=lambda comment: comment["id"])
		gh.lists[f"repos/{REPO}/commits/{pr['head']['sha']}/check-runs"] = pr["failed_check_runs"]
		gh.lists[f"repos/{REPO}/issues/{number}/events"] = [event for event in pr["events"] if event["event"] == "labeled"]
		gh.objects[f"repos/{REPO}/pulls/{number}"] = {"mergeable_state": "clean"}
	gh.lists[f"repos/{REPO}/pulls?state=open"] = prs
	result = outage.tick(gh, REPO, [REPO], now, probe=lambda: {"ok": True, "status": "200"}, release_rerun_enabled=False,
		allow_workflow_edits="false", run_id="36800000000")
	summary = result["summary"]
	assert result["recovered"] is True
	assert summary["dispatched"] == [f"{REPO}#5324", f"{REPO}#5178", f"{REPO}#5215"]
	assert summary["labels_removed"] == [f"{REPO}#5215"]
	assert summary["holds_released"] == [f"{REPO}#5324", f"{REPO}#5178", f"{REPO}#5215"]
	assert summary["errors"] == []
	assert not gh.lists[f"repos/{REPO}/issues?labels={outage.OUTAGE_LABEL}&state=open"]
	assert result["alert_text"].startswith("Model provider recovered: openrouter")
	# The released holds no longer stop the §26 checker: the newest claim is a review claim.
	monkeypatch_env = {"CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN": WORKFLOW_LOGIN}
	old_env = {key: os.environ.get(key) for key in monkeypatch_env}
	os.environ.update(monkeypatch_env)
	try:
		for number, data in replayed.items():
			posted = [call[2]["body"] for call in gh.calls if call[0] == "POST" and call[1] == f"repos/{REPO}/issues/{number}/comments"]
			comments = gh.lists[f"repos/{REPO}/issues/{number}/comments"] + [
				{"id": 10**12, "user": {"login": WORKFLOW_LOGIN}, "author_association": "OWNER", "created_at": "2026-09-30T23:00:00Z", "body": posted[0]}
			]
			claims = checker.read_fix_claims(comments, data["pr"]["head"]["sha"], now, trusted_logins=(WORKFLOW_LOGIN,))
			assert claims["claim"]["state"] == "live" and claims["claim"]["kind"] == "review", number
	finally:
		for key, value in old_env.items():
			if value is None:
				os.environ.pop(key, None)
			else:
				os.environ[key] = value


if __name__ == "__main__":
	sys.exit(pytest.main([__file__]))
