#!/usr/bin/env python3
"""Tests for Ethereum/Hardhat JSON-RPC health-check parsing patterns.

Validates that the bash patterns prescribed in mode-validate-generate.txt and
mode-validate-fix-harness.txt correctly handle:
- Valid Hardhat JSON-RPC object responses (reads `.result`, never numeric indexes)
- Missing `.result` field (fails gracefully with clear message)
- Non-JSON / empty responses (fails gracefully with clear message)
- Array responses (fails gracefully for this single-call probe, which expects
  object responses with a `.result` field)

These tests exercise the canonical bash snippet via subprocess so that regressions
in the recommended pattern are caught before they reach generated harnesses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest


# ---------------------------------------------------------------------------
# Helper: run the canonical RPC probe snippet against a mock RESP variable
# ---------------------------------------------------------------------------

_PROBE_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env bash
    set -euo pipefail

    # The canonical pattern from mode-validate-generate.txt / mode-validate-fix-harness.txt
    # RESP is injected by the test as a pre-set variable.

    CHAIN_ID=""
    RESP="${RESP:-}"

    if [ -z "${RESP:-}" ]; then
        echo "RESULT:no_response"
        exit 0
    fi

    if ! printf '%s' "$RESP" | jq -e 'type == "object"' >/dev/null 2>&1; then
        echo "RESULT:non_json_or_not_object:${RESP}"
        exit 0
    fi

    CHAIN_ID="$(printf '%s' "$RESP" | jq -r 'if has("result") and (.result != null) and (.result | type == "string") and (.result | length) > 0 then .result else empty end')"
    if [ -z "$CHAIN_ID" ]; then
        echo "RESULT:missing_result_field:${RESP}"
        exit 0
    fi

    echo "RESULT:ok:${CHAIN_ID}"
""")


def _run_probe(resp_value: str) -> str:
    """Run the canonical probe script with RESP set to *resp_value*.

    Returns the RESULT: line emitted by the script (stripped).
    """
    env = os.environ.copy()
    env["RESP"] = resp_value

    result = subprocess.run(
        ["bash", "-c", _PROBE_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    for line in result.stdout.splitlines():
        if line.startswith("RESULT:"):
            return line
    raise AssertionError(
        f"No RESULT: line in output.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHardhatRpcProbe(unittest.TestCase):

    def test_valid_eth_chain_id_response(self) -> None:
        """Standard Hardhat eth_chainId response → correctly reads .result."""
        resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x7a69"})
        line = _run_probe(resp)
        self.assertTrue(line.startswith("RESULT:ok:0x7a69"), line)

    def test_valid_eth_block_number_response(self) -> None:
        """Standard Hardhat eth_blockNumber response → correctly reads .result."""
        resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x0"})
        line = _run_probe(resp)
        self.assertTrue(line.startswith("RESULT:ok:0x0"), line)

    def test_valid_response_with_extra_fields(self) -> None:
        """Response with extra fields is still parsed correctly."""
        resp = json.dumps({"jsonrpc": "2.0", "id": 42, "result": "0x1", "extra": "ignored"})
        line = _run_probe(resp)
        self.assertTrue(line.startswith("RESULT:ok:0x1"), line)

    def test_empty_response(self) -> None:
        """Empty response (curl failure / network issue) → clear failure message."""
        line = _run_probe("")
        self.assertEqual(line, "RESULT:no_response", line)

    def test_non_json_response(self) -> None:
        """Non-JSON response (HTML error page, plain text) → clear failure message."""
        line = _run_probe("Not Found")
        self.assertTrue(line.startswith("RESULT:non_json_or_not_object:"), line)

    def test_array_response_not_indexed_numerically(self) -> None:
        """Array response must NOT be indexed numerically.

        The old broken pattern used jq '.[0]' which causes:
            'jq: error (at <stdin>:1): Cannot index object with number'
        when Hardhat returns an object. Here we confirm our guard also handles
        the inverse: if someone mistakenly gets an array, it is caught gracefully.
        """
        # An array is not a JSON object → caught by 'type == "object"' guard.
        line = _run_probe('[{"jsonrpc":"2.0","id":1,"result":"0x1"}]')
        self.assertTrue(line.startswith("RESULT:non_json_or_not_object:"), line)

    def test_error_response_missing_result(self) -> None:
        """JSON-RPC error response (no .result field) → clear failure message."""
        resp = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Invalid request"}})
        line = _run_probe(resp)
        self.assertTrue(line.startswith("RESULT:missing_result_field:"), line)

    def test_null_result_field(self) -> None:
        """Response with null .result → treated as missing, fails gracefully."""
        resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": None})
        line = _run_probe(resp)
        self.assertTrue(line.startswith("RESULT:missing_result_field:"), line)

    def test_empty_string_result_field(self) -> None:
        """Response with empty-string .result → treated as invalid, fails gracefully.

        Ethereum chain IDs are always non-empty hex strings. An empty string in
        .result indicates a malformed response and should fail the health check.
        """
        resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": ""})
        line = _run_probe(resp)
        self.assertTrue(line.startswith("RESULT:missing_result_field:"), line)

    def test_prompt_files_contain_rpc_guidance(self) -> None:
        """Verify that both prompt files contain RPC and graceful-shutdown guidance sections."""
        from pathlib import Path

        prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
        for fname in ("mode-validate-generate.txt", "mode-validate-fix-harness.txt"):
            content = (prompts_dir / fname).read_text(encoding="utf-8")
            self.assertIn(
                "Cannot index object with number",
                content,
                f"{fname} must contain guidance about 'Cannot index object with number'",
            )
            self.assertIn(
                ".result",
                content,
                f"{fname} must instruct to read .result from JSON-RPC response",
            )
            self.assertIn(
                'type == "object"',
                content,
                f"{fname} must include 'type == \"object\"' guard in JSON-RPC pattern",
            )
            self.assertIn(
                "CRITICAL — Dockerfile PID 1 signal handling",
                content,
                f"{fname} must require PID 1 signal-forwarding guidance",
            )
            self.assertIn(
                "exec-form",
                content,
                f"{fname} must require exec-form CMD/ENTRYPOINT for PID 1",
            )
            self.assertIn(
                "init: true",
                content,
                f"{fname} must mention init: true as a fallback",
            )
            self.assertIn(
                "accept exit code 137",
                content,
                f"{fname} must forbid accepting 137 as graceful shutdown",
            )


# ---------------------------------------------------------------------------
# Standalone runner (matches CI convention: python3 tests/test_*.py)
# ---------------------------------------------------------------------------

def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestHardhatRpcProbe)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
