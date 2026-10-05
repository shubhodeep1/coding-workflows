"""Behavioral isolation checks for the shared Semble sandbox helpers."""

import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts/install_semble.sh"
BUILD = ROOT / "scripts/build_semble_wrapper.sh"
IMAGE_ID = "sha256:" + "a" * 64


def _fake_docker(tmp_path: Path, *, fail_build: bool = False, slow_query: bool = False) -> tuple[Path, Path, Path]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log_file = tmp_path / "docker.jsonl"
	context_dir = tmp_path / "captured-context"
	stub = bin_dir / "docker"
	stub.write_text(
		"#!/usr/bin/env python3\n"
		"import json, os, pathlib, shutil, sys, time\n"
		f"log = pathlib.Path({str(log_file)!r})\n"
		f"context = pathlib.Path({str(context_dir)!r})\n"
		"argv = sys.argv[1:]\n"
		"with log.open('a') as out:\n"
		"    out.write(json.dumps({'argv': argv, 'env': dict(os.environ)}) + '\\n')\n"
		"if argv[0] == 'build':\n"
		"    shutil.copytree(argv[-1], context)\n"
		f"    sys.exit({1 if fail_build else 0})\n"
		"if argv[:2] == ['image', 'inspect']:\n"
		f"    print({IMAGE_ID!r})\n"
		"elif argv[0] == 'run':\n"
		"    if '/opt/semble/semble_index.py' in argv:\n"
		"        for value in argv:\n"
		"            if value.startswith('type=bind,src=') and value.endswith(',dst=/repo,readonly'):\n"
		"                snapshot = pathlib.Path(value.split(',')[1][4:])\n"
		"                if list(snapshot.rglob('.git')) or list(snapshot.rglob('.codex-workflow-src')):\n"
		"                    sys.exit(4)\n"
		"            if value.startswith('type=bind,src=') and value.endswith(',dst=/out'):\n"
		"                pathlib.Path(value.split(',')[1][4:], 'index.pkl').write_bytes(b'opaque index')\n"
		"                break\n"
		"    else:\n"
		+ ("        time.sleep(30)\n" if slow_query else "")
		+
		"        print('[1] example.py:1-1\\nhello world')\n"
		"elif argv[0] != 'rm':\n"
		"    sys.exit(3)\n",
		encoding="utf-8",
	)
	stub.chmod(0o755)
	return bin_dir, log_file, context_dir


def _env(tmp_path: Path, bin_dir: Path, env_file: Path) -> dict[str, str]:
	return {
		**os.environ,
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"HOME": str(tmp_path),
		"GITHUB_ENV": str(env_file),
		"GH_TOKEN": "secret-token",
		"OPENROUTER_API_KEY": "secret-key",
		"TG_BOT_SECRET": "secret-bot",
		"PYTHONDONTWRITEBYTECODE": "1",
	}


def _calls(log_file: Path) -> list[dict]:
	return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]


def test_install_uses_only_hash_locked_context_and_scrubbed_docker(tmp_path: Path) -> None:
	bin_dir, log_file, context_dir = _fake_docker(tmp_path)
	env_file = tmp_path / "install.env"
	result = subprocess.run(["bash", str(INSTALL)], env=_env(tmp_path, bin_dir, env_file), capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert env_file.read_text() == f"SEMBLE_SANDBOX_IMAGE_ID={IMAGE_ID}\nSEMBLE_AVAILABLE=true\n"
	assert sorted(p.name for p in context_dir.iterdir()) == ["Dockerfile", "requirements.lock", "semble_index.py", "semble_query.py"]
	dockerfile = (context_dir / "Dockerfile").read_text()
	assert "FROM python:3.12.12-slim-bookworm@sha256:" in dockerfile
	assert "--require-hashes --no-deps --only-binary=:all:" in dockerfile
	lock = (context_dir / "requirements.lock").read_text()
	lines = lock.splitlines()
	assert any(line.startswith("semble==0.1.3 --hash=sha256:") for line in lines)
	assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==\S+( --hash=sha256:[a-f0-9]{64})+", line) for line in lines)
	for call in _calls(log_file):
		assert not {"GH_TOKEN", "OPENROUTER_API_KEY", "TG_BOT_SECRET"} & call["env"].keys()
	assert len(_calls(log_file)) == 2


def test_install_build_failure_is_optional_with_no_host_fallback(tmp_path: Path) -> None:
	bin_dir, log_file, _ = _fake_docker(tmp_path, fail_build=True)
	env_file = tmp_path / "install.env"
	result = subprocess.run(["bash", str(INSTALL)], env=_env(tmp_path, bin_dir, env_file), capture_output=True, text=True)
	assert result.returncode == 0
	assert env_file.read_text() == "SEMBLE_AVAILABLE=false\n"
	assert len(_calls(log_file)) == 1


def test_install_without_docker_falls_back(tmp_path: Path) -> None:
	env_file = tmp_path / "install.env"
	result = subprocess.run(["/bin/bash", str(INSTALL)], env={"PATH": "", "GITHUB_ENV": str(env_file)}, capture_output=True, text=True)
	assert result.returncode == 0
	assert env_file.read_text() == "SEMBLE_AVAILABLE=false\n"


def test_index_and_query_never_mount_git_or_inherit_credentials(tmp_path: Path) -> None:
	bin_dir, log_file, _ = _fake_docker(tmp_path)
	repo = tmp_path / "repo"
	(repo / ".git").mkdir(parents=True)
	(repo / "sub" / ".git").mkdir(parents=True)
	(repo / ".codex-workflow-src" / ".git").mkdir(parents=True)
	(repo / "example.py").write_text("hello world\n")
	index = tmp_path / "index" / "index.pkl"
	bin_output = tmp_path / "wrapper" / "bin"
	env_file = tmp_path / "build.env"
	env = _env(tmp_path, bin_dir, env_file)
	env.update({"GITHUB_WORKSPACE": str(repo), "SEMBLE_INDEX_PATH": str(index), "SEMBLE_WRAPPER_DIR": str(bin_output)})
	result = subprocess.run(["bash", str(BUILD)], env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert "SEMBLE_INDEX_AVAILABLE=true" in env_file.read_text()
	assert index.read_bytes() == b"opaque index"
	index_call = next(call for call in _calls(log_file) if call["argv"][0] == "run")
	args = index_call["argv"]
	for option in ("--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"):
		assert option in args
	assert not {"GH_TOKEN", "OPENROUTER_API_KEY", "TG_BOT_SECRET"} & index_call["env"].keys()
	assert "-e" not in args and "--env" not in args
	snapshot_mount = next(value for value in args if value.endswith(",dst=/repo,readonly"))
	# Fake docker refuses nested .git and support checkouts during execution.
	assert "src=" in snapshot_mount and "--network" in args
	launcher = bin_output / "semble"
	assert launcher.read_text().startswith("#!/usr/bin/env bash")
	assert "pickle" not in launcher.read_text()
	bad_index = subprocess.run([str(launcher), "query", "x", "--index", "/tmp/other", "--top-k", "2", "--format", "text"], env=env)
	assert bad_index.returncode == 2
	bad_k = subprocess.run([str(launcher), "query", "x", "--index", str(index), "--top-k", "abc", "--format", "text"], env=env)
	assert bad_k.returncode == 2
	query = "$(touch /tmp/pwn); x"
	good = subprocess.run([str(launcher), "query", query, "--index", str(index), "--top-k", "2", "--format", "text"], env=env, capture_output=True, text=True)
	assert good.returncode == 0, good.stderr
	assert good.stdout.startswith("[1] example.py:1-1")
	query_args = [call["argv"] for call in _calls(log_file) if call["argv"][0] == "run"][-1]
	assert query_args[query_args.index("--") + 1] == query
	assert "--network" in query_args and "none" in query_args
	assert f"type=bind,src={index},dst=/index/index.pkl,readonly" in query_args
	assert not any("src=" + str(index.parent) + "," in arg for arg in query_args)


def test_launcher_timeout_removes_its_container(tmp_path: Path) -> None:
	bin_dir, log_file, _ = _fake_docker(tmp_path, slow_query=True)
	repo = tmp_path / "repo"
	repo.mkdir()
	(repo / "example.py").write_text("hello world\n")
	index = tmp_path / "index.pkl"
	env = _env(tmp_path, bin_dir, tmp_path / "build.env")
	env.update({"GITHUB_WORKSPACE": str(repo), "SEMBLE_INDEX_PATH": str(index), "SEMBLE_WRAPPER_DIR": str(tmp_path / "bin-out")})
	assert subprocess.run(["bash", str(BUILD)], env=env, capture_output=True).returncode == 0
	launcher = tmp_path / "bin-out" / "semble"
	result = subprocess.run(
		["timeout", "--kill-after=2s", "0.5s", str(launcher), "query", "x", "--index", str(index), "--top-k", "1", "--format", "text"],
		env=env, capture_output=True, timeout=5,
	)
	assert result.returncode != 0
	assert any(call["argv"][:2] == ["rm", "-f"] for call in _calls(log_file))
