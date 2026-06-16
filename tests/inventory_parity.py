#!/usr/bin/env python3
"""Direct-run inventory parity gate for docs/INVENTORY.md, README.md, and agents.md."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import re
import subprocess


REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"
AGENTS_PATH = REPO_ROOT / "agents.md"
INVENTORY_PATH = REPO_ROOT / "docs" / "INVENTORY.md"
EXEMPTIONS_PATH = REPO_ROOT / "tests" / "inventory_exemptions.txt"
SECONDARY_DOC_PATHS = OrderedDict(
	[
		("README.md", README_PATH),
		("agents.md", AGENTS_PATH),
	]
)
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
TRACKED_REFERENCE_PATTERNS = (
	re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.yml"),
	re.compile(r"prompts/mode-[A-Za-z0-9_.-]+\.txt"),
	re.compile(r"prompts/review-[A-Za-z0-9_.-]+\.txt"),
	re.compile(r"prompts/references/[A-Za-z0-9_.-]+\.txt"),
	re.compile(r"prompts/contracts/[A-Za-z0-9_.-]+\.yml"),
	re.compile(r"scripts/[A-Za-z0-9_./-]+"),
	re.compile(r"tests/(?:prompt_size_budget\.py|inventory_parity\.py|inventory_exemptions\.txt)"),
)
PATH_ASSIGNMENT_RE = re.compile(r"\bpath=([A-Za-z0-9_./-]+)")
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
EXAMPLE_PREFIX_RE = re.compile(
	r"(?:such as|for example|e\.g\.)[\s,:-]*(?:see(?:\s+also)?\s*)?$",
	re.IGNORECASE,
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


def classify_surface_path(path_text: str) -> str | None:
	path = Path(path_text)
	for section_name, matcher in SECTION_ORDER.items():
		if matcher(path):
			return section_name
	return None


def normalize_reference(path_text: str) -> str:
	return path_text.rstrip(".,;:")


def is_generated_wrapper_reference(path_text: str) -> bool:
	# README.md documents generated consumer wrapper names and consumer-managed
	# audit-gate assets that are not shipped in this repository, so keep them
	# outside the repo-surface parity check.
	return (
		path_text.startswith(".github/workflows/ai-") or path_text == "scripts/security/check-npm-audit.js"
	) and not (REPO_ROOT / path_text).is_file()


def extract_tracked_paths(text: str) -> list[str]:
	paths: list[str] = []
	seen: set[str] = set()
	for pattern in TRACKED_REFERENCE_PATTERNS:
		for match in pattern.finditer(text):
			candidate = normalize_reference(match.group(0))
			if candidate in seen or classify_surface_path(candidate) is None:
				continue
			seen.add(candidate)
			paths.append(candidate)
	return paths


def parse_secondary_document(document_path: Path) -> dict[str, int]:
	references: dict[str, int] = {}
	for line_number, line in enumerate(document_path.read_text(encoding="utf-8").splitlines(), 1):
		for match in PATH_ASSIGNMENT_RE.finditer(line):
			candidate = normalize_reference(match.group(1))
			if classify_surface_path(candidate) is None:
				continue
			references.setdefault(candidate, line_number)
		for span_match in CODE_SPAN_RE.finditer(line):
			if EXAMPLE_PREFIX_RE.search(line[:span_match.start()]):
				continue
			for candidate in extract_tracked_paths(span_match.group(1)):
				references.setdefault(candidate, line_number)
	return references


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


def validate_secondary_documents(
	authoritative_paths: set[str],
	all_expected: set[str],
	exemptions: dict[str, str],
) -> list[str]:
	failures: list[str] = []
	for document_name, document_path in SECONDARY_DOC_PATHS.items():
		try:
			references = parse_secondary_document(document_path)
		except OSError as exc:
			reason = exc.strerror or str(exc)
			failures.append(f"{document_name}: unable to read ({type(exc).__name__}: {reason})")
			continue
		for path, line_number in sorted(references.items()):
			if path in exemptions or is_generated_wrapper_reference(path):
				continue
			if path not in all_expected:
				failures.append(
					f"{document_name}:{line_number}: unexpected tracked-surface reference {path}"
				)
				continue
			if path not in authoritative_paths:
				failures.append(
					f"{document_name}:{line_number}: references {path} but docs/INVENTORY.md does not document it"
				)
	return failures


def main() -> int:
	exemptions = load_exemptions()
	documented = parse_inventory()
	expected_by_section = collect_expected_paths()
	failures: list[str] = []
	section_membership: dict[str, str] = {}
	all_expected = {path for paths in expected_by_section.values() for path in paths}
	authoritative_paths = {path for paths in documented.values() for path in paths}

	for path in sorted(exemptions):
		if path not in all_expected:
			failures.append(f"inventory_exemptions.txt: {path} is not part of a tracked inventory surface")

	for section_name, expected_paths in expected_by_section.items():
		expected_visible = sorted(path for path in expected_paths if path not in exemptions)
		documented_paths = documented[section_name]
		for path in documented_paths:
			previous = section_membership.get(path)
			if previous is not None:
				failures.append(
					f"docs/INVENTORY.md: {path}: documented in multiple sections ({previous}, {section_name})"
				)
			else:
				section_membership[path] = section_name
		missing = sorted(set(expected_visible) - set(documented_paths))
		extra = sorted(set(documented_paths) - set(expected_visible))
		for path in missing:
			failures.append(f"docs/INVENTORY.md: {section_name}: missing {path}")
		for path in extra:
			failures.append(f"docs/INVENTORY.md: {section_name}: unexpected {path}")

	failures.extend(validate_secondary_documents(authoritative_paths, all_expected, exemptions))

	if failures:
		for failure in failures:
			print(f"FAIL: {failure}")
		return 1

	count = sum(len(paths) for paths in documented.values())
	print(f"OK: inventory parity validated across docs/INVENTORY.md, README.md, and agents.md ({count} documented paths)")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
