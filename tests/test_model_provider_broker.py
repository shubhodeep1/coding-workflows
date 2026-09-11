#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BROKER = REPO_ROOT / "scripts" / "model_provider_broker.py"


def _broker_module():
	spec = importlib.util.spec_from_file_location("model_provider_broker_test", BROKER)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def _broker_policy(module):
	return module.BrokerPolicy(
		frozenset(("openai/test-model",)),
		100,
		150,
		tuple(module.Decimal(value) for value in ("1", "2", "0.1", "0.5")),
	)


def _start_broker(tmp_path: Path) -> tuple[subprocess.Popen[bytes], dict[str, object]]:
	ready_file = tmp_path / "ready.json"
	environment = {"PATH": os.environ["PATH"], "OPENROUTER_API_KEY": "upstream-secret"}
	process = subprocess.Popen(
		[
			"python3", str(BROKER), "--ready-file", str(ready_file), "--max-requests", "2",
			"--allowed-model", "openai/test-model",
		],
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


def test_policy_normalizes_response_limits_and_server_price_ceilings() -> None:
	module = _broker_module()
	policy = _broker_policy(module)
	body, output_tokens = policy.normalize_request(
		"/api/v1/responses",
		b'{"model":"openai/test-model","input":"hello","max_output_tokens":500,"provider":{"max_price":{"prompt":0.5,"completion":9}}}',
	)
	document = json.loads(body)
	assert output_tokens == 100
	assert document["max_output_tokens"] == 100
	assert document["provider"]["max_price"] == {
		"prompt": 0.5,
		"completion": 2,
		"request": 0.1,
		"image": 0.5,
	}


def test_policy_rejects_ambiguous_or_unauthorized_requests() -> None:
	module = _broker_module()
	policy = _broker_policy(module)
	invalid_bodies = (
		b'{"model":"openai/test-model","model":"openai/other"}',
		b'{"input":"hello"}',
		b'{"model":"openai/other"}',
		b'{"model":"openai/test-model","models":["openai/other"]}',
		b'{"model":"openai/test-model","plugins":[]}',
		b'{"model":"openai/test-model","modalities":["text","audio"]}',
	)
	for body in invalid_bodies:
		try:
			policy.normalize_request("/api/v1/responses", body)
		except module.BrokerRequestError:
			pass
		else:
			raise AssertionError(f"broker accepted invalid request: {body!r}")


def test_policy_normalizes_legacy_chat_limit_and_rejects_conflict() -> None:
	module = _broker_module()
	policy = _broker_policy(module)
	body, output_tokens = policy.normalize_request(
		"/api/v1/chat/completions",
		b'{"model":"openai/test-model","messages":[],"max_tokens":40}',
	)
	document = json.loads(body)
	assert output_tokens == 40
	assert document["max_completion_tokens"] == 40
	assert "max_tokens" not in document
	try:
		policy.normalize_request(
			"/api/v1/chat/completions",
			b'{"model":"openai/test-model","messages":[],"max_tokens":40,"max_completion_tokens":41}',
		)
	except module.BrokerRequestError:
		pass
	else:
		raise AssertionError("broker accepted conflicting chat token limits")


def test_request_and_output_budget_reservation_is_race_safe() -> None:
	module = _broker_module()
	policy = _broker_policy(module)
	state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, policy)
	results: list[bool] = []
	results_lock = threading.Lock()

	def reserve() -> None:
		result = state.reserve_request(50)
		with results_lock:
			results.append(result)

	threads = [threading.Thread(target=reserve) for _ in range(20)]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()
	assert results.count(True) == 3
	assert state.output_tokens_reserved == 150


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
		export MODEL_EDITOR="openai/test-model"
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
