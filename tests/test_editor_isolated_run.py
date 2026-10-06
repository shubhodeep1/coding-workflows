"""No model process may start on the credentialed host checkout."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/editor_isolated_run.sh"


def test_prepare_reap_finish_and_argv_guard(tmp_path: Path, request: pytest.FixtureRequest) -> None:
	short_root = tempfile.TemporaryDirectory(prefix="ei-", dir="/tmp")
	request.addfinalizer(short_root.cleanup)
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log = tmp_path / "docker.log"
	docker = bin_dir / "docker"
	docker.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "''' + str(log) + '''"
case "$1" in
build) printf 'sha256:%064d\\n' 0 ;;
ps) [ -f "''' + str(tmp_path / "survivor") + '''" ] && echo container123 || true ;;
esac
''')
	docker.chmod(0o755)
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUNNER_TEMP=short_root.name,
		EDITOR_ISOLATION_SUPPORT_DIR=str(ROOT / "scripts"),
		GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1", GH_TOKEN="forbidden", OPENROUTER_API_KEY="fake-key")
	env.pop("WORKSPACE_PATH", None)
	def invoke(*args: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
		return subprocess.run(["bash", str(HELPER), *args], cwd=ROOT, env=env, text=True, input=input_text, capture_output=True)
	prepared = invoke("prepare", "read", "codex")
	assert prepared.returncode == 0, prepared.stderr
	isolation_root = prepared.stdout.strip()
	assert (Path(isolation_root) / "bin/codex").stat().st_mode & 0o111
	bad = invoke("codex-exec", isolation_root, "--model", "untrusted")
	assert bad.returncode == 2
	assert "run --rm" not in log.read_text()
	model_call = invoke("codex-exec", isolation_root, "--ask-for-approval", "never", "-c", "model_verbosity=low", "-c", "include_apply_patch_tool=true", "exec", "--skip-git-repo-check", "--model", "openai/gpt-6-sol", "--sandbox", "danger-full-access", input_text="plan prompt")
	assert model_call.returncode == 0, model_call.stderr
	second_call = invoke("codex-exec", isolation_root, "--ask-for-approval", "never", "-c", "model_verbosity=low", "-c", "include_apply_patch_tool=true", "exec", "--skip-git-repo-check", "--model", "openai/gpt-5.6-sol", "--sandbox", "read-only", input_text="retry prompt")
	assert second_call.returncode == 0, second_call.stderr
	run_args = next(line for line in log.read_text().splitlines() if line.startswith("run --rm"))
	for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "coding-workflows.editor-isolation.root=", "/source,readonly"):
		assert flag in run_args
	for sensitive in ("forbidden", "fake-key", str(ROOT / ".git"), "--env GH_TOKEN", "--env OPENROUTER_API_KEY"):
		assert sensitive not in run_args
	# A failed removal is not a successful finish; it may never authorize restore.
	(tmp_path / "survivor").touch()
	assert invoke("finish", isolation_root).returncode != 0
	failed_cleanup = invoke("cleanup", isolation_root)
	assert failed_cleanup.returncode != 0
	assert "EDITOR_ISOLATION action=cleanup outcome=reap_failed" in failed_cleanup.stderr
	assert Path(isolation_root).is_dir()  # The next credential-restore step must recheck it.
	assert invoke("reap", isolation_root).returncode != 0
	(tmp_path / "survivor").unlink()
	assert invoke("reap", isolation_root).returncode == 0
	successful_cleanup = invoke("cleanup", isolation_root)
	assert successful_cleanup.returncode == 0
	assert "EDITOR_ISOLATION action=cleanup outcome=removed" in successful_cleanup.stderr
	assert not Path(isolation_root).exists()


def test_launch_contracts_are_isolated_and_reaped_before_restore() -> None:
	implement = (ROOT / ".github/workflows/implement.yml").read_text()
	plan = (ROOT / "scripts/run_plan_codex.sh").read_text()
	helper = HELPER.read_text()
	for block in (implement.split("      - name: Run Codex implementation\n", 1)[1].split("      - name: ", 1)[0],
		implement.split("      - name: Attempt post-Codex syntax repair\n", 1)[1].split("      - name: ", 1)[0]):
		assert 'editor_isolated_run.sh" snapshot' in block
		assert 'editor_isolated_run.sh" finish' in block
		assert block.index('editor_isolated_run.sh" snapshot') < block.index('editor_git_credentials hide')
		assert block.index('editor_isolated_run.sh" finish') < block.index('editor_git_credentials restore', block.index('editor_isolated_run.sh" finish'))
		assert 'editor_isolated_run.sh" reap "${EDITOR_ISOLATION_ROOT}" && editor_git_credentials restore' in block
		assert 'editor_git_credentials restore || isolation_exit_rc=1; bash "${EDITOR_ISOLATION_SUPPORT_DIR}/editor_isolated_run.sh" cleanup "${EDITOR_ISOLATION_ROOT}" || isolation_exit_rc=1' in block
		assert 'CODEX_THREAD_REUSE_REAL_CODEX="${EDITOR_ISOLATION_ROOT}/bin/codex"' in block
		assert block.count('CODEX_THREAD_REUSE_REAL_CODEX=') == 1
		assert 'CODEX_THREAD_REUSE_CLAUDE_RUNNER=' not in block
		assert 'bash "${EDITOR_ISOLATION_SUPPORT_DIR}/codex_thread_reuse.sh" direct-run' in block
	assert 'claude_run PLAN' not in plan
	assert '"${EDITOR_ISOLATION_ROOT}/bin/codex" --ask-for-approval never' in plan
	assert 'editor_isolated_run.sh" finish' in plan
	assert 'editor_git_credentials restore || isolation_exit_rc=1; bash "${EDITOR_ISOLATION_SUPPORT_DIR:-scripts}/editor_isolated_run.sh" cleanup "${EDITOR_ISOLATION_ROOT}" || isolation_exit_rc=1' in plan
	assert '--network none --read-only --cap-drop ALL --security-opt no-new-privileges' in helper
	assert '--label "coding-workflows.editor-isolation.root=' in helper
	assert 'env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker' in helper
	assert '--mount "type=bind,src=${root}/source,dst=/source' not in helper  # profile chooses mode
	assert '--env GH_TOKEN=' not in helper and '--env OPENROUTER_API_KEY=' not in helper
	assert '--env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder' in helper
	assert 'config --key hide_claude_md' in helper
	assert '--mount "type=bind,src=${root}/empty-claude-md,dst=/source/CLAUDE.md,readonly"' in helper
	assert 'if [ -n "${EDITOR_ISOLATION_ROOT:-}" ] && [ -d "${EDITOR_ISOLATION_ROOT}" ]; then\n            bash "${EDITOR_ISOLATION_SUPPORT_DIR}/editor_isolated_run.sh" reap "${EDITOR_ISOLATION_ROOT}"\n          fi\n          editor_git_credentials restore' in implement


def test_exit_traps_cleanup_after_restore_failure_but_never_restore_before_reap(tmp_path: Path) -> None:
	implement = (ROOT / ".github/workflows/implement.yml").read_text()
	plan = (ROOT / "scripts/run_plan_codex.sh").read_text()
	stub = tmp_path / "editor_isolated_run.sh"
	stub.write_text('''printf '%s\\n' "$1" >> "${ISOLATION_LOG}"
if [ "$1" = reap ] && [ "${REAP_FAIL:-0}" = 1 ]; then exit 1; fi
''')
	for text in (implement, plan):
		traps = [line.strip() for line in text.splitlines() if line.strip().startswith("trap 'isolation_exit_rc=")]
		assert len(traps) == (2 if text == implement else 1)
		for trap_line in traps:
			for reap_fails, restore_fails, original_rc, expected_calls, expected_rc in (
				("0", "1", "0", ["reap", "restore", "cleanup"], 1),
				("1", "0", "0", ["reap", "cleanup"], 1),
				("0", "0", "2", ["reap", "restore", "cleanup"], 2),
			):
				log = tmp_path / "isolation.log"
				log.unlink(missing_ok=True)
				env = dict(os.environ, EDITOR_ISOLATION_SUPPORT_DIR=str(tmp_path), EDITOR_ISOLATION_ROOT="unused",
					ISOLATION_LOG=str(log), REAP_FAIL=reap_fails, RESTORE_FAIL=restore_fails)
				command = ("set -euo pipefail\n"
					"editor_git_credentials() { printf '%s\\n' restore >> \"${ISOLATION_LOG}\"; [ \"${RESTORE_FAIL}\" = 0 ]; }\n"
					f"{trap_line}\nexit {original_rc}\n")
				proc = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
				assert proc.returncode == expected_rc, proc.stderr
				assert log.read_text().splitlines() == expected_calls
