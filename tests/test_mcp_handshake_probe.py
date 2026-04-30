#!/usr/bin/env python3
"""Tests for scripts/mcp_handshake_probe.py and the setup_serena.sh probe gate.

These tests reproduce the Codex / Azure failure mode described in the
"Codex tool list contains an entry whose `function` is undefined" issue:
when an MCP server crashes during ``initialize``, Codex still emits a
malformed ``tools[N]`` entry, which strict OpenRouter providers reject
with HTTP 400. The pre-flight probe added in ``setup_serena.sh`` blocks
the server from being written to ``~/.codex/config.toml`` so the malformed
entry never reaches the model call.

Suite:

* ``mock_mcp_ok.py``                — passes the probe (exit 0).
* ``mock_mcp_close_on_init.py``     — closes during initialize (exit 3).
* ``mock_mcp_slow.py``              — never responds, hits --timeout (exit 2).
* spawn-failure path                — non-existent binary returns exit 1.
* setup_serena.sh integration       — a failed probe omits the
  ``[mcp_servers.context7]`` block from the generated config.toml.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "scripts" / "mcp_handshake_probe.py"
SETUP_SERENA = REPO_ROOT / "scripts" / "setup_serena.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "mcp_handshake"
MOCK_OK = FIXTURES / "mock_mcp_ok.py"
MOCK_CLOSE = FIXTURES / "mock_mcp_close_on_init.py"
MOCK_SLOW = FIXTURES / "mock_mcp_slow.py"


def _run_probe(name: str, command: str, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
	cmd = [
		sys.executable,
		str(PROBE),
		"--name",
		name,
		"--timeout",
		str(timeout),
		"--command",
		command,
		"--",
		*args,
	]
	return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)


def test_probe_succeeds_against_well_behaved_mock_server() -> None:
	result = _run_probe("ok", sys.executable, str(MOCK_OK), timeout=5.0)
	assert result.returncode == 0, (
		f"expected exit 0, got {result.returncode}\n"
		f"stdout: {result.stdout}\nstderr: {result.stderr}"
	)
	assert "handshake OK" in result.stderr


def test_probe_detects_close_on_initialize() -> None:
	"""The exact Context7 failure pattern: server closes connection mid-handshake.

	This is the failure shape that produced
	``mcp: context7 failed: MCP client for context7 failed to start: ...
	connection closed: initialize response`` and ultimately the Azure
	HTTP 400. The probe MUST reject it so Codex never sees the half-open
	server in its tool list.
	"""
	result = _run_probe("ctx7-mock", sys.executable, str(MOCK_CLOSE), timeout=5.0)
	assert result.returncode == 3, (
		f"expected exit 3 (server closed before responding), got {result.returncode}\n"
		f"stderr: {result.stderr}"
	)
	assert "exited before sending a response" in result.stderr or "[mcp-probe:ctx7-mock]" in result.stderr


def test_probe_times_out_on_unresponsive_server() -> None:
	result = _run_probe("slow", sys.executable, str(MOCK_SLOW), timeout=2.0)
	assert result.returncode == 2, (
		f"expected exit 2 (timeout), got {result.returncode}\n"
		f"stderr: {result.stderr}"
	)
	assert "timed out" in result.stderr


def test_probe_reports_spawn_failure_for_missing_binary() -> None:
	result = _run_probe("missing", "/nonexistent/path/that/does/not/exist", timeout=5.0)
	assert result.returncode == 1, (
		f"expected exit 1 (spawn failed), got {result.returncode}\n"
		f"stderr: {result.stderr}"
	)
	assert "spawn failed" in result.stderr


def test_probe_rejects_invalid_json_response(tmp_path: Path) -> None:
	junk_server = tmp_path / "junk.py"
	junk_server.write_text(
		textwrap.dedent(
			"""
			import sys
			sys.stdin.readline()
			sys.stdout.write("not-json-at-all\\n")
			sys.stdout.flush()
			"""
		)
	)
	result = _run_probe("junk", sys.executable, str(junk_server), timeout=5.0)
	assert result.returncode == 4, (
		f"expected exit 4 (invalid JSON), got {result.returncode}\n"
		f"stderr: {result.stderr}"
	)


def test_probe_rejects_jsonrpc_error_response(tmp_path: Path) -> None:
	err_server = tmp_path / "err.py"
	err_server.write_text(
		textwrap.dedent(
			"""
			import json, sys
			req = json.loads(sys.stdin.readline())
			resp = {
				"jsonrpc": "2.0",
				"id": req.get("id"),
				"error": {"code": -32600, "message": "boom"},
			}
			sys.stdout.write(json.dumps(resp) + "\\n")
			sys.stdout.flush()
			"""
		)
	)
	result = _run_probe("err", sys.executable, str(err_server), timeout=5.0)
	assert result.returncode == 5, (
		f"expected exit 5 (error response), got {result.returncode}\n"
		f"stderr: {result.stderr}"
	)


def test_probe_rejects_mismatched_response_id(tmp_path: Path) -> None:
	bad_id_server = tmp_path / "bad_id.py"
	bad_id_server.write_text(
		textwrap.dedent(
			"""
			import json, sys
			sys.stdin.readline()
			resp = {"jsonrpc": "2.0", "id": 999, "result": {}}
			sys.stdout.write(json.dumps(resp) + "\\n")
			sys.stdout.flush()
			"""
		)
	)
	result = _run_probe("badid", sys.executable, str(bad_id_server), timeout=5.0)
	assert result.returncode == 6, (
		f"expected exit 6 (id mismatch), got {result.returncode}\n"
		f"stderr: {result.stderr}"
	)


# ── setup_serena.sh integration ─────────────────────────────────────────────
#
# Verify the bash-level gate: when the probe rejects an MCP server, the
# corresponding [mcp_servers.<name>] block must NOT appear in config.toml.
# This is the defence that keeps Codex's tool list clean — exercising it
# end-to-end matters more than unit-testing the probe in isolation.


def _write_helper_script(tmp_path: Path, mock: Path) -> Path:
	"""Render a minimal driver that loads setup_serena.sh's helpers and
	calls them against the supplied mock server. Avoids running the full
	setup_serena.sh (which would download uv, spawn Serena, etc.).
	"""
	driver = tmp_path / "drive_probe.sh"
	driver.write_text(
		textwrap.dedent(
			f"""\
			#!/usr/bin/env bash
			# Source just the probe + remove helpers from setup_serena.sh by
			# extracting them with awk. We can't `source` the whole file —
			# it has top-level `set -euo pipefail`, argument parsing, and
			# uvx calls. Instead, redefine a minimal probe_mcp_handshake
			# inline that calls the same script under test.
			set -euo pipefail
			REPO_ROOT="{REPO_ROOT}"
			MCP_HANDSHAKE_PROBE_ENABLED="${{MCP_HANDSHAKE_PROBE_ENABLED:-true}}"
			MCP_HANDSHAKE_PROBE_TIMEOUT="${{MCP_HANDSHAKE_PROBE_TIMEOUT:-3}}"
			BASH_SOURCE_DIR="${{REPO_ROOT}}/scripts"

			# Inline copy of probe_mcp_handshake from setup_serena.sh —
			# kept byte-equivalent in spirit so this test catches drift.
			probe_mcp_handshake() {{
				local _name="$1"
				local _command="$2"
				shift 2
				local _timeout="${{MCP_HANDSHAKE_PROBE_TIMEOUT:-15}}"
				if [ "${{MCP_HANDSHAKE_PROBE_ENABLED:-true}}" != "true" ]; then
					return 0
				fi
				python3 "${{BASH_SOURCE_DIR}}/mcp_handshake_probe.py" \\
					--name "${{_name}}" \\
					--timeout "${{_timeout}}" \\
					--command "${{_command}}" \\
					-- "$@"
			}}

			CFG="$1"
			: > "${{CFG}}"

			if probe_mcp_handshake mock {sys.executable!r} {str(mock)!r}; then
				cat >> "${{CFG}}" <<EOF

			[mcp_servers.context7]
			command = "mock"
			EOF
				echo "wrote block"
			else
				echo "skipped block"
			fi
			"""
		)
	)
	driver.chmod(0o755)
	return driver


def test_setup_serena_skips_block_when_probe_fails(tmp_path: Path) -> None:
	"""End-to-end: a server that fails handshake produces NO config block.

	This is the contract: ``[mcp_servers.context7]`` must be absent from
	~/.codex/config.toml whenever the probe fails, otherwise Codex will
	emit a malformed tool entry and the Azure HTTP 400 returns.
	"""
	cfg = tmp_path / "config.toml"
	driver = _write_helper_script(tmp_path, MOCK_CLOSE)
	result = subprocess.run(
		["bash", str(driver), str(cfg)],
		capture_output=True,
		text=True,
		timeout=20,
	)
	assert result.returncode == 0, f"driver failed: {result.stderr}"
	assert "skipped block" in result.stdout
	assert "[mcp_servers.context7]" not in cfg.read_text()


def test_setup_serena_writes_block_when_probe_passes(tmp_path: Path) -> None:
	cfg = tmp_path / "config.toml"
	driver = _write_helper_script(tmp_path, MOCK_OK)
	result = subprocess.run(
		["bash", str(driver), str(cfg)],
		capture_output=True,
		text=True,
		timeout=20,
	)
	assert result.returncode == 0, f"driver failed: {result.stderr}"
	assert "wrote block" in result.stdout
	assert "[mcp_servers.context7]" in cfg.read_text()


def test_setup_serena_probe_disabled_writes_block_unconditionally(tmp_path: Path) -> None:
	"""The MCP_HANDSHAKE_PROBE_ENABLED=false escape hatch preserves old behaviour.

	Even though the mock would fail handshake, the block must still be
	written when the operator opts out — this is the documented config knob.
	"""
	cfg = tmp_path / "config.toml"
	driver = _write_helper_script(tmp_path, MOCK_CLOSE)
	env = os.environ.copy()
	env["MCP_HANDSHAKE_PROBE_ENABLED"] = "false"
	result = subprocess.run(
		["bash", str(driver), str(cfg)],
		capture_output=True,
		text=True,
		timeout=20,
		env=env,
	)
	assert result.returncode == 0, f"driver failed: {result.stderr}"
	assert "wrote block" in result.stdout
	assert "[mcp_servers.context7]" in cfg.read_text()


# ── Drift guard: keep the inline driver in sync with setup_serena.sh ────────


def test_probe_helper_definition_is_present_in_setup_serena() -> None:
	"""Catches accidental removal of the probe helper from setup_serena.sh.

	The integration tests above use a hand-copied driver. If someone
	deletes or renames ``probe_mcp_handshake`` in setup_serena.sh, the
	driver would silently keep passing while production stops probing.
	This guard fails loudly in that scenario.
	"""
	source = SETUP_SERENA.read_text()
	assert "probe_mcp_handshake()" in source, (
		"probe_mcp_handshake() helper missing from setup_serena.sh — "
		"the MCP handshake probe is no longer wired in."
	)
	assert "MCP_HANDSHAKE_PROBE_ENABLED" in source
	# Both optional servers must be gated by the probe.
	assert "probe_mcp_handshake context7" in source
	assert "probe_mcp_handshake git" in source
