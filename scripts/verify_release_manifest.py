#!/usr/bin/env python3
"""Verify a coding-workflows release tree against its release manifest.

The consumer updater (`.github/workflows/update_workflows.yml`) will run this
check before it reads or copies anything from a release checkout, and will
refuse any content the manifest does not vouch for. Nothing calls it yet; the
next slice of project #6902 wires it into the updater behind a default-off
flag, so production behaviour is unchanged.

Split of responsibilities:
- Attestation signature verification is NOT done here. The wiring slice runs
  `gh attestation verify` on the manifest file first; this script only checks
  that the manifest is well formed, belongs to the expected repository and
  release commit, and matches the bytes, sizes, modes and symlinks of the
  release tree.
- The manifest format has one definition: the field rules and constants come
  from `scripts/release_manifest.py` (the builder) and
  `scripts/workflow_wrapper_refs.py`, and a test ties this stdlib check to
  `ai-memory/schemas/release_manifest.v1.json`.
- The tree must not change between verification and copy. The caller copies
  from the same verified tree immediately; concurrent modification after the
  check is out of scope here.

Assumption: the authoritative plan
(docs/plans/safeguarded-consumer-updater-plan.md on branch
claude/elegant-mendel-5pli2i-safeguarded-updater-plan) could not be read from
the planning or implementation sandbox, so this module uses the provisional
names from issue #6957: log prefix UPDATER_MANIFEST_VERIFY, the reason tokens
below, and the CLI flags. If the plan names different identifiers, the plan
wins and a follow-up should align them.

Usage:
	verify_release_manifest.py verify --manifest <path> --release-root <dir>
		--expected-sha <40-hex> [--expected-repository shubhodeep1/coding-workflows]
		[--paths-file <file>]

--paths-file lists, one per line, the paths the updater is about to read or
copy; every listed path must be in the manifest (exact match, no
normalisation, empty lines ignored).

	verify_release_manifest.py verify-pr-tree --manifest <path> --release-root <dir>
		--expected-sha <40-hex> [--expected-repository shubhodeep1/coding-workflows]
		--git-dir <pr-checkout/.git> --base <parent-sha> --head <pushed-sha>
		--reproduced-root <base-worktree>

verify-pr-tree (issue #7004) checks the updater's pull request instead of the
release tree. It first verifies every manifest entry exactly like `verify`,
then reads the PR's change set from git blobs (`diff-tree` / `cat-file`, never
working-tree files, so PR-controlled .gitattributes cannot hide a mismatch)
and accepts a changed path only when:
- it is a derived file (audit-gate or changelog output) and its blob equals
  what the attested scripts produced in --reproduced-root (a worktree of
  --base the caller ran them on), or both sides deleted it;
- it is `.github/workflows/<name>.yml` and equals the attested
  `workflow-templates/<name>.yml` rendered with workflow_wrapper_refs at the
  release SHA;
- it is `.claude/<rel>` or `CLAUDE.md` and its sha256 equals the manifest
  entry of `workflow-templates/.claude/<rel>` or `CLAUDE.md`;
- it is deleted, listed in the attested `workflow-templates/retired_files.txt`
  and its base blob sha256 is one of the listed hashes.
A wrapper, `.claude/` or `CLAUDE.md` path the PR adds, or whose mode the PR
changes, must carry the manifest entry's mode (`mode_mismatch`).
Anything else is `unlisted_path`. Every reproduced path must be in the change
set (`derived_missing`). Added or modified paths must be regular files.

Output: exactly one line on stdout and nothing on stderr:
	UPDATER_MANIFEST_VERIFY outcome=ok reason=ok count=<n>
	UPDATER_MANIFEST_VERIFY outcome=rejected reason=<token> count=<n>[ path=<p>]
Exit 0 on success, 2 on any rejection (fail closed: parse errors, missing
fields, mismatches, unreadable files and unexpected exceptions all reject).
`path=` appears only for per-entry and unlisted-path rejections, with every
character outside printable ASCII replaced by `?`.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

LOG_PREFIX = "UPDATER_MANIFEST_VERIFY"

REASONS = frozenset(
	{
		"ok",
		"invalid_argument",
		"manifest_unreadable",
		"paths_file_unreadable",
		"schema_invalid",
		"repository_mismatch",
		"sha_mismatch",
		"unlisted_path",
		"missing_file",
		"read_failed",
		"non_regular_file",
		"symlink_mismatch",
		"path_escape",
		"mode_mismatch",
		"size_mismatch",
		"hash_mismatch",
		"internal_error",
		# verify-pr-tree (issue #7004)
		"content_mismatch",
		"retired_hash_mismatch",
		"derived_mismatch",
		"derived_missing",
		"too_many_changes",
		"git_failed",
	}
)

MANIFEST_MAX_BYTES = 16 * 1024 * 1024
PATHS_FILE_MAX_BYTES = 4 * 1024 * 1024
HASH_CHUNK_BYTES = 64 * 1024
NEUTRALIZE_MAX_CHARS = 200
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
FILE_MODES = ("100644", "100755")
SYMLINK_MODE = "120000"
TOP_LEVEL_KEYS = frozenset({"schema_version", "repository", "release_sha", "tag", "files"})
ENTRY_KEYS = frozenset({"path", "sha256", "size", "mode"})
SYMLINK_ENTRY_KEYS = ENTRY_KEYS | {"link_target"}
# verify-pr-tree limits and patterns.
MAX_PR_CHANGES = 2000
MAX_BLOB_BYTES = 32 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 300
OBJECT_ID_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
WRAPPER_PATH_RE = re.compile(r"\.github/workflows/([A-Za-z0-9._-]+\.yml)\Z")
CLAUDE_DIR_PREFIX = ".claude/"
CLAUDE_TEMPLATE_PREFIX = "workflow-templates/.claude/"
RETIRED_FILES_PATH = "workflow-templates/retired_files.txt"

# Builder-side reason -> verifier reason for _safe_rel refusals.
_SAFE_REL_REASONS = {
	"unexpected_symlink": "symlink_mismatch",
	"path_escape": "path_escape",
	"non_regular_file": "non_regular_file",
	"read_failed": "read_failed",
}

# Sibling modules, loaded inside main() so a missing module still produces a
# structured internal_error line.
_rm = None
_wrr = None


class VerifyError(Exception):
	"""Rejection with a fixed reason token and an optional release path."""

	def __init__(self, reason: str, path: str | None = None):
		super().__init__(reason)
		self.reason = reason
		self.path = path


class _ArgParser(argparse.ArgumentParser):
	def error(self, message: str):  # noqa: ARG002 - message is intentionally dropped
		raise VerifyError("invalid_argument")


def _load_deps() -> None:
	global _rm, _wrr
	if _rm is not None and _wrr is not None:
		return
	script_dir = str(Path(__file__).resolve().parent)
	if script_dir not in sys.path:
		sys.path.insert(0, script_dir)
	import release_manifest  # noqa: PLC0415
	import workflow_wrapper_refs  # noqa: PLC0415

	_rm = release_manifest
	_wrr = workflow_wrapper_refs


def neutralize(text: str) -> str:
	out = []
	for char in str(text)[:NEUTRALIZE_MAX_CHARS]:
		out.append(char if 0x21 <= ord(char) <= 0x7E else "?")
	return "".join(out)


def emit(outcome: str, reason: str, count: int, path: str | None = None) -> None:
	if reason not in REASONS:
		reason = "internal_error"
	line = f"{LOG_PREFIX} outcome={outcome} reason={reason} count={int(count)}"
	if path is not None:
		line += f" path={neutralize(path)}"
	sys.stdout.write(line + "\n")
	sys.stdout.flush()


def _read_capped(path: str, cap: int, unreadable_reason: str) -> bytes:
	flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
	try:
		fd = os.open(path, flags)
	except OSError as exc:
		raise VerifyError(unreadable_reason) from exc
	try:
		with os.fdopen(fd, "rb") as handle:
			if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
				raise VerifyError(unreadable_reason)
			data = handle.read(cap + 1)
	except OSError as exc:
		raise VerifyError(unreadable_reason) from exc
	return data


def _reject_duplicate_keys(pairs):
	result = {}
	for key, value in pairs:
		if key in result:
			raise ValueError("duplicate key")
		result[key] = value
	return result


def _reject_constant(name):
	raise ValueError("non-finite number")


def load_manifest(path: str):
	data = _read_capped(path, MANIFEST_MAX_BYTES, "manifest_unreadable")
	if len(data) > MANIFEST_MAX_BYTES:
		raise VerifyError("schema_invalid")
	try:
		text = data.decode("utf-8")
		return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
	except (ValueError, RecursionError) as exc:
		raise VerifyError("schema_invalid") from exc


def _is_int(value) -> bool:
	return isinstance(value, int) and not isinstance(value, bool)


def _valid_path(value) -> bool:
	if not isinstance(value, str) or not value:
		return False
	for segment in value.split("/"):
		if not _rm.PATH_SEGMENT_RE.fullmatch(segment) or segment in (".", ".."):
			return False
	return True


def _validate_entry(entry) -> None:
	if not isinstance(entry, dict):
		raise VerifyError("schema_invalid")
	mode = entry.get("mode")
	keys = frozenset(entry.keys())
	expected_keys = SYMLINK_ENTRY_KEYS if mode == SYMLINK_MODE else ENTRY_KEYS
	if keys != expected_keys:
		raise VerifyError("schema_invalid")
	path = entry["path"]
	if not _valid_path(path):
		raise VerifyError("schema_invalid")
	sha = entry["sha256"]
	if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
		raise VerifyError("schema_invalid")
	size = entry["size"]
	if not _is_int(size) or size < 0:
		raise VerifyError("schema_invalid")
	if mode == SYMLINK_MODE:
		expected_target = _rm.KNOWN_SYMLINKS.get(path)
		if expected_target is None or entry["link_target"] != expected_target:
			raise VerifyError("schema_invalid")
	elif mode in FILE_MODES:
		if path in _rm.KNOWN_SYMLINKS:
			raise VerifyError("schema_invalid")
	else:
		raise VerifyError("schema_invalid")


def validate_manifest(doc, expected_sha: str, expected_repo: str) -> list:
	"""Return the entries list; checks run in the D3 order of the plan."""
	if not isinstance(doc, dict) or frozenset(doc.keys()) != TOP_LEVEL_KEYS:
		raise VerifyError("schema_invalid")
	if doc["schema_version"] != _rm.SCHEMA_VERSION:
		raise VerifyError("schema_invalid")
	repository = doc["repository"]
	if not isinstance(repository, str) or repository != expected_repo:
		raise VerifyError("repository_mismatch")
	if repository != _rm.RELEASE_MANIFEST_REPOSITORY:
		raise VerifyError("schema_invalid")
	release_sha = doc["release_sha"]
	if not isinstance(release_sha, str) or release_sha != expected_sha:
		raise VerifyError("sha_mismatch")
	tag = doc["tag"]
	if not isinstance(tag, str) or not _rm.TAG_RE.fullmatch(tag):
		raise VerifyError("schema_invalid")
	files = doc["files"]
	if not isinstance(files, list) or not files:
		raise VerifyError("schema_invalid")
	previous = None
	for entry in files:
		_validate_entry(entry)
		key = entry["path"].encode("utf-8")
		if previous is not None and key <= previous:
			raise VerifyError("schema_invalid")
		previous = key
	return files


def load_paths_file(path: str) -> list[str]:
	data = _read_capped(path, PATHS_FILE_MAX_BYTES, "paths_file_unreadable")
	if len(data) > PATHS_FILE_MAX_BYTES:
		raise VerifyError("paths_file_unreadable")
	try:
		text = data.decode("utf-8")
	except UnicodeDecodeError as exc:
		raise VerifyError("paths_file_unreadable") from exc
	return [line for line in text.split("\n") if line != ""]


def check_listed(paths: list[str], manifest_paths: set[str]) -> None:
	for path in paths:
		if path not in manifest_paths:
			raise VerifyError("unlisted_path", path)


def _verify_symlink(target: Path, entry: dict, st: os.stat_result) -> None:
	path = entry["path"]
	if not stat.S_ISLNK(st.st_mode):
		raise VerifyError("symlink_mismatch", path)
	try:
		link_text = os.readlink(target)
	except OSError as exc:
		raise VerifyError("read_failed", path) from exc
	if link_text != entry["link_target"]:
		raise VerifyError("symlink_mismatch", path)
	link_bytes = link_text.encode("utf-8", "surrogateescape")
	if len(link_bytes) != entry["size"]:
		raise VerifyError("size_mismatch", path)
	if hashlib.sha256(link_bytes).hexdigest() != entry["sha256"]:
		raise VerifyError("hash_mismatch", path)


def _verify_regular(target: Path, entry: dict, st: os.stat_result) -> None:
	path = entry["path"]
	if stat.S_ISLNK(st.st_mode):
		raise VerifyError("symlink_mismatch", path)
	if not stat.S_ISREG(st.st_mode):
		raise VerifyError("non_regular_file", path)
	flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
	try:
		fd = os.open(target, flags)
	except OSError as exc:
		if exc.errno == errno.ELOOP:
			raise VerifyError("symlink_mismatch", path) from exc
		raise VerifyError("read_failed", path) from exc
	try:
		with os.fdopen(fd, "rb") as handle:
			fst = os.fstat(handle.fileno())
			if not stat.S_ISREG(fst.st_mode):
				raise VerifyError("non_regular_file", path)
			# Same rule as release_manifest._entry_for: owner exec bit only.
			mode = "100755" if fst.st_mode & stat.S_IXUSR else "100644"
			if mode != entry["mode"]:
				raise VerifyError("mode_mismatch", path)
			if fst.st_size != entry["size"]:
				raise VerifyError("size_mismatch", path)
			digest = hashlib.sha256()
			total = 0
			while True:
				chunk = handle.read(HASH_CHUNK_BYTES)
				if not chunk:
					break
				total += len(chunk)
				digest.update(chunk)
	except OSError as exc:
		raise VerifyError("read_failed", path) from exc
	if total != entry["size"]:
		raise VerifyError("size_mismatch", path)
	if digest.hexdigest() != entry["sha256"]:
		raise VerifyError("hash_mismatch", path)


def verify_entry(root: Path, entry: dict) -> None:
	path = entry["path"]
	try:
		target = _rm._safe_rel(root, path)
	except _rm.ManifestError as exc:
		raise VerifyError(_SAFE_REL_REASONS.get(exc.reason, "internal_error"), path) from exc
	try:
		st = os.lstat(target)
	except FileNotFoundError as exc:
		raise VerifyError("missing_file", path) from exc
	except OSError as exc:
		raise VerifyError("read_failed", path) from exc
	if entry["mode"] == SYMLINK_MODE:
		_verify_symlink(target, entry, st)
	else:
		_verify_regular(target, entry, st)


def verify(args: argparse.Namespace, state: dict) -> int:
	try:
		expected_sha = _wrr.validate_release_sha(args.expected_sha)
	except ValueError as exc:
		raise VerifyError("invalid_argument") from exc
	expected_repo = args.expected_repository
	if expected_repo is None:
		expected_repo = _rm.RELEASE_MANIFEST_REPOSITORY
	root = Path(args.release_root)
	try:
		root_st = os.stat(root)
	except OSError as exc:
		raise VerifyError("invalid_argument") from exc
	if not stat.S_ISDIR(root_st.st_mode):
		raise VerifyError("invalid_argument")
	doc = load_manifest(args.manifest)
	entries = validate_manifest(doc, expected_sha, expected_repo)
	state["count"] = len(entries)
	if args.paths_file is not None:
		check_listed(load_paths_file(args.paths_file), {entry["path"] for entry in entries})
	for entry in entries:
		verify_entry(root, entry)
	return len(entries)


# ── verify-pr-tree (issue #7004) ──────────────────────────────────────────


def _git_env() -> dict:
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	env.update(
		{
			"GIT_CONFIG_NOSYSTEM": "1",
			"GIT_CONFIG_GLOBAL": os.devnull,
			"GIT_TERMINAL_PROMPT": "0",
			"GIT_OPTIONAL_LOCKS": "0",
			"LC_ALL": "C",
		}
	)
	return env


def _git(args: list[str]) -> bytes:
	cmd = [
		"git",
		"-c",
		"core.hooksPath=" + os.devnull,
		"-c",
		"core.fsmonitor=false",
		"-c",
		"core.untrackedCache=false",
		"-c",
		"protocol.allow=never",
		*args,
	]
	try:
		result = subprocess.run(
			cmd,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			env=_git_env(),
			timeout=GIT_TIMEOUT_SECONDS,
			check=False,
		)
	except (OSError, subprocess.SubprocessError) as exc:
		raise VerifyError("git_failed") from exc
	if result.returncode != 0:
		raise VerifyError("git_failed")
	return result.stdout


def _decode_path(raw: bytes) -> str:
	return raw.decode("utf-8", "surrogateescape")


def _changed_entries(git_dir: str, base: str, head: str) -> list[dict]:
	out = _git(["--git-dir", git_dir, "diff-tree", "-r", "-z", "--no-renames", "--raw", "--no-commit-id", base, head])
	parts = out.split(b"\0")
	if parts and parts[-1] == b"":
		parts.pop()
	if len(parts) % 2:
		raise VerifyError("git_failed")
	changes = []
	for index in range(0, len(parts), 2):
		try:
			meta = parts[index].decode("ascii")
		except UnicodeDecodeError as exc:
			raise VerifyError("git_failed") from exc
		fields = meta[1:].split(" ") if meta.startswith(":") else []
		if len(fields) != 5:
			raise VerifyError("git_failed")
		old_mode, new_mode, old_oid, new_oid, status = fields
		changes.append(
			{
				"path": _decode_path(parts[index + 1]),
				"old_mode": old_mode,
				"new_mode": new_mode,
				"old_oid": old_oid,
				"new_oid": new_oid,
				"status": status[:1],
			}
		)
		if len(changes) > MAX_PR_CHANGES:
			raise VerifyError("too_many_changes")
	return changes


def _blob_size(git_dir: str, oid: str) -> int:
	if not OBJECT_ID_RE.fullmatch(oid):
		raise VerifyError("git_failed")
	text = _git(["--git-dir", git_dir, "cat-file", "-s", oid]).strip()
	if not text.isdigit():
		raise VerifyError("git_failed")
	return int(text)


def _blob_bytes(git_dir: str, oid: str, path: str, mismatch_reason: str) -> bytes:
	if _blob_size(git_dir, oid) > MAX_BLOB_BYTES:
		raise VerifyError(mismatch_reason, path)
	return _git(["--git-dir", git_dir, "cat-file", "blob", oid])


def _reproduced_changes(reproduced_root: str) -> dict:
	"""Map each path the attested scripts changed to its blob id (None = deleted)."""
	out = _git(["-C", reproduced_root, "status", "--porcelain=v1", "-z", "-uall", "--no-renames", "--ignore-submodules=none"])
	changed: dict = {}
	for record in out.split(b"\0"):
		if not record:
			continue
		if len(record) < 4 or record[2:3] != b" ":
			raise VerifyError("git_failed")
		path = _decode_path(record[3:])
		segments = path.split("/")
		if any(segment in ("", ".", "..") for segment in segments) or path.startswith(".git/"):
			raise VerifyError("derived_mismatch", path)
		target = os.path.join(reproduced_root, *path.split("/"))
		try:
			st = os.lstat(target)
		except FileNotFoundError:
			changed[path] = None
			continue
		except OSError as exc:
			raise VerifyError("read_failed", path) from exc
		if not stat.S_ISREG(st.st_mode):
			raise VerifyError("derived_mismatch", path)
		oid = _git(["-C", reproduced_root, "hash-object", "--path=" + path, "--", target]).strip().decode("ascii", "replace")
		if not OBJECT_ID_RE.fullmatch(oid):
			raise VerifyError("git_failed")
		changed[path] = oid
	return changed


def _read_release_file(root: Path, rel: str) -> bytes:
	try:
		target = _rm._safe_rel(root, rel)
	except _rm.ManifestError as exc:
		raise VerifyError(_SAFE_REL_REASONS.get(exc.reason, "internal_error"), rel) from exc
	data = _read_capped(str(target), MAX_BLOB_BYTES, "read_failed")
	if len(data) > MAX_BLOB_BYTES:
		raise VerifyError("read_failed", rel)
	return data


def _expected_wrapper_bytes(root: Path, template_rel: str, release_sha: str, path: str) -> bytes:
	raw = _read_release_file(root, template_rel)
	try:
		# Path.read_text() in workflow_wrapper_refs applies universal newlines.
		text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
		return _wrr.pin_reusable_workflow_refs(text, release_sha).encode("utf-8")
	except (UnicodeDecodeError, ValueError) as exc:
		raise VerifyError("content_mismatch", path) from exc


def _retired_hashes(root: Path) -> dict:
	"""Parse retired_files.txt the way the updater's shell loop does."""
	try:
		text = _read_release_file(root, RETIRED_FILES_PATH).decode("utf-8")
	except UnicodeDecodeError as exc:
		raise VerifyError("read_failed", RETIRED_FILES_PATH) from exc
	retired: dict = {}
	for line in text.split("\n"):
		fields = line.split()
		if not fields or fields[0].startswith("#"):
			continue
		retired_path = fields[0]
		if retired_path.startswith("/") or ".." in retired_path or retired_path.startswith(".git/"):
			continue
		retired.setdefault(retired_path, set()).update(fields[1:])
	return retired


def _manifest_regular_entry(index: dict, rel: str):
	entry = index.get(rel)
	if entry is None or entry["mode"] not in FILE_MODES:
		return None
	return entry


def _check_hash_entry(git_dir: str, change: dict, entry: dict) -> None:
	path = change["path"]
	if _blob_size(git_dir, change["new_oid"]) != entry["size"]:
		raise VerifyError("content_mismatch", path)
	data = _blob_bytes(git_dir, change["new_oid"], path, "content_mismatch")
	if hashlib.sha256(data).hexdigest() != entry["sha256"]:
		raise VerifyError("content_mismatch", path)


def _check_mode(change: dict, entry: dict) -> None:
	"""Reject a PR that sets a mode the attested release does not record.

	An added path, or a modified path whose mode the PR changes, must carry
	the manifest entry's mode. A modified path that keeps its existing mode
	is accepted: the updater's `cp` onto an existing file keeps the consumer's
	mode, so only modes the PR itself introduces are checked.
	"""
	if change["status"] == "A" or change["old_mode"] != change["new_mode"]:
		if change["new_mode"] != entry["mode"]:
			raise VerifyError("mode_mismatch", change["path"])


def _check_change(git_dir: str, root: Path, release_sha: str, index: dict, retired, change: dict) -> None:
	path = change["path"]
	status = change["status"]
	if status == "D":
		if retired is None:
			retired = _retired_hashes(root)
		hashes = retired.get(path)
		if hashes is None:
			raise VerifyError("unlisted_path", path)
		data = _blob_bytes(git_dir, change["old_oid"], path, "retired_hash_mismatch")
		if hashlib.sha256(data).hexdigest() not in hashes:
			raise VerifyError("retired_hash_mismatch", path)
		return
	wrapper = WRAPPER_PATH_RE.fullmatch(path)
	if wrapper:
		template_rel = "workflow-templates/" + wrapper.group(1)
		template_entry = _manifest_regular_entry(index, template_rel)
		if template_entry is None:
			raise VerifyError("unlisted_path", path)
		_check_mode(change, template_entry)
		expected = _expected_wrapper_bytes(root, template_rel, release_sha, path)
		if _blob_size(git_dir, change["new_oid"]) != len(expected):
			raise VerifyError("content_mismatch", path)
		if _blob_bytes(git_dir, change["new_oid"], path, "content_mismatch") != expected:
			raise VerifyError("content_mismatch", path)
		return
	if path.startswith(CLAUDE_DIR_PREFIX):
		entry = _manifest_regular_entry(index, CLAUDE_TEMPLATE_PREFIX + path[len(CLAUDE_DIR_PREFIX):])
	elif path == "CLAUDE.md":
		entry = _manifest_regular_entry(index, "CLAUDE.md")
	else:
		entry = None
	if entry is None:
		raise VerifyError("unlisted_path", path)
	_check_mode(change, entry)
	_check_hash_entry(git_dir, change, entry)


def verify_pr_tree(args: argparse.Namespace, state: dict) -> int:
	try:
		expected_sha = _wrr.validate_release_sha(args.expected_sha)
	except ValueError as exc:
		raise VerifyError("invalid_argument") from exc
	base = args.base
	head = args.head
	if not OBJECT_ID_RE.fullmatch(base) or not OBJECT_ID_RE.fullmatch(head):
		raise VerifyError("invalid_argument")
	expected_repo = args.expected_repository
	if expected_repo is None:
		expected_repo = _rm.RELEASE_MANIFEST_REPOSITORY
	root = Path(args.release_root)
	for directory in (args.release_root, args.git_dir, args.reproduced_root):
		try:
			dir_st = os.stat(directory)
		except OSError as exc:
			raise VerifyError("invalid_argument") from exc
		if not stat.S_ISDIR(dir_st.st_mode):
			raise VerifyError("invalid_argument")
	doc = load_manifest(args.manifest)
	entries = validate_manifest(doc, expected_sha, expected_repo)
	for entry in entries:
		verify_entry(root, entry)
	index = {entry["path"]: entry for entry in entries}
	changes = _changed_entries(args.git_dir, base, head)
	state["count"] = len(changes)
	derived = _reproduced_changes(args.reproduced_root)
	retired = None
	changed_paths = set()
	for change in changes:
		path = change["path"]
		changed_paths.add(path)
		if change["status"] != "D" and change["new_mode"] not in FILE_MODES:
			raise VerifyError("mode_mismatch", path)
		if path in derived:
			head_oid = None if change["status"] == "D" else change["new_oid"]
			if head_oid != derived[path]:
				raise VerifyError("derived_mismatch", path)
			continue
		if change["status"] == "D" and retired is None:
			retired = _retired_hashes(root)
		_check_change(args.git_dir, root, expected_sha, index, retired, change)
	for path in sorted(derived):
		if path not in changed_paths:
			raise VerifyError("derived_missing", path)
	return len(changes)


def _parser() -> _ArgParser:
	parser = _ArgParser(description="Verify a release tree against its release manifest.", add_help=False)
	subparsers = parser.add_subparsers(dest="command", required=True, parser_class=_ArgParser)
	cmd = subparsers.add_parser("verify", add_help=False)
	cmd.add_argument("--manifest", required=True)
	cmd.add_argument("--release-root", required=True)
	cmd.add_argument("--expected-sha", required=True)
	cmd.add_argument("--expected-repository", default=None)
	cmd.add_argument("--paths-file", default=None)
	tree = subparsers.add_parser("verify-pr-tree", add_help=False)
	tree.add_argument("--manifest", required=True)
	tree.add_argument("--release-root", required=True)
	tree.add_argument("--expected-sha", required=True)
	tree.add_argument("--expected-repository", default=None)
	tree.add_argument("--git-dir", required=True)
	tree.add_argument("--base", required=True)
	tree.add_argument("--head", required=True)
	tree.add_argument("--reproduced-root", required=True)
	return parser


def main(argv: list[str] | None = None) -> int:
	state = {"count": 0}
	try:
		_load_deps()
		args = _parser().parse_args(argv)
		if args.command == "verify":
			count = verify(args, state)
		elif args.command == "verify-pr-tree":
			count = verify_pr_tree(args, state)
		else:
			raise VerifyError("invalid_argument")
	except VerifyError as exc:
		emit("rejected", exc.reason, state["count"], exc.path)
		return 2
	except Exception:  # noqa: BLE001 - fail closed on anything unexpected
		emit("rejected", "internal_error", state["count"])
		return 2
	emit("ok", "ok", count)
	return 0


if __name__ == "__main__":
	sys.exit(main())
