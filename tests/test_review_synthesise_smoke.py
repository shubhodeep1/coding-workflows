#!/usr/bin/env python3
"""Tests for behavioural smoke synthesis from judge-interim findings."""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SYNTH_SCRIPT = REPO_ROOT / "scripts" / "review_synthesise_smoke.sh"
STAGE_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
VALIDATE_DRIVER = REPO_ROOT / "scripts" / "validate_driver.sh"
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
INTERNAL_VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "internal-validate.yml"
MARK_STABLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mark-stable.yml"
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
TEST_AND_MARK_STABLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"


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
		f"exit \"${{MOCK_CODEX_EXIT_CODE:-{exit_code}}}\"\n",
		encoding="utf-8",
	)
	(mock_bin_dir / "codex").chmod(0o755)


def _install_mock_timeout(mock_bin_dir: Path) -> Path:
	mock_bin_dir.mkdir(parents=True, exist_ok=True)
	timeout_capture = mock_bin_dir / "timeout_duration.txt"
	(mock_bin_dir / "timeout").write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
		"while [ \"$#\" -gt 0 ]; do\n"
		"\tcase \"$1\" in\n"
		"\t\t--signal=*|--kill-after=*) shift ;;\n"
		"\t\t--) shift; break ;;\n"
		"\t\t*) break ;;\n"
		"\tesac\n"
		"done\n"
		"duration=\"${1:-}\"\n"
		"shift || true\n"
		"printf '%s\\n' \"$duration\" > \"${MOCK_TIMEOUT_DURATION_FILE}\"\n"
		"exec \"$@\"\n",
		encoding="utf-8",
	)
	(mock_bin_dir / "timeout").chmod(0o755)
	return timeout_capture


def _seed_repo_with_autofix_commit(workspace: Path) -> str:
	workspace.mkdir(parents=True, exist_ok=True)
	(workspace / "src").mkdir(parents=True, exist_ok=True)
	module = workspace / "src" / "module.py"
	module.write_text("def run():\n\treturn 'seed'\n", encoding="utf-8")

	subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True, timeout=60)
	for key, value in (
		("user.email", "test@local"),
		("user.name", "test"),
		("commit.gpgsign", "false"),
	):
		subprocess.run(["git", "config", key, value], cwd=workspace, check=True, timeout=60)
	subprocess.run(["git", "add", "src/module.py"], cwd=workspace, check=True, timeout=60)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
		cwd=workspace,
		check=True,
		timeout=60,
	)

	module.write_text(
		"def run():\n\tif True:\n\t\treturn 'autofix'\n\treturn 'seed'\n",
		encoding="utf-8",
	)
	subprocess.run(["git", "add", "src/module.py"], cwd=workspace, check=True, timeout=60)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "[ai-autofix] adjust module"],
		cwd=workspace,
		check=True,
		timeout=60,
	)
	return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True, timeout=60).strip()


def _base_env(workspace: Path, runtime_dir: Path, mock_bin_dir: Path) -> dict[str, str]:
	home_dir = workspace / "home"
	(home_dir / ".codex").mkdir(parents=True, exist_ok=True)
	(home_dir / ".codex" / "config.toml").write_text(
		'model_reasoning_effort = "low"\n',
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
	env["PR_NUMBER"] = "4242"
	env["ROUND_NUMBER"] = "0"
	env["HEAD_SHA"] = "stale-head-sha"
	env["BEHAVIOURAL_SMOKE_LANG"] = "python"
	env["MOCK_CODEX_STDOUT_FILE"] = str(mock_bin_dir / "codex_stdout.txt")
	env["MOCK_CODEX_STDERR_FILE"] = str(mock_bin_dir / "codex_stderr.txt")
	return env


def _write_judge_artifact(workspace: Path, head_sha: str, remaining_issues: list[dict[str, object]]) -> Path:
	artifact = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "judge_interim.json"
	artifact.parent.mkdir(parents=True, exist_ok=True)
	artifact.write_text(
		json.dumps(
			{
				"round": 1,
				"head_sha": head_sha,
				"remaining_issues": remaining_issues,
			},
			indent=2,
		)
		+ "\n",
		encoding="utf-8",
	)
	return artifact


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
		if stripped == "{" or stripped.startswith("{ "):
			depth += 1
		elif stripped == "}" or stripped.startswith("}"):
			depth -= 1
			if depth == 0:
				return "\n".join(lines[start : end + 1]) + "\n"
		end += 1

	raise AssertionError(f"could not extract function {function_name}")


def _make_issue(issue_id: str, line_start: int, line_end: int, symptom: str) -> dict[str, object]:
	return {
		"id": issue_id,
		"file": "src/module.py",
		"line_start": line_start,
		"line_end": line_end,
		"symptom": symptom,
		"evidence_quote": "return 'autofix'",
		"severity": "must-fix",
	}


def test_review_synthesise_smoke_writes_manifest_and_cached_wrappers() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_ok_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[
				_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix"),
				_make_issue("src/module.py:4:stale-value", 4, 4, "Fallback branch still uses stale value"),
			],
		)
		_install_mock_codex(
			mock_bin_dir,
			stdout_text=json.dumps(
				[
					{
						"path": "validation/tests/suggested_branch_check.sh",
						"content": "python3 - <<'PY'\nprint('still present')\nraise SystemExit(1)\nPY",
						"expected_to_fail_until_fixed": True,
					},
					{
						"path": "validation/tests/suggested_stale_value.sh",
						"content": "node - <<'NODE'\nconsole.log('cleared')\nprocess.exit(0)\nNODE",
						"expected_to_fail_until_fixed": True,
					},
				]
			)
			+ "\n",
		)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(SYNTH_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)

		manifest = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "synth" / "synth_round_1_manifest.json"
		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert manifest.exists(), combined_output
		payload = json.loads(manifest.read_text(encoding="utf-8"))
		assert payload["round"] == 1
		assert payload["head_sha"] == head_sha
		assert payload["language"] == "python"
		assert len(payload["files"]) == 2
		assert [row["slug"] for row in payload["files"]] == [
			"src_module_py_2_branch_check",
			"src_module_py_4_stale_value",
		]
		assert payload["files"][0]["target_relpath"] == "validation/tests/synth_round_1_src_module_py_2_branch_check.sh"
		assert payload["files"][1]["target_relpath"] == "validation/tests/synth_round_1_src_module_py_4_stale_value.sh"
		for row in payload["files"]:
			wrapper_path = workspace / row["cache_relpath"]
			assert wrapper_path.exists(), wrapper_path
			wrapper_text = wrapper_path.read_text(encoding="utf-8")
			assert "BEHAVIOURAL_SMOKE_PRESENT_PASSED" in wrapper_text
			assert "BEHAVIOURAL_SMOKE_PRESENT_FAILED" in wrapper_text
			assert "BEHAVIOURAL_SMOKE_PRESENT_INCONCLUSIVE" in wrapper_text
		assert "BEHAVIOURAL_SMOKE_SYNTHESISED count=2 round=1 language=python" in combined_output


def test_review_synthesise_smoke_fails_open_on_malformed_output() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_failopen_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix")],
		)
		_install_mock_codex(mock_bin_dir, stdout_text='{"action":"fix"}\n')
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(SYNTH_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)

		manifest = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "synth" / "synth_round_1_manifest.json"
		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert not manifest.exists(), combined_output
		assert "BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=json_parse_failed round=1" in combined_output


def test_review_synthesise_smoke_surfaces_codex_stderr_on_failure() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_stderr_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix")],
		)
		_install_mock_codex(
			mock_bin_dir,
			stdout_text="",
			stderr_text="mock model lookup failed\n",
			exit_code=1,
		)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(SYNTH_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)

		manifest = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "synth" / "synth_round_1_manifest.json"
		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert not manifest.exists(), combined_output
		assert "BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=llm_failed round=1" in combined_output
		assert "BEHAVIOURAL_SMOKE_SYNTHESIS_STDERR_BEGIN" in result.stderr
		assert "mock model lookup failed" in result.stderr
		assert "BEHAVIOURAL_SMOKE_SYNTHESIS_STDERR_END" in result.stderr


def test_review_synthesise_smoke_fails_open_on_wrong_item_count() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_count_mismatch_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix")],
		)
		_install_mock_codex(
			mock_bin_dir,
			stdout_text=json.dumps(
				[
					{
						"path": "validation/tests/first.sh",
						"content": "echo first\nexit 1",
						"expected_to_fail_until_fixed": True,
					},
					{
						"path": "validation/tests/second.sh",
						"content": "echo second\nexit 1",
						"expected_to_fail_until_fixed": True,
					},
				]
			)
			+ "\n",
		)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(SYNTH_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)

		manifest = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "synth" / "synth_round_1_manifest.json"
		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert not manifest.exists(), combined_output
		assert "could not validate synthesis output" in combined_output
		assert "BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=json_parse_failed round=1" in combined_output


def test_review_synthesise_smoke_rejects_unsafe_shell_constructs() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_unsafe_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix")],
		)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)
		manifest = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "synth" / "synth_round_1_manifest.json"

		for content in (
			'printf hello#;eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'AWK=eval\n$AWK "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'coproc eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'coproc\\\n eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'coproc /bin/bash -c "printf unsafe"\nbehavioural_smoke_inconclusive "unsafe"',
			'coproc env eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'coproc\\\n env eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'/bin/bash -c "printf unsafe"\nbehavioural_smoke_inconclusive "unsafe"',
			'/usr/bin/env eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'result="$(whoami)"\nbehavioural_smoke_inconclusive "unsafe"',
			'bash -c "printf unsafe"\nbehavioural_smoke_inconclusive "unsafe"',
			'eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'exec /bin/false\nbehavioural_smoke_inconclusive "unsafe"',
			'env -S "printf unsafe"\nbehavioural_smoke_inconclusive "unsafe"',
			'env --split-string="printf unsafe"\nbehavioural_smoke_inconclusive "unsafe"',
			'source ./payload.sh\nbehavioural_smoke_inconclusive "unsafe"',
			'. ./payload.sh\nbehavioural_smoke_inconclusive "unsafe"',
			'cmd & eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'cmd |& eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'> /dev/null eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'2>/dev/null eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'env eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'env -u SOME_VAR eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'sudo eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'sudo -u root eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'time eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'time -o /dev/null eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'time --format %E eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'time -p eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'time /usr/bin/env eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'! eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'timeout 1 eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'timeout 10s eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'timeout -s KILL 1 eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'xargs eval\nbehavioural_smoke_inconclusive "unsafe"',
			'xargs -d , eval\nbehavioural_smoke_inconclusive "unsafe"',
			'command -p eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'builtin -- source ./payload.sh\nbehavioural_smoke_inconclusive "unsafe"',
			'if eval "$PAYLOAD"; then behavioural_smoke_inconclusive "unsafe"; fi',
		):
			_install_mock_codex(
				mock_bin_dir,
				stdout_text=json.dumps(
					[
						{
							"path": "validation/tests/unsafe.sh",
							"content": content,
							"expected_to_fail_until_fixed": True,
						}
					]
				)
				+ "\n",
			)

			result = subprocess.run(
				["bash", str(SYNTH_SCRIPT)],
				cwd=workspace,
				env=env,
				capture_output=True,
				text=True,
				timeout=60,
			)

			combined_output = result.stdout + result.stderr
			assert result.returncode == 0, f"{content}: {combined_output}"
			assert "could not validate synthesis output" in combined_output, f"{content}: {combined_output}"
			assert "BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=json_parse_failed round=1" in combined_output, f"{content}: {combined_output}"
			assert not manifest.exists(), f"{content}: {combined_output}"


def test_review_synthesise_smoke_invalid_lang_falls_back_to_repo_detection() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_lang_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		(workspace / "requirements-dev.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
		_write_judge_artifact(workspace, head_sha, [])
		_install_mock_codex(mock_bin_dir, stdout_text="[]\n")
		env = _base_env(workspace, runtime_dir, mock_bin_dir)
		env["BEHAVIOURAL_SMOKE_LANG"] = " pythoon \n second-line "

		result = subprocess.run(
			["bash", str(SYNTH_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)

		manifest = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "synth" / "synth_round_1_manifest.json"
		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		payload = json.loads(manifest.read_text(encoding="utf-8"))
		assert payload["language"] == "python"
		assert "::warning::Invalid BEHAVIOURAL_SMOKE_LANG 'pythoon%0Asecond-line'; falling back to repo auto-detection." in result.stdout
		assert "Invalid BEHAVIOURAL_SMOKE_LANG" not in result.stderr
		assert "BEHAVIOURAL_SMOKE_SYNTHESISED count=0 round=1 language=python" in combined_output


def test_behavioural_smoke_emit_warning_is_best_effort_when_fd3_is_closed() -> None:
	function_text = _extract_shell_function(SYNTH_SCRIPT, "behavioural_smoke_emit_warning")
	result = subprocess.run(
		[
			"bash",
			"-c",
			"set -euo pipefail\n"
			"exec 3>&-\n"
			+ function_text
			+ "behavioural_smoke_emit_warning $'broken\\nwarning'\n"
			+ "echo after\n",
		],
		capture_output=True,
		text=True,
		timeout=60,
	)

	assert result.returncode == 0, result.stdout + result.stderr
	assert result.stdout.strip() == "after"
	assert result.stderr == ""


def test_review_synthesise_smoke_clamps_large_timeout_values() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_timeout_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix")],
		)
		_install_mock_codex(
			mock_bin_dir,
			stdout_text=json.dumps(
				[
					{
						"path": "validation/tests/suggested_branch_check.sh",
						"content": "echo still-present\nexit 1",
						"expected_to_fail_until_fixed": True,
					}
				]
			)
			+ "\n",
		)
		timeout_capture = _install_mock_timeout(mock_bin_dir)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)
		env["BEHAVIOURAL_SMOKE_TIMEOUT_S"] = "99999999999999999999"
		env["MOCK_TIMEOUT_DURATION_FILE"] = str(timeout_capture)

		result = subprocess.run(
			["bash", str(SYNTH_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)

		manifest = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "synth" / "synth_round_1_manifest.json"
		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert manifest.exists(), combined_output
		assert timeout_capture.read_text(encoding="utf-8").strip() == "120"
		assert "BEHAVIOURAL_SMOKE_SYNTHESISED count=1 round=1 language=python" in combined_output


def test_generated_wrappers_report_pass_fail_and_inconclusive_advisory_states() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_wrappers_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		issues = [
			_make_issue("src/module.py:2:cleared", 2, 2, "Cleared issue"),
			_make_issue("src/module.py:3:present", 3, 3, "Present issue"),
			_make_issue("src/module.py:4:unknown", 4, 4, "Unknown issue"),
		]
		_write_judge_artifact(workspace, head_sha, issues)
		_install_mock_codex(
			mock_bin_dir,
			stdout_text=json.dumps(
				[
					{
						"path": "validation/tests/cleared.sh",
						"content": "echo cleared\nexit 0",
						"expected_to_fail_until_fixed": True,
					},
					{
						"path": "validation/tests/present.sh",
						"content": "echo still-present\nexit 1",
						"expected_to_fail_until_fixed": True,
					},
					{
						"path": "validation/tests/unknown.sh",
						"content": "echo inconclusive\nexit 2",
						"expected_to_fail_until_fixed": True,
					},
				]
			)
			+ "\n",
		)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(SYNTH_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		assert result.returncode == 0, result.stdout + result.stderr

		manifest = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "synth" / "synth_round_1_manifest.json"
		payload = json.loads(manifest.read_text(encoding="utf-8"))
		markers = {
			"src/module.py:2:cleared": "BEHAVIOURAL_SMOKE_PRESENT_PASSED",
			"src/module.py:3:present": "BEHAVIOURAL_SMOKE_PRESENT_FAILED",
			"src/module.py:4:unknown": "BEHAVIOURAL_SMOKE_PRESENT_INCONCLUSIVE",
		}
		for row in payload["files"]:
			wrapper_path = workspace / row["cache_relpath"]
			wrapper_result = subprocess.run(
				["bash", str(wrapper_path)],
				cwd=workspace,
				capture_output=True,
				text=True,
				timeout=60,
			)
			assert wrapper_result.returncode == 0, wrapper_result.stdout + wrapper_result.stderr
			assert markers[row["issue_id"]] in wrapper_result.stdout
			assert "ok 1 - behavioural smoke" in wrapper_result.stdout


def test_review_autofix_workflow_wires_behavioural_smoke_after_interim_judge() -> None:
	workflow = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")
	stage_helper = STAGE_HELPER.read_text(encoding="utf-8")
	validate_workflow = VALIDATE_WORKFLOW.read_text(encoding="utf-8")
	assert "BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED: ${{ vars.BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED || 'false' }}" in workflow
	assert "BEHAVIOURAL_SMOKE_LANG: ${{ vars.BEHAVIOURAL_SMOKE_LANG || '' }}" in workflow
	assert "BEHAVIOURAL_SMOKE_MODEL: ${{ vars.BEHAVIOURAL_SMOKE_MODEL || 'openai/gpt-5.6-luna' }}" in workflow
	assert "BEHAVIOURAL_SMOKE_TIMEOUT_S: ${{ vars.BEHAVIOURAL_SMOKE_TIMEOUT_S || '120' }}" in workflow
	assert "VALIDATION_INCLUDE_SYNTHESISED: ${{ vars.VALIDATION_INCLUDE_SYNTHESISED || 'true' }}" in workflow
	assert "VALIDATION_INCLUDE_SYNTHESISED: ${{ vars.VALIDATION_INCLUDE_SYNTHESISED || 'true' }}" in validate_workflow
	bootstrap_line = next(
		(line for line in stage_helper.splitlines() if "REQUIRED_BOOTSTRAP_SCRIPTS=" in line),
		"",
	)
	assert "review_synthesise_smoke.sh" in bootstrap_line
	assert "prompts/behavioural-smoke-synthesise.txt" in stage_helper
	judge_idx = workflow.find("- name: Run interim judge")
	synth_idx = workflow.find("- name: Synthesize behavioural smoke")
	ledger_idx = workflow.find("- name: Save review-issue ledger")
	assert judge_idx != -1, "review_autofix.yml missing the Run interim judge step"
	assert synth_idx != -1, "review_autofix.yml missing the behavioural smoke synthesis step"
	assert ledger_idx != -1, "review_autofix.yml missing the Save review-issue ledger step"
	assert judge_idx < synth_idx < ledger_idx, (
		"Behavioural smoke synthesis must run after interim judge and before the review-runtime cache save."
	)
	step_block = workflow[synth_idx : synth_idx + 600]
	assert "env.BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED == 'true'" in step_block
	assert "env.JUDGE_INTERIM_ENABLED == 'true'" in step_block
	assert 'timeout --signal=TERM --kill-after=30s -- "${BEHAVIOURAL_SMOKE_TIMEOUT_S}"' in SYNTH_SCRIPT.read_text(encoding="utf-8")
	assert '--model "${BEHAVIOURAL_SMOKE_MODEL}"' in SYNTH_SCRIPT.read_text(encoding="utf-8")


def test_review_synthesise_smoke_is_registered_in_ci_workflows() -> None:
	for workflow_path in (CI_WORKFLOW, MARK_STABLE_WORKFLOW, TEST_AND_MARK_STABLE_WORKFLOW):
		workflow = workflow_path.read_text(encoding="utf-8")
		assert "PYTHONDONTWRITEBYTECODE=1 python3 tests/test_review_synthesise_smoke.py" in workflow, workflow_path


def test_validate_workflows_restore_cached_behavioural_smoke_artifacts() -> None:
	validate_workflow = VALIDATE_WORKFLOW.read_text(encoding="utf-8")
	internal_validate_workflow = INTERNAL_VALIDATE_WORKFLOW.read_text(encoding="utf-8")
	review_workflow = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")

	assert "description: \"Review PR number for restoring cached behavioural smoke artifacts (0 to auto-detect from tracking issue)\"" in validate_workflow
	assert "- name: Normalize behavioural smoke include flag" in validate_workflow
	assert "id: behavioural_smoke_gate" in validate_workflow
	assert "- name: Resolve behavioural smoke source PR" in validate_workflow
	assert "if: steps.behavioural_smoke_gate.outputs.enabled == 'true'" in validate_workflow
	assert "- name: Restore behavioural smoke runtime cache" in validate_workflow
	assert ".ai/review_runtime/" in validate_workflow
	assert "review-ledger-${{ github.repository }}-pr-${{ steps.behavioural_smoke_pr.outputs.pr_number }}-" in validate_workflow

	assert "pr_number:" in internal_validate_workflow
	assert "pr_number: ${{ inputs.pr_number || '0' }}" in internal_validate_workflow

	dispatch_idx = review_workflow.find("Dispatching standalone validation for linked issue")
	assert dispatch_idx != -1, "review_autofix.yml missing standalone validation dispatch"
	dispatch_block = review_workflow[dispatch_idx : dispatch_idx + 500]
	assert '-f tracking_issue="0"' in dispatch_block
	assert '-f pr_number="${PR_NUMBER}"' in dispatch_block


def test_validate_driver_can_exclude_synthesised_smoke_files() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_driver_synth_gate_") as td:
		test_dir = Path(td) / "validation" / "tests"
		test_dir.mkdir(parents=True, exist_ok=True)
		for name in ("00_canary.sh", "10_regular.sh", "synth_round_1_issue.sh", "_helpers.sh"):
			path = test_dir / name
			path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
			path.chmod(0o755)

		function_text = _extract_shell_function(VALIDATE_DRIVER, "discover_tests")
		base_script = (
			"set -euo pipefail\n"
			f"TEST_DIR={test_dir}\n"
			"COMPOSE_LOG=/dev/null\n"
			"CANARY_PATTERN=*canary*.sh\n"
			"CANARY_REQUIRED=0\n"
			"HELPER_PATTERN=_*.sh\n"
			"TEST_FILES=()\n"
			"CANARY_TEST=''\n"
			"fail_fast() { echo \"FAIL:$1:$2\" >&2; exit 99; }\n"
		)

		include_true = subprocess.run(
			[
				"bash",
				"-c",
				base_script
				+ "VALIDATION_INCLUDE_SYNTHESISED=true\n"
				+ function_text
				+ "discover_tests\nprintf '%s\\n' \"${TEST_FILES[@]}\"\n",
			],
			capture_output=True,
			text=True,
			check=True,
			timeout=60,
		)
		include_false = subprocess.run(
			[
				"bash",
				"-c",
				base_script
				+ "VALIDATION_INCLUDE_SYNTHESISED=false\n"
				+ function_text
				+ "discover_tests\nprintf '%s\\n' \"${TEST_FILES[@]}\"\n",
			],
			capture_output=True,
			text=True,
			check=True,
			timeout=60,
		)

		included_paths = include_true.stdout.splitlines()
		excluded_paths = include_false.stdout.splitlines()
		assert str(test_dir / "10_regular.sh") in included_paths
		assert str(test_dir / "10_regular.sh") in excluded_paths
		assert str(test_dir / "synth_round_1_issue.sh") in included_paths
		assert str(test_dir / "synth_round_1_issue.sh") not in excluded_paths
		assert str(test_dir / "_helpers.sh") not in included_paths
		assert "excluded 1 synthesised behavioural smoke script(s)" in include_false.stderr


def test_validate_process_materializes_latest_cached_synthesised_smoke_tests() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_process_synth_materialize_") as td:
		workspace = Path(td)

		round1_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "synth"
		round3_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-3" / "synth"
		round1_dir.mkdir(parents=True, exist_ok=True)
		round3_dir.mkdir(parents=True, exist_ok=True)

		old_wrapper = round1_dir / "synth_round_1_old_issue.sh"
		old_wrapper.write_text("#!/usr/bin/env bash\necho old\n", encoding="utf-8")
		old_wrapper.chmod(0o755)
		(round1_dir / "synth_round_1_manifest.json").write_text(
			json.dumps(
				{
					"round": 1,
					"head_sha": "oldsha",
					"language": "python",
					"source_artifact": ".ai/review_runtime/pr-4242/round-1/judge_interim.json",
					"target_manifest_relpath": "validation/tests/synth_round_1_manifest.json",
					"files": [
						{
							"issue_id": "old",
							"file": "src/module.py",
							"line_start": 1,
							"line_end": 1,
							"severity": "must-fix",
							"slug": "old_issue",
							"cache_relpath": ".ai/review_runtime/pr-4242/round-1/synth/synth_round_1_old_issue.sh",
							"target_relpath": "validation/tests/synth_round_1_old_issue.sh",
							"suggested_path": "validation/tests/synth_round_1_old_issue.sh",
							"expected_to_fail_until_fixed": True,
						}
					],
				},
				indent=2,
			)
			+ "\n",
			encoding="utf-8",
		)

		latest_wrapper = round3_dir / "synth_round_3_latest_issue.sh"
		latest_wrapper.write_text("#!/usr/bin/env bash\necho latest\n", encoding="utf-8")
		latest_wrapper.chmod(0o755)
		(round3_dir / "synth_round_3_manifest.json").write_text(
			json.dumps(
				{
					"round": 3,
					"head_sha": "newsha",
					"language": "python",
					"source_artifact": ".ai/review_runtime/pr-4242/round-3/judge_interim.json",
					"target_manifest_relpath": "validation/tests/synth_round_3_manifest.json",
					"files": [
						{
							"issue_id": "latest",
							"file": "src/module.py",
							"line_start": 3,
							"line_end": 3,
							"severity": "must-fix",
							"slug": "latest_issue",
							"cache_relpath": ".ai/review_runtime/pr-4242/round-3/synth/synth_round_3_latest_issue.sh",
							"target_relpath": "validation/tests/synth_round_3_latest_issue.sh",
							"suggested_path": "validation/tests/synth_round_3_latest_issue.sh",
							"expected_to_fail_until_fixed": True,
						}
					],
				},
				indent=2,
			)
			+ "\n",
			encoding="utf-8",
		)

		function_text = _extract_shell_function(VALIDATE_PROCESS, "materialize_synthesised_behavioural_smoke_tests")

		disabled = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				+ "VALIDATION_INCLUDE_SYNTHESISED=false\n"
				+ function_text
				+ "materialize_synthesised_behavioural_smoke_tests\n",
			],
			cwd=workspace,
			capture_output=True,
			text=True,
			check=True,
			timeout=60,
		)
		assert "skipping synthesised behavioural smoke materialization" in disabled.stderr
		assert not (workspace / "validation" / "tests" / "synth_round_3_latest_issue.sh").exists()

		enabled = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				+ "VALIDATION_INCLUDE_SYNTHESISED=true\n"
				+ function_text
				+ "materialize_synthesised_behavioural_smoke_tests\n",
			],
			cwd=workspace,
			capture_output=True,
			text=True,
			check=True,
			timeout=60,
		)

		latest_target = workspace / "validation" / "tests" / "synth_round_3_latest_issue.sh"
		latest_manifest = workspace / "validation" / "tests" / "synth_round_3_manifest.json"
		old_target = workspace / "validation" / "tests" / "synth_round_1_old_issue.sh"

		assert "Materialized synthesised behavioural smoke tests" in enabled.stdout
		assert latest_target.exists()
		assert latest_target.read_text(encoding="utf-8") == latest_wrapper.read_text(encoding="utf-8")
		assert os.access(latest_target, os.X_OK)
		assert latest_manifest.exists()
		assert json.loads(latest_manifest.read_text(encoding="utf-8"))["round"] == 3
		assert not old_target.exists()


def test_validate_process_skips_wrapper_copy_when_manifest_target_is_invalid() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_process_synth_manifest_invalid_") as td:
		workspace = Path(td)

		round3_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-3" / "synth"
		round3_dir.mkdir(parents=True, exist_ok=True)
		latest_wrapper = round3_dir / "synth_round_3_latest_issue.sh"
		latest_wrapper.write_text("#!/usr/bin/env bash\necho latest\n", encoding="utf-8")
		latest_wrapper.chmod(0o755)
		(round3_dir / "synth_round_3_manifest.json").write_text(
			json.dumps(
				{
					"round": 3,
					"head_sha": "newsha",
					"language": "python",
					"source_artifact": ".ai/review_runtime/pr-4242/round-3/judge_interim.json",
					"target_manifest_relpath": "../outside.json",
					"files": [
						{
							"issue_id": "latest",
							"file": "src/module.py",
							"line_start": 3,
							"line_end": 3,
							"severity": "must-fix",
							"slug": "latest_issue",
							"cache_relpath": ".ai/review_runtime/pr-4242/round-3/synth/synth_round_3_latest_issue.sh",
							"target_relpath": "validation/tests/synth_round_3_latest_issue.sh",
							"suggested_path": "validation/tests/synth_round_3_latest_issue.sh",
							"expected_to_fail_until_fixed": True,
						}
					],
				},
				indent=2,
			)
			+ "\n",
			encoding="utf-8",
		)

		function_text = _extract_shell_function(VALIDATE_PROCESS, "materialize_synthesised_behavioural_smoke_tests")
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				+ "VALIDATION_INCLUDE_SYNTHESISED=true\n"
				+ function_text
				+ "materialize_synthesised_behavioural_smoke_tests\n",
			],
			cwd=workspace,
			capture_output=True,
			text=True,
			check=True,
			timeout=60,
		)

		latest_target = workspace / "validation" / "tests" / "synth_round_3_latest_issue.sh"
		assert not latest_target.exists()
		assert not (workspace / "outside.json").exists()
		assert "skipping synthesised smoke materialization because target_manifest_relpath is invalid" in result.stderr
		assert "Materialized synthesised behavioural smoke tests" not in result.stdout


def test_validate_process_warns_when_synth_sources_are_missing() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_process_synth_missing_") as td:
		workspace = Path(td)

		round3_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-3" / "synth"
		round3_dir.mkdir(parents=True, exist_ok=True)
		(round3_dir / "synth_round_3_manifest.json").write_text(
			json.dumps(
				{
					"round": 3,
					"head_sha": "newsha",
					"language": "python",
					"source_artifact": ".ai/review_runtime/pr-4242/round-3/judge_interim.json",
					"target_manifest_relpath": "validation/tests/synth_round_3_manifest.json",
					"files": [
						{
							"issue_id": "latest",
							"file": "src/module.py",
							"line_start": 3,
							"line_end": 3,
							"severity": "must-fix",
							"slug": "latest_issue",
							"cache_relpath": ".ai/review_runtime/pr-4242/round-3/synth/missing_wrapper.sh",
							"target_relpath": "validation/tests/synth_round_3_latest_issue.sh",
							"suggested_path": "validation/tests/synth_round_3_latest_issue.sh",
							"expected_to_fail_until_fixed": True,
						}
					],
				},
				indent=2,
			)
			+ "\n",
			encoding="utf-8",
		)

		function_text = _extract_shell_function(VALIDATE_PROCESS, "materialize_synthesised_behavioural_smoke_tests")
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				+ "VALIDATION_INCLUDE_SYNTHESISED=true\n"
				+ function_text
				+ "materialize_synthesised_behavioural_smoke_tests\n",
			],
			cwd=workspace,
			capture_output=True,
			text=True,
			check=True,
			timeout=60,
		)

		manifest_target = workspace / "validation" / "tests" / "synth_round_3_manifest.json"
		assert manifest_target.exists()
		assert "missing synthesised smoke source" in result.stderr
		assert "listed 1 file(s) but none were materialized into validation/tests" in result.stderr
		assert "Materialized synthesised behavioural smoke tests" not in result.stdout


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	passed = 0
	failed = 0

	for func in test_funcs:
		name = func.__name__
		try:
			params = list(inspect.signature(func).parameters)
			if params:
				raise TypeError(f"unsupported test signature for {name}: {params}")
			func()
			print(f"  PASS  {name}")
			passed += 1
		except AssertionError as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
		except Exception as e:
			print(f"  ERROR {name}: {type(e).__name__}: {e}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
