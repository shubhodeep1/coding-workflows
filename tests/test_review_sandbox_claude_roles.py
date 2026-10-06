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


@pytest.mark.parametrize("name,accepted", [("scripts/a.py", True), (".ai/x.txt", False), ("scripts/../a.py", False), ("scripts/a.py\r", False)])
def test_check_paths(tmp_path, name, accepted):
	paths = tmp_path / "paths.txt"
	paths.write_text(name + "\n")
	proc = subprocess.run([sys.executable, str(WORKSPACE), "check-paths", str(tmp_path), str(paths)],
		env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True, text=True)
	assert (proc.returncode == 0) is accepted
	if not accepted:
		assert "unsupported path" in proc.stderr


def test_resolver_path_check_precedes_sandbox_and_does_not_pass_host_git_index():
	text = (ROOT / "scripts/review_conflict_resolve.sh").read_text(encoding="utf-8")
	guard = text[text.index('# Reject unsupported conflict paths for both engines'):text.index('attempt=1\nwhile ')]
	branch = text[text.index('resolver_claude_rc=75'):text.index('resolver_clean_output="${tmp_output}.ansi-clean"')]
	assert 'check-paths "$(pwd)" "${RESOLVER_MODEL_PATHS_FILE}"' in guard
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
	assert branch.index('check-paths "$(pwd)" "${RESOLVER_MODEL_PATHS_FILE}"') < branch.index('_resolver_sandbox_attempt claude')
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


# Issue #6595: a PR that conflicts on both the live merged-PR guard hook and
# its workflow-templates twin must be resolvable without admitting the live
# hook to the sandbox, and a rejected path must be named safely in the log.
LIVE_HOOK = ".claude/hooks/pr_merge_status_guard.py"
TEMPLATE_HOOK = "workflow-templates/.claude/hooks/pr_merge_status_guard.py"
RESOLVE_SCRIPT = ROOT / "scripts/review_conflict_resolve.sh"


def _git(repo, *args):
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@invalid", GIT_COMMITTER_NAME="t",
		GIT_COMMITTER_EMAIL="t@invalid", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
	return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=False)


def _write(repo, name, text, mode=0o644):
	path = repo / name
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")
	path.chmod(mode)


def _paired_conflict_repo(tmp_path, template_theirs="value = 2\n", extra=False):
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	for name in (LIVE_HOOK, TEMPLATE_HOOK):
		_write(repo, name, "value = 0\n", 0o755)
	if extra:
		_write(repo, ".ai/x.txt", "0\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")
	_git(repo, "checkout", "-qb", "feature")
	_write(repo, LIVE_HOOK, "value = 2\n", 0o755)
	_write(repo, TEMPLATE_HOOK, template_theirs, 0o755)
	if extra:
		_write(repo, ".ai/x.txt", "2\n")
	_git(repo, "commit", "-qam", "feature")
	_git(repo, "checkout", "-q", "main")
	for name in (LIVE_HOOK, TEMPLATE_HOOK):
		_write(repo, name, "value = 1\n", 0o755)
	if extra:
		_write(repo, ".ai/x.txt", "1\n")
	_git(repo, "commit", "-qam", "main")
	assert _git(repo, "merge", "-q", "feature").returncode != 0
	conflicted = [LIVE_HOOK, TEMPLATE_HOOK] + ([".ai/x.txt"] if extra else [])
	(tmp_path / "conflicted").write_text("".join(f"{n}\n" for n in conflicted), encoding="utf-8")
	(tmp_path / "unmerged.z").write_bytes(_git(repo, "ls-files", "-u", "-z").stdout)
	return repo


def _workspace(*args):
	return subprocess.run([sys.executable, str(WORKSPACE), *map(str, args)],
		env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True, text=True)


def _pair(tmp_path, repo):
	return _workspace("paired-live-copies", repo, tmp_path / "conflicted", tmp_path / "unmerged.z",
		tmp_path / "pairs", tmp_path / "model_paths")


def test_paired_conflict_moves_live_hook_out_of_model_paths(tmp_path):
	repo = _paired_conflict_repo(tmp_path)
	proc = _pair(tmp_path, repo)
	assert proc.returncode == 0, proc.stderr
	assert (tmp_path / "pairs").read_text() == f"{LIVE_HOOK}\t{TEMPLATE_HOOK}\n"
	assert (tmp_path / "model_paths").read_text() == f"{TEMPLATE_HOOK}\n"
	assert _workspace("check-paths", repo, tmp_path / "model_paths").returncode == 0


def test_divergent_template_conflict_stays_fail_closed(tmp_path):
	repo = _paired_conflict_repo(tmp_path, template_theirs="value = 9\n")
	proc = _pair(tmp_path, repo)
	assert proc.returncode == 0, proc.stderr
	assert (tmp_path / "pairs").read_text() == ""
	assert LIVE_HOOK in (tmp_path / "model_paths").read_text().splitlines()
	check = _workspace("check-paths", repo, tmp_path / "model_paths")
	assert check.returncode == 1
	assert f"REVIEW_RESOLVER_PATH_REJECTED reason=live_safety_hook path={LIVE_HOOK}\n" in check.stderr
	assert "unsupported path" in check.stderr


def test_paired_conflict_with_unrelated_excluded_path_is_still_refused(tmp_path):
	repo = _paired_conflict_repo(tmp_path, extra=True)
	assert _pair(tmp_path, repo).returncode == 0
	assert (tmp_path / "pairs").read_text() == f"{LIVE_HOOK}\t{TEMPLATE_HOOK}\n"
	assert (tmp_path / "model_paths").read_text() == f"{TEMPLATE_HOOK}\n.ai/x.txt\n"
	check = _workspace("check-paths", repo, tmp_path / "model_paths")
	assert check.returncode == 1
	assert "REVIEW_RESOLVER_PATH_REJECTED reason=excluded_component path=.ai/x.txt\n" in check.stderr


def test_malformed_unmerged_dump_fails_closed(tmp_path):
	repo = _paired_conflict_repo(tmp_path)
	(tmp_path / "unmerged.z").write_bytes(b"100644 nothex 2\t" + LIVE_HOOK.encode() + b"\0")
	proc = _pair(tmp_path, repo)
	assert proc.returncode == 1
	assert proc.stderr.strip() == "paired live copy check failed"


def test_mirror_live_copy_is_byte_identical(tmp_path):
	repo = _paired_conflict_repo(tmp_path)
	assert _pair(tmp_path, repo).returncode == 0
	_write(repo, TEMPLATE_HOOK, "value = 3\n", 0o755)
	proc = _workspace("mirror-live-copies", repo, tmp_path / "pairs")
	assert proc.returncode == 0, proc.stderr
	live = repo / LIVE_HOOK
	assert live.read_bytes() == (repo / TEMPLATE_HOOK).read_bytes()
	assert live.stat().st_mode & 0o777 == 0o755
	assert not [p for p in live.parent.iterdir() if p.name.startswith(".review-live-copy-")]


@pytest.mark.parametrize("case", ["markers", "live_symlink", "parent_symlink", "unknown_pair"])
def test_mirror_refuses_unsafe_states_and_leaves_live_unchanged(tmp_path, case):
	repo = _paired_conflict_repo(tmp_path)
	assert _pair(tmp_path, repo).returncode == 0
	_write(repo, TEMPLATE_HOOK, "value = 3\n", 0o755)
	live = repo / LIVE_HOOK
	pairs = tmp_path / "pairs"
	if case == "markers":
		_write(repo, TEMPLATE_HOOK, "<<<<<<< HEAD\nvalue = 1\n=======\nvalue = 2\n>>>>>>> feature\n", 0o755)
	elif case == "live_symlink":
		target = tmp_path / "outside.py"
		target.write_text("outside\n")
		live.unlink()
		live.symlink_to(target)
	elif case == "parent_symlink":
		outside = tmp_path / "outside_hooks"
		(repo / ".claude/hooks").rename(outside)
		(repo / ".claude/hooks").symlink_to(outside, target_is_directory=True)
		live = outside / "pr_merge_status_guard.py"
	else:
		pairs.write_text(f".claude/settings.json\t{TEMPLATE_HOOK}\n")
	before = live.read_bytes()
	proc = _workspace("mirror-live-copies", repo, pairs)
	assert proc.returncode == 1
	assert proc.stderr.strip() == "paired live copy mirror failed"
	assert live.read_bytes() == before
	if case == "live_symlink":
		assert (tmp_path / "outside.py").read_text() == "outside\n"


@pytest.mark.parametrize("name,reason,shown", [
	(".ai/x.txt", "excluded_component", ".ai/x.txt"),
	("scripts/../a.py", "unsafe_name", "redacted"),
	(".claude/settings.json", "dot_directory", ".claude/settings.json"),
	("tests/test_audit_plans_command.py", "operator_input", "tests/test_audit_plans_command.py"),
	("assets/a\x1b]0;::error::x.svg", "unsupported_type", "redacted"),
	("config/secrets.svg", "excluded_component", "redacted"),
])
def test_check_paths_rejection_diagnostic_is_fixed_and_control_safe(tmp_path, name, reason, shown):
	paths = tmp_path / "paths.txt"
	paths.write_text(name + "\n")
	proc = _workspace("check-paths", tmp_path, paths)
	assert proc.returncode == 1
	lines = proc.stderr.splitlines()
	assert lines == [f"REVIEW_RESOLVER_PATH_REJECTED reason={reason} path={shown}", "unsupported path"]


def test_resolver_pairing_mirror_and_index_parity(tmp_path):
	repo = _paired_conflict_repo(tmp_path)
	src = RESOLVE_SCRIPT.read_text(encoding="utf-8")
	pairing = src[src.index("# Paired live/template copies (issue #6595)"):src.index("# Pre-load the conflicted files into the resolver prompt")]
	allowlist = tmp_path / "allowlist"
	allowlist.write_text((tmp_path / "conflicted").read_text())
	github_env = tmp_path / "github_env"
	program = f'''set -euo pipefail
RUNTIME_DIR={str(tmp_path)!r}
SUPPORT_SCRIPTS_DIR={str(ROOT / "scripts")!r}
CONFLICTED_PATHS_FILE={str(tmp_path / "conflicted")!r}
RESOLVER_ALLOWLIST_FILE={str(allowlist)!r}
GITHUB_ENV={str(github_env)!r}
_resolver_fail_closed() {{ echo "fail_closed=$1"; exit 1; }}
{pairing}
cat "${{RESOLVER_TARGETED_PATHS_FILE}}"
_resolver_mirror_paired_live_copies
printf 'value = 3\\n' > {TEMPLATE_HOOK}
_resolver_mirror_paired_live_copies
if [ "${{PARITY_CASE}}" = diverge ]; then printf 'value = 4\\n' > {LIVE_HOOK}; fi
git add -- {LIVE_HOOK} {TEMPLATE_HOOK}
_resolver_verify_paired_live_index && echo parity-ok
'''
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("BASH_ENV", "ENV")}
	env.update(PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
	ok = subprocess.run(["bash", "-c", program], cwd=repo, env=dict(env, PARITY_CASE="same"), capture_output=True, text=True)
	assert ok.returncode == 0, ok.stdout + ok.stderr
	assert f"REVIEW_RESOLVER_PAIRED_LIVE live={LIVE_HOOK} template={TEMPLATE_HOOK} outcome=paired" in ok.stdout
	# Targeted context (and so the model prompt) omits the runner-synced live copy.
	assert (tmp_path / "resolver_targeted_paths.txt").read_text() == f"{TEMPLATE_HOOK}\n"
	assert "REVIEW_RESOLVER_PAIRED_LIVE outcome=skipped reason=template_markers" in ok.stdout
	assert "REVIEW_RESOLVER_PAIRED_LIVE outcome=mirrored" in ok.stdout
	assert "parity-ok" in ok.stdout
	assert (tmp_path / "resolver_model_paths.txt").read_text() == f"{TEMPLATE_HOOK}\n"
	assert (repo / LIVE_HOOK).read_bytes() == (repo / TEMPLATE_HOOK).read_bytes() == b"value = 3\n"
	assert _git(repo, "diff", "--name-only", "--diff-filter=U").stdout == b""
	bad = subprocess.run(["bash", "-c", program], cwd=_reset_repo(repo), env=dict(env, PARITY_CASE="diverge"), capture_output=True, text=True)
	assert bad.returncode == 1
	assert "parity-ok" not in bad.stdout
	assert "::error::Paired live copy is not byte-identical to its template in the index" in bad.stdout
	assert "CONFLICT_RESOLVED=false" in github_env.read_text()


def _reset_repo(repo):
	_git(repo, "merge", "--abort")
	assert _git(repo, "merge", "-q", "feature").returncode != 0
	return repo
