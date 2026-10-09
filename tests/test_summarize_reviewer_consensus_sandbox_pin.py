#!/usr/bin/env python3
"""Contract test for the consensus summariser's read-only OpenCode config."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SUMMARISER_SCRIPT = REPO_ROOT / "scripts" / "summarize_reviewer_consensus.sh"
CONFIG_WRITER = REPO_ROOT / "scripts" / "write_opencode_config.sh"


def test_summariser_uses_isolated_reviewer_opencode_config() -> None:
	src = SUMMARISER_SCRIPT.read_text(encoding="utf-8")
	assert 'OPENCODE_HELPERS_PATH="${OPENCODE_HELPERS_PATH:-${SUPPORT_SCRIPTS_DIR:-scripts}/opencode_helpers.sh}"' in src
	assert 'OPENCODE_CONFIG_WRITER_PATH="${OPENCODE_CONFIG_WRITER_PATH:-${SUPPORT_SCRIPTS_DIR:-scripts}/write_opencode_config.sh}"' in src
	assert '--role reviewer' in src
	assert '--model "${SUMMARISER_MODEL}"' in src
	assert '--config-path "${summariser_opencode_config}"' in src
	assert '--serena off' in src
	assert 'opencode_require_bootstrap review_summariser reviewer "${SUMMARISER_MODEL}"' in src
	# OpenCode runs only in the review sandbox (finding
	# review-summarizer-host-fallback): no host command remains.
	assert 'opencode_run_cmd' not in src
	assert 'summariser_opencode_cmd' not in src
	assert '"${summariser_opencode_config}"' in src
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
	assert '"bash": "deny"' in writer
	assert '"bash": False' in writer
	assert '"edit": "deny"' in writer
	assert '"write": False' in writer
	assert '"patch": False' in writer
	assert '"apply_patch": False' in writer


def test_summariser_never_falls_back_to_the_host() -> None:
	src = SUMMARISER_SCRIPT.read_text(encoding="utf-8")
	assert 'summariser_engine_state="legacy"' not in src
	assert 'summariser_engine_state="sandbox_opencode"' in src
	assert "REVIEW_UTILITY_ISOLATION role=SUMMARISER engine=${refused_engine} outcome=refused" in src
	assert 'SUMMARISER_ISOLATION_MAX_ATTEMPTS="${SUMMARISER_ISOLATION_MAX_ATTEMPTS:-3}"' in src
	assert 'isolation_unavailable || true' in src
	assert "AI_ENGINE_FALLBACK role=SUMMARISER reason=sandbox_unavailable" not in src


def test_review_utility_roles_never_start_host_opencode() -> None:
	# Findings review-summarizer-host-fallback / review-smoke-host-opencode and
	# the interim-judge sibling: PR-derived text reaches OpenCode only through
	# the review sandbox's read-only utility roles.
	scripts_dir = REPO_ROOT / "scripts"
	for name, role in (
		("summarize_reviewer_consensus.sh", "SUMMARISER"),
		("review_synthesise_smoke.sh", "BEHAVIOURAL_SMOKE"),
		("review_run_judge_interim.sh", "JUDGE_INTERIM"),
	):
		src = (scripts_dir / name).read_text(encoding="utf-8")
		assert "opencode_run_cmd" not in src, name
		assert role in src, name
	sandbox = (scripts_dir / "review_untrusted_sandbox.sh").read_text(encoding="utf-8")
	allowlist = next(line for line in sandbox.splitlines() if line.startswith('case "${claude_role}" in REVIEW_EDITOR|'))
	for role in ("SUMMARISER", "BEHAVIOURAL_SMOKE", "JUDGE_INTERIM"):
		assert f"|{role}" in allowlist, role
	assert "WAVE_JUDGE|STALL_JUDGE|INTEGRATION_JUDGE|SECURITY_JUDGE|SUMMARISER|BEHAVIOURAL_SMOKE|JUDGE_INTERIM)" in sandbox
	assert '"SUMMARISER", "BEHAVIOURAL_SMOKE", "JUDGE_INTERIM"}' in sandbox
	assert 'if [ "${claude_role}" = JUDGE_INTERIM ] && [ "${engine}" != codex ]; then' in sandbox


def main() -> int:
	test_summariser_uses_isolated_reviewer_opencode_config()
	test_summariser_does_not_mutate_shared_codex_config()
	test_reviewer_role_denies_edits_and_allows_read_bash()
	print("OK: summariser OpenCode reviewer-role config contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
