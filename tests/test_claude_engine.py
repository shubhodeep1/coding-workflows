#!/usr/bin/env python3
"""Unit tests for scripts/claude_engine.py (plan Phase 3, D1–D3).

Covers engine resolution (labels, per-role and global variables, defaults,
the D2 precedence), the D3 model/effort mapping, config normalisation,
transcript extraction and classification (spike evidence S7, S15),
probe parsing and the least-used account order, and the smoke-run inspector.
"""

from __future__ import annotations

import importlib.util
import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "claude_engine.py"
CONFIG = REPO_ROOT / ".github" / "ai" / "claude_engine.json"


def _load():
	spec = importlib.util.spec_from_file_location("claude_engine", SCRIPT)
	module = importlib.util.module_from_spec(spec)
	sys.modules["claude_engine"] = module
	spec.loader.exec_module(module)
	return module


ce = _load()


@pytest.mark.parametrize("command", [
	"git show HEAD:a", "git log --format='%H|%s'", "git grep 'a|b' -- '*.py'",
	"git diff --no-textconv", "gh pr view 5", "gh api repos/a/b",
])
def test_read_bash_allows_only_literal_read_forms(command: str) -> None:
	assert ce.read_bash_denial(command) is None


@pytest.mark.parametrize("command", [
	"git show --output=../scripts/x HEAD:a", "git show --outp=x HEAD:a",
	"git show --output x HEAD:a", 'git show "--outp""ut=x" HEAD:a',
	"git diff --ext-diff", "git diff --textconv", "git diff --text",
	"git grep -O vim x", "git grep -nOcat x", "git grep --open=sh x",
	"git difftool -x sh", "git diff-tree HEAD", "git show-branch HEAD",
	"git -C .. show HEAD", "git show HEAD > x", "git show $(id)",
	"git show `id`", "git log *", "git log --outp{ut=x,}",
	"gh pr checkout 5", "git show HEAD\nrm x", "git show HEAD && id",
])
def test_read_bash_denies_write_capable_forms(command: str) -> None:
	assert ce.read_bash_denial(command)


def test_read_bash_hook_protocol() -> None:
	for payload, denied in (
		({"tool_name": "Bash", "tool_input": {"command": "git show HEAD:x"}}, False),
		({"tool_name": "Bash", "tool_input": {"command": "git show --output=x HEAD:x"}}, True),
		({"tool_name": "Write", "tool_input": {"command": "git show HEAD:x"}}, True),
		({"tool_name": "Bash", "tool_input": {}}, True),
	):
		result = _run("guard-read-bash", stdin=json.dumps(payload))
		assert result.returncode == 0
		assert (json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny") if denied else not result.stdout
	assert json.loads(_run("guard-read-bash", stdin="not json").stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_support_lock_detects_changes_and_restores_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	root = tmp_path / "support"
	scripts = root / "scripts"
	scripts.mkdir(parents=True)
	file = scripts / "helper.sh"
	file.write_text("trusted\n", encoding="utf-8")
	file.chmod(0o755)
	workdir = scripts / "audit-data"
	(workdir / "scripts").mkdir(parents=True)
	(workdir / "scripts" / "untrusted.sh").write_text("data", encoding="utf-8")
	monkeypatch.setattr(ce, "support_roots", lambda: [root])
	manifest = tmp_path / "manifest.json"
	args = type("Args", (), {"manifest": str(manifest), "workdir": str(workdir)})()
	assert ce.cmd_support_lock(args) == 0
	assert stat.S_IMODE(file.stat().st_mode) == 0o555
	assert stat.S_IMODE(manifest.stat().st_mode) == 0o400
	if os.geteuid() != 0:
		with pytest.raises(PermissionError):
			file.write_text("blocked", encoding="utf-8")
	assert "scripts/audit-data/scripts/untrusted.sh" not in manifest.read_text(encoding="utf-8")
	assert "scripts/helper.sh" in manifest.read_text(encoding="utf-8")
	verify_args = type("Args", (), {"manifest": str(manifest)})()
	assert ce.cmd_support_verify(verify_args) == 0
	# The owner can undo chmod, so the hash check is required as well.
	file.chmod(0o755)
	file.write_text("changed\n", encoding="utf-8")
	assert ce.cmd_support_verify(verify_args) == 1
	assert ce.cmd_support_unlock(verify_args) == 0
	assert stat.S_IMODE(file.stat().st_mode) == 0o755
	assert ce.cmd_support_unlock(verify_args) == 0


def test_support_lock_covers_support_nested_in_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workdir = tmp_path / "consumer"
	root = workdir / ".codex-workflow-src"
	(root / "scripts").mkdir(parents=True)
	support_file = root / "scripts" / "helper.sh"
	support_file.write_text("trusted\n", encoding="utf-8")
	support_file.chmod(0o755)
	monkeypatch.setattr(ce, "support_roots", lambda: [root])
	manifest = tmp_path / "manifest.json"
	args = type("Args", (), {"manifest": str(manifest), "workdir": str(workdir)})()
	assert ce.cmd_support_lock(args) == 0
	assert stat.S_IMODE(support_file.stat().st_mode) == 0o555
	assert "scripts/helper.sh" in manifest.read_text(encoding="utf-8")
	assert ce.cmd_support_verify(args) == 0
	support_file.chmod(0o755)
	support_file.write_text("tampered\n", encoding="utf-8")
	assert ce.cmd_support_verify(args) == 1
	assert ce.cmd_support_unlock(args) == 0
	assert stat.S_IMODE(support_file.stat().st_mode) == 0o755


@pytest.mark.parametrize("mutation", ["added", "removed"])
def test_support_lock_checks_directory_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
	root = tmp_path / "root"
	(root / "scripts").mkdir(parents=True)
	file = root / "scripts" / "x.sh"
	file.write_text("original", encoding="utf-8")
	monkeypatch.setattr(ce, "support_roots", lambda: [root])
	manifest = tmp_path / "manifest.json"
	args = type("Args", (), {"manifest": str(manifest), "workdir": str(root)})()
	assert ce.cmd_support_lock(args) == 0
	(root / "scripts").chmod(0o755)
	if mutation == "added":
		(root / "scripts" / "new.sh").write_text("new", encoding="utf-8")
	else:
		file.unlink()
	assert ce.cmd_support_verify(args) == 1
	assert ce.cmd_support_unlock(args) == 0


def test_support_lock_accepts_read_only_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	root = tmp_path / "support"
	(root / "scripts").mkdir(parents=True)
	file = root / "scripts" / "read.sh"
	file.write_text("original", encoding="utf-8")
	monkeypatch.setattr(ce, "support_roots", lambda: [root])
	original_chmod = Path.chmod

	def simulate_read_only_mount(path: Path, mode: int, **kwargs) -> None:
		if path == file:
			raise OSError(errno.EROFS, "read-only filesystem")
		original_chmod(path, mode, **kwargs)

	monkeypatch.setattr(Path, "chmod", simulate_read_only_mount)
	manifest = tmp_path / "manifest.json"
	args = type("Args", (), {"manifest": str(manifest), "workdir": str(root)})()
	assert ce.cmd_support_lock(args) == 0
	assert ce.cmd_support_verify(args) == 0
	assert ce.cmd_support_unlock(args) == 0


def _run(*args: str, env: dict[str, str] | None = None, stdin: str = "") -> subprocess.CompletedProcess:
	# CI jobs carry a real GITHUB_EVENT_PATH whose labels would leak in.
	full_env = {key: value for key, value in os.environ.items() if not key.startswith(("AI_ENGINE", "GITHUB_EVENT_PATH"))}
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	full_env.update(env or {})
	return subprocess.run(
		[sys.executable, str(SCRIPT), *args],
		input=stdin,
		capture_output=True,
		text=True,
		env=full_env,
		check=False,
	)


def _jsonl(*events: dict) -> str:
	return "".join(json.dumps(event) + "\n" for event in events)


# --- config ------------------------------------------------------------------

# Roles whose checked-in default is Claude, by cutover phase.
CUTOVER_ROLES = {"CLARIFY", "CLARIFY_RESPOND", "PLAN"}  # Phase 5a
CUTOVER_ROLES |= {"IMPLEMENT", "IMPLEMENT_REPAIR", "IMPLEMENT_DIAGNOSE"}  # Phase 5b
CUTOVER_ROLES |= {"ORCHESTRATE", "WAVE_JUDGE", "STALL_JUDGE", "INTEGRATION_JUDGE", "SECURITY_JUDGE", "REVIEW_EDITOR", "REVIEW_CONSOLIDATOR", "CONFLICT_RESOLVER", "RB_JUDGE"}  # Phase 5c
CUTOVER_ROLES |= {"VALIDATE", "VALIDATE_SELF_HEAL", "VALIDATION_REFRESH", "SECURITY_AUDIT", "CHECK_TRIAGE", "WORKFLOW_HEAL", "LOG_ANALYSIS", "LOG_AUDIT", "RETRO", "SUMMARISER", "BEHAVIOURAL_SMOKE"}  # Phase 5d
CUTOVER_ROLES |= {"MATERIALITY"}  # Phase 5d, Q42: the Claude materiality check
CUTOVER_ROLES |= {"LOG_SUMMARY"}  # Phase 5d, Q41: the unselected-run summaries


def test_checked_in_config_is_valid_and_inert() -> None:
	raw = json.loads(CONFIG.read_text(encoding="utf-8"))
	config, invalid = ce.normalize_config(raw)
	assert invalid == []
	assert set(config["role_defaults"]) == set(ce.ROLES)
	# Roles move to Claude only in their own cutover phase (plan Phase 5a-5d).
	on_claude = {role for role, fields in config["role_defaults"].items() if fields["engine"] == "claude"}
	assert on_claude == CUTOVER_ROLES
	assert config["broker_url"] == "https://claude-pool-broker.shubhodeep.workers.dev/v1/pool"
	assert config["gate_utilization"] == 0.9
	assert config["probe_model"] == "claude-haiku-4-5-20251001"
	assert config["oidc_audience"] == "coding-workflows-claude-pool"


def test_checked_in_config_matches_code_defaults() -> None:
	# Only the broker URL and the cut-over roles' engine differ: a missing
	# config file means no broker and every role on codex.
	raw = json.loads(CONFIG.read_text(encoding="utf-8"))
	config, _ = ce.normalize_config(raw)
	expected = json.loads(json.dumps(ce.DEFAULT_CONFIG))
	for role in CUTOVER_ROLES:
		expected["role_defaults"][role]["engine"] = "claude"
	assert {**config, "broker_url": ""} == expected
	assert config["role_defaults"]["UNBLOCK_JUDGE"]["profile"] == "read"


@pytest.mark.parametrize(
	"url, ok",
	[
		("https://claude-pool-broker.shubhodeep.workers.dev/v1/pool", True),
		("http://127.0.0.1:8080/v1/pool", True),
		("http://localhost/v1/pool", True),
		("https://attacker.example/v1/pool", False),
		("https://claude-pool-broker.shubhodeep.workers.dev/other", False),
		("http://claude-pool-broker.example/v1/pool", False),
		("https://evil.example/v1/pool?x=1", False),
		("ftp://x/y", False),
	],
)
def test_broker_url_must_be_https_or_loopback(url: str, ok: bool) -> None:
	_, invalid = ce.normalize_config({"broker_url": url})
	assert ("broker_url" not in invalid) is ok


def test_utility_roles_use_sonnet_and_the_rest_opus() -> None:
	config, _ = ce.normalize_config(None)
	for role in ce.ROLES:
		expected = "claude-sonnet-5-5" if role in ce.UTILITY_ROLES else "claude-opus-5-5"
		assert config["role_defaults"][role]["claude_model"] == expected, role
	assert set(config["utility_roles"]) == {"LOG_SUMMARY", "RETRO", "MATERIALITY", "SUMMARISER", "BEHAVIOURAL_SMOKE"}


def test_missing_config_is_every_role_on_codex(tmp_path: Path) -> None:
	config, invalid = ce.load_config(tmp_path / "absent.json")
	assert invalid == []
	assert all(fields["engine"] == "codex" for fields in config["role_defaults"].values())


def test_unreadable_config_falls_back_to_defaults(tmp_path: Path) -> None:
	path = tmp_path / "bad.json"
	path.write_text("{not json", encoding="utf-8")
	config, invalid = ce.load_config(path)
	assert invalid == ["<unreadable>"]
	assert config == ce.DEFAULT_CONFIG


@pytest.mark.parametrize(
	"raw, key",
	[
		({"cli_version": "latest"}, "cli_version"),
		({"broker_url": "http://insecure.example"}, "broker_url"),
		({"gate_utilization": 1.5}, "gate_utilization"),
		({"gate_utilization": True}, "gate_utilization"),
		({"probe_model": "Haiku!"}, "probe_model"),
		({"default_effort": "extreme"}, "default_effort"),
		({"hide_claude_md": "yes"}, "hide_claude_md"),
		({"utility_roles": ["NOT_A_ROLE"]}, "utility_roles"),
		({"role_defaults": {"PLAN": {"engine": "gpt"}}}, "role_defaults.PLAN.engine"),
		({"role_defaults": {"PLAN": {"claude_model": "gpt-5"}}}, "role_defaults.PLAN.claude_model"),
		({"role_defaults": {"PLAN": {"profile": "admin"}}}, "role_defaults.PLAN.profile"),
		({"role_defaults": {"NOPE": {"engine": "claude"}}}, "role_defaults.NOPE"),
	],
)
def test_invalid_config_values_keep_their_default(raw: dict, key: str) -> None:
	config, invalid = ce.normalize_config(raw)
	assert key in invalid
	assert config["cli_version"] == ce.DEFAULT_CONFIG["cli_version"]
	assert config["role_defaults"]["PLAN"] == ce.DEFAULT_CONFIG["role_defaults"]["PLAN"]


def test_role_defaults_merge_per_field() -> None:
	config, invalid = ce.normalize_config({"role_defaults": {"PLAN": {"engine": "claude"}}})
	assert invalid == []
	assert config["role_defaults"]["PLAN"] == {"engine": "claude", "claude_model": "claude-opus-5-5", "profile": "write"}
	assert config["role_defaults"]["IMPLEMENT"]["engine"] == "codex"


# --- resolution (D1–D3) ------------------------------------------------------


def _resolve(role: str, env: dict[str, str], config: dict | None = None, **hints: str) -> dict:
	return ce.resolve_role(role, config or ce.normalize_config(None)[0], env, **hints)


def test_code_default_is_codex() -> None:
	resolved = _resolve("IMPLEMENT", {})
	assert (resolved["engine"], resolved["source"]) == ("codex", "default")


def test_config_default_can_select_claude() -> None:
	config, _ = ce.normalize_config({"role_defaults": {"PLAN": {"engine": "claude"}}})
	assert _resolve("PLAN", {}, config)["engine"] == "claude"


def test_global_variable_overrides_default() -> None:
	resolved = _resolve("PLAN", {"AI_ENGINE": "Claude"})
	assert (resolved["engine"], resolved["source"]) == ("claude", "var:AI_ENGINE")


def test_role_variable_beats_global_variable() -> None:
	resolved = _resolve("PLAN", {"AI_ENGINE": "claude", "AI_ENGINE_PLAN": "codex"})
	assert (resolved["engine"], resolved["source"]) == ("codex", "var:AI_ENGINE_PLAN")


def test_invalid_role_variable_is_skipped_with_a_warning() -> None:
	resolved = _resolve("PLAN", {"AI_ENGINE_PLAN": "gpt", "AI_ENGINE": "claude"})
	assert (resolved["engine"], resolved["source"]) == ("claude", "var:AI_ENGINE")
	assert resolved["warnings"] and "AI_ENGINE_PLAN" in resolved["warnings"][0]


def test_engine_claude_label_beats_variables_and_forces_opus_high() -> None:
	resolved = _resolve(
		"RETRO",
		{"AI_ENGINE_LABELS": "bug, ai:engine-claude", "AI_ENGINE_RETRO": "codex"},
		model_hint="claude-sonnet-5-5",
		effort_hint="low",
	)
	assert resolved["engine"] == "claude"
	assert resolved["source"] == "label:ai:engine-claude"
	assert (resolved["model"], resolved["effort"]) == ("claude-opus-5-5", "high")


def test_codex_label_wins_over_engine_claude_label() -> None:
	resolved = _resolve("PLAN", {"AI_ENGINE_LABELS": '[{"name": "ai:engine-claude"}, {"name": "ai:codex"}]', "AI_ENGINE": "claude"})
	assert (resolved["engine"], resolved["source"]) == ("codex", "label:ai:codex")


@pytest.mark.parametrize("role", ["REVIEW_EDITOR", "REVIEW_CONSOLIDATOR", "CONFLICT_RESOLVER", "RB_JUDGE"])
def test_claude_fixer_disabled_keeps_review_write_roles_on_codex(role: str) -> None:
	config, _ = ce.normalize_config({"role_defaults": {role: {"engine": "claude"}}})
	env = {"CLAUDE_FIXER_ENABLED": " False ", "AI_ENGINE_LABELS": "ai:engine-claude", "AI_ENGINE": "claude", f"AI_ENGINE_{role}": "claude"}
	resolved = _resolve(role, env, config)
	assert (resolved["engine"], resolved["source"]) == ("codex", "var:CLAUDE_FIXER_ENABLED")


@pytest.mark.parametrize("value", ["true", "", "0", "no"])
def test_claude_fixer_switch_only_acts_on_false(value: str) -> None:
	config, _ = ce.normalize_config({"role_defaults": {"REVIEW_EDITOR": {"engine": "claude"}}})
	resolved = _resolve("REVIEW_EDITOR", {"CLAUDE_FIXER_ENABLED": value}, config)
	assert (resolved["engine"], resolved["source"]) == ("claude", "default")


def test_claude_fixer_switch_leaves_other_roles_alone() -> None:
	config, _ = ce.normalize_config({"role_defaults": {"WAVE_JUDGE": {"engine": "claude"}}})
	resolved = _resolve("WAVE_JUDGE", {"CLAUDE_FIXER_ENABLED": "false"}, config)
	assert resolved["engine"] == "claude"


def test_labels_are_case_insensitive_and_accept_newlines() -> None:
	resolved = _resolve("PLAN", {"AI_ENGINE_LABELS": "AI:Engine-Claude\nother"})
	assert resolved["engine"] == "claude"


@pytest.mark.parametrize(
	"hint, expected",
	[
		("claude-sonnet-5-5", "claude-sonnet-5-5"),
		("openai/gpt-6-sol", "claude-opus-5-5"),
		("gpt-5.5", "claude-opus-5-5"),
		("", "claude-opus-5-5"),
		("claude-Bad Model", "claude-opus-5-5"),
	],
)
def test_model_hint_is_used_only_when_it_is_a_claude_model(hint: str, expected: str) -> None:
	assert _resolve("PLAN", {}, model_hint=hint)["model"] == expected


def test_utility_role_defaults_to_sonnet() -> None:
	assert _resolve("SUMMARISER", {})["model"] == "claude-sonnet-5-5"


@pytest.mark.parametrize(
	"hint, expected",
	[
		("none", "low"),
		("minimal", "low"),
		("low", "low"),
		("medium", "medium"),
		("high", "high"),
		("xhigh", "xhigh"),
		("max", "max"),
		("MEDIUM", "medium"),
		("", "high"),
		("bogus", "high"),
	],
)
def test_effort_mapping(hint: str, expected: str) -> None:
	assert _resolve("PLAN", {}, effort_hint=hint)["effort"] == expected


def test_read_roles_get_the_read_profile() -> None:
	for role in ce.ROLES:
		expected = "read" if role in ce.READ_ROLES else "write"
		assert _resolve(role, {})["profile"] == expected, role


def test_wave_judge_uses_the_read_profile() -> None:
	assert "WAVE_JUDGE" in ce.READ_ROLES
	assert _resolve("WAVE_JUDGE", {})["profile"] == "read"
	assert ce.load_config(CONFIG)[0]["role_defaults"]["WAVE_JUDGE"]["profile"] == "read"


def test_unknown_role_is_rejected() -> None:
	with pytest.raises(ce.EngineError):
		_resolve("NOT_A_ROLE", {})


def test_resolve_cli_prints_one_field_and_warns() -> None:
	result = _run("resolve", "--role", "PLAN", "--field", "engine", env={"AI_ENGINE": "claude", "AI_ENGINE_PLAN": "nope"})
	assert result.returncode == 0
	assert result.stdout.strip() == "claude"
	assert "::warning::AI engine: invalid AI_ENGINE_PLAN" in result.stderr


def test_resolve_cli_rejects_unknown_role() -> None:
	result = _run("resolve", "--role", "NOPE")
	assert result.returncode == 2


# --- read-profile Bash guard ---------------------------------------------------


def test_read_profile_settings_have_a_bash_guard_without_changing_write_settings() -> None:
	template = (REPO_ROOT / "scripts" / "claude_settings.json.tmpl").read_text(encoding="utf-8")
	write = ce.render_settings(template, "/w", "/trusted/gh_guard.py")
	assert write["permissions"]["deny"] == json.loads(template.replace("__CHECKOUT__", "w").replace("__GUARD_HOOK__", "/trusted/gh_guard.py"))["permissions"]["deny"]
	assert len(write["hooks"]["PreToolUse"]) == 1
	read = ce.render_settings(template, "/w", "/trusted/gh_guard.py", profile="read", read_guard_hook="/trusted/claude_engine.py")
	assert set(ce.READ_PROFILE_DENY) <= set(read["permissions"]["deny"])
	assert {
		"Read(//proc/**)", "Grep(**/claude-pool/**)", "Read(**/.git/config)", "Glob(**/.git/config)",
		"Grep(**/.git-credentials)", "Grep(**/.netrc)", "Glob(~/.config/gh/**)",
		"Grep(~/.claude/.credentials.json)", "Bash(git * --no-index*)",
	} <= set(read["permissions"]["deny"])
	assert read["hooks"]["PreToolUse"][1] == {
		"matcher": "Bash", "hooks": [{"type": "command", "command": 'python3 "/trusted/claude_engine.py" guard-read-bash', "timeout": 30}],
	}
	for path in ("", "relative.py", '/bad"path', "/bad\npath"):
		with pytest.raises(ce.EngineError):
			ce.render_settings(template, "/w", "/trusted/gh_guard.py", profile="read", read_guard_hook=path)


def test_settings_cli_accepts_container_read_guard_hook(tmp_path: Path) -> None:
	result = _run("settings", "--checkout", str(tmp_path), "--out", str(tmp_path / "settings.json"),
		"--profile", "read", "--guard-hook", "/opt/gh.py", "--read-guard-hook", "/opt/engine.py")
	assert result.returncode == 0, result.stderr
	settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
	assert settings["hooks"]["PreToolUse"][1]["hooks"][0]["command"] == 'python3 "/opt/engine.py" guard-read-bash'
	assert "FROM node:22.16.0-bookworm-slim" in _run("read-sandbox-dockerfile").stdout


def test_read_snapshot_copies_tracked_files_and_sanitized_git_history(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	def git(*args: str, cwd: Path = repo) -> str:
		return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()
	git("init", "-q")
	git("config", "user.email", "test@example.invalid")
	git("config", "user.name", "Test")
	git("config", "http.https://github.com/.extraheader", "AUTHORIZATION: secret")
	(repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
	(repo / "CLAUDE.md").write_text("private marker\n", encoding="utf-8")
	(repo / ".git-credentials").write_text("https://user:secret@example.invalid\n", encoding="utf-8")
	(repo / ".claude").mkdir()
	(repo / ".claude" / "settings.json").write_text('{"key":"secret"}\n', encoding="utf-8")
	git("add", "tracked.txt", "CLAUDE.md", ".git-credentials", ".claude/settings.json")
	git("commit", "-qm", "initial")
	git("update-ref", "refs/remotes/origin/main", "HEAD")
	(repo / "secret.txt").write_text("secret\n", encoding="utf-8")
	(repo / "tracked-link").symlink_to(repo / "secret.txt")
	git("add", "tracked-link")
	(repo / ".codex-workflow-src" / ".git").mkdir(parents=True)
	(repo / ".codex-workflow-src" / ".git" / "config").write_text("extraheader=secret", encoding="utf-8")
	worktree = tmp_path / "linked"
	git("worktree", "add", "-q", "--detach", str(worktree), "HEAD")
	for source in (repo, worktree):
		dest = tmp_path / f"snapshot-{source.name}"
		result = _run("read-snapshot", "--workdir", str(source), "--dest", str(dest), "--omit-root-claude-md",
			env={"GIT_DIR": str(tmp_path / "not-the-repo"), "GIT_WORK_TREE": str(tmp_path / "wrong-worktree"),
				"GIT_INDEX_FILE": str(tmp_path / "host-index")})
		assert result.returncode == 0, result.stderr
		assert not (tmp_path / "host-index").exists()
		assert json.loads(result.stdout)["git"] == "omitted"
		assert json.loads(result.stdout)["reason"] == "filtered_history"
		assert not (dest / "CLAUDE.md").exists()
		assert not (dest / ".git-credentials").exists()
		assert not (dest / ".claude").exists()
		assert not (dest / ".git").exists()
		assert (dest / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"
		assert not (dest / "secret.txt").exists()
		assert not (dest / "tracked-link").exists()
		assert not (dest / ".codex-workflow-src").exists()
		assert subprocess.run(["git", "-C", str(dest), "show", "HEAD:.git-credentials"], capture_output=True).returncode != 0


def test_read_snapshot_preserves_history_without_filtered_paths(tmp_path: Path) -> None:
	repo = tmp_path / "clean"
	repo.mkdir()
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	(repo / "safe.txt").write_text("safe\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(repo), "add", "safe.txt"], check=True)
	subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "safe history"], check=True)
	dest = tmp_path / "snapshot"
	result = _run("read-snapshot", "--workdir", str(repo), "--dest", str(dest))
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout)["git"] == "copied"
	assert subprocess.check_output(["git", "-C", str(dest), "log", "-1", "--format=%s"], text=True).strip() == "safe history"
	assert (dest / ".git/config").is_file()


def test_read_snapshot_omits_shared_detached_history(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	(repo / "safe.txt").write_text("safe\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(repo), "add", "safe.txt"], check=True)
	subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "safe"], check=True)
	linked = tmp_path / "linked"
	subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(linked)], check=True)
	(linked / ".env").write_text("token=hidden\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(linked), "add", ".env"], check=True)
	subprocess.run(["git", "-C", str(linked), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "detached secret"], check=True)
	for source in (repo, linked):
		dest = tmp_path / f"snapshot-{source.name}"
		result = _run("read-snapshot", "--workdir", str(source), "--dest", str(dest))
		assert result.returncode == 0, result.stderr
		assert json.loads(result.stdout)["git"] == "omitted"
		assert not (dest / ".git").exists()
		assert not (dest / ".env").exists()


def test_read_snapshot_omits_unreachable_fetched_commit(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	source = tmp_path / "source"
	for checkout in (repo, source):
		checkout.mkdir()
		subprocess.run(["git", "init", "-q", str(checkout)], check=True)
		(checkout / "safe.txt").write_text("safe\n", encoding="utf-8")
		subprocess.run(["git", "-C", str(checkout), "add", "safe.txt"], check=True)
		subprocess.run(["git", "-C", str(checkout), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "safe"], check=True)
	(source / ".env").write_text("token=hidden\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(source), "add", ".env"], check=True)
	subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "secret"], check=True)
	subprocess.run(["git", "-C", str(repo), "fetch", "-q", "--no-tags", str(source), "HEAD"], check=True)
	dest = tmp_path / "snapshot"
	result = _run("read-snapshot", "--workdir", str(repo), "--dest", str(dest))
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout)["git"] == "omitted"
	assert not (dest / ".git").exists()
	assert (dest / "safe.txt").read_text(encoding="utf-8") == "safe\n"


def test_read_snapshot_hides_claude_md_from_git_history(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	(repo / "CLAUDE.md").write_text("private marker\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(repo), "add", "CLAUDE.md"], check=True)
	subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "instructions"], check=True)
	dest = tmp_path / "snapshot"
	result = _run("read-snapshot", "--workdir", str(repo), "--dest", str(dest), "--omit-root-claude-md")
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout)["reason"] == "filtered_history"
	assert not (dest / ".git").exists()
	assert not (dest / "CLAUDE.md").exists()


def test_read_snapshot_omits_credentials_removed_from_current_tree(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	(repo / "server.pem").write_text("old credential\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(repo), "add", "server.pem"], check=True)
	subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "credential"], check=True)
	subprocess.run(["git", "-C", str(repo), "rm", "-q", "server.pem"], check=True)
	subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "remove credential"], check=True)
	dest = tmp_path / "snapshot"
	result = _run("read-snapshot", "--workdir", str(repo), "--dest", str(dest))
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout)["reason"] == "filtered_history"
	assert not (dest / ".git").exists()


@pytest.mark.parametrize("secret_name", ("client.pem.txt", "id_ed25519_sk.txt", "id_rsa.bak"))
def test_read_snapshot_omits_double_extension_credential_and_history(tmp_path: Path, secret_name: str) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	(repo / secret_name).write_text("credential\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(repo), "add", secret_name], check=True)
	subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "credential"], check=True)
	current_dest = tmp_path / "current-snapshot"
	current_result = _run("read-snapshot", "--workdir", str(repo), "--dest", str(current_dest))
	assert current_result.returncode == 0, current_result.stderr
	assert not (current_dest / secret_name).exists()
	assert json.loads(current_result.stdout)["reason"] == "filtered_history"
	assert not (current_dest / ".git").exists()
	subprocess.run(["git", "-C", str(repo), "rm", "-q", secret_name], check=True)
	subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "remove credential"], check=True)
	history_dest = tmp_path / "history-snapshot"
	history_result = _run("read-snapshot", "--workdir", str(repo), "--dest", str(history_dest))
	assert history_result.returncode == 0, history_result.stderr
	assert json.loads(history_result.stdout)["reason"] == "filtered_history"
	assert not (history_dest / ".git").exists()


def test_read_snapshot_deduplicates_unmerged_index_paths(tmp_path: Path) -> None:
	repo = tmp_path / "conflicted"
	repo.mkdir()
	def git(*args: str) -> subprocess.CompletedProcess[str]:
		return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
	git("init", "-q")
	git("config", "user.email", "test@example.invalid")
	git("config", "user.name", "Test")
	(repo / "shared.txt").write_text("base\n", encoding="utf-8")
	git("add", "shared.txt")
	git("commit", "-qm", "base")
	git("checkout", "-qb", "other")
	(repo / "shared.txt").write_text("other\n", encoding="utf-8")
	git("commit", "-qam", "other")
	git("checkout", "-q", "-")
	(repo / "shared.txt").write_text("ours\n", encoding="utf-8")
	git("commit", "-qam", "ours")
	merge = subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True, text=True)
	assert merge.returncode == 1
	assert len(git("ls-files", "--cached").stdout.splitlines()) == 3
	dest = tmp_path / "conflict-snapshot"
	result = _run("read-snapshot", "--workdir", str(repo), "--dest", str(dest))
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout)["files"] == 1
	assert (dest / "shared.txt").read_text(encoding="utf-8") == (repo / "shared.txt").read_text(encoding="utf-8")


def test_read_snapshot_limits_non_git_and_alternates(tmp_path: Path) -> None:
	plain = tmp_path / "plain"
	plain.mkdir()
	(plain / "safe.txt").write_text("data", encoding="utf-8")
	(plain / "private.pem").write_text("private", encoding="utf-8")
	result = _run("read-snapshot", "--workdir", str(plain), "--dest", str(tmp_path / "plain-copy"))
	assert json.loads(result.stdout) == {"files": 1, "bytes": 4, "git": "none", "reason": ""}
	assert not (tmp_path / "plain-copy/private.pem").exists()
	result = _run("read-snapshot", "--workdir", str(plain), "--dest", str(tmp_path / "too-small"),
		env={"CLAUDE_READ_SNAPSHOT_MAX_BYTES": "1"})
	assert result.returncode != 0
	repo = tmp_path / "repo"
	repo.mkdir()
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	(repo / "file.txt").write_text("a", encoding="utf-8")
	subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
	(repo / ".git/objects/info/alternates").write_text("/tmp/nonexistent\n", encoding="utf-8")
	result = _run("read-snapshot", "--workdir", str(repo), "--dest", str(tmp_path / "no-history"))
	assert json.loads(result.stdout)["git"] == "omitted"
	assert not (tmp_path / "no-history/.git").exists()


def test_read_snapshot_rejects_symlinked_roots(tmp_path: Path) -> None:
	real = tmp_path / "real"
	real.mkdir()
	(tmp_path / "alias").symlink_to(real, target_is_directory=True)
	result = _run("read-snapshot", "--workdir", str(tmp_path / "alias"), "--dest", str(tmp_path / "copy"))
	assert result.returncode == 2
	assert not (tmp_path / "copy").exists()


def test_read_snapshot_rejects_symlinked_tracked_parent(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	(repo / "code").mkdir()
	(repo / "code" / "tracked.txt").write_text("safe", encoding="utf-8")
	subprocess.run(["git", "-C", str(repo), "add", "code/tracked.txt"], check=True)
	(repo / "code").rename(repo / "old-code")
	(repo / "code").symlink_to(tmp_path, target_is_directory=True)
	result = _run("read-snapshot", "--workdir", str(repo), "--dest", str(tmp_path / "copy"))
	assert result.returncode == 2
	assert "snapshot rejected" in result.stderr or "unsafe snapshot parent" in result.stderr


@pytest.mark.parametrize("command", [
	"git log --oneline -5", "git show HEAD:scripts/x.sh", "git diff --stat a..b -- 'p q'",
	"git diff --text", "git diff --no-ext-diff", "git grep -n foo",
	"gh api repos/o/r --jq '.a | .b'", "gh pr view 3",
])
def test_read_guard_allows_safe_reads(command: str) -> None:
	assert ce.read_profile_bash_decision(command) == (None, "")


@pytest.mark.parametrize("command", [
	"git show --no-patch --format=%B --output=../scripts/review_single_issue_security_pass.sh HEAD",
	"git log --outp=x", "git log --output x", "git grep -O vim x", "git grep -Ovim x",
	"git grep -nOless x", "git grep --op x", "git grep --open-files-in-pager vim x",
	"git diff --ext-diff", "git diff --textconv", "git log > ../x", "git log; id",
	"git log $(id)", 'git log "$(id)"', "git log `id`", "git -c core.pager=sh log",
	"git push", "cat x", "git log 'unterminated", "git log\nid", "git log $GITHUB_ENV",
	"git log *", "git log --format={x,y}",
	"git diff --no-index /tmp/token /dev/null", "git diff --no-i /tmp/token /dev/null",
])
def test_read_guard_denies_write_primitives_and_shell_control(command: str) -> None:
	decision, reason = ce.read_profile_bash_decision(command)
	assert decision == "deny" and reason


def test_read_guard_cli_fails_closed_on_invalid_input() -> None:
	for payload in ("invalid", "[]", "{}", json.dumps({"tool_name": "Bash", "tool_input": {"command": "git show --output=/tmp/x HEAD"}})):
		result = _run("read-guard", stdin=payload)
		assert result.returncode == 0
		response = json.loads(result.stdout)["hookSpecificOutput"]
		assert response["hookEventName"] == "PreToolUse"
		assert response["permissionDecision"] == "deny"
		assert response["permissionDecisionReason"].startswith("claude_engine read-guard:")
	assert _run("read-guard", stdin=json.dumps({"tool_name": "Read", "tool_input": {}})).stdout == ""


def test_read_guard_hook_override_in_cli(tmp_path: Path) -> None:
	settings_file = tmp_path / "settings.json"
	params = ("settings", "--checkout", str(tmp_path), "--out", str(settings_file),
		"--profile", "read", "--guard-hook", "/guard.py")
	assert _run(*params, "--read-guard-hook", "/claude_engine.py").returncode == 0
	settings = json.loads(settings_file.read_text(encoding="utf-8"))
	assert settings["hooks"]["PreToolUse"][1]["hooks"][0]["command"] == 'python3 "/claude_engine.py" guard-read-bash'
	assert _run(*params).returncode == 0
	settings = json.loads(settings_file.read_text(encoding="utf-8"))
	assert settings["hooks"]["PreToolUse"][1]["hooks"][0]["command"].endswith('/scripts/claude_engine.py" guard-read-bash')
	assert _run(*params, "--read-guard-hook", "relative.py").returncode == 2


def test_read_snapshot_excludes_credentials_and_rebuilds_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	source, dest = tmp_path / "source", tmp_path / "snapshot"
	source.mkdir()
	dest.mkdir()
	def git(*args: str) -> str:
		return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()
	git("init", "-q")
	git("config", "user.name", "test")
	git("config", "user.email", "test@example.invalid")
	git("config", "http.https://github.com/.extraheader", "AUTHORIZATION: hidden")
	(source / "ok.txt").write_text("tracked", encoding="utf-8")
	(source / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
	(source / "run.sh").chmod(0o755)
	(source / "removed.txt").write_text("deleted", encoding="utf-8")
	(source / "CLAUDE.md").write_text("invisible", encoding="utf-8")
	git("add", "ok.txt", "run.sh", "removed.txt", "CLAUDE.md")
	git("commit", "-qm", "base")
	(source / "removed.txt").unlink()
	(source / "untracked.txt").write_text("untracked", encoding="utf-8")
	(source / ".env").write_text("private", encoding="utf-8")
	(source / "id.key").write_text("private", encoding="utf-8")
	(source / "skip.pem").write_text("private", encoding="utf-8")
	(source / "client.pem.txt").write_text("private", encoding="utf-8")
	(source / "keys").mkdir()
	for key_name in ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "id_ed25519_sk", "id_ecdsa_sk", "id_xmss"):
		(source / "keys" / key_name).write_text("private", encoding="utf-8")
	(source / ".claude").mkdir()
	(source / ".claude" / ".credentials.json").write_text("private", encoding="utf-8")
	(source / ".ssh").mkdir()
	(source / ".ssh" / "config").write_text("private", encoding="utf-8")
	(source / "link.txt").symlink_to(source / "ok.txt")
	(source / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
	(source / ".codex-workflow-src" / ".git").mkdir(parents=True)
	(source / ".codex-workflow-src" / ".git" / "config").write_text("private", encoding="utf-8")
	(source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
	(source / "ignored.txt").write_text("ignored", encoding="utf-8")
	monkeypatch.setenv("GIT_DIR", str(tmp_path / "bad-git-dir"))
	monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "bad-work-tree"))
	summary = ce.build_read_snapshot(source, dest, omit_claude_md=True)
	assert summary["git"] == "present" and summary["git_objects"] == ""
	assert (dest / "ok.txt").read_text(encoding="utf-8") == "tracked"
	assert (dest / "untracked.txt").read_text(encoding="utf-8") == "untracked"
	assert (dest / "run.sh").stat().st_mode & stat.S_IXUSR
	assert not (dest / "removed.txt").exists()
	monkeypatch.delenv("GIT_DIR")
	monkeypatch.delenv("GIT_WORK_TREE")
	status = subprocess.check_output(["git", "-C", str(dest), "status", "--short"], text=True).splitlines()
	assert status == []
	for path in ("CLAUDE.md", ".env", "id.key", "skip.pem", "client.pem.txt", "link.txt", "big.bin", "ignored.txt", ".codex-workflow-src", ".claude", ".ssh"):
		assert not (dest / path).exists(), path
	assert not (dest / "keys").exists()
	assert "hidden" not in (dest / ".git" / "config").read_text(encoding="utf-8")
	assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
	assert subprocess.check_output(["git", "-C", str(dest), "show", "HEAD:ok.txt"], text=True) == "tracked"
	assert subprocess.check_output(["git", "-C", str(dest), "log", "--format=%s"], text=True).strip() == "Isolated read snapshot"
	assert git("rev-parse", "HEAD") != subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip()


def test_read_snapshot_caps_and_alternates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	source, dest = tmp_path / "source", tmp_path / "snapshot"
	source.mkdir()
	dest.mkdir()
	(source / "one.txt").write_text("hello", encoding="utf-8")
	monkeypatch.setenv("CLAUDE_READ_SNAPSHOT_MAX_BYTES", "4")
	result = _run("read-snapshot", "--source", str(source), "--dest", str(dest))
	assert result.returncode == 2 and "read snapshot limit exceeded" in result.stderr
	monkeypatch.delenv("CLAUDE_READ_SNAPSHOT_MAX_BYTES")
	assert not list(dest.iterdir())
	subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
	subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
		"commit", "--allow-empty", "-qm", "base"], check=True)
	(source / ".git" / "objects" / "info" / "alternates").write_text("/other/objects\n", encoding="utf-8")
	assert ce.build_read_snapshot(source, dest)["git"] == "present"
	assert not (dest / ".git" / "objects" / "info" / "alternates").exists()


@pytest.mark.parametrize("secret_name", (".env", "id_ed25519_sk.txt", "id_rsa.bak"))
def test_read_snapshot_does_not_expose_prior_committed_credentials(tmp_path: Path, secret_name: str) -> None:
	source, dest = tmp_path / "source", tmp_path / "snapshot"
	source.mkdir()
	subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
	(source / secret_name).write_text("HISTORICAL_TOKEN=private\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(source), "add", secret_name], check=True)
	subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "secret"], check=True)
	current_dest = tmp_path / "current-snapshot"
	current_dest.mkdir()
	assert ce.build_read_snapshot(source, current_dest)["git_objects"] == ""
	assert not (current_dest / secret_name).exists()
	secret = subprocess.check_output(["git", "-C", str(source), "rev-parse", f"HEAD:{secret_name}"], text=True).strip()
	(source / secret_name).unlink()
	subprocess.run(["git", "-C", str(source), "add", "-u"], check=True)
	subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "delete secret"], check=True)
	dest.mkdir()
	assert ce.build_read_snapshot(source, dest)["git_objects"] == ""
	assert not (dest / secret_name).exists()
	for spec in (f"HEAD~1:{secret_name}", secret):
		assert subprocess.run(["git", "-C", str(dest), "show", spec], capture_output=True).returncode != 0


# --- transcripts ---------------------------------------------------------------

SUCCESS = _jsonl(
	{"type": "system", "subtype": "init"},
	{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 10}}},
	{
		"type": "result",
		"subtype": "success",
		"is_error": False,
		"result": "final answer\n",
		"total_cost_usd": 0.12,
		"duration_ms": 900,
		"num_turns": 1,
		"usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50},
		"modelUsage": {"claude-opus-5-5": {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.12}},
		"session_id": "x",
	},
)
# Spike S15: a rejected OAuth token.
AUTH_FAILED = _jsonl(
	{"type": "result", "subtype": "success", "is_error": True, "result": "Failed to authenticate. API Error: 401 OAuth access token is invalid."},
)
# Spike S7 shape, with the status a rejected request carries.
RATE_REJECTED = _jsonl(
	{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": 1760000000}}}},
	{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "API Error"},
)
WINDOW_FULL = _jsonl(
	{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {"seven_day": {"utilization": 1.0}}}},
	{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "stopped"},
)
LIMIT_TEXT = _jsonl({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached|1760000000"})
MAX_TURNS = _jsonl({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})


def test_extract_writes_result_text_and_returns_usage(tmp_path: Path) -> None:
	transcript = tmp_path / "t.jsonl"
	transcript.write_text("not json\n" + SUCCESS, encoding="utf-8")
	out = tmp_path / "out.txt"
	result = _run("extract", "--transcript", str(transcript), "--out", str(out))
	assert result.returncode == 0
	assert out.read_text(encoding="utf-8") == "final answer\n"
	usage = json.loads(result.stdout)
	assert usage["type"] == "result"
	assert usage["total_cost_usd"] == 0.12
	assert usage["usage"]["output_tokens"] == 5
	assert "result" not in usage
	assert "session_id" not in usage


def test_extract_without_result_fails_and_writes_nothing(tmp_path: Path) -> None:
	transcript = tmp_path / "t.jsonl"
	transcript.write_text(_jsonl({"type": "system"}), encoding="utf-8")
	out = tmp_path / "out.txt"
	result = _run("extract", "--transcript", str(transcript), "--out", str(out))
	assert result.returncode == 1
	assert not out.exists()


@pytest.mark.parametrize(
	"text, exit_code, outcome, reason",
	[
		(SUCCESS, 0, "success", "result_success"),
		(SUCCESS, None, "success", "result_success"),
		(SUCCESS, 1, "crashed", "result_success"),
		(AUTH_FAILED, 1, "auth_failed", "result_auth_error"),
		(RATE_REJECTED, 1, "usage_limit", "rate_limit_rejected"),
		(WINDOW_FULL, 1, "usage_limit", "usage_limit"),
		(LIMIT_TEXT, 1, "usage_limit", "usage_limit"),
		(MAX_TURNS, 1, "crashed", "result_error_max_turns"),
		("", 1, "crashed", "no_transcript"),
		(_jsonl({"type": "system"}), 1, "crashed", "no_result"),
		(SUCCESS, 124, "timeout", "exit_124"),
		("", 137, "timeout", "exit_137"),
	],
)
def test_classify(text: str, exit_code: int | None, outcome: str, reason: str) -> None:
	verdict = ce.classify(text, exit_code)
	assert (verdict["outcome"], verdict["reason"]) == (outcome, reason)
	assert verdict["outcome"] in ce.OUTCOMES


def test_classify_cli_missing_transcript_is_crashed(tmp_path: Path) -> None:
	result = _run("classify", "--transcript", str(tmp_path / "absent.jsonl"), "--exit-code", "1")
	assert result.returncode == 0
	assert json.loads(result.stdout)["outcome"] == "crashed"


def test_inspect_reports_startup_tokens_and_denials() -> None:
	text = _jsonl(
		{
			"type": "assistant",
			"message": {
				"usage": {"input_tokens": 3, "cache_creation_input_tokens": 12000, "cache_read_input_tokens": 500},
				"content": [
					{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "gh pr merge 1 --squash"}},
					{"type": "tool_use", "id": "b", "name": "Write", "input": {"file_path": "/w/.github/workflows/x.yml"}},
					{"type": "tool_use", "id": "c", "name": "Read", "input": {"file_path": "/w/README.md"}},
				],
			},
		},
		{"type": "assistant", "message": {"usage": {"input_tokens": 99999}, "content": []}},
		{
			"type": "user",
			"message": {
				"content": [
					{"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": "Permission to use Bash has been denied."},
					{"type": "tool_result", "tool_use_id": "b", "is_error": True, "content": [{"type": "text", "text": "denied by rule"}]},
					{"type": "tool_result", "tool_use_id": "c", "content": "ok"},
				]
			},
		},
	)
	facts = ce.inspect_transcript(text)
	assert facts["startup_input_tokens"] == 12503
	assert [(c["tool"], c["is_error"]) for c in facts["tool_calls"]] == [("Bash", True), ("Write", True), ("Read", False)]
	assert facts["tool_calls"][0]["target"] == "gh pr merge 1 --squash"
	assert facts["tool_calls"][1]["result"] == "denied by rule"


# --- probes and account choice -----------------------------------------------


def _probe_text(five: float | None, seven: float | None, *, status: str = "allowed", ok: bool = True) -> str:
	windows = {}
	if five is not None:
		windows["five_hour"] = {"utilization": five, "resetsAt": 1000}
	if seven is not None:
		windows["seven_day"] = {"utilization": seven, "resetsAt": 2000}
	return _jsonl(
		{"type": "rate_limit_event", "rate_limit_info": {"status": status, "unifiedWindows": windows}},
		{"type": "result", "subtype": "success", "is_error": not ok, "result": "OK"},
	)


def test_probe_parse_reads_both_windows() -> None:
	record = ce.parse_probe(_probe_text(0.2, 0.5), "A")
	assert (record["five_hour"], record["seven_day"], record["error"]) == (0.2, 0.5, None)


def test_probe_parse_auth_failure() -> None:
	assert ce.parse_probe(AUTH_FAILED, "A", 1)["error"] == "auth_failed"


def test_probe_parse_limited_account_is_rejected_not_failed() -> None:
	record = ce.parse_probe(RATE_REJECTED, "A", 1)
	assert (record["error"], record["status"]) == (None, "rejected")


def test_probe_parse_cli() -> None:
	result = _run("probe-parse", "--account", "B", stdin=_probe_text(0.1, 0.3))
	assert result.returncode == 0
	assert json.loads(result.stdout)["account"] == "B"


def test_choose_orders_by_least_used_with_alphabetical_ties() -> None:
	probes = [
		ce.parse_probe(_probe_text(0.5, 0.5), "C"),
		ce.parse_probe(_probe_text(0.2, 0.4), "B"),
		ce.parse_probe(_probe_text(0.4, 0.1), "A"),
		ce.parse_probe(_probe_text(None, None), "D"),
	]
	verdict = ce.choose_account(probes, 0.9)
	assert verdict["outcome"] == "selected"
	assert verdict["order"] == ["A", "B", "C", "D"]
	assert verdict["account"] == "A"
	assert verdict["utilization"] == 0.4


def test_choose_gates_accounts_at_or_over_the_gate() -> None:
	probes = [
		ce.parse_probe(_probe_text(0.95, 0.1), "A"),
		ce.parse_probe(_probe_text(0.1, None), "B"),
		ce.parse_probe(_probe_text(0.9, None), "C"),
	]
	verdict = ce.choose_account(probes, 0.9)
	assert verdict["order"] == ["B"]
	assert verdict["gated"] == ["A", "C"]


def test_choose_all_gated_reports_the_earliest_reset() -> None:
	probes = [ce.parse_probe(_probe_text(0.95, 0.1), "A"), ce.parse_probe(_probe_text(0.2, 0.99), "B")]
	verdict = ce.choose_account(probes, 0.9)
	assert verdict["outcome"] == "all_gated"
	assert verdict["resets_at"] == 1000
	assert verdict["order"] == []


@pytest.mark.parametrize(
	"probes, outcome",
	[
		([], "no_accounts"),
		([{"account": "A", "error": "auth_failed"}], "auth_failed"),
		([{"account": "A", "error": "probe_failed"}], "crashed"),
	],
)
def test_choose_without_a_usable_account(probes: list, outcome: str) -> None:
	assert ce.choose_account(probes, 0.9)["outcome"] == outcome


def test_choose_cli_uses_config_gate_and_rejects_bad_input() -> None:
	probes = [ce.parse_probe(_probe_text(0.85, 0.1), "A")]
	result = _run("choose", stdin=json.dumps(probes))
	assert json.loads(result.stdout)["account"] == "A"
	result = _run("choose", "--gate", "0.8", stdin=json.dumps(probes))
	assert json.loads(result.stdout)["outcome"] == "all_gated"
	assert _run("choose", stdin="{}").returncode == 2
	assert _run("choose", "--gate", "2", stdin="[]").returncode == 2


# --- trust and support files ---------------------------------------------------


def test_trust_marks_the_workdir_and_keeps_other_keys(tmp_path: Path) -> None:
	home = tmp_path / "home"
	home.mkdir()
	(home / ".claude.json").write_text(json.dumps({"keep": 1, "projects": {"/other": {"x": True}}}), encoding="utf-8")
	work = tmp_path / "work"
	work.mkdir()
	result = _run("trust", "--workdir", str(work), "--home", str(home))
	assert result.returncode == 0
	data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
	assert data["keep"] == 1
	assert data["projects"]["/other"] == {"x": True}
	assert data["projects"][str(work.resolve())]["hasTrustDialogAccepted"] is True
	assert oct((home / ".claude.json").stat().st_mode & 0o777) == "0o600"


def test_trust_replaces_an_unreadable_file(tmp_path: Path) -> None:
	(tmp_path / ".claude.json").write_text("[1, 2", encoding="utf-8")
	ce.trust_workdir(tmp_path, "/w")
	assert json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8")) == {"projects": {"/w": {"hasTrustDialogAccepted": True}}}


def test_support_files_come_from_the_trusted_roots_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	# A caller checkout's own copy (cwd) is never searched.
	(tmp_path / ".github" / "ai").mkdir(parents=True)
	(tmp_path / ".github" / "ai" / "claude_engine.json").write_text('{"role_defaults": {"PLAN": {"engine": "claude"}}}', encoding="utf-8")
	monkeypatch.chdir(tmp_path)
	monkeypatch.delenv("SUPPORT_ROOT_DIR", raising=False)
	monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
	assert ce.find_support_file(ce.CONFIG_RELATIVE) == CONFIG
	result = _run("support-file", "--name", "guard-hook", env={"SUPPORT_ROOT_DIR": "", "GITHUB_WORKSPACE": ""})
	assert result.returncode == 0
	assert result.stdout.strip().endswith(".claude/hooks/gh_api_write_guard.py")


def test_support_root_takes_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	(tmp_path / ".github" / "ai").mkdir(parents=True)
	staged = tmp_path / ".github" / "ai" / "claude_engine.json"
	staged.write_text('{"role_defaults": {"PLAN": {"engine": "claude"}}}', encoding="utf-8")
	monkeypatch.setenv("SUPPORT_ROOT_DIR", str(tmp_path))
	config, _ = ce.load_config()
	assert config["role_defaults"]["PLAN"]["engine"] == "claude"


# --- cost_audit.py reads the usage line ------------------------------------------


def test_cost_audit_parses_the_usage_line(tmp_path: Path) -> None:
	spec = importlib.util.spec_from_file_location("cost_audit_for_engine", REPO_ROOT / "scripts" / "cost_audit.py")
	cost_audit = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(cost_audit)
	transcript = tmp_path / "t.jsonl"
	transcript.write_text(SUCCESS, encoding="utf-8")
	usage_line = _run("extract", "--transcript", str(transcript), "--out", str(tmp_path / "o")).stdout.strip()
	log = "\n".join(
		[
			f"2026-10-04T01:00:01.0000000Z {usage_line}",
			f"2026-10-04T01:00:02.0000000Z {usage_line}",
			'2026-10-04T01:00:03.0000000Z {"type": "result", "note": "no usage object"}',
			"2026-10-04T01:00:04.0000000Z not json {\"type\": \"result\"",
		]
	)
	parsed = cost_audit.parse_log(log)
	assert parsed["codex_tokens_used"] == 0
	assert parsed["claude_calls"] == 2
	assert parsed["claude_input_tokens"] == 20
	assert parsed["claude_output_tokens"] == 10
	assert parsed["claude_cache_read_tokens"] == 200
	assert parsed["claude_cache_write_tokens"] == 100
	assert parsed["claude_cost_usd"] == 0.24
	assert parsed["claude_models"]["claude-opus-5-5"]["calls"] == 2
	assert parsed["claude_models"]["claude-opus-5-5"]["cost_usd"] == 0.24


def test_cost_audit_ignores_non_finite_costs() -> None:
	spec = importlib.util.spec_from_file_location("cost_audit_costs", REPO_ROOT / "scripts" / "cost_audit.py")
	cost_audit = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(cost_audit)
	line = json.dumps({"type": "result", "usage": {}, "total_cost_usd": "Infinity"})
	assert cost_audit.parse_log(line)["claude_cost_usd"] == 0.0
	line = json.dumps({"type": "result", "usage": {}, "total_cost_usd": -3})
	assert cost_audit.parse_log(line)["claude_cost_usd"] == 0.0


# --- Phase 6: labels from the event payload --------------------------------------


def test_labels_come_from_the_event_payload_when_unset(tmp_path: Path) -> None:
	event = tmp_path / "event.json"
	event.write_text(json.dumps({"issue": {"labels": [{"name": "bug"}, {"name": "ai:engine-claude"}]}}), encoding="utf-8")
	resolved = _resolve("PLAN", {"GITHUB_EVENT_PATH": str(event)})
	assert (resolved["engine"], resolved["source"], resolved["model"], resolved["effort"]) == ("claude", "label:ai:engine-claude", "claude-opus-5-5", "high")
	event.write_text(json.dumps({"pull_request": {"labels": [{"name": "ai:engine-claude"}, {"name": "ai:codex"}]}}), encoding="utf-8")
	assert _resolve("REVIEW_EDITOR", {"GITHUB_EVENT_PATH": str(event)})["source"] == "label:ai:codex"


def test_an_explicit_empty_label_list_overrides_the_payload(tmp_path: Path) -> None:
	event = tmp_path / "event.json"
	event.write_text(json.dumps({"issue": {"labels": [{"name": "ai:engine-claude"}]}}), encoding="utf-8")
	assert _resolve("PLAN", {"GITHUB_EVENT_PATH": str(event), "AI_ENGINE_LABELS": ""})["engine"] == "codex"


@pytest.mark.parametrize("content", ["not json", "[]", '{"issue": {"labels": "x"}}', '{"issue": {"labels": [1, {"name": 2}]}}'])
def test_an_unusable_payload_means_no_labels(tmp_path: Path, content: str) -> None:
	event = tmp_path / "event.json"
	event.write_text(content, encoding="utf-8")
	assert ce.work_item_labels({"GITHUB_EVENT_PATH": str(event)}) == []
	assert ce.work_item_labels({"GITHUB_EVENT_PATH": str(tmp_path / "absent.json")}) == []
