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
	# Issue #5260: a PR head is also checked against its own base.
	assert "git fetch origin <base>" in section
	assert "git diff --quiet HEAD...origin/<base> -- .claude/hooks .claude/settings.json" in section
	assert "only changes the base holds and the working branch lacks" in section
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
	# PR #5282 review rounds: the project-branch sync names the Procedure's step 2 bullet, not the sync's own
	# step 2 (the drift check), and spells out its sequence.
	assert "sync the project branch first with the **Sync the project branch** bullet of [Procedure](#procedure) step 2 (not this section's step 2, which is the drift check)" in section
	assert "merge the default branch into it with that bullet's command and subject, resolve and push as that bullet says" in section
	assert "the procedure's step 2" not in section
	assert "step 2's merge" not in section
	assert "check the PR head branch out again (`git checkout <PR head branch>`). Then merge `origin/<project branch>` into the PR head" in section
	assert "exactly as step 2 does" not in section


def test_project_base_is_verified_and_other_bases_merge_their_own_base(commands):
	"""Issue #5260: sync and verify the project base; a non-default-bound PR head merges its own base."""
	section = _section(commands["implement-plan-claude.md"], "### Claude-asset sync", "### Permission prompt report")
	assert "git fetch origin <project branch>" in section
	assert "git diff --quiet origin/<project branch>...origin/<default> -- .claude/hooks .claude/settings.json" in section
	assert "stop with the caller's blocker as in step 5" in section
	assert "it covers default drift and base drift alike" in section
	assert "never merge the default branch" in section
	assert "With base drift, merge `origin/<base>` into the PR head" in section
	assert "With default drift only, skip the merge" in section
	assert "`<source>` is the default branch, the project branch, or the PR's base, per step 3" in section
	# PR #5282 review round 1: routing, both-drift, and failed-fetch cases.
	assert "any non-zero exit → step 3" in section
	assert "Up to two checks" in section
	assert "When the default check failed too, run it again after step 4's merge" in section
	assert "When `git fetch origin <project branch>` itself fails, it is the same stop" in section
	# PR #5282 review round 3: a moved or missing plan never ends in a raw `git show` error.
	assert "or `git show origin/<base>:docs/completed/<slug>-plan.md` once its completion PR moved it there" in section
	assert "both `git show` calls failing means the branch is not a project branch, not a stop" in section


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
	assert "on the project branch itself, the merge in the **Sync the project branch** bullet of [Procedure](#procedure) step 2 is the sync merge and keeps that bullet's command and subject" in section
	assert "The project branch itself needs only the default check: the merge in the **Sync the project branch** bullet of [Procedure](#procedure) step 2 already brings its own base in" in section
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
	assert "When its project-base check fails" in fixer
	# PR #5282 review round: a failed fetch of the project branch has no paths to name.
	assert "the paths it lists (or, when fetching the project branch failed, the failed fetch and its error line)" in fixer
	assert "on the default branch or on the PR's own base" in fixer
	# PR #5282 review round 1: the conflict notice names the branch the sync merged.
	assert ".claude/ conflict with <source> — decision needed" in fixer
	assert "conflict with <default>" not in fixer


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


def _documented(section: str, pattern: str, **placeholders: str) -> str:
	match = re.search(pattern, section)
	assert match, pattern
	command = match.group(1)
	for name, value in placeholders.items():
		command = command.replace(f"<{name.replace('_', ' ')}>", value)
	assert "<" not in command, command
	return command


def _exit_code(cwd: Path, env: dict[str, str], command: str) -> int:
	return subprocess.run(["bash", "-c", command], cwd=cwd, env=env, capture_output=True, text=True).returncode


@pytest.mark.parametrize("directory", DIRS, ids=lambda path: str(path.relative_to(ROOT)))
def test_base_drift_is_found_when_the_default_branch_has_nothing_new(directory: Path, tmp_path: Path):
	"""Issue #5260: the project base gains a guard change after the PR head was cut, and main has none.

	The default-drift check alone reports the head as fresh; the documented base-drift check finds the
	change, and the documented merge of the base clears it.
	"""
	section = _section(_flat(directory / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	default_check = _documented(section, r"`(git diff --quiet HEAD\.\.\.origin/<default> -- [^`]*)`", default="main")
	base_check = _documented(section, r"`(git diff --quiet HEAD\.\.\.origin/<base> -- [^`]*)`", base="project")
	merge = _documented(section, r"`(git merge [^`]*origin/<source>)`", source="project")
	home = tmp_path / "home"
	home.mkdir()
	env = _git_env(home)
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, env, "init", "-q", "-b", "main")
	_commit(origin, env, ".claude/hooks/guard.py", "v1\n", "guard v1")
	_git(origin, env, "checkout", "-q", "-b", "project")
	_commit(origin, env, "docs/log.md", "log\n", "project start")
	clone = tmp_path / "clone"
	_git(tmp_path, env, "clone", "-q", str(origin), str(clone))
	_git(clone, env, "checkout", "-q", "-b", "work", "origin/project")
	_commit(clone, env, "work.md", "x\n", "phase work")
	_commit(origin, env, ".claude/hooks/guard.py", "v2\n", "project guard fix")
	_git(clone, env, "fetch", "-q", "origin", "main", "project")
	assert _exit_code(clone, env, default_check) == 0
	assert _exit_code(clone, env, base_check) == 1
	subprocess.run(["bash", "-c", merge], cwd=clone, env=env, check=True, capture_output=True, text=True)
	assert _git(clone, env, "log", "-1", "--format=%s") == "[claude-asset-sync] merge project for .claude/ guard updates"
	assert (clone / ".claude/hooks/guard.py").read_text(encoding="utf-8") == "v2\n"
	assert _exit_code(clone, env, base_check) == 0
	assert _exit_code(clone, env, default_check) == 0


@pytest.mark.parametrize("directory", DIRS, ids=lambda path: str(path.relative_to(ROOT)))
def test_project_base_check_fails_until_the_project_branch_holds_the_default_guards(directory: Path, tmp_path: Path):
	"""The documented project-base check exits non-zero while the pushed project branch lacks a main guard change."""
	section = _section(_flat(directory / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	verify = _documented(
		section,
		r"`(git diff --quiet origin/<project branch>\.\.\.origin/<default> -- [^`]*)`",
		project_branch="project",
		default="main",
	)
	home = tmp_path / "home"
	home.mkdir()
	env = _git_env(home)
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, env, "init", "-q", "-b", "main")
	_commit(origin, env, ".claude/settings.json", "{}\n", "settings")
	_git(origin, env, "branch", "project")
	_commit(origin, env, ".claude/settings.json", '{"hooks": {}}\n', "main wiring")
	clone = tmp_path / "clone"
	_git(tmp_path, env, "clone", "-q", str(origin), str(clone))
	_git(clone, env, "fetch", "-q", "origin", "main", "project")
	assert _exit_code(clone, env, verify) == 1
	_git(clone, env, "checkout", "-q", "-b", "project", "origin/project")
	_git(clone, env, "merge", "-q", "--no-edit", "origin/main")
	_git(clone, env, "push", "-q", "origin", "project")
	_git(clone, env, "fetch", "-q", "origin", "project")
	assert _exit_code(clone, env, verify) == 0


@pytest.mark.parametrize("directory", DIRS, ids=lambda path: str(path.relative_to(ROOT)))
def test_default_drift_left_after_a_base_merge_is_still_detected(directory: Path, tmp_path: Path):
	"""PR #5282 review round 1: a PR on another base lacks guard changes from both main and its base.

	The documented base merge clears the base drift, and the default check, run again after the merge,
	still exits non-zero because the base itself lacks main's change, so the stale record is due.
	"""
	section = _section(_flat(directory / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	default_check = _documented(section, r"`(git diff --quiet HEAD\.\.\.origin/<default> -- [^`]*)`", default="main")
	base_check = _documented(section, r"`(git diff --quiet HEAD\.\.\.origin/<base> -- [^`]*)`", base="stable")
	merge = _documented(section, r"`(git merge [^`]*origin/<source>)`", source="stable")
	home = tmp_path / "home"
	home.mkdir()
	env = _git_env(home)
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, env, "init", "-q", "-b", "main")
	_commit(origin, env, ".claude/hooks/guard.py", "v1\n", "guard v1")
	_commit(origin, env, ".claude/settings.json", "{}\n", "settings")
	_git(origin, env, "branch", "stable")
	clone = tmp_path / "clone"
	_git(tmp_path, env, "clone", "-q", str(origin), str(clone))
	_git(clone, env, "checkout", "-q", "-b", "work", "origin/stable")
	_commit(clone, env, "work.md", "x\n", "pr work")
	_commit(origin, env, ".claude/settings.json", '{"hooks": {}}\n', "main wiring")
	_git(origin, env, "checkout", "-q", "stable")
	_commit(origin, env, ".claude/hooks/guard.py", "v2\n", "stable guard fix")
	_git(clone, env, "fetch", "-q", "origin", "main", "stable")
	assert _exit_code(clone, env, default_check) == 1
	assert _exit_code(clone, env, base_check) == 1
	subprocess.run(["bash", "-c", merge], cwd=clone, env=env, check=True, capture_output=True, text=True)
	assert _git(clone, env, "log", "-1", "--format=%s") == "[claude-asset-sync] merge stable for .claude/ guard updates"
	assert (clone / ".claude/hooks/guard.py").read_text(encoding="utf-8") == "v2\n"
	assert _exit_code(clone, env, base_check) == 0
	assert _exit_code(clone, env, default_check) == 1


@pytest.mark.parametrize("directory", DIRS, ids=lambda path: str(path.relative_to(ROOT)))
def test_plan_lookup_finds_a_moved_plan_and_fails_on_a_non_project_branch(directory: Path, tmp_path: Path):
	"""PR #5282 review round 3: the documented plan lookups, run as written.

	A project branch whose completion PR moved its plan is found at the second path; another pull
	request's `-phase-<n>` head has a plan at neither path, which routes to "Any other base".
	"""
	section = _section(_flat(directory / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	plans = r"`(git show origin/<base>:docs/plans/<slug>-plan\.md)`"
	completed = r"`(git show origin/<base>:docs/completed/<slug>-plan\.md)`"
	home = tmp_path / "home"
	home.mkdir()
	env = _git_env(home)
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, env, "init", "-q", "-b", "main")
	_commit(origin, env, "README.md", "x\n", "init")
	_git(origin, env, "checkout", "-q", "-b", "claude/implement-plan-foo")
	_commit(origin, env, "docs/completed/foo-plan.md", "Base branch: main\n", "completion: move the plan")
	_git(origin, env, "checkout", "-q", "-b", "claude/implement-plan-foo-phase-1")
	_commit(origin, env, "work.md", "x\n", "phase work")
	clone = tmp_path / "clone"
	_git(tmp_path, env, "clone", "-q", str(origin), str(clone))
	_git(clone, env, "fetch", "-q", "origin", "claude/implement-plan-foo", "claude/implement-plan-foo-phase-1")
	project = {"base": "claude/implement-plan-foo", "slug": "foo"}
	assert _exit_code(clone, env, _documented(section, plans, **project)) != 0
	assert _exit_code(clone, env, _documented(section, completed, **project)) == 0
	phase_head = {"base": "claude/implement-plan-foo-phase-1", "slug": "foo-phase-1"}
	assert _exit_code(clone, env, _documented(section, plans, **phase_head)) != 0
	assert _exit_code(clone, env, _documented(section, completed, **phase_head)) != 0


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
	(PR #5281 review round 1). PR #5282 names the **Sync the project branch**
	bullet of Procedure step 2 instead."""
	section = _section(_flat(TWIN_DIR / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	assert "sync the project branch first with the **Sync the project branch** bullet of [Procedure](#procedure) step 2 (not this section's step 2, which is the drift check)" in section
	assert "on the project branch itself, the merge in the **Sync the project branch** bullet of [Procedure](#procedure) step 2 is the sync merge and keeps that bullet's command and subject" in section
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


def test_failed_fetch_stops_instead_of_checking_a_stale_ref():
	"""PR #5280 review round 1: a failed fetch in the sync's step 1 is a stop,
	never a drift check against the ref it did not refresh."""
	section = _section(_flat(TWIN_DIR / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	step1 = _section(section, "1. `git fetch origin <default>`", "2. **Stale?**")
	assert "Each fetch must succeed." in step1
	# PR #5280 final-merge review round 1 (head e06795d6fdc1): `;`-chained
	# fetches report only the last exit status.
	assert "Run each fetch as its own Bash call and read its exit status" in step1
	assert "chained with `;` the exit status is the last command's" in step1
	assert "`git fetch origin <default>`;" not in step1
	assert "A failed fetch is therefore a stop with the caller's blocker as in step 5, naming the failed fetch and its error line" in step1
	assert "never run step 2 against a ref this step did not refresh" in step1
	fixer = _section(_flat(TWIN_DIR / "fix-claude-pr.md"), "5. **Fix it.**", "- **`claude/implement-plan-*` head**")
	assert "When one of the sync's own fetches fails (its step 1: `git fetch origin <default>` or `git fetch origin <base ref>`), stop the same way" in fixer
	assert ".claude/ asset sync could not fetch <ref> — decision needed" in fixer


def test_failed_base_fetch_leaves_a_stale_ref_that_reports_no_drift(tmp_path: Path):
	"""Why the stop is needed: after a failed `git fetch origin <base>` the
	documented base-drift check still exits 0 against the stale ref, although
	the live base holds a guard change the head lacks."""
	section = _section(_flat(TWIN_DIR / "implement-plan-claude.md"), "### Claude-asset sync", "### Permission prompt report")
	fetch = _documented(section, r"`(git fetch origin <base>)`", base="project")
	base_check = _documented(section, r"`(git diff --quiet HEAD\.\.\.origin/<base> -- [^`]*)`", base="project")
	home = tmp_path / "home"
	home.mkdir()
	env = _git_env(home)
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, env, "init", "-q", "-b", "main")
	_commit(origin, env, ".claude/hooks/guard.py", "v1\n", "guard v1")
	_git(origin, env, "checkout", "-q", "-b", "project")
	_commit(origin, env, "docs/log.md", "log\n", "project start")
	clone = tmp_path / "clone"
	_git(tmp_path, env, "clone", "-q", str(origin), str(clone))
	_git(clone, env, "checkout", "-q", "-b", "work", "origin/project")
	_commit(clone, env, "work.md", "x\n", "phase work")
	_commit(origin, env, ".claude/hooks/guard.py", "v2\n", "project guard fix")
	# The remote becomes unreachable before the sync's fetch.
	origin.rename(tmp_path / "origin-moved")
	assert _exit_code(clone, env, fetch) != 0
	assert _exit_code(clone, env, base_check) == 0
	# Once the fetch succeeds, the same check finds the drift.
	(tmp_path / "origin-moved").rename(origin)
	assert _exit_code(clone, env, fetch) == 0
	assert _exit_code(clone, env, base_check) == 1
