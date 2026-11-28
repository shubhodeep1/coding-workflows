"""Tests for scripts/alert_triage_route.sh and .github/workflows/alert-triage.yml
(alert triage plan Phase 2, Refs #9394).

Each test stages a copy of the scripts in a temporary repository root, uses a
local bare repository as the `ai/alert-inbox` origin, and puts fake `gh`,
`curl` and engine support on PATH: one test per routing row, dry run, the
self-fix switch and workflow checks, push failure, the no_inbox skip and the
token-free engine environment.
"""

from __future__ import annotations

import datetime
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "alert-triage.yml"
SELF = "shubhodeep1/coding-workflows"
CONSUMER = "shubhodeep1/consumer"
FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJhIjoxfQ.c2ln"

FAKE_GH = r"""#!/usr/bin/env bash
set -u
log="${FAKE_LOG_DIR}/gh.log"
printf '%s\n' "$*" >> "${log}"
if [ "${1:-}" = "api" ] && [ "${2:-}" = "--paginate" ]; then
	case "$3" in
		*workflow-heal*) cat "${FAKE_LOG_DIR}/heal_issues.jsonl" 2>/dev/null ;;
		*alert-self-fix*) cat "${FAKE_LOG_DIR}/prs.jsonl" 2>/dev/null ;;
	esac
	exit 0
fi
if [ "${1:-}" = "api" ] && [ "${2:-}" = "-X" ] && [ "${3:-}" = "POST" ]; then
	input=""
	prev=""
	for arg in "$@"; do
		[ "${prev}" = "--input" ] && input="${arg}"
		prev="${arg}"
	done
	n="$(ls "${FAKE_LOG_DIR}" | grep -c '^post-' || true)"
	printf '%s\n' "$4" > "${FAKE_LOG_DIR}/post-${n}.target"
	cp "${input}" "${FAKE_LOG_DIR}/post-${n}.json"
	[ "${FAKE_GH_FAIL_POST:-false}" = "true" ] && exit 1
	exit 0
fi
case "$*" in
	*"/jobs?per_page=100"*) echo '{"jobs":[{"id":7,"name":"review","conclusion":"failure"}]}' ;;
	*"/logs"*) echo "Error: something broke" ;;
	*) exit 1 ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_LOG_DIR}/curl.log"
echo '{"ok":true,"result":{"message_id":42}}'
"""

FAKE_ENGINE = r"""
claude_run_selected()
{
	local role="$1" prompt="$2" out="$3"
	{
		echo "role=${role}"
		echo "read_only=${AI_ENGINE_READ_ONLY:-}"
		env | grep -E '^(GH_TOKEN|GITHUB_TOKEN|GH_PAT|TG_BOT_SECRET|ACTIONS_ID_TOKEN_REQUEST_TOKEN|ACTIONS_ID_TOKEN_REQUEST_URL)=' | cut -d= -f1 | sed 's/^/leak=/'
	} >> "${FAKE_LOG_DIR}/engine.log"
	local fp
	fp="$(sed -n 's/^FINGERPRINT: \([0-9a-f]\{64\}\)$/\1/p' "${prompt}" | head -1)"
	local cls="${FAKE_CLASSIFICATION:-noise}"
	[ "${FAKE_ENGINE_FAIL:-false}" = "true" ] && return 1
	printf 'analysis...\n{"classification": "%s", "fingerprint": "%s", "summary": "s", "evidence": ["e"], "proposed_fix": "f", "files": %s, "decisions": []}\n' \
		"${cls}" "${fp}" "${FAKE_FILES:-[]}" > "${out}"
}
"""


class _OidcHandler(http.server.BaseHTTPRequestHandler):
	def do_GET(self) -> None:  # noqa: N802
		body = json.dumps({"value": FAKE_JWT}).encode()
		self.send_response(200)
		self.send_header("Content-Type", "application/json")
		self.end_headers()
		self.wfile.write(body)

	def log_message(self, *_args) -> None:
		return


@pytest.fixture(scope="module")
def oidc_url():
	server = http.server.HTTPServer(("127.0.0.1", 0), _OidcHandler)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	yield f"http://127.0.0.1:{server.server_port}/token?x=1"
	server.shutdown()


def _git(cwd: Path, *args: str) -> str:
	env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
	return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


def _record(n: int, *, message: str, workflow: str = "AI Review", repo: str = CONSUMER) -> dict:
	captured = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)
	run_id = str(500 + n)
	return {
		"captured_at": captured.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "id": hashlib.md5(f"r{n}{message}".encode()).hexdigest(),
		"job": "review", "level": "ERROR", "message": message, "release_sha": "", "run_id": run_id,
		"run_url": f"https://github.com/{repo}/actions/runs/{run_id}", "sent_to_telegram": True, "source_repo": repo, "workflow": workflow,
	}


class Harness:
	def __init__(self, tmp: Path, *, records: list[dict] | None, self_fix_workflow: bool = False) -> None:
		self.tmp = tmp
		self.root = tmp / "repo"
		(self.root / "scripts").mkdir(parents=True)
		for name in ("alert_triage_route.sh", "alert_triage.py", "workflow_failure_heal.py", "tg_helpers.sh"):
			shutil.copy(REPO_ROOT / "scripts" / name, self.root / "scripts" / name)
		(self.root / "prompts").mkdir()
		shutil.copy(REPO_ROOT / "prompts" / "mode-alert-triage.txt", self.root / "prompts")
		(self.root / ".github" / "ai").mkdir(parents=True)
		# A fixture, not a copy of the live config: the pre-push gate's
		# credential-free snapshot does not carry .github/ai/alert_inbox.json.
		# These entries are the pipeline_self_files the routing tests rely on.
		(self.root / ".github" / "ai" / "alert_inbox.json").write_text(json.dumps({
			"capture_enabled": False,
			"route_warning_error_to_inbox": False,
			"pipeline_self_files": [
				".github/workflows/implement.yml",
				".github/workflows/review_autofix.yml",
				".github/workflows/alert-triage.yml",
				".github/workflows/alert-self-fix.yml",
				"scripts/ai_engine.sh",
				"scripts/tg_helpers.sh",
			],
		}), encoding="utf-8")
		workflows = self.root / ".github" / "workflows"
		workflows.mkdir()
		(workflows / "review_autofix.yml").write_text("name: Review (Reusable)\n", encoding="utf-8")
		(workflows / "implement.yml").write_text("name: AI Implement\n", encoding="utf-8")
		if self_fix_workflow:
			(workflows / "alert-self-fix.yml").write_text("name: Alert Self Fix\n", encoding="utf-8")
		(self.root / "workflow-templates").mkdir()
		(workflows / "docs_check.yml").write_text("name: Docs Check (Reusable)\n", encoding="utf-8")
		(self.root / "workflow-templates" / "ai-docs.yml").write_text(
			"name: AI Docs\njobs:\n  d:\n    uses: shubhodeep1/coding-workflows/.github/workflows/docs_check.yml@stable\n", encoding="utf-8")
		(self.root / "workflow-templates" / "ai-review.yml").write_text(
			"name: AI Review\njobs:\n  r:\n    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable\n", encoding="utf-8")
		self.logs = tmp / "logs"
		self.logs.mkdir()
		self.bin = tmp / "bin"
		self.bin.mkdir()
		for name, body in (("gh", FAKE_GH), ("curl", FAKE_CURL)):
			(self.bin / name).write_text(body, encoding="utf-8")
			(self.bin / name).chmod(0o755)
		self.engine = tmp / "engine"
		(self.engine / "scripts").mkdir(parents=True)
		(self.engine / "scripts" / "ai_engine.sh").write_text(FAKE_ENGINE, encoding="utf-8")
		self.origin = tmp / "origin.git"
		_git(tmp, "init", "-q", "--bare", str(self.origin))
		if records is not None:
			seed = tmp / "seed"
			_git(tmp, "init", "-q", str(seed))
			for record in records:
				folder = seed / "inbox" / record["captured_at"][:4] / record["captured_at"][5:7] / record["captured_at"][8:10] / record["captured_at"][11:13]
				folder.mkdir(parents=True, exist_ok=True)
				(folder / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")
			(seed / "README").write_text("inbox\n", encoding="utf-8")
			_git(seed, "add", "-A")
			_git(seed, "commit", "-qm", "seed")
			_git(seed, "push", "-q", str(self.origin), "HEAD:refs/heads/ai/alert-inbox")
		self.runner_temp = tmp / "runner"
		self.runner_temp.mkdir()
		self.output = tmp / "github_output"

	def env(self, **overrides: str) -> dict[str, str]:
		env = {key: value for key, value in os.environ.items() if not key.startswith(("ALERT_TRIAGE", "GITHUB_", "GH_", "TG_", "ACTIONS_ID"))}
		env.update({
			"PATH": f"{self.bin}:{env.get('PATH', '')}",
			"FAKE_LOG_DIR": str(self.logs),
			"GITHUB_REPOSITORY": SELF,
			"GITHUB_RUN_ID": "4242",
			"GITHUB_SERVER_URL": "https://github.com",
			"GITHUB_OUTPUT": str(self.output),
			"GITHUB_TOKEN": "ghs_secret_token_value",
			"GH_TOKEN": "pat_secret_value",
			"RUNNER_TEMP": str(self.runner_temp),
			"ALERT_TRIAGE_REMOTE_URL": str(self.origin),
			"CLAUDE_ENGINE_SUPPORT_DIR": str(self.engine),
			"TG_BOT_SECRET": "bot-secret",
			"TG_ADMIN_CHAT_ID": "1",
			"ALERT_MSG_LEVEL": "WARNING",
			"ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req-token",
			"PYTHONDONTWRITEBYTECODE": "1",
		})
		env.update(overrides)
		return env

	def run(self, step: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
		return subprocess.run(["bash", str(self.root / "scripts" / "alert_triage_route.sh"), step], cwd=self.root, env=env, capture_output=True, text=True, check=False, timeout=180)

	def full(self, **overrides: str) -> str:
		env = self.env(**overrides)
		out = ""
		for step in ("fetch", "prepare", "diagnose", "route"):
			result = self.run(step, env)
			out += result.stdout + result.stderr
			if step != "route":
				assert result.returncode == 0, out
			self.last = result
		return out

	def posts(self) -> list[tuple[str, dict]]:
		out = []
		for target in sorted(self.logs.glob("post-*.target")):
			out.append((target.read_text(encoding="utf-8").strip(), json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))))
		return out

	def telegram_count(self) -> int:
		path = self.logs / "curl.log"
		return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if "api.telegram.org" in line) if path.exists() else 0

	def inbox_files(self) -> list[str]:
		return _git(self.tmp, "--git-dir", str(self.origin), "ls-tree", "-r", "--name-only", "ai/alert-inbox").split()

	def markers(self) -> list[dict]:
		out = []
		for name in self.inbox_files():
			if name.startswith("done/"):
				out.append(json.loads(_git(self.tmp, "--git-dir", str(self.origin), "show", f"ai/alert-inbox:{name}")))
		return out


LIVE = {"ALERT_TRIAGE_DRY_RUN": "false"}


def test_no_inbox_branch_skips(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=None)
	result = harness.run("fetch", harness.env())
	assert result.returncode == 0
	assert "ALERT_TRIAGE skip reason=no_inbox" in result.stdout
	assert "present=false" in harness.output.read_text(encoding="utf-8")


def test_noise_writes_done_markers_and_dispatches_nothing(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: flaky 1"), _record(2, message="ERROR: flaky 2")])
	out = harness.full(FAKE_CLASSIFICATION="noise", **LIVE)
	assert harness.last.returncode == 0, out
	assert "route=done" in out and "commit outcome=pushed" in out
	markers = harness.markers()
	assert len(markers) == 2 and {m["route"] for m in markers} == {"done"}
	assert harness.posts() == [] and harness.telegram_count() == 0
	assert sum(1 for name in harness.inbox_files() if name.startswith("ledger/")) == 1


def test_second_run_is_a_noop(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: flaky 1")])
	harness.full(FAKE_CLASSIFICATION="noise", **LIVE)
	(harness.logs / "engine.log").unlink()
	out = harness.full(FAKE_CLASSIFICATION="noise", **LIVE)
	assert "skip reason=no_records" in out
	assert not (harness.logs / "engine.log").exists()


def test_healable_sends_one_heal_report_after_the_push(tmp_path: Path, oidc_url: str) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: docs step broke", workflow="AI Docs")])
	out = harness.full(FAKE_CLASSIFICATION="healable", FAKE_FILES='["scripts/docs_x.sh"]', ACTIONS_ID_TOKEN_REQUEST_URL=oidc_url, **LIVE)
	assert harness.last.returncode == 0, out
	assert "dispatch kind=heal" in out and "outcome=sent" in out
	posts = harness.posts()
	assert len(posts) == 1
	target, body = posts[0]
	assert target == f"repos/{SELF}/dispatches"
	assert body["event_type"] == "workflow-failure-heal"
	report = body["client_payload"]["report"]
	assert report["source_kind"] == "alert_triage" and report["alert_workflow_path"] == ".github/workflows/docs_check.yml"
	assert report["reporter_run_url"] == f"https://github.com/{SELF}/actions/runs/4242"
	assert body["client_payload"]["report_identity"] == FAKE_JWT
	ledger_name = next(name for name in harness.inbox_files() if name.startswith("ledger/"))
	ledger = json.loads(_git(tmp_path, "--git-dir", str(harness.origin), "show", f"ai/alert-inbox:{ledger_name}"))
	assert len(ledger["attempts"]) == 1


def test_alert_from_a_pipeline_workflow_is_host_routed_to_pipeline_blocked(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: review step broke")])
	out = harness.full(FAKE_CLASSIFICATION="healable", **LIVE)
	assert "classification=pipeline_blocked route=escalate reason=self_fix_disabled" in out
	assert harness.posts() == []


def test_healable_without_a_scope_path_escalates(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: x", workflow="Unknown Workflow")])
	out = harness.full(FAKE_CLASSIFICATION="healable", **LIVE)
	assert "reason=healable_no_scope" in out
	assert harness.posts() == [] and harness.telegram_count() == 1


def test_dry_run_commits_markers_but_sends_nothing(tmp_path: Path, oidc_url: str) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: a", workflow="AI Docs"), _record(2, message="ERROR: unsure b", workflow="AI Plan")])
	out = harness.full(FAKE_CLASSIFICATION="healable", ACTIONS_ID_TOKEN_REQUEST_URL=oidc_url)
	assert "dry_run=true" in out and "outcome=skipped_dry_run" in out and "outcome=dry_run" in out
	assert harness.posts() == [] and harness.telegram_count() == 0
	markers = harness.markers()
	assert len(markers) == 2 and all(marker["dry_run"] is True for marker in markers)


@pytest.mark.parametrize(("switch", "workflow_present", "reason"), [("false", True, "self_fix_disabled"), ("true", False, "self_fix_missing")])
def test_pipeline_blocked_escalates_when_self_fix_is_off_or_missing(tmp_path: Path, switch: str, workflow_present: bool, reason: str) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: engine broke")], self_fix_workflow=workflow_present)
	out = harness.full(FAKE_CLASSIFICATION="healable", FAKE_FILES='["scripts/ai_engine.sh"]', ALERT_TRIAGE_SELF_FIX_ENABLED=switch, **LIVE)
	assert f"reason={reason}" in out
	assert harness.posts() == [] and harness.telegram_count() == 1


def test_pipeline_blocked_dispatches_alert_self_fix(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: engine broke")], self_fix_workflow=True)
	out = harness.full(FAKE_CLASSIFICATION="pipeline_blocked", FAKE_FILES='["scripts/ai_engine.sh"]', ALERT_TRIAGE_SELF_FIX_ENABLED="true", **LIVE)
	assert "dispatch kind=self_fix" in out and "outcome=sent" in out
	(target, body), = harness.posts()
	assert target == f"repos/{SELF}/actions/workflows/alert-self-fix.yml/dispatches"
	assert body["ref"] == "main" and re.fullmatch(r"[0-9a-f]{64}", body["inputs"]["fingerprint"])
	assert json.loads(body["inputs"]["diagnosis"])["classification"] == "pipeline_blocked"


def test_failed_self_fix_dispatch_escalates(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: engine broke")], self_fix_workflow=True)
	out = harness.full(FAKE_CLASSIFICATION="pipeline_blocked", FAKE_FILES='["scripts/ai_engine.sh"]', ALERT_TRIAGE_SELF_FIX_ENABLED="true", FAKE_GH_FAIL_POST="true", **LIVE)
	assert "outcome=failed reason=dispatch_failed" in out
	assert harness.telegram_count() == 1


def test_unsure_and_engine_failure_escalate_once_per_group(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: one"), _record(2, message="ERROR: two", workflow="AI Plan")])
	out = harness.full(FAKE_CLASSIFICATION="unsure", **LIVE)
	assert out.count("reason=unsure") == 2
	assert harness.telegram_count() == 2
	harness2 = Harness(tmp_path / "b", records=[_record(3, message="ERROR: three")])
	out2 = harness2.full(FAKE_ENGINE_FAIL="true", **LIVE)
	assert "outcome=engine_failed" in out2 and "reason=engine_failed" in out2
	assert harness2.telegram_count() == 1


def test_escalation_text_never_echoes_the_alert_message(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: SECRET-ALERT-NARRATIVE")])
	harness.full(FAKE_CLASSIFICATION="unsure", **LIVE)
	assert "SECRET-ALERT-NARRATIVE" not in (harness.logs / "curl.log").read_text(encoding="utf-8")


def test_already_tracked_notes_the_ledger_without_a_model_call(tmp_path: Path) -> None:
	import sys
	sys.path.insert(0, str(REPO_ROOT / "scripts"))
	import alert_triage as at  # noqa: E402
	record = _record(1, message="ERROR: tracked")
	harness = Harness(tmp_path, records=[record])
	fp = at.alert_fingerprint(record["source_repo"], record["workflow"], at.normalise_message(record["message"]))
	issue = {"number": 3, "body": f"<!-- workflow-failure-heal:fp={at.heal_fp(record['workflow'], fp)} -->", "labels": [{"name": "ai:workflow-heal"}], "created_at": "2099-01-01T00:00:00Z"}
	(harness.logs / "heal_issues.jsonl").write_text(json.dumps(issue) + "\n", encoding="utf-8")
	out = harness.full(**LIVE)
	assert "route=ledger_note" in out and not (harness.logs / "engine.log").exists()
	assert harness.markers()[0]["route"] == "ledger_note"


def test_push_failure_dispatches_nothing_and_exits_1(tmp_path: Path, oidc_url: str) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: docs step broke", workflow="AI Docs")])
	hook = harness.origin / "hooks" / "pre-receive"
	hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
	hook.chmod(0o755)
	out = harness.full(FAKE_CLASSIFICATION="healable", ACTIONS_ID_TOKEN_REQUEST_URL=oidc_url, **LIVE)
	assert harness.last.returncode == 1
	assert "commit outcome=failed attempts=3" in out
	assert harness.posts() == [] and harness.telegram_count() == 1


def test_engine_runs_without_tokens_and_read_only(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: x")])
	harness.full(**LIVE)
	log = (harness.logs / "engine.log").read_text(encoding="utf-8")
	assert "role=ALERT_TRIAGE" in log and "read_only=true" in log
	assert "leak=" not in log


def test_missing_engine_support_marks_groups_engine_failed(tmp_path: Path) -> None:
	harness = Harness(tmp_path, records=[_record(1, message="ERROR: x")])
	out = harness.full(CLAUDE_ENGINE_SUPPORT_DIR=str(tmp_path / "absent"), **LIVE)
	assert "outcome=engine_failed" in out and harness.telegram_count() == 1


def test_route_script_keeps_tokens_off_argv() -> None:
	text = (REPO_ROOT / "scripts" / "alert_triage_route.sh").read_text(encoding="utf-8")
	assert "GIT_CONFIG_VALUE_0=" in text
	assert not re.search(r"https://[^\s\"]*\$\{?GITHUB_TOKEN", text)
	assert "tg_send_msg_direct" in text and re.search(r"\btg_send_msg\b(?!_)", text) is None


# --- workflow contract ----------------------------------------------------------


def _workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_triggers_concurrency_and_permissions() -> None:
	text = _workflow()
	assert "- cron: '17 * * * *'" in text and "workflow_dispatch: {}" in text
	assert re.search(r"concurrency:\n  group: alert-triage\n  cancel-in-progress: false\n", text)
	assert "  id-token: write\n" in text
	assert "    if: vars.ALERT_TRIAGE_ENABLED == 'true'\n" in text
	assert "uses: ./.github/actions/claude-pool-token" in text
	assert "ALERT_TRIAGE_DRY_RUN: ${{ vars.ALERT_TRIAGE_DRY_RUN || 'true' }}" in text
	assert "ALERT_TRIAGE_SELF_FIX_ENABLED: ${{ vars.ALERT_TRIAGE_SELF_FIX_ENABLED || 'false' }}" in text
	assert "\t" not in text
	assert len(text.encode("utf-8")) < 50_000


def test_workflow_run_bodies_carry_no_expressions() -> None:
	lines = _workflow().splitlines()
	in_run = False
	run_indent = 0
	for line in lines:
		stripped = line.lstrip()
		indent = len(line) - len(stripped)
		if in_run:
			if stripped and indent <= run_indent:
				in_run = False
			else:
				assert "${{" not in line, line
				continue
		if stripped.startswith("run:"):
			assert "${{" not in stripped, line
			in_run = stripped.rstrip() in ("run: |", "run: >")
			run_indent = indent


def test_only_the_prepare_step_gets_the_pat() -> None:
	text = _workflow()
	assert text.count("secrets.GH_PAT") == 1
	prepare = text.split("- name: Prepare groups", 1)[1].split("- name:", 1)[0]
	assert "secrets.GH_PAT" in prepare
	diagnose = text.split("- name: Diagnose groups", 1)[1].split("- name:", 1)[0]
	# Only the inference key the codex fallback's host-side broker needs.
	assert "TOKEN" not in diagnose and "GH_PAT" not in diagnose
	assert re.findall(r"secrets\.[A-Za-z0-9_]+", diagnose) == ["secrets.OPENROUTER_API_KEY"]
