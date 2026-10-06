#!/usr/bin/env python3
"""P5 permission policy: scripts/claude_settings.json.tmpl and its renderer.

The Claude engine runs write roles with `--permission-mode bypassPermissions`,
so the deny rules and the `gh api` guard hook in the rendered `--settings`
file are the whole policy (plan "Ports", P5; spike evidence S13, S14).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "claude_engine.py"
TEMPLATE = REPO_ROOT / "scripts" / "claude_settings.json.tmpl"


def _load():
	spec = importlib.util.spec_from_file_location("claude_engine_policy", SCRIPT)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


ce = _load()

REQUIRED_DENY = (
	"Bash(gh pr merge*)",
	"Bash(gh api * -X DELETE*)",
	"Bash(git push --force*)",
	"Bash(git push * --delete*)",
)


def _render(**kwargs) -> dict:
	options = {"checkout": "/work/repo", "guard_hook": "/support/.claude/hooks/gh_api_write_guard.py"}
	options.update(kwargs)
	return ce.render_settings(TEMPLATE.read_text(encoding="utf-8"), **options)


def test_template_is_json_with_placeholders() -> None:
	text = TEMPLATE.read_text(encoding="utf-8")
	json.loads(text)
	assert "__CHECKOUT__" in text
	assert "__GUARD_HOOK__" in text


@pytest.mark.parametrize("rule", REQUIRED_DENY)
def test_plan_deny_rules_are_present(rule: str) -> None:
	assert rule in _render()["permissions"]["deny"]


def test_force_push_and_delete_variants_are_denied() -> None:
	deny = _render()["permissions"]["deny"]
	for rule in ("Bash(git push * --force*)", "Bash(git push -f*)", "Bash(git push * -f*)", "Bash(gh api -X DELETE*)", "Bash(gh api * --method DELETE*)"):
		assert rule in deny


def test_protected_paths_are_anchored_at_the_checkout() -> None:
	deny = _render()["permissions"]["deny"]
	for tool in ("Edit", "Write", "NotebookEdit"):
		assert f"{tool}(//work/repo/.github/workflows/**)" in deny
		assert f"{tool}(//work/repo/.claude/**)" in deny
	# S13: an unanchored rule would also block workflow-templates/.claude/**.
	assert not any(rule.startswith(("Edit(./", "Write(./", "Edit(.claude", "Write(.claude")) for rule in deny)


def test_allow_workflow_edits_lifts_only_the_workflow_rules() -> None:
	deny = _render(allow_workflow_edits=True)["permissions"]["deny"]
	assert not any("/.github/workflows/" in rule for rule in deny)
	assert "Edit(//work/repo/.claude/**)" in deny
	assert "Bash(gh pr merge*)" in deny


def test_guard_hook_runs_on_every_bash_call() -> None:
	hooks = _render()["hooks"]["PreToolUse"]
	assert hooks[0]["matcher"] == "Bash"
	assert hooks[0]["hooks"][0]["command"] == 'python3 "/support/.claude/hooks/gh_api_write_guard.py"'


def test_env_block_carries_no_credentials() -> None:
	env = _render()["env"]
	assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
	assert not any(word in key.upper() for key in env for word in ("TOKEN", "KEY", "SECRET"))


def test_renderer_refuses_credentials_in_env() -> None:
	template = json.dumps({"permissions": {"deny": []}, "env": {"CLAUDE_CODE_OAUTH_TOKEN": "x"}})
	with pytest.raises(ce.EngineError):
		ce.render_settings(template, "/w", "/g.py")


def test_profiles() -> None:
	assert _render(profile="write")["permissions"]["allow"] == []
	allow = _render(profile="read")["permissions"]["allow"]
	assert {"Read", "Grep", "Glob"} <= set(allow)
	assert all(rule.startswith(("Read", "Grep", "Glob", "Bash(git ", "Bash(gh ")) for rule in allow)
	assert not any("merge" in rule or "push" in rule for rule in allow)
	with pytest.raises(ce.EngineError):
		_render(profile="admin")


@pytest.mark.parametrize("bad", ["relative/path", "/has space'quote", '/dq"', "/dollar$x", "/back`tick", "/new\nline"])
def test_paths_must_be_safe_absolute_paths(bad: str) -> None:
	with pytest.raises(ce.EngineError):
		_render(checkout=bad)
	with pytest.raises(ce.EngineError):
		_render(guard_hook=bad)


def test_settings_cli_writes_owner_only_file(tmp_path: Path) -> None:
	out = tmp_path / "settings.json"
	env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
	result = subprocess.run(
		[sys.executable, str(SCRIPT), "settings", "--checkout", str(tmp_path), "--out", str(out), "--profile", "read"],
		capture_output=True,
		text=True,
		env=env,
		check=False,
	)
	assert result.returncode == 0, result.stderr
	settings = json.loads(out.read_text(encoding="utf-8"))
	assert oct(out.stat().st_mode & 0o777) == "0o600"
	anchor = str(tmp_path.resolve()).lstrip("/")
	assert f"Edit(//{anchor}/.claude/**)" in settings["permissions"]["deny"]
	assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith('/.claude/hooks/gh_api_write_guard.py"')


def test_settings_cli_fails_without_the_guard_hook(tmp_path: Path) -> None:
	# A policy without the guard must not render: claude_run then falls back to codex.
	empty = tmp_path / "empty"
	empty.mkdir()
	result = subprocess.run(
		[sys.executable, "-c", f"import runpy, sys; sys.argv = ['x', 'settings', '--checkout', '/w', '--out', '{tmp_path}/s.json']; "
			f"import importlib.util as u; s = u.spec_from_file_location('m', '{SCRIPT}'); m = u.module_from_spec(s); s.loader.exec_module(m); "
			f"m.SCRIPT_DIR = __import__('pathlib').Path('{empty}') / 'scripts'; m.support_roots = lambda: [__import__('pathlib').Path('{empty}')]; "
			f"sys.exit(m.main(['settings', '--checkout', '/w', '--out', '{tmp_path}/s.json', '--template', '{TEMPLATE}']))"],
		capture_output=True,
		text=True,
		env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
		check=False,
	)
	assert result.returncode == 2
	assert "gh_api_write_guard.py not found" in result.stderr
	assert not (tmp_path / "s.json").exists()
