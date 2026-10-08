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
GITHUB_ENV={str(github_env)!r}
IS_INTEGRATION_SYNC={integration}
emit_conflict_resolver_substate() {{ :; }}
_persist_resolver_retry_state_from_current_failure() {{ printf '%s\\n' "$RESOLVER_ISOLATION_FAILURE_REASON" > "$RUNTIME_DIR/persisted_reason"; }}
{_failure_helper()}
{_path_failure_helper()}
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
	result, _runtime, github_env, _ = _run_take_base(tmp_path, repo, linked=[])
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
	comments = json.dumps([{"body": "x\n<!-- ai:host-only-conflict-reissue:v1 pr=7 issue=900 head=abc -->"}])
	result, calls, _ = _run_reissue_guard(tmp_path, comments=comments)
	assert result.returncode == 1 and "reason=already_reissued" in result.stdout
	assert calls == []


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
