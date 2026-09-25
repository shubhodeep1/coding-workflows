#!/usr/bin/env python3
"""Budget accounting in the clarify / review-editor relay (#4090 controls)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import tempfile
import threading
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RELAY = REPO_ROOT / "scripts" / "clarify_openrouter_broker.py"
MODEL = "openai/gpt-6-sol"
BUDGET_VARS = (
	"MAX_REQUESTS", "MAX_OUTPUT_TOKENS", "MAX_TOTAL_OUTPUT_TOKENS", "MAX_INPUT_TOKENS",
	"MAX_TOTAL_INPUT_TOKENS", "MAX_TOTAL_COST_USD", "MAX_PROMPT_PRICE",
	"MAX_COMPLETION_PRICE", "MAX_REQUEST_PRICE", "MAX_IMAGE_PRICE",
)


def _relay_module():
	spec = importlib.util.spec_from_file_location("clarify_relay_budget_test", RELAY)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


@pytest.fixture
def clean_budget_env(monkeypatch):
	for name in BUDGET_VARS:
		monkeypatch.delenv(f"MODEL_PROVIDER_BROKER_{name}", raising=False)
	return monkeypatch


def test_budget_defaults_match_model_provider_broker(clean_budget_env) -> None:
	relay = _relay_module()
	state = relay.build_budget_state(MODEL)
	budget = state.module
	policy = state.policy
	assert state.max_requests == 100
	assert policy.allowed_models == frozenset((MODEL,))
	assert policy.max_output_tokens == 16384
	assert policy.max_total_output_tokens == 1638400
	assert policy.max_input_tokens == budget.DEFAULT_MAX_INPUT_TOKENS
	assert policy.max_total_input_tokens == min(100 * budget.DEFAULT_MAX_INPUT_TOKENS, budget.MAX_TOTAL_INPUT_TOKENS_CEILING)
	assert policy.max_prices == (Decimal("10"), Decimal("30"), Decimal("0.10"), Decimal("1"))
	expected_cost = policy.estimate_cost_usd(
		policy.max_total_input_tokens, 1638400, 100 * budget.MAX_IMAGE_INPUTS_PER_REQUEST,
	) + 99 * Decimal("0.10")
	assert policy.max_total_cost_usd == expected_cost


def test_budget_env_overrides_and_invalid_values_fail_closed(clean_budget_env) -> None:
	relay = _relay_module()
	clean_budget_env.setenv("MODEL_PROVIDER_BROKER_MAX_REQUESTS", "3")
	clean_budget_env.setenv("MODEL_PROVIDER_BROKER_MAX_TOTAL_COST_USD", "2.5")
	clean_budget_env.setenv("MODEL_PROVIDER_BROKER_MAX_PROMPT_PRICE", "1")
	state = relay.build_budget_state(MODEL)
	assert state.max_requests == 3
	assert state.policy.max_total_cost_usd == Decimal("2.5")
	assert state.policy.max_prices[0] == Decimal("1")
	for name, value in (
		("MAX_REQUESTS", "0"),
		("MAX_REQUESTS", "101"),
		("MAX_REQUESTS", "ten"),
		("MAX_OUTPUT_TOKENS", "-1"),
		("MAX_TOTAL_OUTPUT_TOKENS", "10"),
		("MAX_TOTAL_COST_USD", "0"),
		("MAX_TOTAL_COST_USD", "NaN"),
		("MAX_IMAGE_PRICE", "-1"),
	):
		with mock.patch.dict(os.environ, {f"MODEL_PROVIDER_BROKER_{name}": value}):
			with pytest.raises(SystemExit) as excinfo:
				relay.build_budget_state(MODEL)
			assert excinfo.value.code == 2, (name, value)


def test_relay_without_budget_module_refuses_to_start(tmp_path: Path, clean_budget_env) -> None:
	lonely = tmp_path / "clarify_openrouter_broker.py"
	lonely.write_bytes(RELAY.read_bytes())
	spec = importlib.util.spec_from_file_location("clarify_relay_lonely", lonely)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	with pytest.raises(SystemExit) as excinfo:
		module.build_budget_state(MODEL)
	assert excinfo.value.code == 2


def _serve(relay, socket_path: str, state):
	server = relay.UnixHTTPServer(socket_path, relay.Relay)
	server.mode = "review-broker"
	server.model = MODEL
	server.api_key = "test-only-key"
	server.budget = state
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	return server, thread


def _post(relay, socket_path: str, document: dict) -> int:
	conn = relay.UnixHTTPConnection(socket_path)
	try:
		conn.request("POST", "/api/v1/chat/completions", json.dumps(document), {"Content-Type": "application/json"})
		response = conn.getresponse()
		response.read()
		return response.status
	finally:
		conn.close()


def _upstream(seen: list, status: int, payload: bytes):
	class Upstream:
		def __init__(self, _host, **_kwargs):
			pass

		def request(self, method, path, body, headers):
			seen.append(json.loads(body))

		def getresponse(self):
			chunks = [payload]

			class Response:
				def getheader(self, _name, default):
					return "application/json"

				def read1(self, _size):
					return chunks.pop() if chunks else b""

			Response.status = status
			return Response()

		def close(self):
			pass

	return Upstream


def test_relay_forwards_normalized_body_and_settles_usage(clean_budget_env) -> None:
	relay = _relay_module()
	clean_budget_env.setenv("MODEL_PROVIDER_BROKER_MAX_REQUESTS", "2")
	state = relay.build_budget_state(MODEL)
	seen: list = []
	usage = json.dumps({"usage": {"prompt_tokens": 7, "completion_tokens": 5}}).encode()
	with tempfile.TemporaryDirectory() as td, mock.patch.object(
		relay.http.client, "HTTPSConnection", _upstream(seen, 200, usage),
	):
		socket_path = str(Path(td) / "broker.sock")
		server, thread = _serve(relay, socket_path, state)
		try:
			assert _post(relay, socket_path, {"model": MODEL, "max_tokens": 99999999}) == 200
			assert state.output_tokens_reserved == 5
			assert state.input_tokens_reserved == 7
			assert _post(relay, socket_path, {"model": MODEL}) == 200
			# The request cap is spent: refused before anything is forwarded.
			assert _post(relay, socket_path, {"model": MODEL}) == 429
			# Policy rejections from model_provider_broker.py still apply.
			assert _post(relay, socket_path, {"model": MODEL, "models": [MODEL, "x/y"]}) == 400
			assert _post(relay, socket_path, {"model": "other/model"}) == 400
		finally:
			server.shutdown()
			server.server_close()
			thread.join(timeout=2)
	assert len(seen) == 2
	assert seen[0]["max_completion_tokens"] == 16384
	assert "max_tokens" not in seen[0]
	assert set(seen[0]["provider"]["max_price"]) == {"prompt", "completion", "request", "image"}
	assert state.requests_started == 2


def test_relay_upstream_error_releases_cost_reservation(clean_budget_env) -> None:
	relay = _relay_module()
	state = relay.build_budget_state(MODEL)
	seen: list = []
	with tempfile.TemporaryDirectory() as td, mock.patch.object(
		relay.http.client, "HTTPSConnection", _upstream(seen, 500, b'{"error":"x"}'),
	):
		socket_path = str(Path(td) / "broker.sock")
		server, thread = _serve(relay, socket_path, state)
		try:
			assert _post(relay, socket_path, {"model": MODEL}) == 500
		finally:
			server.shutdown()
			server.server_close()
			thread.join(timeout=2)
	assert len(seen) == 1
	assert state.output_tokens_reserved == 0
	assert state.input_tokens_reserved == 0
	assert state.cost_usd_reserved == 0


def test_relay_launchers_pass_budget_env_through_env_i() -> None:
	for relative_path in ("scripts/clarify_isolated_run.sh", "scripts/review_untrusted_sandbox.sh"):
		text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
		assert re.search(
			r'env -i [^\n]*"\$\{broker_budget_env\[@\]\}" \\\n\s*python3 [^\n]*clarify_openrouter_broker\.py"? (review-)?broker ',
			text,
		), relative_path
		for name in BUDGET_VARS:
			assert name in text, (relative_path, name)
	for workflow_name in ("clarify.yml", "orchestrate_clarify_respond.yml"):
		text = (REPO_ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8")
		assert "clarify_isolated_run.sh clarify_openrouter_broker.py model_provider_broker.py" in text
		assert "MODEL_PROVIDER_BROKER_MAX_TOTAL_COST_USD: ${{ vars.MODEL_PROVIDER_BROKER_MAX_TOTAL_COST_USD || '' }}" in text
	stage = (REPO_ROOT / "scripts/stage_workflow_support.sh").read_text(encoding="utf-8")
	required = stage.split('REQUIRED_BOOTSTRAP_SCRIPTS="', 1)[1].split('"', 1)[0].split()
	assert {"clarify_openrouter_broker.py", "model_provider_broker.py"} <= set(required)
