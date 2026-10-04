#!/usr/bin/env python3
"""Goal G1 of docs/plans/replace-claude-sessions-with-cli-engine-plan.md.

After the session-based Claude automation was retired, no workflow, script,
hook, command or setting may start, wake, schedule, claim for or archive a
claude.ai session again. The plan's check is a grep over
`.github/ scripts/ .claude/ workflow-templates/` that may only hit the
retired-files manifest and the label contract's `retired_labels` list. This
test runs that grep so a codex autofix or a later change that re-adds the
machinery fails CI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = (".github", "scripts", ".claude", "workflow-templates")
PATTERN = re.compile(
	r"send_later|create_session|create_trigger|claude-issue-queue|check_in_status"
	r"|claude_fix_claim|stale_routines|claude_session_janitor"
)
RETIRED_FILES_MANIFEST = Path("workflow-templates/retired_files.txt")
LABEL_CONTRACT = Path(".github/ai/label_contract.v1.json")


def _scanned_files() -> list[Path]:
	files = []
	for directory in SCANNED_DIRS:
		for path in sorted((REPO_ROOT / directory).rglob("*")):
			if path.is_file() and not path.is_symlink():
				files.append(path)
	return files


def _hits() -> list[str]:
	retired_labels = set(json.loads((REPO_ROOT / LABEL_CONTRACT).read_text(encoding="utf-8")).get("retired_labels", []))
	hits = []
	for path in _scanned_files():
		rel = path.relative_to(REPO_ROOT)
		if rel == RETIRED_FILES_MANIFEST:
			continue
		try:
			text = path.read_text(encoding="utf-8")
		except UnicodeDecodeError:
			continue
		for number, line in enumerate(text.splitlines(), 1):
			if not PATTERN.search(line):
				continue
			if rel == LABEL_CONTRACT and line.strip().strip(",").strip('"') in retired_labels:
				continue
			hits.append(f"{rel}:{number}: {line.strip()[:160]}")
	return hits


def test_no_session_automation_remains():
	hits = _hits()
	assert not hits, "session automation is back:\n" + "\n".join(hits)


def test_retired_files_manifest_is_the_only_exempt_file():
	manifest = (REPO_ROOT / RETIRED_FILES_MANIFEST).read_text(encoding="utf-8")
	assert ".claude/scripts/check_in_status.py" in manifest


def test_detector_catches_a_reintroduced_call():
	assert PATTERN.search("mcp__claude-code-remote__send_later")
	assert PATTERN.search("python3 .claude/scripts/check_in_status.py --hand-back")
	assert not PATTERN.search("python3 scripts/security_pass_skip.py --repo o/r --issue 1")


def main() -> int:
	for name in sorted(globals()):
		if name.startswith("test_") and callable(globals()[name]):
			globals()[name]()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
