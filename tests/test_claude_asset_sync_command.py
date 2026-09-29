"""Contract for the Claude-asset sync (issue #4952): stage, resume, and fixer
sessions merge the default branch into their working branch when it holds
`.claude/hooks/**` or `.claude/settings.json` changes the branch lacks, and a
`.claude/` conflict is a §28.C stop instead of a guess.

Both the in-repo commands and their `workflow-templates/.claude/` twins are
checked; the twins must stay byte-identical.
"""

from __future__ import annotations

import os
import re
import subprocess
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
	assert "git merge --no-edit" not in section
	assert "HEAD@{1}" not in section
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
	assert "A conflict under `.claude/` is never resolved here" in step2
	# Step 2's merge stands in for the sync's step 4 and keeps its own subject.
	assert "It takes the place of that section's step 4 and keeps the command and subject above" in step2
	assert "the `[claude-asset-sync]` subject marks only the sync merge into a PR head" in step2
	section = _section(plan, "### Claude-asset sync", "### Permission prompt report")
	assert "on the project branch itself, step 2's merge is the sync merge and keeps step 2's command and subject" in section
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


def _git_env(home: Path) -> dict[str, str]:
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	env.update({
		"HOME": str(home),
		"GIT_CONFIG_NOSYSTEM": "1",
		"GIT_AUTHOR_NAME": "test",
		"GIT_AUTHOR_EMAIL": "test@example.invalid",
		"GIT_COMMITTER_NAME": "test",
		"GIT_COMMITTER_EMAIL": "test@example.invalid",
		"GIT_TERMINAL_PROMPT": "0",
	})
	return env


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
	return subprocess.run(
		["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
	).stdout.strip()


def _commit(repo: Path, env: dict[str, str], rel: str, content: str, message: str) -> None:
	path = repo / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content, encoding="utf-8")
	_git(repo, env, "add", rel)
	_git(repo, env, "commit", "-q", "-m", message)


def _documented_command(section: str, pattern: str) -> str:
	match = re.search(pattern, section)
	assert match, pattern
	return match.group(1).replace("<source>", "main")


@pytest.mark.parametrize("own_commit", (True, False), ids=("diverged-branch", "fast-forwardable-branch"))
@pytest.mark.parametrize("directory", DIRS, ids=lambda path: str(path.relative_to(ROOT)))
def test_documented_merge_and_settings_commands_behave_as_described(directory: Path, own_commit: bool, tmp_path: Path):
	"""Run steps 4 and 6 exactly as written, in a scratch repository with the reflog off."""
	section = _section(_flat(directory / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	merge = _documented_command(section, r"`(git merge [^`]*origin/<source>)`")
	settings = _documented_command(section, r"`(git diff --name-only [^`]*-- \.claude/settings\.json)`")
	home = tmp_path / "home"
	home.mkdir()
	env = _git_env(home)
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, env, "init", "-q", "-b", "main")
	_commit(origin, env, ".claude/settings.json", "{}\n", "settings")
	clone = tmp_path / "clone"
	_git(tmp_path, env, "clone", "-q", str(origin), str(clone))
	_git(clone, env, "config", "core.logAllRefUpdates", "false")
	_git(clone, env, "checkout", "-q", "-b", "work")
	if own_commit:
		_commit(clone, env, "work.md", "x\n", "branch work")
	_commit(origin, env, ".claude/settings.json", '{"hooks": {}}\n', "wiring")
	_git(clone, env, "fetch", "-q", "origin", "main")
	subprocess.run(["bash", "-c", merge], cwd=clone, env=env, check=True, capture_output=True, text=True)
	assert _git(clone, env, "log", "-1", "--format=%s") == "[claude-asset-sync] merge main for .claude/ guard updates"
	assert len(_git(clone, env, "log", "-1", "--format=%P").split()) == 2
	changed = subprocess.run(["bash", "-c", settings], cwd=clone, env=env, check=True, capture_output=True, text=True)
	assert changed.stdout.strip() == ".claude/settings.json"
