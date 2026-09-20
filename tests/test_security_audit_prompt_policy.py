#!/usr/bin/env python3
"""Pin the security-pass mitigation policy: findings must recommend
automated controls, never human gates.

Background
----------
Security-pass finding `prompt-injection-authorizes-pr-merge` (issue #4017,
project #3965) recommended "independently authenticated human approval
before merging or closing PRs". The consolidated fix issue carried that
recommendation verbatim, the implementer followed it literally (PR #4029),
and every terminal review-blocked judge decision on the integration branch
then waited for a maintainer to post `/review-blocked-approve` — the exact
human-in-the-loop that CLAUDE.md §18 / unattended_system_instructions.md
§20 exist to prevent. PR #4079 sat pending for eleven hours behind it.

The contract
------------
1. `prompts/mode-security-audit.txt` and its `_templates/` source carry a
   `Mitigation policy:` block that forbids human-gate recommendations and
   names the `HUMAN GATE REQUIRED:` escape prefix.
2. `prompts/mode-judge-security-pass-exhaustion.txt` and its template carry
   the same rule for `keep_fixing` / `accept_with_followup`.
3. The consolidated fix-issue body and the advisory follow-up body composed
   in `scripts/orchestrate_poll_process.sh` carry a `### Mitigation policy`
   section that repeats the rule for the implementer.

Byte parity between each prompt and its template is covered by
tests/test_assemble_prompt.py; this file checks the policy text itself.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"

AUDIT_PROMPTS = (
	PROMPTS_DIR / "mode-security-audit.txt",
	PROMPTS_DIR / "_templates" / "mode-security-audit.txt",
)
EXHAUSTION_PROMPTS = (
	PROMPTS_DIR / "mode-judge-security-pass-exhaustion.txt",
	PROMPTS_DIR / "_templates" / "mode-judge-security-pass-exhaustion.txt",
)

HUMAN_GATE_PREFIX = "`HUMAN GATE REQUIRED:`"
AUDIT_POLICY_BULLETS = (
	"- Recommendations must preserve unattended operation. Do not recommend a human approval step, an operator-run command, a manual sign-off, or any control whose normal path needs a person as the mitigation for a finding.",
	"- Recommend deterministic, automated controls: identity- and provenance-scoped authorization (who authored or commented, fork versus same-repository head, protected paths), fail-closed validation, least-privilege tokens, sandboxing, and treating author-controlled text as data rather than instructions.",
	"- Only when no automated control can mitigate the finding may the recommendation involve a person. Such a recommendation must start with `HUMAN GATE REQUIRED:` and name the narrowest trigger condition under which a person is consulted, so the planner scopes the gate to that condition and not to the whole workflow.",
)


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def test_audit_prompt_and_template_carry_mitigation_policy() -> None:
	for path in AUDIT_PROMPTS:
		text = _read(path)
		assert "\nMitigation policy:\n" in text, f"{path}: missing `Mitigation policy:` block"
		for bullet in AUDIT_POLICY_BULLETS:
			assert bullet in text, f"{path}: missing bullet {bullet[:60]!r}"
		# The block must precede the output contract so it is read as a rule,
		# not as an afterthought after the JSON example.
		assert text.index("Mitigation policy:") < text.index("Output contract:"), path


def test_audit_prompt_policy_block_sits_after_rules() -> None:
	for path in AUDIT_PROMPTS:
		text = _read(path)
		rules_idx = text.index("\nRules:\n")
		policy_idx = text.index("\nMitigation policy:\n")
		assert rules_idx < policy_idx, path
		between = text[rules_idx:policy_idx]
		assert "If no high-quality findings are supported, return `[]`." in between, (
			f"{path}: the policy block must follow the existing Rules list"
		)


def test_exhaustion_judge_prompt_and_template_carry_mitigation_policy() -> None:
	for path in EXHAUSTION_PROMPTS:
		text = _read(path)
		assert "- Mitigation policy for `keep_fixing` and `accept_with_followup`:" in text, path
		assert "Do not prescribe a human approval step, an operator-run command, a manual sign-off" in text, path
		assert HUMAN_GATE_PREFIX in text, path
		# Inside the Rules list, ahead of the read-only rule that closes it.
		assert text.index("- Mitigation policy for") < text.index("- Never edit, commit, or push."), path


def _python_heredoc_lines(anchor: str) -> str:
	src = _read(POLLER_SCRIPT)
	idx = src.index(anchor)
	end = src.index("body_path.write_text(", idx)
	return src[idx:end]


def test_consolidated_fix_issue_body_carries_mitigation_policy() -> None:
	block = _python_heredoc_lines(
		"The mandatory project security pass found the following blocking issues."
	)
	assert '"### Mitigation policy",' in block
	assert "Implement each mitigation as an automated, deterministic control (unattended_system_instructions.md §20, Automation Bias)." in block
	assert HUMAN_GATE_PREFIX in block
	# The policy must come after the defect-class instruction and before the
	# findings table is appended (the table is extended after the list).
	assert block.index("Fix every instance of each finding's defect class") < block.index('"### Mitigation policy",')


def test_advisory_followup_body_carries_mitigation_policy() -> None:
	block = _python_heredoc_lines('"### Recommendation",')
	assert '"### Mitigation policy",' in block
	assert "Implement the mitigation as an automated, deterministic control (unattended_system_instructions.md §20, Automation Bias)." in block
	assert HUMAN_GATE_PREFIX in block
	assert block.index('"### Recommendation",') < block.index('"### Mitigation policy",')


def test_human_gate_prefix_is_spelled_identically_everywhere() -> None:
	"""One literal prefix so the planner and tests can grep for it."""
	pattern = re.compile(r"HUMAN GATE REQUIRED:")
	for path in (*AUDIT_PROMPTS, *EXHAUSTION_PROMPTS, POLLER_SCRIPT):
		assert pattern.search(_read(path)), path
		assert "HUMAN GATE REQUIRED " not in _read(path).replace("HUMAN GATE REQUIRED:", ""), (
			f"{path}: prefix must always carry the trailing colon"
		)


def main() -> int:
	import unittest

	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except unittest.SkipTest as exc:
			print(f"  SKIP  {name}: {exc}")
		except Exception as exc:  # noqa: BLE001 - report every failure
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
