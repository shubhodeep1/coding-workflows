#!/usr/bin/env python3
"""Build and check the signed consumer sync manifest (issue #7128).

The release jobs of ``test-and-mark-stable.yml`` and ``mark-stable.yml`` run
``build`` on the final release commit, attest the result with
``actions/attest-build-provenance`` and upload it to the GitHub Release as
``sync-manifest.json``. Consumer ``update_workflows.yml`` runs verify the
attestation and the file hashes before any fetched upstream code runs, and
check every staged path against ``allowed_consumer_paths`` before commit.

Subcommands:

``build --repo-root R --commit SHA --version vX.Y.Z --out FILE``
    Writes the canonical manifest. ``files`` lists every entry the
    updater's cone-mode sparse checkout contains (root-level entries plus
    everything under ``workflow-templates/`` and ``scripts/``), read from git
    objects at ``SHA`` so the working tree never affects the result. Regular
    files map to the sha256 of their blob, symlinks to ``symlink:<target>``.
    Any other entry type (a submodule) exits 1.

``allowed-paths --repo-root R [--commit REV]``
    Prints the consumer path allowlist, one entry per line.

``managed-paths --repo-root R [--commit REV]``
    Prints the subset of the allowlist that the updater owns outright (a
    consumer's copy is overwritten on the next sync): the wrapper workflows,
    ``.claude/`` assets, ``CLAUDE.md``, the audit-gate ``managed_files``
    targets and retired files. Consumer-edited entries (``CHANGELOG.md``,
    ``agents.md``, ``package.json`` and the like) are excluded. The security
    audit uses it to keep findings on these paths out of the consumer's
    fix-up queue (a consumer cannot fix them).

``check-paths --allowed-file A --paths-file P``
    Exits 0 when every newline-separated path in ``P`` matches an allowlist
    entry, otherwise prints the first offending path and exits 3. In an
    entry ``*`` matches any run of characters except ``/``; every other
    character is literal.

Exit codes: 0 ok, 1 unreadable or unsupported input, 2 bad arguments,
3 path not allowed. Log prefix: ``SYNC_MANIFEST``. Standard library only;
no network and no GitHub API calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = "coding-workflows.sync-manifest.v1"
REPOSITORY = "shubhodeep1/coding-workflows"
CONE_DIRS = ("workflow-templates/", "scripts/")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# Consumer paths the updater writes that do not depend on the release tree.
STATIC_ALLOWED_PATHS = (
	"CLAUDE.md",
	"package.json",
	"changelog.d/.gitkeep",
	"changelog.d/*.md",
	"CHANGELOG.md",
	# assemble_changelog.py ensure-assets edits the managed .gitattributes block.
	".gitattributes",
	"agents.md",
	"AGENTS.md",
	"agents.d/*.md",
	"scripts/.gitignore",
)


# Static allowlist entries the updater overwrites wholesale. Every other
# STATIC_ALLOWED_PATHS entry is edited in place and stays consumer-owned.
STATIC_UPSTREAM_MANAGED_PATHS = (
	"CLAUDE.md",
)


class ManifestError(Exception):
	"""Unreadable or unsupported input (exit 1)."""


def log(message: str) -> None:
	print(f"SYNC_MANIFEST {message}", file=sys.stderr)


def glob_to_regex(pattern: str) -> "re.Pattern[str]":
	"""Compile an allowlist entry: ``*`` matches ``[^/]*``, all else is literal."""
	parts = [("[^/]*" if char == "*" else re.escape(char)) for char in pattern]
	return re.compile("^" + "".join(parts) + "$")


def path_allowed(path: str, compiled: list) -> bool:
	return any(regex.match(path) for regex in compiled)


def _git(repo_root: Path, *args: str) -> bytes:
	try:
		result = subprocess.run(
			["git", "-C", str(repo_root), *args],
			check=False,
			capture_output=True,
		)
	except OSError as exc:
		raise ManifestError(f"git unavailable: {exc}") from exc
	if result.returncode != 0:
		raise ManifestError(f"git {args[0]} failed")
	return result.stdout


def resolve_commit(repo_root: Path, rev: str) -> str:
	return _git(repo_root, "rev-parse", "--verify", f"{rev}^{{commit}}").decode("ascii").strip()


def list_tree(repo_root: Path, commit: str) -> list:
	"""Return ``(mode, type, object_id, path)`` for every tree entry."""
	raw = _git(repo_root, "ls-tree", "-r", "-z", commit)
	entries = []
	for record in raw.split(b"\0"):
		if not record:
			continue
		meta, _, path_bytes = record.partition(b"\t")
		fields = meta.decode("ascii").split()
		if len(fields) != 3:
			raise ManifestError("unparseable ls-tree entry")
		try:
			path = path_bytes.decode("utf-8")
		except UnicodeDecodeError as exc:
			raise ManifestError("non-UTF-8 path in tree") from exc
		entries.append((fields[0], fields[1], fields[2], path))
	return entries


def read_blobs(repo_root: Path, object_ids: list) -> dict:
	"""Read blob contents with one ``git cat-file --batch`` process."""
	if not object_ids:
		return {}
	unique = sorted(set(object_ids))
	payload = ("\n".join(unique) + "\n").encode("ascii")
	try:
		result = subprocess.run(
			["git", "-C", str(repo_root), "cat-file", "--batch"],
			input=payload,
			check=False,
			capture_output=True,
		)
	except OSError as exc:
		raise ManifestError(f"git unavailable: {exc}") from exc
	if result.returncode != 0:
		raise ManifestError("git cat-file failed")
	out = result.stdout
	blobs = {}
	offset = 0
	for object_id in unique:
		header_end = out.index(b"\n", offset)
		header = out[offset:header_end].decode("ascii").split()
		if len(header) != 3 or header[0] != object_id or header[1] != "blob":
			raise ManifestError(f"unexpected cat-file header for {object_id}")
		size = int(header[2])
		start = header_end + 1
		blobs[object_id] = out[start:start + size]
		offset = start + size + 1
	return blobs


def in_cone(path: str) -> bool:
	return "/" not in path or path.startswith(CONE_DIRS)


def build_files(repo_root: Path, commit: str) -> dict:
	entries = [entry for entry in list_tree(repo_root, commit) if in_cone(entry[3])]
	for mode, _kind, _oid, path in entries:
		if mode not in ("100644", "100755", "120000"):
			raise ManifestError(f"unsupported tree entry mode={mode} path={path}")
	blobs = read_blobs(repo_root, [oid for _m, _k, oid, _p in entries])
	files = {}
	for mode, _kind, oid, path in entries:
		data = blobs[oid]
		if mode == "120000":
			try:
				files[path] = "symlink:" + data.decode("utf-8")
			except UnicodeDecodeError as exc:
				raise ManifestError(f"non-UTF-8 symlink target path={path}") from exc
		else:
			files[path] = hashlib.sha256(data).hexdigest()
	return files


def derive_allowed_paths(repo_root: Path, commit: str = "HEAD", include_consumer_owned: bool = True) -> list:
	"""Consumer paths the updater may stage for the release tree at ``commit``.

	``include_consumer_owned=False`` keeps only the paths the updater owns
	outright (see ``derive_managed_paths``).
	"""
	entries = list_tree(repo_root, commit)
	by_path = {path: (mode, oid) for mode, _kind, oid, path in entries}
	allowed = set(STATIC_ALLOWED_PATHS if include_consumer_owned else STATIC_UPSTREAM_MANAGED_PATHS)
	for path in by_path:
		if path.startswith("workflow-templates/") and path.count("/") == 1 and path.endswith(".yml"):
			allowed.add(".github/workflows/" + path.split("/", 1)[1])
		elif path.startswith("workflow-templates/.claude/"):
			allowed.add(".claude/" + path[len("workflow-templates/.claude/"):])
	wanted = {}
	for name in ("workflow-templates/audit-gate/contract.json", "workflow-templates/retired_files.txt"):
		if name in by_path and by_path[name][0] != "120000":
			wanted[name] = by_path[name][1]
	blobs = read_blobs(repo_root, list(wanted.values()))
	contract_oid = wanted.get("workflow-templates/audit-gate/contract.json")
	if contract_oid:
		try:
			contract = json.loads(blobs[contract_oid].decode("utf-8"))
		except (UnicodeDecodeError, json.JSONDecodeError) as exc:
			raise ManifestError("audit-gate contract.json is not valid JSON") from exc
		for item in contract.get("managed_files", []) or []:
			target = item.get("target") if isinstance(item, dict) else None
			if isinstance(target, str) and target:
				allowed.add(target)
	retired_oid = wanted.get("workflow-templates/retired_files.txt")
	if retired_oid:
		for line in blobs[retired_oid].decode("utf-8", "replace").splitlines():
			tokens = line.split()
			if not tokens or tokens[0].startswith("#"):
				continue
			allowed.add(tokens[0])
	return sorted(allowed)


def derive_managed_paths(repo_root: Path, commit: str = "HEAD") -> list:
	"""Allowlist entries the updater owns: a consumer edit is overwritten."""
	return derive_allowed_paths(repo_root, commit, include_consumer_owned=False)


def render_manifest(repo_root: Path, commit: str, version: str) -> str:
	manifest = {
		"schema": SCHEMA,
		"repository": REPOSITORY,
		"commit": commit,
		"version": version,
		"files": build_files(repo_root, commit),
		"allowed_consumer_paths": derive_allowed_paths(repo_root, commit),
	}
	return json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def cmd_build(args: argparse.Namespace) -> int:
	repo_root = Path(args.repo_root)
	if not COMMIT_RE.match(args.commit or ""):
		log("outcome=error reason=invalid_commit")
		return 2
	if not VERSION_RE.match(args.version or ""):
		log("outcome=error reason=invalid_version")
		return 2
	if resolve_commit(repo_root, args.commit) != args.commit:
		log("outcome=error reason=commit_mismatch")
		return 1
	text = render_manifest(repo_root, args.commit, args.version)
	Path(args.out).write_text(text, encoding="utf-8")
	payload = json.loads(text)
	log(
		f"outcome=built files={len(payload['files'])} "
		f"allowed={len(payload['allowed_consumer_paths'])} commit={args.commit}"
	)
	return 0


def cmd_allowed_paths(args: argparse.Namespace) -> int:
	repo_root = Path(args.repo_root)
	commit = resolve_commit(repo_root, args.commit)
	for entry in derive_allowed_paths(repo_root, commit):
		print(entry)
	return 0


def cmd_managed_paths(args: argparse.Namespace) -> int:
	repo_root = Path(args.repo_root)
	commit = resolve_commit(repo_root, args.commit)
	for entry in derive_managed_paths(repo_root, commit):
		print(entry)
	return 0


def cmd_check_paths(args: argparse.Namespace) -> int:
	try:
		# "\n" only: splitlines() would also split on U+2028/U+2029 and
		# turn one PR filename into two allowlisted paths.
		allowed = Path(args.allowed_file).read_text(encoding="utf-8").split("\n")
		paths = Path(args.paths_file).read_text(encoding="utf-8").split("\n")
	except (OSError, UnicodeDecodeError) as exc:
		raise ManifestError("unreadable allowlist or path file") from exc
	compiled = [glob_to_regex(entry) for entry in allowed if entry]
	if not compiled:
		raise ManifestError("empty allowlist")
	for path in paths:
		if not path:
			continue
		if not path_allowed(path, compiled):
			print(path)
			return 3
	return 0


class _Parser(argparse.ArgumentParser):
	def error(self, message: str) -> None:  # pragma: no cover - argparse hook
		self.print_usage(sys.stderr)
		print(f"SYNC_MANIFEST outcome=error reason=bad_arguments detail={message}", file=sys.stderr)
		raise SystemExit(2)


def main(argv: list | None = None) -> int:
	parser = _Parser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", parser_class=_Parser)
	build = sub.add_parser("build")
	build.add_argument("--repo-root", required=True)
	build.add_argument("--commit", required=True)
	build.add_argument("--version", required=True)
	build.add_argument("--out", required=True)
	allowed = sub.add_parser("allowed-paths")
	allowed.add_argument("--repo-root", required=True)
	allowed.add_argument("--commit", default="HEAD")
	managed = sub.add_parser("managed-paths")
	managed.add_argument("--repo-root", required=True)
	managed.add_argument("--commit", default="HEAD")
	check = sub.add_parser("check-paths")
	check.add_argument("--allowed-file", required=True)
	check.add_argument("--paths-file", required=True)
	args = parser.parse_args(argv)
	handlers = {"build": cmd_build, "allowed-paths": cmd_allowed_paths, "managed-paths": cmd_managed_paths, "check-paths": cmd_check_paths}
	if args.command not in handlers:
		parser.print_usage(sys.stderr)
		return 2
	try:
		return handlers[args.command](args)
	except ManifestError as exc:
		log(f"outcome=error reason=unsupported_input detail={exc}")
		return 1


if __name__ == "__main__":
	sys.exit(main())
