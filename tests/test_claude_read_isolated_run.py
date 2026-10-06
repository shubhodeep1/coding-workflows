"""Read-profile Claude isolation: snapshot, relay fallback and staging contracts."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "scripts/claude_read_snapshot.py"
ENGINE = ROOT / "scripts/ai_engine.sh"


def fake_docker(bin_dir: Path) -> Path:
	"""Emulate build/run/rm without daemon access; keep argv out of logs."""
	script = bin_dir / "docker"
	script.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
root = pathlib.Path(__file__).parent
with (root / "docker_calls.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "image":
    sys.exit(1)  # Force the production image-cache path to exercise build.
if sys.argv[1] == "build":
    if (root / "docker_outcome.txt").exists() and (root / "docker_outcome.txt").read_text().strip() == "build-fail":
        sys.exit(1)
    print("fake-image")
elif sys.argv[1] == "rm":
    (root / "docker_stopped").write_text("yes")
elif sys.argv[1] == "run":
    outcome = (root / "docker_outcome.txt").read_text().strip() if (root / "docker_outcome.txt").exists() else "success"
    print("CLAUDE_READ_CONTAINER_READY", file=sys.stderr)
    if outcome == "wait":
        import time
        while not (root / "docker_stopped").exists():
            time.sleep(.05)
        sys.exit(143)
    if outcome == "timeout":
        sys.exit(124)
    if outcome == "limit-once" and not (root / "first_limit").exists():
        (root / "first_limit").touch()
        outcome = "limit"
    if outcome == "limit":
        print(json.dumps({"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}))
        sys.exit(1)
    if outcome == "crash":
        sys.exit(3)
    print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"done"}))
''', encoding="utf-8")
	script.chmod(0o755)
	return bin_dir / "docker_calls.jsonl"


def _snapshot(work: Path, destination: Path, **env: str) -> subprocess.CompletedProcess:
	return subprocess.run(["python3", str(SNAPSHOT), str(work), str(destination / "source"), str(destination / "gitdir")],
		capture_output=True, text=True, env={**os.environ, **env, "PYTHONDONTWRITEBYTECODE": "1"}, check=False)


def _git(work: Path, *args: str, gitdir: Path | None = None) -> str:
	git_env = os.environ.copy()
	if gitdir is not None:
		git_env.update(GIT_DIR=str(gitdir), GIT_WORK_TREE=str(work))
	return subprocess.check_output(["git", *args], cwd=work, text=True, env=git_env).strip()


def test_git_snapshot_keeps_history_but_not_config_or_credentials(tmp_path: Path) -> None:
	work = tmp_path / "work"
	work.mkdir()
	_git(work, "init", "-q")
	(work / "safe.py").write_text("version = 1\n", encoding="utf-8")
	_git(work, "add", "safe.py")
	_git(work, "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "first")
	(work / "safe.py").write_text("version = 2\n", encoding="utf-8")
	_git(work, "add", "safe.py")
	_git(work, "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "second")
	with (work / ".git/config").open("a") as config:
		config.write('[http "https://github.com/"]\n\textraheader = AUTHORIZATION: secret\n')
	(work / ".env").write_text("SECRET=never\n", encoding="utf-8")
	(work / "creds.key").write_text("never\n", encoding="utf-8")
	(work / "id_ed25519_sk.txt").write_text("never\n", encoding="utf-8")
	(work / "client.pem.txt").write_text("never\n", encoding="utf-8")
	(work / "tokens").mkdir()
	(work / "tokens/A").write_text("never\n", encoding="utf-8")
	(work / "bad.md").symlink_to(work / ".env")
	(work / ".codex-workflow-src").mkdir()
	(work / ".codex-workflow-src/hidden.py").write_text("never\n", encoding="utf-8")
	destination = tmp_path / "dest"
	result = _snapshot(work, destination)
	assert result.returncode == 0, result.stderr
	assert Path(result.stdout.strip()) == work / ".git/objects"
	assert (destination / "source/safe.py").read_text() == "version = 2\n"
	assert not any((destination / "source" / name).exists() for name in (".env", "creds.key", "id_ed25519_sk.txt", "client.pem.txt", "bad.md", ".codex-workflow-src", "tokens/A"))
	assert "extraheader" not in (destination / "gitdir/config").read_text()
	# Simulate the container's /gitobjects mount for the local git probe.
	(destination / "gitdir/objects/info/alternates").write_text(str(work / ".git/objects") + "\n")
	assert _git(destination / "source", "log", "-1", "--format=%s", gitdir=destination / "gitdir") == "second"
	assert _git(destination / "source", "log", "-2", "--format=%s", gitdir=destination / "gitdir").splitlines() == ["second", "first"]


@pytest.mark.parametrize("secret_name", [".env", "id_ed25519_sk.txt", "client.pem.txt"])
def test_filtered_git_history_cannot_recover_a_committed_secret(tmp_path: Path, secret_name: str) -> None:
	work = tmp_path / "work"
	work.mkdir()
	_git(work, "init", "-q")
	(work / "safe.py").write_text("public\n")
	(work / secret_name).write_text("COMMITTED_SECRET=never\n")
	_git(work, "add", "safe.py", secret_name)
	_git(work, "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "first")
	(work / secret_name).unlink()
	_git(work, "add", "-u")
	_git(work, "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "second")
	destination = tmp_path / "dest"
	result = _snapshot(work, destination)
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert not (destination / "source" / secret_name).exists()
	assert not (destination / "gitdir").exists()
	assert _git(destination / "source", "log", "-1", "--format=%s") == "snapshot"
	assert subprocess.run(["git", "show", f"HEAD:{secret_name}"], cwd=destination / "source", capture_output=True).returncode != 0


def test_unreachable_git_objects_are_not_mounted(tmp_path: Path) -> None:
	work = tmp_path / "work"
	work.mkdir()
	_git(work, "init", "-q")
	(work / "safe.py").write_text("public\n")
	_git(work, "add", "safe.py")
	_git(work, "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "first")
	subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=work, input=b"DANGLING_SECRET", capture_output=True, check=True)
	destination = tmp_path / "dest"
	result = _snapshot(work, destination)
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert not (destination / "gitdir").exists()
	assert _git(destination / "source", "log", "-1", "--format=%s") == "snapshot"


def test_history_path_with_leading_newline_is_not_aliased(tmp_path: Path) -> None:
	work = tmp_path / "work"
	work.mkdir()
	_git(work, "init", "-q")
	(work / "safe.py").write_text("public\n")
	(work / "\nsafe.py").write_text("HIDDEN=never\n")
	_git(work, "add", "safe.py", "\nsafe.py")
	_git(work, "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "first")
	(work / "\nsafe.py").unlink()
	_git(work, "add", "-u")
	_git(work, "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "second")
	destination = tmp_path / "dest"
	result = _snapshot(work, destination)
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert _git(destination / "source", "log", "-1", "--format=%s") == "snapshot"


def test_nongit_snapshot_filters_hidden_and_symlinks(tmp_path: Path) -> None:
	work = tmp_path / "work"
	work.mkdir()
	(work / "good.txt").write_text("public\n")
	(work / ".env").write_text("hidden\n")
	(work / "secret.key").write_text("hidden\n")
	(work / "link.txt").symlink_to("secret.key")
	result = _snapshot(work, tmp_path / "dest")
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert {p.name for p in (tmp_path / "dest/source").iterdir()} == {"good.txt", ".git"}


def test_pinned_git_worktree_and_hidden_claude_md(tmp_path: Path) -> None:
	work = tmp_path / "work"
	work.mkdir()
	git_dir = tmp_path / "host-gitdir"
	_git(work, "init", "-q", "--separate-git-dir", str(git_dir))
	(work / "CLAUDE.md").write_text("untrusted instructions\n")
	(work / "safe.py").write_text("data = 1\n")
	_git(work, "add", "safe.py", "CLAUDE.md")
	_git(work, "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "first")
	dest = tmp_path / "dest"
	result = subprocess.run(["python3", str(SNAPSHOT), str(work), str(dest / "source"), str(dest / "gitdir"),
		"--exclude-root-claude-md"], capture_output=True, text=True, check=False,
		env={**os.environ, "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(work), "PYTHONDONTWRITEBYTECODE": "1"})
	assert result.returncode == 0, result.stderr
	assert not (dest / "source/CLAUDE.md").exists()
	assert (dest / "source/safe.py").exists()
	assert result.stdout == ""
	assert not (dest / "gitdir").exists()
	assert _git(dest / "source", "log", "-1", "--format=%s") == "snapshot"


@pytest.fixture()
def isolated(tmp_path: Path):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log = fake_docker(bin_dir)
	(bin_dir / "ps").write_text("#!/bin/sh\nexit 0\n")
	(bin_dir / "ps").chmod(0o755)
	(bin_dir / "claude").write_text("#!/bin/sh\necho host-claude-ran >> '" + str(tmp_path / "host-cli") + "'\nexit 1\n")
	(bin_dir / "claude").chmod(0o755)
	work = tmp_path / "work"
	work.mkdir()
	_git(work, "init", "-q")
	(work / "source.py").write_text("public\n")
	_git(work, "add", "source.py")
	_git(work, "-c", "user.name=test", "-c", "user.email=test@invalid", "commit", "-qm", "fixture")
	pool = tmp_path / "pool"
	(pool / "tokens").mkdir(parents=True)
	(pool / "order").write_text("A\n")
	(pool / "tokens/A").write_text("TOK_OK\n")
	(pool / "tokens/A").chmod(0o600)
	runner = Path(tempfile.mkdtemp(prefix="cr-", dir="/tmp"))
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("Say done.\n")
	env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "AI_ENGINE", "CLAUDE_", "SUPPORT_"))}
	env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(tmp_path), RUNNER_TEMP=str(runner),
		CLAUDE_ENGINE_POOL_DIR=str(pool), SUPPORT_INSTRUCTIONS_FILE=str(ROOT / "unattended_system_instructions.md"),
		PYTHONDONTWRITEBYTECODE="1")
	try:
		yield tmp_path, work, pool, prompt, env, log
	finally:
		shutil.rmtree(runner)


def _run_isolated(case, role="SECURITY_AUDIT", **overrides):
	tmp, work, _pool, prompt, env, _log = case
	script = 'source "' + str(ENGINE) + '"; rc=0; claude_run "' + role + '" "' + str(prompt) + '" "' + str(tmp / "out.txt") + '" "' + str(work) + '" || rc=$?; echo "RC=${rc}"; echo "RUN_DIR=${AI_ENGINE_LAST_RUN_DIR:-}"'
	return subprocess.run(["bash", "-c", script], env={**env, **overrides}, text=True, capture_output=True, timeout=90, check=False)


def test_sandbox_success_has_no_real_token_in_docker_arguments(isolated) -> None:
	result = _run_isolated(isolated)
	assert result.returncode == 0 and "RC=0" in result.stdout, result.stderr
	assert "CLAUDE_READ_ISOLATION role=SECURITY_AUDIT outcome=ready" in result.stderr
	assert (isolated[0] / "out.txt").read_text() == "done"
	assert not (isolated[0] / "host-cli").exists()
	args = [json.loads(row) for row in isolated[5].read_text().splitlines()]
	run = next(row for row in args if row[0] == "run")
	assert all(flag in run for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder"))
	assert "TOK_OK" not in json.dumps(run) and str(isolated[2]) not in json.dumps(run)
	run_dir = Path(next(line[8:] for line in result.stdout.splitlines() if line.startswith("RUN_DIR=")))
	assert (run_dir / "successful-transcript.jsonl").is_symlink()


def test_unavailable_and_all_accounts_fail_closed(isolated) -> None:
	inside = isolated[1] / "pool"
	(inside / "tokens").mkdir(parents=True)
	(inside / "order").write_text("A\n")
	(inside / "tokens/A").write_text("TOK_OK\n")
	(inside / "tokens/A").chmod(0o600)
	result = _run_isolated(isolated, CLAUDE_ENGINE_POOL_DIR=str(inside))
	assert "RC=75" in result.stdout and "reason=isolation_pool_overlap" in result.stderr
	assert not (isolated[0] / "host-cli").exists()
	result = _run_isolated(isolated, SUPPORT_INSTRUCTIONS_FILE=str(isolated[2] / "tokens/A"))
	assert "RC=75" in result.stdout and "reason=isolation_pool_overlap" in result.stderr
	(isolated[0] / "bin/docker_outcome.txt").write_text("limit")
	result = _run_isolated(isolated)
	assert "RC=75" in result.stdout and "reason=all_accounts_failed" in result.stderr
	assert not (isolated[0] / "host-cli").exists()


def test_timeout_does_not_fallback_to_host(isolated) -> None:
	(isolated[0] / "bin/docker_outcome.txt").write_text("timeout")
	result = _run_isolated(isolated)
	assert "RC=124" in result.stdout and "AI_ENGINE_FALLBACK" not in result.stderr
	assert not (isolated[0] / "host-cli").exists()


def test_image_build_failure_falls_back_without_host_claude(isolated) -> None:
	(isolated[0] / "bin/docker_outcome.txt").write_text("build-fail")
	result = _run_isolated(isolated)
	assert "RC=75" in result.stdout
	assert "AI_ENGINE_FALLBACK role=SECURITY_AUDIT reason=isolation_image_build_failed" in result.stderr
	assert not (isolated[0] / "host-cli").exists()


def test_limited_account_moves_to_next_account(isolated) -> None:
	(isolated[2] / "order").write_text("A\nB\n")
	(isolated[2] / "tokens/B").write_text("TOK_SECOND\n")
	(isolated[2] / "tokens/B").chmod(0o600)
	(isolated[0] / "bin/docker_outcome.txt").write_text("limit-once")
	result = _run_isolated(isolated)
	assert "RC=0" in result.stdout, result.stderr
	assert "account=A outcome=usage_limit" in result.stderr
	assert "account=B outcome=success" in result.stderr
	assert "TOK_SECOND" not in isolated[5].read_text()


def test_relay_start_failure_tries_next_account(isolated) -> None:
	(isolated[2] / "order").write_text("A\nB\n")
	# The pool inventory checks only non-empty, regular files; the broker
	# refuses an insecure account file, and must not stop the second attempt.
	(isolated[2] / "tokens/A").chmod(0o644)
	(isolated[2] / "tokens/B").write_text("TOK_SECOND\n")
	(isolated[2] / "tokens/B").chmod(0o600)
	result = _run_isolated(isolated)
	assert "RC=0" in result.stdout, result.stderr
	assert "account=A outcome=crashed reason=relay_unavailable" in result.stderr
	assert "account=B outcome=success" in result.stderr


def test_term_removes_container_and_relay(isolated) -> None:
	tmp, work, _pool, prompt, env, log = isolated
	(tmp / "bin/docker_outcome.txt").write_text("wait")
	run_dir = tmp / "claude-run"
	run_dir.mkdir()
	proc = subprocess.Popen(["bash", str(ROOT / "scripts/claude_read_isolated_run.sh"), "SECURITY_AUDIT",
		str(prompt), str(tmp / "out.txt"), str(work), str(run_dir), "claude-opus-5-5", "high",
		str(ROOT / "unattended_system_instructions.md"), "false"],
		stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
	try:
		proc.stdin.write(b"A\n")
		proc.stdin.close()
		for _ in range(100):
			if log.exists() and any(json.loads(line)[0] == "run" for line in log.read_text().splitlines()):
				break
			time.sleep(.1)
		else:
			pytest.fail("container attempt did not start")
		proc.send_signal(signal.SIGTERM)
		proc.wait(timeout=5)
		assert any(json.loads(line)[0:2] == ["rm", "-f"] for line in log.read_text().splitlines())
	finally:
		if proc.poll() is None:
			os.killpg(proc.pid, signal.SIGKILL)
			proc.wait(timeout=5)
		proc.stdout.close()
		proc.stderr.close()


def test_support_staging_covers_read_runner_and_dependencies() -> None:
	paths = (ROOT / "scripts/stage_workflow_support.sh", ROOT / ".github/workflows/implement.yml",
		ROOT / ".github/workflows/orchestrate_poll.yml")
	for path in paths:
		text = path.read_text()
		for name in ("claude_read_isolated_run.sh", "claude_read_snapshot.py", "claude_anthropic_relay.py", "review_untrusted_workspace.py"):
			assert name in text, (path, name)
