#!/usr/bin/env python3
"""Direct-run prompt size budget gate for mode/review prompt templates."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
TIER_LIMITS = {
	"DEFAULT": 250,
	"LARGE": 500,
	"XL": 800,
}
EXPECTED_TIERS = {
	"mode-validate-diagnose.txt": "LARGE",
	"mode-validate-fix-harness.txt": "LARGE",
	"mode-validate-generate.txt": "XL",
}
TIER_LINE_RE = re.compile(r"^# tier: (DEFAULT|LARGE|XL)$")


def prompt_paths() -> list[Path]:
	paths = sorted(PROMPTS_DIR.glob("mode-*.txt")) + sorted(PROMPTS_DIR.glob("review-*.txt"))
	return list(paths)


def main() -> int:
	failures: list[str] = []
	paths = prompt_paths()
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

	if failures:
		for failure in failures:
			print(f"FAIL: {failure}")
		return 1

	print(f"OK: validated prompt tiers for {len(paths)} files")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
