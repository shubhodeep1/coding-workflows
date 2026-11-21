#!/usr/bin/env python3
"""Bound the workflow-log artifact before passing it to an isolated agent.

The 256 MiB limit mirrors MAX_INCLUDE_TOTAL in codex_isolated_workspace.py;
tests pin the two constants together. Leave 1 MiB of headroom for the mount.
"""

import json
import os
from pathlib import Path
import shutil
import stat
import sys


MAX_INCLUDE_TOTAL = 256 * 1024 * 1024
INCLUDE_HEADROOM = 1024 * 1024
CATEGORIES = ("errors", "slow", "recent")


def safe_part(name: str) -> bool:
	return name not in (".", "..") and name.lower() != ".git" and not any(char in name for char in "\r\n\0\\")


def regular_size(path: Path) -> int | None:
	info = path.lstat()
	return info.st_size if stat.S_ISREG(info.st_mode) else None


def raise_walk_error(error: OSError) -> None:
	raise error


def measure(src: Path) -> int:
	"""Count regular file bytes without following links or special files."""
	total = 0
	for directory, dirs, files in os.walk(src, followlinks=False, onerror=raise_walk_error):
		dirs[:] = [name for name in dirs if safe_part(name) and stat.S_ISDIR((Path(directory) / name).lstat().st_mode)]
		for name in files:
			if safe_part(name):
				size = regular_size(Path(directory) / name)
				if size is not None:
					total += size
	return total


def run_dirs(src: Path):
	for category in CATEGORIES:
		category_dir = src / category
		if not category_dir.is_dir() or category_dir.is_symlink():
			continue
		for repo in sorted(category_dir.iterdir()):
			if not safe_part(repo.name) or not repo.is_dir() or repo.is_symlink():
				continue
			for family in sorted(repo.iterdir()):
				if not safe_part(family.name) or not family.is_dir() or family.is_symlink():
					continue
				for run in sorted(family.iterdir()):
					if safe_part(run.name) and run.is_dir() and not run.is_symlink():
						yield run.relative_to(src)


def copy_file(path: Path, destination: Path, size: int) -> None:
	destination.parent.mkdir(parents=True, exist_ok=True)
	with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as input_file:
		info = os.fstat(input_file.fileno())
		if not stat.S_ISREG(info.st_mode) or info.st_size != size:
			raise OSError("artifact file changed during staging")
		with destination.open("xb") as output_file:
			remaining = size
			while remaining:
				chunk = input_file.read(min(1024 * 1024, remaining))
				if not chunk:
					raise OSError("artifact file truncated during staging")
				output_file.write(chunk)
				remaining -= len(chunk)
			if input_file.read(1):
				raise OSError("artifact file grew during staging")


def copy_tree(src: Path, stage: Path, budget: int) -> int:
	"""Copy a selected subtree; reject files that change size during copying."""
	if not stat.S_ISDIR(src.lstat().st_mode):
		raise OSError("run directory changed during staging")
	copied = 0
	for directory, dirs, files in os.walk(src, followlinks=False, onerror=raise_walk_error):
		dirs[:] = [name for name in dirs if safe_part(name) and stat.S_ISDIR((Path(directory) / name).lstat().st_mode)]
		for name in files:
			if not safe_part(name):
				continue
			path = Path(directory) / name
			size = regular_size(path)
			if size is None:
				continue
			if copied + size > budget:
				raise OSError("artifact changed or exceeds selected budget")
			destination = stage / path.relative_to(src)
			copy_file(path, destination, size)
			copied += size
	return copied


def stage_bundle(src: Path, stage: Path, max_bytes: int) -> dict:
	if not src.is_dir() or src.is_symlink():
		raise OSError("source must be a directory, not a symlink")
	src = src.resolve(strict=True)
	stage = stage.absolute()
	if stage == src or src in stage.parents:
		raise OSError("stage must be outside the source")
	limit = min(max_bytes, MAX_INCLUDE_TOTAL - INCLUDE_HEADROOM)
	total = measure(src)
	if total <= limit:
		return {"mode": "full", "path": str(src), "bytes": total, "total_bytes": total, "omitted_runs": 0}

	# Plan selections before writing so the omission manifest also counts against
	# the include cap. Never publish a partial stage if an artifact read fails.
	header = f"Bounded run-log subset (cap: {limit} bytes; original: {total} bytes).\nOmitted runs:\n"
	summary = src / "summary.json"
	summary_size = regular_size(summary) if summary.exists() or summary.is_symlink() else None
	selected = []
	omitted = []
	used = 0
	if summary_size is not None:
		if summary_size + len(header.encode()) <= limit:
			selected.append((Path("summary.json"), summary_size))
			used += summary_size
		else:
			omitted.append("summary.json")
	for relative in run_dirs(src):
		size = measure(src / relative)
		if used + size + len(header.encode()) <= limit:
			selected.append((relative, size))
			used += size
		else:
			omitted.append(relative.as_posix())
	while selected and used + len((header + "".join(f"{name}\n" for name in omitted)).encode()) > limit:
		last, size = selected.pop()
		used -= size
		omitted.append(last.as_posix())
	note = header + "".join(f"{name}\n" for name in omitted)
	if used + len(note.encode()) > limit:
		raise OSError("cap too small for omission manifest")

	stage.mkdir(parents=True, exist_ok=False)
	try:
		copied = 0
		for relative, size in selected:
			if relative == Path("summary.json"):
				copy_file(src / relative, stage / relative, size)
				copied += size
			else:
				copied += copy_tree(src / relative, stage / relative, size)
		(stage / "BOUNDED_SUBSET.txt").write_text(note, encoding="utf-8")
		if copied + len(note.encode()) > limit:
			raise OSError("staged bundle exceeds cap")
	except OSError:
		shutil.rmtree(stage)
		raise
	return {"mode": "bounded", "path": str(stage.resolve()), "bytes": copied + len(note.encode()),
		"total_bytes": total, "omitted_runs": sum(name != "summary.json" for name in omitted)}


def main() -> int:
	if len(sys.argv) != 4 or not sys.argv[3].isascii() or not sys.argv[3].isdecimal() or int(sys.argv[3]) <= 0:
		print("usage: stage_workflow_log_bundle.py SRC_DIR STAGE_DIR MAX_BYTES (positive integer)", file=sys.stderr)
		return 2
	try:
		result = stage_bundle(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]))
	except OSError:
		print("workflow-log bundle staging failed", file=sys.stderr)
		return 1
	print(json.dumps(result))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
