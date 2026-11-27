#!/usr/bin/env python3
"""The implement prompt forbids tests that pin a specific upstream commit SHA.

Consumer repositories keep their `.github/workflows/ai-*.yml` wrappers in sync
with `shubhodeep1/coding-workflows@stable`, and every sync rewrites the
`uses: ...@<40-hex>` pins. An implement run in bitsafe.io wrote
`PINNED_SUPPORT_SHA = "420545bd50f77b44c59d1a7e8cc3b502fd8d1d6e"` into
test/ai-review-security-hold.test.mjs; the next sync moved the wrapper's pin
and the test failed on the sync PR (bitsafe.io #538, then #527 replaced the
constant with a `^[0-9a-f]{40}$` shape check). This pins the instruction that
tells the implementer to assert the shape instead.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_PROMPTS = (
	REPO_ROOT / "prompts" / "mode-implement.txt",
	REPO_ROOT / "prompts" / "_templates" / "mode-implement.txt",
)
# The prompt implement.yml actually sends is its inline
# `mode-implement-inline.txt` heredoc, not prompts/mode-implement.txt.
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
PINNED_SHA_RULE_PARTS = (
	"Pinned-SHA test discipline: never write a test that asserts a specific",
	"upstream commit SHA in a file the workflow sync manages",
	"`uses: shubhodeep1/coding-workflows/...@<40-hex>`",
	"Every `@stable` sync",
	"rewrites those pins, so the test fails on the next sync PR.",
	"Assert the shape\ninstead: the ref matches `^[0-9a-f]{40}$`",
	"read one from the file and compare it to the other.",
)


def test_implement_prompts_forbid_tests_that_pin_a_specific_upstream_sha() -> None:
	for prompt in IMPLEMENT_PROMPTS:
		text = prompt.read_text(encoding="utf-8")
		for part in PINNED_SHA_RULE_PARTS:
			assert part in text, f"{prompt.relative_to(REPO_ROOT)} lost: {part!r}"


def test_pinned_sha_rule_sits_before_the_verification_loop() -> None:
	for prompt in IMPLEMENT_PROMPTS:
		text = prompt.read_text(encoding="utf-8")
		assert text.index("Pinned-SHA test discipline:") < text.index("Verification stage order:"), prompt
		assert text.count("Pinned-SHA test discipline:") == 1, prompt


def _collapsed(text: str) -> str:
	return " ".join(text.split())


def _inline_implement_prompt() -> str:
	workflow = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	parts = workflow.split('cat > "${PROMPT_TEMPLATE_FILE}" <<\'EOF\'', 1)[1].split("\n          EOF\n", 1)
	assert len(parts) == 2, "inline implement prompt heredoc terminator not found"
	return parts[0]


def test_inline_implement_prompt_the_workflow_sends_carries_the_rule() -> None:
	inline = _inline_implement_prompt()
	flat = _collapsed(inline)
	for part in PINNED_SHA_RULE_PARTS:
		assert _collapsed(part) in flat, f"inline implement prompt lost: {part!r}"
	assert inline.count("Pinned-SHA test discipline:") == 1
	assert inline.index("Pinned-SHA test discipline:") < inline.index("Restrictions:")


if __name__ == "__main__":
	test_implement_prompts_forbid_tests_that_pin_a_specific_upstream_sha()
	test_pinned_sha_rule_sits_before_the_verification_loop()
	test_inline_implement_prompt_the_workflow_sends_carries_the_rule()
