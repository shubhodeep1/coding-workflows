#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import socket
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


def test_policy_keeps_extreme_decimal_exponents_bounded() -> None:
	module = _broker_module()
	policy = _broker_policy(module)
	body, _ = policy.normalize_request(
		"/api/v1/responses",
		b'{"model":"openai/test-model","input":"hello","temperature":1e-1000000000,"provider":{"max_price":{"prompt":"1e-1000000000"}}}',
	)
	assert len(body) < 1000
	assert body.count(b"1E-1000000000") == 2


def test_policy_rejects_normalized_bodies_over_request_limit() -> None:
	module = _broker_module()
	policy = _broker_policy(module)
	request_body = b'{"model":"openai/test-model","input":"hello"}'
	module.MAX_REQUEST_BODY_BYTES = len(request_body)
	try:
		policy.normalize_request("/api/v1/responses", request_body)
	except module.BrokerRequestError:
		pass
	else:
		raise AssertionError("broker accepted an oversized normalized request")


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
		b'{"model":"openai/test-model","temperature":1e-999999999999999999999999999999999999}',
		b'{"model":"openai/test-model","input":' + (b"9" * 5000) + b"}",
		b'{"model":"openai/test-model","input":' + (b"[" * 2000) + (b"]" * 2000) + b"}",
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
		b'{"model":"openai/test-model","messages":[],"max_tokens":40,"n":1}',
	)
	document = json.loads(body)
	assert output_tokens == 40
	assert document["max_completion_tokens"] == 40
	assert document["n"] == 1
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
	for invalid_choice_count in (True, 0, 2, "1"):
		try:
			policy.normalize_request(
				"/api/v1/chat/completions",
				json.dumps({"model": "openai/test-model", "messages": [], "n": invalid_choice_count}).encode("utf-8"),
			)
		except module.BrokerRequestError:
			pass
		else:
			raise AssertionError(f"broker accepted invalid choice count: {invalid_choice_count!r}")


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


def test_broker_connection_admission_is_bounded_and_recovers() -> None:
	module = _broker_module()
	assert module.BROKER_CLIENT_READ_TIMEOUT_SECONDS == 30
	assert module.BROKER_MAX_ACTIVE_CONNECTIONS == 8
	module.BROKER_CLIENT_READ_TIMEOUT_SECONDS = 2
	state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, _broker_policy(module))
	server = module.BrokerServer(("127.0.0.1", 0), state)
	server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
	server_thread.start()
	partial_clients: list[socket.socket] = []
	try:
		partial_request = (
			b"POST /api/v1/responses HTTP/1.1\r\n"
			b"Host: 127.0.0.1\r\n"
			b"Authorization: Bearer token\r\n"
			b"Content-Type: application/json\r\n"
			b"Content-Length: 64\r\n\r\n{"
		)
		partial_header = b"POST /api/v1/responses HTTP/1.1\r\nHost: 127.0.0.1"
		for index in range(module.BROKER_MAX_ACTIVE_CONNECTIONS):
			client = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
			client.sendall(partial_header if index % 2 else partial_request)
			partial_clients.append(client)
		deadline = time.monotonic() + 1
		while server._active_connection_slots._value != 0 and time.monotonic() < deadline:
			time.sleep(0.01)
		assert server._active_connection_slots._value == 0

		excess_client = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
		excess_client.settimeout(0.5)
		excess_client.sendall(partial_request)
		try:
			assert excess_client.recv(1) == b""
		except ConnectionResetError:
			pass
		finally:
			excess_client.close()

		deadline = time.monotonic() + 4
		while server._active_connection_slots._value != module.BROKER_MAX_ACTIVE_CONNECTIONS and time.monotonic() < deadline:
			time.sleep(0.01)
		assert server._active_connection_slots._value == module.BROKER_MAX_ACTIVE_CONNECTIONS

		request = urllib.request.Request(
			f"http://127.0.0.1:{server.server_port}/api/v1/responses",
			data=b"{}",
			headers={"Authorization": "Bearer token", "Content-Type": "application/json"},
			method="POST",
		)
		try:
			urllib.request.urlopen(request, timeout=2)
		except urllib.error.HTTPError as error:
			assert error.code == 400
		else:
			raise AssertionError("broker did not accept a normal request after timed-out clients released their slots")
	finally:
		for client in partial_clients:
			client.close()
		server.shutdown()
		server.server_close()


def test_model_facing_workflows_use_brokered_secret_free_launches() -> None:
	for relative_path in (
		".github/workflows/orchestrate.yml",
		"scripts/run_plan_codex.sh",
	):
		text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
		assert "model_provider_broker_start" in text
		assert "model_provider_broker_exec_unprivileged nobody" in text
		assert "--sandbox read-only" in text
		if relative_path.startswith(".github/workflows/"):
			assert "${{ fromJSON(toJSON(job)).workflow_repository }}" in text
			assert "${{ fromJSON(toJSON(job)).workflow_sha }}" in text
			assert ".codex-workflow-src-main" not in text
	# clarify.yml and orchestrate_clarify_respond.yml are secret-free by a
	# different mechanism: they run Codex through
	# scripts/clarify_isolated_run.sh, which shells out to a docker
	# --network none sandbox and a host-side scripts/clarify_openrouter_broker.py,
	# instead of the in-workflow model_provider_broker_start/exec launcher.
	# The identity/no-moving-ref-fallback hardening still applies to them.
	for relative_path in (
		".github/workflows/clarify.yml",
		".github/workflows/orchestrate_clarify_respond.yml",
	):
		text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
		assert "clarify_isolated_run.sh" in text
		assert "clarify_openrouter_broker.py" in text
		assert "model_provider_broker_start" not in text
		assert "${{ fromJSON(toJSON(job)).workflow_repository }}" in text
		assert "${{ fromJSON(toJSON(job)).workflow_sha }}" in text
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
	assert "model_provider_broker_prepare_isolated_writer nobody" in implement_workflow
	assert 'CODEX_THREAD_REUSE_REAL_CODEX="${IMPLEMENT_ISOLATED_CODEX_LAUNCHER}"' in implement_workflow
	assert '--process-group-file "${implement_process_group_file}"' in implement_workflow
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/codex_thread_reuse.sh" direct-run' in implement_workflow
	assert "model_provider_broker_finish_isolated_writer && model_provider_broker_stop" not in implement_workflow
	assert "writer_isolation_verify_process_group_stopped" in implement_workflow
	assert "model_provider_broker_exec_unprivileged nobody codex" in implement_workflow
	assert "WORKFLOW_DEFINITION_SHA: ${{ fromJSON(toJSON(job)).workflow_sha }}" in implement_workflow
	assert "SCRIPT_REF=stable" not in implement_workflow
	assert ".codex-workflow-src-main" not in implement_workflow
	assert 'model_catalog_path: "scripts/codex_model_catalog.json"' in implement_workflow
	assert 'source "${duplicate_notice_immutable_support_root}/scripts/tg_helpers.sh"' in implement_workflow
	assert 'source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"' not in implement_workflow.split("- name: Telegram duplicate PR notification", 1)[1].split("- name: Exit when existing PR is found", 1)[0]
	validate_workflow = (REPO_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
	validate_process = (REPO_ROOT / "scripts/validate_process.sh").read_text(encoding="utf-8")
	assert "WORKFLOW_SUPPORT_REF=\"${support_sha}\"" in validate_workflow
	assert '"scripts/model_provider_broker.py"' in validate_workflow
	assert "model_provider_broker_prepare_codex_readonly nobody" in validate_process
	assert 'CODEX_THREAD_REUSE_REAL_CODEX="${VALIDATE_ISOLATED_CODEX_LAUNCHER}"' in validate_process
	assert 'validate_codex_argv=(bash "${CODEX_THREAD_REUSE_HELPER}" direct-run)' in validate_process
	assert "--sandbox danger-full-access" not in validate_process
	poller_workflow = (REPO_ROOT / ".github/workflows/orchestrate_poll.yml").read_text(encoding="utf-8")
	poller_process = (REPO_ROOT / "scripts/orchestrate_poll_process.sh").read_text(encoding="utf-8")
	assert 'MODEL_PROVIDER_BROKER_ALLOWED_MODELS="${MODEL_EDITOR}${WORKFLOW_EDITOR_MODEL:+,${WORKFLOW_EDITOR_MODEL}}" model_provider_broker_start' in poller_workflow
	assert "codex_helpers.sh" in poller_workflow
	assert "model_provider_broker.py" in poller_workflow
	assert "model_provider_broker_prepare_codex_writer" in poller_workflow
	assert "model_provider_broker_exec_sanitized" in poller_process
	assert 'OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"' not in poller_process
	retro_fanout = (REPO_ROOT / "scripts/workflow_retro_fanout.sh").read_text(encoding="utf-8")
	assert "model_provider_broker_exec_sanitized" in retro_fanout
	assert "--sandbox read-only" in retro_fanout
	assert "workflow_log_output_contract.py" in retro_fanout
	for review_script_name in ("review_consolidate.sh", "review_rb_judge.sh"):
		review_script = (REPO_ROOT / "scripts" / review_script_name).read_text(encoding="utf-8")
		assert "model_provider_broker_start" in review_script
		assert "--provider-base-url" in review_script
		assert "model_provider_broker_stop" in review_script
	# The editor runs in review_untrusted_sandbox.sh behind the budgeted
	# clarify_openrouter_broker.py review relay, not the loopback broker.
	apply_fixes = (REPO_ROOT / "scripts/review_apply_fixes.sh").read_text(encoding="utf-8")
	assert 'review_untrusted_sandbox.sh" run' in apply_fixes
	assert "--provider-base-url" not in apply_fixes
	assert '"OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"' not in apply_fixes
	for workflow_name in (
		"cancel_on_pr_close.yml",
		"sync_ai_labels.yml",
		"check_failure_triage.yml",
	):
		workflow_text = (REPO_ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8")
		assert "WORKFLOW_DEFINITION_SHA: ${{ fromJSON(toJSON(job)).workflow_sha }}" in workflow_text
		assert "SCRIPT_REF=stable" not in workflow_text
		assert "Checkout workflow support source fallback" not in workflow_text
	# security-audit.yml resolves its support commit from the validated
	# workflow identity (protected main, the stable tag, or a consumer SHA pin).
	security_audit = (REPO_ROOT / ".github/workflows/security-audit.yml").read_text(encoding="utf-8")
	assert "WORKFLOW_JOB_JSON: ${{ toJSON(job) }}" in security_audit
	assert "SCRIPT_REF=stable" not in security_audit
	assert "Checkout workflow support source fallback" not in security_audit
	helpers_text = (REPO_ROOT / "scripts" / "codex_helpers.sh").read_text(encoding="utf-8")
	assert "unshare --fork --pid --mount-proc --kill-child=KILL" in helpers_text
	assert helpers_text.index('kill -TERM "${broker_pid}"') < helpers_text.index('setfacl --restore="${MODEL_PROVIDER_BROKER_ACL_BACKUP}"')
	assert 'setfacl --restore="${MODEL_PROVIDER_BROKER_ACL_BACKUP}" >/dev/null 2>&1 || return 1' not in helpers_text


def test_model_facing_workflows_export_broker_price_policy() -> None:
	price_defaults = {
		"MODEL_PROVIDER_BROKER_MAX_PROMPT_PRICE": "10",
		"MODEL_PROVIDER_BROKER_MAX_COMPLETION_PRICE": "30",
		"MODEL_PROVIDER_BROKER_MAX_REQUEST_PRICE": "0.10",
		"MODEL_PROVIDER_BROKER_MAX_IMAGE_PRICE": "1",
	}
	# clarify.yml and orchestrate_clarify_respond.yml run Codex through
	# scripts/clarify_isolated_run.sh (docker --network none, host-side
	# scripts/clarify_openrouter_broker.py) rather than the in-workflow
	# model_provider_broker_start/exec launcher; that relay applies the same
	# MODEL_PROVIDER_BROKER_MAX_* budgets.
	for workflow_name in (
		"check_failure_triage.yml",
		"clarify.yml",
		"implement.yml",
		"orchestrate.yml",
		"orchestrate_clarify_respond.yml",
		"orchestrate_poll.yml",
		"plan.yml",
		"review_autofix.yml",
		"security-audit.yml",
		"validate.yml",
		"workflow-log-analysis.yml",
	):
		workflow_text = (REPO_ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
		for variable_name, default_value in price_defaults.items():
			expected_mapping = f"{variable_name}: ${{{{ vars.{variable_name} || '{default_value}' }}}}"
			assert expected_mapping in workflow_text, (workflow_name, expected_mapping)


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
		model_provider_broker_exec_sanitized sh -c '
			sudo -n -u nobody true
			for process_environment in /proc/[0-9]*/environ; do
				if grep -aEq "upstream-secret|repository-secret|repository-pat|github-token|state-secret|telegram-secret|telegram-chat" "${{process_environment}}" 2>/dev/null; then
					exit 97
				fi
			done
			env
		'
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


def test_broker_cleanup_stops_process_before_failed_acl_restore(tmp_path: Path) -> None:
	fake_bin = tmp_path / "bin"
	fake_bin.mkdir()
	fake_setfacl = fake_bin / "setfacl"
	fake_setfacl.write_text("#!/usr/bin/env sh\nexit 1\n", encoding="utf-8")
	fake_setfacl.chmod(0o755)
	broker_started = tmp_path / "broker.started"
	broker_process = subprocess.Popen([
		sys.executable,
		"-c",
		f'import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path({str(broker_started)!r}).touch(); time.sleep(60)',
	])
	for _ in range(100):
		if broker_started.exists():
			break
		time.sleep(0.01)
	assert broker_started.exists()
	pid_file = tmp_path / "broker.pid"
	ready_file = tmp_path / "ready.json"
	acl_file = tmp_path / "broker.acl"
	pid_file.write_text(f"{broker_process.pid}\n", encoding="utf-8")
	ready_file.write_text("{}\n", encoding="utf-8")
	acl_file.write_text("forced restore failure\n", encoding="utf-8")
	try:
		result = subprocess.run(
			[
				"bash", "-c",
				f'''set -uo pipefail
				source "{REPO_ROOT / 'scripts' / 'codex_helpers.sh'}"
				export PATH="{fake_bin}:$PATH"
				export MODEL_PROVIDER_BROKER_PID_FILE="{pid_file}"
				export MODEL_PROVIDER_BROKER_READY_FILE="{ready_file}"
				export MODEL_PROVIDER_BROKER_ACL_BACKUP="{acl_file}"
				model_provider_broker_stop
				printf 'cleanup_rc=%s\n' "$?"
				''',
			],
			cwd=REPO_ROOT,
			text=True,
			capture_output=True,
			check=True,
		)
		assert "cleanup_rc=1" in result.stdout
		broker_process.wait(timeout=3)
		assert broker_process.returncode is not None
		assert not pid_file.exists()
		assert not ready_file.exists()
	finally:
		if broker_process.poll() is None:
			broker_process.terminate()
			broker_process.wait(timeout=3)


def test_policy_opts_streamed_chat_requests_into_usage_reporting() -> None:
	module = _broker_module()
	policy = _broker_policy(module)
	streamed, _ = policy.normalize_request(
		"/api/v1/chat/completions",
		b'{"model":"openai/test-model","messages":[],"stream":true}',
	)
	assert json.loads(streamed)["stream_options"] == {"include_usage": True}
	merged, _ = policy.normalize_request(
		"/api/v1/chat/completions",
		b'{"model":"openai/test-model","messages":[],"stream":true,"stream_options":{"include_usage":false}}',
	)
	assert json.loads(merged)["stream_options"] == {"include_usage": True}
	unstreamed, _ = policy.normalize_request(
		"/api/v1/chat/completions",
		b'{"model":"openai/test-model","messages":[]}',
	)
	assert "stream_options" not in json.loads(unstreamed)
	try:
		policy.normalize_request(
			"/api/v1/chat/completions",
			b'{"model":"openai/test-model","messages":[],"stream":true,"stream_options":[]}',
		)
	except module.BrokerRequestError:
		pass
	else:
		raise AssertionError("broker accepted a non-object stream_options")


def test_usage_extraction_reads_final_chunk_or_keeps_reservation() -> None:
	module = _broker_module()
	extract = module._extract_output_token_usage
	chat_stream = (
		b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
		b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":7}}\n\n'
		b"data: [DONE]\n\n"
	)
	assert extract(chat_stream, "text/event-stream; charset=utf-8") == 7
	responses_stream = (
		b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\n'
		b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"output_tokens":11}}}\n\n'
	)
	assert extract(responses_stream, "text/event-stream") == 11
	assert extract(b'{"choices":[],"usage":{"completion_tokens":3}}', "application/json") == 3
	assert extract(b'{"usage":{"output_tokens":4}}', "application/json") == 4
	# Truncated leading SSE line is skipped; a later usage chunk still wins.
	assert extract(b'oices":[]}\n\ndata: {"usage":{"completion_tokens":2}}\n\n', "text/event-stream") == 2
	# No usage, an error event, or malformed usage keeps the reservation.
	assert extract(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n', "text/event-stream") is None
	assert extract(b'data: {"error":{"message":"overloaded"}}\n\n', "text/event-stream") is None
	assert extract(b'{"usage":{"completion_tokens":"7"}}', "application/json") is None
	assert extract(b'{"usage":{"completion_tokens":true}}', "application/json") is None
	assert extract(b"not json", "application/json") is None
	boundary_document = json.dumps(
		{"padding": "", "usage": {"completion_tokens": 6}},
		separators=(",", ":"),
	).encode("utf-8")
	boundary_document = boundary_document.replace(
		b'"padding":""',
		b'"padding":"' + (b"x" * (module.USAGE_SCAN_TAIL_BYTES - len(boundary_document))) + b'"',
	)
	assert len(boundary_document) == module.USAGE_SCAN_TAIL_BYTES
	assert extract(boundary_document, "application/json") == 6


def test_settle_request_trues_up_reservation_against_actual_usage() -> None:
	module = _broker_module()
	policy = _broker_policy(module)
	state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, policy)
	assert state.reserve_request(100)
	assert state.output_tokens_reserved == 100
	state.settle_request(100, 7)
	assert state.output_tokens_reserved == 7
	assert state.reserve_request(100)
	state.settle_request(100, None)
	assert state.output_tokens_reserved == 107, "unreadable usage must keep the full reservation"
	# The provider exceeding the ceiling is charged at what it actually generated.
	state.output_tokens_reserved = 0
	assert state.reserve_request(100)
	state.settle_request(100, 130)
	assert state.output_tokens_reserved == 130
	state.settle_request(100, 0)
	assert state.output_tokens_reserved == 30


def test_sequential_completed_requests_do_not_exhaust_output_budget() -> None:
	"""Regression for PR #4077 / runs 34692519987, 34700918528, 34702442346:
	the editor's agentic loop made 4 calls, each charged the full 16384
	ceiling against a 65536 total, and every later call got HTTP 429."""
	module = _broker_module()
	policy = _broker_policy(module)  # per-request ceiling 100, total 150
	state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, policy)
	for _ in range(5):
		assert state.reserve_request(100), "a completed request must release its unused ceiling"
		state.settle_request(100, 10)
	assert state.output_tokens_reserved == 50
	# Budget is still enforced against real usage once it is genuinely spent:
	# an in-flight request is charged its full ceiling until it settles, so
	# 110 spent + 100 ceiling exceeds the 150 total and the next call is refused.
	assert state.reserve_request(100)
	state.settle_request(100, 60)
	assert state.output_tokens_reserved == 110
	assert not state.reserve_request(100)


class _FakeUpstreamResponse:
	def __init__(self, status: int, body: bytes, content_type: str) -> None:
		self.status = status
		self.reason = "OK" if status < 400 else "ERR"
		self._body = body
		self._offset = 0
		self._content_type = content_type

	def getheaders(self) -> list[tuple[str, str]]:
		return [("Content-Type", self._content_type), ("Content-Length", str(len(self._body)))]

	def getheader(self, name: str, default: str = "") -> str:
		return self._content_type if name.lower() == "content-type" else default

	def read(self, size: int = -1) -> bytes:
		if self._offset >= len(self._body):
			return b""
		chunk = self._body[self._offset : self._offset + max(size, 1)]
		self._offset += len(chunk)
		return chunk


def test_brokered_stream_settles_budget_from_upstream_usage(tmp_path: Path) -> None:
	"""End-to-end through BrokerHandler with a fake HTTPS upstream: streamed
	turns that report usage are settled to their real cost, a confirmed upstream
	error settles to zero, and ambiguous failures keep their full reservation."""
	module = _broker_module()
	policy = _broker_policy(module)  # per-request ceiling 100, total 150
	state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, policy)
	scripted: list[tuple[int, bytes, str]] = []
	fail_connection_construction = False
	fail_upstream_request = False

	class _FakeConnection:
		# http.client.HTTPConnection subclasses carry these class attributes;
		# urllib.request.HTTPSHandler.__init__ reads them directly off
		# http.client.HTTPSConnection when no explicit debuglevel/context is
		# given, so a stand-in used to monkeypatch that slot must provide
		# them too.
		debuglevel = 0
		_http_vsn = 11

		def __init__(self, *_args: object, **_kwargs: object) -> None:
			if fail_connection_construction:
				raise OSError("connection setup failed")

		def request(self, *_args: object, **_kwargs: object) -> None:
			if fail_upstream_request:
				raise OSError("request failed before an upstream response")

		def getresponse(self) -> _FakeUpstreamResponse:
			return _FakeUpstreamResponse(*scripted.pop(0))

		def close(self) -> None:
			pass

	original_connection = module.http.client.HTTPSConnection
	original_usage_scan_tail_bytes = module.USAGE_SCAN_TAIL_BYTES
	module.http.client.HTTPSConnection = _FakeConnection  # type: ignore[misc]
	server = module.BrokerServer(("127.0.0.1", 0), state)
	server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
	server_thread.start()
	try:
		def post(body: dict[str, object]) -> int:
			request = urllib.request.Request(
				f"http://127.0.0.1:{server.server_port}/api/v1/chat/completions",
				data=json.dumps(body).encode("utf-8"),
				headers={"Authorization": "Bearer token", "Content-Type": "application/json"},
				method="POST",
			)
			try:
				with urllib.request.urlopen(request, timeout=5) as response:
					response.read()
					return response.status
			except urllib.error.HTTPError as error:
				error.read()
				return error.code

		usage_stream = (
			b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
			b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":10}}\n\n'
			b"data: [DONE]\n\n"
		)
		streamed_request: dict[str, object] = {"model": "openai/test-model", "messages": [], "stream": True}
		fail_connection_construction = True
		assert post(streamed_request) == 502
		assert state.output_tokens_reserved == 0, "connection setup failure must release its reservation"
		assert state.input_tokens_reserved == 0
		assert state.cost_usd_reserved == 0
		fail_connection_construction = False
		fail_upstream_request = True
		assert post(streamed_request) == 502
		assert state.output_tokens_reserved == 100, "a failure after forwarding starts must keep its output reservation"
		assert state.input_tokens_reserved > 0, "the provider may have billed the forwarded input"
		assert state.cost_usd_reserved > 0, "an ambiguous transport failure must remain charged"
		fail_upstream_request = False
		state.output_tokens_reserved = 0
		state.input_tokens_reserved = 0
		state.cost_usd_reserved = module.Decimal(0)
		for _ in range(5):
			scripted.append((200, usage_stream, "text/event-stream"))
			assert post(streamed_request) == 200
		assert state.output_tokens_reserved == 50, "five settled turns cost their real usage, not 5 x ceiling"

		scripted.append((429, b'{"error":{"message":"rate limited"}}', "application/json"))
		assert post(streamed_request) == 429
		assert state.output_tokens_reserved == 50, "an upstream error generated nothing and settles to zero"

		module.USAGE_SCAN_TAIL_BYTES = 128
		boundary_body = json.dumps(
			{"padding": "", "usage": {"completion_tokens": 10}},
			separators=(",", ":"),
		).encode("utf-8")
		boundary_body = boundary_body.replace(
			b'"padding":""',
			b'"padding":"' + (b"x" * (module.USAGE_SCAN_TAIL_BYTES - len(boundary_body))) + b'"',
		)
		state.output_tokens_reserved = 0
		scripted.append((200, boundary_body, "application/json"))
		assert post(streamed_request) == 200
		assert state.output_tokens_reserved == 10, "a complete body exactly at the tail cap must settle from usage"

		state.output_tokens_reserved = 0
		scripted.append((200, b" " + boundary_body, "application/json"))
		assert post(streamed_request) == 200
		assert state.output_tokens_reserved == 100, "a body larger than the retained tail must keep its reservation"

		state.output_tokens_reserved = 50
		scripted.append((200, b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n', "text/event-stream"))
		assert post(streamed_request) == 200
		assert state.output_tokens_reserved == 150, "a stream without usage keeps the full 100-token reservation"

		scripted.append((200, usage_stream, "text/event-stream"))
		assert post(streamed_request) == 429, "the real budget is still enforced once spent"
		assert len(scripted) == 1, "the rejected request must not reach the upstream"
	finally:
		server.shutdown()
		server.server_close()
		module.http.client.HTTPSConnection = original_connection  # type: ignore[misc]
		module.USAGE_SCAN_TAIL_BYTES = original_usage_scan_tail_bytes


def _broker_policy_with_budgets(module, **overrides):
	"""_broker_policy plus the #4090 input-token / cost budget fields."""
	fields = {
		"max_input_tokens": 20,
		"max_total_input_tokens": 30,
		"max_total_cost_usd": module.Decimal("0.25"),
	}
	fields.update(overrides)
	return module.BrokerPolicy(
		frozenset(("openai/test-model",)),
		100,
		150,
		tuple(module.Decimal(value) for value in ("1", "2", "0.1", "0.5")),
		**fields,
	)


def test_policy_defaults_keep_input_and_cost_budgets_non_binding() -> None:
	"""Regression for #4090: the new fields must not change behaviour for the
	existing positional constructor, and the derived per-request default admits
	any body the 16 MiB request cap already permits."""
	module = _broker_module()
	policy = _broker_policy(module)
	assert policy.max_input_tokens == module.DEFAULT_MAX_INPUT_TOKENS == 16_777_216
	assert policy.max_total_input_tokens == module.MAX_TOTAL_INPUT_TOKENS_CEILING
	assert policy.max_total_cost_usd is None
	assert policy.estimate_input_tokens(b"x" * module.MAX_REQUEST_BODY_BYTES) == module.DEFAULT_MAX_INPUT_TOKENS
	state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, policy)
	assert state.reserve_request(100, module.DEFAULT_MAX_INPUT_TOKENS)
	assert state.input_tokens_reserved == module.DEFAULT_MAX_INPUT_TOKENS


def test_policy_estimates_input_tokens_and_rejects_oversized_prompt() -> None:
	module = _broker_module()
	policy = _broker_policy_with_budgets(module, max_input_tokens=20)
	assert policy.estimate_input_tokens(b"") == 0
	assert policy.estimate_input_tokens(b"!" * 20) == 20, "dense tokenizers can emit one token per byte"
	try:
		policy.estimate_input_tokens(b"!" * 21)
	except module.BrokerRequestError as exc:
		assert "input-token limit" in str(exc)
	else:
		raise AssertionError("a 21-byte body must exceed the conservative 20-token ceiling")
	assert policy.estimate_cost_usd(1_000_000, 1_000_000) == module.Decimal("3.1"), "1 + 2 per million tokens plus 0.1 per request"
	assert policy.estimate_cost_usd(1_000_000, 1_000_000, 2) == module.Decimal("4.1"), "two images add 2 x the 0.5 ceiling"
	assert policy.estimate_cost_usd(0, 0) == module.Decimal("0.1"), "the per-request price is charged even for an empty request"
	assert module._count_image_inputs(b'{"input":[{"type":"input_image"},{"type":"image_url"}]}') == 2
	assert module._count_image_inputs(b'{"input":"\\\"type\\\":\\\"image_url\\\""}') == 0, "prompt text is not an image part"


def test_reserve_request_enforces_input_token_and_cost_budgets() -> None:
	module = _broker_module()
	policy = _broker_policy_with_budgets(module, max_total_input_tokens=30, max_total_cost_usd=module.Decimal("0.25"))
	state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, policy)
	# Two prompts of 15 estimated input tokens fit the 30-token budget; the third does not.
	assert state.reserve_request(1, 15)
	assert state.reserve_request(1, 15)
	assert not state.reserve_request(1, 15), "aggregate input tokens must be budgeted, not only output tokens"
	assert state.input_tokens_reserved == 30
	# Cost budget: each request reserves 0.1 request price + token cost at the ceilings.
	cost_state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, policy)
	assert cost_state.reserve_request(0, 0)
	assert cost_state.reserve_request(0, 0)
	assert cost_state.cost_usd_reserved == module.Decimal("0.2")
	assert not cost_state.reserve_request(0, 0), "the third request would exceed the 0.25 USD budget"
	assert not cost_state.reserve_request(0, 0, 1), "a priced image must be included in cost admission"
	assert cost_state.requests_started == 2
	try:
		cost_state.reserve_request(0, 0, -1)
	except ValueError as exc:
		assert "image_inputs must be non-negative" in str(exc)
	else:
		raise AssertionError("a negative image count must not credit the cost budget")


def test_settle_request_trues_up_input_tokens_and_cost() -> None:
	module = _broker_module()
	policy = _broker_policy_with_budgets(module, max_total_input_tokens=1_000, max_total_cost_usd=module.Decimal("10"))
	state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, policy)
	assert state.reserve_request(100, 20)
	assert state.cost_usd_reserved == policy.estimate_cost_usd(20, 100)
	state.settle_request(100, 7, 20, 5)
	assert state.output_tokens_reserved == 7
	assert state.input_tokens_reserved == 5
	assert state.cost_usd_reserved == policy.estimate_cost_usd(5, 7)
	# Unreadable input usage keeps the input reservation while output still settles.
	assert state.reserve_request(100, 20)
	state.settle_request(100, 3, 20, None)
	assert state.output_tokens_reserved == 10
	assert state.input_tokens_reserved == 25
	assert state.cost_usd_reserved == policy.estimate_cost_usd(5, 7) + policy.estimate_cost_usd(20, 3)
	# A settlement that changes nothing is a no-op, and the legacy two-argument
	# call keeps working for callers that only track output tokens.
	before = (state.output_tokens_reserved, state.input_tokens_reserved, state.cost_usd_reserved)
	state.settle_request(100, None, 20, None)
	assert (state.output_tokens_reserved, state.input_tokens_reserved, state.cost_usd_reserved) == before
	# A non-billable settlement (upstream error) releases the whole cost,
	# including the per-request price the token counts alone would keep.
	assert state.reserve_request(100, 20)
	state.settle_request(100, 0, 20, 0, billable=False)
	assert (state.output_tokens_reserved, state.input_tokens_reserved, state.cost_usd_reserved) == before
	assert state.reserve_request(100)
	state.settle_request(100, 1)
	assert state.output_tokens_reserved == 11
	assert state.input_tokens_reserved == 25
	image_state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, policy)
	assert image_state.reserve_request(100, 20, 1)
	image_state.settle_request(100, 7, 20, 5, reserved_image_inputs=1)
	assert image_state.cost_usd_reserved == policy.estimate_cost_usd(5, 7, 1)
	try:
		image_state.settle_request(100, 7, 20, 5, reserved_image_inputs=-1)
	except ValueError as exc:
		assert "image_inputs must be non-negative" in str(exc)
	else:
		raise AssertionError("settlement must reject a negative reserved image count")


def test_usage_extraction_reads_input_tokens_for_both_response_shapes() -> None:
	module = _broker_module()
	chat_stream = (
		b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
		b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3}}\n\n'
		b"data: [DONE]\n\n"
	)
	assert module._extract_token_usage(chat_stream, "text/event-stream") == (3, 12)
	responses_stream = (
		b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
		b'data: {"type":"response.completed","response":{"usage":{"input_tokens":40,"output_tokens":6}}}\n\n'
	)
	assert module._extract_token_usage(responses_stream, "text/event-stream; charset=utf-8") == (6, 40)
	assert module._extract_token_usage(b'{"usage":{"completion_tokens":2}}', "application/json") == (2, None), (
		"missing input usage must be reported as None so its reservation stays charged"
	)
	assert module._extract_token_usage(b"not json", "application/json") == (None, None)
	# The retained output-only helper still answers the same question.
	assert module._extract_output_token_usage(chat_stream, "text/event-stream") == 3


def test_brokered_requests_settle_input_and_cost_from_upstream_usage() -> None:
	"""End-to-end through BrokerHandler (#4090): input tokens and cost are
	reserved at admission, settled from `usage`, and a tight cost budget refuses
	the request that would exceed it before it reaches the upstream."""
	module = _broker_module()
	policy = _broker_policy_with_budgets(
		module,
		max_input_tokens=300,
		max_total_input_tokens=1_000,
		max_total_cost_usd=module.Decimal("0.25"),
	)
	state = module.BrokerState("https://example.test/api/v1", "secret", "token", 100, policy)
	scripted: list[tuple[int, bytes, str]] = []

	class _FakeConnection:
		# See the identical comment in test_brokered_stream_settles_budget_from_upstream_usage:
		# HTTPSHandler.__init__ reads these class attributes off whatever
		# object stands in for http.client.HTTPSConnection.
		debuglevel = 0
		_http_vsn = 11

		def __init__(self, *_args: object, **_kwargs: object) -> None:
			pass

		def request(self, *_args: object, **_kwargs: object) -> None:
			pass

		def getresponse(self) -> _FakeUpstreamResponse:
			return _FakeUpstreamResponse(*scripted.pop(0))

		def close(self) -> None:
			pass

	original_connection = module.http.client.HTTPSConnection
	module.http.client.HTTPSConnection = _FakeConnection  # type: ignore[misc]
	server = module.BrokerServer(("127.0.0.1", 0), state)
	server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
	server_thread.start()
	try:
		def post(body: dict[str, object]) -> tuple[int, str]:
			request = urllib.request.Request(
				f"http://127.0.0.1:{server.server_port}/api/v1/chat/completions",
				data=json.dumps(body).encode("utf-8"),
				headers={"Authorization": "Bearer token", "Content-Type": "application/json"},
				method="POST",
			)
			try:
				with urllib.request.urlopen(request, timeout=5) as response:
					return response.status, response.read().decode("utf-8")
			except urllib.error.HTTPError as error:
				return error.code, error.read().decode("utf-8")

		usage_stream = (
			b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
			b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":10}}\n\n'
			b"data: [DONE]\n\n"
		)
		streamed_request: dict[str, object] = {"model": "openai/test-model", "messages": [], "stream": True}
		scripted.append((200, usage_stream, "text/event-stream"))
		assert post(streamed_request)[0] == 200
		assert state.input_tokens_reserved == 7, "input reservation settles to the provider-reported prompt tokens"
		assert state.output_tokens_reserved == 10
		assert state.cost_usd_reserved == policy.estimate_cost_usd(7, 10)

		scripted.append((429, b'{"error":{"message":"rate limited"}}', "application/json"))
		assert post(streamed_request)[0] == 429
		assert state.input_tokens_reserved == 7, "an upstream error processed nothing and settles input to zero"
		assert state.cost_usd_reserved == policy.estimate_cost_usd(7, 10)

		scripted.append((200, usage_stream, "text/event-stream"))
		assert post(streamed_request)[0] == 200
		assert state.cost_usd_reserved == 2 * policy.estimate_cost_usd(7, 10)

		# Two settled requests cost 2 x (0.1 + tokens); a third worst-case
		# reservation (0.1 request price + 100 output x 2/M + input estimate)
		# would push past the 0.25 USD budget, so it is refused before upstream.
		scripted.append((200, usage_stream, "text/event-stream"))
		status, body = post(streamed_request)
		assert status == 429
		assert "cost budget exhausted" in body
		assert len(scripted) == 1, "the rejected request must not reach the upstream"

		image_request: dict[str, object] = {
			"model": "openai/test-model",
			"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]}],
		}
		status, body = post(image_request)
		assert status == 429
		assert "cost budget exhausted" in body
		assert len(scripted) == 1, "an image over the cost budget must not reach the upstream"

		oversized_request: dict[str, object] = {"model": "openai/test-model", "messages": [{"role": "user", "content": "x" * 500}]}
		status, body = post(oversized_request)
		assert status == 400
		assert "input-token limit" in body
		assert len(scripted) == 1
	finally:
		server.shutdown()
		server.server_close()
		module.http.client.HTTPSConnection = original_connection  # type: ignore[misc]


def _broker_cli_result(tmp_path: Path, *extra_args: str) -> tuple[int, str]:
	ready_file = tmp_path / "cli-ready.json"
	environment = {"PATH": os.environ["PATH"], "OPENROUTER_API_KEY": "upstream-secret"}
	process = subprocess.Popen(
		["python3", str(BROKER), "--ready-file", str(ready_file), "--allowed-model", "openai/test-model", *extra_args],
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
	)
	for _ in range(100):
		if ready_file.exists():
			process.terminate()
			process.wait(timeout=5)
			return 0, ""
		if process.poll() is not None:
			break
		time.sleep(0.02)
	if process.poll() is None:
		process.terminate()
	_stdout, stderr = process.communicate(timeout=5)
	return process.returncode, stderr.decode("utf-8", errors="replace")


def test_broker_cli_validates_budget_flags_and_starts_with_derived_defaults(tmp_path: Path) -> None:
	module = _broker_module()
	assert _broker_cli_result(tmp_path) == (0, ""), "defaults must derive without any budget flag"
	assert _broker_cli_result(tmp_path, "--max-total-cost-usd", "5", "--max-input-tokens", "1000", "--max-total-input-tokens", "5000") == (0, "")
	for arguments, fragment in (
		(("--max-input-tokens", "0"), "--max-input-tokens must be between 1 and"),
		(("--max-input-tokens", str(module.DEFAULT_MAX_INPUT_TOKENS + 1)), "--max-input-tokens must be between 1 and"),
		(("--max-input-tokens", "1000", "--max-total-input-tokens", "999"), "--max-total-input-tokens must cover one request"),
		(("--max-total-input-tokens", str(module.MAX_TOTAL_INPUT_TOKENS_CEILING + 1)), "--max-total-input-tokens must cover one request"),
		(("--max-total-cost-usd", "0"), "--max-total-cost-usd must be a positive decimal"),
		(("--max-total-cost-usd", "-1"), "--max-total-cost-usd must be a non-negative decimal"),
		(("--max-total-cost-usd", "NaN"), "--max-total-cost-usd must be a non-negative decimal"),
		(("--max-total-cost-usd", "abc"), "--max-total-cost-usd must be a non-negative decimal"),
	):
		returncode, stderr = _broker_cli_result(tmp_path, *arguments)
		assert returncode == 2, (arguments, stderr)
		assert fragment in stderr, (arguments, stderr)


def test_broker_launcher_and_workflows_wire_budget_policy() -> None:
	helpers_text = (REPO_ROOT / "scripts" / "codex_helpers.sh").read_text(encoding="utf-8")
	assert '--max-input-tokens "${MODEL_PROVIDER_BROKER_MAX_INPUT_TOKENS:-16777216}"' in helpers_text
	assert '--max-total-input-tokens "${MODEL_PROVIDER_BROKER_MAX_TOTAL_INPUT_TOKENS:-1677721600}"' in helpers_text
	assert 'broker_budget_args+=(--max-total-cost-usd "${MODEL_PROVIDER_BROKER_MAX_TOTAL_COST_USD}")' in helpers_text
	assert helpers_text.index('"${broker_budget_args[@]}"') < helpers_text.index('"${broker_policy_args[@]}" &')
	budget_mappings = {
		"MODEL_PROVIDER_BROKER_MAX_INPUT_TOKENS": "16777216",
		"MODEL_PROVIDER_BROKER_MAX_TOTAL_INPUT_TOKENS": "1677721600",
		"MODEL_PROVIDER_BROKER_MAX_TOTAL_COST_USD": "",
	}
	# clarify.yml and orchestrate_clarify_respond.yml run Codex through
	# scripts/clarify_isolated_run.sh (docker --network none, host-side
	# scripts/clarify_openrouter_broker.py) rather than the in-workflow
	# model_provider_broker_start/exec launcher, so they never wire these
	# MODEL_PROVIDER_BROKER_MAX_* budget env vars.
	for workflow_name in (
		"check_failure_triage.yml",
		"implement.yml",
		"orchestrate.yml",
		"orchestrate_poll.yml",
		"plan.yml",
		"review_autofix.yml",
		"security-audit.yml",
		"validate.yml",
		"workflow-log-analysis.yml",
	):
		workflow_text = (REPO_ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
		for variable_name, default_value in budget_mappings.items():
			expected_mapping = f"{variable_name}: ${{{{ vars.{variable_name} || '{default_value}' }}}}"
			assert expected_mapping in workflow_text, (workflow_name, expected_mapping)




def _read_rejections(path: Path) -> list[dict[str, object]]:
	return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_broker_records_policy_rejections_to_file_and_stderr(tmp_path: Path) -> None:
	"""Rejection diagnostics are sanitized and bounded in both output sinks.

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
			"--allowed-model",
			"openai/test-model",
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
			(f"/responses?session={ready['token']}&upstream=upstream-secret", str(ready["token"]), 404),
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
		for _ in range(120):
			request = urllib.request.Request(str(ready["base_url"]) + "/models", method="GET")
			try:
				urllib.request.urlopen(request, timeout=2)
			except urllib.error.HTTPError as exc:
				assert exc.code == 405
			else:
				raise AssertionError("broker accepted an unsupported method")
	finally:
		process.terminate()
		_, stderr = process.communicate(timeout=3)

	records = _read_rejections(rejections_file)
	assert len(records) == 100
	assert [(record["status"], record["path"], record["message"]) for record in records[:3]] == [
		(404, "<redacted>", "path not allowed"),
		(401, "/api/v1/responses", "invalid broker token"),
		(404, "/api/v1/responses", "path not allowed"),
	]
	assert all(isinstance(record["ts"], int) for record in records)
	# The record never carries the session token or the upstream key.
	assert "upstream-secret" not in rejections_file.read_text(encoding="utf-8")
	assert str(ready["token"]) not in rejections_file.read_text(encoding="utf-8")
	assert (rejections_file.stat().st_mode & 0o777) == 0o600
	stderr_text = stderr.decode("utf-8")
	assert "upstream-secret" not in stderr_text
	assert str(ready["token"]) not in stderr_text
	reject_lines = [line for line in stderr_text.splitlines() if line.startswith("MODEL_PROVIDER_BROKER_REJECT ")]
	assert len(reject_lines) == 100
	assert reject_lines[:3] == [
		'MODEL_PROVIDER_BROKER_REJECT status=404 path=<redacted> message="path not allowed"',
		'MODEL_PROVIDER_BROKER_REJECT status=401 path=/api/v1/responses message="invalid broker token"',
		'MODEL_PROVIDER_BROKER_REJECT status=404 path=/api/v1/responses message="path not allowed"',
	]


def test_isolated_writer_environment_omits_runner_commands_and_credentials() -> None:
	helper_text = (REPO_ROOT / "scripts" / "codex_helpers.sh").read_text(encoding="utf-8")
	prepare_block = helper_text.split("model_provider_broker_prepare_isolated_writer()", 1)[1].split(
		"model_provider_broker_exec_isolated_writer()", 1
	)[0]
	exec_block = helper_text.split("model_provider_broker_unprivileged_argv_into()", 1)[1].split(
		"model_provider_broker_exec_unprivileged()", 1
	)[0]
	assert "isolated writer requires a dedicated UID" in prepare_block
	assert "GITHUB_ENV" in prepare_block
	assert "GITHUB_WORKSPACE" in prepare_block
	assert "SUPPORT_SCRIPTS_DIR" in prepare_block
	for forbidden_name in (
		"BASH_ENV=",
		"GITHUB_ENV=",
		"GITHUB_OUTPUT=",
		"GH_TOKEN=",
		"GH_PAT=",
		"TG_BOT_SECRET=",
		"ORCHESTRATOR_STATE_AUTH_KEYRING=",
	):
		assert forbidden_name not in exec_block
	assert 'OPENROUTER_API_KEY="${MODEL_PROVIDER_BROKER_TOKEN}"' in exec_block
	assert "writer_isolation_verify_process_group_stopped" in helper_text
	assert 'MODEL_PROVIDER_BROKER_DEFER_ACCESS_RESTORE="true"' in helper_text
	finish_block = helper_text.split("model_provider_broker_finish_isolated_writer()", 1)[1].split(
		"model_provider_broker_exec_sanitized()", 1
	)[0]
	verify_index = finish_block.index("writer_isolation_verify_process_group_stopped")
	reclaim_index = finish_block.index('sudo -n chown -R "${runner_uid_gid}" "${isolated_workspace}"')
	stop_index = finish_block.index("model_provider_broker_stop")
	assert verify_index < reclaim_index < stop_index
	assert 'setfacl -R -m "u:${isolation_user}:---" "${isolated_workspace}"' in finish_block
	assert 'setfacl -m "d:u:${isolation_user}:---"' in finish_block


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


def test_broker_ignores_unusable_rejections_file(tmp_path: Path) -> None:
	ready_file = tmp_path / "ready.json"
	invalid_parent_path = tmp_path / "not-a-directory"
	invalid_parent_path.write_text("blocked", encoding="utf-8")
	environment = {"PATH": os.environ["PATH"], "OPENROUTER_API_KEY": "upstream-secret"}
	process = subprocess.Popen(
		[
			"python3",
			str(BROKER),
			"--ready-file",
			str(ready_file),
			"--rejections-file",
			str(invalid_parent_path / "rejections.jsonl"),
			"--allowed-model",
			"openai/test-model",
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
	stderr_text = stderr.decode("utf-8")
	assert "rejection file unavailable; continuing without file recording" in stderr_text
	assert "MODEL_PROVIDER_BROKER_REJECT status=404" in stderr_text


def test_broker_start_wires_rejections_file_and_count_helpers(tmp_path: Path) -> None:
	"""codex_helpers.sh passes --rejections-file, exports the path, and the
	count helper only counts deterministic 4xx policy rejections."""
	command = f'''
		set -euo pipefail
		source "{REPO_ROOT / 'scripts' / 'codex_helpers.sh'}"
		export RUNTIME_DIR="{tmp_path}"
		export OPENROUTER_API_KEY="upstream-secret"
		export MODEL_PROVIDER_BROKER_ALLOWED_MODELS="openai/test-model"
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
