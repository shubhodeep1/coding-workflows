#!/usr/bin/env python3
"""Unit tests for scripts/partial_delivery.py (issue #7657)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "partial_delivery.py"

_spec = importlib.util.spec_from_file_location("partial_delivery", HELPER)
assert _spec and _spec.loader
pd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pd)

# The #4639 regression shape: an activation-fix issue whose clarification
# limited this delivery to Phase 1.
PLAN_4639 = """Implementation Plan

Scope-mode: Hold Scope

## Files likely to change
- `site/home_guest.py`

## Deferred scope
- Implement the member homepage (Phase 2).

## Functions/modules to implement
1. Guest homepage.

STATUS: CLEAR
"""


def test_plan_with_deferred_scope_is_partial() -> None:
	result = pd.parse_plan(PLAN_4639)
	assert result == {"status": "partial", "items": ["Implement the member homepage (Phase 2)."], "reason": ""}


def test_plan_without_section_is_full() -> None:
	assert pd.parse_plan("Implementation Plan\n\n## Files likely to change\n- `a`\n")["status"] == "full"
	# An inline mention is not a heading.
	assert pd.parse_plan("Emit `## Deferred scope` only when needed.\n")["status"] == "full"


def test_heading_inside_a_code_fence_is_ignored() -> None:
	text = "Plan\n\n```\n## Deferred scope\n- x\n```\n\n## Next\n"
	assert pd.parse_plan(text)["status"] == "full"


def test_malformed_sections_fail_safe() -> None:
	cases = {
		"empty": "## Deferred scope\n\n## Next\n",
		"too_many_items": "## Deferred scope\n" + "".join(f"- item {i}\n" for i in range(21)),
		"item_too_long": "## Deferred scope\n- " + "x" * 501 + "\n",
		"duplicate_heading": "## Deferred scope\n- a\n\n## Deferred scope\n- b\n",
		"prose_in_block": "## Deferred scope\n- a\nsome prose\n",
		"unclosed_fence": "## Deferred scope\n- a\n```\n",
	}
	for reason, text in cases.items():
		result = pd.parse_plan(text)
		assert result["status"] == "malformed", (reason, result)
		assert result["reason"] == reason, (reason, result)
	assert pd.parse_plan("## Deferred scope\n" + "".join(f"- item {i}\n" for i in range(20)))["status"] == "partial"


def test_continuation_lines_join_the_item() -> None:
	result = pd.parse_plan("## Deferred scope\n- first part\n  second part\n* other\n")
	assert result["items"] == ["first part second part", "other"]


def test_sanitize_neutralises_references_markers_and_controls() -> None:
	value = pd.sanitize_item("Fix #123 for @user <!-- x --> \u202esecret\u200b /approved")
	assert "#123" not in value and "#\u2060123" in value
	assert "@user" not in value and "@\u2060user" in value
	assert "<!--" not in value and "-->" not in value
	assert "\u202e" not in value and "\u200b" not in value
	assert pd.sanitize_item("/reclarify now").startswith("\u2060/")
	# Same word-joiner rule as scripts/auto_decisions.py.
	auto_decisions = (REPO_ROOT / "scripts" / "auto_decisions.py").read_text(encoding="utf-8")
	assert 'ISSUE_REF_RE = re.compile(r"#(\\d)")' in auto_decisions
	assert pd.ISSUE_REF_RE.pattern == r"#(\d)"
	# Sanitizing twice changes nothing.
	assert pd.sanitize_item(value) == value


def test_pr_section_round_trip() -> None:
	items = pd.parse_plan(PLAN_4639)["items"]
	section = pd.pr_section(items, 4639)
	assert "<!-- ai:partial-delivery:v1 issue=4639 -->" in section
	body = "Automated implementation. Refs #4639\n" + section + "\n## Auto-decisions\n\n- **AD-1** x\n"
	result = pd.parse_pr_body(body, 4639)
	assert result["status"] == "partial"
	assert result["items"] == items


def test_pr_body_marker_must_match_the_issue_and_be_unique() -> None:
	section = pd.pr_section(["a"], 7)
	assert pd.parse_pr_body(section, 8)["status"] == "full"
	assert pd.parse_pr_body(section + section, 7)["reason"] == "duplicate_marker"
	assert pd.parse_pr_body("<!-- ai:partial-delivery:v1 issue=7 -->\n\nno section\n", 7)["status"] == "full"
	assert pd.parse_pr_body("Automated implementation. Closes https://x/issues/7\n", 7)["reason"] == "no_marker"
	assert pd.parse_pr_body(section.replace("\n", "\r\n"), 7)["status"] == "partial"


def test_remaining_comment_quotes_items_and_ends_with_the_marker() -> None:
	comment = pd.render_remaining_comment(["Implement the member homepage (Phase 2).", "Fix #12"], 4639, 4646)
	lines = comment.rstrip("\n").split("\n")
	assert lines[-1] == "<!-- ai:partial-delivery-remaining:v1 issue=4639 pr=4646 -->"
	quoted = [line for line in lines if line.startswith("> - ")]
	assert quoted == ["> - Implement the member homepage (Phase 2).", "> - Fix #\u206012"]
	assert "untrusted text" in comment
	assert not any(line.startswith("/") for line in lines)


def _cli(*args: str) -> subprocess.CompletedProcess:
	return subprocess.run([sys.executable, "-I", "-B", str(HELPER), *args], capture_output=True, text=True, check=False)


def test_cli_subcommands() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		plan = Path(tmp) / "plan.txt"
		plan.write_text(PLAN_4639, encoding="utf-8")
		status = _cli("plan-status", "--plan-file", str(plan))
		assert status.returncode == 0 and json.loads(status.stdout)["status"] == "partial"
		section = _cli("pr-section", "--plan-file", str(plan), "--issue", "4639")
		assert section.returncode == 0 and "issue=4639" in section.stdout
		body = Path(tmp) / "body.md"
		body.write_text("Automated implementation. Refs #4639\n" + section.stdout, encoding="utf-8")
		body_status = _cli("pr-body-status", "--body-file", str(body), "--issue", "4639")
		assert json.loads(body_status.stdout)["status"] == "partial"
		comment = _cli("remaining-comment", "--body-file", str(body), "--issue", "4639", "--pr", "4646")
		assert comment.stdout.rstrip().endswith("pr=4646 -->")
		missing = _cli("plan-status", "--plan-file", str(Path(tmp) / "absent.txt"))
		assert missing.returncode == 2
		assert _cli("bogus").returncode == 2


def test_plan_prompt_copies_carry_the_same_deferred_scope_rule() -> None:
	rules = []
	for relative in ("prompts/mode-plan.txt", "prompts/_templates/mode-plan.txt", "scripts/run_plan_codex.sh"):
		text = (REPO_ROOT / relative).read_text(encoding="utf-8")
		start = text.index("12. `## Deferred scope` (issue #7657)")
		end = text.index("\n\n", start)
		rules.append(text[start:end])
	assert rules[0] == rules[1] == rules[2]
	assert "never emit it\n   for an orchestrator-managed child issue" in rules[0]


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"OK: {len(tests)} partial-delivery helper tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
