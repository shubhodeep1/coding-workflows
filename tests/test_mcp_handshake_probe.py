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

Self-contained: runs as ``python3 tests/test_mcp_handshake_probe.py`` (no
pytest dependency) so it slots into the project's existing CI harness in
``.github/workflows/ci.yml`` alongside the other ``python3 tests/X.py``
invocations.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import traceback
from pathlib import Path


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
	assert "closed stdout before sending" in result.stderr or "[mcp-probe:ctx7-mock]" in result.stderr


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


def test_probe_rejects_invalid_json_response() -> None:
	with tempfile.TemporaryDirectory() as td:
		junk_server = Path(td) / "junk.py"
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


def test_probe_rejects_jsonrpc_error_response() -> None:
	with tempfile.TemporaryDirectory() as td:
		err_server = Path(td) / "err.py"
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


def test_probe_rejects_response_with_null_error_field() -> None:
	"""Per JSON-RPC spec a response has `result` XOR `error` — but a buggy
	server might still emit `"error": null`. The probe must reject that
	(via key-presence check, not truthiness) so a malformed tool entry can
	never slip through."""
	with tempfile.TemporaryDirectory() as td:
		nullerr_server = Path(td) / "nullerr.py"
		nullerr_server.write_text(
			textwrap.dedent(
				"""
				import json, sys
				req = json.loads(sys.stdin.readline())
				resp = {
					"jsonrpc": "2.0",
					"id": req.get("id"),
					"result": {"protocolVersion": "2024-11-05"},
					"error": None,
				}
				sys.stdout.write(json.dumps(resp) + "\\n")
				sys.stdout.flush()
				"""
			)
		)
		result = _run_probe("nullerr", sys.executable, str(nullerr_server), timeout=5.0)
	assert result.returncode == 5, (
		f"expected exit 5 (error key present even though null), got {result.returncode}\n"
		f"stderr: {result.stderr}"
	)


def test_probe_rejects_oversized_response_without_newline() -> None:
	"""A misbehaving server that streams bytes without a newline must not be
	able to grow the probe's buffer indefinitely. The probe enforces a
	configurable cap (``_MAX_RESPONSE_BYTES``, default 1 MiB, override via
	``MCP_PROBE_MAX_RESPONSE_BYTES``) and returns exit 7 ("response too
	large") when exceeded so the optional MCP block is still skipped
	without the probe itself blowing up on memory.

	The test sets the cap to 4 KiB and has the mock server send a single
	8 KiB write — no flooding, no time loops, no scheduling assumptions.
	The cap fires on the first read regardless of runner speed."""
	with tempfile.TemporaryDirectory() as td:
		flood_server = Path(td) / "flood.py"
		flood_server.write_text(
			textwrap.dedent(
				"""
				import sys, time
				sys.stdin.readline()
				# One write of 8 KiB — already past the test's 4 KiB cap.
				try:
					sys.stdout.buffer.write(b"x" * 8192)
					sys.stdout.buffer.flush()
				except BrokenPipeError:
					pass
				# Idle long enough for the probe's cap-detection to win the
				# race against the --timeout deadline. The probe should
				# raise on the next read after the 8 KiB chunk arrives.
				time.sleep(2)
				"""
			)
		)
		env = os.environ.copy()
		env["MCP_PROBE_MAX_RESPONSE_BYTES"] = "4096"
		cmd = [
			sys.executable,
			str(PROBE),
			"--name",
			"flood",
			"--timeout",
			"5",
			"--command",
			sys.executable,
			"--",
			str(flood_server),
		]
		result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
	assert result.returncode == 7, (
		f"expected exit 7 (response too large), got {result.returncode}\n"
		f"stderr: {result.stderr}"
	)
	assert "exceeded" in result.stderr


def test_probe_distinguishes_eof_from_timeout_when_child_still_alive() -> None:
	"""A server that closes stdout but stays alive must map to exit 3 (EOF),
	not exit 2 (timeout). The previous implementation used ``proc.poll()``
	to discriminate, which is racy: between the read returning None and the
	caller checking ``poll()``, a still-alive child appears as a timeout.
	The fix raises ``_TimeoutError`` only on actual deadline expiry inside
	``_read_line_with_timeout`` and returns ``None`` only on EOF, so this
	branch is now exact regardless of child-process lifecycle."""
	with tempfile.TemporaryDirectory() as td:
		closer = Path(td) / "close_stdout_alive.py"
		closer.write_text(
			textwrap.dedent(
				"""
				import os, sys, time
				sys.stdin.readline()
				# Close FD 1 directly so the probe's pipe-read returns EOF.
				# `sys.stdout.close()` on its own can leave the underlying
				# file descriptor open in some Python startup configurations,
				# so close the FD explicitly.
				try:
					sys.stdout.flush()
				except Exception:
					pass
				os.close(1)
				# Stay alive past the probe's deadline so proc.poll() would
				# return None at the moment the probe sees EOF on stdout.
				time.sleep(30)
				"""
			)
		)
		# Probe timeout is short (3s); the child sleeps 30s so it is still
		# alive when EOF arrives. The exit code must be 3 (EOF), not 2.
		result = _run_probe("eof-alive", sys.executable, str(closer), timeout=3.0)
	assert result.returncode == 3, (
		f"expected exit 3 (EOF), got {result.returncode}\n"
		f"stderr: {result.stderr}"
	)
	assert "closed stdout" in result.stderr


def test_probe_returns_64_on_invalid_timeout() -> None:
	"""Argparse parse errors and ``--timeout <= 0`` must exit 64, not 2.
	Code 2 is reserved for handshake timeout — overlap would make exit-
	code-driven log analysis ambiguous."""
	# Negative --timeout: caught by main()'s explicit check.
	cmd_neg = [
		sys.executable,
		str(PROBE),
		"--name",
		"x",
		"--command",
		"/bin/true",
		"--timeout",
		"-1",
	]
	r1 = subprocess.run(cmd_neg, capture_output=True, text=True, timeout=10)
	assert r1.returncode == 64, (
		f"expected 64 for non-positive timeout, got {r1.returncode}\n"
		f"stderr: {r1.stderr}"
	)
	# Missing required arg: caught by argparse via _ProbeArgParser.
	cmd_missing = [sys.executable, str(PROBE), "--name", "x"]
	r2 = subprocess.run(cmd_missing, capture_output=True, text=True, timeout=10)
	assert r2.returncode == 64, (
		f"expected 64 for missing --command, got {r2.returncode}\n"
		f"stderr: {r2.stderr}"
	)
	# Non-numeric --timeout: caught by argparse type=float coercion.
	cmd_nan = [
		sys.executable,
		str(PROBE),
		"--name",
		"x",
		"--command",
		"/bin/true",
		"--timeout",
		"abc",
	]
	r3 = subprocess.run(cmd_nan, capture_output=True, text=True, timeout=10)
	assert r3.returncode == 64, (
		f"expected 64 for non-numeric timeout, got {r3.returncode}\n"
		f"stderr: {r3.stderr}"
	)


def test_probe_rejects_mismatched_response_id() -> None:
	with tempfile.TemporaryDirectory() as td:
		bad_id_server = Path(td) / "bad_id.py"
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
	"""Render a minimal driver that exercises the same probe wiring as
	``scripts/setup_serena.sh`` — without sourcing the whole script.
	``setup_serena.sh`` has top-level ``set -euo pipefail``, argument parsing,
	and uvx calls that would side-effect the test environment, so we redefine
	a minimal ``probe_mcp_handshake`` inline that calls the same
	``mcp_handshake_probe.py`` script the production code calls. Drift is
	caught by ``test_probe_helper_definition_is_present_in_setup_serena``.
	"""
	driver = tmp_path / "drive_probe.sh"
	driver.write_text(
		textwrap.dedent(
			f"""\
			#!/usr/bin/env bash
			# Don't `source` the whole setup_serena.sh file here — it has
			# top-level `set -euo pipefail`, argument parsing, and uvx
			# calls. Instead, define a minimal inline probe_mcp_handshake
			# helper that invokes the same probe script under test.
			set -euo pipefail
			REPO_ROOT="{REPO_ROOT}"
			MCP_HANDSHAKE_PROBE_ENABLED="${{MCP_HANDSHAKE_PROBE_ENABLED:-true}}"
			MCP_HANDSHAKE_PROBE_TIMEOUT="${{MCP_HANDSHAKE_PROBE_TIMEOUT:-3}}"
			BASH_SOURCE_DIR="${{REPO_ROOT}}/scripts"

			# Inline copy of probe_mcp_handshake from setup_serena.sh —
			# kept in sync via the drift guard test below.
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


def test_setup_serena_skips_block_when_probe_fails() -> None:
	"""End-to-end: a server that fails handshake produces NO config block.

	This is the contract: ``[mcp_servers.context7]`` must be absent from
	~/.codex/config.toml whenever the probe fails, otherwise Codex will
	emit a malformed tool entry and the Azure HTTP 400 returns.
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp_path = Path(td)
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


def test_setup_serena_writes_block_when_probe_passes() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp_path = Path(td)
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


def test_setup_serena_probe_disabled_writes_block_unconditionally() -> None:
	"""The MCP_HANDSHAKE_PROBE_ENABLED=false escape hatch preserves old behaviour.

	Even though the mock would fail handshake, the block must still be
	written when the operator opts out — this is the documented config knob.
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp_path = Path(td)
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
	import re

	source = SETUP_SERENA.read_text()
	assert "probe_mcp_handshake()" in source, (
		"probe_mcp_handshake() helper missing from setup_serena.sh — "
		"the MCP handshake probe is no longer wired in."
	)
	assert "MCP_HANDSHAKE_PROBE_ENABLED" in source
	# Both optional servers must be gated by the probe.
	assert "probe_mcp_handshake context7" in source
	assert "probe_mcp_handshake git" in source
	# Fail-closed contract: missing python3 / probe script must trigger
	# `return 1` from the helper, not silent `return 0`. Match against the
	# structural shape of the fail-closed branches so this guard is robust
	# to wording changes in the human-facing warning text.
	python_check_pattern = re.compile(
		r"if\s+!\s+command\s+-v\s+python3.*?return\s+1",
		re.DOTALL,
	)
	probe_script_check_pattern = re.compile(
		r"if\s+\[\s+!\s+-f\s+\"\$\{_probe_script\}\"\s+\].*?return\s+1",
		re.DOTALL,
	)
	assert python_check_pattern.search(source), (
		"fail-closed `command -v python3` guard with `return 1` not found in "
		"setup_serena.sh — probe_mcp_handshake may silently pass when python3 "
		"is unavailable, re-introducing the malformed-tool-list failure mode."
	)
	assert probe_script_check_pattern.search(source), (
		"fail-closed probe-script-presence guard with `return 1` not found in "
		"setup_serena.sh — probe_mcp_handshake may silently pass when "
		"mcp_handshake_probe.py is missing."
	)


# ── Standalone runner (CI uses `python3 tests/<file>.py`, not pytest) ───────


def _all_tests():
	return [
		test_probe_succeeds_against_well_behaved_mock_server,
		test_probe_detects_close_on_initialize,
		test_probe_times_out_on_unresponsive_server,
		test_probe_reports_spawn_failure_for_missing_binary,
		test_probe_rejects_invalid_json_response,
		test_probe_rejects_jsonrpc_error_response,
		test_probe_rejects_response_with_null_error_field,
		test_probe_rejects_oversized_response_without_newline,
		test_probe_distinguishes_eof_from_timeout_when_child_still_alive,
		test_probe_returns_64_on_invalid_timeout,
		test_probe_rejects_mismatched_response_id,
		test_setup_serena_skips_block_when_probe_fails,
		test_setup_serena_writes_block_when_probe_passes,
		test_setup_serena_probe_disabled_writes_block_unconditionally,
		test_probe_helper_definition_is_present_in_setup_serena,
	]


def main() -> int:
	failures = []
	for fn in _all_tests():
		try:
			fn()
			print(f"  ok  {fn.__name__}")
		except Exception:  # pylint: disable=broad-except
			failures.append(fn.__name__)
			print(f"FAIL  {fn.__name__}")
			traceback.print_exc()
	if failures:
		print(f"\n{len(failures)} test(s) failed: {', '.join(failures)}")
		return 1
	print(f"\nPASS — {len(_all_tests())} tests")
	return 0


if __name__ == "__main__":
	sys.exit(main())
