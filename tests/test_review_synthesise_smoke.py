#!/usr/bin/env python3
"""Tests for behavioural smoke synthesis from judge-interim findings."""

from __future__ import annotations

import base64
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
	(mock_bin_dir / "opencode").write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
		"if [ \"${1:-}\" = \"--version\" ]; then printf '1.18.23\\n'; exit 0; fi\n"
		"if [ \"${1:-}\" != \"run\" ]; then echo \"mock-opencode supports only run\" >&2; exit 2; fi\n"
		"cat \"${MOCK_CODEX_STDOUT_FILE}\"\n"
		"cat \"${MOCK_CODEX_STDERR_FILE}\" >&2\n"
		f"exit \"${{MOCK_CODEX_EXIT_CODE:-{exit_code}}}\"\n",
		encoding="utf-8",
	)
	(mock_bin_dir / "opencode").chmod(0o755)
	(mock_bin_dir / "write_opencode_config.sh").write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n"
		"config_path=''\n"
		"while [ $# -gt 0 ]; do\n"
		"\tif [ \"$1\" = '--config-path' ]; then config_path=\"$2\"; shift 2; else shift; fi\n"
		"done\n"
		"mkdir -p \"$(dirname \"${config_path}\")\"\n"
		"printf '{}\\n' > \"${config_path}\"\n",
		encoding="utf-8",
	)
	(mock_bin_dir / "write_opencode_config.sh").chmod(0o755)


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
	env["OPENCODE_CONFIG_WRITER_PATH"] = str(mock_bin_dir / "write_opencode_config.sh")
	env["OPENCODE_VERSION"] = "1.18.23"
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


def _run_synth(workspace: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	clean_env = env.copy()
	for key in (
		"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
		"GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "BASH_ENV", "ENV",
	):
		clean_env.pop(key, None)
	return subprocess.run(
		["bash", str(SYNTH_SCRIPT)],
		cwd=workspace,
		env=clean_env,
		capture_output=True,
		text=True,
		timeout=60,
	)


def _keyring(key_id: str = "active", key: bytes = b"a" * 32) -> str:
	return json.dumps({
		"schema_version": "orchestrator_state_auth_keyring.v1",
		"active_key_id": key_id,
		"keys": [{"key_id": key_id, "key_base64": base64.b64encode(key).decode("ascii")}],
	})


def _bundle(head_sha: str, assertion: dict[str, object] | None = None) -> dict[str, object]:
	return {
		"schema_version": "behavioural_smoke_assertions.v1",
		"round": 1,
		"head_sha": head_sha,
		"language": "python",
		"assertions": [
			{
				"issue_id": "src/module.py:2:branch-check",
				"file": "src/module.py",
				"line_start": 2,
				"line_end": 3,
				"severity": "must-fix",
				"assertion": assertion or {
					"type": "text_absent",
					"path": "src/module.py",
					"literal": "if True:",
					"expected_to_fail_until_fixed": True,
				},
			}
		],
	}


def _sign_bundle(bundle_path: Path, envelope_path: Path, head_sha: str, keyring: str) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["ORCHESTRATOR_STATE_AUTH_KEYRING"] = keyring
	return subprocess.run(
		[
			"python3", "-I", "-B", str(REPO_ROOT / "scripts" / "orchestrate_state_v2.py"),
			"sign-behavioural-smoke", "--bundle-file", str(bundle_path),
			"--repository", "owner/repo", "--pr-number", "4242", "--head-sha", head_sha,
			"--round", "1", "--producer-run-id", "9001", "--producer-run-attempt", "1",
			"--out-file", str(envelope_path),
		],
		env=env,
		capture_output=True,
		text=True,
		timeout=60,
	)


def test_review_synthesise_smoke_writes_declarative_bundle_only() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_bundle_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix")],
		)
		_install_mock_codex(
			mock_bin_dir,
			stdout_text=json.dumps([{
				"type": "text_absent",
				"path": "src/module.py",
				"literal": "if True:",
				"expected_to_fail_until_fixed": True,
			}]) + "\n",
		)
		result = _run_synth(workspace, _base_env(workspace, runtime_dir, mock_bin_dir))
		bundle_path = workspace / ".ai/review_runtime/pr-4242/round-1/synth/synth_round_1_assertions.json"
		assert result.returncode == 0, result.stdout + result.stderr
		assert bundle_path.exists(), result.stdout + result.stderr
		payload = json.loads(bundle_path.read_text(encoding="utf-8"))
		assert payload["schema_version"] == "behavioural_smoke_assertions.v1"
		assert payload["head_sha"] == head_sha
		assert payload["assertions"][0]["assertion"]["type"] == "text_absent"
		assert not list(bundle_path.parent.glob("*.sh"))
		assert "BEHAVIOURAL_SMOKE_SYNTHESISED count=1" in result.stdout


def test_review_synthesise_smoke_rejects_executable_and_unsafe_assertions() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_reject_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix")],
		)
		(workspace / "linked.py").symlink_to(workspace / "src/module.py")
		(workspace / "oversized.txt").write_bytes(b"x" * (1_048_576 + 1))
		env = _base_env(workspace, runtime_dir, mock_bin_dir)
		invalid_items = (
			{"path": "check.sh", "content": "python3 -c 'print(1)'", "expected_to_fail_until_fixed": True},
			{"type": "shell", "path": "src/module.py", "literal": "gh api", "expected_to_fail_until_fixed": True},
			{"type": "text_present", "path": "../outside", "literal": "x", "expected_to_fail_until_fixed": True},
			{"type": "text_present", "path": ".git/config", "literal": "x", "expected_to_fail_until_fixed": True},
			{"type": "text_present", "path": "validation/validate.env", "literal": "x", "expected_to_fail_until_fixed": True},
			{"type": "text_present", "path": "linked.py", "literal": "x", "expected_to_fail_until_fixed": True},
			{"type": "text_present", "path": "oversized.txt", "literal": "x", "expected_to_fail_until_fixed": True},
		)
		for item in invalid_items:
			_install_mock_codex(mock_bin_dir, stdout_text=json.dumps([item]) + "\n")
			result = _run_synth(workspace, env)
			assert result.returncode == 0
			assert "BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=json_parse_failed" in result.stdout + result.stderr
			assert not (workspace / ".ai/review_runtime/pr-4242/round-1/synth/synth_round_1_assertions.json").exists()


def test_review_synthesise_smoke_fails_open_on_malformed_and_wrong_count_output() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_failopen_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix")],
		)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)
		for output in ('{"action":"fix"}\n', '[]\n'):
			_install_mock_codex(mock_bin_dir, stdout_text=output)
			result = _run_synth(workspace, env)
			assert result.returncode == 0
			assert "BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=json_parse_failed" in result.stdout + result.stderr


def test_review_synthesise_smoke_surfaces_model_stderr_on_failure() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_stderr_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix")],
		)
		_install_mock_codex(mock_bin_dir, stderr_text="mock model lookup failed\n", exit_code=1)
		result = _run_synth(workspace, _base_env(workspace, runtime_dir, mock_bin_dir))
		assert result.returncode == 0
		assert "BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=llm_failed" in result.stdout + result.stderr
		assert "BEHAVIOURAL_SMOKE_SYNTHESIS_STDERR_BEGIN" in result.stderr
		assert "mock model lookup failed" in result.stderr


def test_review_synthesise_smoke_zero_issues_and_invalid_language_fallback() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_zero_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		(workspace / "requirements-dev.txt").write_text("pytest==9.1.1\n", encoding="utf-8")
		_write_judge_artifact(workspace, head_sha, [])
		_install_mock_codex(mock_bin_dir, stdout_text="[]\n")
		env = _base_env(workspace, runtime_dir, mock_bin_dir)
		env["BEHAVIOURAL_SMOKE_LANG"] = " pythoon \n second-line "
		result = _run_synth(workspace, env)
		bundle_path = workspace / ".ai/review_runtime/pr-4242/round-1/synth/synth_round_1_assertions.json"
		assert result.returncode == 0, result.stdout + result.stderr
		assert json.loads(bundle_path.read_text(encoding="utf-8"))["assertions"] == []
		assert "language=python" in result.stdout
		assert "Invalid BEHAVIOURAL_SMOKE_LANG" in result.stdout


def test_review_synthesise_smoke_clamps_oversized_timeout() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_timeout_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True)
		mock_bin_dir = workspace / "mock_bin"
		head_sha = _seed_repo_with_autofix_commit(workspace)
		_write_judge_artifact(
			workspace,
			head_sha,
			[_make_issue("src/module.py:2:branch-check", 2, 3, "Branch still always returns autofix")],
		)
		_install_mock_codex(mock_bin_dir, stdout_text=json.dumps([{
			"type": "literal_count",
			"path": "src/module.py",
			"literal": "if True:",
			"min_count": 0,
			"max_count": 0,
			"expected_to_fail_until_fixed": True,
		}]) + "\n")
		timeout_capture = _install_mock_timeout(mock_bin_dir)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)
		env["MOCK_TIMEOUT_DURATION_FILE"] = str(timeout_capture)
		env["BEHAVIOURAL_SMOKE_TIMEOUT_S"] = "99999999999999999999"
		result = _run_synth(workspace, env)
		assert result.returncode == 0, result.stdout + result.stderr
		assert timeout_capture.read_text(encoding="utf-8").strip() == "120"


def test_behavioural_smoke_provenance_rejects_wrong_context_and_tampering() -> None:
	with tempfile.TemporaryDirectory(prefix="behavioural_smoke_auth_") as td:
		root = Path(td)
		head_sha = "a" * 40
		bundle_path = root / "bundle.json"
		envelope_path = root / "envelope.json"
		bundle_path.write_text(json.dumps(_bundle(head_sha), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
		keyring = _keyring()
		signed = _sign_bundle(bundle_path, envelope_path, head_sha, keyring)
		assert signed.returncode == 0, signed.stderr
		env = os.environ.copy()
		env["ORCHESTRATOR_STATE_AUTH_KEYRING"] = keyring
		base_command = [
			"python3", "-I", "-B", str(REPO_ROOT / "scripts/orchestrate_state_v2.py"),
			"verify-behavioural-smoke", "--bundle-file", str(bundle_path), "--envelope-file", str(envelope_path),
			"--repository", "owner/repo", "--pr-number", "4242", "--head-sha", head_sha, "--round", "1",
		]
		assert subprocess.run(base_command, env=env, timeout=60).returncode == 0
		wrong_head = base_command.copy()
		wrong_head[wrong_head.index("--head-sha") + 1] = "b" * 40
		assert subprocess.run(wrong_head, env=env, timeout=60).returncode == 1
		tampered = _bundle(head_sha)
		tampered["assertions"][0]["assertion"]["literal"] = "tampered"
		bundle_path.write_text(json.dumps(tampered), encoding="utf-8")
		assert subprocess.run(base_command, env=env, timeout=60).returncode == 1
		env["ORCHESTRATOR_STATE_AUTH_KEYRING"] = _keyring("other", b"b" * 32)
		assert subprocess.run(base_command, env=env, timeout=60).returncode == 1


def test_declarative_evaluator_reports_states_without_executing_commands() -> None:
	with tempfile.TemporaryDirectory(prefix="behavioural_smoke_eval_") as td:
		repo = Path(td)
		(repo / "src").mkdir()
		(repo / "validation/tests").mkdir(parents=True)
		(repo / "src/module.py").write_text("danger = 'present'\n", encoding="utf-8")
		head_sha = "a" * 40
		bundle = _bundle(head_sha)
		bundle["assertions"] = [
			{**bundle["assertions"][0], "assertion": {"type": "text_present", "path": "src/module.py", "literal": "present", "expected_to_fail_until_fixed": True}},
			{**bundle["assertions"][0], "issue_id": "absent", "assertion": {"type": "text_absent", "path": "src/module.py", "literal": "present", "expected_to_fail_until_fixed": True}},
			{**bundle["assertions"][0], "issue_id": "unknown", "assertion": {"type": "inconclusive", "reason": "runtime behavior required", "expected_to_fail_until_fixed": True}},
		]
		bundle_path = repo / "validation/tests/synth_round_1_assertions.json"
		bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
		result = subprocess.run(
			["python3", "-I", "-B", str(REPO_ROOT / "scripts/evaluate_behavioural_smoke.py"), str(repo), str(bundle_path)],
			capture_output=True,
			text=True,
			timeout=60,
		)
		assert result.returncode == 0, result.stderr
		assert "BEHAVIOURAL_SMOKE_PRESENT_PASSED" in result.stdout
		assert "BEHAVIOURAL_SMOKE_PRESENT_FAILED" in result.stdout
		assert "BEHAVIOURAL_SMOKE_PRESENT_INCONCLUSIVE" in result.stdout
		assert result.stdout.count("ok ") == 3


def test_materializer_accepts_only_signed_current_pr_head_bundle() -> None:
	with tempfile.TemporaryDirectory(prefix="behavioural_smoke_materialize_") as td:
		workspace = Path(td)
		head_sha = "a" * 40
		keyring = _keyring()
		synth_dir = workspace / ".ai/review_runtime/pr-4242/round-1/synth"
		synth_dir.mkdir(parents=True)
		bundle_path = synth_dir / "synth_round_1_assertions.json"
		envelope_path = synth_dir / "synth_round_1_assertions.envelope.json"
		bundle_path.write_text(json.dumps(_bundle(head_sha), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
		assert _sign_bundle(bundle_path, envelope_path, head_sha, keyring).returncode == 0
		function_text = _extract_shell_function(VALIDATE_PROCESS, "materialize_synthesised_behavioural_smoke_tests")
		env = os.environ.copy()
		for key in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
			env.pop(key, None)
		env.update({
			"VALIDATION_INCLUDE_SYNTHESISED": "true",
			"BEHAVIOURAL_SMOKE_SOURCE_PR": "4242",
			"BEHAVIOURAL_SMOKE_SOURCE_HEAD_SHA": head_sha,
			"GITHUB_REPOSITORY": "owner/repo",
			"ORCHESTRATOR_STATE_AUTH_KEYRING": keyring,
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
		})
		result = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + function_text + "materialize_synthesised_behavioural_smoke_tests\n"],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		target = workspace / "validation/tests/synth_round_1_assertions.json"
		assert result.returncode == 0, result.stdout + result.stderr
		assert target.exists(), result.stdout + result.stderr
		assert "Materialized authenticated behavioural smoke assertions" in result.stdout
		bundle_path.write_text(json.dumps(_bundle("b" * 40)), encoding="utf-8")
		target.unlink()
		tampered = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + function_text + "materialize_synthesised_behavioural_smoke_tests\n"],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		assert tampered.returncode == 0
		assert not target.exists()
		assert "Failed to verify/materialize" in tampered.stderr


def test_workflows_wire_authenticated_scoped_behavioural_smoke() -> None:
	review_workflow = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")
	validate_workflow = VALIDATE_WORKFLOW.read_text(encoding="utf-8")
	assert review_workflow.index("- name: Synthesize behavioural smoke") < review_workflow.index("- name: Authenticate behavioural smoke bundle")
	assert "sign-behavioural-smoke" in review_workflow
	assert 'review_runtime_rel=".ai/review_runtime/pr-${PR_NUMBER}"' in review_workflow
	assert 'rm -rf "${workspace_root}/.ai/review_runtime"' in validate_workflow
	assert "steps.behavioural_smoke_pr.outputs.head_sha" in validate_workflow
	assert 'review_runtime_rel=".ai/review_runtime/pr-${{ steps.behavioural_smoke_pr.outputs.pr_number }}"' in validate_workflow
	for support_path in (
		"scripts/orchestrate_state_v2.py",
		"scripts/evaluate_behavioural_smoke.py",
		"scripts/run_behavioural_smoke_assertions.sh",
	):
		assert support_path in validate_workflow


def test_sandbox_launcher_is_secret_free_networkless_and_read_only() -> None:
	launcher = (REPO_ROOT / "scripts/run_behavioural_smoke_assertions.sh").read_text(encoding="utf-8")
	assert "unset BASH_ENV ENV GH_TOKEN GH_PAT" in launcher
	assert "unshare --mount --net --pid" in launcher
	assert "mount -o remount,bind,ro" in launcher
	assert 'mount -t tmpfs -o ro,nosuid,nodev,noexec,size=4096 tmpfs "${sandbox_repo}/.git"' in launcher
	assert "setpriv --reuid=65534 --regid=65534 --clear-groups --no-new-privs" in launcher
	assert "env -i HOME=/tmp PATH=/usr/bin:/bin" in launcher


def test_review_synthesise_smoke_is_registered_in_ci_workflows() -> None:
	for workflow_path in (CI_WORKFLOW, MARK_STABLE_WORKFLOW, TEST_AND_MARK_STABLE_WORKFLOW):
		workflow = workflow_path.read_text(encoding="utf-8")
		assert "PYTHONDONTWRITEBYTECODE=1 python3 tests/test_review_synthesise_smoke.py" in workflow, workflow_path


def main() -> int:
	test_funcs = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	passed = 0
	failed = 0
	for func in test_funcs:
		try:
			func()
			print(f"  PASS  {func.__name__}")
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {func.__name__}: {type(exc).__name__}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
