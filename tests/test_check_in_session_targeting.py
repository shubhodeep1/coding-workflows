"""Contract for which session a §26 checker or fixer may rename or archive
(issue #4787).

The PR #4706 checker renamed its `notify` subscriber's session as well as
itself, and claimed an archive that never happened; the PR #4704 checker read
a delivered hand-back whose Routine the stale sweep had deleted as a gone
fixer and started a duplicate. These tests pin the instruction text that
prevents both, in CLAUDE.md §26 and in every command that archives a checker.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from claude_twin_state import assert_claude_not_ahead

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = ROOT / "CLAUDE.md"
OWN_ID = 'echo "session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}"'
ARCHIVED_ON_SUCCESS = "only after `archive_session` returned success"


def _flat(path: Path) -> str:
	return " ".join(path.read_text(encoding="utf-8").split())


def _command(prefix: str, name: str) -> Path:
	return ROOT / prefix / ".claude" / "commands" / name


def _section(text: str, start: str, end: str) -> str:
	begin = text.index(start)
	return text[begin:text.index(end, begin)]


@pytest.fixture(scope="module")
def claude_md() -> str:
	return _flat(CLAUDE_MD)


@pytest.fixture(scope="module")
def step_5(claude_md) -> str:
	return _section(claude_md, "5. **Hand-back check** (the 10-minute wake)", "### D) What the pushing session")


@pytest.fixture(scope="module")
def section_d(claude_md) -> str:
	return _section(claude_md, "### D) What the pushing session", "### E) Enforcement")


def test_checker_takes_its_own_id_from_bash(step_5):
	assert "**Which session you may rename:** this session only, whose id you take from Bash" in step_5
	assert OWN_ID in step_5
	assert "never from the instructions or the subscriber list" in step_5


def test_checker_never_targets_a_subscriber(step_5):
	assert "Never pass a subscriber's session id to `set_session_title` or `archive_session`" in step_5
	assert "The checker never archives itself either; the fixer archives it (§26.D)." in step_5


def test_checker_renames_use_the_own_id(step_5):
	assert "this session (`set_session_title` with your own id from Bash, as above)" in step_5
	assert "rename this session (your own id from Bash, as above) with the §26.D title" in step_5


def test_delivered_even_before_the_fixer_claims(step_5):
	assert "→ delivered, even when the fixer has not claimed the head yet." in step_5


def test_missing_routine_alone_is_not_a_gone_subscriber(step_5):
	assert "a missing Routine alone is not a gone subscriber" in step_5
	assert "the §26.G sweep deletes fired hand-backs" in step_5
	gone = step_5.index("the subscriber is gone; drop it.")
	# "the trigger is not found" must no longer sit in the gone list on its own.
	assert "the trigger is not found, or" not in step_5
	assert step_5.index("`get_session` shows the subscriber archived or not found") < gone


def test_fresh_fixer_rechecks_the_pr_first(step_5):
	recheck = step_5.index("first re-run step 1's command and start the fresh fixer only when `action` is still `hand_back_fixer`")
	assert recheck < step_5.index("**fresh fixer**: `create_session`")


def test_instructions_prompt_restates_the_session_rules(claude_md):
	step_3 = _section(claude_md, "3. Call `create_trigger` with `persistent_session_id` = the checker's", "4. Report the checker's session id")
	assert "The prompt also restates the session rules of §26.C step 5" in step_3
	assert "a missing Routine alone is not a gone subscriber (it calls `get_session` on the subscriber, and only an archived or not-found session is gone)" in step_3
	assert "it never archives itself (the fixer archives it, §26.D)" in step_3
	assert "is delivered, even when the fixer has not claimed the head yet" in step_3
	assert "starts one only when `action` is still `hand_back_fixer`" in step_3


def test_fixer_checks_the_checker_before_renaming_or_archiving(section_d):
	assert "**Check the target first:** call `get_session` on the checker id" in section_d
	assert "its title is exactly `PR #<n> status check-in` or already starts `PR #<n> merged — handed to ` or `PR #<n> closed — handed to `" in section_d
	assert "the id is not this session's own" in section_d
	assert OWN_ID in section_d
	assert "skip both calls and say so in one line" in section_d


def test_fixer_reports_archive_truthfully(section_d):
	assert f'Say "archived" {ARCHIVED_ON_SUCCESS}' in section_d
	assert "`archive failed: <error>`" in section_d


@pytest.mark.parametrize("prefix", ["workflow-templates"])  # the twin; .claude/ follows via the sync PR (CLAUDE.md §28.C)
def test_implement_plan_claude_checks_before_archiving_the_checker(prefix):
	text = _flat(_command(prefix, "implement-plan-claude.md"))
	check = _section(text, "### Archiving the project checker", "## Helpers")
	assert "Call `get_session` on the checker id and archive it only when its title contains `implement-plan <slug> — checker` (with or without a numbers prefix; [Session titles](#session-titles))" in check
	assert OWN_ID in check
	assert f'Say "archived" {ARCHIVED_ON_SUCCESS}' in check
	# Every place that archives the project checker points at the check.
	link = "[checker archive check](#archiving-the-project-checker)"
	assert text.count(link) == 3
	assert f"archive the project checker (the project has no more waits), and stop. The checker is archived only after the {link}." in text
	assert f"as the last action. The checker is archived only after the {link}." in text
	assert f"is not archived yet, and passes the {link}, archive it right after the new one is created" in text


@pytest.mark.parametrize("prefix", ["workflow-templates"])  # the twin; .claude/ follows via the sync PR (CLAUDE.md §28.C)
def test_implement_plan_checker_prompt_targets_no_session(prefix):
	text = _flat(_command(prefix, "implement-plan-claude.md"))
	prompt = _section(text, "### Checker prompt", "### Hand-back")
	assert "You rename and archive no existing session, yourself included: never call set_session_title, and call archive_session only in step 5, on the session you just created there when its create_trigger failed." in prompt
	assert "never call set_session_title or archive_session" not in prompt
	# The one archive_session call the intro allows is step 5's cleanup.
	step_5 = prompt[prompt.index("5. `action` is `next_stage`"):]
	assert "create_trigger fails (then archive_session the new session first)" in step_5
	assert "Do **not** archive yourself" in prompt
	step_4b = _section(prompt, "4b. Hand-back check", "5. `action` is `next_stage`")
	assert "a missing Routine alone is not a gone stage session" in step_4b
	assert "call get_session on <stage session id>; not archived → treat it as handed back (above)" in step_4b


@pytest.mark.parametrize("prefix", ["workflow-templates"])  # the twin; .claude/ follows via the sync PR (CLAUDE.md §28.C)
def test_fix_claude_pr_renames_only_its_own_session(prefix):
	text = _flat(_command(prefix, "fix-claude-pr.md"))
	assert "continue with CLAUDE.md §26.D instead, including its `get_session` title check" in text
	assert "Rename this session (`set_session_title` with your session id from step 0, never another session's)" in text
	assert "**Rename and archive only the right session.**" in text
	assert f'Say "archived" {ARCHIVED_ON_SUCCESS}' in text


def test_claude_issue_pickup_archives_only_the_checker_it_created():
	text = _flat(_command("", "claude-issue-pickup.md"))
	assert "passing only the id `create_session` returned in 5.2, never the script's `requester`" in text
	assert ARCHIVED_ON_SUCCESS in text


@pytest.mark.parametrize("name", ["implement-plan-claude.md", "fix-claude-pr.md"])
def test_twins_are_identical(name):
	"""`.claude/` is never ahead of its twin (CLAUDE.md §28.C; the twin may await its sync PR)."""
	assert_claude_not_ahead(f"commands/{name}")
