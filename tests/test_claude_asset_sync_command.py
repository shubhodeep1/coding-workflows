"""Contract for the Claude-asset sync (issue #4952): stage, resume, and fixer
sessions merge the default branch into their working branch when it holds
`.claude/hooks/**` or `.claude/settings.json` changes the branch lacks, and a
`.claude/` conflict is a §28.C stop instead of a guess.

Both the in-repo commands and their `workflow-templates/.claude/` twins are
checked; the twins must stay byte-identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIRS = (ROOT / ".claude" / "commands", ROOT / "workflow-templates" / ".claude" / "commands")
NAMES = ("implement-plan-claude.md", "implement-issue-claude.md", "fix-claude-pr.md")


def _flat(path: Path) -> str:
	return " ".join(path.read_text(encoding="utf-8").split())


def _section(text: str, start: str, end: str) -> str:
	assert start in text, start
	return text.split(start, 1)[1].split(end, 1)[0]


@pytest.mark.parametrize("name", NAMES)
def test_twins_are_byte_identical(name):
	assert (DIRS[0] / name).read_bytes() == (DIRS[1] / name).read_bytes()


@pytest.fixture(params=DIRS, ids=lambda path: str(path.relative_to(ROOT)))
def commands(request) -> dict[str, str]:
	return {name: _flat(request.param / name) for name in NAMES}


def test_sync_procedure_is_defined_once_in_helpers(commands):
	plan = commands["implement-plan-claude.md"]
	section = _section(plan, "### Claude-asset sync", "### Permission prompt report")
	assert "git fetch origin <default>" in section
	assert "git diff --quiet HEAD...origin/<default> -- .claude/hooks .claude/settings.json" in section
	assert "only changes the default branch holds and the working branch lacks" in section
	assert "Never rebase or force-push" in section
	assert "[claude-asset-sync] merge <source> for .claude/ guard updates" in section
	assert "no GitHub API calls" in section
	assert plan.count("### Claude-asset sync") == 1


def test_sync_only_merges_into_branches_that_land_in_the_default_branch(commands):
	section = _section(commands["implement-plan-claude.md"], "### Claude-asset sync", "### Permission prompt report")
	assert "skip the merge" in section
	assert "claude_assets=stale (base <base>)" in section
	assert "sync the project branch first exactly as step 2 does, then merge `origin/<project branch>` into the PR head" in section


def test_claude_conflict_aborts_and_blocks(commands):
	section = _section(commands["implement-plan-claude.md"], "### Claude-asset sync", "### Permission prompt report")
	assert "git diff --name-only --diff-filter=U" in section
	assert "`git merge --abort` and stop with the caller's blocker" in section
	assert "git log --oneline --left-right HEAD...origin/<source> -- <file>" in section
	assert "CLAUDE.md §28.C" in section
	assert "`ai:claude-blocked:v1` comment on the issue" in section
	assert "$(" not in section


def test_settings_change_applies_from_the_next_session(commands):
	section = _section(commands["implement-plan-claude.md"], "### Claude-asset sync", "### Permission prompt report")
	assert "Hook scripts are re-read on every call" in section
	assert "new hook wiring applies from the next session" in section


def test_every_checkout_runs_the_sync(commands):
	plan = commands["implement-plan-claude.md"]
	step2 = _section(plan, "**Sync the project branch**", "- **Context.**")
	assert "[Claude-asset sync](#claude-asset-sync)" in step2
	assert "a conflict under `.claude/` is never resolved here" in step2
	step7 = _section(plan, "- **Blocked**", "7a. **Review round")
	assert "run the [Claude-asset sync](#claude-asset-sync) on it" in step7
	step7a = _section(plan, "7a. **Review round", "- **`kind=conflict`**")
	assert "run the [Claude-asset sync](#claude-asset-sync) on it before any fix" in step7a


def test_resume_and_fixer_run_the_sync(commands):
	issue = _section(commands["implement-issue-claude.md"], "4. **Resume, never duplicate.**", "5. **Read project context.**")
	assert "implement-plan-claude.md#claude-asset-sync" in issue
	fixer = _section(commands["fix-claude-pr.md"], "5. **Fix it.**", "- **`claude/implement-plan-*` head**")
	assert "implement-plan-claude.md#claude-asset-sync" in fixer
	assert "before any fix" in fixer
	assert "--kind hold" in fixer
	assert "<!-- ai:claude-blocked:v1 -->" in fixer
	assert "PushNotification" in fixer
