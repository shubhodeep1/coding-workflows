#!/usr/bin/env python3
"""Tests for validate_driver.sh synthesized-test discovery filtering."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_DRIVER = REPO_ROOT / "scripts" / "validate_driver.sh"


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
		elif stripped.startswith("}"):
			depth -= 1
			if depth == 0:
				return "\n".join(lines[start : end + 1]) + "\n"
		end += 1

	raise AssertionError(f"could not extract function {function_name}")


def _write_exec(path: Path) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n", encoding="utf-8")
	path.chmod(0o755)


def _run_discover_tests(workspace: Path, *, include_synthesised: str | None) -> subprocess.CompletedProcess[str]:
	function_text = _extract_shell_function(VALIDATE_DRIVER, "discover_tests")
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["TEST_DIR"] = "validation/tests"
	env["HELPER_PATTERN"] = "_*.sh"
	env["CANARY_PATTERN"] = "*canary*.sh"
	env["CANARY_REQUIRED"] = "1"
	if include_synthesised is not None:
		env["VALIDATION_INCLUDE_SYNTHESISED"] = include_synthesised

	script = (
		'VALIDATION_INCLUDE_SYNTHESISED="${VALIDATION_INCLUDE_SYNTHESISED:-true}"\n'
		+ function_text
		+ "\n"
		+ "fail_fast()\n"
		+ "{\n"
		+ "\tprintf 'FAIL:%s\\n' \"$2\" >&2\n"
		+ "\texit 99\n"
		+ "}\n"
		+ "discover_tests\n"
		+ "printf 'CANARY_TEST=%s\\n' \"${CANARY_TEST}\"\n"
		+ "printf 'TEST_FILE=%s\\n' \"${TEST_FILES[@]}\"\n"
	)

	return subprocess.run(
		["bash", "-c", script],
		cwd=workspace,
		env=env,
		capture_output=True,
		text=True,
		timeout=60,
	)


def _parse_test_files(stdout: str) -> list[str]:
	return [
		line.split("=", 1)[1]
		for line in stdout.splitlines()
		if line.startswith("TEST_FILE=") and line.split("=", 1)[1]
	]


def _parse_canary(stdout: str) -> str:
	for line in stdout.splitlines():
		if line.startswith("CANARY_TEST="):
			return line.split("=", 1)[1]
	raise AssertionError("missing CANARY_TEST line")


def _seed_test_dir(workspace: Path) -> None:
	test_dir = workspace / "validation" / "tests"
	_write_exec(test_dir / "00_canary.sh")
	_write_exec(test_dir / "20_health.sh")
	_write_exec(test_dir / "_helper.sh")
	_write_exec(test_dir / "synth_round_4_issue.sh")
	(test_dir / "synth_round_4_manifest.json").write_text("{}\n", encoding="utf-8")


def test_discover_tests_includes_synthesised_scripts_by_default() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_driver_synth_default_") as td:
		workspace = Path(td)
		_seed_test_dir(workspace)

		result = _run_discover_tests(workspace, include_synthesised=None)
		assert result.returncode == 0, result.stdout + result.stderr
		assert _parse_canary(result.stdout) == "validation/tests/00_canary.sh"
		assert _parse_test_files(result.stdout) == [
			"validation/tests/00_canary.sh",
			"validation/tests/20_health.sh",
			"validation/tests/synth_round_4_issue.sh",
		]
		assert "synth_round_4_manifest.json" not in result.stdout


def test_discover_tests_excludes_only_synthesised_scripts_when_disabled() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_driver_synth_disabled_") as td:
		workspace = Path(td)
		_seed_test_dir(workspace)

		result = _run_discover_tests(workspace, include_synthesised="false")
		assert result.returncode == 0, result.stdout + result.stderr
		assert _parse_canary(result.stdout) == "validation/tests/00_canary.sh"
		assert _parse_test_files(result.stdout) == [
			"validation/tests/00_canary.sh",
			"validation/tests/20_health.sh",
		]
		assert "validation/tests/_helper.sh" not in result.stdout
		assert "validation/tests/synth_round_4_issue.sh" not in result.stdout


# ---------------------------------------------------------------------------
# Finding smoke-synth-credentialed-test-exec: synth_round_*.sh tests run only
# inside a credential-free, network-less container; never on the host.
# ---------------------------------------------------------------------------

_SANDBOX_HOST_TOOLS = (
	"bash", "sh", "env", "git", "mktemp", "cp", "chmod", "rm", "grep", "id",
	"timeout", "cat", "touch", "dirname", "basename", "printf", "sleep", "mkdir", "head",
)

_STUB_DOCKER = """#!/usr/bin/env bash
record_dir="${0%/*}/../record"
mkdir -p "${record_dir}"
case "$1" in
	image)
		[ -e "${0%/*}/../stub_image_missing" ] && exit 1
		exit 0
		;;
	pull)
		exit 1
		;;
	rm)
		exit 0
		;;
	run)
		printf '%s\\n' "$@" > "${record_dir}/run_argv.txt"
		env > "${record_dir}/run_env.txt"
		cat > "${record_dir}/run_stdin.bin"
		if [ -e "${0%/*}/../stub_hang" ]; then
			echo "1..1"
			echo "not ok 1 - partial"
			exec sleep 30
		fi
		if [ -e "${0%/*}/../stub_tar_fail" ]; then
			echo "# source_snapshot=incomplete"
			exit 86
		fi
		if [ -e "${0%/*}/../stub_start_fail" ]; then
			echo "docker: Error response from daemon" >&2
			exit 125
		fi
		echo "1..1"
		echo "ok 1 - sandboxed"
		exit 0
		;;
esac
exit 0
"""


def _sandbox_harness_script() -> str:
	functions = "".join(
		_extract_shell_function(VALIDATE_DRIVER, name)
		for name in ("synthesised_test_sandbox_skip", "run_synthesised_test_sandboxed", "run_single_test")
	)
	return (
		"set -euo pipefail\n"
		'LOG_DIR="${PWD}/logs"\nmkdir -p "${LOG_DIR}"\n'
		"TOTAL_TESTS=0\nPASSED_TESTS=0\nFAILED_TESTS=0\n"
		'VALIDATION_SYNTH_SANDBOX_TIMEOUT_SECS="${SANDBOX_TIMEOUT_SECS:-60}"\n'
		"VALIDATION_SYNTH_SANDBOX_IMAGE=python:3.12-slim\n"
		"append_failure()\n{\n\tprintf 'APPEND_FAILURE:%s:%s\\n' \"$1\" \"$2\"\n}\n"
		+ functions
		+ 'run_single_test "$1" test || true\n'
		+ "printf 'TOTALS=%s/%s/%s\\n' \"${TOTAL_TESTS}\" \"${PASSED_TESTS}\" \"${FAILED_TESTS}\"\n"
	)


def _run_sandbox_case(
	workspace: Path,
	test_name: str,
	*,
	with_docker: bool,
	stub_marker: str | None = None,
	with_git: bool = True,
	timeout_secs: int = 60,
) -> subprocess.CompletedProcess[str]:
	# The driver runs docker under env -i, so the stub reads marker files,
	# not environment variables.
	if with_git:
		# The sandbox needs a `git archive` of HEAD as its source snapshot.
		git_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(workspace)}
		(workspace / "src.txt").write_text("source\n", encoding="utf-8")
		for git_args in (
			["init", "-q"],
			["add", "src.txt"],
			["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
			 "commit", "-q", "-m", "base"],
		):
			subprocess.run(["git", *git_args], cwd=workspace, env=git_env, check=True, capture_output=True)
	if stub_marker:
		(workspace / stub_marker).write_text("", encoding="utf-8")
	bin_dir = workspace / "bin"
	bin_dir.mkdir(exist_ok=True)
	for tool in _SANDBOX_HOST_TOOLS:
		found = shutil.which(tool)
		if found and not (bin_dir / tool).exists():
			(bin_dir / tool).symlink_to(found)
	if with_docker:
		(bin_dir / "docker").write_text(_STUB_DOCKER, encoding="utf-8")
		(bin_dir / "docker").chmod(0o755)
	sentinel = workspace / "sentinel"
	test_file = workspace / "validation" / "tests" / test_name
	test_file.parent.mkdir(parents=True, exist_ok=True)
	test_file.write_text(
		f'#!/usr/bin/env bash\ntouch "{sentinel}"\necho "1..1"\necho "ok 1 - host"\n', encoding="utf-8"
	)
	test_file.chmod(0o755)
	env = {
		"PATH": str(bin_dir),
		"HOME": str(workspace),
		"GH_TOKEN": "ghp_secret_sentinel",
		"OPENROUTER_API_KEY": "sk-or-secret_sentinel",
		"SANDBOX_TIMEOUT_SECS": str(timeout_secs),
	}
	return subprocess.run(
		[str(bin_dir / "bash"), "-c", _sandbox_harness_script(), "harness", f"validation/tests/{test_name}"],
		cwd=workspace,
		env=env,
		capture_output=True,
		text=True,
		timeout=60,
	)


def test_synthesised_test_is_skipped_not_run_without_docker() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_driver_synth_nodocker_") as td:
		workspace = Path(td)
		result = _run_sandbox_case(workspace, "synth_round_2_issue.sh", with_docker=False)
		assert result.returncode == 0, result.stdout + result.stderr
		assert not (workspace / "sentinel").exists()
		assert "BEHAVIOURAL_SMOKE_SANDBOX test=synth_round_2_issue.sh outcome=skipped reason=docker_missing" in result.stderr
		log = (workspace / "logs" / "synth_round_2_issue.sh").read_text(encoding="utf-8")
		assert "# SKIP behavioural smoke sandbox unavailable" in log
		assert "TOTALS=1/1/0" in result.stdout


def test_synthesised_test_runs_in_credential_free_network_less_container() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_driver_synth_docker_") as td:
		workspace = Path(td)
		result = _run_sandbox_case(workspace, "synth_round_2_issue.sh", with_docker=True)
		assert result.returncode == 0, result.stdout + result.stderr
		assert not (workspace / "sentinel").exists()
		argv = (workspace / "record" / "run_argv.txt").read_text(encoding="utf-8").splitlines()
		joined = " ".join(argv)
		assert "--network none" in joined
		assert "--read-only" in argv
		assert "--cap-drop ALL" in joined
		assert "--security-opt no-new-privileges" in joined
		env_flags = [argv[index + 1] for index, value in enumerate(argv) if value == "--env"]
		assert env_flags == ["HOME=/tmp", "TMPDIR=/tmp", "BEHAVIOURAL_SMOKE_SANDBOXED=1"]
		assert not any(value.startswith("--env-file") or value == "-e" for value in argv)
		assert (workspace / "record" / "run_stdin.bin").stat().st_size > 0
		docker_env = (workspace / "record" / "run_env.txt").read_text(encoding="utf-8")
		assert "secret_sentinel" not in docker_env
		assert "DOCKER_CONFIG=/nonexistent" in docker_env
		assert "BEHAVIOURAL_SMOKE_SANDBOX test=synth_round_2_issue.sh outcome=ran exit=0" in result.stderr
		assert "TOTALS=1/1/0" in result.stdout


def test_synthesised_test_skips_when_image_or_start_unavailable() -> None:
	for stub_marker, reason in (
		("stub_image_missing", "image_unavailable"),
		("stub_start_fail", "start_failed"),
		("stub_tar_fail", "source_snapshot_incomplete"),
	):
		with tempfile.TemporaryDirectory(prefix="validate_driver_synth_unavailable_") as td:
			workspace = Path(td)
			result = _run_sandbox_case(workspace, "synth_round_2_issue.sh", with_docker=True, stub_marker=stub_marker)
			assert result.returncode == 0, result.stdout + result.stderr
			assert not (workspace / "sentinel").exists()
			assert f"outcome=skipped reason={reason}" in result.stderr
			assert "TOTALS=1/1/0" in result.stdout


def test_synthesised_test_skips_when_source_snapshot_unavailable() -> None:
	# Without a `git archive` of HEAD the test would run against an empty
	# workspace and report a meaningless result, so it is skipped.
	with tempfile.TemporaryDirectory(prefix="validate_driver_synth_nosnapshot_") as td:
		workspace = Path(td)
		result = _run_sandbox_case(workspace, "synth_round_2_issue.sh", with_docker=True, with_git=False)
		assert result.returncode == 0, result.stdout + result.stderr
		assert not (workspace / "sentinel").exists()
		assert not (workspace / "record" / "run_argv.txt").exists()
		assert "outcome=skipped reason=source_snapshot_unavailable" in result.stderr
		assert "TOTALS=1/1/0" in result.stdout


def test_synthesised_test_timeout_counts_only_the_skip_result() -> None:
	# A killed run's partial TAP lines are commented out, so only the SKIP counts.
	with tempfile.TemporaryDirectory(prefix="validate_driver_synth_timeout_") as td:
		workspace = Path(td)
		for tool in ("sed", "mv"):
			found = shutil.which(tool)
			if found:
				(workspace / "bin").mkdir(exist_ok=True)
				(workspace / "bin" / tool).symlink_to(found)
		(workspace / "stub_hang").write_text("", encoding="utf-8")
		result = _run_sandbox_case(workspace, "synth_round_2_issue.sh", with_docker=True, timeout_secs=1)
		assert result.returncode == 0, result.stdout + result.stderr
		log = (workspace / "logs" / "synth_round_2_issue.sh").read_text(encoding="utf-8")
		assert "# partial: not ok 1 - partial" in log
		assert "# SKIP behavioural smoke sandbox timeout" in log
		assert "TOTALS=1/1/0" in result.stdout


def test_non_synthesised_test_still_runs_on_host() -> None:
	with tempfile.TemporaryDirectory(prefix="validate_driver_synth_host_") as td:
		workspace = Path(td)
		result = _run_sandbox_case(workspace, "20_health.sh", with_docker=True)
		assert result.returncode == 0, result.stdout + result.stderr
		assert (workspace / "sentinel").exists()
		assert not (workspace / "record" / "run_argv.txt").exists()
		assert "BEHAVIOURAL_SMOKE_SANDBOX" not in result.stderr


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
