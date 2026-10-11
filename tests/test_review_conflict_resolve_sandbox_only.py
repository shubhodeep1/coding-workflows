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
