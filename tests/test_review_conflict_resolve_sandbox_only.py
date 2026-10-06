"""Resolver model launches must stay inside the review sandbox."""

import os
from pathlib import Path
import re
import subprocess

import sys

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
    if [ "${MODE:-}" = transfer_failed ] || [ "${MODE:-}" = transfer_unknown ] || [ "${MODE:-}" = rollback_failed ]; then
      touch "$RUNTIME_DIR/review_sandbox_transfer_failed"
      if [ "${MODE:-}" = rollback_failed ]; then
        echo '::error::Review isolation snapshot or transfer rejected (ValueError) reason=transfer_rollback_failed' > "$RUNTIME_DIR/review_sandbox_transfer_reason_${3##*/}"
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
	("transfer_failed", "codex", 1),
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
	""" + _failure_helper() + "\n" + _helper() + "\n" + _launch() + '\nprintf "exit=%s\\n" "${_codex_exit}"\n'
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
RESOLVER_INITIAL_UNMERGED_PATHS_FILE={str(tmp_path / "resolver_initial_unmerged_paths.txt")!r}
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
	if mode == "outdated":
		assert "reason=sandbox_helper_outdated" in result.stderr


@pytest.mark.parametrize("mode", ["prepare_failed", "outdated", "transfer_failed", "transfer_unknown", "cleanup_failed", "rollback_failed"])
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
		"transfer_unknown": "reason=sandbox_transfer_failed",
		"cleanup_failed": "sandbox cleanup failed",
		"rollback_failed": "reason=transfer_rollback_failed",
	}[mode]
	assert expected_error in result.stderr
	assert (tmp_path / "persisted_reason").read_text().strip() == (
		"sandbox_cleanup_failed" if mode == "cleanup_failed" else expected_error.removeprefix("reason=")
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
RESOLVER_INITIAL_UNMERGED_PATHS_FILE={str(tmp_path / "resolver_initial_unmerged_paths.txt")!r}
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ printf '%s\\n' "$RESOLVER_ISOLATION_FAILURE_REASON" > "$RUNTIME_DIR/persisted_reason"; }}
{_failure_helper()}
{guard}
'''], cwd=tmp_path, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
	assert result.returncode == 1
	assert "reason=sandbox_path_unsupported" in result.stderr
	# Safe path names are logged so the refused path is identifiable (#6596).
	assert "REVIEW_SANDBOX_PATH_REFUSED path=assets/x.svg reason=unsupported_suffix" in result.stderr
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


# --- #6596: paired safety-hook and trusted command conflicts --------------

WORKSPACE_PY = SCRIPT.parent / "review_untrusted_workspace.py"
HOOK_LIVE = ".claude/hooks/pr_merge_status_guard.py"
HOOK_TEMPLATE = "workflow-templates/.claude/hooks/pr_merge_status_guard.py"


def _clean_env(**extra):
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	for inherited in ("BASH_ENV", "ENV", "GITHUB_WORKSPACE"):
		env.pop(inherited, None)
	env.update(PYTHONDONTWRITEBYTECODE="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@invalid",
		GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@invalid", GIT_CONFIG_GLOBAL=os.devnull,
		GIT_CONFIG_NOSYSTEM="1")
	env.update(extra)
	return env


def _git(repo, *args):
	return subprocess.run(["git", *args], cwd=repo, env=_clean_env(), check=True, capture_output=True, text=True).stdout


def _conflict_repo(tmp_path, conflicted, base_extra=None):
	"""Scratch merge with two-sided conflicts on ``conflicted``; returns (repo, paths_file)."""
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	for name, content in {**{n: "base\n" for n in conflicted}, **(base_extra or {})}.items():
		target = repo / name
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(content, encoding="utf-8")
		if name.endswith(".py"):
			target.chmod(0o755)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")
	_git(repo, "checkout", "-q", "-b", "other")
	for name in conflicted:
		(repo / name).write_text("theirs\n", encoding="utf-8")
	_git(repo, "commit", "-qam", "theirs")
	_git(repo, "checkout", "-q", "main")
	for name in conflicted:
		(repo / name).write_text("ours\n", encoding="utf-8")
	_git(repo, "commit", "-qam", "ours")
	merge = subprocess.run(["git", "merge", "-q", "other"], cwd=repo, env=_clean_env(), capture_output=True, text=True)
	assert merge.returncode != 0
	paths = tmp_path / "conflicted_paths.txt"
	paths.write_text(_git(repo, "diff", "--name-only", "--diff-filter=U"), encoding="utf-8")
	(tmp_path / "resolver_initial_unmerged_paths.txt").write_text(paths.read_text(encoding="utf-8"), encoding="utf-8")
	assert sorted(paths.read_text().split()) == sorted(conflicted)
	return repo, paths


def _helper_cmd(cmd, repo, paths, *extra):
	return subprocess.run([sys.executable, str(WORKSPACE_PY), cmd, str(repo), str(paths),
		*([str(repo.parent / "resolver_initial_unmerged_paths.txt")] if cmd == "mirror-safety-hook" else []), *extra],
		env=_clean_env(), capture_output=True, text=True)


def _guard_slice(tmp_path, repo, paths, github_workspace=""):
	src = _source()
	guard = src[src.index('# Reject unsupported conflict paths for both engines'):src.index('_resolver_sandbox_opencode_attempt()')]
	return subprocess.run(["bash", "-c", f'''set -euo pipefail
RUNTIME_DIR={str(tmp_path)!r}
SUPPORT_SCRIPTS_DIR={str(SCRIPT.parent)!r}
CONFLICTED_PATHS_FILE={str(paths)!r}
RESOLVER_INITIAL_UNMERGED_PATHS_FILE={str(tmp_path / "resolver_initial_unmerged_paths.txt")!r}
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ :; }}
{_failure_helper()}
{guard}
echo guard-passed
'''], cwd=repo, env=_clean_env(GITHUB_WORKSPACE=github_workspace), capture_output=True, text=True)


def _parity_helper() -> str:
	import re as _re
	match = _re.search(r"^verify_resolver_safety_hook_parity_or_fail\(\) \{\n.*?\n\}\n", _source(), _re.M | _re.S)
	assert match is not None
	return match.group()


def test_paired_hook_conflict_is_admitted_mirrored_and_staged(tmp_path):
	repo, paths = _conflict_repo(tmp_path, [HOOK_LIVE, HOOK_TEMPLATE])
	guard = _guard_slice(tmp_path, repo, paths)
	assert guard.returncode == 0, guard.stderr
	assert "guard-passed" in guard.stdout
	# The sandbox resolves only the template; simulate its validated transfer.
	(repo / HOOK_TEMPLATE).write_text("resolved = True\n", encoding="utf-8")
	(repo / HOOK_TEMPLATE).chmod(0o755)
	mirror = _helper_cmd("mirror-safety-hook", repo, paths)
	assert mirror.returncode == 0, mirror.stderr
	assert (repo / HOOK_LIVE).read_bytes() == (repo / HOOK_TEMPLATE).read_bytes()
	assert (repo / HOOK_LIVE).stat().st_mode & 0o111
	_git(repo, "add", "--", HOOK_LIVE, HOOK_TEMPLATE)
	staged = {line.split("\t")[1]: line.split()[:2] for line in _git(repo, "ls-files", "-s", "--", HOOK_LIVE, HOOK_TEMPLATE).splitlines()}
	assert staged[HOOK_LIVE] == staged[HOOK_TEMPLATE]
	parity = subprocess.run(["bash", "-c", f'''set -euo pipefail
CONFLICTED_PATHS_FILE={str(paths)!r}
GITHUB_ENV={str(tmp_path / "github_env")!r}
{_parity_helper()}
verify_resolver_safety_hook_parity_or_fail && echo parity-ok
'''], cwd=repo, env=_clean_env(), capture_output=True, text=True)
	assert "parity-ok" in parity.stdout, parity.stdout + parity.stderr


def test_parity_assertion_rejects_divergent_staged_hook(tmp_path):
	repo, paths = _conflict_repo(tmp_path, [HOOK_LIVE, HOOK_TEMPLATE])
	(repo / HOOK_TEMPLATE).write_text("resolved = True\n", encoding="utf-8")
	(repo / HOOK_LIVE).write_text("divergent = True\n", encoding="utf-8")
	_git(repo, "add", "--", HOOK_LIVE, HOOK_TEMPLATE)
	parity = subprocess.run(["bash", "-c", f'''set -euo pipefail
CONFLICTED_PATHS_FILE={str(paths)!r}
GITHUB_ENV={str(tmp_path / "github_env")!r}
{_parity_helper()}
verify_resolver_safety_hook_parity_or_fail || echo parity-failed
'''], cwd=repo, env=_clean_env(), capture_output=True, text=True)
	assert "parity-failed" in parity.stdout
	assert "CONFLICT_RESOLVED=false" in (tmp_path / "github_env").read_text()


def test_live_hook_never_enters_sandbox_snapshot(tmp_path):
	import importlib.util
	repo, _ = _conflict_repo(tmp_path, [HOOK_LIVE, HOOK_TEMPLATE])
	spec = importlib.util.spec_from_file_location("review_untrusted_workspace_6596", WORKSPACE_PY)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	assert module.allowed(HOOK_LIVE) is False
	workspace = tmp_path / "ws"
	workspace.mkdir()
	snap = subprocess.run([sys.executable, str(WORKSPACE_PY), "snapshot", str(repo), str(workspace), str(tmp_path / "manifest.json")],
		env=_clean_env(), capture_output=True, text=True)
	assert snap.returncode == 0, snap.stderr
	assert (workspace / HOOK_TEMPLATE).exists()
	assert not (workspace / HOOK_LIVE).exists()


def test_command_with_trusted_twin_is_admitted(tmp_path):
	command = ".claude/commands/x.md"
	twin = "workflow-templates/.claude/commands/x.md"
	repo, paths = _conflict_repo(tmp_path, [command], base_extra={twin: "twin\n"})
	trusted = tmp_path / "gw"
	(trusted / ".codex-workflow-src" / "workflow-templates/.claude/commands").mkdir(parents=True)
	(trusted / ".codex-workflow-src" / twin).write_text("twin\n", encoding="utf-8")
	result = _guard_slice(tmp_path, repo, paths, github_workspace=str(trusted))
	assert result.returncode == 0, result.stderr
	assert "guard-passed" in result.stdout


def test_command_without_trusted_twin_is_refused(tmp_path):
	command = ".claude/commands/x.md"
	twin = "workflow-templates/.claude/commands/x.md"
	repo, paths = _conflict_repo(tmp_path, [command], base_extra={twin: "twin\n"})
	trusted = tmp_path / "gw"
	(trusted / ".codex-workflow-src").mkdir(parents=True)
	result = _guard_slice(tmp_path, repo, paths, github_workspace=str(trusted))
	assert result.returncode == 1
	assert "REVIEW_SANDBOX_PATH_REFUSED path=.claude/commands/x.md reason=command_not_admitted" in result.stderr
	assert "AI_ENGINE_FALLBACK role=CONFLICT_RESOLVER reason=sandbox_path_unsupported action=fail_closed\n" in result.stderr
	assert "::error::Conflict resolver isolation unavailable (reason=sandbox_path_unsupported); refusing host fallback.\n" in result.stderr
	# Without any support checkout no command is admitted either.
	no_support = _guard_slice(tmp_path, repo, paths)
	assert no_support.returncode == 1
	assert "reason=command_not_admitted" in no_support.stderr


def test_unpaired_live_hook_conflict_is_refused(tmp_path):
	repo, paths = _conflict_repo(tmp_path, [HOOK_LIVE])
	result = _guard_slice(tmp_path, repo, paths)
	assert result.returncode == 1
	assert f"REVIEW_SANDBOX_PATH_REFUSED path={HOOK_LIVE} reason=safety_hook" in result.stderr
	assert "reason=sandbox_path_unsupported" in result.stderr
	mirror = _helper_cmd("mirror-safety-hook", repo, paths)
	assert mirror.returncode == 1


@pytest.mark.parametrize("conflicted,base_extra", [
	([HOOK_TEMPLATE], {HOOK_LIVE: "unchanged\n"}),
	(["scripts/other.py"], {HOOK_LIVE: "unchanged\n", HOOK_TEMPLATE: "unchanged\n"}),
])
def test_fingerprint_expansion_cannot_authorize_live_hook_mirror(tmp_path, conflicted, base_extra):
	repo, paths = _conflict_repo(tmp_path, conflicted, base_extra=base_extra)
	paths.write_text(paths.read_text(encoding="utf-8") + HOOK_LIVE + "\n" + HOOK_TEMPLATE + "\n", encoding="utf-8")
	before = (repo / HOOK_LIVE).read_bytes()
	guard = _guard_slice(tmp_path, repo, paths)
	assert guard.returncode == 1
	assert f"REVIEW_SANDBOX_PATH_REFUSED path={HOOK_LIVE} reason=safety_hook" in guard.stderr
	assert "reason=sandbox_path_unsupported" in guard.stderr
	mirror = _helper_cmd("mirror-safety-hook", repo, paths)
	assert mirror.returncode == 1
	assert (repo / HOOK_LIVE).read_bytes() == before


def test_missing_initial_unmerged_snapshot_refuses_paired_hook(tmp_path):
	repo, paths = _conflict_repo(tmp_path, [HOOK_LIVE, HOOK_TEMPLATE])
	(tmp_path / "resolver_initial_unmerged_paths.txt").unlink()
	guard = _guard_slice(tmp_path, repo, paths)
	assert guard.returncode == 1
	assert f"REVIEW_SANDBOX_PATH_REFUSED path={HOOK_LIVE} reason=safety_hook" in guard.stderr
	assert _helper_cmd("mirror-safety-hook", repo, paths).returncode == 1


def test_unrelated_excluded_path_still_refused(tmp_path):
	repo, paths = _conflict_repo(tmp_path, [".github/ai/other.json"])
	result = _guard_slice(tmp_path, repo, paths)
	assert result.returncode == 1
	assert "REVIEW_SANDBOX_PATH_REFUSED path=.github/ai/other.json reason=hidden_dir" in result.stderr
	assert "AI_ENGINE_FALLBACK role=CONFLICT_RESOLVER reason=sandbox_path_unsupported action=fail_closed" in result.stderr


def test_refusal_log_redacts_and_truncates(tmp_path):
	paths = tmp_path / "paths"
	names = ["scripts/my_secret.py", ".env.local", "scripts/a.py\r::error::forged"] + [f"assets/f{i}.svg" for i in range(25)]
	paths.write_text("\n".join(names) + "\n", encoding="utf-8")
	result = _helper_cmd("check-paths", tmp_path, paths)
	assert result.returncode == 1
	lines = [line for line in result.stderr.splitlines() if line.startswith("REVIEW_SANDBOX_PATH_REFUSED ")]
	assert lines[0] == "REVIEW_SANDBOX_PATH_REFUSED path=redacted reason=excluded_dir"
	assert lines[1] == "REVIEW_SANDBOX_PATH_REFUSED path=redacted reason=excluded_dir"
	assert lines[2] == "REVIEW_SANDBOX_PATH_REFUSED path=redacted reason=invalid_path"
	assert "forged" not in result.stderr
	assert "secret" not in result.stderr
	assert len(lines) == 21 and lines[-1] == "REVIEW_SANDBOX_PATH_REFUSED truncated=8"
	assert result.stderr.rstrip().endswith("unsupported path")


def test_mirror_refuses_unresolved_or_symlinked_template(tmp_path):
	repo, paths = _conflict_repo(tmp_path, [HOOK_LIVE, HOOK_TEMPLATE])
	before = (repo / HOOK_LIVE).read_bytes()
	unresolved = _helper_cmd("mirror-safety-hook", repo, paths)
	assert unresolved.returncode == 3
	assert (repo / HOOK_LIVE).read_bytes() == before
	(repo / HOOK_TEMPLATE).unlink()
	(repo / HOOK_TEMPLATE).symlink_to("/etc/hostname")
	symlinked = _helper_cmd("mirror-safety-hook", repo, paths)
	assert symlinked.returncode == 1
	assert (repo / HOOK_LIVE).read_bytes() == before
	assert "hostname" not in symlinked.stderr


def test_mirror_runs_before_marker_scan_and_fails_closed():
	src = _source()
	mirror = src.index('mirror-safety-hook "$(pwd)" "${CONFLICTED_PATHS_FILE}" "${RESOLVER_INITIAL_UNMERGED_PATHS_FILE}"')
	assert src.index('_resolver_attempt_state check || _scope_status=$?') < mirror < src.index('\n  _scan_residual_markers\n')
	assert '_resolver_fail_closed safety_hook_mirror_failed' in src
	assert src.count('check-paths "$(pwd)" "${CONFLICTED_PATHS_FILE}" "${GITHUB_WORKSPACE:-}" "${RESOLVER_INITIAL_UNMERGED_PATHS_FILE}"') == 2
	assert src.index('verify_resolver_index_complete_or_fail; then\n    exit 1\n  fi\n  if ! verify_resolver_safety_hook_parity_or_fail')
