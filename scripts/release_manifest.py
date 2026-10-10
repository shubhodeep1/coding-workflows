#!/usr/bin/env python3
"""Build the deterministic release manifest of files the consumer updater copies.

The manifest lists every file `.github/workflows/update_workflows.yml` reads
from a coding-workflows release checkout, with its sha256, byte size and git
file mode, so a later phase can attest the manifest and the updater can verify
it. Nothing calls this builder yet; production behaviour is unchanged.

Surface (one rule per updater step, see SURFACE_RULES):
- "Prepare immutable workflow wrappers" / "Compare and update workflow
  wrappers": top-level workflow-templates/*.yml, the ai-update-workflows.yml
  sentinel, workflow-templates/profiles/*.txt, scripts/workflow_wrapper_refs.py.
- "Apply canonical audit-gate assets": workflow-templates/audit-gate/**
  (contract.json is required: the updater runs the applier unconditionally
  and it fails without the contract), scripts/apply_audit_gate_assets.py.
- "Sync .claude/ assets from upstream": workflow-templates/.claude/**.
- "Remove retired upstream files": workflow-templates/retired_files.txt.
- "Sync top-level CLAUDE.md from upstream": the workflow-templates/CLAUDE.md
  symlink and the root CLAUDE.md it points at.
- "Sync changelog fragment assets from upstream" / "Assemble changelog
  fragments": scripts/assemble_changelog.py (it generates the changelog
  assets, so the script is the attested asset).
- "Verify attested release manifest": scripts/verify_release_manifest.py and
  scripts/release_manifest.py (the step hash-checks both, plus
  scripts/workflow_wrapper_refs.py, before running the verifier).
- "Commit and push updates" (pull-request path, #6989):
  scripts/lint_pr_body_auto_close.py checks the composed PR body.
- Not attested: scripts/tg_helpers.sh. The "Send Telegram notification" step
  fetches it through the contents API at ref=stable, not from the release
  checkout, so it is outside the release-to-consumer copy surface.

Assumption: the authoritative plan
(docs/plans/safeguarded-consumer-updater-plan.md on branch
claude/elegant-mendel-5pli2i-safeguarded-updater-plan) could not be read from
the isolated implementation sandbox, so this module uses the provisional names
and behaviour from issue #6903. If the plan names different identifiers, the
plan wins and a follow-up should align them.

Usage:
	release_manifest.py build --repo-root <dir> --release-sha <40-hex>
		--tag <vX.Y.Z> --output <path>

Exit 0 on success (stdout `RELEASE_MANIFEST outcome=written files=<n>`);
exit 2 on any refusal (stderr `RELEASE_MANIFEST error reason=<token>`, no
path echoed, output file not written).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workflow_wrapper_refs import validate_release_sha  # noqa: E402

SCHEMA_VERSION = "release_manifest.v1"
RELEASE_MANIFEST_REPOSITORY = "shubhodeep1/coding-workflows"
TAG_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
KNOWN_SYMLINKS = {"workflow-templates/CLAUDE.md": "../CLAUDE.md"}
HASH_CHUNK_BYTES = 64 * 1024

# (kind, pattern, required). Kinds: glob (one directory level), file, tree
# (recursive regular files), symlink (must be in KNOWN_SYMLINKS).
SURFACE_RULES = (
	("glob", "workflow-templates/*.yml", True),
	("file", "workflow-templates/ai-update-workflows.yml", True),
	("glob", "workflow-templates/profiles/*.txt", False),
	("file", "workflow-templates/retired_files.txt", True),
	("tree", "workflow-templates/.claude", False),
	("tree", "workflow-templates/audit-gate", False),
	("file", "workflow-templates/audit-gate/contract.json", True),
	("symlink", "workflow-templates/CLAUDE.md", True),
	("file", "CLAUDE.md", True),
	("file", "scripts/workflow_wrapper_refs.py", True),
	("file", "scripts/apply_audit_gate_assets.py", True),
	("file", "scripts/assemble_changelog.py", True),
	# The updater's verification step (#6960) runs these two modules from the
	# release checkout, so their bytes must be attested too.
	("file", "scripts/verify_release_manifest.py", True),
	("file", "scripts/release_manifest.py", True),
	# "Commit and push updates" runs the auto-close lint from the release
	# checkout on its pull-request path (#6989).
	("file", "scripts/lint_pr_body_auto_close.py", True),
)

# Directories (and the root file) the output must never be written into, so a
# build cannot poison the surface of the next build.
OUTPUT_FORBIDDEN_DIRS = ("workflow-templates", "scripts")
OUTPUT_FORBIDDEN_FILES = ("CLAUDE.md",)


class ManifestError(Exception):
	"""Refusal with a fixed, path-free reason token."""

	def __init__(self, reason: str):
		super().__init__(reason)
		self.reason = reason


def _check_segments(rel: str) -> list[str]:
	segments = rel.split("/")
	for segment in segments:
		if not PATH_SEGMENT_RE.fullmatch(segment) or segment in (".", ".."):
			raise ManifestError("unsafe_path_name")
	return segments


def _is_within(path: str, root: str) -> bool:
	return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _safe_rel(root: Path, rel: str) -> Path:
	"""Return root/rel after refusing unsafe names and symlinked parents."""
	segments = _check_segments(rel)
	current = root
	for segment in segments[:-1]:
		current = current / segment
		try:
			st = os.lstat(current)
		except FileNotFoundError:
			return root.joinpath(*segments)
		except OSError as exc:
			raise ManifestError("read_failed") from exc
		if stat.S_ISLNK(st.st_mode):
			raise ManifestError("unexpected_symlink")
		if not stat.S_ISDIR(st.st_mode):
			raise ManifestError("non_regular_file")
	target = root.joinpath(*segments)
	root_real = os.path.realpath(root)
	parent_real = os.path.realpath(target.parent)
	if not _is_within(parent_real, root_real):
		raise ManifestError("path_escape")
	return target


def _sha256_file(path: Path) -> str:
	digest = hashlib.sha256()
	# O_NOFOLLOW guards the race between lstat and open.
	flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
	try:
		fd = os.open(path, flags)
	except OSError as exc:
		raise ManifestError("read_failed") from exc
	with os.fdopen(fd, "rb") as handle:
		if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
			raise ManifestError("non_regular_file")
		while True:
			chunk = handle.read(HASH_CHUNK_BYTES)
			if not chunk:
				break
			digest.update(chunk)
	return digest.hexdigest()


def _entry_for(root: Path, rel: str) -> dict:
	path = _safe_rel(root, rel)
	try:
		st = os.lstat(path)
	except FileNotFoundError as exc:
		raise ManifestError("required_path_missing") from exc
	except OSError as exc:
		raise ManifestError("read_failed") from exc
	if stat.S_ISLNK(st.st_mode):
		expected = KNOWN_SYMLINKS.get(rel)
		if expected is None:
			raise ManifestError("unexpected_symlink")
		try:
			link_text = os.readlink(path)
		except OSError as exc:
			raise ManifestError("read_failed") from exc
		if link_text != expected:
			raise ManifestError("unexpected_symlink_target")
		resolved = os.path.realpath(path)
		if not _is_within(resolved, os.path.realpath(root)):
			raise ManifestError("path_escape")
		try:
			target_st = os.lstat(resolved)
		except FileNotFoundError as exc:
			raise ManifestError("dangling_symlink") from exc
		except OSError as exc:
			raise ManifestError("read_failed") from exc
		if not stat.S_ISREG(target_st.st_mode):
			raise ManifestError("unexpected_symlink_target")
		link_bytes = link_text.encode("utf-8")
		return {
			"path": rel,
			"sha256": hashlib.sha256(link_bytes).hexdigest(),
			"size": len(link_bytes),
			"mode": "120000",
			"link_target": link_text,
		}
	if not stat.S_ISREG(st.st_mode):
		raise ManifestError("non_regular_file")
	return {
		"path": rel,
		"sha256": _sha256_file(path),
		"size": st.st_size,
		"mode": "100755" if st.st_mode & stat.S_IXUSR else "100644",
	}


def _dir_or_none(root: Path, rel_dir: str) -> Path | None:
	path = _safe_rel(root, rel_dir)
	try:
		st = os.lstat(path)
	except FileNotFoundError:
		return None
	except OSError as exc:
		raise ManifestError("read_failed") from exc
	if stat.S_ISLNK(st.st_mode):
		raise ManifestError("unexpected_symlink")
	if not stat.S_ISDIR(st.st_mode):
		raise ManifestError("non_regular_file")
	return path


def _walk_tree(root: Path, rel_dir: str) -> list[dict]:
	base = _dir_or_none(root, rel_dir)
	if base is None:
		return []
	entries: list[dict] = []

	def _on_error(exc: OSError) -> None:
		raise ManifestError("read_failed") from exc

	for dirpath, dirnames, filenames in os.walk(base, followlinks=False, onerror=_on_error):
		rel_parent = Path(dirpath).relative_to(root).as_posix()
		for dirname in sorted(dirnames):
			_check_segments(f"{rel_parent}/{dirname}")
			if os.path.islink(os.path.join(dirpath, dirname)):
				raise ManifestError("unexpected_symlink")
		dirnames.sort()
		for filename in sorted(filenames):
			entries.append(_entry_for(root, f"{rel_parent}/{filename}"))
	return entries


def _glob_level(root: Path, pattern: str) -> list[dict]:
	rel_dir, _, name_glob = pattern.rpartition("/")
	if not name_glob.startswith("*."):
		raise ManifestError("invalid_rule")
	suffix = name_glob[1:]
	base = _dir_or_none(root, rel_dir)
	if base is None:
		return []
	entries = []
	try:
		names = sorted(os.listdir(base))
	except OSError as exc:
		raise ManifestError("read_failed") from exc
	for name in names:
		if not name.endswith(suffix) or name.startswith("."):
			continue
		entries.append(_entry_for(root, f"{rel_dir}/{name}"))
	return entries


def _sort_key(entry: dict) -> bytes:
	return entry["path"].encode("utf-8")


def enumerate_surface(root: Path) -> list[dict]:
	"""Return the sorted, de-duplicated manifest entries for the release tree."""
	by_path: dict[str, dict] = {}
	for kind, pattern, required in SURFACE_RULES:
		if kind == "glob":
			found = _glob_level(root, pattern)
			if required and not found:
				raise ManifestError("no_workflow_templates")
		elif kind == "tree":
			found = _walk_tree(root, pattern)
		elif kind in ("file", "symlink"):
			try:
				entry = _entry_for(root, pattern)
			except ManifestError as exc:
				if exc.reason == "required_path_missing" and not required:
					continue
				raise
			if kind == "symlink" and entry["mode"] != "120000":
				raise ManifestError("expected_symlink")
			if kind == "file" and entry["mode"] == "120000":
				raise ManifestError("unexpected_symlink")
			found = [entry]
		else:
			raise ManifestError("invalid_rule")
		for entry in found:
			previous = by_path.get(entry["path"])
			if previous is not None and previous != entry:
				raise ManifestError("inconsistent_entry")
			by_path[entry["path"]] = entry
	return sorted(by_path.values(), key=_sort_key)


def build_manifest(root: Path, release_sha: str, tag: str) -> dict:
	try:
		normalized_sha = validate_release_sha(release_sha)
	except ValueError as exc:
		raise ManifestError("invalid_release_sha") from exc
	if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
		raise ManifestError("invalid_tag")
	try:
		root_st = os.stat(root)
	except OSError as exc:
		raise ManifestError("invalid_repo_root") from exc
	if not stat.S_ISDIR(root_st.st_mode):
		raise ManifestError("invalid_repo_root")
	return {
		"schema_version": SCHEMA_VERSION,
		"repository": RELEASE_MANIFEST_REPOSITORY,
		"release_sha": normalized_sha,
		"tag": tag,
		"files": enumerate_surface(root),
	}


def render_manifest(manifest: dict) -> bytes:
	text = json.dumps(manifest, sort_keys=True, indent="\t", separators=(",", ": "), ensure_ascii=True)
	return (text + "\n").encode("ascii")


def check_output_path(root: Path, output: Path) -> None:
	root_real = os.path.realpath(root)
	output_real = os.path.realpath(output)
	for rel_dir in OUTPUT_FORBIDDEN_DIRS:
		if _is_within(output_real, os.path.join(root_real, rel_dir)):
			raise ManifestError("output_inside_surface")
	for rel_file in OUTPUT_FORBIDDEN_FILES:
		if output_real == os.path.join(root_real, rel_file):
			raise ManifestError("output_inside_surface")


def write_atomic(output: Path, data: bytes) -> None:
	parent = output.parent if str(output.parent) else Path(".")
	tmp_name = None
	try:
		with tempfile.NamedTemporaryFile(dir=parent, prefix=".release_manifest.", delete=False) as handle:
			tmp_name = handle.name
			handle.write(data)
			handle.flush()
			os.fsync(handle.fileno())
		os.chmod(tmp_name, 0o644)
		os.replace(tmp_name, output)
		tmp_name = None
	except OSError as exc:
		raise ManifestError("write_failed") from exc
	finally:
		if tmp_name is not None:
			try:
				os.unlink(tmp_name)
			except OSError:
				pass


def _cmd_build(args: argparse.Namespace) -> int:
	root = Path(args.repo_root)
	output = Path(args.output)
	manifest = build_manifest(root, args.release_sha, args.tag)
	check_output_path(root, output)
	write_atomic(output, render_manifest(manifest))
	print(f"RELEASE_MANIFEST outcome=written files={len(manifest['files'])}")
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	subparsers = parser.add_subparsers(dest="command", required=True)
	build = subparsers.add_parser("build", help="Build the release manifest.")
	build.add_argument("--repo-root", required=True)
	build.add_argument("--release-sha", required=True)
	build.add_argument("--tag", required=True)
	build.add_argument("--output", required=True)
	args = parser.parse_args(argv)
	try:
		if args.command == "build":
			return _cmd_build(args)
	except ManifestError as exc:
		print(f"RELEASE_MANIFEST error reason={exc.reason}", file=sys.stderr)
		return 2
	parser.error("unknown command")
	return 2


if __name__ == "__main__":
	sys.exit(main())
