#!/usr/bin/env python3
"""Copy review source into a disposable workspace and validate editor output.

The host checkout and this manifest never enter the container. No paths from
the container are trusted, including directory entries and file modes.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_FILES = 5000
EXCLUDED = {".git", ".ai", ".codex", ".opencode", ".serena", ".venv", ".review-venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".tox", ".nox", "dist", "build", "coverage", ".next", ".turbo", ".codex-workflow-src", ".codex-workflow-src-main", "secrets", "credentials"}
ROOT_FILES = {"README.md", "agents.md", "AGENTS.md", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pyproject.toml", "requirements.txt", "setup.cfg", "pytest.ini", "tox.ini", "go.mod", "Cargo.toml"}
SUFFIXES = {".py", ".sh", ".js", ".jsx", ".cjs", ".mjs", ".ts", ".tsx", ".cts", ".mts", ".go", ".rs", ".java", ".json", ".md", ".yml", ".yaml", ".toml", ".txt", ".css", ".html", ".sql", ".lock", ".cfg", ".ini"}
COMMAND_TWIN_DIR = "workflow-templates/.claude/commands"


def git_env(manifest):
	# The runner exports GIT_DIR/GIT_WORK_TREE and may set global hooks or
	# credential helpers. None may reach the synthetic repository.
	return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(manifest.parent),
		"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
		"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}


def allowed(name, host=None, commands=None):
	parts = PurePosixPath(name).parts
	if not parts or name.startswith("/") or ".." in parts or "\\" in name or "\n" in name or "\r" in name:
		return False
	if any(part.lower() in EXCLUDED or part.lower().startswith(".env") or "secret" in part.lower() or "credential" in part.lower() or part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".egg-info", ".dist-info")) for part in parts):
		return False
	if name in (".github/ai/claude_engine.json", ".claude/hooks/gh_api_write_guard.py", ".claude/hooks/pr_merge_status_guard.py", "scripts/claude_settings.json.tmpl"):
		return True
	# Command admission must use the inventory frozen by snapshot.
	if len(parts) == 3 and parts[:2] == (".claude", "commands") and parts[2].endswith(".md") and not parts[2].startswith("."):
		return commands is not None and name in commands
	if parts[0].startswith(".") and (len(parts) < 3 or parts[:2] not in ((".github", "workflows"), (".github", "actions"))):
		return False
	return name in ROOT_FILES or PurePosixPath(name).suffix.lower() in SUFFIXES or parts[-1] == "Dockerfile"


def checked_path(root, name):
	path = root
	for part in PurePosixPath(name).parts:
		path = path / part
		if path.is_symlink():
			raise ValueError("symlink in workspace path")
	return path


def admitted_commands_path(manifest):
	return manifest.with_name(manifest.name + ".admitted_commands.json")


def synthetic_git_config_path(manifest):
	return manifest.with_name(manifest.name + ".git_config.sha256")


def template_command_inventory(host):
	try:
		directory = checked_path(host, COMMAND_TWIN_DIR)
	except ValueError:
		return frozenset()
	if not directory.is_dir():
		return frozenset()
	commands = set()
	for entry in directory.iterdir():
		name = ".claude/commands/" + entry.name
		if entry.is_file() and not entry.is_symlink() and allowed(name, commands={name}):
			commands.add(name)
	return frozenset(commands)


def load_admitted_commands(manifest):
	try:
		inventory = json.loads(admitted_commands_path(manifest).read_text(encoding="utf-8"))
	except (OSError, ValueError, UnicodeError):
		raise ValueError("admitted command inventory missing") from None
	if not isinstance(inventory, list) or any(not isinstance(name, str) or not re.fullmatch(r"\.claude/commands/[^/.][^/]*\.md", name) or not allowed(name, commands={name}) for name in inventory):
		raise ValueError("admitted command inventory missing")
	return frozenset(inventory)


def read_regular(path):
	info = path.lstat()
	if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE or info.st_mode & (stat.S_ISUID | stat.S_ISGID):
		raise ValueError("unsafe file type or size")
	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
	with os.fdopen(fd, "rb") as handle:
		opened = os.fstat(handle.fileno())
		if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
			raise ValueError("file changed during read")
		data = handle.read(MAX_FILE + 1)
	if len(data) != info.st_size:
		raise ValueError("file changed during read")
	return data, 0o755 if info.st_mode & 0o111 else 0o644


def fingerprint(path):
	data, mode = read_regular(path)
	return [hashlib.sha256(data).hexdigest(), mode]


def enumerate_workspace(root, host=None, commands=None, strict=False):
	count = 0
	total = 0
	entries = 0
	for directory, dirs, files in os.walk(root, followlinks=False):
		rel = Path(directory).relative_to(root)
		for child in dirs[:]:
			entries += 1
			if entries > 10000:
				raise ValueError("workspace entry limit exceeded")
			name = (rel / child).as_posix()
			if child in EXCLUDED or child.endswith((".egg-info", ".dist-info")) or (rel == Path(".") and child.startswith(".") and child not in (".github", ".claude")):
				dirs.remove(child)
				continue
			if (name not in (".github", ".github/ai", ".claude", ".claude/hooks", ".claude/commands") and not allowed(name + "/placeholder.py")) or (Path(directory) / child).is_symlink():
				raise ValueError("unsafe workspace directory")
		for child in files:
			entries += 1
			if entries > 10000:
				raise ValueError("workspace entry limit exceeded")
			name = (rel / child).as_posix()
			if not allowed(name, commands=commands):
				if strict and name.startswith((".github/ai/", ".claude/")):
					raise ValueError("out of heal scope")
				# Build products and cached dependencies are not editor output.
				if name in ROOT_FILES or rel == Path("."):
					raise ValueError("unsafe workspace result path")
				continue
			data, mode = read_regular(checked_path(root, name))
			count += 1
			total += len(data)
			if count > MAX_FILES or total > MAX_TOTAL:
				raise ValueError("workspace size limit exceeded")
			yield name, data, mode


def snapshot(host, workspace, manifest, host_git_dir=None):
	paths = set()
	env = git_env(manifest)
	# A per-PR workspace has no .git of its own: list it through the checkout's
	# Git database, exactly as the commit step sees it (issues #4580, #6055).
	git = ["git"]
	if host_git_dir is not None:
		if not host_git_dir.is_dir():
			raise ValueError("host git dir missing")
		git += ["--git-dir", str(host_git_dir), "--work-tree", str(host)]
	for cmd in (git + ["ls-files", "-z"], git + ["ls-files", "--others", "--exclude-standard", "-z"]):
		paths.update(p.decode("utf-8") for p in subprocess.check_output(cmd, cwd=host, env=env).split(b"\0") if p)
	# The PR worktree can add twins before snapshot; only names present in the
	# verified workflow-support checkout may authorize a command for this run.
	trusted_checkout = os.environ.get("GITHUB_WORKSPACE")
	commands = (template_command_inventory(Path(trusted_checkout) / ".codex-workflow-src") &
		template_command_inventory(host)) if trusted_checkout else frozenset()
	baseline = {}
	total = 0
	for name in sorted(paths):
		if not allowed(name, commands=commands):
			continue
		if (host / name).is_symlink():
			continue  # Existing tracked symlinks are not in the editor snapshot.
		path = checked_path(host, name)
		if not path.exists():
			continue
		data, mode = read_regular(path)
		total += len(data)
		if len(baseline) >= MAX_FILES or total > MAX_TOTAL:
			raise ValueError("snapshot size limit exceeded")
		target = checked_path(workspace, name)
		target.parent.mkdir(parents=True, exist_ok=True)
		with target.open("xb") as out:
			out.write(data)
		os.chmod(target, mode)
		baseline[name] = [hashlib.sha256(data).hexdigest(), mode]
	admitted_commands_path(manifest).write_text(json.dumps(sorted(commands)), encoding="utf-8")
	manifest.write_text(json.dumps(baseline), encoding="utf-8")
	# A local, unauthenticated Git database supports editor diff commands.
	# It is synthetic, and is never copied back to the host.
	subprocess.run(["git", "init", "-q"], cwd=workspace, check=True, env=env)
	(workspace / ".git/info/exclude").write_text(".review-venv/\nnode_modules/\n.venv/\n__pycache__/\n*.egg-info/\n*.dist-info/\n.pytest_cache/\n", encoding="utf-8")
	subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "add", "--all"], cwd=workspace, check=True, env=env)
	subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "user.name=isolated", "-c", "user.email=isolated@invalid", "commit", "--allow-empty", "-qm", "snapshot"], cwd=workspace, check=True, env=env)
	synthetic_git_config_path(manifest).write_text(hashlib.sha256((workspace / ".git/config").read_bytes()).hexdigest(), encoding="ascii")


def transfer(host, workspace, manifest, scope=None):
	commands = load_admitted_commands(manifest)
	baseline = json.loads(manifest.read_text(encoding="utf-8"))
	if scope is not None and hashlib.sha256(read_regular(workspace / ".git/config")[0]).hexdigest() != synthetic_git_config_path(manifest).read_text(encoding="ascii"):
		raise ValueError("out of heal scope")
	results = {name: (data, mode) for name, data, mode in enumerate_workspace(workspace, None, commands, strict=scope is not None)}
	changes = []
	# Even an untouched result must not conceal a host-side update made since
	# the snapshot (including a write by another workflow process).
	for name, old in baseline.items():
		host_file = checked_path(host, name)
		if not host_file.exists() or fingerprint(host_file) != old:
			raise ValueError("host baseline changed")
	for name in sorted(set(baseline) | set(results)):
		old = baseline.get(name)
		new = results.get(name)
		if new is not None and old == [hashlib.sha256(new[0]).hexdigest(), new[1]]:
			continue
		if not allowed(name, None, commands):
			raise ValueError("unsafe result path")
		if scope is not None:
			from files_touched_scope_guard import entry_matches

			if name.startswith((".github/ai/", ".claude/")) or name in (".gitattributes", ".gitmodules") or not any(entry_matches(entry, name) for entry in scope):
				raise ValueError("out of heal scope")
			if new is not None and old is not None and new[1] != old[1]:
				raise ValueError("out of heal scope")
		host_file = checked_path(host, name)
		if old is None and (host_file.exists() or host_file.is_symlink()):
			raise ValueError("new result conflicts with host path")
		changes.append((name, host_file, new))
	# All preconditions are checked before the first host write.
	for name, host_file, new in changes:
		if new is None:
			host_file.unlink()
			baseline.pop(name, None)
			continue
		host_file.parent.mkdir(parents=True, exist_ok=True)
		fd, tmp = tempfile.mkstemp(dir=host_file.parent, prefix=".review-isolated-")
		try:
			with os.fdopen(fd, "wb") as out:
				out.write(new[0])
			os.chmod(tmp, new[1])
			os.replace(tmp, host_file)
			baseline[name] = [hashlib.sha256(new[0]).hexdigest(), new[1]]
		finally:
			if os.path.exists(tmp):
				os.unlink(tmp)
	manifest.write_text(json.dumps(baseline), encoding="utf-8")


def refresh(host, workspace, manifest):
	"""Discard PR build-backend source writes before giving the writer access."""
	commands = load_admitted_commands(manifest)
	baseline = json.loads(manifest.read_text(encoding="utf-8"))
	results = {name: (data, mode) for name, data, mode in enumerate_workspace(workspace, None, commands)}
	for name, old in baseline.items():
		host_file = checked_path(host, name)
		if not host_file.exists() or fingerprint(host_file) != old:
			raise ValueError("host baseline changed before editor")
	# Excluded command writes are not in results, but must not survive a retry.
	command_dir = checked_path(workspace, ".claude/commands")
	if command_dir.is_dir():
		for target in command_dir.iterdir():
			if (target.is_file() or target.is_symlink()) and not target.name.startswith(".") and ".claude/commands/" + target.name not in commands:
				target.unlink()
	for name in sorted(set(baseline) | set(results)):
		target = checked_path(workspace, name)
		if name not in baseline:
			target.unlink()
		elif name not in results or fingerprint(target) != baseline[name]:
			data, mode = read_regular(checked_path(host, name))
			target.parent.mkdir(parents=True, exist_ok=True)
			if target.exists():
				target.unlink()
			with target.open("xb") as out:
				out.write(data)
			os.chmod(target, mode)


def main():
	# snapshot alone takes an optional fifth argument: the host Git dir.
	if sys.argv[1:2] not in (["snapshot"], ["refresh"], ["transfer"]) or not (len(sys.argv) == 5 or (len(sys.argv) == 6 and sys.argv[1] == "snapshot") or (len(sys.argv) == 7 and sys.argv[1] == "transfer" and sys.argv[5] == "--scope-file")):
		raise SystemExit(2)
	host, workspace, manifest = map(Path, sys.argv[2:5])
	try:
		if sys.argv[1] == "snapshot":
			snapshot(host, workspace, manifest, Path(sys.argv[5]) if len(sys.argv) == 6 else None)
		elif sys.argv[1] == "refresh":
			refresh(host, workspace, manifest)
		else:
			scope = None
			if len(sys.argv) == 7:
				scope = Path(sys.argv[6]).read_text(encoding="utf-8").splitlines()
				if not scope or any(not entry or entry not in ("tests/**", "changelog.d/*.md") and not re.fullmatch(r"[A-Za-z0-9_./-]+", entry) for entry in scope):
					raise ValueError("out of heal scope")
			transfer(host, workspace, manifest, scope)
	except (OSError, ValueError, UnicodeError, subprocess.CalledProcessError) as exc:
		# Only fixed, path-free transfer reasons may cross into workflow logs.
		reason_code = {
			"admitted command inventory missing": "admitted_inventory_missing",
			"symlink in workspace path": "symlink_path",
			"unsafe file type or size": "unsafe_file",
			"file changed during read": "file_changed",
			"workspace entry limit exceeded": "entry_limit",
			"unsafe workspace directory": "unsafe_directory",
			"unsafe workspace result path": "unsafe_result_path",
			"workspace size limit exceeded": "workspace_size_limit",
			"host baseline changed": "host_baseline_changed",
			"new result conflicts with host path": "host_path_conflict",
			"unsafe result path": "unsafe_result_path",
			"out of heal scope": "out_of_heal_scope",
		}.get(str(exc), "unknown") if sys.argv[1] == "transfer" and isinstance(exc, ValueError) else "unknown"
		print(f"::error::Review isolation snapshot or transfer rejected ({type(exc).__name__}) reason={reason_code}", file=sys.stderr)
		raise SystemExit(1) from None


if __name__ == "__main__":
	main()
