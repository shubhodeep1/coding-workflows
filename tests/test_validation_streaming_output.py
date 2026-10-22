#!/usr/bin/env python3
"""Validation test output must reach the log while the test is still running.

scripts/validate_process.sh kills a validation run after VALIDATION_TIMEOUT
minutes in which validation.log does not grow. The driver used to write each
test script's output to a file and print it only after the script exited, and
the repo-checks families wrote each custom_tests command's output only to
logs/repo_check_<n>.log. A custom_tests step that ran longer than the timeout
(pip install plus a full pytest suite) was therefore killed while it was still
working (tele-funtoken-msg-scoring validate runs 39725752780, 39726381586 and
39736025924).

These tests pin that:
- scripts/validate_driver.sh and the runtime driver heredoc in
  scripts/validate_process.sh stream a test's output through tee while it
  runs and keep the test's exit status;
- the rendered 40_repo_checks.sh of python-mongo-repo-checks and node-runtime
  echo each command's output as TAP comments while it runs, keep its exit
  status, and never let a line from the command count as a TAP result.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER = REPO_ROOT / "scripts" / "validate_driver.sh"
PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
RENDERER = REPO_ROOT / "scripts" / "render_validation_templates.py"
SCHEMA = REPO_ROOT / "scripts" / "templates" / "slot_manifest.schema.json"
TEMPLATES_ROOT = REPO_ROOT / "workflow-templates" / "validation-harness"
FIXTURES = REPO_ROOT / "examples" / "validation-fixtures"

# Runs `docker compose -f F exec -T [-e NAME[=VALUE]]... SERVICE CMD...` on the
# host, so the rendered script can be exercised without Docker.
FAKE_DOCKER = r"""#!/usr/bin/env bash
set -euo pipefail
while [ "$#" -gt 0 ] && [ "$1" != "exec" ]; do shift; done
[ "$#" -gt 0 ] || { echo "fake docker: no exec" >&2; exit 2; }
shift
while [ "$#" -gt 0 ]; do
	case "$1" in
		-T) shift ;;
		-e)
			case "$2" in
				*=*) export "$2" ;;
			esac
			shift 2
			;;
		*) break ;;
	esac
done
shift
exec "$@"
"""


def _extract_function(text: str, name: str) -> str:
	match = re.search(rf"^{name}\(\)\n\{{\n.*?^\}}\n", text, re.M | re.S)
	assert match is not None, f"{name} not found"
	return match.group(0)


def test_drivers_stream_test_output_through_tee() -> None:
	driver = DRIVER.read_text(encoding="utf-8")
	assert 'run_test_without_credentials "${test_file}" 2>&1 | tee "${test_log}"' in driver
	assert "exit_code=${PIPESTATUS[0]}" in driver
	# Only the synthesised branch, which writes its own log file, still prints it afterwards.
	single = _extract_function(driver, "run_single_test")
	synth_branch = single.split("if is_synthesised_test", 1)[1].split("\telse\n", 1)[0]
	assert 'cat "${test_log}"' in synth_branch
	assert single.count('cat "${test_log}"') == 1

	process = PROCESS.read_text(encoding="utf-8")
	assert 'run_test_without_credentials "${test_script}" 2>&1 | tee "${test_log}"' in process
	assert "test_rc=${PIPESTATUS[0]}" in process
	assert 'test_script_is_synthesised=1' in process
	assert 'if [ "${test_script_is_synthesised}" -eq 1 ]; then\n    cat "${test_log}" || true\n  fi' in process


@pytest.mark.parametrize("source", [DRIVER, PROCESS], ids=["validate_driver", "runtime_driver_heredoc"])
def test_driver_pipeline_streams_and_keeps_the_exit_status(source: Path) -> None:
	function_text = _extract_function(source.read_text(encoding="utf-8"), "run_test_without_credentials")
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		probe = root / "probe.sh"
		probe.write_text('#!/usr/bin/env bash\necho "ok 1 - early"\nsleep 3\necho "late"\nexit 7\n', encoding="utf-8")
		log = root / "probe.log"
		script = function_text + 'run_test_without_credentials "$1" 2>&1 | tee "$2"\necho "rc=${PIPESTATUS[0]}"\n'
		env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root)}
		proc = subprocess.Popen(
			["bash", "-c", script, "harness", str(probe), str(log)],
			cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
		)
		assert proc.stdout is not None
		first = proc.stdout.readline()
		# The first line arrives while the test is still sleeping.
		assert first.strip() == "ok 1 - early"
		assert proc.poll() is None
		rest = proc.stdout.read()
		proc.wait(timeout=30)
	assert "late" in rest
	assert "rc=7" in rest


def _render(family: str, out: Path) -> Path:
	env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
	result = subprocess.run(
		[
			"python3", str(RENDERER),
			"--manifest", str(FIXTURES / f"{family}.yml"),
			"--schema", str(SCHEMA),
			"--templates-root", str(TEMPLATES_ROOT),
			"--output-root", str(out),
		],
		capture_output=True, text=True, env=env, check=False,
	)
	assert result.returncode == 0, result.stderr
	return out / "tests" / "40_repo_checks.sh"


def _fake_docker_path(root: Path) -> str:
	bin_dir = root / "bin"
	bin_dir.mkdir()
	docker = bin_dir / "docker"
	docker.write_text(FAKE_DOCKER, encoding="utf-8")
	docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
	return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


@pytest.mark.parametrize("family", ["python-mongo-repo-checks", "node-runtime"])
def test_repo_checks_stream_command_output_as_tap_comments(family: str) -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		script = _render(family, root / "out")
		command = "echo first-line; echo 'ok 99 - from the command'; sleep 3; echo last-line"
		env = {
			"PATH": _fake_docker_path(root),
			"HOME": str(root),
			"LOG_DIR": str(root / "logs"),
			"CUSTOM_TESTS_JSON": f'["{command}"]',
			"SKIP_TESTS_JSON": "",
		}
		proc = subprocess.Popen(
			["bash", str(script)], cwd=root, env=env,
			stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
		)
		assert proc.stdout is not None
		seen: list[str] = []
		deadline = time.monotonic() + 20
		while time.monotonic() < deadline:
			line = proc.stdout.readline()
			if not line:
				break
			seen.append(line.rstrip("\n"))
			if line.rstrip("\n") == "# first-line":
				break
		# The command's first line is printed while the command is still sleeping.
		assert "# first-line" in seen, seen
		assert proc.poll() is None
		seen.extend(proc.stdout.read().splitlines())
		assert proc.wait(timeout=30) == 0, seen
		log_text = (root / "logs" / "repo_check_1.log").read_text(encoding="utf-8")
	assert "# ok 99 - from the command" in seen
	assert "# last-line" in seen
	# Only the script's own result counts: the command's "ok 99" stays a comment.
	results = [line for line in seen if re.match(r"^\s*(not )?ok\b", line)]
	assert results == [f"ok 1 - repo check command: {command}"], results
	assert any(line.startswith("# repo check 1 started ") for line in seen)
	assert any(line.startswith("# repo check 1 finished ") and line.endswith("(exit=0)") for line in seen)
	assert any(line.startswith("# repo checks: reading skip_tests and custom_tests ") for line in seen)
	# The per-command log still holds the raw output.
	assert "first-line\nok 99 - from the command\nlast-line\n" in log_text


@pytest.mark.parametrize("family", ["python-mongo-repo-checks", "node-runtime"])
def test_repo_checks_keep_a_failing_command_status(family: str) -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		script = _render(family, root / "out")
		env = {
			"PATH": _fake_docker_path(root),
			"HOME": str(root),
			"LOG_DIR": str(root / "logs"),
			"CUSTOM_TESTS_JSON": '["echo boom; exit 3"]',
			"SKIP_TESTS_JSON": "",
		}
		result = subprocess.run(
			["bash", str(script)], cwd=root, env=env, capture_output=True, text=True, timeout=60, check=False,
		)
	lines = result.stdout.splitlines()
	assert result.returncode == 1, result.stdout + result.stderr
	assert "not ok 1 - repo check command: echo boom; exit 3" in lines
	assert "# repo check command failed (exit=3)" in lines
	assert "# boom" in lines
	assert any(line.startswith("# repo check 1 finished ") and line.endswith("(exit=3)") for line in lines)


def main() -> int:
	return pytest.main([__file__, "-q", "-p", "no:cacheprovider"])


if __name__ == "__main__":
	raise SystemExit(main())
