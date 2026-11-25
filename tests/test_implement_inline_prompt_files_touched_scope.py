#!/usr/bin/env python3
"""The inline implement prompt keeps the editor inside the issue's files_touched scope.

An implement run for tele-funtoken-msg-scoring#5539 edited
`ft.games/tests/test_root_new_ui_routes.py`, which the issue's
`files_touched` list did not cover, so the commit-time scope guard refused the
change and latched `ai:scope-blocked` (heal #9775). The inline prompt that
`implement.yml` sends told the model to update every test listed under the
advisory Expected files section and carried no `files_touched` rule. This pins
the scope-qualified bullet and the Restrictions rule in that inline prompt.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"

FILES_TOUCHED_RULE = (
	"If the issue body lists `files_touched:` (machine or prose form), stay inside"
	" that set plus this issue's own `changelog.d/` and `agents.d/` fragments."
)
ADVISORY_SENTENCE = "The Expected files list is advisory and is not a scope."
# Heal-routed runs check the verified heal-scope marker, not files_touched
# (HEAL_SCOPE_FILE in the Preflight destructive-commit guard), so the prompt
# names that marker too.
HEAL_SCOPE_RULE = (
	"On a workflow heal issue, the `ai:workflow-heal-scope:v1` marker's `paths=` list is the scope"
	" instead, plus only this issue's own `agents.d/` fragment."
)
SCOPE_ACCEPTANCE_RULE = "If the issue names files only in its scope or acceptance criteria, stay inside those files."
OLD_UNCONDITIONAL_SENTENCE = (
	"Update the tests and registries listed under Expected files together with the"
	" code; a pre-push gate re-runs them."
)


def _scope_collapsed(text: str) -> str:
	return " ".join(text.split())


def _scope_inline_prompt() -> str:
	workflow = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	opener = 'cat > "${PROMPT_TEMPLATE_FILE}" <<\'EOF\''
	assert opener in workflow, f"inline implement prompt heredoc opener {opener!r} not found in {IMPLEMENT_WORKFLOW}"
	parts = workflow.split(opener, 1)[1].split("\n          EOF\n", 1)
	assert len(parts) == 2, "inline implement prompt heredoc terminator (10-space-indented EOF line) not found"
	return parts[0]


def _expected_files_bullet() -> str:
	inline = _scope_inline_prompt()
	start = inline.index("Update the tests and registries listed under Expected files together")
	end = inline.index("Pinned-SHA test discipline:", start)
	return inline[start:end]


def _restrictions_block() -> str:
	inline = _scope_inline_prompt()
	start = inline.index("Restrictions:")
	end = inline.index("<completeness_contract>", start)
	return inline[start:end]


def test_inline_prompt_restrictions_carry_the_files_touched_rule() -> None:
	flat = _scope_collapsed(_scope_inline_prompt())
	assert flat.count(FILES_TOUCHED_RULE) == 1, f"files_touched rule count {flat.count(FILES_TOUCHED_RULE)} != 1"
	assert ADVISORY_SENTENCE in flat, "advisory Expected-files sentence missing from the inline prompt"
	restrictions = _scope_collapsed(_restrictions_block())
	assert FILES_TOUCHED_RULE in restrictions, restrictions
	assert ADVISORY_SENTENCE in restrictions, restrictions


def test_inline_prompt_expected_files_bullet_is_scope_qualified() -> None:
	bullet = _scope_collapsed(_expected_files_bullet())
	for part in (
		"only those inside the issue's scope",
		"do not edit it",
		"the commit-time scope guard refuses out-of-scope paths",
		"report the conflict in your final summary",
	):
		assert part in bullet, f"Expected files bullet lost: {part!r}"


def test_inline_prompt_drops_the_unconditional_expected_files_sentence() -> None:
	assert OLD_UNCONDITIONAL_SENTENCE not in _scope_collapsed(_scope_inline_prompt()), "unconditional Expected-files sentence is back"


def test_inline_prompt_restrictions_carry_the_heal_scope_rule() -> None:
	restrictions = _scope_collapsed(_restrictions_block())
	assert HEAL_SCOPE_RULE in restrictions, restrictions
	assert SCOPE_ACCEPTANCE_RULE in restrictions, restrictions
	assert "files_touched and heal-scope rule under Restrictions" in _scope_collapsed(_expected_files_bullet())


def test_inline_prompt_scope_text_is_ascii() -> None:
	assert _expected_files_bullet().isascii(), "non-ASCII text in the Expected files bullet"
	assert _restrictions_block().isascii(), "non-ASCII text in the Restrictions block"


if __name__ == "__main__":
	test_inline_prompt_restrictions_carry_the_files_touched_rule()
	test_inline_prompt_expected_files_bullet_is_scope_qualified()
	test_inline_prompt_drops_the_unconditional_expected_files_sentence()
	test_inline_prompt_restrictions_carry_the_heal_scope_rule()
	test_inline_prompt_scope_text_is_ascii()
