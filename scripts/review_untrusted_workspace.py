#!/usr/bin/env python3
"""Copy review source into a disposable workspace and validate editor output.

The host checkout and this manifest never enter the container. No paths from
the container are trusted, including directory entries and file modes.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

MAX_FILE = 2 * 1024 * 1024
MAX_README_PROMPT_BYTES = 200000
MAX_TOTAL = 64 * 1024 * 1024
MAX_FILES = 5000
EXCLUDED = {".git", ".ai", ".codex", ".opencode", ".serena", ".venv", ".review-venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".tox", ".nox", "dist", "build", "coverage", ".next", ".turbo", ".codex-workflow-src", ".codex-workflow-src-main", "secrets", "credentials"}
ROOT_FILES = {"README.md", "agents.md", "AGENTS.md", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pyproject.toml", "requirements.txt", "setup.cfg", "pytest.ini", "tox.ini", "go.mod", "Cargo.toml"}
SUFFIXES = {".py", ".sh", ".js", ".jsx", ".cjs", ".mjs", ".ts", ".tsx", ".cts", ".mts", ".go", ".rs", ".java", ".json", ".md", ".yml", ".yaml", ".toml", ".txt", ".css", ".html", ".sql", ".lock", ".cfg", ".ini"}
KEY_MATERIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")

# Rejections carry fixed tokens only (issue #6424). Paths come from the
# untrusted container and are never printed: a name could inject a workflow
# command or look like a secret. main() re-checks every token against these
# allowlists before it reaches the ::error:: line.
_REJECTION_REASONS = frozenset({
	"unsafe_directory", "entry_limit", "size_limit", "unsafe_result_path",
	"symlink_in_path", "unsafe_file", "file_changed", "host_baseline_changed",
	"result_conflicts_host",
})
_DIRECTORY_CATEGORIES = frozenset({
	"symlink", "invalid_name", "dot_github_subtree", "env_like", "sensitive_name",
	"key_material_suffix", "excluded_name_variant", "other",
})
_DIRECTORY_DEPTHS = frozenset({"1", "2", "3+"})


def _rejection(message, reason, category=None, depth=None):
	# A plain ValueError keeps "(ValueError)" in the existing error line.
	exc = ValueError(message)
	exc.review_reason = reason
	exc.review_category = category
	exc.review_depth = depth
	return exc


def _directory_category(name, is_symlink):
	"""Name which rule rejected a workspace directory, without the path."""
	if is_symlink:
		return "symlink"
	parts = PurePosixPath(name).parts
	if not parts or name.startswith("/") or ".." in parts or "\\" in name or "\n" in name or "\r" in name:
		return "invalid_name"
	lowered = [part.lower() for part in parts]
	if parts[0] == ".github":
		return "dot_github_subtree"
	if any(part.startswith(".env") for part in lowered):
		return "env_like"
	if any("secret" in part or "credential" in part for part in lowered):
		return "sensitive_name"
	if any(part.endswith(KEY_MATERIAL_SUFFIXES) for part in lowered):
		return "key_material_suffix"
	if any(part in EXCLUDED for part in lowered):
		return "excluded_name_variant"
	return "other"


def _directory_depth(name):
	count = len(PurePosixPath(name).parts)
	return str(count) if count < 3 else "3+"


def _rejection_line(exc):
	line = f"::error::Review isolation snapshot or transfer rejected ({type(exc).__name__})"
	reason = getattr(exc, "review_reason", None)
	if not isinstance(reason, str) or reason not in _REJECTION_REASONS:
		return line
	line += f" reason={reason}"
	category = getattr(exc, "review_category", None)
	if isinstance(category, str) and category in _DIRECTORY_CATEGORIES:
		line += f" category={category}"
	depth = getattr(exc, "review_depth", None)
	if isinstance(depth, str) and depth in _DIRECTORY_DEPTHS:
		line += f" depth={depth}"
	return line
COMMAND_TWIN_DIR = "workflow-templates/.claude/commands"
# Live copies the resolver may sync from their workflow-templates twin
# (issue #6595). The live path stays excluded from allowed(), snapshot and
# transfer: only the runner writes it, byte-identical to the resolved template.
PAIRED_LIVE_COPIES = {
	".claude/hooks/pr_merge_status_guard.py": "workflow-templates/.claude/hooks/pr_merge_status_guard.py",
}
_UNMERGED_MODE_RE = re.compile(r"[0-7]{6}")
_UNMERGED_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class UnsafeWorkspaceDirectory(ValueError):
	def __init__(self, rejected_dir):
		super().__init__("unsafe workspace directory")
		self.rejected_dir = rejected_dir


def _log_safe_dir(name):
	if not re.fullmatch(r"[A-Za-z0-9._-][A-Za-z0-9._/-]{0,63}", name):
		return "redacted"
	parts = name.split("/")
	if any(not part or part in (".", "..") or "secret" in part.lower() or "credential" in part.lower() or part.lower().startswith(".env") for part in parts):
		return "redacted"
	return name


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
	if name == "tests/test_audit_plans_command.py":
		return False  # Its operator-facing command input is intentionally excluded.
	if any(part.lower() in EXCLUDED or part.lower().startswith(".env") or "secret" in part.lower() or "credential" in part.lower() or part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".egg-info", ".dist-info")) for part in parts):
		return False
	# This host-executed safety hook is never review-editor output.
	if name == ".claude/hooks/pr_merge_status_guard.py":
		return False
	if name in (".github/ai/claude_engine.json", ".claude/hooks/gh_api_write_guard.py", "scripts/claude_settings.json.tmpl"):
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
			raise _rejection("symlink in workspace path", "symlink_in_path")
	return path


def admitted_commands_path(manifest):
	return manifest.with_name(manifest.name + ".admitted_commands.json")


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
		raise _rejection("unsafe file type or size", "unsafe_file")
	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
	with os.fdopen(fd, "rb") as handle:
		opened = os.fstat(handle.fileno())
		if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
			raise _rejection("file changed during read", "file_changed")
		data = handle.read(MAX_FILE + 1)
	if len(data) != info.st_size:
		raise _rejection("file changed during read", "file_changed")
	return data, 0o755 if info.st_mode & 0o111 else 0o644


def readme_trimmed(host: Path) -> bytes:
	# README is PR-controlled; only read the fixed name through the no-follow reader.
	try:
		os.lstat(host / "README.md")
	except FileNotFoundError:
		return b""
	data, _ = read_regular(checked_path(host, "README.md"))
	if not data:
		return b""
	lines = []
	# awk prints each input record with a newline, including an unterminated last line.
	for line in data.split(b"\n")[: -1 if data.endswith(b"\n") else None]:
		if line.startswith(b"### 2. Create wrapper workflows"):
			break
		lines.append(line + b"\n")
	if sum(len(line) + len(b"UNTRUSTED_DATA: ") for line in lines) > MAX_README_PROMPT_BYTES:
		raise ValueError("README exceeds prompt budget")
	return b"".join(lines)


def fingerprint(path):
	data, mode = read_regular(path)
	return [hashlib.sha256(data).hexdigest(), mode]


def enumerate_workspace(root, host=None, commands=None):
	count = 0
	total = 0
	entries = 0
	for directory, dirs, files in os.walk(root, followlinks=False):
		rel = Path(directory).relative_to(root)
		for child in dirs[:]:
			entries += 1
			if entries > 10000:
				raise _rejection("workspace entry limit exceeded", "entry_limit")
			name = (rel / child).as_posix()
			if child in EXCLUDED or child.endswith((".egg-info", ".dist-info")) or (rel == Path(".") and child.startswith(".") and child not in (".github", ".claude")):
				dirs.remove(child)
				continue
			child_is_symlink = (Path(directory) / child).is_symlink()
			if (name not in (".github", ".github/ai", ".claude", ".claude/hooks") and not (name == ".claude/commands" and commands) and not allowed(name + "/placeholder.py")) or child_is_symlink:
				raise _rejection("unsafe workspace directory", "unsafe_directory", _directory_category(name, child_is_symlink), _directory_depth(name))
		for child in files:
			entries += 1
			if entries > 10000:
				raise _rejection("workspace entry limit exceeded", "entry_limit")
			name = (rel / child).as_posix()
			if not allowed(name, commands=commands):
				# Build products and cached dependencies are not editor output.
				if name in ROOT_FILES or rel == Path("."):
					raise _rejection("unsafe workspace result path", "unsafe_result_path")
				continue
			data, mode = read_regular(checked_path(root, name))
			count += 1
			total += len(data)
			if count > MAX_FILES or total > MAX_TOTAL:
				raise _rejection("workspace size limit exceeded", "size_limit")
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
			raise _rejection("snapshot size limit exceeded", "size_limit")
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


def _check_destination_parents(host, name, deleted_names):
	parent = host
	for part in PurePosixPath(name).parts[:-1]:
		parent = parent / part
		try:
			info = parent.lstat()
		except FileNotFoundError:
			return
		except NotADirectoryError:
			raise _rejection("new result conflicts with host path", "result_conflicts_host") from None
		if stat.S_ISDIR(info.st_mode):
			continue
		if stat.S_ISREG(info.st_mode) and parent.relative_to(host).as_posix() in deleted_names:
			return
		raise _rejection("new result conflicts with host path", "result_conflicts_host")


def transfer(host, workspace, manifest):
	commands = load_admitted_commands(manifest)
	baseline = json.loads(manifest.read_text(encoding="utf-8"))
	results = {name: (data, mode) for name, data, mode in enumerate_workspace(workspace, None, commands)}
	changes = []
	# Even an untouched result must not conceal a host-side update made since
	# the snapshot (including a write by another workflow process).
	for name, old in baseline.items():
		host_file = checked_path(host, name)
		if not host_file.exists() or fingerprint(host_file) != old:
			raise _rejection("host baseline changed", "host_baseline_changed")
	for name in sorted(set(baseline) | set(results)):
		old = baseline.get(name)
		new = results.get(name)
		if new is not None and old == [hashlib.sha256(new[0]).hexdigest(), new[1]]:
			continue
		if not allowed(name, None, commands):
			raise _rejection("unsafe result path", "unsafe_result_path")
		host_file = checked_path(host, name)
		if old is None and (host_file.exists() or host_file.is_symlink()):
			raise _rejection("new result conflicts with host path", "result_conflicts_host")
		changes.append((name, host_file, new))
	deleted_names = {name for name, _, new in changes if new is None}
	for name, host_file, new in changes:
		if new is None:
			continue
		_check_destination_parents(host, name, deleted_names)
		try:
			info = host_file.lstat()
		except FileNotFoundError:
			pass
		except NotADirectoryError:
			# Only a baseline file scheduled for deletion may become a directory.
			if not any(name.startswith(deleted + "/") for deleted in deleted_names):
				raise _rejection("new result conflicts with host path", "result_conflicts_host") from None
		else:
			if not stat.S_ISREG(info.st_mode):
				raise _rejection("new result conflicts with host path", "result_conflicts_host")
	backups = {}
	for name, host_file, _ in changes:
		if name in baseline:
			backups[name] = read_regular(host_file)
			if [hashlib.sha256(backups[name][0]).hexdigest(), backups[name][1]] != baseline[name]:
				raise _rejection("host baseline changed", "host_baseline_changed")
	stage = Path(tempfile.mkdtemp(dir=host, prefix=".review-isolated-stage-"))
	journal = []
	try:
		# Stage every payload before touching a destination.
		for index, (_, _, new) in enumerate(changes):
			if new is not None:
				staged = stage / str(index)
				fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
				with os.fdopen(fd, "wb") as out:
					out.write(new[0])
				os.chmod(staged, new[1])
		updated = baseline.copy()
		try:
			for index, (name, host_file, new) in enumerate(changes):
				if new is None:
					host_file.unlink()
					journal.append(("deleted", host_file, *backups[name]))
					updated.pop(name, None)
					continue
				missing = []
				parent = host_file.parent
				while parent != host and not parent.exists():
					missing.append(parent)
					parent = parent.parent
				for directory in reversed(missing):
					directory.mkdir()
					journal.append(("mkdir", directory))
				# Do not follow a parent that changed since prevalidation.
				_check_destination_parents(host, name, set())
				if name in backups:
					os.replace(stage / str(index), host_file)
					journal.append(("replaced", host_file, *backups[name]))
				else:
					os.link(stage / str(index), host_file, follow_symlinks=False)
					journal.append(("created", host_file))
				updated[name] = [hashlib.sha256(new[0]).hexdigest(), new[1]]
			# Cleanup is part of the transaction: a cleanup failure must not
			# report failure after publishing host edits and the manifest.
			shutil.rmtree(stage)
			# Publish the manifest atomically only after the entire host apply succeeds.
			fd, manifest_tmp = tempfile.mkstemp(dir=manifest.parent, prefix=".review-isolated-")
			try:
				with os.fdopen(fd, "w", encoding="utf-8") as out:
					json.dump(updated, out)
				os.replace(manifest_tmp, manifest)
			finally:
				if os.path.exists(manifest_tmp):
					os.unlink(manifest_tmp)
		except Exception:
			try:
				for entry in reversed(journal):
					kind, path, *original = entry
					if kind == "mkdir":
						path.rmdir()
					elif kind == "created":
						path.unlink()
					else:
						fd, restore_tmp = tempfile.mkstemp(dir=path.parent, prefix=".review-isolated-")
						try:
							with os.fdopen(fd, "wb") as out:
								out.write(original[0])
							os.chmod(restore_tmp, original[1])
							os.replace(restore_tmp, path)
						finally:
							if os.path.exists(restore_tmp):
								os.unlink(restore_tmp)
			except Exception:  # noqa: BLE001 - any rollback failure invalidates atomicity
				raise ValueError("transfer rollback failed") from None
			raise
	finally:
		if stage.exists():
			# Preserve the original apply/rollback failure if staging cleanup
			# also fails; the sandbox marker still prevents a commit.
			pending_error = sys.exc_info()[0]
			try:
				shutil.rmtree(stage)
			except OSError:
				if pending_error is None:
					raise


def refresh(host, workspace, manifest):
	"""Discard PR build-backend source writes before giving the writer access."""
	commands = load_admitted_commands(manifest)
	baseline = json.loads(manifest.read_text(encoding="utf-8"))
	results = {name: (data, mode) for name, data, mode in enumerate_workspace(workspace, None, commands)}
	for name, old in baseline.items():
		host_file = checked_path(host, name)
		if not host_file.exists() or fingerprint(host_file) != old:
			raise _rejection("host baseline changed before editor", "host_baseline_changed")
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


def _log_safe_path(name):
	"""Return a rejected path only when it cannot inject log or workflow text."""
	if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-][A-Za-z0-9._/-]{0,127}", name):
		return "redacted"
	parts = name.split("/")
	if any(not part or part in (".", "..") or "secret" in part.lower() or "credential" in part.lower() or part.lower().startswith(".env") for part in parts):
		return "redacted"
	return name


def _path_rejection_reason(name):
	"""Name, as a fixed token, the allowed() rule that refused a path."""
	parts = PurePosixPath(name).parts
	if not parts or name.startswith("/") or ".." in parts or "\\" in name or "\n" in name or "\r" in name:
		return "unsafe_name"
	if name == "tests/test_audit_plans_command.py":
		return "operator_input"
	if any(part.lower() in EXCLUDED or part.lower().startswith(".env") or "secret" in part.lower() or "credential" in part.lower() or part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".egg-info", ".dist-info")) for part in parts):
		return "excluded_component"
	if name in PAIRED_LIVE_COPIES:
		return "live_safety_hook"
	if parts[0].startswith("."):
		return "dot_directory"
	return "unsupported_type"


def check_paths(host, paths_file):
	with paths_file.open(encoding="utf-8", newline="") as handle:
		path_lines = handle.read().split("\n")
	for name in path_lines:
		if not name:
			continue
		reason = "unsafe_file"
		try:
			if not allowed(name):
				reason = _path_rejection_reason(name)
				raise ValueError("unsupported path")
			checked_path(host, name)
		except (ValueError, OSError):
			# Fixed reason token plus a charset-limited path (or "redacted"):
			# the name is PR-controlled and must never reach logs verbatim.
			print(f"REVIEW_RESOLVER_PATH_REJECTED reason={reason} path={_log_safe_path(name)}", file=sys.stderr)
			print("unsupported path", file=sys.stderr)
			raise SystemExit(1) from None


def _read_conflicted_paths(paths_file):
	with paths_file.open(encoding="utf-8", newline="") as handle:
		names = [name for name in handle.read().split("\n") if name]
	return list(dict.fromkeys(names))


def _parse_unmerged_index(unmerged_file):
	"""Parse `git ls-files -u -z` output into {path: {stage: (mode, sha)}}."""
	data = unmerged_file.read_bytes()
	entries = {}
	if not data:
		return entries
	if not data.endswith(b"\0"):
		raise ValueError("malformed unmerged record")
	for record in data[:-1].split(b"\0"):
		meta, sep, raw_name = record.partition(b"\t")
		fields = meta.split(b" ")
		if not sep or not raw_name or len(fields) != 3:
			raise ValueError("malformed unmerged record")
		mode, sha, stage = (field.decode("ascii") for field in fields)
		if not _UNMERGED_MODE_RE.fullmatch(mode) or not _UNMERGED_SHA_RE.fullmatch(sha) or stage not in ("1", "2", "3"):
			raise ValueError("malformed unmerged record")
		stages = entries.setdefault(raw_name.decode("utf-8"), {})
		if stage in stages:
			raise ValueError("malformed unmerged record")
		stages[stage] = (mode, sha)
	return entries


def paired_live_copies(host, paths_file, unmerged_file, pairs_out, model_paths_out):
	"""Split conflicted paths into runner-synced live copies and model paths.

	A live copy is paired only when it and its template conflict with exactly
	the same base/ours/theirs index entries, so the resolved template is the
	resolved live file. Any other shape stays in the model paths, where
	check-paths still refuses the live hook (fail closed).
	"""
	conflicted = _read_conflicted_paths(paths_file)
	unmerged = _parse_unmerged_index(unmerged_file)
	conflicted_set = set(conflicted)
	pairs = []
	for live, template in sorted(PAIRED_LIVE_COPIES.items()):
		if live not in conflicted_set or template not in conflicted_set:
			continue
		live_stages = unmerged.get(live)
		template_stages = unmerged.get(template)
		if not live_stages or live_stages != template_stages or "2" not in live_stages or "3" not in live_stages:
			continue
		if not allowed(template):
			continue
		try:
			checked_path(host, live)
			checked_path(host, template)
		except (ValueError, OSError):
			continue
		pairs.append((live, template))
	paired_live = {live for live, _ in pairs}
	pairs_out.write_text("".join(f"{live}\t{template}\n" for live, template in pairs), encoding="utf-8")
	model_paths_out.write_text("".join(f"{name}\n" for name in conflicted if name not in paired_live), encoding="utf-8")


def _has_conflict_markers(data):
	return any(line.startswith((b"<<<<<<< ", b">>>>>>> ")) for line in data.split(b"\n"))


def mirror_live_copies(host, pairs_file):
	"""Copy each resolved template byte-for-byte over its live copy."""
	pairs = []
	for line in pairs_file.read_text(encoding="utf-8").split("\n"):
		if not line:
			continue
		live, sep, template = line.partition("\t")
		# Never trust the file for the mapping itself: only known pairs.
		if not sep or PAIRED_LIVE_COPIES.get(live) != template:
			raise ValueError("unknown paired live copy")
		data, mode = read_regular(checked_path(host, template))
		if _has_conflict_markers(data):
			raise ValueError("template still has conflict markers")
		live_path = checked_path(host, live)
		if not stat.S_ISREG(live_path.lstat().st_mode):
			raise ValueError("live copy is not a regular file")
		pairs.append((live, live_path, data, mode))
	for live, live_path, data, mode in pairs:
		fd, tmp_name = tempfile.mkstemp(dir=live_path.parent, prefix=".review-live-copy-")
		try:
			with os.fdopen(fd, "wb") as out:
				out.write(data)
			os.chmod(tmp_name, mode)
			# Do not follow a path that changed since validation.
			if not stat.S_ISREG(checked_path(host, live).lstat().st_mode):
				raise ValueError("live copy is not a regular file")
			os.replace(tmp_name, live_path)
		finally:
			if os.path.exists(tmp_name):
				os.unlink(tmp_name)


def main():
	if sys.argv[1:2] == ["readme-trimmed"]:
		if len(sys.argv) != 3:
			raise SystemExit(2)
		try:
			readme_data = readme_trimmed(Path(sys.argv[2]))
		except ValueError as exc:
			reason = {
				"symlink in workspace path": "symlink_path",
				"unsafe file type or size": "unsafe_file",
				"file changed during read": "file_changed",
				"README exceeds prompt budget": "prompt_size",
			}.get(str(exc), "unknown")
			print(f"REVIEW_STATIC_CONTEXT_README outcome=rejected reason={reason}", file=sys.stderr)
			raise SystemExit(3) from None
		except Exception:
			print("::error::Review static README read failed", file=sys.stderr)
			raise SystemExit(1) from None
		try:
			sys.stdout.buffer.write(readme_data)
		except Exception:
			print("::error::Review static README output failed", file=sys.stderr)
			raise SystemExit(1) from None
		return
	if sys.argv[1:2] == ["paired-live-copies"]:
		if len(sys.argv) != 7:
			raise SystemExit(2)
		try:
			paired_live_copies(*map(Path, sys.argv[2:7]))
		except Exception:  # noqa: BLE001 - any failure must fail closed without detail
			print("paired live copy check failed", file=sys.stderr)
			raise SystemExit(1) from None
		return
	if sys.argv[1:2] == ["mirror-live-copies"]:
		if len(sys.argv) != 4:
			raise SystemExit(2)
		try:
			mirror_live_copies(Path(sys.argv[2]), Path(sys.argv[3]))
		except Exception:  # noqa: BLE001 - never print file content or paths
			print("paired live copy mirror failed", file=sys.stderr)
			raise SystemExit(1) from None
		return
	# snapshot alone takes an optional fifth argument: the host Git dir.
	if sys.argv[1:2] == ["check-paths"] and len(sys.argv) == 4:
		try:
			check_paths(Path(sys.argv[2]), Path(sys.argv[3]))
		except (OSError, UnicodeError):
			print("unsupported path", file=sys.stderr)
			raise SystemExit(1) from None
		return
	if sys.argv[1:2] not in (["snapshot"], ["refresh"], ["transfer"]) or not (len(sys.argv) == 5 or (len(sys.argv) == 6 and sys.argv[1] == "snapshot")):
		raise SystemExit(2)
	host, workspace, manifest = map(Path, sys.argv[2:5])
	try:
		if sys.argv[1] == "snapshot":
			snapshot(host, workspace, manifest, Path(sys.argv[5]) if len(sys.argv) == 6 else None)
		elif sys.argv[1] == "refresh":
			refresh(host, workspace, manifest)
		else:
			transfer(host, workspace, manifest)
	except (OSError, ValueError, UnicodeError, subprocess.CalledProcessError) as exc:
		if getattr(exc, "review_reason", None) in _REJECTION_REASONS:
			print(_rejection_line(exc), file=sys.stderr)
		else:
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
				"transfer rollback failed": "transfer_rollback_failed",
				"unsafe result path": "unsafe_result_path",
			}.get(str(exc), "unknown") if sys.argv[1] == "transfer" and isinstance(exc, ValueError) else "unknown"
			print(f"::error::Review isolation snapshot or transfer rejected ({type(exc).__name__}) reason={reason_code}", file=sys.stderr)
		raise SystemExit(1) from None


if __name__ == "__main__":
	main()
