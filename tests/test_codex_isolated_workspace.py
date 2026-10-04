#!/usr/bin/env python3
"""Unit tests for scripts/codex_isolated_workspace.py.

The module builds the disposable copy an isolated Codex agent works on and
carries the agent's file changes back. These tests pin the security
properties: no .git (and so no GH_PAT remote URL or checkout extraheader)
reaches the copy, secret-looking and untracked files stay out of read-only
snapshots, symlinks and special files never come back, and a transfer is
all-or-nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE = REPO_ROOT / "scripts" / "codex_isolated_workspace.py"
TOKEN = "ghp_isolationtesttoken000000000000000000"


def run(command, *args, check=True, hide=None):
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_") and key != "CODEX_ISOLATED_HIDE"}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	if hide is not None:
		env["CODEX_ISOLATED_HIDE"] = hide
	proc = subprocess.run(
		[sys.executable, str(MODULE), command, *[str(a) for a in args]],
		capture_output=True,
		text=True,
		env=env,
	)
	if check and proc.returncode != 0:
		raise AssertionError(f"{command} failed: {proc.stderr}")
	return proc


def git(repo, *args):
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
	return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path):
	"""A checkout whose .git config carries a token, like actions/checkout leaves it."""
	host = tmp_path / "host"
	host.mkdir()
	git(host, "init", "-q")
	git(host, "remote", "add", "origin", f"https://x-access-token:{TOKEN}@github.com/o/r")
	git(host, "config", "http.https://github.com/.extraheader", f"AUTHORIZATION: basic {TOKEN}")
	(host / "src").mkdir()
	(host / "src" / "app.py").write_text("print('app')\n")
	(host / "frontend").mkdir()
	(host / "frontend" / "index.ts").write_text("export {}\n")
	(host / "run.sh").write_text("#!/bin/sh\necho run\n")
	(host / "run.sh").chmod(0o755)
	(host / ".env").write_text("SECRET=1\n")
	(host / "deploy.pem").write_text("-----BEGIN-----\n")
	(host / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
	os.symlink("/etc/passwd", host / "link")
	git(host, "add", "-A")
	git(host, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
	(host / "untracked.txt").write_text("not tracked\n")
	return host


def tree(root):
	return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink())


def test_readonly_snapshot_takes_tracked_files_only_and_skips_secrets(repo, tmp_path):
	dest = tmp_path / "copy"
	proc = run("snapshot-readonly", repo, dest)
	assert tree(dest) == ["frontend/index.ts", "run.sh", "src/app.py"]
	assert "skipped_large=1" in proc.stderr
	assert os.access(dest / "run.sh", os.X_OK)


def test_credential_paths_are_absent_from_snapshots_and_synthetic_git(repo, tmp_path):
	hidden = (
		".npmrc", ".netrc", ".pypirc", ".ssh/id_ed25519", ".config/gh/hosts.yml",
		"client_secret.json", "secrets.json", "credentials.yaml", "secrets.yml",
		"credentials.toml", "credentials.ini", "aws_credentials", "aws_credentials.json",
		"client-secret.json", "client-secret.yaml", "secret.json", "credentials.txt",
	)
	for name in hidden:
		path = repo / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("credential value\n")
	git(repo, "add", "-A")
	git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "add credential fixtures")
	readonly = tmp_path / "readonly"
	workspace, _manifest = snapshot_ws(repo, tmp_path)
	run("snapshot-readonly", repo, readonly)
	run("seed-git", repo, readonly)
	run("seed-git", repo, workspace)
	for copied in (readonly, workspace):
		for name in hidden:
			assert not (copied / name).exists()
			assert subprocess.run(["git", "show", f"HEAD:{name}"], cwd=copied, capture_output=True).returncode != 0


def test_source_files_about_secrets_remain_editable(repo, tmp_path):
	paths = (
		"src/secret_manager.py", "src/credential_provider.py", "tests/test_secrets.py",
		"src/secret.py", "src/secrets.py", "src/credentials.ts", "src/credential.go",
	)
	for name in paths:
		path = repo / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("original\n")
	git(repo, "add", "-A")
	git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "add source")
	readonly = tmp_path / "readonly"
	workspace, manifest = snapshot_ws(repo, tmp_path)
	run("snapshot-readonly", repo, readonly)
	for name in paths:
		assert (readonly / name).read_text() == "original\n"
		(workspace / name).write_text("edited\n")
	run("transfer", repo, workspace, manifest)
	for name in paths:
		assert (repo / name).read_text() == "edited\n"


def test_readonly_snapshot_limits_fail_closed(repo, tmp_path, monkeypatch):
	sys.path.insert(0, str(MODULE.parent))
	import codex_isolated_workspace as module

	monkeypatch.setattr(module, "MAX_READONLY_FILES", 1)
	with pytest.raises(module.Rejected):
		module.snapshot_readonly(repo, tmp_path / "copy")


def test_seed_git_copies_objects_but_no_config_or_credentials(repo, tmp_path):
	dest = tmp_path / "copy"
	run("snapshot-readonly", repo, dest)
	run("seed-git", repo, dest)
	config = (dest / ".git" / "config").read_text()
	assert TOKEN not in config
	assert "extraheader" not in config and "remote" not in config
	assert not list((dest / ".git").rglob("FETCH_HEAD"))
	# Neither the worktree nor the synthetic HEAD may expose excluded blobs.
	status = git(dest, "status", "--porcelain")
	assert status == ""
	for hidden in (".env", "deploy.pem", "big.bin"):
		assert subprocess.run(["git", "show", f"HEAD:{hidden}"], cwd=dest, capture_output=True).returncode != 0
	assert git(dest, "show", "HEAD:src/app.py") == "print('app')\n"


def snapshot_ws(repo, tmp_path):
	dest = tmp_path / "work"
	manifest = tmp_path / "manifest.json"
	run("snapshot-workspace", repo, dest, manifest)
	return dest, manifest


def test_workspace_snapshot_excludes_git_and_keeps_symlinks(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	assert not (dest / ".git").exists()
	assert not (dest / ".env").exists() and not (dest / "deploy.pem").exists()
	assert (dest / "link").is_symlink() and os.readlink(dest / "link") == "/etc/passwd"
	assert (dest / "untracked.txt").exists()
	entries = json.loads(manifest.read_text())["entries"]
	assert entries["link"] == ["l", "/etc/passwd"]
	assert not any(name == ".git" or name.startswith(".git/") for name in entries)
	assert ".env" not in entries and "deploy.pem" not in entries


def test_workspace_snapshot_and_transfer_exclude_nested_credentials(repo, tmp_path):
	(repo / "secrets").mkdir()
	(repo / "secrets" / "token.txt").write_text("sensitive\n")
	dest, manifest = snapshot_ws(repo, tmp_path)
	assert not (dest / "secrets").exists()
	(dest / ".env").write_text("injected\n")
	(dest / "src" / "app.py").write_text("changed\n")
	run("transfer", repo, dest, manifest)
	assert (repo / ".env").read_text() == "SECRET=1\n"
	assert (repo / "secrets" / "token.txt").read_text() == "sensitive\n"
	assert (repo / "src" / "app.py").read_text() == "changed\n"


def test_workspace_snapshot_skips_worktree_git_file(repo, tmp_path):
	(repo / "nested").mkdir()
	(repo / "nested" / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
	dest, manifest = snapshot_ws(repo, tmp_path)
	assert not (dest / "nested" / ".git").exists()


def test_transfer_applies_new_changed_deleted_files_and_modes(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "src" / "app.py").write_text("print('changed')\n")
	(dest / "src" / "new.py").write_text("x = 1\n")
	(dest / "frontend" / "index.ts").unlink()
	(dest / "tool.sh").write_text("#!/bin/sh\n")
	(dest / "tool.sh").chmod(0o755)
	(dest / "deep" / "er").mkdir(parents=True)
	(dest / "deep" / "er" / "f.txt").write_text("f\n")
	(dest / ".git").mkdir()
	(dest / ".git" / "config").write_text("[core]\n")
	proc = run("transfer", repo, dest, manifest)
	assert (repo / "src" / "app.py").read_text() == "print('changed')\n"
	assert (repo / "src" / "new.py").read_text() == "x = 1\n"
	assert not (repo / "frontend" / "index.ts").exists()
	assert os.access(repo / "tool.sh", os.X_OK)
	assert (repo / "deep" / "er" / "f.txt").exists()
	assert TOKEN in (repo / ".git" / "config").read_text(), "host .git must never be touched"
	assert "written=4 deleted=1" in proc.stderr


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_transfer_refuses_symlink_and_special_results_without_host_writes(repo, tmp_path, kind):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "src" / "app.py").write_text("print('changed')\n")
	if kind == "symlink":
		os.symlink(str(Path.home() / ".ssh" / "id_rsa"), dest / "evil")
	else:
		os.mkfifo(dest / "evil")
	proc = run("transfer", repo, dest, manifest, check=False)
	assert proc.returncode == 1
	assert "CODEX_ISOLATION transfer rejected" in proc.stderr
	assert (repo / "src" / "app.py").read_text() == "print('app')\n"
	assert not (repo / "evil").exists() and not (repo / "evil").is_symlink()


def test_transfer_refuses_changed_symlink(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "link").unlink()
	os.symlink("/etc/shadow", dest / "link")
	assert run("transfer", repo, dest, manifest, check=False).returncode == 1
	assert os.readlink(repo / "link") == "/etc/passwd"


def test_transfer_refuses_when_host_changed_since_snapshot(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "src" / "app.py").write_text("agent\n")
	(repo / "src" / "app.py").write_text("host moved on\n")
	proc = run("transfer", repo, dest, manifest, check=False)
	assert proc.returncode == 1 and "host path changed since snapshot" in proc.stderr
	assert (repo / "src" / "app.py").read_text() == "host moved on\n"


def test_transfer_refuses_writes_through_a_host_symlinked_parent(repo, tmp_path):
	outside = tmp_path / "outside"
	outside.mkdir()
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "pkg").mkdir()
	(dest / "pkg" / "x.py").write_text("x\n")
	os.symlink(outside, repo / "pkg")  # host gains a symlink where the agent created a directory
	proc = run("transfer", repo, dest, manifest, check=False)
	assert proc.returncode == 1
	assert not (outside / "x.py").exists()


def test_transfer_handles_directory_replaced_by_file(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "frontend" / "index.ts").unlink()
	(dest / "frontend").rmdir()
	(dest / "frontend").write_text("now a file\n")
	run("transfer", repo, dest, manifest)
	assert (repo / "frontend").read_text() == "now a file\n"


def test_transfer_rejects_host_only_file_before_deleting_directory(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "frontend" / "index.ts").unlink()
	(dest / "frontend").rmdir()
	(dest / "frontend").write_text("replacement\n")
	(repo / "frontend" / "host-only.txt").write_text("keep\n")
	proc = run("transfer", repo, dest, manifest, check=False)
	assert proc.returncode == 1
	assert (repo / "frontend" / "index.ts").read_text() == "export {}\n"
	assert (repo / "frontend" / "host-only.txt").read_text() == "keep\n"


def test_transfer_rolls_back_prior_writes_and_deletes_on_late_failure(repo, tmp_path, monkeypatch):
	sys.path.insert(0, str(MODULE.parent))
	import codex_isolated_workspace as module

	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "frontend" / "index.ts").unlink()
	(dest / "src" / "app.py").write_text("changed\n")
	(dest / "src" / "new.py").write_text("new\n")
	real_replace = module.os.replace

	def fail_new_file(source, target):
		if str(target).endswith("/src/new.py"):
			raise OSError("simulated late write failure")
		return real_replace(source, target)

	monkeypatch.setattr(module.os, "replace", fail_new_file)
	with pytest.raises(OSError):
		module.transfer(repo, dest, manifest)
	assert (repo / "frontend" / "index.ts").read_text() == "export {}\n"
	assert (repo / "src" / "app.py").read_text() == "print('app')\n"
	assert not (repo / "src" / "new.py").exists()


def test_transfer_restores_file_replaced_by_directory_on_failure(repo, tmp_path, monkeypatch):
	sys.path.insert(0, str(MODULE.parent))
	import codex_isolated_workspace as module

	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "run.sh").unlink()
	(dest / "run.sh").mkdir()
	(dest / "run.sh" / "new.py").write_text("new\n")
	real_replace = module.os.replace

	def fail_new_file(source, target):
		if str(target).endswith("/run.sh/new.py"):
			raise OSError("simulated late write failure")
		return real_replace(source, target)

	monkeypatch.setattr(module.os, "replace", fail_new_file)
	with pytest.raises(OSError):
		module.transfer(repo, dest, manifest)
	assert (repo / "run.sh").read_text() == "#!/bin/sh\necho run\n"
	assert (repo / "run.sh").is_file()


def test_prep_roots_are_kept_and_never_transferred(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "node_modules" / "pkg").mkdir(parents=True)
	(dest / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n")
	(dest / "src" / "app.py").write_text("build backend wrote this\n")
	(dest / "src" / "app.egg-info").mkdir()
	(dest / "src" / "app.egg-info" / "PKG-INFO").write_text("x\n")
	run("prep-finalize", repo, dest, manifest)
	roots = json.loads(manifest.read_text())["prep_roots"]
	assert roots == ["node_modules", "src/app.egg-info"]
	# Build-backend writes to snapshotted files are undone before the agent runs.
	assert (dest / "src" / "app.py").read_text() == "print('app')\n"
	assert (dest / "node_modules" / "pkg" / "index.js").exists()
	# A later attempt keeps prep roots, and agent writes inside them stay put.
	run("snapshot-workspace", repo, dest, manifest)
	assert (dest / "node_modules" / "pkg" / "index.js").exists()
	(dest / "node_modules" / ".cache").mkdir()
	(dest / "node_modules" / ".cache" / "x").write_text("cache\n")
	run("transfer", repo, dest, manifest)
	assert not (repo / "node_modules").exists()
	assert not (repo / "src" / "app.egg-info").exists()


def test_new_snapshot_discards_leftovers_from_an_earlier_attempt(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "leftover.txt").write_text("from a killed attempt\n")
	run("snapshot-workspace", repo, dest, manifest)
	assert not (dest / "leftover.txt").exists()


def test_copy_include_copies_regular_files_only(tmp_path):
	src = tmp_path / "runtime"
	(src / "sub").mkdir(parents=True)
	(src / "sub" / "a.txt").write_text("a\n")
	(src / ".git").write_text("gitdir: /x\n")
	(src / "secrets").mkdir()
	(src / "secrets" / "token.txt").write_text("secret\n")
	(src / ".env").write_text("secret\n")
	os.symlink("/etc/passwd", src / "sub" / "link")
	target = tmp_path / "out" / "runtime"
	run("copy-include", src, target)
	assert tree(target) == ["sub/a.txt"]


def test_copy_include_rejects_symlink_source(tmp_path):
	real = tmp_path / "real.txt"
	real.write_text("x\n")
	os.symlink(real, tmp_path / "alias.txt")
	assert run("copy-include", tmp_path / "alias.txt", tmp_path / "out", check=False).returncode == 1


# --- hidden paths (CODEX_ISOLATED_HIDE; the Claude engine's hide_claude_md) ------------


def test_hidden_claude_md_is_never_copied_shown_or_written_back(repo, tmp_path):
	(repo / "CLAUDE.md").write_text("repository instructions\n")
	git(repo, "add", "CLAUDE.md")
	git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "claude md")
	readonly = tmp_path / "ro"
	run("snapshot-readonly", repo, readonly, hide="CLAUDE.md")
	run("seed-git", repo, readonly, hide="CLAUDE.md")
	assert not (readonly / "CLAUDE.md").exists() and (readonly / "src" / "app.py").exists()
	assert subprocess.run(["git", "show", "HEAD:CLAUDE.md"], cwd=readonly, capture_output=True).returncode != 0
	dest = tmp_path / "work"
	manifest = tmp_path / "manifest.json"
	run("snapshot-workspace", repo, dest, manifest, hide="CLAUDE.md")
	assert not (dest / "CLAUDE.md").exists()
	(dest / "CLAUDE.md").write_text("agent instructions\n")
	(dest / "src" / "app.py").write_text("print('edited')\n")
	proc = run("transfer", repo, dest, manifest, hide="CLAUDE.md")
	assert "transfer ignored=CLAUDE.md reason=hidden" in proc.stderr
	assert (repo / "CLAUDE.md").read_text() == "repository instructions\n"
	assert (repo / "src" / "app.py").read_text() == "print('edited')\n"


def test_hidden_path_does_not_delete_the_host_file(repo, tmp_path):
	(repo / "CLAUDE.md").write_text("repository instructions\n")
	dest = tmp_path / "work"
	manifest = tmp_path / "manifest.json"
	run("snapshot-workspace", repo, dest, manifest, hide="CLAUDE.md")
	run("transfer", repo, dest, manifest, hide="CLAUDE.md")
	assert (repo / "CLAUDE.md").read_text() == "repository instructions\n"


@pytest.mark.parametrize("value", ["../CLAUDE.md", "docs/CLAUDE.md", ".", "a b"])
def test_invalid_hidden_path_is_refused(repo, tmp_path, value):
	proc = run("snapshot-readonly", repo, tmp_path / "ro", check=False, hide=value)
	assert proc.returncode == 1
	assert "invalid hidden path" in proc.stderr
