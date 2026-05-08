#!/usr/bin/env python3
"""Contract tests for Semble foundation plumbing.

The implement workflow now stages two shared shell helpers:

- scripts/install_semble.sh
- scripts/semble_helpers.sh

These tests pin the fail-soft install contract, the helper's strict
stdout/stderr split, and the minimal workflow wiring that makes the new
foundation available to downstream callers.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_semble.sh"
HELPERS_SCRIPT = REPO_ROOT / "scripts" / "semble_helpers.sh"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
REVIEW_CONSOLIDATE_SCRIPT = REPO_ROOT / "scripts" / "review_consolidate.sh"


def _workflow_step_block_from(workflow_path: Path, step_name: str) -> str:
	body = workflow_path.read_text(encoding="utf-8")
	marker = f"\n      - name: {step_name}\n"
	_, found, tail = body.partition(marker)
	assert found, f"workflow step missing: {step_name}"
	block, _, _ = tail.partition("\n      - name: ")
	return block


def _workflow_step_block(step_name: str) -> str:
	return _workflow_step_block_from(IMPLEMENT_WORKFLOW, step_name)


def _write_executable(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_install(env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	full_env["PATH"] = full_env.get("PATH", "")
	if env_overrides:
		full_env.update(env_overrides)
	return subprocess.run(
		["bash", str(INSTALL_SCRIPT)],
		env=full_env,
		text=True,
		capture_output=True,
		check=False,
	)


def _run_helper(env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	if env_overrides:
		full_env.update(env_overrides)
	harness = f"""
		set -u
		source {HELPERS_SCRIPT}
		semble_query_block 'needle query' 3 'Implement Context'
	"""
	return subprocess.run(
		["bash", "-lc", harness],
		env=full_env,
		text=True,
		capture_output=True,
		check=False,
	)


def test_install_semble_disabled_is_noop_and_fail_soft() -> None:
	with tempfile.TemporaryDirectory() as td:
		env_file = Path(td) / "github_env.txt"
		result = _run_install(
			{
				"SEMBLE_ENABLED": "false",
				"GITHUB_ENV": str(env_file),
			}
		)
		assert result.returncode == 0, result.stderr
		body = env_file.read_text(encoding="utf-8")
		assert "SEMBLE_AVAILABLE=false" in body, body
		assert "SEMBLE_FALLBACK" not in result.stdout, result.stdout


def test_install_semble_failure_exports_false_without_failing() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		env_file = tmp / "github_env.txt"
		_write_executable(
			bin_dir / "uv",
			"#!/usr/bin/env bash\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = list ]; then\n"
			"  echo 'No tools installed'\n"
			"  exit 0\n"
			"fi\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = dir ] && [ \"$3\" = --bin ]; then\n"
			f"  echo '{bin_dir}'\n"
			"  exit 0\n"
			"fi\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = install ]; then\n"
			"  echo 'simulated install failure' >&2\n"
			"  exit 42\n"
			"fi\n"
			"exit 0\n",
		)
		result = _run_install(
			{
				"SEMBLE_ENABLED": "true",
				"GITHUB_ENV": str(env_file),
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			}
		)
		assert result.returncode == 0, result.stderr
		body = env_file.read_text(encoding="utf-8")
		assert "SEMBLE_AVAILABLE=false" in body, body
		assert "SEMBLE_FALLBACK target=install reason=install_failed" in result.stderr, result.stderr
		assert result.stdout == "", result.stdout


def test_install_semble_skips_uv_when_matching_binary_is_already_on_path() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		env_file = tmp / "github_env.txt"
		expected_version = "9.9.9"
		_write_executable(
			bin_dir / "semble",
			"#!/usr/bin/env bash\n"
			"if [ \"${1:-}\" = --version ]; then\n"
			f"  echo 'semble/{expected_version}'\n"
			"  exit 0\n"
			"fi\n"
			"exit 0\n",
		)
		result = _run_install(
			{
				"SEMBLE_ENABLED": "true",
				"SEMBLE_VERSION": expected_version,
				"GITHUB_ENV": str(env_file),
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			}
		)
		assert result.returncode == 0, result.stderr
		body = env_file.read_text(encoding="utf-8")
		assert "SEMBLE_AVAILABLE=true" in body, body
		assert f"status=already_installed version={expected_version} source=path" in result.stderr, result.stderr
		assert "uv_unavailable" not in result.stderr, result.stderr


def test_install_semble_skips_reinstall_for_matching_uv_tool() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		env_file = tmp / "github_env.txt"
		_write_executable(bin_dir / "semble", "#!/usr/bin/env bash\nexit 0\n")
		_write_executable(
			bin_dir / "uv",
			"#!/usr/bin/env bash\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = list ]; then\n"
			"  echo 'semble v0.1.3'\n"
			"  echo '- semble'\n"
			"  exit 0\n"
			"fi\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = dir ] && [ \"$3\" = --bin ]; then\n"
			f"  echo '{bin_dir}'\n"
			"  exit 0\n"
			"fi\n"
			"if [ \"$1\" = tool ] && [ \"$2\" = install ]; then\n"
			"  echo 'unexpected reinstall' >&2\n"
			"  exit 99\n"
			"fi\n"
			"exit 0\n",
		)
		result = _run_install(
			{
				"SEMBLE_ENABLED": "true",
				"GITHUB_ENV": str(env_file),
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			}
		)
		assert result.returncode == 0, result.stderr
		body = env_file.read_text(encoding="utf-8")
		assert "SEMBLE_AVAILABLE=true" in body, body
		assert "already_installed" in result.stderr, result.stderr
		assert "unexpected reinstall" not in result.stderr, result.stderr


def test_semble_query_block_keeps_prompt_output_on_stdout_only() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		runtime_dir = tmp / "runtime"
		index_dir = runtime_dir / ".semble-index"
		index_dir.mkdir(parents=True)
		(index_dir / "repo_root").write_text(str(tmp), encoding="utf-8")
		_write_executable(
			bin_dir / "semble",
			"#!/usr/bin/env bash\n"
			"[ \"$1\" = query ] || { echo 'wrong subcommand' >&2; exit 9; }\n"
			"[ \"$2\" = 'needle query' ] || { echo 'wrong query text' >&2; exit 10; }\n"
			"[ \"$3\" = --index ] || { echo 'missing index flag' >&2; exit 11; }\n"
			"[ \"$5\" = --top-k ] || { echo 'missing top-k flag' >&2; exit 12; }\n"
			"[ \"$6\" = 3 ] || { echo 'wrong chunk count' >&2; exit 13; }\n"
			"[ \"$7\" = --format ] || { echo 'missing format flag' >&2; exit 14; }\n"
			"[ \"$8\" = text ] || { echo 'wrong format' >&2; exit 15; }\n"
			"printf 'relevant prompt block\\nsecond line\\n'\n",
		)
		result = _run_helper(
			{
				"RUNTIME_DIR": str(runtime_dir),
				"SEMBLE_INDEX_AVAILABLE": "true",
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			}
		)
		assert result.returncode == 0, result.stderr
		assert "=== SEMBLE: Implement Context ===" in result.stdout, result.stdout
		assert "relevant prompt block" in result.stdout, result.stdout
		assert "SEMBLE_QUERY target=Implement Context" in result.stderr, result.stderr
		assert "SEMBLE_QUERY" not in result.stdout, result.stdout
		assert "SEMBLE_FALLBACK" not in result.stdout, result.stdout


def test_semble_query_block_falls_back_cleanly_when_index_disabled() -> None:
	result = _run_helper({"SEMBLE_INDEX_AVAILABLE": "false"})
	assert result.returncode == 1, result.returncode
	assert result.stdout == "", result.stdout
	assert "SEMBLE_FALLBACK target=Implement Context reason=index_unavailable" in result.stderr, result.stderr


def test_semble_query_block_rejects_implicit_root_index_dir() -> None:
	result = _run_helper({"SEMBLE_INDEX_AVAILABLE": "true", "RUNTIME_DIR": "", "SEMBLE_INDEX_DIR": ""})
	assert result.returncode == 1, result.returncode
	assert result.stdout == "", result.stdout
	assert "SEMBLE_FALLBACK target=Implement Context reason=index_dir_unset" in result.stderr, result.stderr


def test_semble_query_block_query_failures_do_not_leak_to_stdout() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		runtime_dir = tmp / "runtime"
		index_dir = runtime_dir / ".semble-index"
		index_dir.mkdir(parents=True)
		(index_dir / "repo_root").write_text(str(tmp), encoding="utf-8")
		_write_executable(
			bin_dir / "semble",
			"#!/usr/bin/env bash\n"
			"[ \"$1\" = query ] || exit 8\n"
			"echo 'backend exploded' >&2\n"
			"exit 7\n",
		)
		result = _run_helper(
			{
				"RUNTIME_DIR": str(runtime_dir),
				"SEMBLE_INDEX_AVAILABLE": "true",
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			}
		)
		assert result.returncode == 1, result.returncode
		assert result.stdout == "", result.stdout
		assert "SEMBLE_FALLBACK target=Implement Context reason=query_failed detail=backend exploded" in result.stderr, result.stderr


def test_semble_query_block_timeouts_map_to_timeout_fallback() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		runtime_dir = tmp / "runtime"
		index_dir = runtime_dir / ".semble-index"
		index_dir.mkdir(parents=True)
		(index_dir / "repo_root").write_text(str(tmp), encoding="utf-8")
		_write_executable(
			bin_dir / "semble",
			"#!/usr/bin/env bash\n"
			"[ \"$1\" = query ] || exit 8\n"
			"sleep 10\n",
		)
		result = _run_helper(
			{
				"RUNTIME_DIR": str(runtime_dir),
				"SEMBLE_INDEX_AVAILABLE": "true",
				"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			}
		)
		assert result.returncode == 1, result.returncode
		assert result.stdout == "", result.stdout
		assert "SEMBLE_FALLBACK target=Implement Context reason=query_timeout" in result.stderr, result.stderr


def test_implement_workflow_stages_and_gates_semble_foundation() -> None:
	body = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	assert "write_codex_config.sh install_semble.sh semble_helpers.sh; do" in body, "workflow must stage the Semble support scripts"
	assert "SEMBLE_ENABLED" in body, "workflow must expose SEMBLE_ENABLED"
	assert "IMPLEMENTATION_SEMBLE_QUERY_FILE" in body, "workflow must export a runtime-local Semble query file"
	setup_step = _workflow_step_block("Setup uv for Semble")
	assert "uses: astral-sh/setup-uv@v3" in setup_step, "workflow must install uv when Semble is enabled"
	install_step = _workflow_step_block("Install Semble")
	assert "bash scripts/install_semble.sh" in install_step, "workflow must run the install helper"
	index_step = _workflow_step_block("Build Semble index")
	assert 'workspace_root="${GITHUB_WORKSPACE:-}"' in index_step, "workflow must guard the workspace path before indexing"
	assert "reason=workspace_unavailable" in index_step, "workflow must fail soft when the workspace path is unavailable"
	assert 'SEMBLE_INDEX_DIR="${RUNTIME_DIR}/.semble-index"' in index_step, "workflow must build the workspace-local index directory"
	assert 'semble index . --out "${SEMBLE_INDEX_DIR}"' in index_step, "workflow must build a real Semble index before advertising it"
	query_step = _workflow_step_block("Build implementation Semble query file")
	assert 'echo "ISSUE DESCRIPTION"' in query_step, "workflow must derive the Semble query file from issue description"
	assert 'cat "${CLARIFICATION_ANSWERS_FILE}"' in query_step, "workflow must include clarification answers in the Semble query file"
	assert 'cat "${PLAN_FILE}"' in query_step, "workflow must include the approved implementation plan in the Semble query file"
	assert '--semble-index "${SEMBLE_INDEX_DIR:-}"' in body, "targeted-file-context must receive the Semble index path"
	assert '--semble-query-from "${IMPLEMENTATION_SEMBLE_QUERY_FILE}"' in body, "targeted-file-context must receive the runtime-local query file"
	assert '--semble-max-chunks "${SEMBLE_TARGETED_FILE_MAX_CHUNKS:-6}"' in body, "targeted-file-context must receive the Semble chunk cap"
	assert '--semble-fallback marker' in body, "targeted-file-context must keep marker fallback as the default fail-open behavior"


def test_review_autofix_workflow_stages_and_gates_semble_foundation() -> None:
	body = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")
	assert "SEMBLE_ENABLED" in body, "review workflow must expose SEMBLE_ENABLED"
	assert "install_semble.sh semble_helpers.sh" in body, "review workflow must stage the Semble support scripts"
	setup_block = _workflow_step_block_from(REVIEW_AUTOFIX_WORKFLOW, "Setup uv for Semble")
	assert "uses: astral-sh/setup-uv@v3" in setup_block, "review workflow must install uv when Semble is enabled"
	install_block = _workflow_step_block_from(REVIEW_AUTOFIX_WORKFLOW, "Install Semble")
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/install_semble.sh"' in install_block, "review workflow must run the install helper from staged support scripts"
	index_block = _workflow_step_block_from(REVIEW_AUTOFIX_WORKFLOW, "Build Semble index")
	assert 'workspace_root="${GITHUB_WORKSPACE:-}"' in index_block, "review workflow must guard the workspace path before indexing"
	assert "reason=workspace_unavailable" in index_block, "review workflow must fail soft when the workspace path is unavailable"
	assert 'SEMBLE_INDEX_DIR="${RUNTIME_DIR}/.semble-index"' in index_block, "review workflow must build the workspace-local index directory"
	assert 'SEMBLE_INDEX target=review_autofix path=${SEMBLE_INDEX_DIR}' in index_block, "review workflow must log review-path Semble index creation"
	assert 'semble index . --out "${SEMBLE_INDEX_DIR}"' in index_block, "review workflow must build a real Semble index before advertising it"


def test_review_consolidate_uses_additive_semble_context() -> None:
	body = REVIEW_CONSOLIDATE_SCRIPT.read_text(encoding="utf-8")
	assert "source \"${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh\"" in body or "source \"scripts/semble_helpers.sh\"" in body, (
		"review_consolidate.sh must source the shared Semble helper for additive retrieval."
	)
	assert '"Consolidator Context"' in body, "review_consolidate.sh must use a stable Semble header label"
	assert 'cat "${CONSOLIDATOR_SEMBLE_CONTEXT_FILE}"' in body, (
		"review_consolidate.sh must append the additive Semble block into the prompt body when available."
	)


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
