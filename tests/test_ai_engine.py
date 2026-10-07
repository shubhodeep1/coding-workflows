#!/usr/bin/env python3
"""scripts/ai_engine.sh: engine selection, D1 fallback and claude_run.

claude_run runs the CLI through scripts/codex_isolated_exec.sh with the
recording fake `docker` of tests/codex_isolation_fakes.py: the real helper
renders the snapshot, starts the real claude_anthropic_relay.py broker on the
account's token file and copies workspace changes back, and the fake
container runs a fake `claude` executable. The fake behaves according to the
account token the host relay holds (success, usage limit, rejected token,
crash), which it learns from the relay's command line only because it is a
test double; the token itself never enters the container. The real CLI is
never called. Also covers the `--engine` log label of the codex wrappers.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
from pathlib import Path

import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_isolation_fakes import docker_runs, enable_fake_isolation, install_fake_docker, short_temp_dir  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_ENGINE = REPO_ROOT / "scripts" / "ai_engine.sh"
STALL_GUARD = REPO_ROOT / "scripts" / "codex_stall_guard.sh"
HEARTBEAT = REPO_ROOT / "scripts" / "codex_heartbeat.sh"
INSTRUCTIONS = REPO_ROOT / "unattended_system_instructions.md"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_CLAUDE_LOG"]
# The relay's account token, recovered by the fake container (test only).
token = os.environ.get("FAKE_RELAY_TOKEN", "")
mounts = json.loads(os.environ.get("FAKE_CONTAINER_MOUNTS", "[]"))
def host_path(path):
	for mount in mounts:
		dst = mount.get("dst", "")
		if path == dst or path.startswith(dst + "/"):
			return mount["src"] + path[len(dst):]
	return path
argv = sys.argv[1:]
settings_text = ""
if "--settings" in argv:
	with open(host_path(argv[argv.index("--settings") + 1]), encoding="utf-8") as handle:
		settings_text = handle.read()
record = {
	"argv": argv,
	"stdin": sys.stdin.read(),
	"token": token,
	"container_token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
	"base_url": os.environ.get("ANTHROPIC_BASE_URL", ""),
	"cwd": os.getcwd(),
	"claude_md_visible": os.path.exists("CLAUDE.md"),
	"api_key_env": "ANTHROPIC_API_KEY" in os.environ,
	"gh_token_env": "GH_TOKEN" in os.environ,
	"settings": settings_text,
}
with open(log, "a", encoding="utf-8") as handle:
	handle.write(json.dumps(record) + "\n")
def emit(event):
	print(json.dumps(event), flush=True)
emit({"type": "system", "subtype": "init"})
if token.startswith("TOK_LIMIT"):
	emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
	emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "API Error"})
	sys.exit(1)
if token.startswith("TOK_AUTH"):
	emit({"type": "result", "subtype": "success", "is_error": True, "result": "Failed to authenticate. API Error: 401 OAuth access token is invalid."})
	sys.exit(1)
if token.startswith("TOK_CRASH"):
	print("boom", file=sys.stderr)
	sys.exit(3)
if token.startswith("TOK_WRITE_MD"):
	with open("CLAUDE.md", "w", encoding="utf-8") as handle:
		handle.write("new instructions\n")
emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.01, "usage": {"input_tokens": 1, "output_tokens": 2}})
'''


@pytest.fixture()
def sandbox(tmp_path: Path):
	fake_bin = tmp_path / "bin"
	fake_bin.mkdir()
	fake = fake_bin / "claude"
	fake.write_text(FAKE_CLAUDE, encoding="utf-8")
	fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
	home = tmp_path / "home"
	home.mkdir()
	runner_temp = tmp_path / "rt"
	runner_temp.mkdir()
	work = tmp_path / "work"
	work.mkdir()
	(work / "CLAUDE.md").write_text("checkout CLAUDE.md\n", encoding="utf-8")
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("do the thing\n", encoding="utf-8")
	pool = tmp_path / "pool"
	(pool / "tokens").mkdir(parents=True)
	env = {
		key: value
		for key, value in os.environ.items()
		if not key.startswith(("AI_ENGINE", "CLAUDE_", "ANTHROPIC_", "SUPPORT_", "TG_", "GITHUB_WORKSPACE", "GITHUB_EVENT_PATH"))
	}
	env.update(
		{
			"PATH": f"{fake_bin}:{os.environ['PATH']}",
			"HOME": str(home),
			"RUNNER_TEMP": str(runner_temp),
			"CLAUDE_ENGINE_POOL_DIR": str(pool),
			"SUPPORT_INSTRUCTIONS_FILE": str(INSTRUCTIONS),
			"FAKE_CLAUDE_LOG": str(tmp_path / "calls.jsonl"),
			"PYTHONDONTWRITEBYTECODE": "1",
			"CODEX_HEARTBEAT_INTERVAL_SECS": "30",
			"ANTHROPIC_API_KEY": "must-not-reach-the-cli",
			"GH_TOKEN": "ghp_mustnotreachtheclaudecli0000000000000",
		}
	)
	# The broker's Unix socket path must stay under 108 bytes.
	env["RUNNER_TEMP"] = str(short_temp_dir())
	env = enable_fake_isolation(fake_bin, REPO_ROOT / "scripts", env, passthrough_prefixes=("FAKE_",), short_temp=False)
	yield {"tmp": tmp_path, "env": env, "pool": pool, "work": work, "prompt": prompt, "home": home, "bin": fake_bin, "runner_temp": Path(env["RUNNER_TEMP"])}
	import shutil

	shutil.rmtree(env["RUNNER_TEMP"], ignore_errors=True)


def _accounts(sandbox: dict, **tokens: str) -> None:
	(sandbox["pool"] / "order").write_text("".join(f"{name}\n" for name in tokens), encoding="utf-8")
	for name, token in tokens.items():
		path = sandbox["pool"] / "tokens" / name
		path.write_text(f"{token}\n", encoding="utf-8")
		path.chmod(0o600)


def _bash(sandbox: dict, script: str, **extra_env: str) -> subprocess.CompletedProcess:
	env = dict(sandbox["env"], **extra_env)
	return subprocess.run(
		["bash", "-c", f"set -euo pipefail; source {shlex.quote(str(AI_ENGINE))}; {script}"],
		capture_output=True,
		text=True,
		env=env,
		cwd=sandbox["tmp"],
		timeout=120,
		check=False,
	)


def _claude_run(sandbox: dict, role: str = "PLAN", session: str = "", **extra_env: str) -> subprocess.CompletedProcess:
	out = sandbox["tmp"] / "out.txt"
	args = " ".join(shlex.quote(part) for part in (role, str(sandbox["prompt"]), str(out), str(sandbox["work"]), session) if part)
	return _bash(sandbox, f'rc=0; claude_run {args} || rc=$?; echo "RC=${{rc}}"; echo "RUN_DIR=${{AI_ENGINE_LAST_RUN_DIR:-}}"', **extra_env)


def _calls(sandbox: dict) -> list[dict]:
	path = sandbox["tmp"] / "calls.jsonl"
	if not path.exists():
		return []
	return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _rc(result: subprocess.CompletedProcess) -> int:
	for line in result.stdout.splitlines():
		if line.startswith("RC="):
			return int(line[3:])
	raise AssertionError(result.stdout + result.stderr)


# --- selection -------------------------------------------------------------------


def test_for_role_defaults_to_codex_and_logs(sandbox: dict) -> None:
	# The code defaults (an empty config), not the checked-in role cutovers.
	support = sandbox["tmp"] / "support"
	(support / ".github" / "ai").mkdir(parents=True)
	(support / ".github" / "ai" / "claude_engine.json").write_text("{}", encoding="utf-8")
	result = _bash(sandbox, "ai_engine_for_role IMPLEMENT", SUPPORT_ROOT_DIR=str(support))
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "codex"
	assert "AI_ENGINE_SELECTED role=IMPLEMENT engine=codex model=claude-opus-5-5 effort=high source=default" in result.stderr


def test_for_role_honours_variables_and_labels(sandbox: dict) -> None:
	assert _bash(sandbox, "ai_engine_for_role PLAN", AI_ENGINE_PLAN="claude").stdout.strip() == "claude"
	result = _bash(sandbox, "ai_engine_for_role PLAN", AI_ENGINE="claude", AI_ENGINE_LABELS="ai:codex,ai:engine-claude")
	assert result.stdout.strip() == "codex"
	assert "source=label:ai:codex" in result.stderr


def test_for_role_with_invalid_role_is_codex(sandbox: dict) -> None:
	result = _bash(sandbox, "ai_engine_for_role 'bad role'")
	assert result.stdout.strip() == "codex"
	assert "invalid role" in result.stderr


def test_model_effort_and_version_helpers(sandbox: dict) -> None:
	assert _bash(sandbox, "ai_engine_model RETRO").stdout.strip() == "claude-sonnet-5-5"
	assert _bash(sandbox, "ai_engine_model PLAN claude-sonnet-5-5").stdout.strip() == "claude-sonnet-5-5"
	assert _bash(sandbox, "ai_engine_model PLAN openai/gpt-6-sol").stdout.strip() == "claude-opus-5-5"
	assert _bash(sandbox, "ai_engine_effort PLAN minimal").stdout.strip() == "low"
	assert _bash(sandbox, "ai_engine_effort PLAN xhigh").stdout.strip() == "xhigh"
	version = _bash(sandbox, "ai_engine_cli_version").stdout.strip()
	assert version.count(".") == 2 and version.replace(".", "").isdigit()


# --- fallback (D1) -----------------------------------------------------------------


def test_no_credential_falls_back_with_one_telegram_note(sandbox: dict) -> None:
	notes = sandbox["tmp"] / "tg.txt"
	script = (
		f'tg_send_msg() {{ printf "%s|%s\\n" "$1" "$2" >> {shlex.quote(str(notes))}; }}; '
		f"rc1=0; claude_run PLAN {shlex.quote(str(sandbox['prompt']))} out1 {shlex.quote(str(sandbox['work']))} || rc1=$?; "
		f"rc2=0; claude_run IMPLEMENT {shlex.quote(str(sandbox['prompt']))} out2 {shlex.quote(str(sandbox['work']))} || rc2=$?; "
		'echo "RC=${rc1}${rc2}"'
	)
	result = _bash(sandbox, script)
	assert "RC=7575" in result.stdout, result.stderr
	assert "AI_ENGINE_FALLBACK role=PLAN reason=no_credential" in result.stderr
	assert "AI_ENGINE_FALLBACK role=IMPLEMENT reason=no_credential" in result.stderr
	lines = notes.read_text(encoding="utf-8").splitlines()
	assert len(lines) == 1
	assert "Claude unavailable for PLAN (no_credential)" in lines[0] and lines[0].endswith("|WARNING")
	assert _calls(sandbox) == []


def test_isolation_unavailable_falls_back(sandbox: dict) -> None:
	# No image (or no Docker): no account can run, so claude_run falls back
	# to codex once instead of trying every account (answer Q20 A).
	_accounts(sandbox, A="TOK_OK", B="TOK_OK")
	install_fake_docker(sandbox["bin"], sandbox["bin"].parent / "fake-docker.jsonl", {"FAKE_DOCKER_BUILD_FAIL": "1", "FAKE_CLAUDE_LOG": sandbox["env"]["FAKE_CLAUDE_LOG"]})
	result = _claude_run(sandbox)
	assert _rc(result) == 75
	assert "CODEX_ISOLATION unavailable engine=claude reason=image_build_failed" in result.stderr
	assert "CLAUDE_POOL run role=PLAN account=A outcome=unavailable reason=isolation_unavailable exit_code=75" in result.stderr
	assert "account=B" not in result.stderr
	assert "AI_ENGINE_FALLBACK role=PLAN reason=isolation_unavailable" in result.stderr
	assert _calls(sandbox) == []


def test_missing_isolation_helper_falls_back(sandbox: dict, tmp_path: Path) -> None:
	_accounts(sandbox, A="TOK_OK")
	scripts = tmp_path / "scripts-no-helper"
	scripts.mkdir()
	for name in ("ai_engine.sh", "claude_engine.py", "claude_settings.json.tmpl", "codex_stall_guard.sh"):
		(scripts / name).write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
	out = sandbox["tmp"] / "out.txt"
	result = subprocess.run(
		["bash", "-c", f'source {shlex.quote(str(scripts / "ai_engine.sh"))}; rc=0; claude_run PLAN {shlex.quote(str(sandbox["prompt"]))} {shlex.quote(str(out))} {shlex.quote(str(sandbox["work"]))} || rc=$?; echo "RC=${{rc}}"'],
		capture_output=True, text=True, env=dict(sandbox["env"], SUPPORT_ROOT_DIR=str(REPO_ROOT)), cwd=sandbox["tmp"], timeout=120, check=False,
	)
	assert _rc(result) == 75, result.stderr
	assert "AI_ENGINE_FALLBACK role=PLAN reason=support_missing" in result.stderr
	assert _calls(sandbox) == []


def test_relay_that_cannot_start_moves_to_the_next_account(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_LOOSE", B="TOK_OK")
	(sandbox["pool"] / "tokens" / "A").chmod(0o644)  # The relay refuses a token file others can read.
	result = _claude_run(sandbox)
	assert _rc(result) == 0, result.stderr
	assert "CLAUDE_POOL run role=PLAN account=A outcome=crashed reason=relay_unavailable exit_code=73" in result.stderr
	assert [call["token"] for call in _calls(sandbox)] == ["TOK_OK"]


def test_missing_instructions_falls_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	result = _claude_run(sandbox, SUPPORT_INSTRUCTIONS_FILE="/nonexistent", SUPPORT_ROOT_DIR="/nonexistent", GITHUB_WORKSPACE="/nonexistent")
	# The repository copy next to ai_engine.sh is still found.
	assert _rc(result) == 0, result.stderr


def test_usage_limit_and_rejected_token_move_to_the_next_account(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_LIMIT_a", B="TOK_AUTH_b", C="TOK_OK_c")
	result = _claude_run(sandbox)
	assert _rc(result) == 0, result.stderr
	assert (sandbox["tmp"] / "out.txt").read_text(encoding="utf-8") == "done"
	assert "CLAUDE_POOL run role=PLAN account=A outcome=usage_limit reason=rate_limit_rejected exit_code=1" in result.stderr
	assert "CLAUDE_POOL run role=PLAN account=B outcome=auth_failed reason=result_auth_error exit_code=1" in result.stderr
	assert "CLAUDE_POOL run role=PLAN account=C outcome=success reason=result_success exit_code=0" in result.stderr
	# The usage line cost_audit.py reads.
	usage_lines = [line for line in result.stderr.splitlines() if line.startswith('{"duration_ms"') or '"type": "result"' in line]
	assert usage_lines and json.loads(usage_lines[-1])["total_cost_usd"] == 0.01
	calls = _calls(sandbox)
	assert [call["token"] for call in calls] == ["TOK_LIMIT_a", "TOK_AUTH_b", "TOK_OK_c"]
	assert "AI_ENGINE_FALLBACK" not in result.stderr


def test_all_accounts_failing_falls_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_LIMIT", B="TOK_AUTH")
	result = _claude_run(sandbox)
	assert _rc(result) == 75
	assert "AI_ENGINE_FALLBACK role=PLAN reason=all_accounts_failed" in result.stderr


def test_crash_is_not_retried_on_another_account(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_CRASH", B="TOK_OK")
	result = _claude_run(sandbox)
	assert _rc(result) == 1
	assert len(_calls(sandbox)) == 1
	assert "AI_ENGINE_FALLBACK" not in result.stderr


def test_missing_token_file_is_skipped(sandbox: dict) -> None:
	_accounts(sandbox, B="TOK_OK")
	(sandbox["pool"] / "order").write_text("A\nB\nbad name\n", encoding="utf-8")
	result = _claude_run(sandbox)
	assert _rc(result) == 0, result.stderr
	assert "CLAUDE_POOL account_skipped account=A reason=token_missing" in result.stderr


# --- the command line ----------------------------------------------------------------


def test_write_role_command_line(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	result = _claude_run(sandbox, "IMPLEMENT", AI_ENGINE_EFFORT_HINT="none")
	assert _rc(result) == 0, result.stderr
	call = _calls(sandbox)[0]
	argv = call["argv"]
	assert argv[0] == "-p"
	expected_pairs = {
		"--model": "claude-opus-5-5",
		"--effort": "low",
		"--system-prompt-file": "/support/instructions.md",
		"--settings": "/support/settings.json",
		"--setting-sources": "",
		"--tools": "Read,Grep,Glob,Bash,Edit,Write,WebFetch,WebSearch",
		"--permission-mode": "bypassPermissions",
		"--output-format": "stream-json",
	}
	for flag, value in expected_pairs.items():
		assert argv[argv.index(flag) + 1] == value, flag
	for flag in ("--strict-mcp-config", "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections", "--verbose"):
		assert flag in argv
	policy = json.loads(call["settings"])
	anchor = str(sandbox["work"].resolve()).lstrip("/")
	assert f"Edit(//{anchor}/.github/workflows/**)" in policy["permissions"]["deny"]
	assert "/support/guard.py" in call["settings"]
	# The token never reaches the CLI: the host relay holds it, the
	# container sees a placeholder and talks to the loopback bridge.
	assert "TOK_OK" not in " ".join(argv)
	assert call["token"] == "TOK_OK"
	assert call["container_token"] == "isolated-placeholder"
	assert call["base_url"] == "http://127.0.0.1:8765"
	assert call["api_key_env"] is False
	assert call["gh_token_env"] is False
	assert call["stdin"] == "do the thing\n"
	# A write role edits a copy of the workdir, never the checkout itself.
	assert call["cwd"] != str(sandbox["work"].resolve())
	assert Path(call["cwd"]).name == "work" and "codex-isolated-" in call["cwd"]
	assert "TOK_OK" not in result.stderr + result.stdout
	assert not (sandbox["home"] / ".claude.json").exists()
	run = docker_runs(sandbox["bin"].parent / "fake-docker.jsonl")[-1]
	assert "--network" in run["argv"] and run["argv"][run["argv"].index("--network") + 1] == "none"
	assert "TOK_OK" not in json.dumps(run)
	assert "ghp_" not in json.dumps(run["argv"]) and "GH_TOKEN" not in run["env"]
	assert any(arg.endswith(",readonly") is False and f"dst={sandbox['work'].resolve()}" in arg for arg in run["argv"])


def test_read_role_command_line(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	result = _claude_run(sandbox, "SECURITY_AUDIT", AI_ENGINE_MODEL_HINT="claude-sonnet-5-5")
	assert _rc(result) == 0, result.stderr
	argv = _calls(sandbox)[0]["argv"]
	assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,Bash"
	assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
	assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
	# A read profile gets the read-only snapshot (answer Q17 A).
	run = docker_runs(sandbox["bin"].parent / "fake-docker.jsonl")[-1]
	assert any(f"dst={sandbox['work'].resolve()},readonly" in arg for arg in run["argv"])


def test_profile_tool_lists_match_claude_engine() -> None:
	# The write profile names its tools: `--tools default` loads ~35 tools and
	# pushed a no-op start-up past the 25,000-token context gate of
	# claude-engine-smoke.yml (run 37191845530: 29,369 tokens).
	import importlib.util

	spec = importlib.util.spec_from_file_location("claude_engine", REPO_ROOT / "scripts" / "claude_engine.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	engine_src = AI_ENGINE.read_text(encoding="utf-8")
	assert f'read) tools="{module.PROFILE_TOOLS["read"]}"' in engine_src
	assert f'*) tools="{module.PROFILE_TOOLS["write"]}"' in engine_src
	sandbox_src = (REPO_ROOT / "scripts" / "review_untrusted_sandbox.sh").read_text(encoding="utf-8")
	assert f'--tools {module.PROFILE_TOOLS["write"]} --permission-mode bypassPermissions' in sandbox_src
	for src in (engine_src, sandbox_src):
		assert "--tools default" not in src
		assert 'tools="default"' not in src
	assert module.PROFILE_TOOLS["write"] != "default"


def test_allow_workflow_edits_reaches_the_policy(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	_claude_run(sandbox, ALLOW_WORKFLOW_EDITS="true")
	policy = json.loads(_calls(sandbox)[0]["settings"])
	assert not any("/.github/workflows/" in rule for rule in policy["permissions"]["deny"])


def test_session_id_starts_then_resumes(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	session = "0123abcd-0000-4000-8000-00000000abcd"
	_claude_run(sandbox, session=session)
	# The session store lives outside the container (answer Q18 A).
	project = sandbox["runner_temp"] / "claude-isolated-home" / "projects" / "p"
	project.mkdir(parents=True)
	(project / f"{session}.jsonl").write_text("{}\n", encoding="utf-8")
	_claude_run(sandbox, session=session)
	first, second = (call["argv"] for call in _calls(sandbox))
	assert first[first.index("--session-id") + 1] == session
	assert second[second.index("--resume") + 1] == session


def test_invalid_arguments_are_rejected(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	assert _rc(_claude_run(sandbox, session="not-a-uuid")) == 2
	assert _rc(_claude_run(sandbox, role="lower")) == 2
	assert _calls(sandbox) == []


def test_relative_paths_survive_the_directory_change(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	result = _bash(sandbox, 'rc=0; claude_run PLAN prompt.txt rel-out.txt work || rc=$?; echo "RC=${rc}"')
	assert _rc(result) == 0, result.stderr
	assert (sandbox["tmp"] / "rel-out.txt").read_text(encoding="utf-8") == "done"


def _hide_claude_md_support(sandbox: dict) -> Path:
	support = sandbox["tmp"] / "support"
	(support / ".github" / "ai").mkdir(parents=True)
	(support / ".github" / "ai" / "claude_engine.json").write_text('{"hide_claude_md": true}', encoding="utf-8")
	return support


def test_hide_claude_md_keeps_it_out_of_the_container(sandbox: dict) -> None:
	# answer Q19 A: the copy leaves CLAUDE.md out; the host file never moves.
	_accounts(sandbox, A="TOK_OK")
	result = _claude_run(sandbox, "IMPLEMENT", SUPPORT_ROOT_DIR=str(_hide_claude_md_support(sandbox)))
	assert _rc(result) == 0, result.stderr
	assert _calls(sandbox)[0]["claude_md_visible"] is False
	assert (sandbox["work"] / "CLAUDE.md").read_text(encoding="utf-8") == "checkout CLAUDE.md\n"


def test_hide_claude_md_never_writes_one_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_WRITE_MD")
	result = _claude_run(sandbox, "IMPLEMENT", SUPPORT_ROOT_DIR=str(_hide_claude_md_support(sandbox)))
	assert _rc(result) == 0, result.stderr
	assert (sandbox["work"] / "CLAUDE.md").read_text(encoding="utf-8") == "checkout CLAUDE.md\n"
	assert list(sandbox["work"].glob("CLAUDE.md.original.*")) == []
	run_dir = Path(next(line[8:] for line in result.stdout.splitlines() if line.startswith("RUN_DIR=")))
	assert "CODEX_ISOLATION transfer ignored=CLAUDE.md reason=hidden" in (run_dir / "stderr-A.txt").read_text(encoding="utf-8")


def test_write_role_changes_are_copied_back(sandbox: dict) -> None:
	# Without hide_claude_md the copy holds CLAUDE.md and an edit to it is an
	# ordinary changed file.
	_accounts(sandbox, A="TOK_WRITE_MD")
	result = _claude_run(sandbox, "IMPLEMENT")
	assert _rc(result) == 0, result.stderr
	assert (sandbox["work"] / "CLAUDE.md").read_text(encoding="utf-8") == "new instructions\n"


def test_claude_md_stays_by_default(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	_claude_run(sandbox)
	assert _calls(sandbox)[0]["claude_md_visible"] is True


def test_last_run_dir_holds_the_transcripts(sandbox: dict) -> None:
	_accounts(sandbox, Z="TOK_LIMIT", A="TOK_OK")
	result = _claude_run(sandbox)
	run_dir = Path(next(line[8:] for line in result.stdout.splitlines() if line.startswith("RUN_DIR=")))
	assert (run_dir / "transcript-A.jsonl").exists()
	assert (run_dir / "transcript-Z.jsonl").exists()
	assert (run_dir / "successful-transcript.jsonl").resolve() == run_dir / "transcript-A.jsonl"


# --- wrappers: the --engine label ------------------------------------------------------


@pytest.mark.parametrize("wrapper", [STALL_GUARD, HEARTBEAT])
def test_engine_flag_only_adds_a_log_field(wrapper: Path, tmp_path: Path) -> None:
	env = dict(os.environ, CODEX_HEARTBEAT_INTERVAL_SECS="1", CODEX_STALL_HEARTBEAT_DIR=str(tmp_path / "hb"), RUNNER_TEMP=str(tmp_path))
	labelled = subprocess.run(
		["bash", str(wrapper), "--phase", "plan", "--engine", "claude", "--", "sleep", "2.5"],
		capture_output=True, text=True, env=env, timeout=60, check=False,
	)
	plain = subprocess.run(
		["bash", str(wrapper), "--phase", "plan", "--", "sleep", "2.5"],
		capture_output=True, text=True, env=env, timeout=60, check=False,
	)
	assert labelled.returncode == 0 and plain.returncode == 0
	labelled_beats = [line for line in labelled.stderr.splitlines() if line.startswith("CODEX_HEARTBEAT:")]
	plain_beats = [line for line in plain.stderr.splitlines() if line.startswith("CODEX_HEARTBEAT:")]
	assert labelled_beats and all(line.endswith(" engine=claude") for line in labelled_beats)
	assert plain_beats and not any("engine=" in line for line in plain_beats)


@pytest.mark.parametrize("wrapper", [STALL_GUARD, HEARTBEAT])
def test_invalid_engine_label_is_dropped_with_a_warning(wrapper: Path, tmp_path: Path) -> None:
	env = dict(os.environ, CODEX_HEARTBEAT_INTERVAL_SECS="1", CODEX_STALL_HEARTBEAT_DIR=str(tmp_path / "hb"), RUNNER_TEMP=str(tmp_path))
	result = subprocess.run(
		["bash", str(wrapper), "--phase", "plan", "--engine", "gpt x", "--", "sleep", "1.5"],
		capture_output=True, text=True, env=env, timeout=60, check=False,
	)
	assert result.returncode == 0
	assert "Invalid --engine" in result.stderr
	assert "engine=" not in result.stderr.replace("Invalid --engine", "")


def test_engine_label_is_added_to_stall_lines(tmp_path: Path) -> None:
	env = dict(
		os.environ,
		CODEX_HEARTBEAT_INTERVAL_SECS="1",
		CODEX_STALL_TIMEOUT_SECONDS="1",
		CODEX_STALL_GUARD_ENABLED="false",
		CODEX_STALL_HEARTBEAT_DIR=str(tmp_path / "hb"),
		RUNNER_TEMP=str(tmp_path),
	)
	result = subprocess.run(
		["bash", str(STALL_GUARD), "--phase", "plan", "--engine", "claude", "--", "sleep", "3.5"],
		capture_output=True, text=True, env=env, timeout=60, check=False,
	)
	observed = [line for line in result.stderr.splitlines() if line.startswith("codex_stall_observed")]
	assert observed and all(line.endswith(" engine=claude") for line in observed)


def test_every_workflow_staging_ai_engine_also_stages_its_stall_guard() -> None:
	"""claude_run calls ${_AI_ENGINE_DIR}/codex_stall_guard.sh with --engine.

	A workflow that stages ai_engine.sh from the support ref but leaves the
	stall guard to the checkout runs whatever guard the checked-out branch
	carries. A heal issue plans against `stable`, whose older guard rejected
	`--engine` and crashed every Claude planning attempt (runs 37389550162,
	37389524799, 37389535878).
	"""
	workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
	staging_lists = []
	for path in workflows:
		for line in path.read_text(encoding="utf-8").splitlines():
			stripped = line.strip()
			if stripped.startswith("for f in ") and stripped.endswith("; do") and " ai_engine.sh " in f" {stripped} ":
				staging_lists.append((path.name, stripped.split()))
	assert {name for name, _ in staging_lists} >= {"plan.yml", "clarify.yml", "orchestrate_clarify_respond.yml", "implement.yml"}
	missing = [name for name, names in staging_lists if "codex_stall_guard.sh" not in names]
	assert not missing, f"stage codex_stall_guard.sh beside ai_engine.sh in: {missing}"


def test_claude_home_helper_names_the_isolated_session_store(sandbox: dict) -> None:
	result = _bash(sandbox, "ai_engine_claude_home")
	assert result.stdout.strip() == f"{sandbox['runner_temp']}/claude-isolated-home"


def test_write_role_reuses_the_prepared_implement_sandbox(sandbox: dict) -> None:
	# implement prepares one workspace sandbox (dependencies preinstalled);
	# a Claude write role in that job runs in it, as the codex attempts do.
	_accounts(sandbox, A="TOK_OK")
	prepare = subprocess.run(
		["bash", str(REPO_ROOT / "scripts" / "codex_isolated_exec.sh"), "prepare", "--workdir", str(sandbox["work"])],
		capture_output=True, text=True, env=sandbox["env"], timeout=120, check=False,
	)
	assert prepare.returncode == 0, prepare.stderr
	root = prepare.stdout.strip()
	result = _claude_run(sandbox, "IMPLEMENT", CODEX_ISOLATED_ROOT=root, CODEX_ISOLATED_MODE="workspace")
	assert _rc(result) == 0, result.stderr
	run = docker_runs(sandbox["bin"].parent / "fake-docker.jsonl")[-1]
	assert f"coding-workflows.codex-isolated.root={root}" in run["argv"]
	assert Path(root).is_dir(), "the persistent root outlives the attempt"
	# A read profile never reuses it.
	result = _claude_run(sandbox, "SECURITY_AUDIT", CODEX_ISOLATED_ROOT=root, CODEX_ISOLATED_MODE="workspace")
	assert _rc(result) == 0, result.stderr
	run = docker_runs(sandbox["bin"].parent / "fake-docker.jsonl")[-1]
	assert f"coding-workflows.codex-isolated.root={root}" not in run["argv"]


# --- Claude engine fallback reports -------------------------------------------

ENGINE_FALLBACK_WORKFLOWS = {
	# workflow file -> phase job
	"clarify.yml": "clarify",
	"plan.yml": "plan",
	"implement.yml": "implement",
	"orchestrate_clarify_respond.yml": "respond",
}


def _fallback_record(sandbox: dict) -> list[str]:
	path = sandbox["runner_temp"] / "ai-engine-fallbacks.txt"
	return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_fallback_is_recorded_and_reporter_jobs_own_the_telegram_note(sandbox: dict) -> None:
	notes = sandbox["tmp"] / "tg.txt"
	script = (
		f'tg_send_msg() {{ printf "%s|%s\\n" "$1" "$2" >> {shlex.quote(str(notes))}; }}; '
		f"rc=0; claude_run PLAN {shlex.quote(str(sandbox['prompt']))} out1 {shlex.quote(str(sandbox['work']))} || rc=$?; "
		'echo "RC=${rc}"'
	)
	result = _bash(sandbox, script, AI_ENGINE_FALLBACK_REPORTER="true")
	assert _rc(result) == 75, result.stderr
	assert "AI_ENGINE_FALLBACK role=PLAN reason=no_credential" in result.stderr
	assert _fallback_record(sandbox) == ["role=PLAN reason=no_credential class="]
	# The engine-fallback-report job sends the alert instead.
	assert not notes.exists()


def test_fallback_record_file_can_be_overridden(sandbox: dict) -> None:
	record = sandbox["tmp"] / "custom-record.txt"
	result = _bash(sandbox, "ai_engine_fallback IMPLEMENT cli_missing; ai_engine_fallback IMPLEMENT all_accounts_failed capacity; ai_engine_fallback PLAN x bogus", AI_ENGINE_FALLBACK_RECORD_FILE=str(record))
	assert result.returncode == 0, result.stderr
	assert record.read_text(encoding="utf-8").splitlines() == [
		"role=IMPLEMENT reason=cli_missing class=",
		"role=IMPLEMENT reason=all_accounts_failed class=capacity",
		"role=PLAN reason=x class=",
	]
	assert _fallback_record(sandbox) == []


def test_all_accounts_over_their_limit_is_a_capacity_fallback(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_LIMIT_a", B="TOK_LIMIT_b")
	result = _claude_run(sandbox)
	assert _rc(result) == 75, result.stderr
	assert _fallback_record(sandbox) == ["role=PLAN reason=all_accounts_failed class=capacity"]


def test_a_rejected_token_makes_all_accounts_failed_a_defect(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_LIMIT", B="TOK_AUTH")
	result = _claude_run(sandbox)
	assert _rc(result) == 75, result.stderr
	assert _fallback_record(sandbox) == ["role=PLAN reason=all_accounts_failed class="]


def test_every_bare_fallback_line_is_also_recorded() -> None:
	# Scripts that print AI_ENGINE_FALLBACK themselves (ai_engine.sh missing)
	# must append to the record, or the engine-fallback-report job never sees it.
	for path in sorted((REPO_ROOT / "scripts").glob("*.sh")):
		if path.name in ("ai_engine.sh", "review_untrusted_sandbox.sh", "ai_engine_fallback_report.sh"):
			continue
		lines = path.read_text(encoding="utf-8").splitlines()
		for index, line in enumerate(lines):
			if 'echo "AI_ENGINE_FALLBACK role=' not in line:
				continue
			window = "\n".join(lines[index:index + 5])
			assert "ai-engine-fallbacks.txt" in window and "AI_ENGINE_FALLBACK_RECORD_FILE" in window, f"{path.name}:{index + 1}"


def test_engine_fallback_report_is_wired_into_every_cut_over_workflow() -> None:
	for name, job in ENGINE_FALLBACK_WORKFLOWS.items():
		doc = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
		phase = doc["jobs"][job]
		assert phase["env"]["AI_ENGINE_FALLBACK_REPORTER"] == "true", name
		assert phase["outputs"]["engine_fallbacks"] == "${{ steps.engine_fallbacks.outputs.fallbacks }}", name
		assert phase["outputs"]["engine_fallback_alert_level"] == "${{ steps.engine_fallbacks.outputs.alert_msg_level }}", name
		pool = [step for step in phase["steps"] if step.get("name") == "Resolve Claude credential"]
		assert len(pool) == 1 and pool[0]["id"] == "claude_pool", name
		last = phase["steps"][-1]
		assert last["name"] == "Collect AI engine fallbacks" and last["id"] == "engine_fallbacks", name
		# clarify-respond gates every later step on its orchestrator metadata check.
		assert last["if"] in ("always()", "always() && steps.check_orchestrator.outputs.respond == 'true'"), name
		assert last["if"].startswith("always()") and last["continue-on-error"] is True, name
		assert last["env"]["CLAUDE_POOL_REASON"] == "${{ steps.claude_pool.outputs.reason }}", name
		assert '.codex-workflow-src/scripts/claude_engine.py"' in last["run"] and '"${engine_py}" fallbacks --record-file' in last["run"], name
		report = doc["jobs"]["engine-fallback-report"]
		assert report["needs"] == job and report["permissions"] == {}, name
		assert f"needs.{job}.outputs.engine_fallbacks != '[]'" in report["if"] and report["if"].startswith("always()"), name
		assert report["env"]["ENGINE_FALLBACK_PHASE"] == {"respond": "clarify_respond"}.get(job, job), name
		assert report["env"]["TG_BOT_SECRET"] == "${{ secrets.TG_BOT_SECRET }}", name
		steps = {step["name"]: step for step in report["steps"]}
		assert "ai_engine_fallback_report.sh" in steps["Report Claude engine fallbacks"]["run"], name
		assert steps["Report Claude engine fallbacks"]["env"]["ALERT_MSG_LEVEL"].startswith(f"${{{{ env.ALERT_MSG_LEVEL || needs.{job}.outputs.engine_fallback_alert_level ||"), name
	assert (REPO_ROOT / "scripts" / "ai_engine_fallback_report.sh").is_file()
