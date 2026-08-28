#!/usr/bin/env python3
"""Contract test for the consensus summariser's read-only OpenCode config."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SUMMARISER_SCRIPT = REPO_ROOT / "scripts" / "summarize_reviewer_consensus.sh"
CONFIG_WRITER = REPO_ROOT / "scripts" / "write_opencode_config.sh"


def test_summariser_uses_isolated_reviewer_opencode_config() -> None:
	src = SUMMARISER_SCRIPT.read_text(encoding="utf-8")
	assert 'OPENCODE_HELPERS_PATH="${SUPPORT_SCRIPTS_DIR:-scripts}/opencode_helpers.sh"' in src
	assert 'OPENCODE_CONFIG_WRITER_PATH="${SUPPORT_SCRIPTS_DIR:-scripts}/write_opencode_config.sh"' in src
	assert '--role reviewer' in src
	assert '--model "${SUMMARISER_MODEL}"' in src
	assert '--config-path "${summariser_opencode_config}"' in src
	assert '--serena off' in src
	assert 'opencode_require_bootstrap review_summariser reviewer "${SUMMARISER_MODEL}"' in src
	assert 'opencode_run_cmd "$@"' in src
	assert '"${SUMMARISER_REASONING}"' in src
	assert 'opencode_strip_ansi < "${tmp_stdout}"' in src
	assert 'opencode_strip_ansi < "${tmp_stderr}"' in src


def test_summariser_does_not_mutate_shared_codex_config() -> None:
	src = SUMMARISER_SCRIPT.read_text(encoding="utf-8")
	assert "summariser_codex_home" not in src
	assert "sandbox_mode" not in src
	assert "model_reasoning_effort" not in src
	assert "sed -i" not in src
	assert 'command -v codex' not in src
	assert '"${codex_bin}"' not in src


def test_reviewer_role_denies_edits_and_allows_read_bash() -> None:
	writer = CONFIG_WRITER.read_text(encoding="utf-8")
	assert '"read": "allow"' in writer
	assert '"bash": "allow"' in writer
	assert '"edit": "deny"' in writer
	assert '"write": False' in writer
	assert '"patch": False' in writer
	assert '"apply_patch": False' in writer


def main() -> int:
	test_summariser_uses_isolated_reviewer_opencode_config()
	test_summariser_does_not_mutate_shared_codex_config()
	test_reviewer_role_denies_edits_and_allows_read_bash()
	print("OK: summariser OpenCode reviewer-role config contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
