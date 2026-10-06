"""Resolver model launches must stay inside the review sandbox."""

import os
from pathlib import Path
import re
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts/review_conflict_resolve.sh"


def _source() -> str:
	return SCRIPT.read_text(encoding="utf-8")


def _helper() -> str:
	match = re.search(r"^_resolver_sandbox_opencode_attempt\(\)\n\{\n.*?\n\}\n", _source(), re.M | re.S)
	assert match is not None
	return match.group()


def _failure_helper() -> str:
	match = re.search(r"^_resolver_fail_closed\(\)\n\{\n.*?\n\}\n", _source(), re.M | re.S)
	assert match is not None
	return match.group()


def _persistence_helper() -> str:
	match = re.search(r"^_persist_resolver_retry_state_from_current_failure\(\)\n\{\n.*?\n\}\n", _source(), re.M | re.S)
	assert match is not None
	return match.group()


def _launch() -> str:
	src = _source()
	return src[src.index('  if [ "${_run_codex}" = "true" ]; then\n'):src.index('  resolver_clean_output="${tmp_output}.ansi-clean"')]


def _stub(tmp_path: Path) -> tuple[Path, Path]:
	calls = tmp_path / "calls"
	support = tmp_path / "support"
	support.mkdir()
	sandbox = support / "review_untrusted_sandbox.sh"
	sandbox.write_text("""#!/usr/bin/env bash
printf '%s %s %s\\n' "$1" "${REVIEW_SANDBOX_ROOT:-}" "$*" >> "$CALLS"
case "$1" in
  prepare-ephemeral)
    [ "${MODE:-}" != prepare_failed ] || exit 1
    if [ "${2:-}" = codex ]; then echo "$FAKE_ROOT/codex"; else echo "$FAKE_ROOT/claude"; fi ;;
  run)
    if [ "${MODE:-}" = transfer_failed ] || [ "${MODE:-}" = rollback_failed ]; then
      touch "$RUNTIME_DIR/review_sandbox_transfer_failed"
      if [ "${MODE:-}" = rollback_failed ]; then
        echo '::error::Review isolation snapshot or transfer rejected (ValueError) reason=transfer_rollback_failed' > "$RUNTIME_DIR/review_sandbox_transfer_reason_${3##*/}"
      else
        echo '::error::Review isolation snapshot or transfer rejected (ValueError) reason=unsafe_file' > "$RUNTIME_DIR/review_sandbox_transfer_reason_${3##*/}"
      fi
      exit 1
    fi
    [ "${MODE:-}" != outdated ] || exit 2
    [ "${7:-}" != claude ] || exit 75
    printf 'model output\\n' > "$3" ;;
  cleanup) [ "${MODE:-}" != cleanup_failed ] || exit 1 ;;
esac
""", encoding="utf-8")
	return sandbox, calls


@pytest.mark.parametrize("mode,engine,expected_rc", [
	("ok", "codex", 0), ("ok", "claude", 0),
	("prepare_failed", "codex", 1), ("outdated", "codex", 1),
	("transfer_failed", "codex", 1),
])
def test_attempts_never_run_host_writer(tmp_path, mode, engine, expected_rc):
	sandbox, calls = _stub(tmp_path)
	(sandbox.parent / "review_untrusted_workspace.py").symlink_to(SCRIPT.parent / "review_untrusted_workspace.py")
	paths = tmp_path / "paths"
	paths.write_text("scripts/example.py\n", encoding="utf-8")
	program = """set -euo pipefail
emit_conflict_resolver_substate() { :; }
_resolver_sandbox_attempt() {
  printf 'sandbox-attempt %s\\n' "$1" >> "$CALLS"
  if [ "$1" = claude ]; then return 75; fi
  _resolver_sandbox_opencode_attempt
  return "${_codex_exit}"
}
""" + _helper() + "\n" + _launch() + '\nprintf "exit=%s\\n" "${_codex_exit}"\n'
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("resolve conflict\n", encoding="utf-8")
	output = tmp_path / "output.txt"
	output.touch()
	env = dict(os.environ, SUPPORT_SCRIPTS_DIR=str(sandbox.parent), CALLS=str(calls),
		FAKE_ROOT=str(tmp_path), MODE=mode, RUNTIME_DIR=str(tmp_path),
		AI_ENGINE_RESOLVED_CONFLICT_RESOLVER=engine)
	env.pop("BASH_ENV", None)
	env.pop("ENV", None)
	setup = f'''resolver_sandbox_sh={str(sandbox)!r}
CONFLICTED_PATHS_FILE={str(paths)!r}
_effective_prompt_file={str(prompt)!r}
tmp_output={str(output)!r}
_stall_status_file={str(tmp_path / "status")!r}
CODEX_STALL_GUARD_HELPER=/nonexistent
CODEX_HEARTBEAT_HELPER=/nonexistent
MODEL_EDITOR=openai/model
_current_reasoning_effort=high
RESOLVER_OPENCODE_CONFIG=/dev/null
CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS=5
_codex_exit=0
_run_codex=true
attempt=1
'''
	result = subprocess.run(["bash", "-c", setup + program], env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert f"exit={expected_rc}" in result.stdout
	logged = calls.read_text(encoding="utf-8")
	assert "opencode_run_cmd" not in logged
	if mode == "prepare_failed":
		assert "run " not in logged and "cleanup " not in logged
	else:
		assert "prepare-ephemeral  prepare-ephemeral codex" in logged
		assert "run " in logged and "codex CONFLICT_RESOLVER write" in logged
		assert "cleanup " in logged
	if engine == "claude":
		assert logged.index("sandbox-attempt claude") < logged.index("sandbox-attempt codex") < logged.index("prepare-ephemeral ") < logged.index("run ")
		assert "prepare-ephemeral  prepare-ephemeral codex" in logged
		assert "claude" in logged and "codex" in logged
		assert "reason=claude_unavailable action=sandbox_opencode" in result.stderr
	assert not (tmp_path / "review_sandbox_transfer_failed").exists()
	if mode == "transfer_failed":
		assert "reason=unsafe_file" in result.stderr
	if mode == "outdated":
		assert "reason=sandbox_helper_outdated" in result.stderr


@pytest.mark.parametrize("mode", ["cleanup_failed", "rollback_failed"])
def test_unsafe_failure_stops_instead_of_retrying(tmp_path, mode):
	sandbox, calls = _stub(tmp_path)
	src = _helper()
	prompt = tmp_path / "prompt"
	prompt.write_text("resolve\n")
	output = tmp_path / "output"
	env = dict(os.environ, SUPPORT_SCRIPTS_DIR=str(sandbox.parent), CALLS=str(calls),
		FAKE_ROOT=str(tmp_path), MODE=mode, RUNTIME_DIR=str(tmp_path))
	result = subprocess.run(["bash", "-c", f'''set -euo pipefail
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ printf '%s\\n' "$RESOLVER_ISOLATION_FAILURE_REASON" > "$RUNTIME_DIR/persisted_reason"; }}
{_failure_helper()}
{src}
resolver_sandbox_sh={str(sandbox)!r}
tmp_output={str(output)!r}
_effective_prompt_file={str(prompt)!r}
MODEL_EDITOR=openai/model
_current_reasoning_effort=high
RESOLVER_OPENCODE_CONFIG=/dev/null
CODEX_STALL_GUARD_HELPER=/nonexistent
CODEX_HEARTBEAT_HELPER=/nonexistent
CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS=5
_codex_exit=0
_resolver_sandbox_opencode_attempt
echo unexpected
'''], env=env, capture_output=True, text=True)
	assert result.returncode == 1 and "unexpected" not in result.stdout
	expected_error = "sandbox cleanup failed" if mode == "cleanup_failed" else "reason=transfer_rollback_failed"
	assert expected_error in result.stderr
	assert (tmp_path / "persisted_reason").read_text().strip() == (
		"sandbox_cleanup_failed" if mode == "cleanup_failed" else "transfer_rollback_failed"
	)


def test_unsupported_path_refuses_before_any_model(tmp_path):
	sandbox, calls = _stub(tmp_path)
	paths = tmp_path / "paths"
	paths.write_text("assets/x.svg\n")
	src = _source()
	guard = src[src.index('# Reject unsupported conflict paths for both engines'):src.index('_resolver_sandbox_opencode_attempt()')]
	result = subprocess.run(["bash", "-c", f'''set -euo pipefail
RUNTIME_DIR={str(tmp_path)!r}
SUPPORT_SCRIPTS_DIR={str(SCRIPT.parent)!r}
CONFLICTED_PATHS_FILE={str(paths)!r}
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ printf '%s\\n' "$RESOLVER_ISOLATION_FAILURE_REASON" > "$RUNTIME_DIR/persisted_reason"; }}
{_failure_helper()}
{guard}
'''], cwd=tmp_path, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
	assert result.returncode == 1
	assert "reason=sandbox_path_unsupported" in result.stderr
	assert "assets/x.svg" not in result.stderr
	assert (tmp_path / "persisted_reason").read_text().strip() == "sandbox_path_unsupported"
	assert not calls.exists()


def test_missing_sandbox_support_refuses_before_any_model(tmp_path):
	src = _source()
	guard = src[src.index('# Reject unsupported conflict paths for both engines'):src.index('_resolver_sandbox_opencode_attempt()')]
	result = subprocess.run(["bash", "-c", f'''set -euo pipefail
RUNTIME_DIR={str(tmp_path)!r}
SUPPORT_SCRIPTS_DIR={str(tmp_path)!r}
CONFLICTED_PATHS_FILE={str(tmp_path / "paths")!r}
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ printf '%s\\n' "$RESOLVER_ISOLATION_FAILURE_REASON" > "$RUNTIME_DIR/persisted_reason"; }}
{_failure_helper()}
{guard}
'''], cwd=tmp_path, capture_output=True, text=True)
	assert result.returncode == 1
	assert "reason=sandbox_support_missing" in result.stderr
	assert "sandbox_path_unsupported" not in result.stderr
	assert (tmp_path / "persisted_reason").read_text().strip() == "sandbox_support_missing"


@pytest.mark.parametrize("reason,integration,should_build", [
	("sandbox_path_unsupported", "true", True),
	("sandbox_support_missing", "true", True),
	("sandbox_cleanup_failed", "true", True),
	("transfer_rollback_failed", "true", True),
	("", "true", False),
	("sandbox_path_unsupported", "false", False),
])
def test_isolation_failure_reaches_real_retry_state_gate(tmp_path, reason, integration, should_build):
	pr_payload = tmp_path / "pr.json"
	pr_payload.write_text('{"head":{"sha":"test-head"}}', encoding="utf-8")
	build_marker = tmp_path / "retry_builder_called"
	program = f'''set -euo pipefail
_build_resolver_retry_state_artifact() {{ printf '%s\\n' "$RESOLVER_ISOLATION_FAILURE_REASON" > "$BUILD_MARKER"; return 1; }}
{_persistence_helper()}
_persist_resolver_retry_state_from_current_failure
'''
	env = dict(os.environ, IS_INTEGRATION_SYNC=integration, RESOLVER_ISOLATION_FAILURE_REASON=reason,
		RESOLVER_FP_EXIT="0", RESOLVER_FP_VERIFICATION_TIER="strict", PR_NUMBER="123",
		GITHUB_REPOSITORY="owner/repo", PR_PAYLOAD_FILE=str(pr_payload), RUNTIME_DIR=str(tmp_path),
		RESOLVER_RETRY_STATE_ARTIFACT_FILE=str(tmp_path / "retry.json"), BUILD_MARKER=str(build_marker),
		SUPPORT_SCRIPTS_DIR=str(SCRIPT.parent))
	env.pop("BASH_ENV", None)
	env.pop("ENV", None)
	result = subprocess.run(["bash", "-c", program], env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert build_marker.exists() is should_build
	if should_build:
		assert build_marker.read_text(encoding="utf-8").strip() == reason


def test_no_host_model_launch_or_private_host_index_in_launch():
	src = _source()
	assert "opencode_run_cmd" not in src
	assert "GIT_INDEX_FILE=" not in _launch()
	assert "GIT_INDEX_FILE=" not in _helper()
	assert 'codex CONFLICT_RESOLVER write)' in _helper()
