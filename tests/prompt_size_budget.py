#!/usr/bin/env python3
"""Direct-run prompt size budget gate for tiered and operational prompts."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
TIER_PROMPT_GLOBS = ("mode-*.txt", "review-*.txt")
TIER_LIMITS = {
	"DEFAULT": 250,
	"LARGE": 500,
	"XL": 800,
}
OPERATIONAL_PROMPT_LIMITS = {
	"conflict-resolver.txt": 150,
	"integration-sync-conflict-resolver.txt": 200,
	"integration-sync-conflict-resolver-retry-prelude.txt": 100,
	"integration-sync-conflict-resolver-retry-timeout-prelude.txt": 100,
	"header.txt": 20,
}
EXPECTED_TIERS = {
	"mode-validate-diagnose.txt": "LARGE",
	"mode-validate-fix-harness.txt": "LARGE",
	"mode-validate-generate.txt": "XL",
}
TIER_LINE_RE = re.compile(r"^# tier: (DEFAULT|LARGE|XL)$")


def tier_prompt_paths() -> list[Path]:
	paths: list[Path] = []
	for pattern in TIER_PROMPT_GLOBS:
		paths.extend(sorted(PROMPTS_DIR.glob(pattern)))
	return paths


def main() -> int:
	failures: list[str] = []
	paths = tier_prompt_paths()
	if not paths:
		print("FAIL: no prompt files matched")
		return 1

	seen_names = {path.name for path in paths}
	for name in sorted(EXPECTED_TIERS):
		if name not in seen_names:
			failures.append(f"Missing expected prompt file: {name}")

	for path in paths:
		lines = path.read_text(encoding="utf-8").splitlines()
		if not lines:
			failures.append(f"{path.as_posix()}: file is empty")
			continue
		match = TIER_LINE_RE.fullmatch(lines[0])
		if match is None:
			failures.append(
				f"{path.as_posix()}: first line must be '# tier: DEFAULT|LARGE|XL', got {lines[0]!r}"
			)
			continue
		tier = match.group(1)
		expected_tier = EXPECTED_TIERS.get(path.name, "DEFAULT")
		if tier != expected_tier:
			failures.append(
				f"{path.as_posix()}: tier {tier} does not match expected {expected_tier}"
			)
		limit = TIER_LIMITS[tier]
		line_count = len(lines)
		if line_count > limit:
			failures.append(
				f"{path.as_posix()}: {line_count} lines exceeds {tier} limit of {limit}"
			)

	for name, limit in OPERATIONAL_PROMPT_LIMITS.items():
		path = PROMPTS_DIR / name
		if not path.is_file():
			failures.append(f"Missing operational prompt file: {path.as_posix()}")
			continue
		line_count = len(path.read_text(encoding="utf-8").splitlines())
		if line_count > limit:
			failures.append(
				f"{path.as_posix()}: {line_count} lines exceeds operational limit of {limit}"
			)

	if failures:
		for failure in failures:
			print(f"FAIL: {failure}")
		return 1

	print(
		f"OK: validated prompt budgets for {len(paths)} tiered files and "
		f"{len(OPERATIONAL_PROMPT_LIMITS)} operational files"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
