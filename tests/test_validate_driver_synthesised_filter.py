#!/usr/bin/env python3
"""Tests for validate_driver.sh synthesized-test discovery filtering."""

from __future__ import annotations

import os
import re
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
	for variable in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
		env.pop(variable, None)
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


def _run_driver_with_stubbed_probe(workspace: Path, url: str | None, ports: str, *, via_env_file: bool = False) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
	bin_dir = workspace / "bin"
	bin_dir.mkdir(parents=True)
	compose_file = workspace / "validation" / "docker-compose.test.yml"
	compose_file.parent.mkdir(parents=True)
	compose_file.write_text("services: {app: {image: busybox}}\n", encoding="utf-8")
	test_file = workspace / "validation" / "tests" / "00_canary.sh"
	_write_exec(test_file)
	with test_file.open("a", encoding="utf-8") as handle:
		handle.write("printf '1..1\\nok 1 - canary\\n'\n")
	docker_stub = bin_dir / "docker"
	docker_stub.write_text('''#!/usr/bin/env bash
if [ "$1" = "inspect" ]; then
    case "$3" in
        '{{json .NetworkSettings.Ports}}') printf '%s\\n' "${DOCKER_PORTS}" ;;
        '{{.State.Running}}') echo true ;;
        '{{.State.Status}}') echo running ;;
        *) echo healthy ;;
    esac
elif [[ "$*" == *"ps -q"* ]]; then
    echo selected-app-container
fi
''', encoding="utf-8")
	docker_stub.chmod(0o755)
	curl_stub = bin_dir / "curl"
	curl_stub.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$@" >> "${CURL_LOG}"
printf 'proxy=%s curl_home=%s\\n' "${HTTP_PROXY-unset}" "${CURL_HOME-unset}" > "${CURL_ENV_LOG}"
''', encoding="utf-8")
	curl_stub.chmod(0o755)
	curl_log = workspace / "curl_calls"
	curl_env_log = workspace / "curl_env"
	env_file = workspace / "validation" / "validate.env"
	if via_env_file and url is not None:
		env_file.write_text(f"APP_URL={url}\n", encoding="utf-8")
	env = os.environ.copy()
	for variable in ("APP_URL", "BASH_ENV", "ENV", "WORKSPACE_PATH"):
		env.pop(variable, None)
	env.update({
		"PATH": f"{bin_dir}:{env['PATH']}", "VALIDATE_ENV_FILE": str(env_file),
		"COMPOSE_FILE": str(compose_file), "DOCKER_PORTS": ports,
		"CURL_LOG": str(curl_log), "CURL_ENV_LOG": str(curl_env_log),
		"CURL_HOME": str(workspace), "HTTP_PROXY": "http://proxy.invalid:1234",
		"HEALTH_TIMEOUT": "2", "HEALTH_POLL_INTERVAL": "1", "PYTHONDONTWRITEBYTECODE": "1",
	})
	if url is not None and not via_env_file:
		env["APP_URL"] = url
	result = subprocess.run(["bash", str(VALIDATE_DRIVER)], cwd=workspace, env=env,
		capture_output=True, text=True, timeout=15)
	return result, curl_log, curl_env_log


def test_driver_refuses_untrusted_urls_and_unverified_bindings(tmp_path: Path) -> None:
	loopback_ports = '{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"8080"}]}'
	for index, url in enumerate(("http://169.254.169.254:80/", "http://localhost:8080/",
			"http://127.0.0.1:8080@evil.invalid/", "http://127.0.0.1:8080/#frag",
			"http://127.0.0.1:8080/\n")):
		result, curl_log, _ = _run_driver_with_stubbed_probe(tmp_path / f"url{index}", url, loopback_ports,
			via_env_file=(index == 0))
		assert result.returncode != 0, result.stdout + result.stderr
		assert "preflight_app_url" in result.stdout
		assert not curl_log.exists()
	for index, ports in enumerate(("", "null", "{", '{"8000/tcp":[{"HostIp":"0.0.0.0","HostPort":"8080"}]}',
			'{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"8081"}]}',
			'{"8000/udp":[{"HostIp":"127.0.0.1","HostPort":"8080"}]}')):
		result, curl_log, _ = _run_driver_with_stubbed_probe(tmp_path / f"port{index}",
			"http://127.0.0.1:8080/health", ports)
		assert result.returncode != 0, result.stdout + result.stderr
		assert "preflight_app_url_binding" in result.stdout
		assert not curl_log.exists()


def test_driver_probes_only_selected_loopback_binding_without_curl_config(tmp_path: Path) -> None:
	ports = '{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"8080"}]}'
	result, curl_log, curl_env_log = _run_driver_with_stubbed_probe(tmp_path / "valid",
		"http://127.0.0.1:8080/health", ports, via_env_file=True)
	assert result.returncode == 0, result.stdout + result.stderr
	args = curl_log.read_text(encoding="utf-8").splitlines()
	assert args[0] == "-q"
	assert args[args.index("--proto") + 1] == "=http,https"
	assert args[args.index("--noproxy") + 1] == "*"
	assert "--location" not in args and "-L" not in args
	assert args[-1] == "http://127.0.0.1:8080/health"
	assert curl_env_log.read_text(encoding="utf-8").strip() == "proxy=unset curl_home=unset"
	for index, url in enumerate((None, "")):
		result, curl_log, _ = _run_driver_with_stubbed_probe(tmp_path / f"disabled{index}", url, "")
		assert result.returncode == 0, result.stdout + result.stderr
		assert not curl_log.exists()


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
