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
if [ -n "${{GH_STUB_FAIL_DISPATCH:-}}" ] && [[ "$*" == *dispatches* ]]; then
  echo "HTTP 404: Not Found" >&2
  exit 1
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
		"GH_TOKEN": "x",
		"CLAUDE_ISSUE_PAYLOAD_FILE": str(payload_file),
		"CLAUDE_ISSUE_ROUTINE_ID": "trig_01ABC",
		"CLAUDE_ISSUE_ROUTINE_TOKEN": "sk-test-secret",
		"CLAUDE_ISSUE_RETRY_DELAYS": "0",
		**extra,
	}


def test_intake_fires_routine_and_comments(stubs):
	env = _intake_env(
		stubs,
		_payload(repo="shubhodeep1/digital_pa", issue_number=9),
		CURL_STUB_BODY='{"claude_code_session_id":"session_1","claude_code_session_url":"https://claude.ai/code/session_1"}',
	)
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 0, result.stderr + result.stdout
	args = (stubs["tmp"] / "curl_args.log").read_text()
	assert "https://api.anthropic.com/v1/claude_code/routines/trig_01ABC/fire" in args
	# The token only travels in the header file, never on the command line or stdout.
	assert "sk-test-secret" not in args
	assert "sk-test-secret" not in result.stdout + result.stderr
	assert not list((stubs["tmp"] / "rt").glob("fire_headers.txt"))
	body = json.loads((stubs["tmp"] / "rt" / "fire_body.json").read_text())
	assert "issue: 9" in body["text"]
	calls = stubs["log"].read_text()
	assert "repos/shubhodeep1/digital_pa/issues/9/comments" in calls
	assert "https://claude.ai/code/session_1" in calls


def test_intake_rejects_unregistered_repo(stubs):
	env = _intake_env(stubs, _payload(repo="stranger/repo", issue_number=3))
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 1
	assert "reason=invalid_payload" in result.stdout
	assert not (stubs["tmp"] / "curl_args.log").exists()


def test_intake_without_routine_config_marks_issue(stubs):
	env = _intake_env(stubs, _payload(repo="shubhodeep1/coding-workflows", issue_number=4), CLAUDE_ISSUE_ROUTINE_ID="")
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 1
	assert "reason=routine_not_configured" in result.stdout
	assert "labels[]=ai:claude-handoff-failed" in stubs["log"].read_text()


def test_intake_does_not_retry_client_errors(stubs):
	env = _intake_env(
		stubs,
		_payload(repo="shubhodeep1/coding-workflows", issue_number=4),
		CURL_STUB_CODE="401",
		CLAUDE_ISSUE_RETRY_DELAYS="0 0 0",
	)
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 1
	assert "reason=fire_failed" in result.stdout
	assert "HTTP 401 after 1 attempt(s)" in result.stdout


def test_intake_retries_server_errors(stubs):
	env = _intake_env(
		stubs,
		_payload(repo="shubhodeep1/coding-workflows", issue_number=4),
		CURL_STUB_CODE="503",
		CLAUDE_ISSUE_RETRY_DELAYS="0 0",
	)
	result = _run("claude_issue_intake.sh", env)
	assert result.returncode == 1
	assert "HTTP 503 after 3 attempt(s)" in result.stdout


# --- workflow wiring (no-clash contract) ---------------------------------------------------


def test_clarify_routes_before_codex_and_stages_scripts():
	text = CLARIFY.read_text()
	assert "claude_issue_route.py claude_issue_handoff.sh; do" in text
	assert "AI_ISSUE_IMPLEMENTER: ${{ vars.AI_ISSUE_IMPLEMENTER || 'claude' }}" in text
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
	assert env["CLAUDE_ISSUE_ROUTINE_TOKEN"] == "${{ secrets.CLAUDE_ISSUE_ROUTINE_TOKEN }}"
	assert workflow["permissions"] == {"contents": "read"}
	run_text = INTAKE_WF.read_text()
	# External payload is env-bound, never interpolated into run: bodies.
	assert "${{ github.event.client_payload" not in "".join(
		step.get("run", "") for step in workflow["jobs"]["intake"]["steps"]
	)
	assert "CLIENT_PAYLOAD_JSON: ${{ toJson(github.event.client_payload) }}" in run_text
