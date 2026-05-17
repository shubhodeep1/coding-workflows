#!/usr/bin/env python3
"""Tests for behavioural smoke synthesis generation and workflow wiring."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_synthesise_smoke.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
PROMPT = REPO_ROOT / "prompts" / "behavioural-smoke-synthesise.txt"
AGENTS = REPO_ROOT / "agents.md"


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
	stdin_file = mock_bin_dir / "codex_stdin.txt"
	stdout_file.write_text(stdout_text, encoding="utf-8")
	stderr_file.write_text(stderr_text, encoding="utf-8")
	stdin_file.write_text("", encoding="utf-8")

	(mock_bin_dir / "codex").write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
		"if [ -n \"${MOCK_CODEX_STDIN_FILE:-}\" ]; then\n"
		"\tcat > \"${MOCK_CODEX_STDIN_FILE}\"\n"
		"else\n"
		"\tcat >/dev/null\n"
		"fi\n"
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


def _slugify(value: str) -> str:
	text = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
	text = re.sub(r"_+", "_", text)
	text = text[:48].rstrip("_")
	return text or "issue"


def _expected_output_path(*, test_dir: str, issue: dict[str, object], round_number: int) -> str:
	hash_source = (
		f"{issue['id']}|{issue['file']}|{issue['line_start']}|{issue['line_end']}"
	)
	digest = hashlib.sha256(hash_source.encode("utf-8")).hexdigest()[:8]
	return (
		f"{test_dir}/synth_round_{round_number}_{_slugify(str(issue['id']))}_{digest}.sh"
	)


def _write_judge_interim_artifact(workspace: Path, issues: list[dict[str, object]]) -> Path:
	artifact = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "judge_interim.json"
	artifact.parent.mkdir(parents=True, exist_ok=True)
	artifact.write_text(
		json.dumps(
			{
				"round": 1,
				"head_sha": "abc123",
				"remaining_issues": issues,
			}
		)
		+ "\n",
		encoding="utf-8",
	)
	return artifact


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
	env["TEST_DIR"] = "validation/tests"
	env["VALIDATE_ENV_FILE"] = "validation/validate.env"
	env["BEHAVIOURAL_SMOKE_LANG"] = "python"
	env["MOCK_CODEX_STDOUT_FILE"] = str(mock_bin_dir / "codex_stdout.txt")
	env["MOCK_CODEX_STDERR_FILE"] = str(mock_bin_dir / "codex_stderr.txt")
	env["MOCK_CODEX_STDIN_FILE"] = str(mock_bin_dir / "codex_stdin.txt")
	env["MOCK_CODEX_EXIT_CODE"] = "0"
	return env


def test_review_synthesise_smoke_writes_scripts_manifest_and_mirror() -> None:
	with tempfile.TemporaryDirectory(prefix="behavioural_smoke_ok_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		issues = [
			{
				"id": "src/module.py:7:carry-over",
				"file": "src/module.py",
				"line_start": 7,
				"line_end": 8,
				"symptom": "Branch remains unnecessary (src/module.py:7-8)",
				"evidence_quote": "return stale_value",
				"severity": "must-fix",
			},
			{
				"id": "src/api.py:14:guard-check",
				"file": "src/api.py",
				"line_start": 14,
				"line_end": 16,
				"symptom": "Guard still blocks the fixed path (src/api.py:14-16)",
				"evidence_quote": "if should_skip:\n\treturn None",
				"severity": "nice-to-have",
			},
		]
		_write_judge_interim_artifact(workspace, issues)
		(workspace / "validation").mkdir(parents=True, exist_ok=True)
		(workspace / "validation" / "validate.env").write_text(
			"CANARY_TOOLS=bash python3\n",
			encoding="utf-8",
		)

		path_one = _expected_output_path(test_dir="validation/tests", issue=issues[0], round_number=1)
		path_two = _expected_output_path(test_dir="validation/tests", issue=issues[1], round_number=1)
		_install_mock_codex(
			mock_bin_dir,
			stdout_text=json.dumps(
				[
					{
						"path": path_one,
						"content": 'behavioural_smoke_present "branch still present"',
						"expected_to_fail_until_fixed": True,
					},
					{
						"path": path_two,
						"content": 'behavioural_smoke_cleared "guard removed"',
						"expected_to_fail_until_fixed": False,
					},
				]
			)
			+ "\n",
		)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)

		combined = result.stdout + result.stderr
		manifest_path = workspace / "validation" / "tests" / "synth_round_1_manifest.json"
		script_one = workspace / path_one
		script_two = workspace / path_two
		mirror_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "synth"
		assert result.returncode == 0, combined
		assert manifest_path.exists(), combined
		assert script_one.exists(), combined
		assert script_two.exists(), combined
		assert (script_one.stat().st_mode & stat.S_IXUSR) != 0
		assert (script_two.stat().st_mode & stat.S_IXUSR) != 0
		assert (mirror_dir / script_one.name).exists(), combined
		assert (mirror_dir / script_two.name).exists(), combined
		assert (mirror_dir / "synth_round_1_manifest.json").exists(), combined
		assert "BEHAVIOURAL_SMOKE_SYNTHESISED round=1 count=2 manifest=validation/tests/synth_round_1_manifest.json" in combined

		manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
		assert manifest["generated_count"] == 2
		assert {row["path"] for row in manifest["files"]} == {path_one, path_two}
		assert {row["mirror_path"] for row in manifest["files"]} == {
			(mirror_dir / script_one.name).relative_to(workspace).as_posix(),
			(mirror_dir / script_two.name).relative_to(workspace).as_posix(),
		}

		generated_text = script_one.read_text(encoding="utf-8")
		assert "BEHAVIOURAL_SMOKE_PRESENT_FAILED" in generated_text
		assert "BEHAVIOURAL_SMOKE_PRESENT_PASSED" in generated_text
		assert "behavioural_smoke_inconclusive" in generated_text

		run_one = subprocess.run(
			["bash", str(script_one)],
			cwd=workspace,
			capture_output=True,
			text=True,
		)
		assert run_one.returncode == 0, run_one.stdout + run_one.stderr
		assert "BEHAVIOURAL_SMOKE_PRESENT_FAILED" in run_one.stdout
		assert "not ok 1 - branch still present" in run_one.stdout

		run_two = subprocess.run(
			["bash", str(script_two)],
			cwd=workspace,
			capture_output=True,
			text=True,
		)
		assert run_two.returncode == 0, run_two.stdout + run_two.stderr
		assert "BEHAVIOURAL_SMOKE_PRESENT_PASSED" in run_two.stdout
		assert "ok 1 - guard removed" in run_two.stdout

		prompt_text = (mock_bin_dir / "codex_stdin.txt").read_text(encoding="utf-8")
		assert "language_hint: python" in prompt_text
		assert "PROMPT INJECTION GUARD" in prompt_text
		assert "=== BEGIN UNTRUSTED ISSUE / PR CONTEXT ===" in prompt_text
		assert "=== END UNTRUSTED ISSUE / PR CONTEXT ===" in prompt_text
		assert path_one in prompt_text
		assert path_two in prompt_text


def test_review_synthesise_smoke_keeps_valid_entries_when_some_rows_are_invalid() -> None:
	with tempfile.TemporaryDirectory(prefix="behavioural_smoke_partial_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		issues = [
			{
				"id": "src/module.py:7:carry-over",
				"file": "src/module.py",
				"line_start": 7,
				"line_end": 8,
				"symptom": "Branch remains unnecessary (src/module.py:7-8)",
				"evidence_quote": "return stale_value",
				"severity": "must-fix",
			},
			{
				"id": "src/api.py:14:guard-check",
				"file": "src/api.py",
				"line_start": 14,
				"line_end": 16,
				"symptom": "Guard still blocks the fixed path (src/api.py:14-16)",
				"evidence_quote": "if should_skip:\n\treturn None",
				"severity": "nice-to-have",
			},
		]
		_write_judge_interim_artifact(workspace, issues)
		(workspace / "validation").mkdir(parents=True, exist_ok=True)
		(workspace / "validation" / "validate.env").write_text(
			"CANARY_TOOLS=bash python3\n",
			encoding="utf-8",
		)

		path_one = _expected_output_path(test_dir="validation/tests", issue=issues[0], round_number=1)
		path_two = _expected_output_path(test_dir="validation/tests", issue=issues[1], round_number=1)
		_install_mock_codex(
			mock_bin_dir,
			stdout_text=json.dumps(
				[
					{
						"path": path_one,
						"content": 'behavioural_smoke_present "branch still present"',
						"expected_to_fail_until_fixed": True,
					},
					{
						"path": "validation/tests/not_allowed.sh",
						"content": 'behavioural_smoke_present "bogus path"',
						"expected_to_fail_until_fixed": True,
					},
					{
						"path": path_two,
						"content": 'behavioural_smoke_cleared "guard removed"',
						"expected_to_fail_until_fixed": False,
					},
					{
						"path": path_one,
						"content": 'behavioural_smoke_present "duplicate row should be ignored"',
						"expected_to_fail_until_fixed": True,
					},
				]
			)
			+ "\n",
		)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)

		combined = result.stdout + result.stderr
		manifest_path = workspace / "validation" / "tests" / "synth_round_1_manifest.json"
		assert result.returncode == 0, combined
		assert manifest_path.exists(), combined
		manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
		assert manifest["generated_count"] == 2
		assert {row["path"] for row in manifest["files"]} == {path_one, path_two}
		assert not (workspace / "validation" / "tests" / "not_allowed.sh").exists()
		assert "duplicate row should be ignored" not in (workspace / path_one).read_text(encoding="utf-8")


def test_review_synthesise_smoke_fails_open_without_artifact() -> None:
	with tempfile.TemporaryDirectory(prefix="behavioural_smoke_missing_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		_install_mock_codex(mock_bin_dir)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)

		combined = result.stdout + result.stderr
		assert result.returncode == 0, combined
		assert "missing judge_interim artifact" in combined
		assert not (workspace / "validation" / "tests").exists()


def test_review_synthesise_smoke_fails_open_on_invalid_model_output() -> None:
	with tempfile.TemporaryDirectory(prefix="behavioural_smoke_invalid_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		_write_judge_interim_artifact(
			workspace,
			[
				{
					"id": "src/module.py:7:carry-over",
					"file": "src/module.py",
					"line_start": 7,
					"line_end": 8,
					"symptom": "Branch remains unnecessary (src/module.py:7-8)",
					"evidence_quote": "return stale_value",
					"severity": "must-fix",
				}
			],
		)
		(workspace / "validation").mkdir(parents=True, exist_ok=True)
		(workspace / "validation" / "validate.env").write_text(
			"CANARY_TOOLS=bash python3\n",
			encoding="utf-8",
		)
		_install_mock_codex(mock_bin_dir, stdout_text='{"status":"bad"}\n')
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)

		combined = result.stdout + result.stderr
		assert result.returncode == 0, combined
		assert "could not validate synthesis output" in combined
		assert not (workspace / "validation" / "tests" / "synth_round_1_manifest.json").exists()


def test_review_synthesise_smoke_rejects_unsafe_shell_constructs() -> None:
	with tempfile.TemporaryDirectory(prefix="behavioural_smoke_unsafe_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		issues = [
			{
				"id": "src/module.py:7:carry-over",
				"file": "src/module.py",
				"line_start": 7,
				"line_end": 8,
				"symptom": "Branch remains unnecessary (src/module.py:7-8)",
				"evidence_quote": "return stale_value",
				"severity": "must-fix",
			}
		]
		_write_judge_interim_artifact(workspace, issues)
		(workspace / "validation").mkdir(parents=True, exist_ok=True)
		(workspace / "validation" / "validate.env").write_text(
			"CANARY_TOOLS=bash python3\n",
			encoding="utf-8",
		)
		path_one = _expected_output_path(test_dir="validation/tests", issue=issues[0], round_number=1)
		env = _base_env(workspace, runtime_dir, mock_bin_dir)
		manifest_path = workspace / "validation" / "tests" / "synth_round_1_manifest.json"

		for content in (
			'result="$(whoami)"\nbehavioural_smoke_inconclusive "unsafe"',
			'eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'exec /bin/false\nbehavioural_smoke_inconclusive "unsafe"',
			'source ./payload.sh\nbehavioural_smoke_inconclusive "unsafe"',
			'. ./payload.sh\nbehavioural_smoke_inconclusive "unsafe"',
			'cmd & eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'cmd |& eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'> /dev/null eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'2>/dev/null eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'env eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'timeout 1 eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'command -p eval "$PAYLOAD"\nbehavioural_smoke_inconclusive "unsafe"',
			'builtin -- source ./payload.sh\nbehavioural_smoke_inconclusive "unsafe"',
			'if eval "$PAYLOAD"; then behavioural_smoke_inconclusive "unsafe"; fi',
		):
			_install_mock_codex(
				mock_bin_dir,
				stdout_text=json.dumps(
					[
						{
							"path": path_one,
							"content": content,
							"expected_to_fail_until_fixed": True,
						}
					]
				)
				+ "\n",
			)

			result = subprocess.run(
				["bash", str(SCRIPT)],
				cwd=workspace,
				env=env,
				capture_output=True,
				text=True,
			)

			combined = result.stdout + result.stderr
			assert result.returncode == 0, f"{content}: {combined}"
			assert "could not validate synthesis output" in combined, f"{content}: {combined}"
			assert not manifest_path.exists(), f"{content}: {combined}"


def test_review_synthesise_smoke_warns_when_zero_issue_manifest_write_fails() -> None:
	with tempfile.TemporaryDirectory(prefix="behavioural_smoke_zero_issue_warn_") as td:
		workspace = Path(td)
		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		mock_bin_dir = workspace / "mock_bin"
		_install_mock_codex(mock_bin_dir)
		_write_judge_interim_artifact(workspace, [])
		(workspace / "validation").write_text("occupied\n", encoding="utf-8")
		env = _base_env(workspace, runtime_dir, mock_bin_dir)

		result = subprocess.run(
			["bash", str(SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)

		combined = result.stdout + result.stderr
		assert result.returncode == 0, combined
		assert "failed to write synthesized outputs for zero-issue round" in combined
		assert "BEHAVIOURAL_SMOKE_SYNTHESISED" not in combined


def test_review_synthesise_smoke_workflow_contract() -> None:
	workflow = WORKFLOW.read_text(encoding="utf-8")
	assert "BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED: ${{ vars.BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED || 'false' }}" in workflow
	assert "BEHAVIOURAL_SMOKE_LANG: ${{ vars.BEHAVIOURAL_SMOKE_LANG || '' }}" in workflow
	assert "review_synthesise_smoke.sh" in workflow
	assert "behavioural-smoke-synthesise.txt" in workflow
	judge_idx = workflow.find("- name: Run interim judge")
	synth_idx = workflow.find("- name: Synthesise behavioural smoke")
	save_idx = workflow.find("- name: Save review-issue ledger")
	assert judge_idx != -1, "review_autofix.yml missing the Run interim judge step"
	assert synth_idx != -1, "review_autofix.yml missing the behavioural smoke synthesis step"
	assert save_idx != -1, "review_autofix.yml missing the Save review-issue ledger step"
	assert judge_idx < synth_idx < save_idx, (
		"Behavioural smoke synthesis must run after the interim judge and before the runtime cache save."
	)
	step_block = workflow[synth_idx:save_idx]
	assert "env.BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED == 'true'" in step_block
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/review_synthesise_smoke.sh"' in step_block
	assert re.search(
		r"install -m 0644 [^\n]*\$\{SUPPORT_PROMPTS_DIR\}/behavioural-smoke-synthesise\.txt",
		workflow,
	) is not None
	assert 'check_soft_file "${SUPPORT_PROMPTS_DIR}/behavioural-smoke-synthesise.txt"' in workflow

	agents_text = AGENTS.read_text(encoding="utf-8")
	for prefix in (
		"BEHAVIOURAL_SMOKE_SYNTHESISED",
		"BEHAVIOURAL_SMOKE_PRESENT_FAILED",
		"BEHAVIOURAL_SMOKE_PRESENT_PASSED",
	):
		assert prefix in agents_text, f"agents.md missing stable log prefix {prefix}"

	prompt_text = PROMPT.read_text(encoding="utf-8")
	assert "behavioural_smoke_present" in prompt_text
	assert '"expected_to_fail_until_fixed"' in prompt_text


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
