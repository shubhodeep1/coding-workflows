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


def test_read_role_cannot_write_snapshot_or_transfer():
	text = SANDBOX.read_text(encoding="utf-8")
	claude = text[text.index('if [ "${engine}" = claude ]; then'):text.index('[[ "${model}" =~')]
	assert 'settings_args=(settings --checkout /source --out "${root}/claude-settings.json" --profile "${claude_access}"' in claude
	assert "claude_tools='Read,Grep,Glob,Bash'" in claude
	assert 'claude_permissions=dontAsk' in claude
	assert "claude_source_mount+=',readonly'" in claude
	assert 'mktemp -d "${root}/home-read-XXXXXXXX"' in claude
	assert 'rm -rf -- "${claude_home}"' in claude
	assert '[ "${rc}" -eq 0 ] && [ "${claude_access}" = write ]' in claude
	assert '--network none --read-only --cap-drop ALL' in claude
	assert '--env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder' in claude
	assert '--mount "type=bind,src=$(ai_engine_pool_dir)' not in claude
	assert '--mount "type=bind,src=${root}/socket,dst=/socket,readonly"' in claude


def test_prepare_ephemeral_skips_dependency_container(tmp_path):
	workspace = tmp_path / "checkout"
	workspace.mkdir()
	for args in (("init", "-q"),):
		subprocess.run(["git", *args], cwd=workspace, check=True)
	(workspace / "app.py").write_text("value = 1\n")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log = tmp_path / "docker-calls"
	fake_docker = bin_dir / "docker"
	fake_docker.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{log}"\nprintf "sha256:%064d\\n" 0\n')
	fake_docker.chmod(0o755)
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
		GITHUB_WORKSPACE=str(workspace), SUPPORT_SCRIPTS_DIR=str(ROOT / "scripts"))
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
		env.pop(inherited, None)
	proc = subprocess.run(["bash", str(SANDBOX), "prepare-ephemeral"], cwd=workspace,
		env=env, capture_output=True, text=True)
	assert proc.returncode == 0, proc.stderr
	sandbox_root = Path(proc.stdout.strip())
	assert sandbox_root.parent == tmp_path
	assert (sandbox_root / "engine").read_text() == "claude\n"
	assert (sandbox_root / "workspace").read_text().strip() == str(workspace)
	assert (sandbox_root / "source" / "app.py").read_text() == "value = 1\n"
	assert "build " in log.read_text()
	assert "run " not in log.read_text()
	assert not (tmp_path / "github_env").exists()
	clean = subprocess.run(["bash", str(SANDBOX), "cleanup"], env=dict(env, REVIEW_SANDBOX_ROOT=str(sandbox_root)),
		capture_output=True, text=True)
	assert clean.returncode == 0, clean.stderr
	assert not sandbox_root.exists()
	mismatch = subprocess.run(["bash", str(SANDBOX), "prepare-ephemeral"], cwd=tmp_path,
		env=env, capture_output=True, text=True)
	assert mismatch.returncode == 1
	assert "workspace mismatch" in mismatch.stderr
	assert "run " not in log.read_text()


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
	branch = text[text.index('resolver_claude_rc=75'):text.index('if [ "${resolver_claude_rc}" -ne 75 ]; then')]
	assert branch.index('check-paths "$(pwd)" "${CONFLICTED_PATHS_FILE}"') < branch.index('prepare-ephemeral')
	assert branch.index('prepare-ephemeral') < branch.index('/dev/null claude CONFLICT_RESOLVER write') < branch.index('cleanup')
	assert 'GIT_INDEX_FILE=' not in branch
