#!/usr/bin/env python3
"""Keep a consumer's Dependabot config off the sync-managed wrappers (issue #9485).

The ``@stable`` sync owns the managed wrapper files under
``.github/workflows/``; ``coding-workflows/workflow-integrity`` fails any pull
request that edits them. Dependabot ``github-actions`` bump PRs against those
files therefore fail by design and pile up. This helper, run by
``update_workflows.yml`` (step "Sync Dependabot exclusions for managed
wrappers") and again by its PR-delivery ``verify`` job on the PR's parent
tree, adds the managed set to ``exclude-paths`` of the consumer's own root
``github-actions`` update entries.

Rules:

- It edits ``.github/dependabot.yml`` (or ``.github/dependabot.yaml``) in
  place and never creates the file or a ``github-actions`` entry: without such
  an entry Dependabot opens no bump PRs for the wrappers.
- The managed set matches ``compare()`` in ``workflow_integrity.yml``: the
  entries of ``profiles/<profile>.txt``, ``ai-workflow-integrity.yml``, and
  ``ai-update-workflows.yml`` only when the consumer already has it.
- An ``exclude-paths`` item equal to ``.github/workflows/<name>`` where
  ``<name>`` is a top-level ``workflow-templates/*.yml`` basename is
  sync-owned and rebuilt on every run, so the list follows profile changes.
  Every other item, comment and key is kept byte for byte.
- The editor is line based and handles a restricted subset of block YAML. It
  refuses (no change, ``reason=unsupported_layout``) anything else: document
  markers, anchors/aliases, tabs, CR line endings, flow-style ``updates`` or
  ``exclude-paths``, block scalars inside an edited entry. It never imports a
  YAML library, so the update job and the verify job produce identical bytes.

CLI::

    sync_dependabot_exclusions.py --repo-root DIR --templates-dir DIR --profile NAME
        [--changed-paths-file PATH] [--dry-run]

Writes the repo-relative path of a changed file to ``--changed-paths-file``
(always created, empty when nothing changed) and
``dependabot_has_changes=true|false`` to ``$GITHUB_OUTPUT`` when set. Prints
one ``DEPENDABOT_EXCLUDE_SYNC outcome=<updated|unchanged|skipped>
reason=<token> file=<path|none> entries=<n> managed=<n>`` line. Exits 0 on
every fail-open path and 2 only for bad arguments. Standard library only; no
network, no subprocess, no GitHub API calls.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

LOG_PREFIX = "DEPENDABOT_EXCLUDE_SYNC"
CONFIG_CANDIDATES = (".github/dependabot.yml", ".github/dependabot.yaml")
MAX_CONFIG_BYTES = 256 * 1024
PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
WRAPPER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")
INTEGRITY_WRAPPER = "ai-workflow-integrity.yml"
UPDATER_WRAPPER = "ai-update-workflows.yml"
WORKFLOWS_PREFIX = ".github/workflows/"
KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):(?:[ ]+(.*))?$")
ITEM_RE = re.compile(r"^( *)-(?: (.*))?$")
# A value that opens a literal/folded block scalar ("key: |", "- >-").
BLOCK_SCALAR_RE = re.compile(r"(?:^|[\s:])[|>][1-9+-]{0,2}$")


class UnsupportedLayout(Exception):
	"""The config uses YAML this editor does not handle; leave it untouched."""


def log(outcome: str, reason: str, file_rel: str | None, entries: int, managed: int) -> None:
	print(f"{LOG_PREFIX} outcome={outcome} reason={reason} file={file_rel or 'none'} entries={entries} managed={managed}")


# ── Managed set ─────────────────────────────────────────────────────────


def template_basenames(templates_dir: Path) -> set:
	"""Basenames of the top-level ``workflow-templates/*.yml`` files."""
	names = set()
	for entry in templates_dir.glob("*.yml"):
		if WRAPPER_NAME_RE.match(entry.name):
			names.add(entry.name)
	return names


def load_managed_names(templates_dir: Path, profile: str, repo_root: Path):
	"""Return ``(sorted managed names, None)`` or ``(None, skip reason)``."""
	if not PROFILE_RE.match(profile or ""):
		return None, "profile_invalid"
	profile_file = templates_dir / "profiles" / f"{profile}.txt"
	try:
		if not profile_file.is_file() or profile_file.is_symlink():
			return None, "profile_unknown"
		lines = profile_file.read_text(encoding="utf-8").splitlines()
	except (OSError, UnicodeDecodeError):
		return None, "profile_unknown"
	managed = set()
	for raw in lines:
		entry = raw.strip()
		if not entry or entry.startswith("#"):
			continue
		if not WRAPPER_NAME_RE.match(entry) or not (templates_dir / entry).is_file():
			continue
		managed.add(entry)
	if not managed:
		return None, "profile_empty"
	managed.add(INTEGRITY_WRAPPER)
	# The updater wrapper is bootstrap-only (never created by the sync), so it
	# is managed only once the consumer has it, as in workflow_integrity.yml.
	if (repo_root / ".github" / "workflows" / UPDATER_WRAPPER).exists():
		managed.add(UPDATER_WRAPPER)
	else:
		managed.discard(UPDATER_WRAPPER)
	return sorted(managed), None


# ── Config discovery ────────────────────────────────────────────────────


def locate_config(repo_root: Path):
	"""Return ``(relative path, text, None)`` or ``(rel or None, None, reason)``."""
	github_dir = repo_root / ".github"
	try:
		github_st = os.lstat(github_dir)
	except FileNotFoundError:
		return None, None, "no_config"
	except OSError:
		return None, None, "read_failed"
	if not stat.S_ISDIR(github_st.st_mode):
		return None, None, "unsupported_layout"
	present = []
	for rel in CONFIG_CANDIDATES:
		try:
			present.append((rel, os.lstat(repo_root / rel)))
		except FileNotFoundError:
			continue
		except OSError:
			return rel, None, "read_failed"
	if not present:
		return None, None, "no_config"
	if len(present) > 1:
		return None, None, "ambiguous_config"
	rel, st = present[0]
	if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CONFIG_BYTES:
		return rel, None, "unsupported_layout"
	try:
		fd = os.open(repo_root / rel, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
		with os.fdopen(fd, "rb") as handle:
			data = handle.read(MAX_CONFIG_BYTES + 1)
	except OSError:
		return rel, None, "read_failed"
	if len(data) > MAX_CONFIG_BYTES or data.startswith(b"\xef\xbb\xbf"):
		return rel, None, "unsupported_layout"
	try:
		text = data.decode("utf-8")
	except UnicodeDecodeError:
		return rel, None, "unsupported_layout"
	if "\r" in text or "\t" in text:
		return rel, None, "unsupported_layout"
	return rel, text, None


# ── Line-level YAML helpers ─────────────────────────────────────────────


def split_comment(line: str) -> str:
	"""Return the code part of ``line`` (a ``#`` comment outside quotes removed)."""
	quote = None
	index = 0
	while index < len(line):
		char = line[index]
		if quote:
			if char == quote:
				if quote == "'" and index + 1 < len(line) and line[index + 1] == "'":
					index += 2
					continue
				quote = None
			elif char == "\\" and quote == '"':
				index += 2
				continue
		elif char in ("'", '"') and (index == 0 or line[index - 1] in " [{,"):
			# A quote opens a quoted scalar only at the start of a value; an
			# apostrophe inside a plain scalar ("it's") is literal.
			quote = char
		elif char == "#" and (index == 0 or line[index - 1] == " "):
			return line[:index].rstrip()
		index += 1
	if quote:
		raise UnsupportedLayout("unterminated quote")
	return line.rstrip()


def indent_of(line: str) -> int:
	return len(line) - len(line.lstrip(" "))


def is_content(line: str) -> bool:
	"""True for a line carrying YAML content (not blank, not a pure comment)."""
	stripped = line.strip()
	return bool(stripped) and not stripped.startswith("#")


def unquote(value: str) -> str:
	value = value.strip()
	if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
		inner = value[1:-1]
		if value[0] == '"' and "\\" in inner:
			raise UnsupportedLayout("escaped double-quoted scalar")
		if value[0] == "'":
			inner = inner.replace("''", "'")
		return inner
	if value[:1] in ("'", '"'):
		raise UnsupportedLayout("multi-line quoted scalar")
	return value


def value_has_node_property(code: str) -> bool:
	"""True when a value on this line starts with an anchor, alias or tag."""
	rest = code.strip()
	while True:
		item = ITEM_RE.match(rest)
		if item and rest.startswith("-"):
			rest = (item.group(2) or "").strip()
			continue
		break
	if rest.startswith(("&", "*", "!", "?", "%")):
		return True
	key = re.match(r"^[^'\"\[\]{}#][^:]*:(?:[ ]+(.*))?$", rest)
	if key:
		value = (key.group(1) or "").strip()
		return value.startswith(("&", "*", "!"))
	return False


def normalize_directory(value: str) -> str:
	value = value.strip()
	if len(value) > 1:
		value = value.rstrip("/") or "/"
	return value


def parse_flow_list(value: str) -> list:
	inner = value.strip()
	if not (inner.startswith("[") and inner.endswith("]")):
		raise UnsupportedLayout("flow list")
	inner = inner[1:-1]
	if any(char in inner for char in "[]{}"):
		raise UnsupportedLayout("nested flow collection")
	return [unquote(part) for part in inner.split(",") if part.strip()]


# ── Editor ──────────────────────────────────────────────────────────────


def _parse_item(lines: list, start: int, end: int, item_indent: int) -> dict:
	"""Split one ``updates`` entry into keys with their line ranges."""
	key_indent = item_indent + 2
	first = lines[start]
	match = ITEM_RE.match(split_comment(first))
	if not match or not match.group(2) or match.group(2).startswith(" "):
		raise UnsupportedLayout("entry does not start with '- key:'")
	keys = []
	for index in range(start, end):
		line = lines[index]
		if not is_content(line):
			continue
		code = split_comment(line)
		if index == start:
			body = code[key_indent:]
		else:
			current = indent_of(code)
			if current < key_indent:
				raise UnsupportedLayout("entry content left of its keys")
			if current > key_indent or (code[key_indent:].startswith("-") and keys):
				keys[-1]["children"].append(index)
				keys[-1]["last"] = index
				continue
			body = code[key_indent:]
		key_match = KEY_RE.match(body)
		if not key_match:
			raise UnsupportedLayout("unsupported key line")
		keys.append({
			"name": key_match.group(1),
			"value": (key_match.group(2) or "").strip(),
			"line": index,
			"last": index,
			"children": [],
		})
	names = [key["name"] for key in keys]
	if len(names) != len(set(names)):
		raise UnsupportedLayout("duplicate key in entry")
	return {"keys": {key["name"]: key for key in keys}, "ordered": keys, "key_indent": key_indent, "start": start, "end": end}


def _scalar(lines: list, key) -> str:
	if key is None:
		return ""
	if key["children"]:
		raise UnsupportedLayout("multi-line scalar")
	if key["value"][:1] in ("|", ">", "[", "{"):
		return ""
	return unquote(key["value"])


def _directories(lines: list, key) -> list:
	if key is None:
		return []
	value = key["value"]
	if value:
		if key["children"]:
			raise UnsupportedLayout("mixed directories layout")
		if value.startswith("["):
			return parse_flow_list(value)
		return []
	result = []
	for index in key["children"]:
		match = ITEM_RE.match(split_comment(lines[index]).strip())
		if not match or match.group(2) is None:
			raise UnsupportedLayout("unsupported directories item")
		result.append(unquote(match.group(2)))
	return result


def _matches_root_actions(lines: list, item: dict):
	"""Return ``(is_github_actions, covers_root)`` for one entry."""
	keys = item["keys"]
	ecosystem = _scalar(lines, keys.get("package-ecosystem"))
	if ecosystem != "github-actions":
		return False, False
	if normalize_directory(_scalar(lines, keys.get("directory"))) == "/":
		return True, True
	directories = [normalize_directory(value) for value in _directories(lines, keys.get("directories"))]
	return True, "/" in directories


def _edit_item(lines: list, item: dict, managed: list, owned_paths: set, drop: set, insert_after: dict) -> None:
	for index in range(item["start"], item["end"]):
		if is_content(lines[index]) and BLOCK_SCALAR_RE.search(split_comment(lines[index]).strip()):
			raise UnsupportedLayout("block scalar in edited entry")
	key_indent = item["key_indent"]
	exclude = item["keys"].get("exclude-paths")
	if exclude is None:
		anchor = max(key["last"] for key in item["ordered"])
		new_lines = [" " * key_indent + "exclude-paths:"]
		new_lines += [" " * (key_indent + 2) + f'- "{WORKFLOWS_PREFIX}{name}"' for name in managed]
		insert_after.setdefault(anchor, []).extend(new_lines)
		return
	if exclude["value"]:
		raise UnsupportedLayout("exclude-paths is not a block list")
	list_indent = None
	last_item_line = exclude["line"]
	for index in exclude["children"]:
		code = split_comment(lines[index])
		current = indent_of(code)
		if list_indent is None:
			list_indent = current
		if current != list_indent:
			raise UnsupportedLayout("multi-line exclude-paths item")
		match = ITEM_RE.match(code[current:])
		if not match or not match.group(2):
			raise UnsupportedLayout("unsupported exclude-paths item")
		value = match.group(2).strip()
		if value[:1] in ("[", "{", "|", ">"):
			raise UnsupportedLayout("unsupported exclude-paths item")
		if unquote(value) in owned_paths:
			drop.add(index)
		last_item_line = index
	if list_indent is None:
		list_indent = key_indent + 2
	new_lines = [" " * list_indent + f'- "{WORKFLOWS_PREFIX}{name}"' for name in managed]
	insert_after.setdefault(last_item_line, []).extend(new_lines)


def plan_edit(text: str, managed: list, owned_names: set):
	"""Return ``(new_text, reason, matched_entries)``; new_text None = no change.

	Raises ``UnsupportedLayout`` for any layout outside the supported subset.
	"""
	for name in managed:
		if not WRAPPER_NAME_RE.match(name):
			raise ValueError(f"invalid managed name {name!r}")
	lines = text.split("\n")
	updates_lines = []
	for index, line in enumerate(lines):
		if not is_content(line):
			continue
		if line.startswith(("---", "...")) and (len(line) == 3 or line[3] == " "):
			raise UnsupportedLayout("document marker")
		code = split_comment(line)
		if value_has_node_property(code):
			raise UnsupportedLayout("anchor, alias, tag or complex key")
		if indent_of(line) == 0:
			if re.match(r"^[\"']updates[\"']\s*:", code):
				raise UnsupportedLayout("quoted updates key")
			if re.match(r"^updates\s*:", code):
				updates_lines.append(index)
	if not updates_lines:
		return None, "no_github_actions_entry", 0
	if len(updates_lines) > 1:
		raise UnsupportedLayout("duplicate updates key")
	updates_index = updates_lines[0]
	if split_comment(lines[updates_index]) != "updates:":
		raise UnsupportedLayout("updates is not a block sequence")
	# Collect the updates block and its entries.
	item_indent = None
	item_starts = []
	block_end = len(lines)
	for index in range(updates_index + 1, len(lines)):
		line = lines[index]
		if not is_content(line):
			continue
		current = indent_of(line)
		starts_item = line[current:].startswith("-")
		if item_indent is None:
			if not starts_item:
				raise UnsupportedLayout("updates is not a block sequence")
			item_indent = current
		if current == item_indent and starts_item:
			item_starts.append(index)
			continue
		if current > item_indent:
			continue
		if current == 0:
			block_end = index
			break
		raise UnsupportedLayout("misaligned updates entry")
	if not item_starts:
		return None, "no_github_actions_entry", 0
	# An entry ends at the next entry; trailing blank/comment lines before the
	# next top-level key stay with the file, not the entry.
	bounds = []
	for position, start in enumerate(item_starts):
		end = item_starts[position + 1] if position + 1 < len(item_starts) else block_end
		bounds.append((start, end))
	owned_paths = {WORKFLOWS_PREFIX + name for name in owned_names}
	drop = set()
	insert_after = {}
	matched = 0
	saw_actions = False
	for start, end in bounds:
		item = _parse_item(lines, start, end, item_indent)
		is_actions, covers_root = _matches_root_actions(lines, item)
		saw_actions = saw_actions or is_actions
		if not covers_root:
			continue
		matched += 1
		_edit_item(lines, item, managed, owned_paths, drop, insert_after)
	if not matched:
		return None, ("no_root_entry" if saw_actions else "no_github_actions_entry"), 0
	output = []
	for index, line in enumerate(lines):
		if index not in drop:
			output.append(line)
		output.extend(insert_after.get(index, []))
	new_text = "\n".join(output)
	if new_text == text:
		return None, "up_to_date", matched
	return new_text, "excluded", matched


# ── Entry point ─────────────────────────────────────────────────────────


def write_github_output(changed: bool) -> None:
	target = os.environ.get("GITHUB_OUTPUT")
	if not target:
		return
	try:
		with open(target, "a", encoding="utf-8") as handle:
			handle.write(f"dependabot_has_changes={'true' if changed else 'false'}\n")
	except OSError:
		print(f"::warning::{LOG_PREFIX} could not write GITHUB_OUTPUT")


def write_changed_paths(path: str | None, changed: list) -> None:
	if not path:
		return
	try:
		Path(path).write_text("".join(f"{item}\n" for item in changed), encoding="utf-8")
	except OSError:
		print(f"::warning::{LOG_PREFIX} could not write the changed-paths file")


def write_config(repo_root: Path, rel: str, text: str) -> None:
	fd = os.open(repo_root / rel, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
	with os.fdopen(fd, "wb") as handle:
		handle.write(text.encode("utf-8"))


def run(args: argparse.Namespace) -> tuple:
	"""Return ``(outcome, reason, file_rel, entries, managed_count, changed)``."""
	repo_root = Path(args.repo_root)
	templates_dir = Path(args.templates_dir)
	if not repo_root.is_dir() or not templates_dir.is_dir():
		return "skipped", "templates_missing" if repo_root.is_dir() else "repo_missing", None, 0, 0, []
	managed, reason = load_managed_names(templates_dir, args.profile, repo_root)
	if managed is None:
		return "skipped", reason, None, 0, 0, []
	rel, text, reason = locate_config(repo_root)
	if text is None:
		return "skipped", reason, rel, 0, len(managed), []
	owned = template_basenames(templates_dir) | set(managed)
	try:
		new_text, reason, matched = plan_edit(text, managed, owned)
	except UnsupportedLayout:
		return "skipped", "unsupported_layout", rel, 0, len(managed), []
	if new_text is None:
		outcome = "unchanged" if reason == "up_to_date" else "skipped"
		return outcome, reason, rel, matched, len(managed), []
	if args.dry_run:
		return "updated", "dry_run", rel, matched, len(managed), [rel]
	try:
		write_config(repo_root, rel, new_text)
	except OSError:
		return "skipped", "write_failed", rel, matched, len(managed), []
	return "updated", reason, rel, matched, len(managed), [rel]


class _Parser(argparse.ArgumentParser):
	def error(self, message: str) -> None:  # pragma: no cover - argparse hook
		self.print_usage(sys.stderr)
		print(f"{LOG_PREFIX} outcome=error reason=bad_arguments", file=sys.stderr)
		raise SystemExit(2)


def main(argv: list | None = None) -> int:
	parser = _Parser(description=__doc__.splitlines()[0])
	parser.add_argument("--repo-root", required=True)
	parser.add_argument("--templates-dir", required=True)
	parser.add_argument("--profile", required=True)
	parser.add_argument("--changed-paths-file")
	parser.add_argument("--dry-run", action="store_true")
	args = parser.parse_args(argv)
	try:
		outcome, reason, rel, entries, managed_count, changed = run(args)
	except Exception:  # noqa: BLE001 - fail open by contract: the sync continues
		outcome, reason, rel, entries, managed_count, changed = "skipped", "internal_error", None, 0, 0, []
	if args.dry_run:
		write_changed_paths(args.changed_paths_file, [])
		write_github_output(False)
	else:
		write_changed_paths(args.changed_paths_file, changed)
		write_github_output(bool(changed))
	if outcome == "skipped" and reason in ("unsupported_layout", "read_failed", "write_failed", "internal_error", "ambiguous_config"):
		print(f"::warning::{LOG_PREFIX} left {rel or 'the Dependabot config'} unchanged (reason={reason}); add the managed wrappers to exclude-paths by hand.")
	log(outcome, reason, rel, entries, managed_count)
	return 0


if __name__ == "__main__":
	sys.exit(main())
