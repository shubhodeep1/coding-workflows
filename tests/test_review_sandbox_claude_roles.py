"""Review Claude roles must use the credential-free sandbox, never host Claude."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "scripts/review_untrusted_sandbox.sh"
WORKSPACE = ROOT / "scripts/review_untrusted_workspace.py"


def test_role_and_access_are_allowlisted(tmp_path):
	root = tmp_path / "review-isolated-test"
	root.mkdir()
	(root / "image").write_text("image\n")
	(root / "baseline.json").write_text("{}")
	(root / "workspace").write_text(str(tmp_path))
	env = dict(os.environ, RUNNER_TEMP=str(tmp_path), REVIEW_SANDBOX_ROOT=str(root),
		SUPPORT_SCRIPTS_DIR=str(ROOT / "scripts"))
	for extras in (("claude", "UNTRUSTED", "read"), ("claude", "RB_JUDGE", "unsafe")):
		proc = subprocess.run(["bash", str(SANDBOX), "run", "prompt", "out", "model", "high", "/dev/null", *extras],
			env=env, capture_output=True, text=True)
		assert proc.returncode == 2, proc.stderr
	text = SANDBOX.read_text(encoding="utf-8")
	assert 'claude_role="${8:-REVIEW_EDITOR}"' in text
	assert 'engine="${7:-codex}"' in text
	assert 'claude_access="${9:-write}"' in text
	assert '"${claude_role}" = REVIEW_CONSOLIDATOR' in text
	assert '# Poller judges (WAVE/STALL/INTEGRATION/SECURITY) are read-only sandbox roles; never transfer.' in text
	for role in ("WAVE_JUDGE", "STALL_JUDGE", "INTEGRATION_JUDGE", "SECURITY_JUDGE"):
		proc = subprocess.run(["bash", str(SANDBOX), "run", "prompt", "out", "model", "high", "/dev/null", "codex", role, "write"],
			env=env, capture_output=True, text=True)
		assert proc.returncode == 2, (role, proc.stderr)
		assert role in text.split('case "${claude_role}" in', 1)[1].split('esac', 1)[0]


def test_read_role_cannot_write_snapshot_or_transfer():
	text = SANDBOX.read_text(encoding="utf-8")
	claude = text[text.index('if [ "${engine}" = claude ]; then'):text.index('[[ "${model}" =~')]
	assert 'settings_args=(settings --checkout /source --out "${root}/claude-settings.json" --profile "${claude_access}"' in claude
	assert "claude_tools='Read,Grep,Glob'" in claude
	assert 'claude_permissions=dontAsk' in claude
	assert "claude_source_mount+=',readonly'" in claude
	assert 'mktemp -d "${root}/home-read-XXXXXXXX"' in claude
	assert 'rm -rf -- "${claude_home}"' in claude
	assert '[ "${rc}" -eq 0 ] && [ "${claude_access}" = write ]' in claude
	assert '--network none --read-only --cap-drop ALL' in claude
	assert '--env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder' in claude
	assert '--mount "type=bind,src=$(ai_engine_pool_dir)' not in claude
	assert '--mount "type=bind,src=${root}/socket,dst=/socket,readonly"' in claude
	opencode = text[text.index('[[ "${model}" =~'):]
	assert 'claude_access="${9:-write}"' in text
	assert 'if [ "${rc}" -eq 0 ] && [ "${claude_access}" = write ]; then' in opencode
	assert "opencode_source_mount+=',readonly'" in opencode
	assert 'config["snapshot"] = False' in opencode
	assert '"SECURITY_JUDGE", "RB_JUDGE", "REVIEW_CONSOLIDATOR"}' in opencode
	rb_judge_case = opencode.split('\tRB_JUDGE|REVIEW_CONSOLIDATOR)\n', 1)[1].split('\n\t\t;;', 1)[0]
	assert '[ "${claude_access}" = read ]' in rb_judge_case
	assert "opencode_source_mount+=',readonly'" in rb_judge_case
	assert 'opencode_agent=reviewer' in rb_judge_case
	assert 'opencode_agent=writer' in opencode
	assert '--env "OPENCODE_AGENT=${opencode_agent}"' in opencode
	assert '--agent "${OPENCODE_AGENT}"' in opencode


@pytest.mark.parametrize("engine", ("claude", "codex"))
def test_prepare_ephemeral_skips_dependency_container(tmp_path, engine):
	workspace = tmp_path / "checkout"
	workspace.mkdir()
	for args in (("init", "-q"),):
		subprocess.run(["git", *args], cwd=workspace, check=True)
	(workspace / "app.py").write_text("value = 1\n")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log = tmp_path / "docker-calls"
	fake_docker = bin_dir / "docker"
	fake_docker.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{log}"\n'
		f'if [[ "$*" == *CLAUDE_CLI_VERSION=* && -e "{tmp_path}/reject-claude" ]]; then exit 25; fi\n'
		'printf "sha256:%064d\\n" 0\n')
	fake_docker.chmod(0o755)
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
		GITHUB_WORKSPACE=str(workspace), SUPPORT_SCRIPTS_DIR=str(ROOT / "scripts"))
	if engine == "codex":
		# An OpenCode retry must work even without engine files, and must not
		# attempt the Claude CLI build that this Docker stand-in rejects.
		(tmp_path / "reject-claude").touch()
		support = tmp_path / "support"
		(support / "review_sandbox").mkdir(parents=True)
		for name in ("review_untrusted_workspace.py", "clarify_openrouter_broker.py"):
			(support / name).symlink_to(ROOT / "scripts" / name)
		(support / "review_sandbox" / "Dockerfile").symlink_to(ROOT / "scripts" / "review_sandbox" / "Dockerfile")
		env["SUPPORT_SCRIPTS_DIR"] = str(support)
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
		env.pop(inherited, None)
	proc = subprocess.run(["bash", str(SANDBOX), "prepare-ephemeral", engine], cwd=workspace,
		env=env, capture_output=True, text=True)
	assert proc.returncode == 0, proc.stderr
	sandbox_root = Path(proc.stdout.strip())
	assert sandbox_root.parent == tmp_path
	assert (sandbox_root / "engine").exists() is (engine == "claude")
	assert (sandbox_root / "workspace").read_text().strip() == str(workspace)
	assert (sandbox_root / "source" / "app.py").read_text() == "value = 1\n"
	assert "build " in log.read_text()
	assert ("CLAUDE_CLI_VERSION=" in log.read_text()) is (engine == "claude")
	assert "run " not in log.read_text()
	assert not (tmp_path / "github_env").exists()
	clean = subprocess.run(["bash", str(SANDBOX), "cleanup"], env=dict(env, REVIEW_SANDBOX_ROOT=str(sandbox_root)),
		capture_output=True, text=True)
	assert clean.returncode == 0, clean.stderr
	assert not sandbox_root.exists()
	mismatch = subprocess.run(["bash", str(SANDBOX), "prepare-ephemeral", engine], cwd=tmp_path,
		env=env, capture_output=True, text=True)
	assert mismatch.returncode == 1
	assert "workspace mismatch" in mismatch.stderr
	assert "run " not in log.read_text()
	invalid = subprocess.run(["bash", str(SANDBOX), "prepare-ephemeral", "invalid"], cwd=workspace,
		env=env, capture_output=True, text=True)
	assert invalid.returncode == 2


def test_ephemeral_callers_prepare_their_selected_engine():
	poller = (ROOT / "scripts/orchestrate_poll_process.sh").read_text(encoding="utf-8")
	resolver = (ROOT / "scripts/review_conflict_resolve.sh").read_text(encoding="utf-8")
	judge = (ROOT / "scripts/review_rb_judge.sh").read_text(encoding="utf-8")
	ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
	assert 'prepare-ephemeral "${rb_engine}"' in poller
	assert 'prepare-ephemeral "${sandbox_attempt_engine}"' in resolver
	assert 'prepare-ephemeral claude' in judge
	assert 'prepare-ephemeral codex' in judge
	assert "tests/test_review_sandbox_claude_roles.py" in ci


@pytest.mark.parametrize("name,accepted,reason", [
	("scripts/a.py", True, None), (".ai/x.txt", False, "excluded_dir"),
	("scripts/../a.py", False, "invalid_path"), ("scripts/a.py\r", False, "invalid_path"),
	(".claude/settings.json", False, "hidden_dir"), (".claude/commands/x.md", False, "command_not_admitted"),
	(".claude/hooks/pr_merge_status_guard.py", False, "safety_hook"), ("assets/x.svg", False, "unsupported_suffix"),
])
def test_check_paths(tmp_path, name, accepted, reason):
	paths = tmp_path / "paths.txt"
	paths.write_text(name + "\n")
	proc = subprocess.run([sys.executable, str(WORKSPACE), "check-paths", str(tmp_path), str(paths)],
		env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True, text=True)
	assert (proc.returncode == 0) is accepted
	if not accepted:
		assert "unsupported path" in proc.stderr
		assert f" reason={reason}" in proc.stderr
		assert "REVIEW_SANDBOX_PATH_REFUSED path=" in proc.stderr


def test_resolver_path_check_precedes_sandbox_and_does_not_pass_host_git_index():
	text = (ROOT / "scripts/review_conflict_resolve.sh").read_text(encoding="utf-8")
	guard = text[text.index('# Reject unsupported conflict paths for both engines'):text.index('attempt=1\nwhile ')]
	branch = text[text.index('resolver_claude_rc=75'):text.index('resolver_clean_output="${tmp_output}.ansi-clean"')]
	assert 'check-paths "$(pwd)" "${CONFLICTED_PATHS_FILE}"' in guard
	assert guard.index('check-paths') < guard.index('prepare-ephemeral codex')
	assert branch.index('_resolver_sandbox_attempt claude') < branch.index('_resolver_sandbox_attempt codex')
	assert 'GIT_INDEX_FILE=' not in branch
	assert 'GIT_INDEX_FILE=' not in guard


def test_prepare_ephemeral_codex_works_without_optional_claude_support(tmp_path):
	workspace = tmp_path / "checkout"
	workspace.mkdir()
	subprocess.run(["git", "init", "-q", str(workspace)], check=True)
	(workspace / "app.py").write_text("value = 1\n")
	support = tmp_path / "support"
	support.mkdir()
	for name in ("review_untrusted_workspace.py", "clarify_openrouter_broker.py"):
		(support / name).symlink_to(ROOT / "scripts" / name)
	(support / "review_sandbox").symlink_to(ROOT / "scripts/review_sandbox", target_is_directory=True)
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	docker = bin_dir / "docker"
	docker.write_text('#!/bin/bash\nprintf "sha256:%064d\\n" 0\n')
	docker.chmod(0o755)
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
		GITHUB_WORKSPACE=str(workspace), SUPPORT_SCRIPTS_DIR=str(support))
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
		env.pop(inherited, None)
	without_arg = subprocess.run(["bash", str(SANDBOX), "prepare-ephemeral"], cwd=workspace,
		env=env, capture_output=True, text=True)
	assert without_arg.returncode == 1
	assert "Review Claude support missing" in without_arg.stderr
	with_codex = subprocess.run(["bash", str(SANDBOX), "prepare-ephemeral", "codex"], cwd=workspace,
		env=env, capture_output=True, text=True)
	assert with_codex.returncode == 0, with_codex.stderr
	root = Path(with_codex.stdout.strip())
	assert (root / "image").exists()
	assert not (root / "engine").exists()
	assert subprocess.run(["bash", str(SANDBOX), "cleanup"], env=dict(env, REVIEW_SANDBOX_ROOT=str(root)),
		capture_output=True).returncode == 0


def test_claude_resolver_sandbox_attempt_and_path_gate():
	text = (ROOT / "scripts/review_conflict_resolve.sh").read_text(encoding="utf-8")
	helper = SANDBOX.read_text(encoding="utf-8")
	assert 'prepare|prepare-ephemeral|run|cleanup' in helper
	assert '[ "$#" -ge 6 ] && [ "$#" -le 9 ]' in helper
	attempt = text[text.index('_resolver_sandbox_attempt()'):text.index('# Source-repo only: the final touched-set gate')]
	branch = text[text.index('resolver_claude_rc=75'):text.index('resolver_clean_output="${tmp_output}.ansi-clean"')]
	assert branch.index('check-paths "$(pwd)" "${CONFLICTED_PATHS_FILE}"') < branch.index('_resolver_sandbox_attempt claude')
	assert attempt.index('prepare-ephemeral') < attempt.index('run "${_effective_prompt_file}"') < attempt.index('if ! REVIEW_SANDBOX_ROOT=')
	assert 'GIT_INDEX_FILE=' not in attempt + branch


def test_claude_resolver_isolation_failures_do_not_select_host_writer():
	text = (ROOT / "scripts/review_conflict_resolve.sh").read_text(encoding="utf-8")
	closed = text[text.index('_resolver_fail_closed()'):].split('\n}\n', 1)[0] + '\n}'
	assert 'action=fail_closed' in closed
	assert 'RESOLVER_ISOLATION_FAILURE_REASON="$1" _persist_resolver_retry_state_from_current_failure' in closed
	assert closed.rstrip().endswith('exit 1\n}')
	branch = text[text.index('resolver_claude_rc=75'):text.index('resolver_clean_output="${tmp_output}.ansi-clean"')]
	assert branch.count('AI_ENGINE_FALLBACK role=CONFLICT_RESOLVER') == 1
	assert 'reason=claude_unavailable action=sandbox_opencode' in branch
	for reason in ('sandbox_prepare_failed', 'sandbox_path_unsupported', 'sandbox_opencode_unavailable'):
		assert f'_resolver_fail_closed {reason}' in branch
	assert '_resolver_fail_closed "${resolver_sandbox_failure_reason}"' in branch
	assert branch.index('resolver_claude_rc=0\n    fi') > branch.index('_codex_exit="${resolver_claude_rc}"')


def test_progress_monitor_stops_without_waiting_for_its_sleep(tmp_path):
	text = SANDBOX.read_text(encoding="utf-8")
	monitor_body = text.split('\t\t(\n\t\t\tlast_size=0\n', 1)[1].split('\n\t\t) &', 1)[0]
	process = subprocess.run(
		["bash", "-c", 'root="$1"; REVIEW_SANDBOX_PROGRESS_SECS=60; ( last_size=0\n' + monitor_body + '\n) & progress_pid=$!; sleep 0.1; kill "$progress_pid"; wait "$progress_pid" || true', "_", str(tmp_path)],
		capture_output=True, text=True, timeout=3, check=False,
	)
	assert process.returncode == 0, process.stderr
