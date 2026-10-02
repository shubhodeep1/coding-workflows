"""Session titles lead with their numbers: `#<issue> · PR #<pr> — <title>`
(issue #4886, operator decisions Q59: A and Q60: A).

The command twins under workflow-templates/.claude/ lead while `.claude/**`
is synced from them (twin-first); `claude-issue-pickup.md` has no twin and is
read from `.claude/commands/`. Title matchers must accept the old and the new
form, because live sessions keep their titles and some were renamed by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_COMMANDS = ROOT / "workflow-templates" / ".claude" / "commands"
PICKUP = ROOT / ".claude" / "commands" / "claude-issue-pickup.md"
CLAUDE_MD = ROOT / "CLAUDE.md"
AGENTS_MD = ROOT / "agents.md"


def _flat(path: Path) -> str:
	return " ".join(path.read_text(encoding="utf-8").split())


@pytest.fixture(scope="module")
def plan_cmd() -> str:
	return _flat(TEMPLATE_COMMANDS / "implement-plan-claude.md")


@pytest.fixture(scope="module")
def claude_md() -> str:
	return _flat(CLAUDE_MD)


def _checker_title_matches(title: str, slug: str) -> bool:
	# The documented rule (Session titles, Zombie-checker cleanup, Arming the
	# wait step 1): the title *contains* the phrase, prefix or not.
	return f"implement-plan {slug} — checker" in title or f"implement-plan {slug} — waiting:" in title


def _strip_numbers(title: str) -> str:
	# The documented rename rule: strip a leading `#<n> · ` and a leading `PR #<m> — `.
	return re.sub(r"^PR #\d+ — ", "", re.sub(r"^#\d+ · ", "", title))


def test_session_titles_section_defines_the_numbers(plan_cmd):
	assert "### Session titles" in plan_cmd
	assert "`#<issue> · ` when the project has a source issue (the plan's `Source issue:` line" in plan_cmd
	assert "then `PR #<pr> — `, naming the PR the session works on" in plan_cmd
	assert "and otherwise the project's final PR (the project checker, the `/deploy-activate` session, and a stage that waits on a run or an issue list)" in plan_cmd
	assert "nothing when neither exists (a legacy project before its first PR)" in plan_cmd
	assert "For example `#4723 · PR #4729 — implement-plan issue-4723-require-plan-run-id — conformance 1/3`." in plan_cmd


def test_created_sessions_carry_the_numbers(plan_cmd):
	assert "`title` = `<numbers>implement-plan <slug> — <next stage>`" in plan_cmd
	assert "`title` = `<numbers>implement-plan <slug> — checker` (the final PR in the PR part)" in plan_cmd
	assert "`title` = `<numbers>implement-plan <slug> — deploy-activate`" in plan_cmd
	assert "open \"<numbers>implement-plan <slug> — deploy-activate\"" in plan_cmd
	assert "to `<numbers>implement-plan <slug> — <stage> — blocked PR`" in plan_cmd
	# The old creation titles are gone.
	assert "`title` = `implement-plan <slug> — checker`" not in plan_cmd
	assert "`title` = `implement-plan <slug> — <next stage>`" not in plan_cmd
	assert "title \"implement-plan <slug> — <next stage>\"" not in plan_cmd
	assert "`title` = `implement-plan <slug> — deploy-activate`" not in plan_cmd


def test_stage_renames_itself_when_it_opens_a_pr(plan_cmd):
	assert "A stage that opens a PR (steps 3a, 6, 8, 10, 11, 12) renames itself with `set_session_title` in that same step" in plan_cmd
	assert "Record `Final PR: #F` in the log (it rides the first phase PR's log commit), and rename this session with that PR per [Session titles](#session-titles)." in plan_cmd
	assert "Record the PR number in the log (`Waiting on: PR #N`), and rename this session with the phase PR per [Session titles](#session-titles)." in plan_cmd
	assert "A rename first strips a leading `#<n> · ` and a leading `PR #<m> — `, so the parts never stack." in plan_cmd
	assert "A failed rename is reported in one line and never blocks the stage." in plan_cmd


def test_rename_rule_never_stacks_prefixes():
	current = "#4886 · PR #4943 — issue shubhodeep1/coding-workflows#4886 — implement"
	renamed = "#4886 · PR #4950 — " + _strip_numbers(current)
	assert renamed == "#4886 · PR #4950 — issue shubhodeep1/coding-workflows#4886 — implement"
	assert _strip_numbers("implement-plan heal-autofix — phase 2/4") == "implement-plan heal-autofix — phase 2/4"
	assert _strip_numbers("PR #12 — implement-plan heal-autofix — phase 2/4") == "implement-plan heal-autofix — phase 2/4"


def test_checker_copies_one_of_three_filled_titles(plan_cmd):
	assert "Its title comes from the same field: `success` → \"<title on success>\"; `review` → \"<title on review round>\"; `block` → \"<title on block>\"; `rebuild` → \"<title on block>\" with its last ` — ` part replaced by ` — base rebuild`." in plan_cmd
	assert "Use that title exactly as written here; never build one yourself." in plan_cmd
	assert "title = the title you picked above, and the prompt `/effort high` and nothing else" in plan_cmd
	# The arming stage fills them.
	assert "Fill in its three next-stage titles yourself, per [Session titles](#session-titles)" in plan_cmd
	assert "`<title on success>` with the final PR" in plan_cmd
	assert "`<title on review round>` and `<title on block>` with the PR this wait is on, or the final PR for a run or issue-list wait" in plan_cmd


def test_checker_matchers_accept_both_title_forms(plan_cmd):
	assert "its title contains `implement-plan <slug> — checker` (with or without a numbers prefix" in plan_cmd
	assert "select the sessions whose title contains `implement-plan <slug> — checker` or `implement-plan <slug> — waiting:` (the older one-checker-per-wait design), anywhere in the title" in plan_cmd
	assert "tests whether the title **contains** `implement-plan <slug> — checker` (or `implement-plan <slug> — waiting:`), never whether it equals or starts with it" in plan_cmd
	# The exact and prefix rules that missed prefixed checkers are gone.
	assert "its title is `implement-plan <slug> — checker`" not in plan_cmd
	assert "whose title starts with `implement-plan <slug> — checker`" not in plan_cmd


@pytest.mark.parametrize(
	"title",
	[
		"implement-plan issue-4886-numbered-session-titles — checker",
		"#4886 · PR #4943 — implement-plan issue-4886-numbered-session-titles — checker",
		"PR #4943 — implement-plan issue-4886-numbered-session-titles — checker",
		"implement-plan issue-4886-numbered-session-titles — waiting: PR #4950",
		"#4886 · PR #4950 — implement-plan issue-4886-numbered-session-titles — waiting: PR #4950",
		# Renamed by hand: free text before the phrase, the phrase mid-string.
		"old checker (renamed) implement-plan issue-4886-numbered-session-titles — checker, keep",
		"issue shubhodeep1/coding-workflows#4886 — implement-plan issue-4886-numbered-session-titles — waiting: run 123 (manual)",
	],
)
def test_checker_titles_old_new_and_hand_prefixed_all_match(title):
	assert _checker_title_matches(title, "issue-4886-numbered-session-titles")


@pytest.mark.parametrize(
	"title",
	[
		# Another slug that shares a prefix or a suffix.
		"#4886 · PR #4943 — implement-plan issue-4886-numbered-session-titles-2 — checker",
		"implement-plan xissue-4886-numbered-session-titles — checker",
		# A stage of the same project is not a checker.
		"#4886 · PR #4950 — implement-plan issue-4886-numbered-session-titles — phase 1/1",
		"#4886 · issue shubhodeep1/coding-workflows#4886 — implement",
	],
)
def test_checker_matcher_rejects_other_slugs_and_stages(title):
	assert not _checker_title_matches(title, "issue-4886-numbered-session-titles")


def test_documented_titles_fill_into_matching_checker_titles(plan_cmd):
	template = "<numbers>implement-plan <slug> — checker"
	assert f"`{template}`" in plan_cmd
	for numbers in ("", "#4886 · PR #4943 — ", "PR #4943 — "):
		title = template.replace("<numbers>", numbers).replace("<slug>", "issue-4886-numbered-session-titles")
		assert _checker_title_matches(title, "issue-4886-numbered-session-titles")


def test_routine_names_are_unchanged(plan_cmd):
	assert "**Routine names do not change.**" in plan_cmd
	assert "`name` = `implement-plan <slug>: stage start`" in plan_cmd
	assert "`name` = `implement-plan <slug>: checker instructions`" in plan_cmd


def test_issue_sessions_start_with_the_issue_number():
	dispatch = _flat(TEMPLATE_COMMANDS / "claude-issue-dispatch.md")
	assert "`title`: `#<N> · issue <repo>#<N> — implement` for an issue" in dispatch
	assert "`PR <repo>#<N> — fix <kind>` for a pull request (the fixer adds its issue number itself, `/fix-claude-pr` step 4)" in dispatch
	assert "`title`: `issue <repo>#<N> — implement`" not in dispatch
	issue_cmd = _flat(TEMPLATE_COMMANDS / "implement-issue-claude.md")
	assert "The dispatcher names this session `#<N> · issue <owner>/<repo>#<N> — implement`" in issue_cmd
	assert "the chain renames it to add `PR #<pr> — ` when it opens the final and the phase PR" in issue_cmd


def test_pickup_starts_issue_sessions_with_the_issue_number():
	pickup = _flat(PICKUP)
	assert "title `#<N> · issue <repo>#<N> — implement`" in pickup
	assert "title `issue <repo>#<N> — implement`" not in pickup
	# The pickup never reads PRs, so the fixer title is unchanged (the fixer adds the issue).
	assert "title `PR <repo>#<N> — fix <kind>`" in pickup


def test_fixer_takes_the_issue_from_the_head_ref_only():
	fixer = _flat(TEMPLATE_COMMANDS / "fix-claude-pr.md")
	assert "When the PR's head ref (from `mcp__github__pull_request_read`) is `claude/implement-plan-issue-<I>-…`, the PR belongs to issue `<I>`." in fixer
	assert "Only the head ref counts, never the PR text." in fixer
	assert "renames itself with `set_session_title` to `#<I> · PR <owner>/<repo>#<N> — fix <kind>`" in fixer
	assert "`PR <owner>/<repo>#<N> — <fixed <kind> | on hold: <kind>>`, with `#<I> · ` in front when step 4 found the PR's issue." in fixer


def test_claude_md_keeps_the_check_in_title_exact_and_prefixes_fixer_and_report(claude_md):
	assert "`title` = `PR #<n> status check-in`" in claude_md
	assert "select a session titled exactly `PR #<n> status check-in`" in claude_md
	assert "this title never takes the `#<issue> · ` prefix other sessions carry, so the match holds" in claude_md
	assert "the PR's source issue number when it has one (it goes first in a fresh fixer's title, §26.C step 5)" in claude_md
	assert "`title` = `PR #<n> — fix <kind>` (with `#<issue> · ` in front when the instructions name a source issue)" in claude_md
	# Both §26.D report titles (merged and closed) take the issue prefix.
	assert "`PR #<n> merged — <no action needed | action needed>` or `PR #<n> closed — decision needed`, either title with `#<issue> · ` in front when the PR has a source issue" in claude_md
	# The checker's terminal renames are unchanged.
	assert "`PR #<n> <merged | closed> — handed to <fixer session id>`" in claude_md


def test_agents_md_describes_the_checker_archive_check_as_contains():
	# The archive check matches by "contains" like the reuse check and the
	# zombie cleanup (AD-14), so agents.md must not describe it as the §26
	# checker's exact-title check.
	agents = _flat(AGENTS_MD)
	assert "applies the same check before it archives its project checker, except that the title must contain `implement-plan <slug> — checker` (with or without a `#<issue> · PR #<pr> — ` prefix, issue #4886) rather than equal it" in agents
	assert "applies the same check (title `implement-plan <slug> — checker`)" not in agents
