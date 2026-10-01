"""Contract for the Claude-asset sync (issue #4952): stage, resume, and fixer
sessions merge the default branch into their working branch when it holds
`.claude/hooks/**` or `.claude/settings.json` changes the branch lacks, and a
`.claude/` conflict is a §28.C stop instead of a guess.

Both the in-repo commands and their `workflow-templates/.claude/` twins are
checked; the twins must stay byte-identical.
"""

from __future__ import annotations

import hashlib
import json
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


def test_claude_conflict_aborts_and_blocks(commands):
	section = _section(commands["implement-plan-claude.md"], "### Claude-asset sync", "### Permission prompt report")
	assert "git diff --name-only --diff-filter=U" in section
	assert "`git merge --abort` and stop with the caller's blocker" in section
	assert "git log --oneline --left-right HEAD...origin/<source> -- <file>" in section
	assert "CLAUDE.md §28.C" in section
	assert "`ai:claude-blocked:v1` comment on the issue" in section
	assert "$(" not in section


def test_settings_change_is_verified_before_any_further_work(commands):
	"""Issue #5259: a merged settings.json is checked, never assumed active."""
	section = _section(commands["implement-plan-claude.md"], "### Claude-asset sync", "### Settings restart")
	assert "Hook scripts are re-read on every call" in section
	# The old claim contradicted Claude Code's documented file watcher.
	assert "reads `settings.json` only at session start" not in section
	assert "new hook wiring applies from the next session" not in section
	assert "the watcher can miss a change" in section
	assert "PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/loaded_settings_check.py" in section
	# A branch that predates the recorder cannot record the merged file's reload
	# (ConfigChange runs the hooks loaded before a change); the check says so.
	assert "PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/loaded_settings_check.py --before HEAD^1" in section
	assert "`\"before_recorder_wired\": false` means the branch predates the recorder" in section
	assert "Claude Code runs `ConfigChange` with the hooks loaded before a change" in section
	assert "as its own Bash call before any other tool call" in section
	assert "including no record, which fails closed" in section
	assert "run it once more" in section
	assert "make no further commit, push, claim, comment, label change, or dispatch for this work" in section
	assert "[Settings restart](#settings-restart)" in section
	# Step 4 never pushes the sync merge ahead of the step 6 check (PR #5511 review round 1).
	assert "Push it with the push the caller already uses" not in section
	assert "Do not push it on its own: the caller's own push carries it, and never before step 6 below has confirmed the settings when the merge changed `.claude/settings.json`" in section
	assert "$(" not in section


def test_settings_restart_hands_the_work_to_a_fresh_session_once(commands):
	plan = commands["implement-plan-claude.md"]
	assert plan.count("### Settings restart") == 1
	restart = _section(plan, "### Settings restart", "### Permission prompt report")
	assert "The sync merge stays local and unpushed" in restart
	assert "No push, commit, claim, comment, label change, dispatch, or wait." in restart
	# One restart per stage; a second miss is a failure escalation.
	assert "do not restart again: that is a failure escalation (CLAUDE.md §28.C), never auto-decided" in restart
	assert "`ai:claude-blocked:v1` comment on the issue" in restart
	assert "post a hold claim" in restart
	# This command: same stage, fresh session, marked as a restart.
	assert "[two-step start](#two-step-start)" in restart
	assert "`implement-plan <slug> — <stage> — settings restart`" in restart
	assert "`Asset-sync restart: <this session's id>`" in restart
	assert "`Previous stage session:` = this session's id" in restart
	# /fix-claude-pr: fresh fixer that looks past this session's claim.
	assert "/fix-claude-pr <PR URL> — kind <kind> — head <head_sha> — claim <this session's id> — settings restart" in restart
	assert "`PR #<N> status check-in: fixer start`" in restart
	assert "never continue here instead" in restart
	assert "$(" not in restart
	template = _section(plan, "## Stage Sessions", "### Claims")
	assert "<Asset-sync restart: <session id>, only in a [settings restart](#settings-restart)>" in template
	claims = _section(plan, "### Claims", "### Two-step start")
	assert "add `--ignore-claim-by <id>` for the `Asset-sync restart:` session, whose claim you inherit" in claims
	assert "a fixer, and a [settings restart](#settings-restart) of its own stage." in plan


def test_fixer_routes_an_unconfirmed_settings_load_to_the_restart(commands):
	fixer = commands["fix-claude-pr.md"]
	step5 = _section(fixer, "5. **Fix it.**", "- **`claude/implement-plan-*` head**")
	assert "do not fix or push here: follow its [Settings restart](.claude/commands/implement-plan-claude.md#settings-restart)" in step5
	assert "`— claim <session id> — settings restart`" in fixer


def test_every_checkout_runs_the_sync(commands):
	plan = commands["implement-plan-claude.md"]
	step2 = _section(plan, "**Sync the project branch**", "- **Context.**")
	assert "[Claude-asset sync](#claude-asset-sync)" in step2
	assert "A conflict under `.claude/` is never resolved here" in step2
	# Step 2's merge stands in for the sync's step 4 and keeps its own subject.
	assert "It takes the place of that section's step 4 and keeps the command and subject above" in step2
	assert "the `[claude-asset-sync]` subject marks only the sync merge into a PR head" in step2
	# Issue #5259: step 2 pushes its merge itself, so the settings check must come first.
	assert "When the merge changed `.claude/settings.json`, run the sync's step 6 check right after the merge commit, before the repo's checks and before this push; when it ends in a [Settings restart](#settings-restart), nothing is pushed." in step2
	section = _section(plan, "### Claude-asset sync", "### Permission prompt report")
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


# Issue #5258: a sync merge that conflicts only outside `.claude/` is resolved
# inside that merge (or aborted and stopped), never aborted to let the fix go on
# under the old guards. These read the `workflow-templates/.claude/` twins: the
# `.claude/` copies follow through the twin sync, and
# `test_twins_are_byte_identical` covers them from then on.
TWIN_DIR = DIRS[1]


def test_outside_claude_conflict_never_continues_on_the_unsynced_head():
	section = _section(_flat(TWIN_DIR / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	assert "continues its fix" not in section
	assert "claude_assets=stale (conflict outside .claude/)" not in section
	assert "On a PR head, resolve it inside the sync merge." in section
	assert "The source is always the PR's base (step 3)" in section
	assert "Git has already written the cleanly merged `.claude/` files into the working tree" in section
	assert "`git commit --no-edit`, which keeps the step 4 subject" in section
	assert "is never aborted so that the work can go on" not in section
	assert "is resolved, never aborted to continue on the unsynced head" in section
	assert "On the project branch, Procedure step 2 resolves it as before." in section
	assert "run `git merge --abort` and stop the way the caller stops on a conflict it cannot resolve" in section
	assert "Never continue the fix on the unsynced head." in section
	fixer = _section(_flat(TWIN_DIR / "fix-claude-pr.md"), "5. **Fix it.**", "- **`claude/implement-plan-*` head**")
	assert "the fix continues on the unsynced head" not in fixer
	assert "is resolved inside the sync merge, never aborted to continue on the unsynced head" in fixer
	assert "`git commit --no-edit` (the sync's subject stays)" in fixer
	assert "For `conflict` that merge is the conflict fix" in fixer
	assert "run `git merge --abort`, post a hold claim, ask in the §2 format which side wins, send one `PushNotification`, and end the turn" in fixer


def test_sync_names_procedure_step_2_not_its_own_step_2():
	"""Inside the Claude-asset sync list a bare "step 2" is its own `Stale?`
	step, so references to the project-branch sync say "Procedure step 2"
	(PR #5281 review round 1)."""
	section = _section(_flat(TWIN_DIR / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	assert "sync the project branch first exactly as Procedure step 2 does, then merge `origin/<project branch>` into the PR head" in section
	assert "on the project branch itself, Procedure step 2's merge is the sync merge and keeps that step's command and subject" in section
	assert "exactly as step 2 does" not in section
	assert "step 2's command" not in section


def test_outside_conflict_resolution_runs_under_the_merged_guards(tmp_path: Path):
	"""A sync merge stopped on a conflict outside `.claude/` already has the
	default branch's hook in the working tree, and resolving it with
	`git commit --no-edit` keeps the sync subject."""
	section = _section(_flat(TWIN_DIR / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	merge = _documented_command(section, r"`(git merge [^`]*origin/<source>)`")
	assert "`git commit --no-edit`" in section
	home = tmp_path / "home"
	home.mkdir()
	env = _git_env(home)
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, env, "init", "-q", "-b", "main")
	_commit(origin, env, ".claude/hooks/guard.sh", "old guard\n", "guard")
	_commit(origin, env, "work.md", "base\n", "work")
	clone = tmp_path / "clone"
	_git(tmp_path, env, "clone", "-q", str(origin), str(clone))
	_git(clone, env, "config", "core.logAllRefUpdates", "false")
	_git(clone, env, "checkout", "-q", "-b", "work")
	_commit(clone, env, "work.md", "branch side\n", "branch work")
	_commit(origin, env, ".claude/hooks/guard.sh", "new guard\n", "guard fix")
	_commit(origin, env, "work.md", "main side\n", "main work")
	_git(clone, env, "fetch", "-q", "origin", "main")
	stopped = subprocess.run(["bash", "-c", merge], cwd=clone, env=env, capture_output=True, text=True)
	assert stopped.returncode != 0
	assert _git(clone, env, "diff", "--name-only", "--diff-filter=U") == "work.md"
	assert (clone / ".claude" / "hooks" / "guard.sh").read_text(encoding="utf-8") == "new guard\n"
	(clone / "work.md").write_text("branch side\nmain side\n", encoding="utf-8")
	_git(clone, env, "add", "work.md")
	_git(clone, env, "commit", "--no-edit")
	assert _git(clone, env, "log", "-1", "--format=%s") == "[claude-asset-sync] merge main for .claude/ guard updates"
	assert len(_git(clone, env, "log", "-1", "--format=%P").split()) == 2
	assert _git(clone, env, "show", "HEAD:.claude/hooks/guard.sh") == "new guard"


@pytest.mark.parametrize("own_commit", (True, False), ids=("diverged-branch", "fast-forwardable-branch"))
def test_project_branch_sync_merge_never_hides_a_settings_change_from_step_6(own_commit: bool, tmp_path: Path):
	"""Procedure step 2's merge, run exactly as written, keeps `HEAD^1` the branch
	before the merge. A fast-forward would move `HEAD^1` to the parent of the last
	upstream commit, and step 6 would miss a `settings.json` change in an earlier
	one (PR #5511 review round 1 on bfab7b4). Reads the twin until the twin sync;
	`test_twins_are_byte_identical` covers the `.claude/` copy from then on."""
	plan = _flat(TWIN_DIR / "implement-plan-claude.md")
	step_2 = _section(plan, "**Sync the project branch**", "This and the step 3a log commit")
	merge = re.search(r"`(git merge [^`]*origin/<default>)`", step_2)
	assert merge, step_2
	merge_command = merge.group(1).replace("<default>", "main")
	section = _section(plan, "### Claude-asset sync", "### Permission prompt report")
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
	_git(clone, env, "checkout", "-q", "-b", "claude/implement-plan-demo")
	if own_commit:
		_commit(clone, env, "docs/implement-plan/demo.md", "log\n", "log")
	before = _git(clone, env, "rev-parse", "HEAD")
	_commit(origin, env, ".claude/settings.json", '{"hooks": {}}\n', "wiring")
	_commit(origin, env, "later.md", "x\n", "a later commit that leaves settings.json alone")
	_git(clone, env, "fetch", "-q", "origin", "main")
	subprocess.run(["bash", "-c", merge_command], cwd=clone, env=env, check=True, capture_output=True, text=True)
	assert len(_git(clone, env, "log", "-1", "--format=%P").split()) == 2
	assert _git(clone, env, "rev-parse", "HEAD^1") == before
	changed = subprocess.run(["bash", "-c", settings], cwd=clone, env=env, check=True, capture_output=True, text=True)
	assert changed.stdout.strip() == ".claude/settings.json"
	# An up-to-date branch still gets no commit.
	head = _git(clone, env, "rev-parse", "HEAD")
	subprocess.run(["bash", "-c", merge_command], cwd=clone, env=env, check=True, capture_output=True, text=True)
	assert _git(clone, env, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("loaded", ("merged", "pre-merge"))
def test_outside_conflict_checks_the_merged_settings_before_any_resolution(loaded: str, tmp_path: Path):
	"""A sync merge stopped by a conflict outside `.claude/` has no merge commit
	yet, so step 6's `HEAD^1` check cannot run; step 5 runs the same check with
	`--before HEAD` before anything is resolved, and Procedure step 2 and
	`/fix-claude-pr` defer to it (PR #5511 review round 2 on d192dbc). Runs the
	documented commands mid-merge. Reads the twins until the twin sync;
	`test_twins_are_byte_identical` covers the `.claude/` copies from then on."""
	plan = _flat(TWIN_DIR / "implement-plan-claude.md")
	section = _section(plan, "### Claude-asset sync", "### Permission prompt report")
	step5 = _section(section, "5. **Conflict.**", "6. **Settings.**")
	assert "the resolution runs under the current guards" not in step5
	assert step5.index("loaded_settings_check.py --before HEAD`") < step5.index("`git add` the resolved files")
	assert "as its own Bash call before any other tool call" in step5
	assert "resolve, `git add`, and commit nothing, and follow [Settings restart](#settings-restart)" in step5
	step2 = _section(plan, "**Sync the project branch**", "This and the step 3a log commit")
	assert step2.index("step 5 settings check (`--before HEAD`)") < step2.index("then resolve it keeping both sides' intent")
	fixer = _section(_flat(TWIN_DIR / "fix-claude-pr.md"), "5. **Fix it.**", "- **`claude/implement-plan-*` head**")
	assert fixer.index("run the sync's step 5 settings check (`--before HEAD`) before resolving anything") < fixer.index("`git add` the resolved files")
	assert "When the sync's step 5 or step 6 check cannot confirm" in fixer
	merge = _documented_command(section, r"`(git merge [^`]*origin/<source>)`")
	changed = re.search(r"`(git diff --quiet HEAD -- \.claude/settings\.json)`", step5)
	check = re.search(r"`(PYTHONDONTWRITEBYTECODE=1 python3 \.claude/scripts/loaded_settings_check\.py --before HEAD)`", step5)
	assert changed and check, step5
	home = tmp_path / "home"
	home.mkdir()
	env = _git_env(home)
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, env, "init", "-q", "-b", "main")
	_commit(origin, env, ".claude/settings.json", "{}\n", "settings")
	_commit(origin, env, "work.md", "base\n", "work")
	clone = tmp_path / "clone"
	_git(tmp_path, env, "clone", "-q", str(origin), str(clone))
	_git(clone, env, "config", "core.logAllRefUpdates", "false")
	_git(clone, env, "checkout", "-q", "-b", "work")
	_commit(clone, env, "work.md", "branch side\n", "branch work")
	merged_settings = '{"hooks": {"PreToolUse": []}}\n'
	_commit(origin, env, ".claude/settings.json", merged_settings, "new guard wiring")
	_commit(origin, env, "work.md", "main side\n", "main work")
	_git(clone, env, "fetch", "-q", "origin", "main")
	before = _git(clone, env, "rev-parse", "HEAD")
	stopped = subprocess.run(["bash", "-c", merge], cwd=clone, env=env, capture_output=True, text=True)
	assert stopped.returncode != 0
	assert _git(clone, env, "diff", "--name-only", "--diff-filter=U") == "work.md"
	# Mid-merge, HEAD is still the branch before the merge and the merged file is on disk.
	assert _git(clone, env, "rev-parse", "HEAD") == before
	assert subprocess.run(["bash", "-c", changed.group(1)], cwd=clone, env=env).returncode == 1
	record_dir = home / ".claude" / "loaded-settings"
	record_dir.mkdir(parents=True)
	content = merged_settings if loaded == "merged" else "{}\n"
	(record_dir / "session-1.json").write_text(
		json.dumps({"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "event": "ConfigChange"}),
		encoding="utf-8",
	)
	command = check.group(1).replace(".claude/scripts/loaded_settings_check.py", str(TWIN_DIR.parent / "scripts" / "loaded_settings_check.py"))
	result = subprocess.run(["bash", "-c", command], cwd=clone, env={**env, "CLAUDE_CODE_SESSION_ID": "session-1"}, capture_output=True, text=True)
	verdict = json.loads(result.stdout)
	assert verdict["before_recorder_wired"] is False
	if loaded == "merged":
		assert result.returncode == 0 and verdict["current"] is True
	else:
		assert result.returncode == 1 and verdict["current"] is False
		assert "before the merge (HEAD)" in verdict["reason"]
	# The check leaves the unfinished merge as it was.
	assert _git(clone, env, "diff", "--name-only", "--diff-filter=U") == "work.md"
