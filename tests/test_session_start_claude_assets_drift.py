"""Contract for the `claude_assets=stale` diagnostic in .claude/hooks/session-start.sh
(issue #4952).

A session runs the hooks of the branch it has checked out, so a long-running
branch keeps running a guard the default branch has since fixed. The hook logs
`[session-start] claude_assets=stale behind=<n> files=<list>` when the checkout
lacks default-branch changes to `.claude/hooks/**` or `.claude/settings.json`,
and never fails the hook: not offline, not outside a git repository, not
without an `origin/<default>` ref.

Every case runs in a scratch repository with its own origin, so no network
and no live checkout are involved. Both the in-repo hook and its
`workflow-templates/.claude/` twin are exercised.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = (
	REPO_ROOT / ".claude" / "hooks" / "session-start.sh",
	REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "session-start.sh",
)
STALE_PREFIX = "[session-start] claude_assets=stale"


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


@pytest.fixture()
def scratch(tmp_path: Path):
	"""An origin with `main`, and a clone on branch `work` cut from it."""
	home = tmp_path / "home"
	home.mkdir()
	env = _git_env(home)
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, env, "init", "-q", "-b", "main")
	_commit(origin, env, ".claude/hooks/guard.py", "v1\n", "guard v1")
	_commit(origin, env, ".claude/settings.json", "{}\n", "settings")
	_commit(origin, env, "README.md", "readme\n", "readme")
	clone = tmp_path / "clone"
	_git(tmp_path, env, "clone", "-q", str(origin), str(clone))
	_git(clone, env, "checkout", "-q", "-b", "work")
	return origin, clone, env


def _run(hook: Path, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["bash", "-c", f'source "{hook}" && report_claude_assets_drift'],
		cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False,
	)


@pytest.mark.parametrize("hook", HOOKS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_stale_guard_is_reported(hook: Path, scratch):
	origin, clone, env = scratch
	_commit(origin, env, ".claude/hooks/guard.py", "v2\n", "guard fix")
	_commit(origin, env, "docs.md", "x\n", "unrelated")
	result = _run(hook, clone, env)
	assert result.returncode == 0, result.stderr
	assert f"{STALE_PREFIX} behind=2 files=.claude/hooks/guard.py" in result.stdout


@pytest.mark.parametrize("hook", HOOKS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_settings_change_is_reported_with_hooks(hook: Path, scratch):
	origin, clone, env = scratch
	_commit(origin, env, ".claude/hooks/guard.py", "v2\n", "guard fix")
	_commit(origin, env, ".claude/settings.json", '{"hooks": {}}\n', "wiring")
	result = _run(hook, clone, env)
	assert result.returncode == 0, result.stderr
	assert f"{STALE_PREFIX} behind=2 files=.claude/hooks/guard.py,.claude/settings.json" in result.stdout


@pytest.mark.parametrize("hook", HOOKS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_up_to_date_and_unrelated_changes_are_silent(hook: Path, scratch):
	origin, clone, env = scratch
	_commit(origin, env, "docs.md", "x\n", "unrelated")
	result = _run(hook, clone, env)
	assert result.returncode == 0, result.stderr
	assert "claude_assets" not in result.stdout


@pytest.mark.parametrize("hook", HOOKS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_branch_own_guard_edit_is_not_staleness(hook: Path, scratch):
	_origin, clone, env = scratch
	_commit(clone, env, ".claude/hooks/guard.py", "branch edit\n", "branch edits its own guard")
	result = _run(hook, clone, env)
	assert result.returncode == 0, result.stderr
	assert "claude_assets" not in result.stdout


@pytest.mark.parametrize("hook", HOOKS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_offline_fetch_falls_back_to_the_existing_ref(hook: Path, scratch, tmp_path: Path):
	origin, clone, env = scratch
	_commit(origin, env, ".claude/hooks/guard.py", "v2\n", "guard fix")
	_git(clone, env, "fetch", "-q", "origin", "main")
	_git(clone, env, "remote", "set-url", "origin", str(tmp_path / "missing-remote"))
	result = _run(hook, clone, env)
	assert result.returncode == 0, result.stderr
	assert f"{STALE_PREFIX} behind=1 files=.claude/hooks/guard.py" in result.stdout


@pytest.mark.parametrize("hook", HOOKS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_missing_default_ref_is_silent(hook: Path, scratch, tmp_path: Path):
	_origin, clone, env = scratch
	_git(clone, env, "update-ref", "-d", "refs/remotes/origin/main")
	_git(clone, env, "remote", "set-url", "origin", str(tmp_path / "missing-remote"))
	result = _run(hook, clone, env)
	assert result.returncode == 0, result.stderr
	assert "claude_assets" not in result.stdout


@pytest.mark.parametrize("hook", HOOKS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_outside_a_git_repository_is_silent(hook: Path, tmp_path: Path):
	plain = tmp_path / "plain"
	plain.mkdir()
	home = tmp_path / "home2"
	home.mkdir()
	env = _git_env(home)
	env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
	result = _run(hook, plain, env)
	assert result.returncode == 0, result.stderr
	assert "claude_assets" not in result.stdout


@pytest.mark.parametrize("hook", HOOKS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_main_calls_the_drift_report_without_failing_the_hook(hook: Path):
	text = hook.read_text(encoding="utf-8")
	body = text.split("main() {", 1)[1].split("\n}", 1)[0]
	assert "report_claude_assets_drift || true" in body
	assert 'CLAUDE_CODE_REMOTE:-}" != "true"' in body


def test_hook_twins_are_byte_identical():
	assert HOOKS[0].read_bytes() == HOOKS[1].read_bytes()
