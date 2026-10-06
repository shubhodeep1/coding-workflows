#!/usr/bin/env python3
"""Give a read-profile Claude container filtered source and credential-free git metadata."""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from claude_engine import _SNAPSHOT_BAD_PARTS, _snapshot_bad_suffix
from review_untrusted_workspace import (
	MAX_FILES,
	MAX_TOTAL,
	allowed,
	checked_path,
	git_env,
	read_regular,
)


def _git(args, cwd, env):
	return subprocess.check_output(["git", *args], cwd=cwd, env=env, stderr=subprocess.DEVNULL)


def _source_files(root, env):
	"""Use git's ignore rules when possible; otherwise walk bounded regular files."""
	try:
		git_dir = Path(os.fsdecode(_git(["rev-parse", "--absolute-git-dir"], root, env)).strip()).resolve()
		paths = set()
		for args in (["ls-files", "-z"], ["ls-files", "--others", "--exclude-standard", "-z"]):
			paths.update(os.fsdecode(p) for p in _git(args, root, env).split(b"\0") if p)
		return sorted(paths), git_dir
	except subprocess.CalledProcessError:
		paths = []
		entries = 0
		for directory, dirs, files in os.walk(root, followlinks=False):
			rel = Path(directory).relative_to(root)
			entries += len(dirs) + len(files)
			if entries > 10000:
				raise ValueError("entry limit")
			dirs[:] = [child for child in dirs if allowed((rel / child / "placeholder.py").as_posix()) and not (Path(directory) / child).is_symlink()]
			paths.extend((rel / child).as_posix() for child in files)
		return sorted(paths), None


def _synthetic(source, env):
	_git(["init", "-q"], source, env)
	_git(["-c", "core.hooksPath=/dev/null", "add", "--all"], source, env)
	_git(["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
		"-c", "user.name=isolated", "-c", "user.email=isolated@invalid",
		"commit", "--allow-empty", "-qm", "snapshot"], source, env)


def _history(root, source, host_git, gitdir, copied, env):
	"""Generate metadata, not a copy of the host config or refs directory."""
	objects = host_git / "objects"
	if (objects / "info" / "alternates").exists() or (objects / "info" / "alternates").is_symlink() or objects.is_symlink() or not objects.is_dir():
		return None
	# An alternate object mount exposes all reachable and dangling objects,
	# not just the paths copied into the working-tree snapshot.
	if (host_git / "worktrees").exists() and any((host_git / "worktrees").iterdir()):
		return None
	with subprocess.Popen(["git", "log", "--all", "HEAD", "--format=", "--name-only", "-z", "--no-renames"],
			cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as history_process:
		assert history_process.stdout is not None
		history_pending = b""
		while history_chunk := history_process.stdout.read(65536):
			history_paths = (history_pending + history_chunk).split(b"\0")
			history_pending = history_paths.pop()
			if len(history_pending) > 1048576 or any(os.fsdecode(path) not in copied for path in history_paths):
				history_process.terminate()
				return None
		if history_pending or history_process.wait() != 0:
			return None
	with subprocess.Popen(["git", "fsck", "--unreachable", "--no-reflogs"],
			cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as fsck_probe:
		assert fsck_probe.stdout is not None
		if fsck_probe.stdout.read(1):
			fsck_probe.terminate()
			return None
		if fsck_probe.wait() != 0:
			return None
	sha = _git(["rev-parse", "HEAD"], root, env).decode().strip()
	if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
		return None
	refs = _git(["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/remotes", "refs/tags"], root, env).decode()
	for line in refs.splitlines():
		if not re.fullmatch(r"[0-9a-f]{40,64} refs/(heads|remotes|tags)/[A-Za-z0-9_./-]+", line) or ".." in line:
			return None
	clean_env = {key: value for key, value in env.items() if key not in ("GIT_DIR", "GIT_WORK_TREE")}
	_git(["init", "-q", "--separate-git-dir", str(gitdir)], source, clean_env)
	(gitdir / "HEAD").write_text(sha + "\n", encoding="ascii")
	(gitdir / "packed-refs").write_text("# pack-refs with: peeled fully-peeled \n" + refs, encoding="ascii")
	(gitdir / "objects/info/alternates").write_text("/gitobjects\n", encoding="ascii")
	shallow = host_git / "shallow"
	if shallow.is_file() and not shallow.is_symlink():
		shallow_bytes, _ = read_regular(shallow)
		if not all(re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", row) for row in shallow_bytes.splitlines()):
			return None
		(gitdir / "shallow").write_bytes(shallow_bytes)
	# The in-container object location is absent here. Use the host objects only
	# for preparing the index; the alternates file remains container-relative.
	index_env = {**clean_env, "GIT_DIR": str(gitdir), "GIT_WORK_TREE": str(source),
		"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects)}
	_git(["read-tree", "HEAD"], source, index_env)
	tracked = _git(["ls-files", "-z"], root, env).split(b"\0")
	for raw_path in tracked:
		if raw_path and os.fsdecode(raw_path) not in copied:
			_git(["update-index", "--skip-worktree", "--", os.fsdecode(raw_path)], source, index_env)
	return objects


def snapshot(root, source, gitdir, exclude_claude_md=False):
	root = root.resolve(strict=True)
	source.mkdir(mode=0o755, parents=True)
	# review_untrusted_workspace.git_env intentionally strips runner git env.
	env = git_env(source / "manifest")
	if os.environ.get("GIT_WORK_TREE") and Path(os.environ["GIT_WORK_TREE"]).resolve() == root and os.environ.get("GIT_DIR"):
		env.update(GIT_WORK_TREE=str(root), GIT_DIR=os.environ["GIT_DIR"])
	paths, host_git = _source_files(root, env)
	copied = set()
	total = 0
	for name in paths:
		if (not allowed(name) or (exclude_claude_md and name == "CLAUDE.md") or
			any(part.lower() in _SNAPSHOT_BAD_PARTS or part.lower().startswith(".env") or
				part.lower().split(".", 1)[0] in _SNAPSHOT_BAD_PARTS or _snapshot_bad_suffix(part)
				for part in Path(name).parts)):
			continue
		if (root / name).is_symlink():
			continue
		path = checked_path(root, name)
		if not path.exists():
			continue
		data, mode = read_regular(path)
		total += len(data)
		if len(copied) >= MAX_FILES or total > MAX_TOTAL:
			raise ValueError("snapshot size limit")
		target = checked_path(source, name)
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_bytes(data)
		target.chmod(mode)
		copied.add(name)
	objects = None
	if host_git is not None:
		try:
			objects = _history(root, source, host_git, gitdir, copied, env)
		except (OSError, ValueError, subprocess.CalledProcessError, UnicodeError):
			pass
	if objects is None:
		# A failed history construction must not retain partial refs/index/alternates.
		if gitdir.exists():
			shutil.rmtree(gitdir)
		if (source / ".git").exists():
			(source / ".git").unlink()
		_synthetic(source, {key: value for key, value in env.items() if key not in ("GIT_DIR", "GIT_WORK_TREE")})
	else:
		# Replace the host-pointing gitfile with a mount point for the new gitdir.
		(source / ".git").unlink()
		(source / ".git").mkdir()
		print(objects)


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("workdir", type=Path)
	parser.add_argument("source", type=Path)
	parser.add_argument("gitdir", type=Path)
	parser.add_argument("--exclude-root-claude-md", action="store_true")
	args = parser.parse_args()
	try:
		snapshot(args.workdir, args.source, args.gitdir, args.exclude_root_claude_md)
	except (OSError, ValueError, UnicodeError, subprocess.CalledProcessError) as exc:
		print(f"::error::Claude read snapshot rejected ({type(exc).__name__})", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())
