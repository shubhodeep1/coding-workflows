#!/usr/bin/env python3
"""Integration tests for the review artifact stage chain."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "review_pipeline"
FLOOR_SCRIPT = REPO_ROOT / "scripts" / "review_floor_rules.sh"
CONSOLIDATE_SCRIPT = REPO_ROOT / "scripts" / "review_consolidate.sh"
PARSER_SCRIPT = REPO_ROOT / "scripts" / "review_parse_consolidator.sh"
LEDGER_SCRIPT = REPO_ROOT / "scripts" / "review_issue_ledger.sh"


def _isolated_test_env(extra_env: dict[str, str] | None = None, *, cwd: Path | None = None) -> dict[str, str]:
	baseline_env = os.environ.copy()
	env = baseline_env.copy()
	if extra_env:
		env.update(extra_env)
	for key in ("BASH_ENV", "ENV"):
		env.pop(key, None)
	for key in ("WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
		if env.get(key) == baseline_env.get(key):
			env.pop(key, None)
	if cwd is not None:
		env["PWD"] = str(cwd)
		env.pop("OLDPWD", None)
	return env


def _seed_workspace_repo(workspace_dir: Path) -> Path:
	workspace_dir.mkdir(parents=True, exist_ok=True)
	runtime_dir = workspace_dir / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src").mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src" / "module.py").write_text(
		"line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\n",
		encoding="utf-8",
	)

	env = _isolated_test_env(cwd=workspace_dir)
	subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace_dir, env=env, check=True)
	for key, value in (
		("user.email", "test@local"),
		("user.name", "test"),
		("commit.gpgsign", "false"),
	):
		subprocess.run(["git", "config", key, value], cwd=workspace_dir, env=env, check=True)
	subprocess.run(["git", "add", "src/module.py"], cwd=workspace_dir, env=env, check=True)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
		cwd=workspace_dir,
		env=env,
		check=True,
	)

	shutil.copy2(FIXTURES / "reviewer_bundle.txt", runtime_dir / "reviewer_bundle.txt")
	return runtime_dir


def _install_mock_opencode(mock_bin_dir: Path, *, consolidator_fixture: str | None) -> Path:
	mock_bin_dir.mkdir(parents=True, exist_ok=True)
	output_file = mock_bin_dir / "opencode_output.txt"
	if consolidator_fixture is None:
		output_file.write_text("", encoding="utf-8")
	else:
		shutil.copy2(FIXTURES / consolidator_fixture, output_file)

	opencode_script = mock_bin_dir / "opencode"
	opencode_script.write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
		"if [ \"${1:-}\" = \"--version\" ]; then printf '1.18.23\\n'; exit 0; fi\n"
		"if [ \"${1:-}\" != \"run\" ]; then echo \"mock-opencode supports only run\" >&2; exit 2; fi\n"
		"if [ -n \"${MOCK_OPENCODE_OUTPUT_FILE:-}\" ] && [ -f \"${MOCK_OPENCODE_OUTPUT_FILE}\" ]; then\n"
		"\tcat \"${MOCK_OPENCODE_OUTPUT_FILE}\"\n"
		"fi\n",
		encoding="utf-8",
	)
	opencode_script.chmod(0o755)
	config_writer = mock_bin_dir / "write_opencode_config.sh"
	config_writer.write_text(
		"#!/usr/bin/env bash\nset -euo pipefail\n"
		"config_path=''\nwhile [ $# -gt 0 ]; do if [ \"$1\" = '--config-path' ]; then config_path=\"$2\"; shift 2; else shift; fi; done\n"
		"mkdir -p \"$(dirname \"${config_path}\")\"\nprintf '{}\\n' > \"${config_path}\"\n",
		encoding="utf-8",
	)
	config_writer.chmod(0o755)
	return config_writer


def _run_stage_chain(
	workspace_dir: Path,
	runtime_dir: Path,
	*,
	mock_bin_dir: Path | None,
	consolidator_enabled: str,
) -> dict[str, subprocess.CompletedProcess[str]]:
	env = _isolated_test_env(
		{
			"PYTHONDONTWRITEBYTECODE": "1",
			"RUNTIME_DIR": str(runtime_dir),
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"SUPPORT_PROMPTS_DIR": str(REPO_ROOT / "prompts"),
			"PR_NUMBER": "4242",
			"AUTOFIX_ITERATION": "1",
			"REVIEW_CONSOLIDATOR_ENABLED": consolidator_enabled,
			"REVIEW_PARSER_FAILOPEN": "1",
			"REVIEW_ISSUES_FILE": str(runtime_dir / "review_issues.txt"),
			"PARSER_STATS_FILE": str(runtime_dir / "parser_stats.txt"),
			"LEDGER_STATUS_FILE": str(runtime_dir / "ledger_status.txt"),
			"FLOOR_TAGS_FILE": str(runtime_dir / "floor_tags.txt"),
			"CONSOLIDATOR_RAW_FILE": str(runtime_dir / "consolidator_raw.txt"),
			"REVIEW_LEDGER_ENABLED": "1",
			"REVIEW_LEDGER_PATH": str(runtime_dir / "review_issue_ledger.txt"),
		},
		cwd=workspace_dir,
	)
	if mock_bin_dir is not None:
		env["MOCK_OPENCODE_OUTPUT_FILE"] = str(mock_bin_dir / "opencode_output.txt")
		env["OPENCODE_CONFIG_WRITER_PATH"] = str(mock_bin_dir / "write_opencode_config.sh")
		env["PATH"] = f"{mock_bin_dir}:{env.get('PATH', '')}"

	results: dict[str, subprocess.CompletedProcess[str]] = {}
	results["floor"] = subprocess.run(
		[
			"bash",
			str(FLOOR_SCRIPT),
			str(runtime_dir / "reviewer_bundle.txt"),
			str(runtime_dir / "floor_tags.txt"),
		],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	results["consolidate"] = subprocess.run(
		["bash", str(CONSOLIDATE_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	results["parse"] = subprocess.run(
		["bash", str(PARSER_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	results["ledger"] = subprocess.run(
		["bash", str(LEDGER_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	return results


def _load_kv_file(path: Path) -> dict[str, str]:
	pairs: dict[str, str] = {}
	for raw in path.read_text(encoding="utf-8").splitlines():
		if "=" not in raw:
			continue
		k, v = raw.split("=", 1)
		pairs[k] = v
	return pairs


def _parse_status_rows(path: Path) -> list[list[str]]:
	rows: list[list[str]] = []
	for raw in path.read_text(encoding="utf-8").splitlines():
		if not raw.strip():
			continue
		rows.append(raw.split("\t"))
	return rows


def _assert_artifacts_present(runtime_dir: Path) -> None:
	for name in ("floor_tags.txt", "review_issues.txt", "parser_stats.txt", "ledger_status.txt"):
		artifact = runtime_dir / name
		assert artifact.exists(), f"missing artifact: {artifact}"


def test_chain_happy_path_with_mocked_consolidator() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_workspace_repo(workspace)
		mock_bin = workspace / "mock_bin"
		_install_mock_opencode(mock_bin, consolidator_fixture="consolidator_well_formed.txt")

		results = _run_stage_chain(workspace, runtime, mock_bin_dir=mock_bin, consolidator_enabled="1")
		for stage, result in results.items():
			assert result.returncode == 0, f"{stage} failed: {result.stderr}"

		_assert_artifacts_present(runtime)
		stats = _load_kv_file(runtime / "parser_stats.txt")
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		status_rows = _parse_status_rows(runtime / "ledger_status.txt")

		assert stats["parse_failed"] == "0"
		assert stats["parsed_blocks"] == "1"
		assert stats["passthrough_blocks"] == "2"
		assert stats["anchors_total"] == "4"
		assert stats["anchors_covered"] == "2"
		assert "=== ISSUE 001 ===" in issues
		assert "=== ISSUE PASSTHROUGH 002 ===" in issues
		assert len(status_rows) == 3
		assert all(row[1] == "NEW" for row in status_rows)
		assert any(row[4] == "CORRECTNESS & LOGIC" for row in status_rows)


def test_chain_fail_open_when_consolidator_disabled() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_workspace_repo(workspace)
		results = _run_stage_chain(workspace, runtime, mock_bin_dir=None, consolidator_enabled="0")
		for stage, result in results.items():
			assert result.returncode == 0, f"{stage} failed: {result.stderr}"

		_assert_artifacts_present(runtime)
		stats = _load_kv_file(runtime / "parser_stats.txt")
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		status_rows = _parse_status_rows(runtime / "ledger_status.txt")

		assert "disabled=1" in results["consolidate"].stderr
		assert stats["parse_failed"] == "1"
		assert stats["parse_error"] == "no_issue_markers"
		assert stats["parsed_blocks"] == "0"
		assert stats["passthrough_blocks"] == "4"
		assert stats["anchors_total"] == "4"
		assert stats["anchors_covered"] == "0"
		assert "=== ISSUE 001 ===" not in issues
		assert "=== ISSUE PASSTHROUGH 004 ===" in issues
		assert len(status_rows) == 4
		assert all(row[4] == "UNKNOWN_LENS" for row in status_rows)


def test_chain_fail_open_when_consolidator_returns_empty() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_workspace_repo(workspace)
		mock_bin = workspace / "mock_bin"
		_install_mock_opencode(mock_bin, consolidator_fixture=None)

		results = _run_stage_chain(workspace, runtime, mock_bin_dir=mock_bin, consolidator_enabled="1")
		for stage, result in results.items():
			assert result.returncode == 0, f"{stage} failed: {result.stderr}"

		_assert_artifacts_present(runtime)
		stats = _load_kv_file(runtime / "parser_stats.txt")
		status_rows = _parse_status_rows(runtime / "ledger_status.txt")

		assert "failopen=1" in results["consolidate"].stderr
		assert (runtime / "consolidator_raw.txt").read_text(encoding="utf-8") == ""
		assert stats["parse_failed"] == "1"
		assert stats["parse_error"] == "no_issue_markers"
		assert stats["passthrough_blocks"] == "4"
		assert len(status_rows) == 4
		assert all(row[1] == "NEW" for row in status_rows)


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
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
