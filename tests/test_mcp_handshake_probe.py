#!/usr/bin/env python3
"""Contract tests for scripts/mcp_handshake_probe.py and setup_serena.sh."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "mcp_handshake"
PROBE_SCRIPT = SCRIPTS_DIR / "mcp_handshake_probe.py"
BASH_BIN = shutil.which("bash") or "bash"

sys.path.insert(0, str(SCRIPTS_DIR))

from mcp_handshake_probe import ProbeError, _sanitize_log_value, validate_initialize_response  # noqa: E402


def _run_probe(command: list[str], *, timeout: float = 1.0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	if env:
		full_env.update(env)
	return subprocess.run(
		[sys.executable, str(PROBE_SCRIPT), "--name", "serena", "--timeout", str(timeout), "--", *command],
		env=full_env,
		capture_output=True,
		text=True,
		check=False,
	)


def _stage_setup_serena(tmp_root: Path) -> Path:
	stage_scripts = tmp_root / "scripts"
	stage_templates = stage_scripts / "templates"
	stage_templates.mkdir(parents=True, exist_ok=True)
	shutil.copy2(SCRIPTS_DIR / "setup_serena.sh", stage_scripts / "setup_serena.sh")
	shutil.copy2(SCRIPTS_DIR / "mcp_handshake_probe.py", stage_scripts / "mcp_handshake_probe.py")
	shutil.copy2(SCRIPTS_DIR / "templates" / "serena_project.yml.j2", stage_templates / "serena_project.yml.j2")
	return stage_scripts / "setup_serena.sh"


def _read_supported_template_languages() -> list[str]:
	template = (SCRIPTS_DIR / "templates" / "serena_project.yml.j2").read_text(encoding="utf-8")
	values: list[str] = []
	in_languages = False
	for line in template.splitlines():
		if line == "languages:":
			in_languages = True
			continue
		if in_languages and not line.startswith("  - "):
			break
		if in_languages:
			values.append(line.split('"')[1])
	return values


def _write_executable(path: Path, content: str) -> None:
	path.write_text(content, encoding="utf-8")
	path.chmod(0o755)


def _write_fake_serena(path: Path, *, version: str = "1.2.0") -> None:
	_write_executable(
		path,
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n"
		"if [ \"${1:-}\" = \"--version\" ]; then\n"
		f"\tprintf 'Serena {version}\\n'\n"
		"\texit 0\n"
		"fi\n"
		"if [ \"${1:-}\" = \"start-mcp-server\" ]; then\n"
		"\tshift\n"
		"\texec \"${PYTHON_BIN:?}\" \"${FAKE_SERENA_FIXTURE:?}\" \"$@\"\n"
		"fi\n"
		"printf 'unexpected args: %s\\n' \"$*\" >&2\n"
		"exit 2\n",
	)


def _run_staged_setup(
	setup_script: Path,
	*,
	home: Path,
	path_value: str,
	github_env: Path,
	extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	full_env.update(
		{
			"PYTHONDONTWRITEBYTECODE": "1",
			"GITHUB_WORKSPACE": str(setup_script.parent.parent),
			"HOME": str(home),
			"GITHUB_ENV": str(github_env),
			"PATH": path_value,
			"SERENA_ENABLED": "true",
			"SERENA_UV_PYTHON_BIN": sys.executable,
			"PYTHON_BIN": sys.executable,
		}
	)
	if extra_env:
		full_env.update(extra_env)
	return subprocess.run(
		[BASH_BIN, str(setup_script)],
		cwd=setup_script.parent.parent,
		env=full_env,
		capture_output=True,
		text=True,
		check=False,
	)


def test_validate_initialize_response_accepts_happy_path() -> None:
	result = validate_initialize_response(
		{
			"jsonrpc": "2.0",
			"id": 1,
			"result": {
				"serverInfo": {
					"name": "mock-serena",
					"version": "0.0.1",
				},
			},
		},
		1,
	)

	assert result["serverInfo"]["name"] == "mock-serena"


def test_validate_initialize_response_rejects_id_mismatch() -> None:
	try:
		validate_initialize_response(
			{
				"jsonrpc": "2.0",
				"id": 2,
				"result": {
					"serverInfo": {
						"name": "mock-serena",
						"version": "0.0.1",
					},
				},
			},
			1,
		)
	except ProbeError as exc:
		assert exc.reason == "id-mismatch"
	else:
		raise AssertionError("expected ProbeError for mismatched initialize response id")


def test_validate_initialize_response_rejects_null_error_field() -> None:
	try:
		validate_initialize_response(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"error": None,
				"result": {
					"serverInfo": {
						"name": "mock-serena",
						"version": "0.0.1",
					},
				},
			},
			1,
		)
	except ProbeError as exc:
		assert exc.reason == "error-response"
	else:
		raise AssertionError("expected ProbeError for initialize responses carrying an error field")


def test_sanitize_log_value_replaces_equals_and_whitespace() -> None:
	assert _sanitize_log_value("mock=serena 1") == "mock_serena_1"


def test_template_languages_match_serena_v1_2_0_fixture_contract() -> None:
	assert _read_supported_template_languages() == [
		"al",
		"ansible",
		"bash",
		"clojure",
		"cpp",
		"cpp_ccls",
		"crystal",
		"csharp",
		"csharp_omnisharp",
		"dart",
		"elixir",
		"elm",
		"erlang",
		"fortran",
		"fsharp",
		"go",
		"groovy",
		"haskell",
		"haxe",
		"hlsl",
		"java",
		"json",
		"julia",
		"kotlin",
		"lean4",
		"lua",
		"luau",
		"markdown",
		"matlab",
		"msl",
		"nix",
		"ocaml",
		"pascal",
		"perl",
		"php",
		"php_phpactor",
		"powershell",
		"python",
		"python_jedi",
		"python_ty",
		"r",
		"rego",
		"ruby",
		"ruby_solargraph",
		"rust",
		"scala",
		"solidity",
		"swift",
		"systemverilog",
		"terraform",
		"toml",
		"typescript",
		"typescript_vts",
		"vue",
		"yaml",
		"zig",
	]


def test_probe_cli_happy_path() -> None:
	result = _run_probe([sys.executable, str(FIXTURES_DIR / "mock_mcp_happy.py")])

	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert "SERENA_PROBE target=serena result=ok" in result.stderr
	assert "server_name=mock-serena" in result.stderr


def test_probe_cli_rejects_invalid_json() -> None:
	result = _run_probe([sys.executable, str(FIXTURES_DIR / "mock_mcp_invalid_json.py")])

	assert result.returncode == 1
	assert "reason=invalid-json" in result.stderr


def test_probe_cli_rejects_close_on_init() -> None:
	result = _run_probe([sys.executable, str(FIXTURES_DIR / "mock_mcp_close_on_init.py")])

	assert result.returncode == 1
	assert "reason=eof" in result.stderr


def test_probe_cli_rejects_error_response() -> None:
	result = _run_probe([sys.executable, str(FIXTURES_DIR / "mock_mcp_error_response.py")])

	assert result.returncode == 1
	assert "reason=error-response" in result.stderr


def test_probe_cli_rejects_id_mismatch() -> None:
	result = _run_probe([sys.executable, str(FIXTURES_DIR / "mock_mcp_id_mismatch.py")])

	assert result.returncode == 1
	assert "reason=id-mismatch" in result.stderr


def test_probe_cli_rejects_timeout() -> None:
	result = _run_probe([sys.executable, str(FIXTURES_DIR / "mock_mcp_timeout.py")], timeout=0.1)

	assert result.returncode == 1
	assert "reason=timeout" in result.stderr


def test_probe_cli_rejects_spawn_failure() -> None:
	result = _run_probe(["/definitely/missing/probe-server"])

	assert result.returncode == 1
	assert "reason=spawn-failed" in result.stderr


def test_probe_cli_kill_switch_skips_probe_and_succeeds() -> None:
	result = _run_probe(
		["/definitely/missing/probe-server"],
		env={"MCP_HANDSHAKE_PROBE_ENABLED": "false"},
	)

	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert "SERENA_PROBE target=serena result=skipped reason=disabled" in result.stderr


def test_setup_serena_writes_block_only_when_probe_succeeds() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		setup_script = _stage_setup_serena(root)
		home = root / "home"
		home.mkdir()
		github_env = root / "github.env"
		bin_dir = root / "bin"
		bin_dir.mkdir()
		fake_serena = bin_dir / "serena"
		_write_fake_serena(fake_serena)
		config_path = home / ".codex" / "config.toml"
		config_path.parent.mkdir(parents=True, exist_ok=True)
		config_path.write_text("[existing]\nvalue = \"keep\"\n", encoding="utf-8")

		result = _run_staged_setup(
			setup_script,
			home=home,
			path_value=f"{bin_dir}:{os.environ.get('PATH', '')}",
			github_env=github_env,
			extra_env={"FAKE_SERENA_FIXTURE": str(FIXTURES_DIR / "mock_mcp_happy.py")},
		)

		assert result.returncode == 0, result.stderr
		config_text = config_path.read_text(encoding="utf-8")
		assert "[mcp_servers.serena]" in config_text
		assert github_env.read_text(encoding="utf-8").splitlines()[-1] == "SERENA_AVAILABLE=true"
		assert (root / ".serena" / "project.yml").is_file()

		result = _run_staged_setup(
			setup_script,
			home=home,
			path_value=f"{bin_dir}:{os.environ.get('PATH', '')}",
			github_env=github_env,
			extra_env={"FAKE_SERENA_FIXTURE": str(FIXTURES_DIR / "mock_mcp_close_on_init.py")},
		)

		assert result.returncode == 0, result.stderr
		config_text = config_path.read_text(encoding="utf-8")
		assert "[existing]" in config_text
		assert "[mcp_servers.serena]" not in config_text
		assert github_env.read_text(encoding="utf-8").splitlines()[-1] == "SERENA_AVAILABLE=false"


def test_setup_serena_probe_kill_switch_forces_success() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		setup_script = _stage_setup_serena(root)
		home = root / "home"
		home.mkdir()
		github_env = root / "github.env"
		bin_dir = root / "bin"
		bin_dir.mkdir()
		fake_serena = bin_dir / "serena"
		_write_fake_serena(fake_serena)

		result = _run_staged_setup(
			setup_script,
			home=home,
			path_value=f"{bin_dir}:{os.environ.get('PATH', '')}",
			github_env=github_env,
			extra_env={
				"FAKE_SERENA_FIXTURE": str(FIXTURES_DIR / "mock_mcp_invalid_json.py"),
				"MCP_HANDSHAKE_PROBE_ENABLED": "false",
			},
		)

		assert result.returncode == 0, result.stderr
		config_text = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
		assert "[mcp_servers.serena]" in config_text
		assert github_env.read_text(encoding="utf-8").splitlines()[-1] == "SERENA_AVAILABLE=true"


def test_setup_serena_preserves_existing_project_config_and_uses_workspace_root() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		setup_stage_root = root / "staged-support"
		setup_stage_root.mkdir()
		setup_script = _stage_setup_serena(setup_stage_root)
		workspace = root / "workspace"
		workspace.mkdir()
		home = root / "home"
		home.mkdir()
		github_env = root / "github.env"
		bin_dir = root / "bin"
		bin_dir.mkdir()
		fake_serena = bin_dir / "serena"
		_write_fake_serena(fake_serena)

		existing_project = workspace / ".serena" / "project.yml"
		existing_project.parent.mkdir(parents=True, exist_ok=True)
		existing_project.write_text("project_name: \"keep-me\"\n", encoding="utf-8")

		result = _run_staged_setup(
			setup_script,
			home=home,
			path_value=f"{bin_dir}:{os.environ.get('PATH', '')}",
			github_env=github_env,
			extra_env={
				"FAKE_SERENA_FIXTURE": str(FIXTURES_DIR / "mock_mcp_happy.py"),
				"GITHUB_WORKSPACE": str(workspace),
			},
		)

		assert result.returncode == 0, result.stderr
		assert existing_project.read_text(encoding="utf-8") == 'project_name: "keep-me"\n'
		assert not (setup_stage_root / ".serena" / "project.yml").exists()


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
