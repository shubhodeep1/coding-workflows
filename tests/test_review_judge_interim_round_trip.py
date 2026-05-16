#!/usr/bin/env python3
"""Tests for interim judge artifact emission and prior-round carry-over."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
JUDGE_INTERIM_SCRIPT = REPO_ROOT / "scripts" / "review_run_judge_interim.sh"
REVIEW_APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
CONSOLIDATE_SCRIPT = REPO_ROOT / "scripts" / "review_consolidate.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "review_pipeline"


def _install_mock_codex(
	mock_bin_dir: Path,
	*,
	stdout_text: str = "",
	stderr_text: str = "",
	exit_code: int = 0,
) -> None:
	mock_bin_dir.mkdir(parents=True, exist_ok=True)
	stdout_file = mock_bin_dir / "codex_stdout.txt"
	stderr_file = mock_bin_dir / "codex_stderr.txt"
	stdout_file.write_text(stdout_text, encoding="utf-8")
	stderr_file.write_text(stderr_text, encoding="utf-8")

	(mock_bin_dir / "codex").write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
		"case \" $* \" in\n"
		"\t*\" exec \"*) ;;\n"
		"\t*) echo \"mock-codex supports only exec\" >&2; exit 2 ;;\n"
		"esac\n"
		"if [ -n \"${MOCK_CODEX_STDOUT_FILE:-}\" ] && [ -f \"${MOCK_CODEX_STDOUT_FILE}\" ]; then\n"
		"\tcat \"${MOCK_CODEX_STDOUT_FILE}\"\n"
		"fi\n"
		"if [ -n \"${MOCK_CODEX_STDERR_FILE:-}\" ] && [ -f \"${MOCK_CODEX_STDERR_FILE}\" ]; then\n"
		"\tcat \"${MOCK_CODEX_STDERR_FILE}\" >&2\n"
		"fi\n"
		"exit \"${MOCK_CODEX_EXIT_CODE:-0}\"\n",
		encoding="utf-8",
	)
	(mock_bin_dir / "codex").chmod(0o755)


def _seed_repo_with_autofix_commit(workspace: Path) -> str:
	workspace.mkdir(parents=True, exist_ok=True)
	(workspace / "src").mkdir(parents=True, exist_ok=True)
	module = workspace / "src" / "module.py"
	module.write_text("def run():\n\treturn 'seed'\n", encoding="utf-8")

	subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True)
	for key, value in (
		("user.email", "test@local"),
		("user.name", "test"),
		("commit.gpgsign", "false"),
	):
		subprocess.run(["git", "config", key, value], cwd=workspace, check=True)
	subprocess.run(["git", "add", "src/module.py"], cwd=workspace, check=True)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
		cwd=workspace,
		check=True,
	)

	module.write_text(
		"def run():\n\tif True:\n\t\treturn 'autofix'\n\treturn 'seed'\n",
		encoding="utf-8",
	)
	subprocess.run(["git", "add", "src/module.py"], cwd=workspace, check=True)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "[ai-autofix] adjust module"],
		cwd=workspace,
		check=True,
	)
	return subprocess.check_output(
		["git", "rev-parse", "HEAD"],
		cwd=workspace,
		text=True,
	).strip()


def _base_env(workspace: Path, runtime_dir: Path, mock_bin_dir: Path) -> dict[str, str]:
	home_dir = workspace / "home"
	(home_dir / ".codex").mkdir(parents=True, exist_ok=True)
	(home_dir / ".codex" / "config.toml").write_text(
		'model_reasoning_effort = "low"\n',
		encoding="utf-8",
	)
	linked_issue_file = runtime_dir / "linked_issue_context.txt"
	linked_issue_file.write_text(
		"Implement the round artifact and preserve prior carry-over.\n",
		encoding="utf-8",
	)
	pr_meta_file = runtime_dir / "pr_meta.json"
	pr_meta_file.write_text(
		json.dumps({"title": "Review autofix", "body": "Interim judge fixture"}) + "\n",
		encoding="utf-8",
	)

	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["HOME"] = str(home_dir)
	env["PATH"] = f"{mock_bin_dir}:{env.get('PATH', '')}"
	env["SUPPORT_ROOT_DIR"] = str(REPO_ROOT)
	env["SUPPORT_SCRIPTS_DIR"] = str(REPO_ROOT / "scripts")
	env["SUPPORT_PROMPTS_DIR"] = str(REPO_ROOT / "prompts")
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["LINKED_ISSUE_CONTEXT_FILE"] = str(linked_issue_file)
	env["PR_META_FILE"] = str(pr_meta_file)
	env["PR_NUMBER"] = "4242"
	env["ROUND_NUMBER"] = "0"
	env["HEAD_SHA"] = "stale-head-sha"
	env["JUDGE_INTERIM_REASONING"] = "low"
	env["JUDGE_INTERIM_TIMEOUT_S"] = "10"
	env["MODEL_EDITOR"] = "openai/gpt-5.4"
	env["TOOL_CALL_BUDGET_JUDGE"] = "20"
	env["MOCK_CODEX_STDOUT_FILE"] = str(mock_bin_dir / "codex_stdout.txt")
	env["MOCK_CODEX_STDERR_FILE"] = str(mock_bin_dir / "codex_stderr.txt")
	env["MOCK_CODEX_EXIT_CODE"] = "0"
	return env


def _extract_shell_function(path: Path, function_name: str) -> str:
	lines = path.read_text(encoding="utf-8").splitlines()
	start = None
	for idx, line in enumerate(lines):
		if line.startswith(f"{function_name}()"):
			start = idx
			break
	if start is None:
		raise AssertionError(f"missing function {function_name} in {path}")

	brace_line = start + 1
	while brace_line < len(lines) and lines[brace_line].strip() != "{":
		brace_line += 1
	if brace_line >= len(lines):
		raise AssertionError(f"missing opening brace for {function_name}")

	in_heredoc: str | None = None
	depth = 1
	end = brace_line + 1
	while end < len(lines):
		stripped = lines[end].strip()
		if in_heredoc is not None:
			if stripped == in_heredoc:
				in_heredoc = None
			end += 1
			continue
		match = re.search(r"<<[-]?'?([A-Za-z_][A-Za-z0-9_]*)'?", lines[end])
		if match:
			in_heredoc = match.group(1)
		if stripped == "{":
			depth += 1
		elif stripped == "}":
			depth -= 1
			if depth == 0:
				return "\n".join(lines[start : end + 1]) + "\n"
		end += 1

	raise AssertionError(f"could not extract function {function_name}")


def _run_prepare_priors_function(
	workspace: Path,
	runtime_dir: Path,
	*,
	autofix_iteration: int,
) -> subprocess.CompletedProcess[str]:
	function_text = _extract_shell_function(REVIEW_APPLY_FIXES, "prepare_judge_interim_priors")
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["PR_NUMBER"] = "4242"
	env["AUTOFIX_ITERATION"] = str(autofix_iteration)
	env["JUDGE_INTERIM_PRIORS_FILE"] = str(runtime_dir / "judge_interim_priors.txt")
	return subprocess.run(
		["bash", "-c", function_text + "\nprepare_judge_interim_priors\n"],
		cwd=workspace,
		env=env,
		capture_output=True,
		text=True,
	)


def test_review_run_judge_interim_writes_round_artifact() -> None:
	with tempfile.TemporaryDirectory(prefix="judge_interim_ok_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		current_head = _seed_repo_with_autofix_commit(workspace)
		_install_mock_codex(
			mock_bin_dir,
			stdout_text=json.dumps(
				{
					"round": 1,
					"head_sha": current_head,
					"remaining_issues": [
						{
							"id": "src/module.py:2:branch-check",
							"file": "src/module.py",
							"line_start": 2,
							"line_end": 3,
							"symptom": "Branch remains unnecessary (src/module.py:run:2-3)",
							"evidence_quote": "\tif True:\\n\t\treturn 'autofix'",
							"severity": "nice-to-have",
						}
					],
				}
			)
			+ "\n",
		)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(JUDGE_INTERIM_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)

		artifact = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "judge_interim.json"
		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert artifact.exists(), combined_output
		payload = json.loads(artifact.read_text(encoding="utf-8"))
		assert payload["round"] == 1
		assert payload["head_sha"] == current_head
		assert payload["remaining_issues"][0]["file"] == "src/module.py"
		assert "JUDGE_INTERIM_PASS_OK round=1" in combined_output


def test_review_run_judge_interim_fails_open_on_malformed_output() -> None:
	with tempfile.TemporaryDirectory(prefix="judge_interim_failopen_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		_seed_repo_with_autofix_commit(workspace)
		_install_mock_codex(mock_bin_dir, stdout_text='{"action":"fix"}\n')
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(JUDGE_INTERIM_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)

		artifact = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "judge_interim.json"
		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert not artifact.exists(), combined_output
		assert "JUDGE_INTERIM_PASS_FAIL reason=json_parse_failed" in combined_output


def test_prepare_priors_merges_prior_round_into_consolidator_prompt() -> None:
	with tempfile.TemporaryDirectory(prefix="judge_interim_priors_") as td:
		workspace = Path(td)
		workspace.mkdir(parents=True, exist_ok=True)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		prior_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1"
		prior_dir.mkdir(parents=True, exist_ok=True)
		(prior_dir / "judge_interim.json").write_text(
			json.dumps(
				{
					"round": 1,
					"head_sha": "abc123",
					"remaining_issues": [
						{
							"id": "src/module.py:7:carry-over",
							"file": "src/module.py",
							"line_start": 7,
							"line_end": 8,
							"symptom": "Carry-over issue (src/module.py:7-8)",
							"evidence_quote": "return stale_value",
							"severity": "must-fix",
						}
					],
				}
			)
			+ "\n",
			encoding="utf-8",
		)

		prepare_result = _run_prepare_priors_function(
			workspace,
			runtime_dir,
			autofix_iteration=2,
		)
		priors_file = runtime_dir / "judge_interim_priors.txt"
		assert prepare_result.returncode == 0, prepare_result.stderr
		assert priors_file.exists(), prepare_result.stdout + prepare_result.stderr
		assert "<judge_interim_priors>" in priors_file.read_text(encoding="utf-8")
		assert "JUDGE_INTERIM_PRIORS_MERGED count=1" in prepare_result.stdout

		shutil.copy2(FIXTURES / "reviewer_bundle.txt", runtime_dir / "reviewer_bundle.txt")
		mock_bin_dir = workspace / "mock_bin"
		_install_mock_codex(
			mock_bin_dir,
			stdout_text=(FIXTURES / "consolidator_well_formed.txt").read_text(encoding="utf-8"),
		)
		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["PATH"] = f"{mock_bin_dir}:{env.get('PATH', '')}"
		env["RUNTIME_DIR"] = str(runtime_dir)
		env["SUPPORT_PROMPTS_DIR"] = str(REPO_ROOT / "prompts")
		env["JUDGE_INTERIM_PRIORS_FILE"] = str(priors_file)
		env["MOCK_CODEX_STDOUT_FILE"] = str(mock_bin_dir / "codex_stdout.txt")
		env["MOCK_CODEX_STDERR_FILE"] = str(mock_bin_dir / "codex_stderr.txt")
		env["MOCK_CODEX_EXIT_CODE"] = "0"

		consolidate_result = subprocess.run(
			["bash", str(CONSOLIDATE_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)
		assert consolidate_result.returncode == 0, consolidate_result.stderr
		prompt_text = (runtime_dir / "review_consolidator_prompt.txt").read_text(encoding="utf-8")
		assert "<judge_interim_priors>" in prompt_text
		assert "src/module.py" in prompt_text


def test_prepare_priors_is_noop_on_round_one_cache_miss() -> None:
	with tempfile.TemporaryDirectory(prefix="judge_interim_noop_") as td:
		workspace = Path(td)
		workspace.mkdir(parents=True, exist_ok=True)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)

		result = _run_prepare_priors_function(
			workspace,
			runtime_dir,
			autofix_iteration=1,
		)

		priors_file = runtime_dir / "judge_interim_priors.txt"
		assert result.returncode == 0, result.stderr
		assert not priors_file.exists()
		assert "JUDGE_INTERIM_PRIORS_MERGED count=0 source=none" in result.stdout
