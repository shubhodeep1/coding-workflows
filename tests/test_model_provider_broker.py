#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BROKER = REPO_ROOT / "scripts" / "model_provider_broker.py"


def _start_broker(tmp_path: Path) -> tuple[subprocess.Popen[bytes], dict[str, object]]:
	ready_file = tmp_path / "ready.json"
	environment = {"PATH": os.environ["PATH"], "OPENROUTER_API_KEY": "upstream-secret"}
	process = subprocess.Popen(
		["python3", str(BROKER), "--ready-file", str(ready_file), "--max-requests", "2"],
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
	)
	for _ in range(100):
		if ready_file.exists():
			return process, json.loads(ready_file.read_text(encoding="utf-8"))
		if process.poll() is not None:
			break
		time.sleep(0.02)
	process.terminate()
	raise AssertionError("broker did not become ready")


def test_broker_rejects_wrong_path_without_forwarding(tmp_path: Path) -> None:
	process, ready = _start_broker(tmp_path)
	try:
		request = urllib.request.Request(
			str(ready["base_url"]) + "/models",
			data=b"{}",
			headers={"Authorization": f"Bearer {ready['token']}", "Content-Type": "application/json"},
			method="POST",
		)
		try:
			urllib.request.urlopen(request, timeout=2)
		except urllib.error.HTTPError as exc:
			assert exc.code == 404
		else:
			raise AssertionError("broker accepted an unapproved path")
	finally:
		process.terminate()
		process.wait(timeout=3)


def test_broker_rejects_invalid_session_token(tmp_path: Path) -> None:
	process, ready = _start_broker(tmp_path)
	try:
		request = urllib.request.Request(
			str(ready["base_url"]) + "/responses",
			data=b"{}",
			headers={"Authorization": "Bearer attacker-token", "Content-Type": "application/json"},
			method="POST",
		)
		try:
			urllib.request.urlopen(request, timeout=2)
		except urllib.error.HTTPError as exc:
			assert exc.code == 401
		else:
			raise AssertionError("broker accepted an invalid token")
	finally:
		process.terminate()
		process.wait(timeout=3)


def test_model_facing_workflows_use_brokered_secret_free_launches() -> None:
	for relative_path in (
		".github/workflows/clarify.yml",
		".github/workflows/orchestrate.yml",
		".github/workflows/orchestrate_clarify_respond.yml",
		"scripts/run_plan_codex.sh",
	):
		text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
		assert "model_provider_broker_start" in text
		assert "model_provider_broker_exec_unprivileged nobody" in text
		assert "--sandbox read-only" in text
		if relative_path.startswith(".github/workflows/"):
			assert "${{ job.workflow_repository }}" in text
			assert "${{ job.workflow_sha }}" in text
			assert ".codex-workflow-src-main" not in text
	workflow_log = (REPO_ROOT / ".github/workflows/workflow-log-analysis.yml").read_text(encoding="utf-8")
	assert workflow_log.count("model_provider_broker_exec_sanitized bash scripts/codex_heartbeat.sh") == 4
	assert workflow_log.count("model_provider_broker_exec_unprivileged nobody") >= 4
	assert workflow_log.count("--sandbox read-only") >= 4
	resolver = (REPO_ROOT / "scripts/review_conflict_resolve.sh").read_text(encoding="utf-8")
	assert "model_provider_broker_exec_unprivileged" in resolver
	assert "gh_retry gh" not in resolver
	review_workflow = (REPO_ROOT / ".github/workflows/review_autofix.yml").read_text(encoding="utf-8")
	resolver_step = review_workflow.split("- name: Run Codex resolver, validate, stage, commit", 1)[1].split("- name: Actuate validated resolver outcome", 1)[0]
	assert "GH_TOKEN:" not in resolver_step
	assert "GH_PAT:" not in resolver_step
	implement_workflow = (REPO_ROOT / ".github/workflows/implement.yml").read_text(encoding="utf-8")
	assert "model_provider_broker_prepare_codex_writer" in implement_workflow
	assert "model_provider_broker_exec_sanitized bash scripts/codex_thread_reuse.sh direct-run" in implement_workflow
	assert "model_provider_broker_exec_unprivileged nobody codex" in implement_workflow
	assert "WORKFLOW_DEFINITION_SHA: ${{ job.workflow_sha }}" in implement_workflow
	assert "SCRIPT_REF=stable" not in implement_workflow
	assert ".codex-workflow-src-main" not in implement_workflow
	validate_workflow = (REPO_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
	validate_process = (REPO_ROOT / "scripts/validate_process.sh").read_text(encoding="utf-8")
	assert "WORKFLOW_SUPPORT_REF=\"${helper_ref}\"" in validate_workflow
	assert '"scripts/model_provider_broker.py"' in validate_workflow
	assert "model_provider_broker_prepare_codex_writer" in validate_process
	assert "model_provider_broker_exec_sanitized" in validate_process
	poller_workflow = (REPO_ROOT / ".github/workflows/orchestrate_poll.yml").read_text(encoding="utf-8")
	poller_process = (REPO_ROOT / "scripts/orchestrate_poll_process.sh").read_text(encoding="utf-8")
	assert "codex_helpers.sh" in poller_workflow
	assert "model_provider_broker.py" in poller_workflow
	assert "model_provider_broker_prepare_codex_writer" in poller_workflow
	assert "model_provider_broker_exec_sanitized" in poller_process
	assert 'OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"' not in poller_process
	retro_fanout = (REPO_ROOT / "scripts/workflow_retro_fanout.sh").read_text(encoding="utf-8")
	assert "model_provider_broker_exec_sanitized" in retro_fanout
	assert "--sandbox read-only" in retro_fanout
	assert "workflow_log_output_contract.py" in retro_fanout
	for review_script_name in ("review_apply_fixes.sh", "review_consolidate.sh", "review_rb_judge.sh"):
		review_script = (REPO_ROOT / "scripts" / review_script_name).read_text(encoding="utf-8")
		assert "model_provider_broker_start" in review_script
		assert "--provider-base-url" in review_script
		assert "model_provider_broker_stop" in review_script
	assert '"OPENROUTER_API_KEY=${MODEL_PROVIDER_BROKER_TOKEN}"' in (REPO_ROOT / "scripts/review_apply_fixes.sh").read_text(encoding="utf-8")
	assert '"OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"' not in (REPO_ROOT / "scripts/review_apply_fixes.sh").read_text(encoding="utf-8")
	for workflow_name in (
		"cancel_on_pr_close.yml",
		"sync_ai_labels.yml",
		"security-audit.yml",
		"check_failure_triage.yml",
	):
		workflow_text = (REPO_ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8")
		assert "WORKFLOW_DEFINITION_SHA: ${{ job.workflow_sha }}" in workflow_text
		assert "SCRIPT_REF=stable" not in workflow_text
		assert "Checkout workflow support source fallback" not in workflow_text


def test_sanitized_launcher_exposes_only_ephemeral_provider_token(tmp_path: Path) -> None:
	command = f'''
		set -euo pipefail
		source "{REPO_ROOT / 'scripts' / 'codex_helpers.sh'}"
		export RUNTIME_DIR="{tmp_path}"
		export OPENROUTER_API_KEY="upstream-secret"
		export GH_TOKEN="repository-secret"
		export GH_PAT="repository-pat"
		export GITHUB_TOKEN="github-token"
		export ORCHESTRATOR_STATE_AUTH_KEYRING="state-secret"
		export TG_BOT_SECRET="telegram-secret"
		export TG_ADMIN_CHAT_ID="telegram-chat"
		model_provider_broker_start
		model_provider_broker_exec_sanitized sh -c env
		model_provider_broker_stop
	'''
	result = subprocess.run(
		["bash", "-c", command],
		cwd=REPO_ROOT,
		text=True,
		capture_output=True,
		check=True,
	)
	assert "upstream-secret" not in result.stdout
	assert "repository-secret" not in result.stdout
	assert "repository-pat" not in result.stdout
	assert "github-token" not in result.stdout
	assert "state-secret" not in result.stdout
	assert "telegram-secret" not in result.stdout
	assert "telegram-chat" not in result.stdout
	assert "OPENROUTER_API_KEY=" in result.stdout
	assert "GH_TOKEN=" not in result.stdout
	assert "GH_PAT=" not in result.stdout
	assert "GITHUB_TOKEN=" not in result.stdout
	assert "ORCHESTRATOR_STATE_AUTH_KEYRING=" not in result.stdout
	assert "TG_BOT_SECRET=" not in result.stdout
	assert "TG_ADMIN_CHAT_ID=" not in result.stdout


def _read_rejections(path: Path) -> list[dict[str, object]]:
	return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_broker_records_policy_rejections_to_file_and_stderr(tmp_path: Path) -> None:
	"""Every `_reject` answer is mirrored to --rejections-file and stderr.

	PR #4077 (runs 34663517732 / 34654303940): the editor's three attempts and
	the fallback model all failed on the same deterministic HTTP 429, but the
	only trace was the model runtime's opaque `AI_APICallError`. The launcher
	needs a broker-side record to stop retrying.
	"""
	ready_file = tmp_path / "ready.json"
	rejections_file = tmp_path / "nested" / "rejections.jsonl"
	environment = {"PATH": os.environ["PATH"], "OPENROUTER_API_KEY": "upstream-secret"}
	process = subprocess.Popen(
		[
			"python3",
			str(BROKER),
			"--ready-file",
			str(ready_file),
			"--rejections-file",
			str(rejections_file),
			"--max-requests",
			"2",
		],
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
	)
	try:
		for _ in range(100):
			if ready_file.exists():
				break
			if process.poll() is not None:
				break
			time.sleep(0.02)
		ready = json.loads(ready_file.read_text(encoding="utf-8"))
		for path, token, expected in (
			("/models", str(ready["token"]), 404),
			("/responses", "attacker-token", 401),
		):
			request = urllib.request.Request(
				str(ready["base_url"]) + path,
				data=b"{}",
				headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
				method="POST",
			)
			try:
				urllib.request.urlopen(request, timeout=2)
			except urllib.error.HTTPError as exc:
				assert exc.code == expected
			else:
				raise AssertionError(f"broker accepted {path}")
	finally:
		process.terminate()
		_, stderr = process.communicate(timeout=3)

	records = _read_rejections(rejections_file)
	assert [(record["status"], record["path"], record["message"]) for record in records] == [
		(404, "/api/v1/models", "path not allowed"),
		(401, "/api/v1/responses", "invalid broker token"),
	]
	assert all(isinstance(record["ts"], int) for record in records)
	# The record never carries the session token or the upstream key.
	assert "upstream-secret" not in rejections_file.read_text(encoding="utf-8")
	assert str(ready["token"]) not in rejections_file.read_text(encoding="utf-8")
	assert (rejections_file.stat().st_mode & 0o777) == 0o600
	reject_lines = [line for line in stderr.decode("utf-8").splitlines() if line.startswith("MODEL_PROVIDER_BROKER_REJECT ")]
	assert reject_lines == [
		'MODEL_PROVIDER_BROKER_REJECT status=404 path=/api/v1/models message="path not allowed"',
		'MODEL_PROVIDER_BROKER_REJECT status=401 path=/api/v1/responses message="invalid broker token"',
	]


def test_broker_rejections_file_is_optional(tmp_path: Path) -> None:
	process, ready = _start_broker(tmp_path)
	try:
		request = urllib.request.Request(
			str(ready["base_url"]) + "/models",
			data=b"{}",
			headers={"Authorization": f"Bearer {ready['token']}", "Content-Type": "application/json"},
			method="POST",
		)
		try:
			urllib.request.urlopen(request, timeout=2)
		except urllib.error.HTTPError as exc:
			assert exc.code == 404
	finally:
		process.terminate()
		_, stderr = process.communicate(timeout=3)
	assert not list(tmp_path.glob("*.jsonl"))
	assert "MODEL_PROVIDER_BROKER_REJECT status=404" in stderr.decode("utf-8")


def test_broker_start_wires_rejections_file_and_count_helpers(tmp_path: Path) -> None:
	"""codex_helpers.sh passes --rejections-file, exports the path, and the
	count helper only counts deterministic 4xx policy rejections."""
	command = f'''
		set -euo pipefail
		source "{REPO_ROOT / 'scripts' / 'codex_helpers.sh'}"
		export RUNTIME_DIR="{tmp_path}"
		export OPENROUTER_API_KEY="upstream-secret"
		model_provider_broker_start
		echo "rejections_file=${{MODEL_PROVIDER_BROKER_REJECTIONS_FILE}}"
		echo "before=$(model_provider_broker_policy_rejection_count)"
		curl -sS -o /dev/null -X POST -H "Authorization: Bearer ${{MODEL_PROVIDER_BROKER_TOKEN}}" -H "Content-Type: application/json" --data "{{}}" "${{MODEL_PROVIDER_BROKER_BASE_URL}}/models" || true
		echo "after=$(model_provider_broker_policy_rejection_count)"
		echo "last=$(model_provider_broker_last_policy_rejection)"
		# A relayed upstream failure is transient and must not count.
		printf '%s\\n' '{{"message":"upstream request failed","path":"/api/v1/responses","status":502,"ts":1}}' >> "${{MODEL_PROVIDER_BROKER_REJECTIONS_FILE}}"
		echo "after_502=$(model_provider_broker_policy_rejection_count)"
		model_provider_broker_stop
		echo "stopped_count=$(model_provider_broker_policy_rejection_count)"
		echo "stopped_file=${{MODEL_PROVIDER_BROKER_REJECTIONS_FILE:-unset}}"
	'''
	result = subprocess.run(["bash", "-c", command], cwd=REPO_ROOT, text=True, capture_output=True, check=True)
	lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
	assert lines["rejections_file"] == str(tmp_path / "model-provider-broker-rejections.jsonl")
	assert lines["before"] == "0"
	assert lines["after"] == "1"
	assert lines["last"].startswith('status=404 message="path not allowed"')
	assert lines["after_502"] == "1"
	assert lines["stopped_count"] == "0"
	assert lines["stopped_file"] == "unset"
	assert not (tmp_path / "model-provider-broker-rejections.jsonl").exists()


def test_count_helper_is_zero_without_a_rejections_file() -> None:
	command = f'''
		set -euo pipefail
		source "{REPO_ROOT / 'scripts' / 'codex_helpers.sh'}"
		unset MODEL_PROVIDER_BROKER_REJECTIONS_FILE
		echo "count=$(model_provider_broker_policy_rejection_count)"
		echo "last=[$(model_provider_broker_last_policy_rejection)]"
		export MODEL_PROVIDER_BROKER_REJECTIONS_FILE=/nonexistent/rejections.jsonl
		echo "missing=$(model_provider_broker_policy_rejection_count)"
	'''
	result = subprocess.run(["bash", "-c", command], cwd=REPO_ROOT, text=True, capture_output=True, check=True)
	assert "count=0" in result.stdout
	assert "last=[]" in result.stdout
	assert "missing=0" in result.stdout
