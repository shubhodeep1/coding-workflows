#!/usr/bin/env python3
"""Copy review source into a disposable workspace and validate editor output.

The host checkout and this manifest never enter the container. No paths from
the container are trusted, including directory entries and file modes.
"""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile


MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_FILES = 5000
EXCLUDED = {".git", ".ai", ".codex", ".opencode", ".serena", ".venv", ".review-venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".tox", ".nox", "dist", "build", "coverage", ".next", ".turbo", ".codex-workflow-src", ".codex-workflow-src-main", "secrets", "credentials"}
ROOT_FILES = {"README.md", "agents.md", "AGENTS.md", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pyproject.toml", "requirements.txt", "setup.cfg", "pytest.ini", "tox.ini", "go.mod", "Cargo.toml"}
SUFFIXES = {".py", ".sh", ".js", ".jsx", ".cjs", ".mjs", ".ts", ".tsx", ".cts", ".mts", ".go", ".rs", ".java", ".json", ".md", ".yml", ".yaml", ".toml", ".txt", ".css", ".html", ".sql", ".lock", ".cfg", ".ini"}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
DISPLAY_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._/+@-")
DISPLAY_MAX = 200


class WorkspaceRejection(ValueError):
	"""A path rejection that also names the rule it broke (#6413).

	Diagnostics only: the decision to reject is made by the existing checks
	before this is raised, and str(exc) stays the original message so every
	`except ValueError` caller keeps working.
	"""

	def __init__(self, message, reason, name, is_symlink=False, directory=False):
		super().__init__(message)
		self.reason = reason
		probe = name + "/placeholder.py" if directory and not is_symlink else name
		self.rule, redact_index = rejection_rule(probe, is_symlink=is_symlink, directory=directory)
		self.display = display_path(name, redact_index)


def rejection_rule(name, is_symlink=False, directory=False):
	"""Name the rule a rejected path broke. Never changes the decision."""
	if is_symlink:
		return "symlink", None
	if not name or name.startswith("/") or "\\" in name or "\n" in name or "\r" in name or ".." in name.split("/"):
		return "invalid_name", None
	parts = name.split("/")
	for index, part in enumerate(parts):
		lowered = part.lower()
		if part in EXCLUDED:
			return "excluded_name", None
		if lowered in EXCLUDED or (lowered.endswith((".egg-info", ".dist-info")) and not part.endswith((".egg-info", ".dist-info"))):
			return "excluded_case_variant", None
		if lowered.startswith(".env"):
			return "env_name", index
		if "secret" in lowered or "credential" in lowered:
			return "secret_name", index
		if lowered.endswith(SECRET_SUFFIXES):
			return "secret_suffix", index
	if parts[0].startswith(".") and (len(parts) < 3 or tuple(parts[:2]) not in ((".github", "workflows"), (".github", "actions"))):
		return "dot_subdir", None
	return ("disallowed" if directory else "disallowed_suffix"), None


def display_path(name, redact_index=None):
	"""Make an untrusted path safe for logs: no workflow commands, no secrets."""
	def clean(text):
		return "".join(ch if ch in DISPLAY_SAFE else "?" for ch in text)
	if redact_index is None:
		shown = clean(name)
	else:
		parts = name.split("/")
		shown = "/".join("<redacted>" if index == redact_index else clean(part) for index, part in enumerate(parts))
	if len(shown) > DISPLAY_MAX:
		shown = shown[:DISPLAY_MAX - 3] + "..."
	return shown


def write_rejection_reason(line):
	"""Write the rejection line for the editor wrapper; optional, never fatal."""
	target = os.environ.get("REVIEW_SANDBOX_TRANSFER_REASON_FILE", "")
	if not target:
		return
	tmp = None
	try:
		directory = os.path.dirname(os.path.abspath(target))
		fd, tmp = tempfile.mkstemp(dir=directory, prefix=".review-transfer-reason-")
		with os.fdopen(fd, "w", encoding="utf-8") as out:
			out.write(line + "\n")
		os.replace(tmp, target)
		tmp = None
	except OSError:
		pass  # Diagnostics only: the exit status must not depend on this write.
	finally:
		if tmp is not None:
			try:
				os.unlink(tmp)
			except OSError:
				pass


def git_env(manifest):
	# The runner exports GIT_DIR/GIT_WORK_TREE and may set global hooks or
	# credential helpers. None may reach the synthetic repository.
	return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(manifest.parent),
		"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
		"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}


def allowed(name):
	parts = PurePosixPath(name).parts
	if not parts or name.startswith("/") or ".." in parts or "\\" in name or "\n" in name or "\r" in name:
		return False
	if any(part.lower() in EXCLUDED or part.lower().startswith(".env") or "secret" in part.lower() or "credential" in part.lower() or part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".egg-info", ".dist-info")) for part in parts):
		return False
	if parts[0].startswith(".") and (len(parts) < 3 or parts[:2] not in ((".github", "workflows"), (".github", "actions"))):
		return False
	return name in ROOT_FILES or PurePosixPath(name).suffix.lower() in SUFFIXES or parts[-1] == "Dockerfile"


def checked_path(root, name):
	path = root
	walked = []
	for part in PurePosixPath(name).parts:
		path = path / part
		walked.append(part)
		if path.is_symlink():
			raise WorkspaceRejection("symlink in workspace path", "symlink_path", "/".join(walked), is_symlink=True)
	return path


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


def enumerate_workspace(root):
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
			if child in EXCLUDED or child.endswith((".egg-info", ".dist-info")) or (rel == Path(".") and child.startswith(".") and child != ".github"):
				dirs.remove(child)
				continue
			child_is_symlink = (Path(directory) / child).is_symlink()
			if (name != ".github" and not allowed(name + "/placeholder.py")) or child_is_symlink:
				raise WorkspaceRejection("unsafe workspace directory", "unsafe_directory", name, is_symlink=child_is_symlink, directory=True)
		for child in files:
			entries += 1
			if entries > 10000:
				raise ValueError("workspace entry limit exceeded")
			name = (rel / child).as_posix()
			if not allowed(name):
				# Build products and cached dependencies are not editor output.
				if name in ROOT_FILES or rel == Path("."):
					raise WorkspaceRejection("unsafe workspace result path", "unsafe_result_path", name)
				continue
			data, mode = read_regular(checked_path(root, name))
			count += 1
			total += len(data)
			if count > MAX_FILES or total > MAX_TOTAL:
				raise ValueError("workspace size limit exceeded")
			yield name, data, mode


def snapshot(host, workspace, manifest):
	paths = set()
	env = git_env(manifest)
	for cmd in (["git", "ls-files", "-z"], ["git", "ls-files", "--others", "--exclude-standard", "-z"]):
		paths.update(p.decode("utf-8") for p in subprocess.check_output(cmd, cwd=host, env=env).split(b"\0") if p)
	baseline = {}
	total = 0
	for name in sorted(paths):
		if not allowed(name):
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
	manifest.write_text(json.dumps(baseline), encoding="utf-8")
	# A local, unauthenticated Git database supports editor diff commands.
	# It is synthetic, and is never copied back to the host.
	subprocess.run(["git", "init", "-q"], cwd=workspace, check=True, env=env)
	(workspace / ".git/info/exclude").write_text(".review-venv/\nnode_modules/\n.venv/\n__pycache__/\n*.egg-info/\n*.dist-info/\n.pytest_cache/\n", encoding="utf-8")
	subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "add", "--all"], cwd=workspace, check=True, env=env)
	subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "user.name=isolated", "-c", "user.email=isolated@invalid", "commit", "--allow-empty", "-qm", "snapshot"], cwd=workspace, check=True, env=env)


def transfer(host, workspace, manifest):
	baseline = json.loads(manifest.read_text(encoding="utf-8"))
	results = dict((name, (data, mode)) for name, data, mode in enumerate_workspace(workspace))
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
		if not allowed(name):
			raise WorkspaceRejection("unsafe result path", "unsafe_result_path", name)
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
	baseline = json.loads(manifest.read_text(encoding="utf-8"))
	results = dict((name, (data, mode)) for name, data, mode in enumerate_workspace(workspace))
	for name, old in baseline.items():
		host_file = checked_path(host, name)
		if not host_file.exists() or fingerprint(host_file) != old:
			raise ValueError("host baseline changed before editor")
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
	if len(sys.argv) != 5 or sys.argv[1] not in ("snapshot", "refresh", "transfer"):
		raise SystemExit(2)
	host, workspace, manifest = map(Path, sys.argv[2:])
	try:
		if sys.argv[1] == "snapshot":
			snapshot(host, workspace, manifest)
		elif sys.argv[1] == "refresh":
			refresh(host, workspace, manifest)
		else:
			transfer(host, workspace, manifest)
	except WorkspaceRejection as exc:
		# The subclass keeps the "(ValueError)" text existing log searches use.
		reject_exit(f"(ValueError) reason={exc.reason} rule={exc.rule} path={exc.display}")
	except (OSError, UnicodeError, subprocess.CalledProcessError) as exc:
		# No detail: OSError text can carry host paths.
		reject_exit(f"({type(exc).__name__}) reason=other rule={type(exc).__name__}")
	except ValueError as exc:
		reject_exit(f"({type(exc).__name__}) reason=other rule=ValueError detail={display_path(str(exc))}")


def reject_exit(suffix):
	head, _, line = suffix.partition(" ")
	print(f"::error::Review isolation snapshot or transfer rejected {head} {line}", file=sys.stderr)
	write_rejection_reason(line)
	raise SystemExit(1) from None


if __name__ == "__main__":
	main()
