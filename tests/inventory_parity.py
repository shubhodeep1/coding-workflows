#!/usr/bin/env python3
"""Direct-run inventory parity gate for docs/INVENTORY.md."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY_PATH = REPO_ROOT / "docs" / "INVENTORY.md"
EXEMPTIONS_PATH = REPO_ROOT / "tests" / "inventory_exemptions.txt"
SECTION_ORDER = OrderedDict(
	[
		("Phase prompts", lambda path: path.match("prompts/mode-*.txt") or path.match("prompts/review-*.txt")),
		("Workflows", lambda path: path.match(".github/workflows/*.yml")),
		("Scripts", lambda path: path.parts and path.parts[0] == "scripts"),
		("Prompt references", lambda path: path.match("prompts/references/*.txt")),
		(
			"Audit-gate assets",
			lambda path: path.match("prompts/contracts/*.yml")
			or path.as_posix()
			in {
				"tests/prompt_size_budget.py",
				"tests/inventory_parity.py",
				"tests/inventory_exemptions.txt",
			},
		),
	]
)


def iter_surface_files() -> list[Path]:
	try:
		proc = subprocess.run(
			["git", "ls-files", "-z"],
			cwd=REPO_ROOT,
			check=True,
			capture_output=True,
			text=True,
		)
		return sorted(
			Path(path)
			for path in proc.stdout.split("\0")
			if path and (REPO_ROOT / path).is_file()
		)
	except (FileNotFoundError, OSError, subprocess.SubprocessError):
		pass

	paths: list[Path] = []
	for path in REPO_ROOT.rglob("*"):
		if not path.is_file():
			continue
		relative_path = path.relative_to(REPO_ROOT)
		if ".git" in relative_path.parts or "__pycache__" in relative_path.parts or relative_path.suffix == ".pyc":
			continue
		paths.append(relative_path)
	return sorted(paths)


def load_exemptions() -> dict[str, str]:
	exemptions: dict[str, str] = {}
	for line_number, raw_line in enumerate(EXEMPTIONS_PATH.read_text(encoding="utf-8").splitlines(), 1):
		line = raw_line.strip()
		if not line or line.startswith("#"):
			continue
		path, separator, justification = line.partition(" | ")
		if separator != " | " or not path or not justification:
			raise SystemExit(
				f"FAIL: {EXEMPTIONS_PATH.as_posix()}:{line_number}: expected 'path | justification'"
			)
		if path in exemptions:
			raise SystemExit(f"FAIL: duplicate exemption entry for {path}")
		exemptions[path] = justification
	return exemptions


def parse_inventory() -> dict[str, list[str]]:
	sections = {name: [] for name in SECTION_ORDER}
	current_section: str | None = None
	for line_number, line in enumerate(INVENTORY_PATH.read_text(encoding="utf-8").splitlines(), 1):
		if line.startswith("## "):
			heading = line[3:].strip()
			current_section = heading if heading in sections else None
			continue
		if current_section is None or not line.startswith("- "):
			continue
		prefix = "- `"
		separator = "` — "
		if not line.startswith(prefix) or separator not in line:
			raise SystemExit(
				f"FAIL: {INVENTORY_PATH.as_posix()}:{line_number}: expected '- `path` — role'"
			)
		path_part = line[len(prefix):].split(separator, 1)[0]
		if not path_part:
			raise SystemExit(f"FAIL: {INVENTORY_PATH.as_posix()}:{line_number}: empty path entry")
		sections[current_section].append(path_part)
	return sections


def collect_expected_paths() -> dict[str, list[str]]:
	sections = {name: [] for name in SECTION_ORDER}
	for path in iter_surface_files():
		for section_name, matcher in SECTION_ORDER.items():
			if matcher(path):
				sections[section_name].append(path.as_posix())
				break
	return sections


def main() -> int:
	exemptions = load_exemptions()
	documented = parse_inventory()
	expected_by_section = collect_expected_paths()
	failures: list[str] = []
	section_membership: dict[str, str] = {}
	all_expected = {path for paths in expected_by_section.values() for path in paths}

	for path in sorted(exemptions):
		if path not in all_expected:
			failures.append(f"inventory_exemptions.txt: {path} is not part of a tracked inventory surface")

	for section_name, expected_paths in expected_by_section.items():
		expected_visible = sorted(path for path in expected_paths if path not in exemptions)
		documented_paths = documented[section_name]
		for path in documented_paths:
			previous = section_membership.get(path)
			if previous is not None:
				failures.append(f"{path}: documented in multiple sections ({previous}, {section_name})")
			else:
				section_membership[path] = section_name
		missing = sorted(set(expected_visible) - set(documented_paths))
		extra = sorted(set(documented_paths) - set(expected_visible))
		for path in missing:
			failures.append(f"{section_name}: missing {path}")
		for path in extra:
			failures.append(f"{section_name}: unexpected {path}")

	if failures:
		for failure in failures:
			print(f"FAIL: {failure}")
		return 1

	count = sum(len(paths) for paths in documented.values())
	print(f"OK: inventory parity validated across {count} documented paths")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
