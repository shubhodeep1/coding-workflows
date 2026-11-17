#!/usr/bin/env python3
"""Contract tests for scripts/setup_serena.sh TOML emission."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "mcp_handshake"
BASH_BIN = shutil.which("bash") or "bash"


def _stage_setup_serena(tmp_root: Path) -> Path:
	stage_scripts = tmp_root / "scripts"
	stage_templates = stage_scripts / "templates"
	stage_templates.mkdir(parents=True, exist_ok=True)
	shutil.copy2(SCRIPTS_DIR / "setup_serena.sh", stage_scripts / "setup_serena.sh")
	shutil.copy2(SCRIPTS_DIR / "mcp_handshake_probe.py", stage_scripts / "mcp_handshake_probe.py")
	shutil.copy2(SCRIPTS_DIR / "templates" / "serena_project.yml.j2", stage_templates / "serena_project.yml.j2")
	return stage_scripts / "setup_serena.sh"


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


def _run_setup(
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


def test_setup_serena_emits_parser_valid_toml_and_quotes_command_path() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp) / "workspace with spaces"
		root.mkdir(parents=True)
		setup_script = _stage_setup_serena(root)
		home = root / "home"
		home.mkdir()
		github_env = root / "github.env"
		bin_dir = root / "bin with spaces"
		bin_dir.mkdir()
		fake_serena = bin_dir / "serena"
		_write_fake_serena(fake_serena)

		config_path = home / ".codex" / "config.toml"
		config_path.parent.mkdir(parents=True, exist_ok=True)
		config_path.write_text("[existing]\nvalue = \"keep\"\n", encoding="utf-8")

		result = _run_setup(
			setup_script,
			home=home,
			path_value=f"{bin_dir}:{os.environ.get('PATH', '')}",
			github_env=github_env,
			extra_env={
				"FAKE_SERENA_FIXTURE": str(FIXTURES_DIR / "mock_mcp_happy.py"),
				"SERENA_STARTUP_TIMEOUT_SEC": "45",
			},
		)

		assert result.returncode == 0, result.stderr
		body = config_path.read_text(encoding="utf-8")
		parsed = tomllib.loads(body)
		assert parsed["existing"]["value"] == "keep"
		assert parsed["mcp_servers"]["serena"]["command"] == str(fake_serena)
		assert parsed["mcp_servers"]["serena"]["args"] == [
			"start-mcp-server",
			"--context=codex",
			"--project-from-cwd",
			"--transport",
			"stdio",
		]
		assert parsed["mcp_servers"]["serena"]["startup_timeout_sec"] == 45
		assert f"command = {json.dumps(str(fake_serena))}" in body
		assert body.count("[mcp_servers.serena]") == 1


def test_setup_serena_replaces_existing_block_without_duplication() -> None:
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
		config_path.write_text(
			"[existing]\nvalue = \"keep\"\n\n"
			"[mcp_servers.serena]\n"
			"command = \"/old/path\"\n"
			"args = [\"old\"]\n"
			"startup_timeout_sec = 99\n",
			encoding="utf-8",
		)

		for _ in range(2):
			result = _run_setup(
				setup_script,
				home=home,
				path_value=f"{bin_dir}:{os.environ.get('PATH', '')}",
				github_env=github_env,
				extra_env={"FAKE_SERENA_FIXTURE": str(FIXTURES_DIR / "mock_mcp_happy.py")},
			)
			assert result.returncode == 0, result.stderr

		body = config_path.read_text(encoding="utf-8")
		parsed = tomllib.loads(body)
		assert parsed["existing"]["value"] == "keep"
		assert parsed["mcp_servers"]["serena"]["command"] == str(fake_serena)
		assert body.count("[mcp_servers.serena]") == 1


def test_setup_serena_replaces_existing_block_when_adjacent_tables_have_trailing_comments() -> None:
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
		config_path.write_text(
			'[existing] # keep-me\nvalue = "keep"\n\n'
			'[mcp_servers.serena] # stale\n'
			'command = "/old/path"\n'
			'args = ["old"]\n\n'
			'[after] # trailing comment\nvalue = "after"\n',
			encoding="utf-8",
		)

		result = _run_setup(
			setup_script,
			home=home,
			path_value=f"{bin_dir}:{os.environ.get('PATH', '')}",
			github_env=github_env,
			extra_env={"FAKE_SERENA_FIXTURE": str(FIXTURES_DIR / "mock_mcp_happy.py")},
		)

		assert result.returncode == 0, result.stderr
		body = config_path.read_text(encoding="utf-8")
		parsed = tomllib.loads(body)
		assert parsed["existing"]["value"] == "keep"
		assert parsed["after"]["value"] == "after"
		assert parsed["mcp_servers"]["serena"]["command"] == str(fake_serena)
		assert body.count("[mcp_servers.serena]") == 1


def test_setup_serena_clears_stale_block_fail_soft_when_binary_unavailable() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		setup_script = _stage_setup_serena(root)
		home = root / "home"
		home.mkdir()
		github_env = root / "github.env"

		config_path = home / ".codex" / "config.toml"
		config_path.parent.mkdir(parents=True, exist_ok=True)
		config_path.write_text(
			"[existing]\nvalue = \"keep\"\n\n"
			"[mcp_servers.serena]\n"
			"command = \"/stale/path\"\n"
			"args = [\"start-mcp-server\"]\n",
			encoding="utf-8",
		)

		result = _run_setup(
			setup_script,
			home=home,
			path_value="",
			github_env=github_env,
			extra_env={"SERENA_FALLBACK_TARGET": "implement"},
		)

		assert result.returncode == 0, result.stderr
		body = config_path.read_text(encoding="utf-8")
		parsed = tomllib.loads(body)
		assert parsed == {"existing": {"value": "keep"}}
		assert "[mcp_servers.serena]" not in body
		assert "SERENA_FALLBACK target=implement reason=setup-failure" in result.stderr
		assert github_env.read_text(encoding="utf-8").splitlines()[-1] == "SERENA_AVAILABLE=false"


def test_setup_serena_uses_workspace_for_project_path_and_preserves_existing_project() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		stage_root = root / "staged-support"
		stage_root.mkdir()
		setup_script = _stage_setup_serena(stage_root)
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
		existing_project.write_text("project_name = \"keep\"\n", encoding="utf-8")

		result = _run_setup(
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
		assert existing_project.read_text(encoding="utf-8") == 'project_name = "keep"\n'
		assert not (stage_root / ".serena" / "project.yml").exists()


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
