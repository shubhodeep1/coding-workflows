#!/usr/bin/env python3
"""Host-side file handling for scripts/codex_isolated_exec.sh.

The isolated Codex container never sees the host checkout, its .git directory
(which holds the GH_PAT remote URL and checkout extraheader), HOME, the runner
file-command files, or any credential. This module builds the disposable copy
the container works on and, in workspace mode, carries the agent's file
changes back to the host.

Subcommands:

  snapshot-readonly HOST DEST
      Copy every tracked regular file of HOST into DEST (CLAUDE.md answer
      Q11 A): all top-level directories, symlinks and secret-looking paths
      skipped, files over MAX_READONLY_FILE skipped and logged, totals capped
      at MAX_READONLY_FILES / MAX_READONLY_TOTAL (exceeding fails closed).
      Falls back to walking HOST when it is not a git work tree.

  export-oversized HOST SCOPE_FILE DEST MAX_FILE_BYTES MAX_TOTAL_BYTES [SCOPE_MODE]
      Export scoped files omitted by snapshot-readonly as bounded chunks,
      using its same credential and hidden-path filters. Filtered files are
      never exported; scoped filtered files are listed in the manifest's
      `filtered_scoped` (reason `credential_filter` or `hidden`) so the audit
      reports them as not inspected. The default explicit mode lists unscoped
      oversized files for coverage; all mode exports every eligible oversized
      tracked file.

  snapshot-workspace HOST DEST MANIFEST
      Copy HOST except credential-looking paths and `.git` into DEST, keeping
      allowed symlinks as symlinks (never dereferenced), and record the copy in
      MANIFEST. Paths listed in MANIFEST's `prep_roots` (dependency
      directories created by the credential-free dependency phase) are kept
      in DEST and never copied from or back to HOST.

  prep-finalize HOST DEST MANIFEST
      After the dependency phase: record every top-level path it created as
      a prep root and restore every snapshotted path from HOST, so build
      backends cannot change the files the agent starts from.

  stage-deps WORK STAGE
       Stage allowlisted Node manifests and filtered Python requirement lists
       from the credential-filtered WORK copy. Only STAGE (not WORK) is
       mounted in the network-isolated dependency container, alongside the
       allowlisted registry proxy. Besides the root manifests, up to
       MAX_NESTED_NODE_PROJECTS subdirectory Node projects (a package.json
       plus a lockfile, at most MAX_NESTED_NODE_DEPTH levels down, outside
       hidden directories and node_modules) are staged one per
       `.codex-deps/nested/<n>/` and listed in `.codex-deps/nested.json`
       (tele-funtoken-msg-scoring#4770: the Worker in
       cloudflare/funtoken-competition-worker/ got no dependencies).

  handoff-nested-deps STAGE WORK LIST
      After the dependency phase: move each `.codex-deps/nested/<n>/node_modules`
      the install produced into WORK at the project path LIST names (LIST is
      a copy of `nested.json` taken before the install container ran). A path
      that is not a safe directory with a package.json, or that already has
      node_modules, is skipped. prep-finalize then records each moved
      directory as a prep root, so it is never copied back to the host.

  transfer HOST DEST MANIFEST
      Apply the agent's changes in DEST to HOST (answer Q5 A): changed or new
      regular files are written atomically with mode 0644/0755, removed files
      and symlinks are unlinked. A new or changed symlink, a special file, or
      any host path that no longer matches the manifest rejects the whole
      transfer before the first host write.

  seed-git HOST DEST [BASE_REF]
      Best effort: create a credential-free git repository in DEST whose
      HEAD contains only allowed blobs from HOST's HEAD tree. Filtered
      paths and their objects are not available through git show.
      With BASE_REF (a ref or commit in HOST, normally the default branch
      fetched by the caller), the same filter builds a second commit from
      it: branch `isolated-base`, and the parent of HEAD. The agent can then
      read the default branch's files offline (`git show isolated-base:PATH`,
      `git diff isolated-base`) without any remote (fun-token-multi-chain#639).
      An unresolvable BASE_REF is logged and left out.

  copy-include SRC TARGET
      Copy a trusted runtime file or directory SRC (regular files only, no
      symlinks, no .git) to TARGET; the helper mounts it read-only at SRC's
      absolute path, or places it inside a read-only workdir snapshot.

Hidden paths: CODEX_ISOLATED_HIDE (comma-separated top-level file names,
for example `CLAUDE.md` when the Claude engine's hide_claude_md is on) are
treated like credential paths by every subcommand: never copied into the
container, absent from the synthetic git tree, and never written back, so the
host file is never moved or replaced.

Every failure prints `::error::CODEX_ISOLATION ...` and exits 1.
"""

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


MAX_READONLY_FILE = 2 * 1024 * 1024
MAX_READONLY_FILES = 50000
MAX_READONLY_TOTAL = 512 * 1024 * 1024
MAX_WORKSPACE_ENTRIES = 300000
MAX_WORKSPACE_TOTAL = 4 * 1024 * 1024 * 1024
MAX_WORKSPACE_FILE = 512 * 1024 * 1024
MAX_TRANSFER_FILES = 50000
MAX_TRANSFER_TOTAL = 2 * 1024 * 1024 * 1024
MAX_INCLUDE_TOTAL = 256 * 1024 * 1024
MAX_DEPS_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_DEPS_REQUIREMENTS = 5000
MAX_NESTED_NODE_PROJECTS = 6
MAX_NESTED_NODE_DEPTH = 4
NESTED_NODE_LOCKS = (("package-lock.json", "npm-ci"), ("yarn.lock", "yarn"), ("pnpm-lock.yaml", "pnpm"))
CHUNK = 1024 * 1024
BINARY_SNIFF_BYTES = 8192

DEPS_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?=$|[\s\[<>=!~;])")
DEPS_HASH_RE = re.compile(r"--hash=(?:sha256|sha384|sha512):[a-fA-F0-9]+$")

# Read-only snapshots never include support checkouts, Codex state, env files
# or anything that looks like a credential store (clarify's rules, Q11 A).
READONLY_BAD_PARTS = {
	".git", ".codex", ".codex-workflow-src", ".codex-workflow-src-main", "secrets", "credentials", "__pycache__",
	".ssh", ".aws", ".azure", ".kube", ".config", ".docker", ".npmrc", ".netrc", ".pypirc",
	"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
HIDDEN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def hidden_names():
	"""Top-level file names CODEX_ISOLATED_HIDE keeps out of the container."""
	names = set()
	for value in os.environ.get("CODEX_ISOLATED_HIDE", "").split(","):
		value = value.strip()
		if not value:
			continue
		if not HIDDEN_NAME_RE.match(value) or value in (".", ".."):
			raise Rejected(f"invalid hidden path: {value!r}")
		names.add(value)
	return frozenset(names)


HIDDEN_NAMES = frozenset()


class Rejected(Exception):
	pass


def log(message):
	print(f"CODEX_ISOLATION {message}", file=sys.stderr)


def git_env(home):
	"""Environment for host-side git reads (local object reads only).

	The caller's environment is kept (GIT_DIR / GIT_WORK_TREE matter: the
	implement workspace is a split work tree), with system/global config,
	prompts and optional locks switched off. Nothing here talks to a remote,
	and nothing read here reaches the container except blob/tree objects.
	"""
	env = dict(os.environ)
	env.update({
		"HOME": str(home),
		"GIT_CONFIG_GLOBAL": os.devnull,
		"GIT_CONFIG_NOSYSTEM": "1",
		"GIT_TERMINAL_PROMPT": "0",
		"GIT_LFS_SKIP_SMUDGE": "1",
		"GIT_OPTIONAL_LOCKS": "0",
	})
	return env


GIT_SAFE = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]


def safe_name(name):
	parts = PurePosixPath(name).parts
	return bool(parts) and not name.startswith("/") and ".." not in parts and "\\" not in name and "\n" not in name and "\r" not in name and "\0" not in name


def has_git_part(name):
	return ".git" in PurePosixPath(name).parts


def readonly_allowed(name):
	if not safe_name(name):
		return False
	if name in HIDDEN_NAMES:
		return False
	for part in PurePosixPath(name).parts:
		lower = part.lower()
		if (lower in READONLY_BAD_PARTS or lower.startswith(".env") or lower.endswith(SECRET_SUFFIXES)
			or lower in ("secret", "credential")
			or lower.endswith(("_secret", "_secrets", "_credential", "_credentials", "-secret", "-secrets", "-credential", "-credentials"))
			or (lower.rsplit(".", 1)[-1] in ("json", "yaml", "yml", "toml", "ini", "env", "txt", "conf", "cfg", "properties", "xml")
				and (lower.rsplit(".", 1)[0] in ("secret", "secrets", "credential", "credentials")
					or lower.rsplit(".", 1)[0].endswith(("_secret", "_secrets", "_credential", "_credentials", "-secret", "-secrets", "-credential", "-credentials", ".secret", ".secrets", ".credential", ".credentials"))))):
			return False
	return True


def walk_no_follow(path, name):
	"""Return the final node for `name` under `path`, refusing symlinked parents."""
	node = path
	parts = PurePosixPath(name).parts
	for index, part in enumerate(parts):
		node = node / part
		if index < len(parts) - 1:
			info = node.lstat()
			if not stat.S_ISDIR(info.st_mode):
				raise Rejected("non-directory parent")
	return node


def copy_regular(source, target, info, limit):
	"""Copy one regular file without following symlinks; returns (sha256, size)."""
	if info.st_size > limit:
		raise Rejected("file too large")
	fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
	digest = hashlib.sha256()
	size = 0
	with os.fdopen(fd, "rb") as handle:
		opened = os.fstat(handle.fileno())
		if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
			raise Rejected("file changed during copy")
		target.parent.mkdir(parents=True, exist_ok=True)
		with open(target, "xb") as sink:
			while True:
				chunk = handle.read(CHUNK)
				if not chunk:
					break
				size += len(chunk)
				if size > limit:
					raise Rejected("file grew during copy")
				digest.update(chunk)
				sink.write(chunk)
	mode = 0o755 if info.st_mode & 0o111 else 0o644
	os.chmod(target, mode)
	return digest.hexdigest(), size, mode


def hash_regular(path):
	info = path.lstat()
	if not stat.S_ISREG(info.st_mode):
		raise Rejected("not a regular file")
	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
	digest = hashlib.sha256()
	with os.fdopen(fd, "rb") as handle:
		while True:
			chunk = handle.read(CHUNK)
			if not chunk:
				break
			digest.update(chunk)
	return digest.hexdigest(), 0o755 if info.st_mode & 0o111 else 0o644


def tracked_paths(host):
	try:
		out = subprocess.run(GIT_SAFE + ["ls-files", "-z", "--cached"], cwd=host, env=git_env(tempfile.gettempdir()), check=True, capture_output=True).stdout
	except (OSError, subprocess.CalledProcessError):
		return None
	return [entry.decode("utf-8") for entry in out.split(b"\0") if entry]


def walk_paths(host, skip_dirs=frozenset()):
	for directory, dirs, files in os.walk(host, followlinks=False):
		rel = Path(directory).relative_to(host)
		for child in sorted(dirs):
			name = (rel / child).as_posix()
			if child == ".git" or name in skip_dirs:
				dirs.remove(child)
		for child in sorted(files):
			yield (rel / child).as_posix()
		for child in sorted(dirs):
			if os.path.islink(os.path.join(directory, child)):
				yield (rel / child).as_posix()


def snapshot_readonly(host, dest):
	names = tracked_paths(host)
	source = "git"
	if names is None:
		names = list(walk_paths(host))
		source = "walk"
	count = 0
	total = 0
	skipped_large = 0
	skipped_other = 0
	for name in sorted(set(names)):
		if not readonly_allowed(name):
			skipped_other += 1
			continue
		try:
			node = walk_no_follow(host, name)
			info = node.lstat()
		except (FileNotFoundError, NotADirectoryError, Rejected):
			skipped_other += 1
			continue
		if not stat.S_ISREG(info.st_mode) or info.st_mode & (stat.S_ISUID | stat.S_ISGID):
			skipped_other += 1  # Symlinks and special files never enter the snapshot.
			continue
		if info.st_size > MAX_READONLY_FILE:
			skipped_large += 1
			continue
		count += 1
		total += info.st_size
		if count > MAX_READONLY_FILES or total > MAX_READONLY_TOTAL:
			raise Rejected(f"read-only snapshot limit exceeded (files={count} bytes={total})")
		copy_regular(node, dest.joinpath(*PurePosixPath(name).parts), info, MAX_READONLY_FILE)
	log(f"snapshot mode=read-only source={source} files={count} bytes={total} skipped_large={skipped_large} skipped_other={skipped_other}")


def _looks_binary(node):
	"""True when a file's first bytes hold a NUL, git's binary heuristic."""
	try:
		fd = os.open(node, os.O_RDONLY | os.O_NOFOLLOW)
	except OSError:
		return True  # Unreadable: never chunk it as text.
	with os.fdopen(fd, "rb") as handle:
		return b"\0" in handle.read(BINARY_SNIFF_BYTES)


def export_oversized(host, scope_file, dest, max_file, max_total, scope_mode="explicit"):
	if scope_mode not in ("explicit", "all"):
		raise Rejected("invalid oversized scope mode")
	try:
		file_cap, total_cap = int(str(max_file)), min(int(str(max_total)), MAX_INCLUDE_TOTAL)
	except ValueError as exc:
		raise Rejected("invalid oversized export cap") from exc
	if file_cap <= 0 or total_cap <= 0:
		raise Rejected("invalid oversized export cap")
	scope = {name for name in scope_file.read_text(encoding="utf-8").splitlines() if name}
	# git diff --name-only quotes paths containing newlines or non-ASCII
	# characters. Such a display name cannot be matched to ls-files -z;
	# refusing it is safer than declaring an omitted scoped file clean.
	if any(not safe_name(name) or name.startswith('"') for name in scope):
		raise Rejected("unrepresentable oversized scope path")
	names = tracked_paths(host)
	if names is None:
		names = list(walk_paths(host))
	scoped = []
	unscoped = []
	unscoped_text_capped = []
	# Scoped files the credential / hidden-path filter keeps out of every
	# isolated view. They can never be inspected, so rejecting them would
	# block every audit whose diff touches one (a committed `.env.example`
	# stalled fun-token-multi-chain#579's security pass on every poll).
	# They are recorded instead and reported as not inspected.
	filtered_scoped = []
	explicit_candidates = []
	extra_candidates = []
	total = 0
	for name in sorted(set(names)):
		if not readonly_allowed(name):
			if name in scope:
				try:
					filtered_node = walk_no_follow(host, name)
					filtered_node.lstat()
				except (FileNotFoundError, NotADirectoryError):
					pass  # A deleted file has no contents to audit.
				else:
					filtered_reason = "hidden" if name in HIDDEN_NAMES else "credential_filter"
					filtered_scoped.append({"path": name, "reason": filtered_reason})
			continue
		try:
			node = walk_no_follow(host, name)
			info = node.lstat()
		except (FileNotFoundError, NotADirectoryError):
			continue
		except Rejected:
			if name in scope:
				raise
			continue
		if not stat.S_ISREG(info.st_mode) or info.st_mode & (stat.S_ISUID | stat.S_ISGID):
			if name in scope:
				raise Rejected(f"scoped file excluded by read-only snapshot (path={name})")
			continue
		if info.st_size <= MAX_READONLY_FILE:
			continue
		if name in scope:
			explicit_candidates.append((name, node, info))
		elif scope_mode == "all":
			extra_candidates.append((name, node, info))
		else:
			unscoped.append({"path": name, "size": info.st_size, "reason": "outside_scope"})
	# Explicitly scoped files fail closed and take the cap budget first.
	for name, node, info in explicit_candidates:
		if info.st_size > file_cap:
			raise Rejected(f"scoped oversized file exceeds cap (path={name} size={info.st_size} cap={file_cap})")
		total += info.st_size
		if total > total_cap:
			raise Rejected(
				f"scoped oversized total exceeds cap "
				f"(files={len(scoped) + 1} total={total} cap={total_cap} last_path={name})"
			)
		scoped.append((name, node, info))
	# Full-audit extras never fail the audit: binaries and files past the caps
	# are reported as not inspected instead.
	for name, node, info in extra_candidates:
		if info.st_size > file_cap:
			reason = "over_file_cap"
		elif total + info.st_size > total_cap:
			reason = "over_total_cap"
		elif _looks_binary(node):
			unscoped.append({"path": name, "size": info.st_size, "reason": "binary"})
			continue
		else:
			total += info.st_size
			scoped.append((name, node, info))
			continue
		cap_skip_record = {"path": name, "size": info.st_size, "reason": reason}
		unscoped.append(cap_skip_record)
		if not _looks_binary(node):
			unscoped_text_capped.append(cap_skip_record)
	scoped.sort(key=lambda item: item[0])
	unscoped.sort(key=lambda entry: entry["path"])

	remove_node(dest)
	dest.mkdir(parents=True)
	exported = []
	chunk_count = 0
	for number, (name, node, info) in enumerate(scoped, 1):
		folder = f"f{number:03d}"
		(dest / folder).mkdir()
		fd = os.open(node, os.O_RDONLY | os.O_NOFOLLOW)
		digest = hashlib.sha256()
		chunks = []
		line = 1
		size = 0
		with os.fdopen(fd, "rb") as handle:
			opened = os.fstat(handle.fileno())
			if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
				raise Rejected("oversized file changed during export")
			while data := handle.read(CHUNK):
				cut = data.rfind(b"\n") + 1
				if cut >= CHUNK // 2 and cut < len(data):
					handle.seek(cut - len(data), os.SEEK_CUR)
					data = data[:cut]
				part = f"{folder}/part-{len(chunks) + 1:04d}.txt"
				(dest / part).write_bytes(data)
				os.chmod(dest / part, 0o644)
				digest.update(data)
				size += len(data)
				newlines = data.count(b"\n")
				chunks.append({
					"file": part, "start_line": line,
					"end_line": line + newlines - int(data.endswith(b"\n")), "bytes": len(data),
				})
				line += newlines
				chunk_count += 1
			closed = os.fstat(handle.fileno())
			if size != info.st_size or closed.st_size != info.st_size or (closed.st_dev, closed.st_ino) != (info.st_dev, info.st_ino):
				raise Rejected("oversized file changed during export")
		exported.append({"path": name, "size": size, "sha256": digest.hexdigest(), "dir": folder, "chunks": chunks})
	(dest / "manifest.json").write_text(json.dumps({
		"schema_version": "oversized_readonly_export.v1", "threshold_bytes": MAX_READONLY_FILE,
		"scope_mode": scope_mode,
		"chunk_bytes": CHUNK, "scoped": exported, "unscoped_oversized": unscoped[:50],
		"unscoped_oversized_count": len(unscoped),
		"unscoped_text_capped": unscoped_text_capped[:50],
		"unscoped_text_capped_count": len(unscoped_text_capped),
		"filtered_scoped": filtered_scoped[:50],
		"filtered_scoped_count": len(filtered_scoped),
	}), encoding="utf-8")
	log(f"export-oversized scoped={len(scoped)} scoped_bytes={total} chunks={chunk_count} unscoped={len(unscoped)} text_capped={len(unscoped_text_capped)} filtered_scoped={len(filtered_scoped)} mode={scope_mode}")


def load_manifest(manifest):
	data = json.loads(manifest.read_text(encoding="utf-8"))
	if not isinstance(data, dict) or not isinstance(data.get("entries"), dict) or not isinstance(data.get("prep_roots"), list):
		raise Rejected("invalid manifest")
	return data


def under_prep_root(name, prep_roots):
	return any(name == root or name.startswith(root + "/") for root in prep_roots)


def clear_except(directory, prefix, keep):
	"""Remove everything under `directory` except `keep` paths and their ancestors."""
	for child in os.listdir(directory):
		name = f"{prefix}{child}"
		path = directory / child
		if name in keep:
			continue
		if path.is_dir() and not path.is_symlink():
			if any(root.startswith(name + "/") for root in keep):
				clear_except(path, name + "/", keep)
				continue
			shutil.rmtree(path)
		else:
			path.unlink()


def snapshot_workspace(host, dest, manifest):
	prep_roots = []
	if manifest.exists():
		prep_roots = load_manifest(manifest)["prep_roots"]
	# Start from an empty copy apart from dependency directories created by
	# the credential-free dependency phase. Leftovers from an earlier (or
	# killed) attempt never survive into the next one.
	dest.mkdir(parents=True, exist_ok=True)
	clear_except(dest, "", set(prep_roots))
	entries = {}
	total = 0
	skipped = 0
	for directory, dirs, files in os.walk(host, followlinks=False):
		rel = Path(directory).relative_to(host)
		for child in sorted(dirs):
			name = (rel / child).as_posix()
			path = Path(directory) / child
			if not readonly_allowed(name) or under_prep_root(name, prep_roots):
				dirs.remove(child)
				continue
			if path.is_symlink():
				dirs.remove(child)
				files.append(child)
				continue
			entries[name] = ["d"]
			(dest / name).mkdir(parents=True, exist_ok=True)
		for child in sorted(files):
			name = (rel / child).as_posix()
			if not readonly_allowed(name) or under_prep_root(name, prep_roots):
				continue  # A worktree's .git file points back at the host repository.
			if not safe_name(name):
				raise Rejected("unsafe workspace path")
			path = Path(directory) / child
			info = path.lstat()
			if len(entries) >= MAX_WORKSPACE_ENTRIES:
				raise Rejected("workspace entry limit exceeded")
			if stat.S_ISLNK(info.st_mode):
				link = os.readlink(path)
				target = dest / name
				target.parent.mkdir(parents=True, exist_ok=True)
				os.symlink(link, target)
				entries[name] = ["l", link]
			elif stat.S_ISREG(info.st_mode):
				digest, size, mode = copy_regular(path, dest / name, info, MAX_WORKSPACE_FILE)
				total += size
				if total > MAX_WORKSPACE_TOTAL:
					raise Rejected("workspace size limit exceeded")
				entries[name] = ["f", digest, mode]
			else:
				skipped += 1
	manifest.write_text(json.dumps({"entries": entries, "prep_roots": prep_roots}), encoding="utf-8")
	log(f"snapshot mode=workspace entries={len(entries)} bytes={total} skipped_special={skipped} prep_roots={len(prep_roots)}")


def current_state(path):
	"""Describe a copy-side node: ["f", sha, mode] | ["l", target] | ["d"] | ["x"] (special)."""
	info = path.lstat()
	if stat.S_ISLNK(info.st_mode):
		return ["l", os.readlink(path)]
	if stat.S_ISDIR(info.st_mode):
		return ["d"]
	if stat.S_ISREG(info.st_mode):
		digest, mode = hash_regular(path)
		return ["f", digest, mode]
	return ["x"]


def copy_tree_state(dest, prep_roots):
	state = {}
	for directory, dirs, files in os.walk(dest, followlinks=False):
		rel = Path(directory).relative_to(dest)
		for child in sorted(dirs):
			name = (rel / child).as_posix()
			if not readonly_allowed(name) or under_prep_root(name, prep_roots):
				dirs.remove(child)
				continue
			path = Path(directory) / child
			if path.is_symlink():
				dirs.remove(child)
				state[name] = current_state(path)
				continue
			state[name] = ["d"]
		for child in files:
			name = (rel / child).as_posix()
			if not readonly_allowed(name) or under_prep_root(name, prep_roots):
				continue
			if len(state) > MAX_WORKSPACE_ENTRIES:
				raise Rejected("result entry limit exceeded")
			state[name] = current_state(Path(directory) / child)
	return state


def prep_finalize(host, dest, manifest):
	data = load_manifest(manifest)
	entries = data["entries"]
	roots = set()
	# Top-most paths the dependency phase created (node_modules, *.egg-info
	# next to a package, ...) become prep roots: kept in the copy, never
	# copied from or back to the host. Everything that was snapshotted is
	# then restored from the host, so build backends cannot change the files
	# the agent starts from.
	for directory, dirs, files in os.walk(dest, followlinks=False):
		rel = Path(directory).relative_to(dest)
		for child in list(dirs) + list(files):
			name = (rel / child).as_posix()
			if not readonly_allowed(name):
				if child in dirs:
					dirs.remove(child)
				continue
			if name not in entries:
				roots.add(name)
				if child in dirs:
					dirs.remove(child)
	data["prep_roots"] = sorted(roots)
	manifest.write_text(json.dumps(data), encoding="utf-8")
	snapshot_workspace(host, dest, manifest)
	log(f"prep-finalize prep_roots={len(data['prep_roots'])}")


def deps_manifest(work, name):
	"""Read a bounded, non-symlink manifest from the filtered workspace."""
	path = work / name
	try:
		path = walk_no_follow(work, name)
		info = path.lstat()
	except FileNotFoundError:
		return None
	except (NotADirectoryError, Rejected):
		log(f"stage-deps skipped={name} reason=missing_or_unsafe_parent")
		return None
	if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DEPS_MANIFEST_BYTES:
		log(f"stage-deps skipped={name} reason=type_or_size")
		return None
	try:
		fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
		with os.fdopen(fd, "rb") as source:
			opened = os.fstat(source.fileno())
			if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
				raise Rejected("manifest changed during read")
			data = source.read(MAX_DEPS_MANIFEST_BYTES + 1)
			if len(data) > MAX_DEPS_MANIFEST_BYTES:
				log(f"stage-deps skipped={name} reason=size")
				return None
			return data
	except (OSError, Rejected):
		log(f"stage-deps skipped={name} reason=read_failed")
		return None


def deps_normalize_name(value):
	return re.sub(r"[-_.]+", "-", value).lower()


def deps_filter(lines, project_name="", max_entries=MAX_DEPS_REQUIREMENTS):
	"""Accept only third-party distribution specifications, never pip directives."""
	accepted = []
	joined = []
	continuation = ""
	for raw in lines:
		if not isinstance(raw, str):
			log("stage-deps dropped requirement reason=non_string")
			continue
		part = raw.strip()
		if part.endswith("\\"):
			continuation += part[:-1] + " "
			continue
		joined.append(continuation + part)
		continuation = ""
	if continuation:
		joined.append(continuation)
	for index, raw in enumerate(joined):
		# A fragment in an HTTPS URL is not a comment. Only whitespace-#
		# comments are removed; credentials and arbitrary URL schemes are refused.
		line = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
		if not line:
			continue
		reason = ""
		parts = line.split()
		match = DEPS_NAME_RE.match(line)
		if len(accepted) >= max_entries:
			reason = "entry_limit"
		elif any(ord(char) < 32 or ord(char) == 127 for char in line):
			reason = "control_character"
		elif line.startswith("-") or "file:" in line.lower():
			reason = "option_or_file_url"
		elif line[0] in "./~" or not match:
			reason = "not_distribution"
		elif project_name and deps_normalize_name(match.group(1)) == deps_normalize_name(project_name):
			reason = "self_reference"
		else:
			# Pip's requirements-file syntax accepts options after a package.
			# Whitelist only hashes and a single HTTPS direct reference.
			for token in parts[1:]:
				if token.startswith("-") and not DEPS_HASH_RE.fullmatch(token):
					reason = "per_requirement_option"
					break
			if not reason:
					if " @ " in line:
						url_tokens = line.split(" @ ", 1)[1].split()
						url = url_tokens[0] if url_tokens else ""
						parsed = urlsplit(url.removeprefix("git+"))
						if (not (url.startswith("https://") or url.startswith("git+https://"))
							or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
							or any(token != "@" and not DEPS_HASH_RE.fullmatch(token) for token in url_tokens[1:])):
							reason = "unsafe_direct_reference"
					elif "@" in line or "/" in line or "\\" in line or "${" in line:
						reason = "unsafe_requirement"
		if reason:
			# Requirement text may contain tokens or URL credentials: never log it.
			log(f"stage-deps dropped requirement index={index} reason={reason}")
		else:
			accepted.append(line)
	return accepted


def find_nested_node_projects(work):
	"""Subdirectory Node projects with a lockfile, in sorted walk order.

	Returns (paths, over_limit): the relative POSIX paths of at most
	MAX_NESTED_NODE_PROJECTS directories below WORK (never WORK itself), at
	most MAX_NESTED_NODE_DEPTH levels down, that hold a package.json and one of
	NESTED_NODE_LOCKS, plus how many more qualified past the limit. Symlinks
	are never followed; hidden directories, node_modules, credential-looking
	paths and projects that already have node_modules are skipped. Reads
	directory names only, never file content.
	"""
	found = []
	over_limit = 0
	for directory, dirs, files in os.walk(work, followlinks=False):
		here = Path(directory)
		rel = here.relative_to(work)
		depth = len(rel.parts)
		dirs[:] = sorted(
			name for name in dirs
			if depth < MAX_NESTED_NODE_DEPTH and not name.startswith(".") and name != "node_modules"
			and not (here / name).is_symlink() and readonly_allowed((rel / name).as_posix())
		)
		if depth == 0 or "package.json" not in files or not any(lock in files for lock, _ in NESTED_NODE_LOCKS):
			continue
		name = rel.as_posix()
		if not readonly_allowed(name):
			continue
		if (here / "node_modules").exists() or (here / "node_modules").is_symlink():
			log(f"stage-deps nested_skipped=present depth={depth}")
			continue
		if len(found) >= MAX_NESTED_NODE_PROJECTS:
			over_limit += 1
			continue
		found.append(name)
	return found, over_limit


def stage_nested_node(work, stage):
	"""Stage each nested Node project's package.json and lockfile, nothing else."""
	meta = stage / ".codex-deps"
	projects, over_limit = find_nested_node_projects(work)
	listed = []
	for path in projects:
		package = deps_manifest(work, f"{path}/package.json")
		if package is None:
			log("stage-deps nested_skipped=manifest")
			continue
		lock_name, lock_data, mode = "", None, ""
		for name, selected_mode in NESTED_NODE_LOCKS:
			lock_data = deps_manifest(work, f"{path}/{name}")
			if lock_data is not None:
				lock_name, mode = name, selected_mode
				break
		if lock_data is None:
			log("stage-deps nested_skipped=manifest")
			continue
		target = meta / "nested" / str(len(listed))
		target.mkdir(parents=True)
		(target / "package.json").write_bytes(package)
		(target / lock_name).write_bytes(lock_data)
		(target / "mode").write_text(mode + "\n", encoding="ascii")
		listed.append({"index": len(listed), "path": path, "mode": mode})
	if listed:
		(meta / "nested.json").write_text(json.dumps(listed), encoding="utf-8")
	log(f"stage-deps nested_node={len(listed)} nested_over_limit={over_limit}")


def handoff_nested_deps(stage, work, listing):
	"""Move nested node_modules from STAGE into WORK at the listed project paths.

	LISTING is the host-side copy of `nested.json` (the install container can
	write STAGE, so its copy is not trusted). Every path is re-checked here.
	"""
	try:
		entries = json.loads(Path(listing).read_text(encoding="utf-8"))
	except FileNotFoundError:
		entries = []
	except (OSError, ValueError):
		log("handoff-nested-deps skipped=list reason=malformed")
		entries = []
	if not isinstance(entries, list):
		raise Rejected("nested dependency list is not a list")
	moved = 0
	for entry in entries[:MAX_NESTED_NODE_PROJECTS]:
		if not isinstance(entry, dict) or type(entry.get("index")) is not int or not isinstance(entry.get("path"), str):
			log("handoff-nested-deps skipped=entry reason=malformed")
			continue
		index, path = entry["index"], entry["path"]
		# The install container can write every stage directory, so no parent
		# of the source may be a symlink (walk_no_follow lstat-checks them).
		try:
			source = walk_no_follow(stage, f".codex-deps/nested/{index}/node_modules")
		except FileNotFoundError:
			log(f"handoff-nested-deps skipped={index} reason=install_missing")
			continue
		except (NotADirectoryError, Rejected):
			log(f"handoff-nested-deps skipped={index} reason=unsafe_source")
			continue
		if source.is_symlink():
			log(f"handoff-nested-deps skipped={index} reason=unsafe_source")
			continue
		if not source.is_dir():
			log(f"handoff-nested-deps skipped={index} reason=install_missing")
			continue
		parts = PurePosixPath(path).parts
		if not readonly_allowed(path) or not parts or len(parts) > MAX_NESTED_NODE_DEPTH \
			or PurePosixPath(path).is_absolute() \
			or any(part.startswith(".") or part == "node_modules" for part in parts):
			log(f"handoff-nested-deps skipped={index} reason=unsafe_path")
			continue
		try:
			project = walk_no_follow(work, path)
			project_info = project.lstat()
			package_info = (project / "package.json").lstat()
		except (FileNotFoundError, NotADirectoryError, Rejected):
			log(f"handoff-nested-deps skipped={index} reason=missing_project")
			continue
		if not stat.S_ISDIR(project_info.st_mode) or not stat.S_ISREG(package_info.st_mode):
			log(f"handoff-nested-deps skipped={index} reason=missing_project")
			continue
		target = project / "node_modules"
		if target.exists() or target.is_symlink():
			log(f"handoff-nested-deps skipped={index} reason=present")
			continue
		try:
			os.rename(source, target)
		except OSError as exc:
			log(f"handoff-nested-deps skipped={index} reason=rename_failed errno={exc.errno}")
			continue
		moved += 1
	log(f"handoff-nested-deps moved={moved}")


def stage_deps(work, stage):
	"""Stage fixed filenames only; neither parse nor execute source code."""
	meta = stage / ".codex-deps"
	meta.mkdir(parents=True, exist_ok=True)
	package = deps_manifest(work, "package.json")
	locks = (("package-lock.json", "npm-ci"), ("yarn.lock", "yarn"), ("pnpm-lock.yaml", "pnpm"))
	node_mode = "none"
	if (work / "node_modules").exists() or (work / "node_modules").is_symlink():
		node_mode = "present"
	elif package is not None:
		(stage / "package.json").write_bytes(package)
		node_mode = "npm-install"
		for name, selected_mode in locks:
			lock = deps_manifest(work, name)
			if lock is not None:
				(stage / name).write_bytes(lock)
				node_mode = selected_mode
				break
	(meta / "node").write_text(node_mode + "\n", encoding="ascii")

	python_mode = "none"
	requirements = deps_manifest(work, "requirements.txt")
	pyproject = deps_manifest(work, "pyproject.toml")
	project = None
	remaining = MAX_DEPS_REQUIREMENTS
	if pyproject is not None:
		try:
			import tomllib
			project = tomllib.loads(pyproject.decode("utf-8"))
		except (ImportError, ValueError, UnicodeError):
			log("stage-deps skipped=pyproject.toml reason=parse_failed")
	if requirements is not None:
		try:
			lines = requirements.decode("utf-8").splitlines()
		except UnicodeError:
			log("stage-deps skipped=requirements.txt reason=decode_failed")
		else:
			project_name = project.get("project", {}).get("name", "") if isinstance(project, dict) and isinstance(project.get("project"), dict) else ""
			filtered = deps_filter(lines, project_name if isinstance(project_name, str) else "", remaining)
			remaining -= len(filtered)
			(meta / "requirements.txt").write_text("\n".join(filtered) + "\n", encoding="utf-8")
			python_mode = "requirements"
	if pyproject is not None and project is not None:
		project_info = project.get("project", {})
		optional = project_info.get("optional-dependencies", {}) if isinstance(project_info, dict) else {}
		build_info = project.get("build-system", {})
		name = project_info.get("name", "") if isinstance(project_info, dict) else ""
		if not isinstance(name, str):
			name = ""
		for filename, values in (("build.txt", build_info.get("requires", []) if isinstance(build_info, dict) else []),
				("base.txt", project_info.get("dependencies", []) if isinstance(project_info, dict) else []),
				("dev.txt", optional.get("dev", []) if isinstance(optional, dict) else [])):
			filtered = deps_filter(values if isinstance(values, list) else [], name, remaining)
			remaining -= len(filtered)
			(meta / filename).write_text("\n".join(filtered) + "\n", encoding="utf-8")
		python_mode = "both" if python_mode == "requirements" else "pyproject"
	if (python_mode in ("pyproject", "both") and ("project" in project or "build-system" in project)
		or python_mode != "none" and deps_manifest(work, "setup.py") is not None):
		(meta / "source-install").touch()
	(meta / "python").write_text(python_mode + "\n", encoding="ascii")
	if (any(deps_manifest(work, name) is not None for name in ("pytest.ini", "conftest.py", "tests/conftest.py"))
		or isinstance(project, dict) and isinstance(project.get("tool"), dict)
		and isinstance(project["tool"].get("pytest"), dict) and "ini_options" in project["tool"]["pytest"]):
		(meta / "want-pytest").touch()
	log(f"stage-deps node={node_mode} python={python_mode}")
	stage_nested_node(work, stage)


def host_matches(host_path, expected):
	try:
		info = host_path.lstat()
	except FileNotFoundError:
		return expected is None
	if expected is None:
		return False
	kind = expected[0]
	if kind == "d":
		return stat.S_ISDIR(info.st_mode)
	if kind == "l":
		return stat.S_ISLNK(info.st_mode) and os.readlink(host_path) == expected[1]
	if kind == "f":
		return stat.S_ISREG(info.st_mode) and list(hash_regular(host_path)) == expected[1:]
	return False


def transfer(host, dest, manifest):
	data = load_manifest(manifest)
	entries = data["entries"]
	prep_roots = data["prep_roots"]
	results = copy_tree_state(dest, prep_roots)
	for name in sorted(HIDDEN_NAMES):
		if (dest / name).exists() or (dest / name).is_symlink():
			log(f"transfer ignored={name} reason=hidden")
	writes = []
	deletes = {}
	rmdirs = []
	total = 0
	for name in sorted(set(entries) | set(results)):
		if has_git_part(name) or under_prep_root(name, prep_roots):
			continue
		old = entries.get(name)
		new = results.get(name)
		if old == new:
			continue
		if not safe_name(name):
			raise Rejected("unsafe result path")
		if new is not None and new[0] == "l":
			raise Rejected(f"symlink result is not transferred: {name}")
		if new is not None and new[0] == "x":
			raise Rejected(f"special file result is not transferred: {name}")
		if old is not None and old[0] == "d":
			# A removed directory (or one replaced by a file): its children
			# are handled as their own entries; the directory itself is
			# removed once it is empty.
			rmdirs.append(name)
		elif old is not None and (new is None or new[0] == "d"):
			deletes[name] = old
		if new is not None and new[0] == "f":
			writes.append((name, old, new))
			total += (dest / name).lstat().st_size
	if len(writes) + len(deletes) > MAX_TRANSFER_FILES or total > MAX_TRANSFER_TOTAL:
		raise Rejected(f"transfer limit exceeded (files={len(writes) + len(deletes)} bytes={total})")
	# Every precondition is checked before the first host write: host paths
	# still match the manifest, parents are real directories (or are being
	# replaced), and a new file never lands on an existing host path.
	for name, old in deletes.items():
		if not host_matches(walk_no_follow(host, name), old):
			raise Rejected(f"host path changed since snapshot: {name}")
	removed = set(deletes) | set(rmdirs)
	for name, old, _new in writes:
		parts = PurePosixPath(name).parts
		node = host
		for index in range(len(parts) - 1):
			node = node / parts[index]
			parent = "/".join(parts[: index + 1])
			try:
				info = node.lstat()
			except FileNotFoundError:
				break
			if parent in deletes:
				break
			if not stat.S_ISDIR(info.st_mode):
				raise Rejected(f"result parent is not a host directory: {name}")
		target = host / name
		if old is not None and old[0] == "d":
			if not (target.is_dir() and not target.is_symlink()):
				raise Rejected(f"host path changed since snapshot: {name}")
			continue
		if old is None and any(name.startswith(prefix + "/") for prefix in removed):
			if target.exists() or target.is_symlink():
				raise Rejected(f"new result conflicts with host path: {name}")
			continue
		if not host_matches(target, old):
			raise Rejected(f"host path changed since snapshot: {name}")
	for name in rmdirs:
		path = host / name
		if any(f"{name}/{child}" not in removed for child in os.listdir(path)):
			raise Rejected(f"directory contains host-only files: {name}")
	# Stage and hash every replacement and back up every original before the
	# first mutation. A later I/O failure restores all paths touched so far.
	with tempfile.TemporaryDirectory(prefix=".codex-isolated-transfer-", dir=host) as staged_root:
		staged = Path(staged_root)
		originals = {name: entries[name] for name in removed | {name for name, _, _ in writes} if name in entries}
		for name, old in originals.items():
			if old[0] == "f":
				backup_digest, _backup_size, backup_mode = copy_regular(host / name, staged / "before" / name, (host / name).lstat(), MAX_WORKSPACE_FILE)
				if [backup_digest, backup_mode] != old[1:]:
					raise Rejected(f"host path changed during backup: {name}")
		for name, _old, new in writes:
			digest, _size, mode = copy_regular(dest / name, staged / "after" / name, (dest / name).lstat(), MAX_WORKSPACE_FILE)
			if [digest, mode] != new[1:]:
				raise Rejected(f"result changed during transfer: {name}")
		changed = []
		written = 0
		try:
			for name in sorted(deletes, key=lambda item: item.count("/"), reverse=True):
				(host / name).unlink()
				changed.append(name)
			for name in sorted(rmdirs, key=lambda item: item.count("/"), reverse=True):
				(host / name).rmdir()
				changed.append(name)
			for name, _old, _new in writes:
				target = host / name
				missing_parents = []
				parent_path = target.parent
				while parent_path != host and (str(parent_path.relative_to(host)) in deletes or not parent_path.exists()):
					missing_parents.append(parent_path)
					parent_path = parent_path.parent
				changed.extend(str(parent.relative_to(host)) for parent in reversed(missing_parents))
				target.parent.mkdir(parents=True, exist_ok=True)
				os.replace(staged / "after" / name, target)
				changed.append(name)
				written += 1
		except (OSError, Rejected):
			try:
				for name in sorted(set(changed), key=lambda item: item.count("/"), reverse=True):
					path = host / name
					if path.is_dir() and not path.is_symlink():
						path.rmdir()
					elif path.exists() or path.is_symlink():
						path.unlink()
				for name in sorted(set(changed), key=lambda item: item.count("/")):
					old = originals.get(name)
					if old is None:
						continue
					path = host / name
					path.parent.mkdir(parents=True, exist_ok=True)
					if old[0] == "d":
						path.mkdir(exist_ok=True)
					elif old[0] == "l":
						path.symlink_to(old[1])
					else:
						os.replace(staged / "before" / name, path)
			except OSError as exc:
				raise Rejected(f"transfer rollback failed: {type(exc).__name__}") from exc
			raise
	log(f"transfer written={written} deleted={len(deletes)} removed_dirs={len(rmdirs)} bytes={total}")


def remove_node(path):
	if path.is_dir() and not path.is_symlink():
		shutil.rmtree(path, ignore_errors=True)
	elif path.exists() or path.is_symlink():
		path.unlink()


def seed_git_tree_entries(treeish, host, dest, env, *, readonly_limit_only=False):
	"""Return (index entries, blob ids) for the allowed blobs of TREEISH in HOST.

	Applies the read-only snapshot filter (readonly_allowed) and the size
	limits, so a filtered path never becomes readable through `git show`.
	With readonly_limit_only every blob is held to MAX_READONLY_FILE; the
	larger workspace limit applies only to HEAD files present in DEST.
	"""
	ls_tree = subprocess.run(GIT_SAFE + ["ls-tree", "-rz", "--full-tree", treeish], cwd=host, env=env, check=True, capture_output=True).stdout
	index_entries = []
	blob_ids = set()
	eligible = []
	for record in ls_tree.split(b"\0"):
		if not record:
			continue
		meta, path = record.split(b"\t", 1)
		mode, kind, oid = meta.split(b" ")
		name = path.decode("utf-8")
		if kind != b"blob" or mode not in (b"100644", b"100755") or not readonly_allowed(name):
			continue
		eligible.append((mode, oid, path, name))
	blob_sizes = {}
	if eligible:
		blob_input = b"\n".join(sorted({oid for _mode, oid, _path, _name in eligible})) + b"\n"
		blob_metadata = subprocess.run(GIT_SAFE + ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"], cwd=host, env=env, input=blob_input, check=True, capture_output=True).stdout
		for line in blob_metadata.splitlines():
			object_id, object_type, object_size = line.split()
			if object_type != b"blob":
				raise Rejected("non-blob in synthetic Git tree")
			blob_sizes[object_id] = int(object_size)
	readonly_total = 0
	for mode, oid, path, name in eligible:
		if readonly_limit_only:
			limit = MAX_READONLY_FILE
		else:
			limit = MAX_READONLY_FILE if (dest / name).is_dir() or not (dest / name).exists() else MAX_WORKSPACE_FILE
		if blob_sizes[oid] > limit:
			continue
		index_entries.append(mode + b" " + oid + b"\t" + path + b"\0")
		blob_ids.add(oid)
		if readonly_limit_only:
			# Same totals as snapshot-readonly; the caller drops the tree.
			readonly_total += blob_sizes[oid]
			if len(index_entries) > MAX_READONLY_FILES or readonly_total > MAX_READONLY_TOTAL:
				raise Rejected("tree exceeds read-only snapshot limits")
	return index_entries, blob_ids


def seed_git(host, dest, base_ref=""):
	home = Path(tempfile.mkdtemp(prefix="codex-isolated-git-"))
	try:
		env = git_env(home)
		try:
			tree = subprocess.run(GIT_SAFE + ["rev-parse", "--verify", "HEAD^{tree}"], cwd=host, env=env, check=True, capture_output=True, text=True).stdout.strip()
		except (OSError, subprocess.CalledProcessError):
			log("seed-git outcome=skipped reason=no_head")
			return
		if not tree or not all(c in "0123456789abcdef" for c in tree):
			log("seed-git outcome=skipped reason=bad_tree")
			return
		base_commit = ""
		base_state = "none"
		if base_ref:
			try:
				base_commit = subprocess.run(
					GIT_SAFE + ["rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"],
					cwd=host, env=env, check=True, capture_output=True, text=True,
				).stdout.strip()
			except (OSError, subprocess.CalledProcessError):
				base_commit = ""
			if not base_commit or not all(c in "0123456789abcdef" for c in base_commit):
				base_commit = ""
				base_state = "unresolved"
		# HEAD may contain tracked credentials excluded from the snapshot.
		# Construct a fresh index/tree from allowed blobs only, rather than
		# packing HEAD (which would make filtered files readable via git show).
		index_entries, blob_ids = seed_git_tree_entries("HEAD", host, dest, env)
		base_index_entries = []
		if base_commit:
			# The base is optional: any failure leaves it out, never HEAD.
			try:
				base_index_entries, base_blob_ids = seed_git_tree_entries(base_commit, host, dest, env, readonly_limit_only=True)
				blob_ids |= base_blob_ids
			except (OSError, subprocess.CalledProcessError, Rejected, ValueError) as exc:
				log(f"seed-git base_skipped reason={type(exc).__name__}")
				base_commit, base_index_entries, base_state = "", [], "unresolved"
		pack = home / "objects.pack"
		with open(pack, "wb") as sink:
			objects = b"\n".join(sorted(blob_ids)) + b"\n" if blob_ids else b""
			subprocess.run(GIT_SAFE + ["pack-objects", "--stdout", "-q"], cwd=host, env=env, input=objects, stdout=sink, check=True)
		local = dict(env)
		local.pop("GIT_INDEX_FILE", None)
		local.pop("GIT_COMMON_DIR", None)
		local.pop("GIT_OBJECT_DIRECTORY", None)
		local.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
		local.update({
			"GIT_DIR": str(dest / ".git"), "GIT_WORK_TREE": str(dest),
			"GIT_AUTHOR_NAME": "isolated", "GIT_AUTHOR_EMAIL": "isolated@invalid",
			"GIT_COMMITTER_NAME": "isolated", "GIT_COMMITTER_EMAIL": "isolated@invalid",
		})
		remove_node(dest / ".git")
		subprocess.run(["git", "init", "-q", str(dest)], env=local, check=True, capture_output=True)
		with open(pack, "rb") as source:
			subprocess.run(["git", "unpack-objects", "-q"], env=local, stdin=source, check=True, capture_output=True)
		head_parents = []
		if base_commit:
			# The base tree is built in a scratch index outside DEST, so the
			# agent's index only ever holds HEAD's filtered tree.
			base_local = dict(local, GIT_INDEX_FILE=str(home / "base.index"))
			try:
				subprocess.run(["git", "update-index", "-z", "--index-info"], env=base_local, input=b"".join(base_index_entries), check=True, capture_output=True)
				base_tree = subprocess.run(["git", "write-tree"], env=base_local, check=True, capture_output=True, text=True).stdout.strip()
				base_synthetic = subprocess.run(["git", "commit-tree", base_tree, "-m", "isolated base (default branch)"], env=local, check=True, capture_output=True, text=True).stdout.strip()
				subprocess.run(["git", "update-ref", "refs/heads/isolated-base", base_synthetic], env=local, check=True, capture_output=True)
				head_parents = ["-p", base_synthetic]
				base_state = "present"
			except (OSError, subprocess.CalledProcessError) as exc:
				log(f"seed-git base_skipped reason={type(exc).__name__}")
				base_state = "unresolved"
		subprocess.run(["git", "update-index", "-z", "--index-info"], env=local, input=b"".join(index_entries), check=True, capture_output=True)
		filtered_tree = subprocess.run(["git", "write-tree"], env=local, check=True, capture_output=True, text=True).stdout.strip()
		commit = subprocess.run(["git", "commit-tree", filtered_tree, *head_parents, "-m", "isolated snapshot"], env=local, check=True, capture_output=True, text=True).stdout.strip()
		subprocess.run(["git", "update-ref", "refs/heads/isolated", commit], env=local, check=True, capture_output=True)
		subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/isolated"], env=local, check=True, capture_output=True)
		subprocess.run(["git", "read-tree", filtered_tree], env=local, check=True, capture_output=True)
		log(f"seed-git outcome=ok base={base_state}")
	except (OSError, subprocess.CalledProcessError) as exc:
		log(f"seed-git outcome=skipped reason={type(exc).__name__}")
		remove_node(dest / ".git")
	finally:
		shutil.rmtree(home, ignore_errors=True)


def copy_include(source, target):
	"""Copy SRC (regular files only, no symlinks, no .git) to exactly TARGET."""
	source = Path(os.path.abspath(source))
	if not source.is_absolute() or source.is_symlink():
		raise Rejected("include must be an absolute, non-symlink path")
	total = 0
	info = source.lstat()
	if stat.S_ISREG(info.st_mode):
		if not readonly_allowed(source.name):
			raise Rejected("include path filtered")
		pairs = [(source, target, info)]
	elif stat.S_ISDIR(info.st_mode):
		if not readonly_allowed(source.name):
			raise Rejected("include path filtered")
		pairs = []
		for directory, dirs, files in os.walk(source, followlinks=False):
			dirs[:] = [d for d in dirs if readonly_allowed((Path(directory) / d).relative_to(source).as_posix()) and not (Path(directory) / d).is_symlink()]
			for child in files:
				path = Path(directory) / child
				child_info = path.lstat()
				if readonly_allowed(path.relative_to(source).as_posix()) and stat.S_ISREG(child_info.st_mode):
					pairs.append((path, target / path.relative_to(source), child_info))
	else:
		raise Rejected("include must be a regular file or directory")
	for path, destination, path_info in pairs:
		total += path_info.st_size
		if total > MAX_INCLUDE_TOTAL:
			raise Rejected("include size limit exceeded")
		if destination.exists():
			destination.unlink()
		copy_regular(path, destination, path_info, MAX_INCLUDE_TOTAL)
	if not pairs and stat.S_ISDIR(info.st_mode):
		target.mkdir(parents=True, exist_ok=True)
	log(f"include files={len(pairs)} bytes={total}")


def main():
	command = sys.argv[1] if len(sys.argv) > 1 else ""
	arity = {"snapshot-readonly": 2, "export-oversized": 5, "snapshot-workspace": 3, "prep-finalize": 3, "stage-deps": 2, "handoff-nested-deps": 3, "transfer": 3, "seed-git": 2, "copy-include": 2}
	if command not in arity or (len(sys.argv) != arity[command] + 2
		and not (command == "export-oversized" and len(sys.argv) == 8)
		and not (command == "seed-git" and len(sys.argv) == 5)):
		print("usage: codex_isolated_workspace.py <snapshot-readonly|export-oversized|snapshot-workspace|prep-finalize|stage-deps|handoff-nested-deps|transfer|seed-git|copy-include> ARGS", file=sys.stderr)
		raise SystemExit(2)
	args = [Path(value) for value in sys.argv[2:]]
	if command == "export-oversized" and len(args) == 6:
		args[-1] = str(args[-1])
	if command == "seed-git" and len(args) == 3:
		# BASE_REF is a git ref, not a path; an empty value means none.
		args[-1] = sys.argv[4]
	global HIDDEN_NAMES
	try:
		HIDDEN_NAMES = hidden_names()
		if command == "snapshot-readonly":
			snapshot_readonly(*args)
		elif command == "export-oversized":
			export_oversized(*args)
		elif command == "snapshot-workspace":
			snapshot_workspace(*args)
		elif command == "prep-finalize":
			prep_finalize(*args)
		elif command == "stage-deps":
			stage_deps(*args)
		elif command == "handoff-nested-deps":
			handoff_nested_deps(*args)
		elif command == "transfer":
			transfer(*args)
		elif command == "seed-git":
			seed_git(*args)
		else:
			copy_include(*args)
	except (OSError, ValueError, UnicodeError, Rejected, subprocess.CalledProcessError) as exc:
		detail = str(exc) if isinstance(exc, Rejected) else type(exc).__name__
		print(f"::error::CODEX_ISOLATION {command} rejected: {detail}", file=sys.stderr)
		raise SystemExit(1) from None


if __name__ == "__main__":
	main()
