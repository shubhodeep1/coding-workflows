"""Claude issue implementer: routing, handoff, intake, and the no-clash gates.

Standalone issues default to Claude (AI_ISSUE_IMPLEMENTER, default `claude`);
orchestrator-managed issues and release-gate fixtures always stay on Codex.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import claude_issue_route as route  # noqa: E402

CLARIFY = ROOT / ".github" / "workflows" / "clarify.yml"
PLAN = ROOT / ".github" / "workflows" / "plan.yml"
IMPLEMENT = ROOT / ".github" / "workflows" / "implement.yml"
INTAKE_WF = ROOT / ".github" / "workflows" / "claude-issue-intake.yml"
POLLER = ROOT / "scripts" / "orchestrate_poll_process.sh"
CONTRACT = ROOT / ".github" / "ai" / "label_contract.v1.json"


def _issue(labels=(), body="", title="Fix the thing", number=7):
	return {
		"number": number,
		"title": title,
		"body": body,
		"labels": [{"name": name} for name in labels],
	}


# --- route_issue ---------------------------------------------------------------


@pytest.mark.parametrize(
	("labels", "body", "title", "var", "expected", "reason"),
	[
		((), "", "Fix", "", "claude", "default"),
		((), "", "Fix", "claude", "claude", "repo_var"),
		((), "", "Fix", " CODEX ", "codex", "repo_var"),
		((), "", "Fix", "gpt", "claude", "invalid_repo_var_default"),
		(("ai:orchestrator-managed",), "", "Fix", "claude", "codex", "orchestrator_managed"),
		((), "- Managed by: AI Orchestrator\n", "Fix", "claude", "codex", "orchestrator_managed"),
		(("ai:orchestrator-tracking",), "", "Fix", "claude", "codex", "codex_only_issue_type"),
		(("ai:security-audit",), "", "Fix", "claude", "codex", "codex_only_issue_type"),
		(("ai:retro",), "", "Fix", "claude", "codex", "codex_only_issue_type"),
		((), "", "[E2E Smoke Test] canary", "claude", "codex", "e2e_fixture"),
		((), "", "[e2e Clarify Negative Test] x", "claude", "codex", "e2e_fixture"),
		(("ai:codex",), "", "Fix", "claude", "codex", "label_override"),
		(("ai:claude",), "", "Fix", "codex", "claude", "label_override"),
		(("ai:claude", "ai:codex"), "", "Fix", "claude", "codex", "label_override"),
		(("ai:orchestrator-managed", "ai:claude"), "", "Fix", "claude", "codex", "orchestrator_managed"),
	],
)
def test_route_issue(labels, body, title, var, expected, reason):
	result = route.route_issue(_issue(labels, body, title), var)
	assert result["implementer"] == expected
	assert result["reason"] == reason


def test_mid_title_e2e_mention_is_not_a_fixture():
	assert route.route_issue(_issue(title="Fix [E2E Smoke Test] alerts"), "")["implementer"] == "claude"


def test_body_marker_must_be_a_line_not_prose():
	body = "The orchestrator writes `Managed by: AI Orchestrator` into its issues."
	assert route.route_issue(_issue(body=body), "")["implementer"] == "claude"


@pytest.mark.parametrize("label", ["ai:security", "ai:check-triage", "ai:workflow-heal"])
def test_automation_issues_route_to_claude_and_skip_security_pass(label):
	result = route.route_issue(_issue([label]), "")
	assert result["implementer"] == "claude"
	assert result["skip_security_pass"] is True


def test_human_issue_keeps_security_pass():
	assert route.route_issue(_issue(), "")["skip_security_pass"] is False


def test_labels_accept_plain_strings():
	assert route.route_issue({"labels": ["ai:codex"], "title": "x"}, "")["implementer"] == "codex"


# --- dispatch / validation / fire text ------------------------------------------------


def test_build_dispatch_shape():
	body = route.build_dispatch("o/r", _issue(number=12), "opened", "https://run", True)
	assert body["event_type"] == "claude-issue"
	payload = body["client_payload"]
	assert payload == {
		"schema_version": "claude_issue.v1",
		"repo": "o/r",
		"issue_number": 12,
		"issue_url": "https://github.com/o/r/issues/12",
		"trigger": "opened",
		"skip_security_pass": True,
		"reporter_run_url": "https://run",
	}
	# GitHub rejects client_payload with more than 10 top-level properties.
	assert len(payload) <= 10


@pytest.mark.parametrize(
	("repo", "issue", "trigger"),
	[
		("bad slug", _issue(), "opened"),
		("o/r", {"number": 0}, "opened"),
		("o/r", _issue(), "comment"),
	],
)
def test_build_dispatch_rejects_bad_input(repo, issue, trigger):
	with pytest.raises(ValueError):
		route.build_dispatch(repo, issue, trigger, "", False)


def _payload(**overrides):
	payload = {
		"schema_version": "claude_issue.v1",
		"repo": "Owner/Repo",
		"issue_number": 5,
		"trigger": "opened",
		"skip_security_pass": False,
		"reporter_run_url": "",
	}
	payload.update(overrides)
	return payload


def test_validate_payload_accepts_registered_repo_case_insensitively():
	validated = route.validate_payload(_payload(), ["owner/repo"])
	assert validated["repo"] == "Owner/Repo"
	assert validated["issue_url"] == "https://github.com/Owner/Repo/issues/5"


@pytest.mark.parametrize(
	"overrides",
	[
		{"repo": "other/repo"},
		{"repo": "owner/repo;rm -rf /"},
		{"issue_number": True},
		{"issue_number": -1},
		{"issue_number": "5"},
		{"trigger": "anything"},
		{"schema_version": "v0"},
	],
)
def test_validate_payload_rejects(overrides):
	with pytest.raises(ValueError):
		route.validate_payload(_payload(**overrides), ["owner/repo"])


def test_skip_security_pass_must_be_literal_true():
	assert route.validate_payload(_payload(skip_security_pass="true"), ["owner/repo"])["skip_security_pass"] is False


def test_fire_text_has_fixed_keys_only():
	validated = route.validate_payload(_payload(skip_security_pass=True), ["owner/repo"])
	text = route.build_fire_text(validated)
	assert text.splitlines() == [
		"claude_issue.v1",
		"repo: Owner/Repo",
		"issue: 5",
		"url: https://github.com/Owner/Repo/issues/5",
		"trigger: opened",
		"skip_security_pass: true",
	]


def test_allowlist_is_registry_plus_self(tmp_path):
	registry = tmp_path / "consumer_repos.json"
	registry.write_text(json.dumps(["a/b"]))
	assert route.load_allowed_repos(registry, "shubhodeep1/coding-workflows") == ["a/b", "shubhodeep1/coding-workflows"]
	assert route.load_allowed_repos(tmp_path / "missing.json", "x/y") == ["x/y"]


def test_real_registry_parses():
	repos = route.load_allowed_repos(ROOT / ".github" / "ai" / "consumer_repos.json", "shubhodeep1/coding-workflows")
	assert len(repos) > 1


def test_new_labels_are_in_the_contract():
	labels = json.loads(CONTRACT.read_text())["labels"]
	for name in (route.CLAUDE_LABEL, route.CODEX_LABEL, route.HANDOFF_FAILED_LABEL, route.BLOCKED_LABEL):
		assert name in labels


# --- CLI ----------------------------------------------------------------------------


def _cli(*args):
	return subprocess.run(
		[sys.executable, str(ROOT / "scripts" / "claude_issue_route.py"), *args],
		capture_output=True,
		text=True,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
	)


def test_cli_route_and_validate(tmp_path):
	issue_file = tmp_path / "issue.json"
	issue_file.write_text(json.dumps(_issue(["ai:check-triage"])))
	out = _cli("route", "--issue-json", str(issue_file), "--implementer-var", "")
	assert out.returncode == 0
	assert json.loads(out.stdout) == {"implementer": "claude", "reason": "default", "skip_security_pass": True}

	payload_file = tmp_path / "payload.json"
	payload_file.write_text(json.dumps({"client_payload": _payload(repo="shubhodeep1/coding-workflows")}))
	registry = tmp_path / "registry.json"
	registry.write_text("[]")
	out = _cli("validate-payload", "--payload-json", str(payload_file), "--registry", str(registry))
	assert out.returncode == 0, out.stderr
	payload_file.write_text(json.dumps(_payload(repo="evil/repo")))
	out = _cli("validate-payload", "--payload-json", str(payload_file), "--registry", str(registry))
	assert out.returncode == 2
	assert "not registered" in out.stderr


# --- shell drivers with stubbed gh / curl ----------------------------------------------


def _write_stub(path: Path, body: str) -> None:
	path.write_text("#!/usr/bin/env bash\n" + body)
	path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def stubs(tmp_path):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log = tmp_path / "gh.log"
	_write_stub(
		bin_dir / "gh",
		f"""printf '%s\\n' "$*" >> "{log}"
printf '%s|%s\\n' "${{GH_TOKEN:-}}" "$*" >> "{tmp_path}/gh_tokens.log"
if [ -n "${{GH_STUB_FAIL_DISPATCH:-}}" ] && [[ "$*" == *dispatches* ]]; then
  echo "HTTP 404: Not Found" >&2
  exit 1
fi
if [[ "$*" == *"issues?labels=ai:claude-issue-queue"* ]]; then
  [ -z "${{GH_STUB_FAIL_QUEUE_READ:-}}" ] || {{ echo "HTTP 500" >&2; exit 1; }}
  printf '%s' "${{GH_STUB_QUEUE_JSON:-[]}}"
  exit 0
fi
if [[ "$*" == *"coding-workflows/issues -f title="* ]]; then
  [ -z "${{GH_STUB_FAIL_QUEUE_CREATE:-}}" ] || {{ echo "HTTP 403" >&2; exit 1; }}
  printf '%s\\n' "${{GH_STUB_QUEUE_NUMBER:-77}}"
  exit 0
fi
exit 0
""",
	)
	_write_stub(
		bin_dir / "curl",
		f"""out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -H) printf '%s\\n' "$2" >> "{tmp_path}/curl_headers.log"; shift 2 ;;
    *) printf '%s\\n' "$1" >> "{tmp_path}/curl_args.log"; shift ;;
  esac
done
printf '%s' "${{CURL_STUB_BODY:-{{}}}}" > "$out"
printf '%s' "${{CURL_STUB_CODE:-200}}"
""",
	)
	env = {
		**os.environ,
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"PYTHONDONTWRITEBYTECODE": "1",
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"TG_BOT_SECRET": "",
		"RUNTIME_DIR": str(tmp_path / "rt"),
	}
	return {"env": env, "log": log, "tmp": tmp_path}


def _run(script, env):
	return subprocess.run(["bash", str(ROOT / "scripts" / script)], cwd=ROOT, env=env, capture_output=True, text=True)


def test_handoff_claims_dispatches_and_comments(stubs):
	issue_file = stubs["tmp"] / "issue.json"
	issue_file.write_text(json.dumps(_issue(["ai:planning", "ai:claude-handoff-failed"], number=41)))
	env = {
		**stubs["env"],
		"GITHUB_REPOSITORY": "o/r",
		"ISSUE_NUMBER": "41",
		"ISSUE_META_FILE": str(issue_file),
		"CLAUDE_ISSUE_TRIGGER": "reclarify",
		"CLAUDE_ISSUE_ROUTE_REASON": "default",
		"GH_TOKEN": "x",
	}
	result = _run("claude_issue_handoff.sh", env)
	assert result.returncode == 0, result.stderr
	calls = stubs["log"].read_text()
	assert "repos/o/r/issues/41/labels -f labels[]=ai:claude" in calls
	assert "repos/shubhodeep1/coding-workflows/dispatches" in calls
	assert "labels/ai%3Aplanning" in calls
	assert "labels/ai%3Aclaude-handoff-failed" in calls
	# Only labels the issue carries are removed.
	assert "labels/ai%3Aawaiting-approval" not in calls
	assert "CLAUDE_ISSUE_HANDOFF dispatched issue=41" in result.stdout
	assert "ai:claude-issue-routed:v1" in calls


def test_handoff_failure_marks_issue_and_exits_zero(stubs):
	issue_file = stubs["tmp"] / "issue.json"
	issue_file.write_text(json.dumps(_issue(number=42)))
	env = {
		**stubs["env"],
		"GITHUB_REPOSITORY": "o/r",
		"ISSUE_NUMBER": "42",
		"ISSUE_META_FILE": str(issue_file),
		"GH_TOKEN": "x",
		"GH_STUB_FAIL_DISPATCH": "1",
	}
	result = _run("claude_issue_handoff.sh", env)
	assert result.returncode == 0, result.stderr
	calls = stubs["log"].read_text()
	assert "labels[]=ai:claude-handoff-failed" in calls
	assert "ai:claude-handoff-failed:v1" in calls
	assert "CLAUDE_ISSUE_HANDOFF error dispatch_failed issue=42" in result.stdout


def _intake_env(stubs, payload, **extra):
	payload_file = stubs["tmp"] / "payload.json"
	payload_file.write_text(json.dumps(payload))
	return {
		**stubs["env"],
		"GITHUB_REPOSITORY": "shubhodeep1/coding-workflows",
		"GH_TOKEN": "pat-token",
		"CLAUDE_ISSUE_PAYLOAD_FILE": str(payload_file),
		"CLAUDE_ISSUE_QUEUE_TOKEN": "gha-token",
		"RUN_URL": "https://github.com/shubhodeep1/coding-workflows/actions/runs/123",
		**extra,
	}


def test_intake_queues_issue_with_github_token_and_comments(stubs):
	env = _intake_env(stubs, _payload(repo="shubhodeep1/digital_pa", issue_number=9), GH_STUB_QUEUE_NUMBER="88")
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "CLAUDE_ISSUE_INTAKE queued repo=shubhodeep1/digital_pa issue=9 trigger=opened queue_issue=88" in result.stdout
	tokens = (stubs["tmp"] / "gh_tokens.log").read_text().splitlines()
	create = [line for line in tokens if "coding-workflows/issues -f title=" in line]
	assert len(create) == 1
	# The queue issue is opened with GITHUB_TOKEN so no workflow reacts to it.
	assert create[0].startswith("gha-token|")
	assert "title=[claude-issue-queue] shubhodeep1/digital_pa#9" in create[0]
	calls = stubs["log"].read_text()
	assert "-f labels[]=ai:claude-issue-queue --jq .number" in calls
	# The body carries only the fixed-key payload.
	assert "```text\nclaude_issue.v1\nrepo: shubhodeep1/digital_pa\nissue: 9\n" in calls
	# The target-issue comment still goes through GH_PAT and keeps its marker.
	comment = [line for line in tokens if "repos/shubhodeep1/digital_pa/issues/9/comments" in line]
	assert len(comment) == 1 and comment[0].startswith("pat-token|")
	assert "ai:claude-issue-dispatched:v1" in comment[0]
	assert "https://github.com/shubhodeep1/coding-workflows/issues/88" in stubs["log"].read_text()
	# No routine is fired any more.
	assert not (stubs["tmp"] / "curl_args.log").exists()


def test_intake_reuses_open_queue_item(stubs):
	existing = [{"number": 55, "title": "[claude-issue-queue] shubhodeep1/digital_pa#9", "user": {"login": "github-actions[bot]"}}]
	env = _intake_env(stubs, _payload(repo="shubhodeep1/digital_pa", issue_number=9, trigger="reclarify"), GH_STUB_QUEUE_JSON=json.dumps(existing))
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "already_queued repo=shubhodeep1/digital_pa issue=9 trigger=reclarify queue_issue=55" in result.stdout
	assert "-f title=" not in stubs["log"].read_text()


def test_intake_ignores_same_title_from_other_author(stubs):
	spoof = [{"number": 55, "title": "[claude-issue-queue] shubhodeep1/digital_pa#9", "user": {"login": "someone"}}]
	env = _intake_env(stubs, _payload(repo="shubhodeep1/digital_pa", issue_number=9), GH_STUB_QUEUE_JSON=json.dumps(spoof))
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "queue_issue=77" in result.stdout


def test_intake_rejects_unregistered_repo(stubs):
	env = _intake_env(stubs, _payload(repo="stranger/repo", issue_number=3))
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 1
	assert "reason=invalid_payload" in result.stdout
	assert "-f title=" not in (stubs["log"].read_text() if stubs["log"].exists() else "")


def test_intake_without_queue_token_marks_issue(stubs):
	env = _intake_env(stubs, _payload(repo="shubhodeep1/coding-workflows", issue_number=4), CLAUDE_ISSUE_QUEUE_TOKEN="")
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 1
	assert "reason=queue_not_configured" in result.stdout
	assert "labels[]=ai:claude-handoff-failed" in stubs["log"].read_text()


@pytest.mark.parametrize("failure", ["GH_STUB_FAIL_QUEUE_READ", "GH_STUB_FAIL_QUEUE_CREATE"])
def test_intake_queue_failure_marks_issue(stubs, failure):
	env = _intake_env(stubs, _payload(repo="shubhodeep1/coding-workflows", issue_number=4), **{failure: "1"})
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 1
	assert "reason=queue_failed" in result.stdout
	assert "labels[]=ai:claude-handoff-failed" in stubs["log"].read_text()


def test_intake_logs_deprecated_routine_id_without_firing(stubs):
	env = _intake_env(stubs, _payload(repo="shubhodeep1/coding-workflows", issue_number=4), CLAUDE_ISSUE_ROUTINE_ID="trig_01ABC", CLAUDE_ISSUE_ROUTINE_TOKEN="sk-test-secret")
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "notice routine_deprecated" in result.stdout
	assert "sk-test-secret" not in result.stdout + result.stderr
	assert not (stubs["tmp"] / "curl_args.log").exists()


# --- queue parsing (pickup) and staleness (watchdog) -----------------------------------------


def _validated(repo="shubhodeep1/digital_pa", number=9, trigger="opened"):
	return {
		"repo": repo,
		"issue_number": number,
		"issue_url": f"https://github.com/{repo}/issues/{number}",
		"trigger": trigger,
		"skip_security_pass": False,
	}


def _queue_item(number, validated=None, author="github-actions[bot]", created="2026-09-26T00:00:00Z", labels=("ai:claude-issue-queue",), body=None, title=None):
	validated = validated or _validated()
	rendered = route.build_queue_issue(validated, "https://github.com/shubhodeep1/coding-workflows/actions/runs/1")
	return {
		"number": number,
		"title": rendered["title"] if title is None else title,
		"body": rendered["body"] if body is None else body,
		"user": {"login": author},
		"labels": [{"name": name} for name in labels],
		"state": "open",
		"created_at": created,
	}


REGISTRY_ALLOWED = ["shubhodeep1/digital_pa", "shubhodeep1/coding-workflows"]


def test_parse_fire_text_round_trips():
	validated = _validated(trigger="reclarify")
	assert route.parse_fire_text(route.build_fire_text(validated)) == validated


@pytest.mark.parametrize(
	"mutate",
	[
		lambda t: t.replace("claude_issue.v1", "claude_issue.v2"),
		lambda t: t + "extra: line\n",
		lambda t: t.replace("trigger: opened", "trigger: evil"),
		lambda t: t.replace("url: https://github.com/shubhodeep1/digital_pa/issues/9", "url: https://evil.example/9"),
		lambda t: t.replace("issue: 9", "issue: 09"),
		lambda t: t + "repo: other/repo\n",
		lambda t: t.replace("skip_security_pass: false\n", ""),
	],
)
def test_parse_fire_text_rejects(mutate):
	with pytest.raises(ValueError):
		route.parse_fire_text(mutate(route.build_fire_text(_validated())))


def test_queue_issue_carries_no_prose_and_drops_bad_run_url():
	rendered = route.build_queue_issue(_validated(), "https://evil.example/x")
	assert rendered["label"] == "ai:claude-issue-queue"
	assert "Intake run:" not in rendered["body"]
	assert rendered["body"].startswith("<!-- ai:claude-issue-queue:v1 -->\n")


def test_queue_pending_groups_duplicates_and_ignores_untrusted():
	issues = [
		_queue_item(12, _validated(trigger="reclarify")),
		_queue_item(10),
		_queue_item(11, _validated(repo="shubhodeep1/coding-workflows", number=3)),
		_queue_item(13, author="mallory"),
		_queue_item(14, _validated(repo="stranger/repo", number=1)),
		_queue_item(15, body="<!-- ai:claude-issue-queue:v1 -->\nno payload"),
		_queue_item(16, title="[claude-issue-queue] shubhodeep1/digital_pa#99"),
		{"number": 17, "pull_request": {}, "labels": [{"name": "ai:claude-issue-queue"}]},
	]
	out = route.queue_pending(issues, REGISTRY_ALLOWED)
	assert [(e["repo"], e["issue_number"]) for e in out["pending"]] == [("shubhodeep1/digital_pa", 9), ("shubhodeep1/coding-workflows", 3)]
	first = out["pending"][0]
	assert [q["number"] for q in first["queue_issues"]] == [10, 12]
	assert first["trigger"] == "opened"
	assert first["fire_text"] == route.build_fire_text(_validated())
	assert {i["queue_issue"]: i["reason"].split(":")[0] for i in out["ignored"]} == {
		13: "untrusted_author",
		14: "repo_not_registered",
		15: "no_payload",
		16: "title_mismatch",
	}
	assert out["remaining"] == 0


def test_queue_pending_accepts_crlf_bodies_and_limits():
	items = [_queue_item(n, _validated(number=n)) for n in range(1, 5)]
	items[0]["body"] = items[0]["body"].replace("\n", "\r\n")
	out = route.queue_pending(items, REGISTRY_ALLOWED, limit=3)
	assert [e["issue_number"] for e in out["pending"]] == [1, 2, 3]
	assert out["remaining"] == 1


def test_queue_stale_flags_old_trusted_items_once():
	from datetime import datetime, timezone

	now = datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)
	issues = [
		_queue_item(1, created="2026-09-26T01:00:00Z"),
		_queue_item(2, created="2026-09-26T05:00:00Z"),
		_queue_item(3, created="2026-09-26T00:00:00Z", labels=("ai:claude-issue-queue", "ai:claude-issue-queue-stale")),
		_queue_item(4, created="2026-09-25T00:00:00Z", author="mallory"),
	]
	stale = route.queue_stale(issues, now, 3)
	assert [(s["number"], s["age_hours"]) for s in stale] == [(1, 5.0)]


def test_cli_queue_pending_and_stale(tmp_path):
	issues_file = tmp_path / "q.json"
	issues_file.write_text(json.dumps([_queue_item(10)]))
	registry = tmp_path / "registry.json"
	registry.write_text(json.dumps(["shubhodeep1/digital_pa"]))
	out = _cli("queue-pending", "--issues-json", str(issues_file), "--registry", str(registry))
	assert out.returncode == 0, out.stderr
	assert json.loads(out.stdout)["pending"][0]["issue_url"] == "https://github.com/shubhodeep1/digital_pa/issues/9"
	out = _cli("queue-stale", "--issues-json", str(issues_file), "--stale-hours", "3", "--now", "2026-09-26T04:00:00Z")
	assert out.returncode == 0, out.stderr
	assert [s["number"] for s in json.loads(out.stdout)] == [10]
	issues_file.write_text("{}")
	assert _cli("queue-pending", "--issues-json", str(issues_file), "--registry", str(registry)).returncode == 2


def _watchdog_env(stubs, queue):
	return {
		**stubs["env"],
		"GITHUB_REPOSITORY": "shubhodeep1/coding-workflows",
		"GH_TOKEN": "gha-token",
		"GH_STUB_QUEUE_JSON": json.dumps(queue),
	}


def test_watchdog_labels_stale_items_and_exits_zero(stubs):
	queue = [_queue_item(21, created="2020-01-01T00:00:00Z"), _queue_item(22, created="2999-01-01T00:00:00Z")]
	result = _run("claude_issue_queue_watchdog.sh", _watchdog_env(stubs, queue))
	assert result.returncode == 0, result.stderr + result.stdout
	calls = stubs["log"].read_text()
	assert "repos/shubhodeep1/coding-workflows/issues/21/labels -f labels[]=ai:claude-issue-queue-stale" in calls
	assert "issues/22/labels" not in calls
	assert "CLAUDE_ISSUE_QUEUE_WATCHDOG checked open=2 newly_stale=1" in result.stdout


def test_watchdog_fails_open_on_read_error(stubs):
	env = {**_watchdog_env(stubs, []), "GH_STUB_FAIL_QUEUE_READ": "1"}
	result = _run("claude_issue_queue_watchdog.sh", env)
	assert result.returncode == 0
	assert "warn queue_read_failed" in result.stdout


# --- workflow wiring (no-clash contract) ---------------------------------------------------


def test_clarify_routes_before_codex_and_stages_scripts():
	text = CLARIFY.read_text()
	assert "claude_issue_route.py claude_issue_handoff.sh; do" in text
	# Unset must reach the router as "" so the routed comment says `default`.
	assert "AI_ISSUE_IMPLEMENTER: ${{ vars.AI_ISSUE_IMPLEMENTER || '' }}" in text
	assert route.route_issue(_issue(), "") == {"implementer": "claude", "reason": "default", "skip_security_pass": False}
	assert "reason=claude_routed outcome=handoff" in text
	steps = yaml.safe_load(text)["jobs"]["clarify"]["steps"]
	names = [step["name"] for step in steps]
	route_idx = names.index("Decide clarify route")
	handoff_idx = names.index("Claude issue handoff")
	release_idx = names.index("Release Claude claim on switch to Codex")
	assert route_idx < release_idx < handoff_idx < names.index("Run Codex")
	handoff = steps[handoff_idx]
	assert "issue_implementer == 'claude'" in handoff["if"]
	assert handoff["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT }}"


def test_plan_and_implement_skip_claude_claimed_issues():
	assert "gate=trigger_validation reason=claude_routed outcome=skip" in PLAN.read_text()
	assert "gate=phase_precheck reason=claude_routed outcome=skip" in IMPLEMENT.read_text()


def test_standalone_stall_recovery_skips_claude_claimed_issues():
	assert "STALL_SKIP issue=${issue_num} reason=claude_routed action=none" in POLLER.read_text()


def test_intake_workflow_triggers_and_secret_binding():
	workflow = yaml.safe_load(INTAKE_WF.read_text())
	triggers = workflow[True] if True in workflow else workflow["on"]
	assert triggers["repository_dispatch"]["types"] == ["claude-issue"]
	assert set(triggers["workflow_dispatch"]["inputs"]) == {"repo", "issue_number"}
	env = workflow["jobs"]["intake"]["env"]
	# The queue is written with GITHUB_TOKEN; the routine token is no longer bound (#4525).
	assert env["CLAUDE_ISSUE_QUEUE_TOKEN"] == "${{ github.token }}"
	assert "CLAUDE_ISSUE_ROUTINE_TOKEN" not in env
	assert workflow["permissions"] == {"contents": "read", "issues": "write"}
	run_text = INTAKE_WF.read_text()
	# External payload is env-bound, never interpolated into run: bodies.
	assert "${{ github.event.client_payload" not in "".join(
		step.get("run", "") for step in workflow["jobs"]["intake"]["steps"]
	)
	assert "CLIENT_PAYLOAD_JSON: ${{ toJson(github.event.client_payload) }}" in run_text


def test_watchdog_workflow_is_hourly_and_scoped():
	workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "claude-issue-queue-watchdog.yml").read_text())
	triggers = workflow[True] if True in workflow else workflow["on"]
	assert triggers["schedule"] == [{"cron": "17 * * * *"}]
	assert workflow["permissions"] == {"contents": "read", "issues": "write"}
	job = workflow["jobs"]["watchdog"]
	assert job["if"] == "github.repository == 'shubhodeep1/coding-workflows'"
	assert job["env"]["GH_TOKEN"] == "${{ github.token }}"
	assert "bash scripts/claude_issue_queue_watchdog.sh" in job["steps"][-1]["run"]


def test_queue_labels_are_in_the_contract():
	labels = json.loads(CONTRACT.read_text())["labels"]
	assert route.QUEUE_LABEL in labels and route.QUEUE_STALE_LABEL in labels


def test_cli_queue_pending_fetches_with_one_gh_read(stubs, tmp_path):
	registry = tmp_path / "registry.json"
	registry.write_text(json.dumps(["shubhodeep1/digital_pa"]))
	env = {**stubs["env"], "GH_STUB_QUEUE_JSON": json.dumps([_queue_item(10)])}
	cmd = [sys.executable, str(ROOT / "scripts" / "claude_issue_route.py"), "queue-pending", "--fetch-repo", "shubhodeep1/coding-workflows", "--registry", str(registry)]
	out = subprocess.run(cmd, capture_output=True, text=True, env=env)
	assert out.returncode == 0, out.stderr
	assert json.loads(out.stdout)["pending"][0]["issue_number"] == 9
	assert stubs["log"].read_text().splitlines() == ["api repos/shubhodeep1/coding-workflows/issues?labels=ai:claude-issue-queue&state=open&per_page=100"]
	env["GH_STUB_FAIL_QUEUE_READ"] = "1"
	assert subprocess.run(cmd, capture_output=True, text=True, env=env).returncode == 3
