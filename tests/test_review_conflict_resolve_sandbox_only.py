"""Resolver model launches must stay inside the review sandbox."""

import json
import os
from pathlib import Path
import re
import shutil
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


def _path_failure_helper() -> str:
	match = re.search(r"^_resolver_fail_closed_for_conflict_paths\(\)\n\{\n.*?\n\}\n", _source(), re.M | re.S)
	assert match is not None
	return match.group()


def _persistence_helper() -> str:
	match = re.search(r"^_persist_resolver_retry_state_from_current_failure\(\)\n\{\n.*?\n\}\n", _source(), re.M | re.S)
	assert match is not None
	return match.group()


def _report_helper() -> str:
	match = re.search(r"^_resolver_report_rejected_path\(\)\n\{\n.*?\n\}\n", _source(), re.M | re.S)
	assert match is not None
	return match.group()


def _guard() -> str:
	src = _source()
	return src[src.index('# Reject unsupported conflict paths for both engines'):src.index('_resolver_sandbox_opencode_attempt()')]


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
    if [ "${MODE:-}" = transfer_failed ] || [ "${MODE:-}" = transfer_category ] || [ "${MODE:-}" = transfer_unknown ] || [ "${MODE:-}" = rollback_failed ]; then
      touch "$RUNTIME_DIR/review_sandbox_transfer_failed"
      if [ "${MODE:-}" = rollback_failed ]; then
        echo '::error::Review isolation snapshot or transfer rejected (ValueError) reason=transfer_rollback_failed' > "$RUNTIME_DIR/review_sandbox_transfer_reason_${3##*/}"
      elif [ "${MODE:-}" = transfer_category ]; then
        echo '::error::Review isolation snapshot or transfer rejected (ValueError) reason=unsafe_directory category=symlink depth=2' > "$RUNTIME_DIR/review_sandbox_transfer_reason_${3##*/}"
      elif [ "${MODE:-}" = transfer_unknown ]; then
        echo 'unexpected transfer error' > "$RUNTIME_DIR/review_sandbox_transfer_reason_${3##*/}"
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
	("transfer_failed", "codex", 1), ("transfer_category", "codex", 1),
])
def test_attempts_never_run_host_writer(tmp_path, mode, engine, expected_rc):
	sandbox, calls = _stub(tmp_path)
	(sandbox.parent / "review_untrusted_workspace.py").symlink_to(SCRIPT.parent / "review_untrusted_workspace.py")
	paths = tmp_path / "paths"
	paths.write_text("scripts/example.py\n", encoding="utf-8")
	program = """set -euo pipefail
emit_conflict_resolver_substate() { :; }
_persist_resolver_retry_state_from_current_failure() { :; }
_resolver_sandbox_attempt() {
  printf 'sandbox-attempt %s\\n' "$1" >> "$CALLS"
  if [ "$1" = claude ]; then return 75; fi
  _resolver_sandbox_opencode_attempt
  return "${_codex_exit}"
}
	""" + _failure_helper() + "\n" + _report_helper() + "\n" + _helper() + "\n" + _launch() + '\nprintf "exit=%s\\n" "${_codex_exit}"\n'
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
RESOLVER_MODEL_PATHS_FILE={str(paths)!r}
RESOLVER_CHECK_PATHS_STDERR_FILE={str(tmp_path / "check_paths_stderr")!r}
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
	assert result.returncode == (0 if expected_rc == 0 else 1), result.stderr
	if expected_rc == 0:
		assert "exit=0" in result.stdout
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
	if mode == "transfer_category":
		assert "reason=unsafe_directory category=symlink depth=2" in result.stderr
	if mode == "outdated":
		assert "reason=sandbox_helper_outdated" in result.stderr


@pytest.mark.parametrize("mode", ["prepare_failed", "outdated", "transfer_failed", "transfer_category", "transfer_unknown", "cleanup_failed", "rollback_failed"])
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
	expected_error = {
		"prepare_failed": "reason=sandbox_prepare_failed",
		"outdated": "reason=sandbox_helper_outdated",
		"transfer_failed": "reason=sandbox_transfer_failed",
		"transfer_category": "reason=sandbox_transfer_failed",
		"transfer_unknown": "reason=sandbox_transfer_failed",
		"cleanup_failed": "sandbox cleanup failed",
		"rollback_failed": "reason=transfer_rollback_failed",
	}[mode]
	assert expected_error in result.stderr
	assert (tmp_path / "persisted_reason").read_text().strip() == (
		"sandbox_cleanup_failed" if mode == "cleanup_failed" else expected_error.removeprefix("reason=")
	)


REJECTED_LINE = re.compile(r"^REVIEW_RESOLVER_PATH_REJECTED reason=[a-z_]{1,32} path=(redacted|[A-Za-z0-9._/-]{1,128})$", re.M)


def _run_guard(tmp_path, model_paths):
	sandbox, calls = _stub(tmp_path)
	result = subprocess.run(["bash", "-c", f'''set -euo pipefail
RUNTIME_DIR={str(tmp_path)!r}
SUPPORT_SCRIPTS_DIR={str(SCRIPT.parent)!r}
CONFLICTED_PATHS_FILE={str(tmp_path / "all_conflicted")!r}
RESOLVER_MODEL_PATHS_FILE={str(model_paths)!r}
RESOLVER_CHECK_PATHS_STDERR_FILE={str(tmp_path / "check_paths_stderr")!r}
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ printf '%s\\n' "$RESOLVER_ISOLATION_FAILURE_REASON" > "$RUNTIME_DIR/persisted_reason"; }}
{_failure_helper()}
{_path_failure_helper()}
{_report_helper()}
{_guard()}
echo guard-passed
'''], cwd=tmp_path, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
	return result, calls


# Plainly named paths absent from the host are host-only (origin/main);
# only the odd name keeps the nameless sandbox_path_unsupported reason.
@pytest.mark.parametrize("path,reason,shown,fail_reason", [
	("assets/x.svg", "unsupported_type", "assets/x.svg", "sandbox_path_host_only"),
	(".ai/x.txt", "excluded_component", ".ai/x.txt", "sandbox_path_host_only"),
	(".claude/settings.json", "dot_directory", ".claude/settings.json", "sandbox_path_host_only"),
	(".claude/hooks/pr_merge_status_guard.py", "live_safety_hook", ".claude/hooks/pr_merge_status_guard.py", "sandbox_path_host_only"),
	("assets/\x1b[31m::set-output.svg", "unsupported_type", "redacted", "sandbox_path_unsupported"),
])
def test_rejected_path_reason_is_logged_before_any_model(tmp_path, path, reason, shown, fail_reason):
	paths = tmp_path / "paths"
	paths.write_text(path + "\n", encoding="utf-8")
	result, calls = _run_guard(tmp_path, paths)
	assert result.returncode == 1
	assert "guard-passed" not in result.stdout
	# The existing fail-closed lines stay byte-identical.
	assert f"AI_ENGINE_FALLBACK role=CONFLICT_RESOLVER reason={fail_reason} action=fail_closed\n" in result.stderr
	assert f"::error::Conflict resolver isolation unavailable (reason={fail_reason}); refusing host fallback.\n" in result.stderr
	rejected = REJECTED_LINE.findall(result.stderr)
	assert len(rejected) == 1
	assert f"REVIEW_RESOLVER_PATH_REJECTED reason={reason} path={shown}\n" in result.stderr
	assert "\x1b" not in result.stderr and "::set-output" not in result.stderr
	assert (tmp_path / "persisted_reason").read_text().strip() == fail_reason
	assert not calls.exists()


def _run_path_guard(tmp_path, conflicted, integration="false"):
	paths = tmp_path / "paths"
	paths.write_text("".join(name + "\n" for name in conflicted))
	github_env = tmp_path / "github_env"
	src = _source()
	guard = src[src.index('# Reject unsupported conflict paths for both engines'):src.index('_resolver_sandbox_opencode_attempt()')]
	result = subprocess.run(["bash", "-c", f'''set -euo pipefail
RUNTIME_DIR={str(tmp_path)!r}
SUPPORT_SCRIPTS_DIR={str(SCRIPT.parent)!r}
CONFLICTED_PATHS_FILE={str(paths)!r}
RESOLVER_MODEL_PATHS_FILE={str(paths)!r}
RESOLVER_CHECK_PATHS_STDERR_FILE={str(tmp_path / "check_paths_stderr")!r}
GITHUB_ENV={str(github_env)!r}
IS_INTEGRATION_SYNC={integration}
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ printf '%s\\n' "$RESOLVER_ISOLATION_FAILURE_REASON" > "$RUNTIME_DIR/persisted_reason"; }}
{_failure_helper()}
{_path_failure_helper()}
{_report_helper()}
{guard}
echo guard-passed
'''], cwd=tmp_path, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
	env_text = github_env.read_text() if github_env.exists() else ""
	return result, env_text


def test_unsupported_path_refuses_before_any_model(tmp_path):
	sandbox, calls = _stub(tmp_path)
	(tmp_path / "scripts").mkdir()
	os.symlink("/etc/passwd", tmp_path / "scripts/link.py")
	result, env_text = _run_path_guard(tmp_path, ["scripts/link.py", "assets/x.svg"])
	assert result.returncode == 1 and "guard-passed" not in result.stdout
	assert "reason=sandbox_path_unsupported" in result.stderr
	# An unsafe entry keeps every name out of the log, even a plain one.
	assert "assets/x.svg" not in result.stderr and "link.py" not in result.stderr
	assert (tmp_path / "persisted_reason").read_text().strip() == "sandbox_path_unsupported"
	assert env_text == "AUTOFIX_FAILURE_REASON=conflict_resolver_sandbox_path_unsupported\n"
	assert not calls.exists()


def test_paired_model_paths_pass_guard(tmp_path):
	# The paired live hook is removed from the model paths; the template passes.
	paths = tmp_path / "model_paths"
	paths.write_text("workflow-templates/.claude/hooks/pr_merge_status_guard.py\n", encoding="utf-8")
	result, calls = _run_guard(tmp_path, paths)
	assert result.returncode == 0, result.stderr
	assert "guard-passed" in result.stdout
	assert "REVIEW_RESOLVER_PATH_REJECTED" not in result.stderr
	assert not calls.exists()


def test_paired_live_copy_source_contract():
	src = _source()
	assert 'check-paths "$(pwd)" "${CONFLICTED_PATHS_FILE}"' not in src
	assert src.count('check-paths "$(pwd)" "${RESOLVER_MODEL_PATHS_FILE}"') == 2
	assert src.index('paired-live-copies "$(pwd)"') < src.index('# Reject unsupported conflict paths for both engines')
	assert src.index("  _resolver_mirror_paired_live_copies\n  _scan_residual_markers\n") > 0
	tail = src[src.index("  if ! _resolver_verify_paired_live_index; then"):]
	assert tail.index("_resolver_verify_paired_live_index") < tail.index("verify_resolver_index_complete_or_fail")
	assert '--paths-file "${RESOLVER_TARGETED_PATHS_FILE}"' in src
	workspace = (SCRIPT.parent / "review_untrusted_workspace.py").read_text(encoding="utf-8")
	assert 'if name == ".claude/hooks/pr_merge_status_guard.py":\n\t\treturn False' in workspace


def test_host_only_paths_stop_once_with_their_names(tmp_path):
	"""PR #6438: the host-executed guard hook must never reach the sandboxed model."""
	sandbox, calls = _stub(tmp_path)
	(tmp_path / ".claude/hooks").mkdir(parents=True)
	(tmp_path / ".claude/hooks/pr_merge_status_guard.py").write_text("x = 1\n")
	result, env_text = _run_path_guard(tmp_path, [".claude/hooks/pr_merge_status_guard.py", "agents.md", "assets/x.svg"])
	assert result.returncode == 1 and "guard-passed" not in result.stdout
	assert "::error::Conflict resolver: host-only conflicted path(s) need a manual merge: .claude/hooks/pr_merge_status_guard.py, assets/x.svg" in result.stderr
	assert "agents.md" not in result.stderr
	assert "reason=sandbox_path_host_only" in result.stderr and "sandbox_path_unsupported" not in result.stderr
	assert (tmp_path / "persisted_reason").read_text().strip() == "sandbox_path_host_only"
	assert env_text == "AUTOFIX_FAILURE_REASON=conflict_resolver_sandbox_path_host_only\n"
	assert not calls.exists()


def test_integration_sync_keeps_the_generic_failure_reason(tmp_path):
	"""Integration-sync failures must keep counting toward the retry-state escape threshold."""
	(tmp_path / ".claude/hooks").mkdir(parents=True)
	(tmp_path / ".claude/hooks/pr_merge_status_guard.py").write_text("x = 1\n")
	result, env_text = _run_path_guard(tmp_path, [".claude/hooks/pr_merge_status_guard.py"], integration="true")
	assert result.returncode == 1 and "reason=sandbox_path_host_only" in result.stderr
	assert env_text == ""


def test_sandbox_supported_paths_pass_the_guard(tmp_path):
	result, env_text = _run_path_guard(tmp_path, ["agents.md", ".github/workflows/review_autofix.yml"])
	assert result.returncode == 0, result.stderr
	assert "guard-passed" in result.stdout and env_text == ""


def test_host_only_error_is_the_failure_headline(tmp_path):
	import importlib.util
	spec = importlib.util.spec_from_file_location("workflow_failure_heal", SCRIPT.parent / "workflow_failure_heal.py")
	heal = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(heal)
	(tmp_path / ".claude/hooks").mkdir(parents=True)
	(tmp_path / ".claude/hooks/pr_merge_status_guard.py").write_text("x = 1\n")
	result, _env_text = _run_path_guard(tmp_path, [".claude/hooks/pr_merge_status_guard.py"])
	assert heal.failure_headline([result.stderr]) == "Conflict resolver: host-only conflicted path(s) need a manual merge: .claude/hooks/pr_merge_status_guard.py"


def test_real_merge_conflict_on_guard_hook_fails_closed(tmp_path):
	"""Heal #6800 / PR #6555 (run 37828359475): the host-only hook needs a human merge."""
	git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_") and key not in {"BASH_ENV", "ENV"}}

	def git(*args, check=True):
		return subprocess.run(["git", *args], cwd=tmp_path, env=git_env, check=check, capture_output=True, text=True)

	git("init", "-q", "-b", "main")
	git("config", "user.name", "t")
	git("config", "user.email", "t@t")
	git("config", "commit.gpgsign", "false")
	hook = tmp_path / ".claude/hooks/pr_merge_status_guard.py"
	hook.parent.mkdir(parents=True)
	hook.write_text("x = 1\n", encoding="utf-8")
	git("add", "-A")
	git("commit", "-qm", "base")
	git("checkout", "-q", "-b", "pr")
	hook.write_text("x = 2\n", encoding="utf-8")
	git("commit", "-qam", "pr")
	git("checkout", "-q", "main")
	hook.write_text("x = 3\n", encoding="utf-8")
	git("commit", "-qam", "main")
	git("checkout", "-q", "pr")
	assert git("merge", "--no-edit", "main", check=False).returncode != 0
	stages = git("ls-files", "-u", "--", ".claude/hooks/pr_merge_status_guard.py").stdout
	assert set(re.findall(r"^\d+ [0-9a-f]+ ([123])\t", stages, re.M)) == {"1", "2", "3"}
	conflicted = git("diff", "--name-only", "--diff-filter=U").stdout.splitlines()
	assert conflicted == [".claude/hooks/pr_merge_status_guard.py"]

	_sandbox, calls = _stub(tmp_path)
	result, env_text = _run_path_guard(tmp_path, conflicted)
	assert result.returncode == 1 and "guard-passed" not in result.stdout
	assert "::error::Conflict resolver: host-only conflicted path(s) need a manual merge: .claude/hooks/pr_merge_status_guard.py" in result.stderr
	assert "reason=sandbox_path_host_only" in result.stderr and "sandbox_path_unsupported" not in result.stderr
	assert (tmp_path / "persisted_reason").read_text().strip() == "sandbox_path_host_only"
	assert env_text == "AUTOFIX_FAILURE_REASON=conflict_resolver_sandbox_path_host_only\n"
	assert not calls.exists()
	assert "<<<<<<<" in hook.read_text(encoding="utf-8")


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


# Issue #7059: a nameless sandbox_path_unsupported failure now names the kind of
# path it rejected, as a fixed category token and a depth bucket, before the
# generic fail-closed line, so it becomes the failure comment's First error.
UNSAFE_SUMMARY_PREFIX = "::error::Conflict resolver: conflicted path(s) cannot enter the sandbox: "


def _first_error(stderr):
	return next(line for line in stderr.splitlines() if line.startswith("::error::"))


def _make_symlinked_file(tmp_path):
	(tmp_path / "scripts").mkdir(exist_ok=True)
	os.symlink("/etc/passwd", tmp_path / "scripts/link.py")
	return "scripts/link.py", "symlink", "2"


def _make_symlinked_parent(tmp_path):
	(tmp_path / "real").mkdir()
	(tmp_path / "real/x.py").write_text("x = 1\n")
	(tmp_path / "scripts").mkdir(exist_ok=True)
	os.symlink(tmp_path / "real", tmp_path / "scripts/sub")
	return "scripts/sub/x.py", "symlink_in_path", "3+"


def _make_fifo(tmp_path):
	(tmp_path / "assets").mkdir(exist_ok=True)
	os.mkfifo(tmp_path / "assets/pipe.svg")
	return "assets/pipe.svg", "special_file", "2"


def _make_odd_name(tmp_path):
	return "assets/odd $name.svg", "unsafe_name", "2"


def _make_odd_named_symlink(tmp_path):
	(tmp_path / "assets").mkdir(exist_ok=True)
	os.symlink("/etc/passwd", tmp_path / "assets/odd $link.svg")
	return "assets/odd $link.svg", "symlink", "2"


def _make_nul_byte_name(tmp_path):
	# Pure paths accept NUL; the classifier must not walk the host for it.
	return "assets/odd\x00name.svg", "unsafe_name", "2"


@pytest.mark.parametrize("make", [_make_symlinked_file, _make_symlinked_parent, _make_fifo, _make_odd_name, _make_odd_named_symlink, _make_nul_byte_name])
@pytest.mark.parametrize("integration", ["false", "true"])
def test_unsafe_path_category_is_named_without_the_path(tmp_path, make, integration):
	_sandbox, calls = _stub(tmp_path)
	name, category, depth = make(tmp_path)
	result, env_text = _run_path_guard(tmp_path, [name], integration=integration)
	assert result.returncode == 1 and "guard-passed" not in result.stdout
	summary = f"{UNSAFE_SUMMARY_PREFIX}unsafe=1 host_only=0 categories={category} depths={depth}"
	assert summary + "\n" in result.stderr
	assert _first_error(result.stderr) == summary
	assert "reason=sandbox_path_unsupported" in result.stderr
	for part in name.split("/"):
		if part not in ("scripts", "assets"):
			assert part not in result.stderr
	for line in result.stderr.splitlines():
		if line.startswith("REVIEW_RESOLVER_PATH_REJECTED"):
			assert REJECTED_LINE.fullmatch(line) and line.endswith("path=redacted")
	assert (tmp_path / "persisted_reason").read_text().strip() == "sandbox_path_unsupported"
	expected_env = "" if integration == "true" else "AUTOFIX_FAILURE_REASON=conflict_resolver_sandbox_path_unsupported\n"
	assert env_text == expected_env
	assert not calls.exists()


@pytest.mark.parametrize("make,reason", [(_make_symlinked_file, "symlink"), (_make_symlinked_parent, "symlink_in_path")])
def test_symlink_rejection_logs_its_specific_reason(tmp_path, make, reason):
	name, _category, _depth = make(tmp_path)
	result, _env_text = _run_path_guard(tmp_path, [name])
	assert f"REVIEW_RESOLVER_PATH_REJECTED reason={reason} path=redacted\n" in result.stderr
	assert "reason=unsafe_file" not in result.stderr


def test_mixed_host_only_and_unsafe_names_categories_only(tmp_path):
	_sandbox, calls = _stub(tmp_path)
	name, _category, _depth = _make_symlinked_file(tmp_path)
	result, env_text = _run_path_guard(tmp_path, [name, "assets/x.svg"])
	assert result.returncode == 1
	assert _first_error(result.stderr) == f"{UNSAFE_SUMMARY_PREFIX}unsafe=1 host_only=1 categories=symlink depths=2"
	assert "assets/x.svg" not in result.stderr and "link.py" not in result.stderr
	assert "host-only conflicted path(s) need a manual merge" not in result.stderr
	assert env_text == "AUTOFIX_FAILURE_REASON=conflict_resolver_sandbox_path_unsupported\n"
	assert not calls.exists()


def test_all_host_only_report_has_no_category_line(tmp_path):
	(tmp_path / ".claude/hooks").mkdir(parents=True)
	(tmp_path / ".claude/hooks/pr_merge_status_guard.py").write_text("x = 1\n")
	result, _env_text = _run_path_guard(tmp_path, [".claude/hooks/pr_merge_status_guard.py", "assets/x.svg"])
	assert "cannot enter the sandbox" not in result.stderr
	assert _first_error(result.stderr) == "::error::Conflict resolver: host-only conflicted path(s) need a manual merge: .claude/hooks/pr_merge_status_guard.py, assets/x.svg"


def _run_report_only(tmp_path, report_text):
	report = tmp_path / "report.txt"
	report.write_text(report_text, encoding="utf-8")
	result = subprocess.run(["bash", "-c", f'''set -euo pipefail
RUNTIME_DIR={str(tmp_path)!r}
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ :; }}
{_failure_helper()}
{_path_failure_helper()}
_resolver_fail_closed_for_conflict_paths {str(report)!r}
'''], cwd=tmp_path, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
	return result


def test_forged_and_legacy_report_lines_read_as_other(tmp_path):
	result = _run_report_only(tmp_path, "unsafe\tevil$(x)\t9\nunsafe\nunsafe\t::error::spoof\t1\n")
	assert result.returncode == 1
	assert _first_error(result.stderr) == f"{UNSAFE_SUMMARY_PREFIX}unsafe=3 host_only=0 categories=other depths=1"
	assert "evil" not in result.stderr and "spoof" not in result.stderr
	assert "reason=sandbox_path_unsupported" in result.stderr


def test_empty_report_keeps_the_generic_failure(tmp_path):
	result = _run_report_only(tmp_path, "")
	assert result.returncode == 1
	assert "cannot enter the sandbox" not in result.stderr
	assert "reason=sandbox_path_unsupported" in result.stderr


def test_unsafe_category_is_the_failure_headline(tmp_path):
	import importlib.util
	spec = importlib.util.spec_from_file_location("workflow_failure_heal", SCRIPT.parent / "workflow_failure_heal.py")
	heal = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(heal)
	name, _category, _depth = _make_symlinked_file(tmp_path)
	result, _env_text = _run_path_guard(tmp_path, [name])
	assert heal.failure_headline([result.stderr]) == "Conflict resolver: conflicted path(s) cannot enter the sandbox: unsafe=1 host_only=0 categories=symlink depths=2"


# --- Host-only conflicts: base wins out of scope, re-issue in scope (#6748) ---

PREPARE = SCRIPT.parent / "review_conflict_prepare.sh"
HOOK = ".claude/hooks/pr_merge_status_guard.py"
ALLOWLIST_BODY = "Change the hook.\n\nfiles_touched:\n  - " + HOOK + "\n"


def _named_function(name: str) -> str:
	match = re.search(rf"^{name}\(\)\n\{{\n.*?\n\}}\n", _source(), re.M | re.S)
	assert match is not None, name
	return match.group()


def _take_base_block() -> str:
	text = PREPARE.read_text(encoding="utf-8")
	start = text.index("# >>> host-only take-base (issue #6748)")
	end = text.index("# <<< host-only take-base (issue #6748)", start)
	return text[start:end]


def _git_env() -> dict:
	return {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("BASH_ENV", "ENV")}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
	return subprocess.run(["git", *args], cwd=repo, env=_git_env(), check=check, text=True, capture_output=True)


def _host_only_conflict(repo: Path, shape: str = "both", ordinary: bool = False) -> None:
	"""HEAD `feat` and merged-in `main` conflict on the guard hook.

	shape: `both` (both modify, stages 1 2 3), `base_deleted` (main deletes,
	stages 1 2), `pr_deleted` (feat deletes, stages 1 3).
	"""
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "t")
	_git(repo, "config", "user.email", "t@t")
	hook = repo / HOOK
	hook.parent.mkdir(parents=True)
	hook.write_text("x = 0\n", encoding="utf-8")
	(repo / "notes.md").write_text("base\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")
	_git(repo, "checkout", "-q", "-b", "feat")
	if shape == "pr_deleted":
		_git(repo, "rm", "-q", "--", HOOK)
	else:
		hook.write_text("x = 'pr side'\n", encoding="utf-8")
	if ordinary:
		(repo / "notes.md").write_text("pr\n", encoding="utf-8")
	_git(repo, "commit", "-qam", "feat change")
	_git(repo, "checkout", "-q", "main")
	if shape == "base_deleted":
		_git(repo, "rm", "-q", "--", HOOK)
	else:
		hook.write_text("x = 'base side'\n", encoding="utf-8")
	if ordinary:
		(repo / "notes.md").write_text("main\n", encoding="utf-8")
	_git(repo, "commit", "-qam", "main change")
	_git(repo, "checkout", "-q", "feat")
	assert _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False).returncode != 0


def _run_take_base(tmp_path: Path, repo: Path, *, linked=None, status="ok", target_branch="", enabled=None):
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	github_env = tmp_path / "github.env"
	allowlist = runtime / "resolver_unmerged_allowlist.txt"
	linked_file = runtime / "linked_issues_raw.json"
	linked_file.write_text(json.dumps(linked if linked is not None else [{"number": 31, "title": "t", "body": "No scope", "labels": []}]), encoding="utf-8")
	(runtime / "linked_issues_raw.json.status").write_text(status + "\n", encoding="utf-8")
	script = f"""set -euo pipefail
RUNTIME_DIR={str(runtime)!r}
GITHUB_ENV={str(github_env)!r}
SUPPORT_SCRIPTS_DIR={str(SCRIPT.parent)!r}
GITHUB_REPOSITORY=o/r
PR_NUMBER=7
BASE_BRANCH=main
HEAD_REF=feat
IS_WORKFLOW_SOURCE_REPO=true
RESOLVER_ALLOWLIST_FILE={str(allowlist)!r}
gh_retry() {{ printf '%s\\n' "$*" >> "$RUNTIME_DIR/gh_calls"; for a in "$@"; do case "$a" in body=@*) cp "${{a#body=@}}" "$RUNTIME_DIR/posted.md" ;; esac; done; }}
git diff --name-only --diff-filter=U | sort -u > "${{RESOLVER_ALLOWLIST_FILE}}"
_resolver_allowlist_count="$(wc -l < "${{RESOLVER_ALLOWLIST_FILE}}" | tr -d '[:space:]')"
{_take_base_block()}
echo block-fell-through
"""
	env = {**_git_env(), "PYTHONDONTWRITEBYTECODE": "1"}
	for name in ("TARGET_BRANCH", "HOST_ONLY_CONFLICT_TAKE_BASE_ENABLED", "IS_INTEGRATION_SYNC", "LINKED_ISSUES_RAW_FILE"):
		env.pop(name, None)
	if target_branch:
		env["TARGET_BRANCH"] = target_branch
	if enabled is not None:
		env["HOST_ONLY_CONFLICT_TAKE_BASE_ENABLED"] = enabled
	result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, text=True, capture_output=True)
	return result, runtime, github_env, allowlist


def _unmerged(repo: Path) -> list[str]:
	return _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split()


def test_unlisted_host_only_conflict_takes_base_and_commits(tmp_path):
	repo = tmp_path / "repo"
	repo.mkdir()
	_host_only_conflict(repo)
	result, runtime, github_env, _ = _run_take_base(tmp_path, repo)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "block-fell-through" not in result.stdout
	assert f"CONFLICT_RESOLVER_HOST_ONLY_TAKE_BASE pr=7 path={HOOK} base=main stages=1 2 3" in result.stdout
	assert github_env.read_text(encoding="utf-8") == "CONFLICT_RESOLVED=true\n"
	assert len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()) == 3
	assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "[ai-merge-resolve] resolve merge conflicts"
	assert (repo / HOOK).read_text(encoding="utf-8") == "x = 'base side'\n"
	posted = (runtime / "posted.md").read_text(encoding="utf-8")
	assert posted.startswith("<!-- ai:host-only-take-base:v1 pr=7 head=")
	assert "+x = 'pr side'" in posted and "```diff" in posted
	assert (runtime / "host_only_conflict_scope.tsv").read_text(encoding="utf-8") == f"take_base\t{HOOK}\n"


@pytest.mark.parametrize("shape,stages,exists", [("base_deleted", "1 2", False), ("pr_deleted", "1 3", True)])
def test_modify_delete_host_only_conflict_takes_base(tmp_path, shape, stages, exists):
	repo = tmp_path / "repo"
	repo.mkdir()
	_host_only_conflict(repo, shape=shape)
	result, _runtime, github_env, _ = _run_take_base(tmp_path, repo)
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"stages={stages}" in result.stdout
	assert github_env.read_text(encoding="utf-8") == "CONFLICT_RESOLVED=true\n"
	assert (repo / HOOK).exists() is exists
	assert (HOOK in _git(repo, "ls-files").stdout.split()) is exists
	if exists:
		assert (repo / HOOK).read_text(encoding="utf-8") == "x = 'base side'\n"


def test_ordinary_conflicts_still_go_to_the_sandbox(tmp_path):
	repo = tmp_path / "repo"
	repo.mkdir()
	_host_only_conflict(repo, ordinary=True)
	result, runtime, github_env, allowlist = _run_take_base(tmp_path, repo)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "block-fell-through" in result.stdout
	assert allowlist.read_text(encoding="utf-8") == "notes.md\n"
	assert _unmerged(repo) == ["notes.md"]
	assert not github_env.exists()
	assert (runtime / "host_only_take_base_comment.md").is_file()


def _paired_hook_conflict(repo: Path, template_differs: bool) -> None:
	"""Live hook and its workflow-templates twin both conflict."""
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "t")
	_git(repo, "config", "user.email", "t@t")
	template = repo / "workflow-templates" / HOOK
	for path in (repo / HOOK, template):
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("x = 0\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")
	_git(repo, "checkout", "-q", "-b", "feat")
	(repo / HOOK).write_text("x = 'pr'\n", encoding="utf-8")
	template.write_text("x = 'pr'\n", encoding="utf-8")
	_git(repo, "commit", "-qam", "feat change")
	_git(repo, "checkout", "-q", "main")
	(repo / HOOK).write_text("x = 'base'\n", encoding="utf-8")
	template.write_text("x = 'base template'\n" if template_differs else "x = 'base'\n", encoding="utf-8")
	_git(repo, "commit", "-qam", "main change")
	_git(repo, "checkout", "-q", "feat")
	assert _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False).returncode != 0


@pytest.mark.parametrize("template_differs", [False, True])
def test_paired_live_copy_is_never_taken_from_base(tmp_path, template_differs):
	repo = tmp_path / "repo"
	repo.mkdir()
	_paired_hook_conflict(repo, template_differs)
	result, runtime, github_env, _ = _run_take_base(tmp_path, repo)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "CONFLICT_RESOLVER_HOST_ONLY_TAKE_BASE" not in result.stdout
	assert "block-fell-through" in result.stdout
	assert sorted(_unmerged(repo)) == sorted([HOOK, "workflow-templates/" + HOOK])
	assert not (runtime / "host_only_conflict_scope.tsv").exists()
	assert not github_env.exists()
	if template_differs:
		assert "reason=unpaired_live_copy" in result.stdout


def test_declared_host_only_path_is_left_for_reissue(tmp_path):
	repo = tmp_path / "repo"
	repo.mkdir()
	_host_only_conflict(repo)
	result, runtime, github_env, _ = _run_take_base(tmp_path, repo, linked=[{"number": 31, "title": "t", "body": ALLOWLIST_BODY, "labels": []}])
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=in_scope" in result.stdout and "block-fell-through" in result.stdout
	assert (runtime / "host_only_conflict_scope.tsv").read_text(encoding="utf-8") == f"in_scope\t{HOOK}\n"
	assert _unmerged(repo) == [HOOK]
	assert not github_env.exists()


@pytest.mark.parametrize("kwargs,log", [
	({"status": "failed"}, "outcome=unknown reason=linked_issues_unavailable"),
	({"linked": []}, "outcome=unknown reason=linked_issues_unavailable_or_empty"),
	({"target_branch": "orchestrator/project-12"}, ""),
	({"enabled": "false"}, ""),
])
def test_unknown_scope_or_disabled_leaves_the_conflict_untouched(tmp_path, kwargs, log):
	repo = tmp_path / "repo"
	repo.mkdir()
	_host_only_conflict(repo)
	result, runtime, github_env, _ = _run_take_base(tmp_path, repo, **kwargs)
	assert result.returncode == 0, result.stdout + result.stderr
	assert log in result.stdout and "block-fell-through" in result.stdout
	assert not (runtime / "host_only_conflict_scope.tsv").exists()
	assert _unmerged(repo) == [HOOK]
	assert not github_env.exists()


FAKE_GH_SCRIPT = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
endpoint=""
for a in "$@"; do case "$a" in repos/*) endpoint="$a" ;; body=@*) cat "${a#body=@}" >> "$GH_CALLS.bodies" ;; esac; done
if [ "$endpoint" = "repos/o/r/issues" ]; then
  [ -z "${FAIL_CREATE:-}" ] || exit 1
  labels=""
  for a in "$@"; do case "$a" in labels\\[\\]=*) labels="${a#labels[]=}" ;; esac; done
  printf '901\\t%s\\n' "$labels"
fi
exit 0
"""


def _run_reissue_guard(tmp_path, *, linked=None, assoc="MEMBER", fail_create=False, comments="[]", scope=None):
	(tmp_path / ".claude/hooks").mkdir(parents=True)
	(tmp_path / HOOK).write_text("x = 1\n")
	paths = tmp_path / "paths"
	paths.write_text(HOOK + "\n")
	(tmp_path / "host_only_conflict_scope.tsv").write_text(scope if scope is not None else f"in_scope\t{HOOK}\n")
	payload = tmp_path / "pr.json"
	payload.write_text(json.dumps({"title": "Edit the hook", "author_association": assoc, "user": {"login": "alice"}, "head": {"repo": {"full_name": "o/r"}, "sha": "a" * 40}}))
	linked_file = tmp_path / "linked_issues_raw.json"
	linked_file.write_text(json.dumps(linked if linked is not None else [{"number": 31, "title": "t", "body": ALLOWLIST_BODY, "labels": []}]))
	(tmp_path / "linked_issues_raw.json.status").write_text("ok\n")
	comments_file = tmp_path / "pr_comments.json"
	comments_file.write_text(comments)
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(FAKE_GH_SCRIPT)
	(bin_dir / "gh").chmod(0o755)
	github_env = tmp_path / "github_env"
	src = _source()
	guard = src[src.index('# Reject unsupported conflict paths for both engines'):src.index('_resolver_sandbox_opencode_attempt()')]
	helpers = "".join(_named_function(name) for name in (
		"_resolver_host_only_scope_requires_reissue", "_resolver_run_unblock_ops", "_resolver_reissue_for_host_only_scope"))
	env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_CALLS": str(tmp_path / "gh_calls")}
	for name in ("BASH_ENV", "ENV", "TG_BOT_SECRET", "LINKED_ISSUES_RAW_FILE"):
		env.pop(name, None)
	if fail_create:
		env["FAIL_CREATE"] = "1"
	result = subprocess.run(["bash", "-c", f'''set -euo pipefail
RUNTIME_DIR={str(tmp_path)!r}
SUPPORT_SCRIPTS_DIR={str(SCRIPT.parent)!r}
CONFLICTED_PATHS_FILE={str(paths)!r}
RESOLVER_MODEL_PATHS_FILE={str(paths)!r}
RESOLVER_CHECK_PATHS_STDERR_FILE={str(tmp_path / "check_paths_stderr")!r}
GITHUB_ENV={str(github_env)!r}
GITHUB_REPOSITORY=o/r
PR_NUMBER=7
BASE_BRANCH=main
PR_PAYLOAD_FILE={str(payload)!r}
PR_ISSUE_COMMENTS_FILE={str(comments_file)!r}
IS_INTEGRATION_SYNC=false
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ :; }}
{_failure_helper()}
{helpers}
{_path_failure_helper()}
{_report_helper()}
{guard}
echo guard-passed
'''], cwd=tmp_path, env=env, capture_output=True, text=True)
	calls_file = tmp_path / "gh_calls"
	calls = calls_file.read_text().splitlines() if calls_file.exists() else []
	return result, calls, (github_env.read_text() if github_env.exists() else "")


needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="unblock_run_ops and the re-issue context need jq (present on CI runners)")


def _call_index(calls, needle):
	return next(i for i, call in enumerate(calls) if needle in call)


@needs_jq
def test_declared_host_only_path_closes_and_reissues_the_pr(tmp_path):
	result, calls, env_text = _run_reissue_guard(tmp_path)
	assert result.returncode == 1 and "guard-passed" not in result.stdout
	assert f"PR closed and re-issued as #901: {HOOK}" in result.stderr
	assert "reason=sandbox_path_host_only" in result.stderr
	assert "outcome=reissued" in result.stdout
	assert "AUTOFIX_FAILURE_REASON=conflict_resolver_sandbox_path_host_only" in env_text
	create = _call_index(calls, "api repos/o/r/issues -f title=Re-issue of #7: Edit the hook")
	comment = _call_index(calls, "api repos/o/r/issues/7/comments")
	close = _call_index(calls, "-X PATCH repos/o/r/pulls/7 -f state=closed")
	source_close = _call_index(calls, "-X PATCH repos/o/r/issues/31")
	assert create < comment < close < source_close
	assert "ai:unblock-provenance:v1 source_pr=7" in "\n".join(calls)
	bodies = (tmp_path / "gh_calls.bodies").read_text()
	assert f"<!-- ai:host-only-conflict-reissue:v1 pr=7 issue=901 head=" in bodies and "as #901" in bodies


@needs_jq
def test_reissue_create_failure_never_closes(tmp_path):
	result, calls, _ = _run_reissue_guard(tmp_path, fail_create=True)
	assert result.returncode == 1
	assert "re-issue failed, manual merge needed" in result.stderr
	assert "outcome=create_failed" in result.stdout
	assert not any("PATCH" in call for call in calls)


def test_existing_reissue_marker_prevents_a_second_issue(tmp_path):
	comments = json.dumps([{"body": "x\n<!-- ai:host-only-conflict-reissue:v1 pr=7 issue=900 head=abc -->", "author_association": "OWNER"}])
	result, calls, _ = _run_reissue_guard(tmp_path, comments=comments)
	assert result.returncode == 1 and "reason=already_reissued" in result.stdout
	assert calls == []


@needs_jq
def test_untrusted_reissue_marker_does_not_suppress_the_reissue(tmp_path):
	comments = json.dumps([{"body": "<!-- ai:host-only-conflict-reissue:v1 pr=7 issue=900 head=abc -->", "author_association": "NONE"}])
	result, calls, _ = _run_reissue_guard(tmp_path, comments=comments)
	assert "reason=already_reissued" not in result.stdout
	assert "outcome=reissued" in result.stdout, result.stdout + result.stderr


@needs_jq
def test_untrusted_pr_is_closed_without_an_issue(tmp_path):
	result, calls, _ = _run_reissue_guard(tmp_path, assoc="CONTRIBUTOR")
	assert result.returncode == 1 and "outcome=untrusted_closed" in result.stdout
	assert not any(call.startswith("api repos/o/r/issues -f title=") for call in calls)
	assert any("-X PATCH repos/o/r/pulls/7 -f state=closed" in call for call in calls)


@needs_jq
def test_orchestrator_managed_source_issue_is_only_annotated(tmp_path):
	linked = [{"number": 31, "title": "t", "body": ALLOWLIST_BODY, "labels": ["ai:orchestrator-managed"]}]
	result, calls, _ = _run_reissue_guard(tmp_path, linked=linked)
	assert "outcome=reissued" in result.stdout, result.stdout + result.stderr
	assert any("repos/o/r/issues/31/comments" in call for call in calls)
	assert not any("PATCH repos/o/r/issues/31" in call for call in calls)


def test_unclassified_scope_keeps_the_manual_merge_failure(tmp_path):
	result, calls, _ = _run_reissue_guard(tmp_path, scope="")
	assert result.returncode == 1
	assert f"need a manual merge: {HOOK}" in result.stderr
	assert calls == []


def _paired_and_path_guard() -> str:
	src = _source()
	paired = src[src.index("# Paired live/template copies (issue #6595)."):src.index("# Copy each resolved template over its live copy after a model attempt.")]
	return paired + _guard()


def _real_conflict(tmp_path, name):
	git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_") and key not in {"BASH_ENV", "ENV"}}

	def git(*args, check=True):
		return subprocess.run(["git", *args], cwd=tmp_path, env=git_env, check=check, capture_output=True, text=True)

	git("init", "-q", "-b", "main")
	git("config", "user.name", "t")
	git("config", "user.email", "t@t")
	git("config", "commit.gpgsign", "false")
	target = tmp_path / name
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text('{"a": 1}\n', encoding="utf-8")
	git("add", "-A")
	git("commit", "-qm", "base")
	git("checkout", "-q", "-b", "pr")
	target.write_text('{"a": 2}\n', encoding="utf-8")
	git("commit", "-qam", "pr")
	git("checkout", "-q", "main")
	target.write_text('{"a": 3}\n', encoding="utf-8")
	git("commit", "-qam", "main")
	git("checkout", "-q", "pr")
	assert git("merge", "--no-edit", "main", check=False).returncode != 0
	return git("diff", "--name-only", "--diff-filter=U").stdout.splitlines(), git_env


def _run_consumer_guard(tmp_path, conflicted, git_env, source_repo="false"):
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	allowlist = runtime / "resolver_unmerged_allowlist.txt"
	allowlist.write_text("".join(name + "\n" for name in conflicted), encoding="utf-8")
	github_env = runtime / "github_env"
	result = subprocess.run(["bash", "-c", f'''set -euo pipefail
RUNTIME_DIR={str(runtime)!r}
SUPPORT_SCRIPTS_DIR={str(SCRIPT.parent)!r}
CONFLICTED_PATHS_FILE="${{RUNTIME_DIR}}/conflicted_paths.txt"
RESOLVER_ALLOWLIST_FILE={str(allowlist)!r}
GITHUB_ENV={str(github_env)!r}
IS_WORKFLOW_SOURCE_REPO={source_repo}
IS_INTEGRATION_SYNC=false
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ printf '%s\\n' "$RESOLVER_ISOLATION_FAILURE_REASON" > "$RUNTIME_DIR/persisted_reason"; }}
{_failure_helper()}
{_path_failure_helper()}
{_report_helper()}
{_paired_and_path_guard()}
echo guard-passed
'''], cwd=tmp_path, env={**git_env, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
	return result, runtime


def test_consumer_conflict_reaches_the_model_without_the_source_repo_conflict_set(tmp_path):
	"""drhyg_ecommerce_automation PR #66 (run 38035623653): review_conflict_prepare.sh
	writes conflicted_paths.txt only on the workflow source repo, so every consumer
	conflict failed closed with sandbox_path_unsupported before any model ran."""
	conflicted, git_env = _real_conflict(tmp_path, "src/config.json")
	assert conflicted == ["src/config.json"]
	result, runtime = _run_consumer_guard(tmp_path, conflicted, git_env)
	assert result.returncode == 0, result.stderr
	assert "guard-passed" in result.stdout
	assert "sandbox_path_unsupported" not in result.stderr
	assert not (runtime / "conflicted_paths.txt").exists()
	assert (runtime / "resolver_model_paths.txt").read_text(encoding="utf-8") == "src/config.json\n"


def test_consumer_host_only_conflict_names_the_path(tmp_path):
	"""The PR #66 conflict itself: .claude/settings.json is host-only, so it needs a
	human merge and the failure must say so instead of the nameless reason."""
	conflicted, git_env = _real_conflict(tmp_path, ".claude/settings.json")
	result, runtime = _run_consumer_guard(tmp_path, conflicted, git_env)
	assert result.returncode == 1 and "guard-passed" not in result.stdout
	assert "::error::Conflict resolver: host-only conflicted path(s) need a manual merge: .claude/settings.json" in result.stderr
	assert "reason=sandbox_path_host_only" in result.stderr and "sandbox_path_unsupported" not in result.stderr
	assert (runtime / "persisted_reason").read_text().strip() == "sandbox_path_host_only"


def test_source_repo_without_its_conflict_set_still_fails_closed(tmp_path):
	conflicted, git_env = _real_conflict(tmp_path, "src/config.json")
	result, runtime = _run_consumer_guard(tmp_path, conflicted, git_env, source_repo="true")
	assert result.returncode == 1 and "guard-passed" not in result.stdout
	assert "Conflict resolver paired live copy check failed" in result.stderr
	assert "reason=sandbox_path_unsupported" in result.stderr


# --- pre-push affected-test gate (issue #8201, plan P4) --------------------------------

CHECK_RESOLVER_DIFF = SCRIPT.parent / "check_resolver_diff.sh"


def _gate_function_body() -> str:
	text = SCRIPT.read_text(encoding="utf-8")
	start = text.index("_resolver_prepush_test_gate()\n{")
	return text[start:text.index("\n}\n", start)]


def test_resolver_gate_runs_after_every_guard_and_before_the_commit() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	commit = text.index('git commit -m "[ai-merge-resolve] resolve merge conflicts"')
	call = text.index("_resolver_prepush_test_gate || true")
	assert call < commit
	# Every existing guard runs first; the commit follows whatever the gate says.
	assert text.rindex('"${SUPPORT_SCRIPTS_DIR}/check_resolver_diff.sh" \\', 0, call) < call
	assert text.rindex("verify_resolver_index_complete_or_fail", 0, call) < call
	assert text.rindex("git diff --cached --quiet", 0, call) < call
	assert text[call:commit].count("\n") <= 2


def test_resolver_gate_repair_contract() -> None:
	body = _gate_function_body()
	assert "review_autofix_step_prepush_test_gate.sh" in body
	assert 'gate --phase resolver --worktree "${PWD}" --base "${gate_base}"' in body
	assert "prepare-ephemeral codex" in body and "cleanup" in body
	# One extra sandbox attempt: Claude, then OpenCode only on exit 75.
	# Trusted support only, and the conflicted set (not the merge diff) seeds the allow-list.
	assert "${SUPPORT_SCRIPTS_DIR:-scripts}" not in body
	assert 'git diff --name-only HEAD -- > "${gate_dir}/changed.txt"' not in body
	assert '"${CONFLICTED_PATHS_FILE:-/nonexistent}" > "${gate_dir}/changed.txt"' in body
	assert body.count("_resolver_sandbox_attempt claude") == 1
	# The repair attempt's prepare-ephemeral is bounded to the 630 s reserved.
	assert "local resolver_gate_prepare_bounded=true" in body
	attempt = SCRIPT.read_text(encoding="utf-8").split("_resolver_sandbox_attempt()\n{", 1)[1].split("\n}\n", 1)[0]
	assert '[ "${resolver_gate_prepare_bounded:-}" != true ] || sandbox_attempt_prepare=(timeout --kill-after=30s 600' in attempt
	assert body.count("_resolver_sandbox_attempt codex") == 2
	assert "--extra-allowed-file" in body
	assert '--conflicted-set "${CONFLICTED_PATHS_FILE}"' in body
	assert "_verify_fingerprints_soft" in body and 'RESOLVER_FP_EXIT:-0}" -eq 1' in body
	# Reverts go back to the merge index, never to HEAD.
	assert 'gate_index_tree="$(git write-tree' in body and 'git read-tree "${gate_index_tree}"' in body
	assert "--source=HEAD" not in body
	# Fail-open: the gate never exits the resolver.
	assert not re.search(r"(^|[;&|]\s*)\s*exit\b", body, re.MULTILINE)
	assert body.rstrip().endswith("return 0")


def _resolver_diff_repo(tmp_path: Path) -> Path:
	repo = tmp_path / "repo"
	(repo / "tests").mkdir(parents=True)
	(repo / "a.py").write_text("x = 1\n", encoding="utf-8")
	(repo / "tests" / "test_a.py").write_text("y = 1\n", encoding="utf-8")
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
	(tmp_path / "conflicted.txt").write_text("a.py\n", encoding="utf-8")
	(tmp_path / "touched.txt").write_text("a.py\ntests/test_a.py\n", encoding="utf-8")
	return repo


def _run_check(tmp_path: Path, repo: Path, *extra: str) -> subprocess.CompletedProcess:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	return subprocess.run(["bash", str(CHECK_RESOLVER_DIFF), "--conflicted-set", str(tmp_path / "conflicted.txt"),
		"--touched-set", str(tmp_path / "touched.txt"), "--repo-root", str(repo), *extra],
		capture_output=True, text=True, env=env, timeout=60)


def test_check_resolver_diff_without_extra_paths_is_unchanged(tmp_path: Path) -> None:
	repo = _resolver_diff_repo(tmp_path)
	proc = _run_check(tmp_path, repo)
	assert proc.returncode == 1
	assert "edited files outside the conflicted set" in proc.stderr and "tests/test_a.py" in proc.stderr


def test_check_resolver_diff_extra_allowed_file_passes_the_subset_check(tmp_path: Path) -> None:
	repo = _resolver_diff_repo(tmp_path)
	proc = _run_check(tmp_path, repo, "--extra-allowed-file", "tests/test_a.py")
	assert proc.returncode == 0, proc.stderr
	assert "check_resolver_diff: OK" in proc.stdout


def test_check_resolver_diff_extra_allowed_file_without_value_is_a_usage_error(tmp_path: Path) -> None:
	repo = _resolver_diff_repo(tmp_path)
	proc = _run_check(tmp_path, repo, "--extra-allowed-file")
	assert proc.returncode == 2
	assert "--extra-allowed-file needs a value" in proc.stderr


@pytest.mark.parametrize("value", ["/abs/tests/test_a.py", "../tests/test_a.py", "tests/../tests/test_a.py", "tests/test\x01a.py"])
def test_check_resolver_diff_ignores_unsafe_extra_paths(tmp_path: Path, value: str) -> None:
	repo = _resolver_diff_repo(tmp_path)
	proc = _run_check(tmp_path, repo, "--extra-allowed-file", value)
	assert proc.returncode == 1
	assert "ignoring unsafe --extra-allowed-file value" in proc.stderr


_GATE_DOCKER = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "__WORK__/docker.log"
if [ "$1" = run ]; then
	for arg in "$@"; do
		case "$arg" in
			type=bind,src=*,dst=/results) results="${arg#type=bind,src=}"; results="${results%,dst=/results}" ;;
		esac
	done
	n=$(( $(cat "__WORK__/count" 2>/dev/null || echo 0) + 1 ))
	echo "$n" > "__WORK__/count"
	if [ "$n" = 1 ]; then cp "__WORK__/fail.json" "$results/result.json"; else cp "__WORK__/pass.json" "$results/result.json"; fi
fi
exit 0
'''


def _gate_result(status: str, failures: list) -> str:
	return json.dumps({"schema": "prepush_test_gate.v1", "status": status, "ran": ["tests/test_a.py"], "failures": failures,
		"inherited": [], "unverified": [], "changed_assertions": [], "selected": 1, "elapsed_secs": 1})


def _run_resolver_gate_fixture(tmp_path: Path, repair_edit: str, start_seconds: int = 0, full_prepare: bool = True) -> tuple:
	root = SCRIPT.parent.parent
	work = tmp_path / "work"
	work.mkdir()
	failure = [{"test": "tests/test_a.py", "file": "tests/test_a.py", "names": ["test_x"], "tail": "AssertionError"}]
	(work / "fail.json").write_text(_gate_result("fail", failure), encoding="utf-8")
	(work / "pass.json").write_text(_gate_result("pass", []), encoding="utf-8")
	repo = tmp_path / "repo"
	(repo / "tests").mkdir(parents=True)
	(repo / "a.py").write_text("x = 1\n", encoding="utf-8")
	(repo / "tests" / "test_a.py").write_text("# covers a.py\nimport a\n", encoding="utf-8")
	(repo / "outside.py").write_text("keep\n", encoding="utf-8")
	env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "PREPUSH_", "AI_ENGINE_"))}
	git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
	subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
	subprocess.run([*git, "add", "-A"], cwd=repo, check=True, env=env)
	subprocess.run([*git, "commit", "-q", "-m", "base"], cwd=repo, check=True, env=env)
	(repo / "a.py").write_text("x = 2\n", encoding="utf-8")
	(repo / "base_side.py").write_text("from base\n", encoding="utf-8")
	subprocess.run(["git", "add", "a.py", "base_side.py"], cwd=repo, check=True, env=env)
	support = tmp_path / "support"
	support.mkdir()
	for name in ("review_autofix_step_prepush_test_gate.sh", "prepush_test_gate_run.sh", "review_untrusted_workspace.py", "check_resolver_diff.sh"):
		shutil.copy2(root / "scripts" / name, support / name)
	shutil.copy2(root / "workflow-templates/.claude/hooks/prepush_affected_tests.py", support / "prepush_affected_tests.py")
	prompts = tmp_path / "prompts"
	prompts.mkdir()
	shutil.copy2(root / "prompts/mode-implement-repair-tests.txt", prompts / "mode-implement-repair-tests.txt")
	sandbox_root = tmp_path / "review-isolated-gate"
	sandbox_root.mkdir()
	prepare_branch = (
		f'prepare) printf "sha256:{"c" * 64}\\n" > "{sandbox_root}/image"; mkdir -p "{sandbox_root}/source/.review-venv/bin"; '
		f'printf "REVIEW_SANDBOX_ROOT=%s\\n" "{sandbox_root}" >> "$GITHUB_ENV" ;; '
	) if full_prepare else 'prepare) exit 1 ;; '
	(support / "review_untrusted_sandbox.sh").write_text(
		f'#!/usr/bin/env bash\necho "$*" >> "{work}/sandbox.log"\ncase "$1" in {prepare_branch}'
		f'prepare-ephemeral) printf "sha256:{"c" * 64}\\n" > "{sandbox_root}/image"; echo "{sandbox_root}" ;; esac\nexit 0\n',
		encoding="utf-8")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	(bin_dir / "docker").write_text(_GATE_DOCKER.replace("__WORK__", str(work)), encoding="utf-8")
	(bin_dir / "docker").chmod(0o755)
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	conflicted = runtime / "conflicted_paths.txt"
	conflicted.write_text("a.py\n", encoding="utf-8")
	text = SCRIPT.read_text(encoding="utf-8")
	start = text.index("_resolver_prepush_test_gate()\n{")
	function = text[start:text.index("\n}\n", start) + 3]
	harness = f'''set -euo pipefail
_resolver_sandbox_attempt() {{ echo "attempt $1" >> "{work}/attempts"; cp "${{_effective_prompt_file}}" "{work}/prompt"; {repair_edit}; return 0; }}
_verify_fingerprints_soft() {{ RESOLVER_FP_EXIT=0; }}
{function}
SECONDS={start_seconds}
_resolver_prepush_test_gate
git commit -q -m "[ai-merge-resolve] resolve merge conflicts"
echo COMMITTED
'''
	env.update(PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}", SUPPORT_SCRIPTS_DIR=str(support), SUPPORT_PROMPTS_DIR=str(prompts),
		RUNTIME_DIR=str(runtime), RUNNER_TEMP=str(tmp_path), CONFLICTED_PATHS_FILE=str(conflicted), GITHUB_WORKSPACE=str(repo),
		GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
	proc = subprocess.run(["bash", "-c", harness], cwd=repo, env=env, capture_output=True, text=True, timeout=180)
	assert "COMMITTED" in proc.stdout, proc.stderr
	return proc, work, repo, env


@pytest.mark.parametrize("repair_edit, expect_kept", [
	("printf 'fixed\\n' > tests/test_a.py", True),
	("printf 'fixed\\n' > tests/test_a.py; printf 'bad\\n' > outside.py", False),
	# A non-conflicted file the merge brought in stays outside the extra
	# allowed paths, even though it differs from the pre-merge head.
	("printf 'fixed\\n' > tests/test_a.py; printf 'rewritten\\n' > base_side.py", False),
])
def test_resolver_gate_fixture_runs_one_repair_and_keeps_or_reverts_it(tmp_path: Path, repair_edit: str, expect_kept: bool) -> None:
	proc, work, repo, env = _run_resolver_gate_fixture(tmp_path, repair_edit)
	assert (work / "attempts").read_text(encoding="utf-8") == "attempt codex\n"
	assert "=== CAPTURED TEST FAILURES (UNTRUSTED) ===" in (work / "prompt").read_text(encoding="utf-8")
	committed = subprocess.run(["git", "show", "HEAD:tests/test_a.py"], cwd=repo, capture_output=True, text=True, env=env).stdout
	assert subprocess.run(["git", "show", "HEAD:a.py"], cwd=repo, capture_output=True, text=True, env=env).stdout == "x = 2\n"
	assert (repo / "outside.py").read_text(encoding="utf-8") == "keep\n"
	assert subprocess.run(["git", "show", "HEAD:base_side.py"], cwd=repo, capture_output=True, text=True, env=env).stdout == "from base\n"
	if expect_kept:
		assert committed == "fixed\n"
		assert "phase=resolver outcome=pass" in proc.stderr and "repair=ran" in proc.stderr
		assert (work / "count").read_text(encoding="utf-8").strip() == "2"
	else:
		assert committed == "# covers a.py\nimport a\n"
		assert "outcome=repair_reverted reason=check_resolver_diff" in proc.stderr


def test_resolver_gate_skips_the_repair_when_the_step_budget_is_short(tmp_path: Path) -> None:
	# 5000s into the step: run1 still fits, a 3000s repair attempt plus a gate
	# run does not. The commit still lands.
	proc, work, repo, env = _run_resolver_gate_fixture(tmp_path, "printf 'fixed\\n' > tests/test_a.py", start_seconds=5000)
	assert not (work / "attempts").exists()
	assert (work / "count").read_text(encoding="utf-8").strip() == "1"
	assert "phase=resolver outcome=fail" in proc.stderr and "repair=skipped_step_time_budget" in proc.stderr
	assert subprocess.run(["git", "show", "HEAD:a.py"], cwd=repo, capture_output=True, text=True, env=env).stdout == "x = 2\n"


def test_resolver_gate_is_skipped_when_no_gate_run_fits_the_step_budget(tmp_path: Path) -> None:
	proc, work, repo, env = _run_resolver_gate_fixture(tmp_path, "printf 'fixed\\n' > tests/test_a.py", start_seconds=8500)
	assert not (work / "count").exists() and not (work / "attempts").exists()
	assert "PREPUSH_TEST_GATE phase=resolver outcome=skipped reason=step_budget" in proc.stderr
	assert subprocess.run(["git", "show", "HEAD:a.py"], cwd=repo, capture_output=True, text=True, env=env).stdout == "x = 2\n"


# ── --test-repair (#8210, plan phase P5) ─────────────────────────────

_REPO_ROOT = SCRIPT.parent.parent


def _test_repair_functions() -> str:
	src = _source()
	start = src.index("# ── Integration post-sync test repair (#8210")
	end = src.index('if [ -n "${RESOLVER_TEST_REPAIR_RESULT_FILE}" ]; then', start)
	return src[start:end]


def _tr_git(repo: Path, *args: str) -> str:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def _test_repair_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
	repo = tmp_path / "repo"
	repo.mkdir()
	_tr_git(repo, "init", "--quiet", "-b", "main")
	(repo / "tests").mkdir()
	(repo / "lib.py").write_text("def value():\n\treturn 1\n", encoding="utf-8")
	(repo / "other.py").write_text("X = 1\n", encoding="utf-8")
	(repo / "tests" / "test_lib.py").write_text("from lib import value\n\n\ndef test_value():\n\tassert value() == 1\n", encoding="utf-8")
	_tr_git(repo, "add", "-A")
	_tr_git(repo, "commit", "--quiet", "-m", "base")
	_tr_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
	_tr_git(repo, "checkout", "--quiet", "-b", "orchestrator/project-1")
	(repo / "lib.py").write_text("def value():\n\treturn 2\n", encoding="utf-8")
	_tr_git(repo, "commit", "--quiet", "-am", "sync broke the test")
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	result = runtime / "result.json"
	result.write_text(json.dumps({
		"schema": "prepush_test_gate.v1", "status": "fail", "ran": ["tests/test_lib.py"],
		"failures": [{"test": "tests/test_lib.py", "file": "tests/test_lib.py", "names": ["test_value"], "tail": "assert 2 == 1"}],
		"inherited": [], "unverified": [], "changed_assertions": [], "selected": 1, "elapsed_secs": 1,
	}), encoding="utf-8")
	return repo, runtime, result


def _run_test_repair(tmp_path: Path, mode: str, result_override: str | None = None, fp_json: str = "") -> tuple[subprocess.CompletedProcess, Path, Path]:
	repo, runtime, result = _test_repair_fixture(tmp_path)
	github_env = tmp_path / "github_env"
	github_env.touch()
	calls = tmp_path / "calls"
	program = _test_repair_functions() + """
_resolver_test_repair_context() {
  _test_repair_title="t" _test_repair_body="b" _test_repair_merged_list="none" _test_repair_merged_count=0
  INTEGRATION_FINGERPRINTS_FILE="${RUNTIME_DIR}/fp.json"
  if [ -n "${FP_JSON:-}" ]; then printf '%s' "${FP_JSON}"; else printf '{}'; fi > "${INTEGRATION_FINGERPRINTS_FILE}"
}
_resolver_sandbox_attempt() {
  printf 'sandbox-attempt %s prompt=%s\\n' "$1" "${_effective_prompt_file}" >> "$CALLS"
  case "${MODE}" in
    fix) printf 'def value():\\n\\treturn 1\\n' > lib.py ;;
    stray) printf 'def value():\\n\\treturn 1\\n' > lib.py; printf 'X = 2\\n' > other.py ;;
    fail) printf 'def value():\\n\\treturn 1\\n' > lib.py; return 1 ;;
  esac
  return 0
}
_resolver_test_repair_main
"""
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("BASH_ENV", "ENV")}
	env.update({
		"SUPPORT_SCRIPTS_DIR": str(_REPO_ROOT / "scripts"), "SUPPORT_PROMPTS_DIR": str(_REPO_ROOT / "prompts"),
		"RUNTIME_DIR": str(runtime), "GITHUB_ENV": str(github_env), "TARGET_BRANCH": "orchestrator/project-1",
		"BASE_BRANCH": "main", "MODE": mode, "CALLS": str(calls), "GITHUB_WORKSPACE": str(tmp_path / "none"),
		"RESOLVER_TEST_REPAIR_RESULT_FILE": result_override if result_override is not None else str(result),
		"RESOLVER_TEST_REPAIR_BUDGET_SECS": "7000", "PREPUSH_TEST_GATE_MODE": "repair",
		"FP_JSON": fp_json,
	})
	proc = subprocess.run(["bash", "-c", "set -euo pipefail\n" + program], cwd=repo, env=env, capture_output=True, text=True)
	return proc, repo, github_env


def test_test_repair_commits_one_repair_on_the_integration_head(tmp_path):
	proc, repo, github_env = _run_test_repair(tmp_path, "fix")
	assert proc.returncode == 0, proc.stderr
	assert "outcome=committed" in proc.stderr, proc.stderr
	log = _tr_git(repo, "log", "--format=%s", "-3").splitlines()
	assert log[0] == "[ai-merge-resolve] repair post-sync test failures"
	assert log.count("[ai-merge-resolve] repair post-sync test failures") == 1
	assert "INTEGRATION_TEST_REPAIRED=true" in github_env.read_text(encoding="utf-8")
	prompt = (tmp_path / "runtime" / "integration_test_repair" / "repair-prompt.txt").read_text(encoding="utf-8")
	assert "Repair test failures on an orchestrator integration branch" in prompt
	assert "=== CAPTURED TEST FAILURES (UNTRUSTED) ===" in prompt
	assert "orchestrator/project-1" in prompt and "{{" not in prompt.split("=== CAPTURED")[0]
	assert "lib.py" in (tmp_path / "runtime" / "integration_test_repair" / "allow.txt").read_text(encoding="utf-8")


def test_test_repair_prompt_carries_the_intent_fingerprints(tmp_path):
	# The context helper sets INTEGRATION_FINGERPRINTS_FILE as a plain shell
	# variable; the renderer must still receive it, or the model gets "{}".
	fp = '{"11":{"must_contain":[{"file":"lib.py","pattern":"keep_me_marker"}]}}'
	proc, _repo, _env = _run_test_repair(tmp_path, "none", fp_json=fp)
	assert proc.returncode == 0, proc.stderr
	assert "outcome=no_edits" in proc.stderr, proc.stderr
	rendered = (tmp_path / "runtime" / "integration_test_repair" / "rendered-template.txt").read_text(encoding="utf-8")
	assert "keep_me_marker" in rendered and "{{INTENT_FINGERPRINTS_JSON}}" not in rendered


def test_test_repair_commits_despite_fingerprint_drift_already_on_the_head(tmp_path):
	# The pattern is missing before the model runs (a sync can do this): the
	# baseline compare lets the repair through.
	fp = '{"11":{"issue":11,"pr":12,"must_contain":[{"file":"lib.py","regex":"keep_me_marker"}]}}'
	proc, repo, github_env = _run_test_repair(tmp_path, "fix", fp_json=fp)
	assert proc.returncode == 0, proc.stderr
	assert "outcome=committed" in proc.stderr, proc.stderr
	assert "INTEGRATION_TEST_REPAIRED=true" in github_env.read_text(encoding="utf-8")


def test_test_repair_reverts_a_repair_that_adds_fingerprint_drift(tmp_path):
	# "return 2" is present before the repair and removed by it.
	fp = '{"11":{"issue":11,"pr":12,"must_contain":[{"file":"lib.py","regex":"return 2"}]}}'
	proc, repo, github_env = _run_test_repair(tmp_path, "fix", fp_json=fp)
	assert proc.returncode == 0, proc.stderr
	assert "outcome=repair_reverted reason=fingerprints" in proc.stderr, proc.stderr
	assert _tr_git(repo, "log", "--format=%s", "-1").strip() == "sync broke the test"
	assert "INTEGRATION_TEST_REPAIRED" not in github_env.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode,reason", [("stray", "check_resolver_diff"), ("fail", "editor_exit_1")])
def test_test_repair_reverts_rejected_repairs_without_committing(tmp_path, mode, reason):
	proc, repo, github_env = _run_test_repair(tmp_path, mode)
	assert proc.returncode == 0, proc.stderr
	assert f"outcome=repair_reverted reason={reason}" in proc.stderr, proc.stderr
	assert _tr_git(repo, "log", "--format=%s", "-1").strip() == "sync broke the test"
	assert _tr_git(repo, "status", "--porcelain").strip() == ""
	assert "INTEGRATION_TEST_REPAIRED" not in github_env.read_text(encoding="utf-8")


def test_test_repair_refuses_a_symlinked_result_without_a_model_run(tmp_path):
	link = tmp_path / "runtime-link.json"
	proc, _repo, _env = _run_test_repair(tmp_path, "fix", result_override="")
	assert "reason=invalid_input" in proc.stderr
	(tmp_path / "runtime" / "linked.json").symlink_to(tmp_path / "runtime" / "result.json")
	env_override = str(tmp_path / "runtime" / "linked.json")
	program = _test_repair_functions() + '\n_resolver_sandbox_attempt() { echo model >> "$CALLS"; }\n_resolver_test_repair_main\n'
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("BASH_ENV", "ENV")}
	env.update({"RUNTIME_DIR": str(tmp_path / "runtime"), "TARGET_BRANCH": "orchestrator/project-1",
		"RESOLVER_TEST_REPAIR_RESULT_FILE": env_override, "CALLS": str(link)})
	proc = subprocess.run(["bash", "-c", program], cwd=tmp_path / "repo", env=env, capture_output=True, text=True)
	assert "reason=invalid_input detail=result_file" in proc.stderr
	assert not link.exists()
	assert not (tmp_path / "calls").exists()


def test_test_repair_rejects_unknown_arguments_and_skips_the_conflict_path():
	proc = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True)
	assert proc.returncode == 2 and "unknown argument" in proc.stderr
	proc = subprocess.run(["bash", str(SCRIPT), "--test-repair"], capture_output=True, text=True)
	assert proc.returncode == 2 and "needs a result file" in proc.stderr
	src = _source()
	branch = src[src.index('if [ -n "${RESOLVER_TEST_REPAIR_RESULT_FILE}" ]; then'):]
	branch = branch[:branch.index("\nfi\n")]
	# No integration-judge dispatch (EXIT trap) and nothing conflict-specific.
	assert "trap - EXIT" in branch and "_resolver_test_repair_main" in branch and "exit 0" in branch
	assert src.index("_resolver_sandbox_attempt()") < src.index('if [ -n "${RESOLVER_TEST_REPAIR_RESULT_FILE}" ]; then')
	assert src.index('if [ -n "${RESOLVER_TEST_REPAIR_RESULT_FILE}" ]; then') < src.index(': > "${RESOLVER_ATTEMPT_BASE_MISSING_FILE}"')


def _run_test_repair_context(tmp_path: Path, comments: list) -> tuple[subprocess.CompletedProcess, Path]:
	if shutil.which("jq") is None:
		pytest.skip("jq binary not available in test environment")
	runtime = tmp_path / "runtime"
	runtime.mkdir(exist_ok=True)
	comments_file = tmp_path / "comments.json"
	comments_file.write_text(json.dumps(comments), encoding="utf-8")
	program = _test_repair_functions() + """
gh() {
  case "$*" in
    "api user"*) echo bot ;;
    *"--paginate"*) cat "$COMMENTS_FILE" ;;
    *) echo '{"title":"t","body":"b"}' ;;
  esac
}
rc=0
_resolver_test_repair_context 7 || rc=$?
echo "RC=${rc}"
echo "MERGED=${_test_repair_merged_count}"
[ -z "${INTEGRATION_FINGERPRINTS_FILE:-}" ] || echo "FP=$(cat "${INTEGRATION_FINGERPRINTS_FILE}")"
"""
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("BASH_ENV", "ENV")}
	env.update({"SUPPORT_SCRIPTS_DIR": str(_REPO_ROOT / "scripts"), "RUNTIME_DIR": str(runtime),
		"GITHUB_REPOSITORY": "o/r", "COMMENTS_FILE": str(comments_file), "GITHUB_WORKSPACE": str(tmp_path / "none")})
	proc = subprocess.run(["bash", "-c", program], cwd=tmp_path, env=env, capture_output=True, text=True)
	return proc, runtime


def test_test_repair_context_reads_v2_state_before_legacy_v1(tmp_path):
	v2_state = {"waves": [{"issues": [{"id": "a", "github_issue": 11, "status": "merged"}]}],
		"merged_issue_fingerprints": {"11": {"must_contain": [{"file": "lib.py", "pattern": "v2"}]}}}
	v1_state = {"waves": [], "merged_issue_fingerprints": {"9": {"stale": True}}}
	state_file = tmp_path / "state.json"
	state_file.write_text(json.dumps(v2_state), encoding="utf-8")
	chunks = tmp_path / "chunks"
	subprocess.run(["python3", "-I", "-B", str(_REPO_ROOT / "scripts" / "orchestrate_state_v2.py"), "pack",
		"--state-file", str(state_file), "--out-dir", str(chunks)], check=True)
	comments = [{"user": {"login": "bot"}, "body": "ORCHESTRATOR_STATE_V1\n" + json.dumps(v1_state) + "\nORCHESTRATOR_STATE_V1"}]
	comments += [{"user": {"login": "bot"}, "body": p.read_text(encoding="utf-8")} for p in sorted(chunks.iterdir())]
	proc, _runtime = _run_test_repair_context(tmp_path, comments)
	assert "RC=0" in proc.stdout, proc.stdout + proc.stderr
	assert "MERGED=1" in proc.stdout
	assert '"pattern":"v2"' in proc.stdout and "stale" not in proc.stdout


def test_test_repair_context_fails_closed_without_trusted_state(tmp_path):
	# Only another author's state: no trusted state, so no repair (rc 2).
	comments = [{"user": {"login": "someone"}, "body": "ORCHESTRATOR_STATE_V1\n{\"waves\": []}\nORCHESTRATOR_STATE_V1"}]
	proc, _runtime = _run_test_repair_context(tmp_path, comments)
	assert "RC=2" in proc.stdout, proc.stdout + proc.stderr
	src = _source()
	assert 'reason=state_unavailable' in src[src.index("_resolver_test_repair_main()"):]


def test_resolver_gate_full_prepare_passes_the_venv_and_cleans_up(tmp_path: Path) -> None:
	proc, work, repo, env = _run_resolver_gate_fixture(tmp_path, "printf 'fixed\\n' > tests/test_a.py")
	sandbox_log = (work / "sandbox.log").read_text(encoding="utf-8").splitlines()
	assert sandbox_log[0] == "prepare codex" and sandbox_log[-1] == "cleanup"
	assert "prepare-ephemeral codex" not in sandbox_log
	runs = [line for line in (work / "docker.log").read_text(encoding="utf-8").splitlines() if line.startswith("run ")]
	assert len(runs) == 2 and all("dst=/opt/codex-venv,readonly" in line for line in runs)
	assert "reason=venv_absent" not in proc.stderr


def test_resolver_gate_falls_back_to_an_image_only_prepare(tmp_path: Path) -> None:
	proc, work, repo, env = _run_resolver_gate_fixture(tmp_path, "printf 'fixed\\n' > tests/test_a.py", full_prepare=False)
	sandbox_log = (work / "sandbox.log").read_text(encoding="utf-8").splitlines()
	assert sandbox_log[:2] == ["prepare codex", "prepare-ephemeral codex"] and sandbox_log[-1] == "cleanup"
	assert "/opt/codex-venv" not in (work / "docker.log").read_text(encoding="utf-8")
	assert "phase=resolver outcome=warning reason=venv_absent" in proc.stderr
	assert "phase=resolver outcome=pass" in proc.stderr
