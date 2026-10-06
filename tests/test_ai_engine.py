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
import shutil
import shlex
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_isolation_fakes import docker_runs, enable_fake_isolation, install_fake_docker, short_temp_dir  # noqa: E402
# CI enumerates this module; collect the launch contract tests here as well.
from test_codex_isolated_exec_claude_contract import (  # noqa: E402
	test_claude_never_runs_on_the_host_without_the_isolation_helper,  # noqa: F401
	test_implement_prompt_marks_user_comments_as_untrusted,  # noqa: F401
)

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
account = "ok"
for prefix, outcome in (("TOK_LIMIT", "limit"), ("TOK_AUTH", "auth"), ("TOK_CRASH", "crash"), ("TOK_WRITE_MD", "write_md")):
	if token.startswith(prefix):
		account = outcome
		break
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
	"gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
	"credentials": {key: key in os.environ for key in (
		"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_PAT", "GH_HOST",
		"TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "TG_CHAT_ID", "OPENROUTER_API_KEY",
		"ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_RUNTIME_TOKEN",
	)},
	"gh_token_env": "GH_TOKEN" in os.environ,
	"secret_env_names": sorted(name for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "TG_BOT_SECRET") if name in os.environ),
	"settings": settings_text,
}
with open(log, "a", encoding="utf-8") as handle:
	handle.write(json.dumps(record) + "\n")
def emit(event):
	print(json.dumps(event), flush=True)
emit({"type": "system", "subtype": "init"})
if account == "limit":
	emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
	emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "API Error"})
	sys.exit(1)
if account == "auth":
	emit({"type": "result", "subtype": "success", "is_error": True, "result": "Failed to authenticate. API Error: 401 OAuth access token is invalid."})
	sys.exit(1)
if account == "crash":
	print("boom", file=sys.stderr)
	sys.exit(3)
if account == "write_md":
	with open("CLAUDE.md", "w", encoding="utf-8") as handle:
		handle.write("new instructions\n")
if token.startswith("TOK_TAMPER"):
	path = os.environ["FAKE_SUPPORT_FILE"]
	os.chmod(path, 0o755)
	with open(path, "a", encoding="utf-8") as handle:
		handle.write("# tampered\n")
emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.01, "usage": {"input_tokens": 1, "output_tokens": 2}})
'''

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
log = Path(__DOCKER_LOG__)
argv = sys.argv[1:]
if (argv[0] == "run" and "--init" in argv) or (argv[0] == "build" and "-t" not in argv):
	os.execv(str(Path(__file__).with_name("write-docker")), ["write-docker", *argv])
record = {"argv": argv, "env": dict(os.environ), "stdin": sys.stdin.read() if argv[0] == "run" else ""}
if argv[0] == "run":
	record["auxiliary_snapshots"] = {}
	record["auxiliary_configs"] = {}
	for arg in argv:
		if arg.startswith("type=bind,src=") and "/source-snapshot,dst=" in arg:
			snapshot = Path(arg.split("src=", 1)[1].split(",dst=", 1)[0])
			record["snapshot_files"] = sorted(str(p.relative_to(snapshot)) for p in snapshot.rglob("*") if p.is_file())
			record["snapshot_config"] = (snapshot / ".git" / "config").read_text() if (snapshot / ".git" / "config").exists() else ""
			if (snapshot / ".git").is_dir():
				record["snapshot_head"] = subprocess.check_output(["git", "-C", str(snapshot), "log", "-1", "--format=%s"], text=True).strip()
		if arg.startswith("type=bind,src=") and "/source-snapshot-" in arg:
			snapshot = Path(arg.split("src=", 1)[1].split(",dst=", 1)[0])
			destination = arg.split(",dst=", 1)[1].removesuffix(",readonly")
			record["auxiliary_snapshots"][destination] = sorted(str(p.relative_to(snapshot)) for p in snapshot.rglob("*") if p.is_file())
			record["auxiliary_configs"][destination] = (snapshot / ".git" / "config").read_text() if (snapshot / ".git" / "config").exists() else ""
		if arg.startswith("type=bind,src=") and ",dst=/home/agent/.claude/projects" in arg:
			session_mount = Path(arg.split("src=", 1)[1].split(",dst=", 1)[0])
			record["session_files"] = sorted(str(p.relative_to(session_mount)) for p in session_mount.rglob("*") if p.is_file())
		if arg.startswith("type=bind,src=") and "/extra-snapshots/" in arg:
			extra_mount = Path(arg.split("src=", 1)[1].split(",dst=", 1)[0])
			record["extra_snapshot_files"] = sorted(str(p.relative_to(extra_mount)) for p in extra_mount.rglob("*") if p.is_file())
			record["extra_snapshot_config"] = (extra_mount / ".git" / "config").read_text() if (extra_mount / ".git" / "config").exists() else ""
with log.open("a", encoding="utf-8") as handle:
	handle.write(json.dumps(record) + "\n")
if argv[:2] == ["image", "inspect"]:
	sys.exit(0 if log.with_name("docker-cache-hit").exists() else 1)
if argv[0] == "build":
	sys.exit(1 if log.with_name("docker-build-fail").exists() else 0)
if argv[0] == "run":
	mode_file = log.with_name("docker-mode")
	mode = mode_file.read_text().strip() if mode_file.exists() else "success"
	if mode == "limit-once":
		attempt_file = log.with_name("docker-attempt")
		mode = "success" if attempt_file.exists() else "limit"
		attempt_file.touch()
	if mode == "hang":
		import time
		time.sleep(30)
	if mode == "startup-fail":
		print("docker: container could not start", file=sys.stderr)
		sys.exit(125)
	if mode == "startup-kill":
		sys.exit(137)
	if mode == "cli-crash":
		print("CLAUDE_READ_CONTAINER_READY", file=sys.stderr)
		sys.exit(1)
	print("CLAUDE_READ_CONTAINER_READY", file=sys.stderr)
	if mode == "tamper-support":
		path = os.environ["FAKE_SUPPORT_FILE"]
		os.chmod(path, 0o755)
		with open(path, "a", encoding="utf-8") as handle:
			handle.write("# tampered\n")
	if mode == "limit":
		print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}))
		print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "API Error"}))
		sys.exit(1)
	print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done", "usage": {"input_tokens": 1}}))
'''

FAKE_PS = r'''#!/usr/bin/env python3
import sys
from pathlib import Path
if sys.argv[1:3] == ["-eo", "args="]:
	for process_path in Path("/proc").glob("[0-9]*"):
		try:
			print((process_path / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace"))
		except OSError:
			pass
	sys.exit(0)
try:
	print(Path(f"/proc/{sys.argv[-1]}/stat").read_text().split(") ", 1)[1].split()[0])
except (OSError, IndexError):
	sys.exit(1)
'''


@pytest.fixture()
def sandbox(tmp_path: Path):
	fake_bin = tmp_path / "bin"
	fake_bin.mkdir()
	fake = fake_bin / "claude"
	fake.write_text(FAKE_CLAUDE, encoding="utf-8")
	fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
	ps = fake_bin / "ps"
	ps.write_text(FAKE_PS, encoding="utf-8")
	ps.chmod(ps.stat().st_mode | stat.S_IXUSR)
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
	# A read-profile lock must never chmod the live repository in a test.
	support = tmp_path / "trusted-support"
	(support / "scripts").mkdir(parents=True)
	(support / ".claude" / "hooks").mkdir(parents=True)
	(support / ".github" / "ai").mkdir(parents=True)
	for filename in ("ai_engine.sh", "claude_engine.py", "claude_anthropic_relay.py", "claude_settings.json.tmpl", "codex_stall_guard.sh"):
		shutil.copy2(REPO_ROOT / "scripts" / filename, support / "scripts" / filename)
	(support / "scripts" / "clarify_sandbox").mkdir()
	shutil.copy2(REPO_ROOT / "scripts" / "clarify_sandbox" / "Dockerfile", support / "scripts" / "clarify_sandbox" / "Dockerfile")
	shutil.copy2(REPO_ROOT / ".claude/hooks/gh_api_write_guard.py", support / ".claude/hooks/gh_api_write_guard.py")
	shutil.copy2(REPO_ROOT / ".github/ai/claude_engine.json", support / ".github/ai/claude_engine.json")
	shutil.copy2(INSTRUCTIONS, support / "unattended_system_instructions.md")
	env = {
		key: value
		for key, value in os.environ.items()
		if key not in {"ALLOW_WORKFLOW_EDITS", "BASH_ENV", "ENV", "WORKSPACE_PATH"} and not key.startswith(("AI_ENGINE", "CLAUDE_", "ANTHROPIC_", "SUPPORT_", "TG_", "GITHUB_WORKSPACE", "GITHUB_EVENT_PATH"))
	}
	env.update(
		{
			"PATH": f"{fake_bin}:{os.environ['PATH']}",
			"HOME": str(home),
			"RUNNER_TEMP": str(runner_temp),
			"CLAUDE_ENGINE_POOL_DIR": str(pool),
			"SUPPORT_INSTRUCTIONS_FILE": str(INSTRUCTIONS),
			"FAKE_CLAUDE_LOG": str(tmp_path / "calls.jsonl"),
			"FAKE_SUPPORT_FILE": str(support / "scripts" / "ai_engine.sh"),
			"FAKE_CLAUDE_BIN": str(fake),
			"PYTHONDONTWRITEBYTECODE": "1",
			"CODEX_HEARTBEAT_INTERVAL_SECS": "30",
			"ALLOW_WORKFLOW_EDITS": "false",
			"ANTHROPIC_API_KEY": "must-not-reach-the-cli",
			"GH_TOKEN": "ghp_mustnotreachtheclaudecli0000000000000",
			"OPENROUTER_API_KEY": "must-not-reach-the-cli",
		}
	)
	# The broker's Unix socket path must stay under 108 bytes.
	env["RUNNER_TEMP"] = str(short_temp_dir())
	env = enable_fake_isolation(fake_bin, support / "scripts", env, passthrough_prefixes=("FAKE_",), short_temp=False)
	docker = fake_bin / "docker"
	docker.rename(fake_bin / "write-docker")
	docker.write_text(FAKE_DOCKER.replace("__DOCKER_LOG__", repr(str(tmp_path / "docker.jsonl"))), encoding="utf-8")
	docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
	yield {"tmp": tmp_path, "env": env, "pool": pool, "work": work, "prompt": prompt, "home": home, "bin": fake_bin, "support": support, "ai_engine": support / "scripts" / "ai_engine.sh", "runner_temp": Path(env["RUNNER_TEMP"])}
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
		["bash", "-c", f"set -euo pipefail; source {shlex.quote(str(sandbox['support'] / 'scripts/ai_engine.sh'))}; {script}"],
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


def _docker_calls(sandbox: dict, command: str = "run") -> list[dict]:
	path = sandbox["tmp"] / "docker.jsonl"
	return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if json.loads(line)["argv"][0] == command] if path.exists() else []


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


def test_missing_cli_falls_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	# The fake runner executes the image CLI from its own path, not the host PATH.
	image_claude = sandbox["tmp"] / "image-claude"
	shutil.copy2(sandbox["bin"] / "claude", image_claude)
	install_fake_docker(sandbox["bin"], sandbox["bin"].parent / "fake-docker.jsonl", {
		"FAKE_CLAUDE_BIN": str(image_claude), "FAKE_CLAUDE_LOG": sandbox["env"]["FAKE_CLAUDE_LOG"],
	})
	(sandbox["bin"] / "claude").unlink()
	env_path = ":".join(part for part in sandbox["env"]["PATH"].split(":") if not (Path(part) / "claude").exists())
	result = _claude_run(sandbox, PATH=env_path)
	assert _rc(result) == 0, result.stderr
	assert [call["container_token"] for call in _calls(sandbox)] == ["isolated-placeholder"]
	assert "AI_ENGINE_FALLBACK" not in result.stderr


@pytest.mark.parametrize("symlink", (False, True))
def test_missing_isolation_helper_falls_back(sandbox: dict, symlink: bool) -> None:
	_accounts(sandbox, A="TOK_OK")
	helper = sandbox["ai_engine"].parent / "codex_isolated_exec.sh"
	helper.unlink()
	if symlink:
		helper.symlink_to(REPO_ROOT / "scripts" / "claude_engine.py")
	result = _claude_run(sandbox, "IMPLEMENT")
	assert _rc(result) == 75
	assert "AI_ENGINE_FALLBACK role=IMPLEMENT reason=support_missing" in result.stderr
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
	assert [call["container_token"] for call in calls] == ["isolated-placeholder"] * 3
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
		"--tools": "Read,Grep,Glob,Bash,Edit,Write",
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
	assert call["secret_env_names"] == []
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
	token_file = sandbox["pool"] / "tokens" / "A"
	assert not any(token_file.is_relative_to(Path(arg.split(",", 2)[1][4:])) for arg in run["argv"] if arg.startswith("type=bind,src="))
	assert any(arg.endswith(",readonly") is False and f"dst={sandbox['work'].resolve()}" in arg for arg in run["argv"])


def test_read_role_command_line(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["work"] / ".env").write_text("PRIVATE=do-not-mount\n")
	(sandbox["work"] / "file.txt").write_text("safe\n")
	(sandbox["work"] / "untracked.json").write_text('{"token": "secret"}\n')
	subprocess.run(["git", "-C", str(sandbox["work"]), "init", "-q"], check=True)
	subprocess.run(["git", "-C", str(sandbox["work"]), "add", "file.txt"], check=True)
	subprocess.run(["git", "-C", str(sandbox["work"]), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
	subprocess.run(["git", "-C", str(sandbox["work"]), "config", "http.extraheader", "AUTHORIZATION: hidden"], check=True)
	result = _claude_run(sandbox, "SECURITY_AUDIT", AI_ENGINE_MODEL_HINT="claude-sonnet-5-5")
	assert _rc(result) == 0, result.stderr
	if _calls(sandbox):
		isolated_argv = _calls(sandbox)[0]["argv"]
		assert isolated_argv[isolated_argv.index("--tools") + 1] == "Read,Grep,Glob,Bash"
		assert isolated_argv[isolated_argv.index("--permission-mode") + 1] == "dontAsk"
		assert isolated_argv[isolated_argv.index("--model") + 1] == "claude-sonnet-5-5"
		# A read profile gets the read-only snapshot (answer Q17 A).
		isolated_run = docker_runs(sandbox["bin"].parent / "fake-docker.jsonl")[-1]
		assert any(f"dst={sandbox['work'].resolve()},readonly" in arg for arg in isolated_run["argv"])
		return
	assert _calls(sandbox) == []
	call = _docker_calls(sandbox)[0]
	argv = call["argv"]
	assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
	assert "--read-only" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
	assert "--user" in argv
	assert "CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder" in argv
	assert "ANTHROPIC_BASE_URL=http://127.0.0.1:8765" in argv
	assert "--tools Read,Grep,Glob,Bash" in argv[-2]
	assert "--model \"${CLAUDE_MODEL}\"" in argv[-2]
	assert "CLAUDE_MODEL=claude-sonnet-5-5" in argv
	assert subprocess.run(["bash", "-n"], input=argv[-2], capture_output=True, text=True).returncode == 0
	assert call["stdin"] == "do the thing\n"
	assert (sandbox["tmp"] / "out.txt").read_text() == "done"
	assert "CLAUDE_ISOLATION role=SECURITY_AUDIT profile=read mode=container" in result.stderr
	assert "CLAUDE_ISOLATION role=SECURITY_AUDIT profile=read mode=container" in result.stderr.splitlines()
	assert "CLAUDE_READ_ISOLATION role=SECURITY_AUDIT outcome=ready reason=none files=1 git=copied extra_dirs=0" in result.stderr
	assert "AI_ENGINE_SUPPORT_LOCK role=SECURITY_AUDIT outcome=locked" in result.stderr
	assert "AI_ENGINE_SUPPORT_LOCK role=SECURITY_AUDIT outcome=verified" in result.stderr
	snapshot_mount = next(arg for arg in argv if arg.startswith("type=bind,src=") and f",dst={sandbox['work']},readonly" in arg)
	snapshot = Path(snapshot_mount.split("src=", 1)[1].split(",dst=", 1)[0])
	assert snapshot != sandbox["work"] and "file.txt" in call["snapshot_files"]
	assert ".env" not in call["snapshot_files"]
	assert "CLAUDE.md" not in call["snapshot_files"]
	assert "untracked.json" not in call["snapshot_files"]
	assert call["snapshot_head"] == "base"
	assert "hidden" not in call["snapshot_config"]
	assert not any("dst=/git-objects" in arg or f"src={sandbox['work'] / '.git' / 'objects'}" in arg for arg in argv)
	assert not snapshot.exists()
	assert all(f"src={sandbox['work']}," not in arg for arg in argv)
	assert stat.S_IMODE(sandbox["support"].stat().st_mode) == 0o755
	settings_path = next(arg.split("src=", 1)[1].split(",dst=", 1)[0] for arg in argv if "dst=/settings.json" in arg)
	policy = json.loads(Path(settings_path).read_text(encoding="utf-8"))
	assert policy["hooks"]["PreToolUse"][1]["hooks"][0]["command"] == 'python3 "/read-guard.py" guard-read-bash'
	assert not any(str(sandbox["pool"]) in arg for arg in argv)
	assert "TOK_OK" not in json.dumps(call) + result.stdout + result.stderr
	for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
		assert key not in call["env"]


def test_read_isolation_tamper_stops_before_checkout_classifier(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["tmp"] / "docker-mode").write_text("tamper-support")
	result = _claude_run(sandbox, "SECURITY_AUDIT")
	assert _rc(result) == 86, result.stderr
	assert "AI_ENGINE_SUPPORT_LOCK role=SECURITY_AUDIT outcome=tampered" in result.stderr
	assert "AI_ENGINE_FALLBACK" not in result.stderr
	assert not (sandbox["tmp"] / "out.txt").exists()
	assert stat.S_IMODE((sandbox["support"] / "scripts" / "ai_engine.sh").stat().st_mode) == 0o644


def test_read_isolation_unsafe_support_lock_is_terminal(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["support"] / "scripts" / "unsafe.sh").symlink_to(sandbox["work"] / "CLAUDE.md")
	result = _claude_run(sandbox, "SECURITY_AUDIT")
	assert _rc(result) == 86, result.stderr
	assert "symlink in trusted support" in result.stderr
	assert "AI_ENGINE_SUPPORT_LOCK role=SECURITY_AUDIT outcome=tampered" in result.stderr
	assert "AI_ENGINE_FALLBACK" not in result.stderr
	assert not _docker_calls(sandbox)
	assert not (sandbox["tmp"] / "out.txt").exists()


@pytest.mark.parametrize("tamper", [False, True])
@pytest.mark.parametrize("signal_name, signal_rc", [("INT", 130), ("TERM", 143)])
def test_read_isolation_signal_preserves_support_failure(sandbox: dict, tamper: bool, signal_name: str, signal_rc: int) -> None:
	_accounts(sandbox, A="TOK_OK")
	mutation = 'chmod 0755 "${FAKE_SUPPORT_FILE}"; printf "# tampered\\n" >> "${FAKE_SUPPORT_FILE}";' if tamper else ""
	result = _bash(sandbox, f'_ai_engine_claude_run_isolated() {{ {mutation} kill -{signal_name} "${{BASHPID}}"; }}; '
		f'claude_run SECURITY_AUDIT {shlex.quote(str(sandbox["prompt"]))} '
		f'{shlex.quote(str(sandbox["tmp"] / "out.txt"))} {shlex.quote(str(sandbox["work"]))}')
	assert result.returncode == (86 if tamper else signal_rc), result.stderr
	assert f"AI_ENGINE_SUPPORT_LOCK role=SECURITY_AUDIT outcome={'tampered' if tamper else 'verified'}" in result.stderr
	assert stat.S_IMODE((sandbox["support"] / "scripts" / "ai_engine.sh").stat().st_mode) == 0o644


def test_read_isolation_image_cache_skips_build(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["tmp"] / "docker-cache-hit").touch()
	assert _rc(_claude_run(sandbox, "SECURITY_AUDIT")) == 0
	assert not _docker_calls(sandbox, "build")


def test_read_isolation_build_failure_falls_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["tmp"] / "docker-build-fail").touch()
	result = _claude_run(sandbox, "SECURITY_AUDIT")
	assert _rc(result) == 75
	assert "AI_ENGINE_FALLBACK role=SECURITY_AUDIT reason=isolation_image_build_failed" in result.stderr
	assert not _docker_calls(sandbox)
	assert _calls(sandbox) == []


def test_read_snapshot_limit_falls_back_without_mounting_checkout(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["work"] / "file.txt").write_text("too big")
	args = " ".join(shlex.quote(str(part)) for part in ("SECURITY_AUDIT", sandbox["prompt"], sandbox["tmp"] / "out.txt", sandbox["work"]))
	result = _bash(sandbox, f'_ai_engine_isolation_image() {{ printf "%s\\n" fake-image; }}; rc=0; claude_run {args} || rc=$?; echo "RC=${{rc}}"', CLAUDE_READ_SNAPSHOT_MAX_BYTES="1")
	assert _rc(result) == 75, result.stderr
	assert "reason=isolation_snapshot_failed" in result.stderr
	assert not _docker_calls(sandbox)


def test_heal_auxiliary_worktrees_are_filtered_and_mounted_from_snapshots(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	engine_config_path = sandbox["support"] / ".github" / "ai" / "claude_engine.json"
	engine_config = json.loads(engine_config_path.read_text(encoding="utf-8"))
	engine_config["hide_claude_md"] = True
	engine_config_path.write_text(json.dumps(engine_config), encoding="utf-8")
	runtime = sandbox["tmp"] / "heal-runtime"
	runtime.mkdir()
	for name in ("heal_src", "heal_branch_tip"):
		checkout = runtime / name
		checkout.mkdir()
		(checkout / "code.sh").write_text("safe\n")
		(checkout / ".env").write_text("secret\n")
		(checkout / "id_ed25519").write_text("secret\n")
		(checkout / "CLAUDE.md").write_text("hide me\n")
		subprocess.run(["git", "-C", str(checkout), "init", "-q"], check=True)
		subprocess.run(["git", "-C", str(checkout), "add", "code.sh", ".env"], check=True)
		subprocess.run(["git", "-C", str(checkout), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
		subprocess.run(["git", "-C", str(checkout), "config", "http.extraheader", "AUTHORIZATION: TEST_ONLY_CREDENTIAL"], check=True)
	# AF_UNIX limits socket paths to 107 bytes; this test's pytest tmp name is long.
	with tempfile.TemporaryDirectory(prefix="heal-", dir="/tmp") as short_temp:
		result = _claude_run(sandbox, "WORKFLOW_HEAL", RUNNER_TEMP=short_temp, RUNTIME_DIR=str(runtime),
			AI_ENGINE_ISOLATED_READ_PATHS=f"{runtime / 'heal_src'}:{runtime / 'heal_branch_tip'}")
		assert _rc(result) == 0, result.stderr
		call = _docker_calls(sandbox)[0]
		for name in ("heal_src", "heal_branch_tip"):
			checkout = runtime / name
			files = call["auxiliary_snapshots"][str(checkout)]
			assert "code.sh" in files and ".git/config" in files
			assert not {".env", "id_ed25519", "CLAUDE.md"} & set(files)
			assert "TEST_ONLY_CREDENTIAL" not in call["auxiliary_configs"][str(checkout)]
			assert not any(f"src={checkout}," in arg for arg in call["argv"])
		assert not list(Path(short_temp).glob("claude-run-*/source-snapshot-*"))


def test_heal_auxiliary_snapshot_shares_file_budget(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	runtime = sandbox["tmp"] / "heal-runtime"
	checkout = runtime / "heal_src"
	checkout.mkdir(parents=True)
	(checkout / "code.sh").write_text("safe\n")
	result = _claude_run(sandbox, "WORKFLOW_HEAL", RUNTIME_DIR=str(runtime),
		AI_ENGINE_ISOLATED_READ_PATHS=str(checkout), CLAUDE_READ_SNAPSHOT_MAX_FILES="1")
	assert _rc(result) == 75, result.stderr
	assert "reason=isolation_snapshot_failed" in result.stderr
	assert not _docker_calls(sandbox)


def test_heal_duplicate_auxiliary_paths_fail_before_docker(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	runtime = sandbox["tmp"] / "heal-runtime"
	checkout = runtime / "heal_src"
	checkout.mkdir(parents=True)
	result = _claude_run(sandbox, "WORKFLOW_HEAL", RUNTIME_DIR=str(runtime),
		AI_ENGINE_ISOLATED_READ_PATHS=f"{checkout}:{checkout}")
	assert _rc(result) == 75, result.stderr
	assert "reason=isolation_read_path_invalid" in result.stderr
	assert not _docker_calls(sandbox)


@pytest.mark.parametrize("invalid", ["other", "symlink", "extra_role"])
def test_heal_auxiliary_paths_fail_closed_on_unapproved_mount(sandbox: dict, invalid: str) -> None:
	_accounts(sandbox, A="TOK_OK")
	runtime = sandbox["tmp"] / "heal-runtime"
	runtime.mkdir()
	checkout = runtime / "heal_src"
	if invalid == "symlink":
		checkout.symlink_to(sandbox["work"], target_is_directory=True)
	else:
		checkout.mkdir()
	path = sandbox["work"] if invalid == "other" else checkout
	role = "SECURITY_AUDIT" if invalid == "extra_role" else "WORKFLOW_HEAL"
	result = _claude_run(sandbox, role, RUNTIME_DIR=str(runtime), AI_ENGINE_ISOLATED_READ_PATHS=str(path))
	assert _rc(result) == 75, result.stderr
	assert "reason=isolation_read_path_invalid" in result.stderr
	assert not _docker_calls(sandbox)


@pytest.mark.parametrize("mode", ["startup-fail", "startup-kill"])
def test_read_isolation_container_start_failure_falls_back(sandbox: dict, mode: str) -> None:
	_accounts(sandbox, A="TOK_OK", B="TOK_OK")
	(sandbox["tmp"] / "docker-mode").write_text(mode)
	result = _claude_run(sandbox, "SECURITY_AUDIT")
	assert _rc(result) == 75, result.stderr
	assert "AI_ENGINE_FALLBACK role=SECURITY_AUDIT reason=isolation_container_start_failed" in result.stderr
	assert len(_docker_calls(sandbox)) == 1
	assert _calls(sandbox) == []


def test_read_isolation_cli_crash_is_not_startup_failure(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK", B="TOK_OK")
	(sandbox["tmp"] / "docker-mode").write_text("cli-crash")
	result = _claude_run(sandbox, "SECURITY_AUDIT")
	assert _rc(result) == 1, result.stderr
	assert "AI_ENGINE_FALLBACK" not in result.stderr
	assert len(_docker_calls(sandbox)) == 1


@pytest.mark.parametrize("reason,env", [
	("isolation_pool_overlap", {"pool_in_work": True}),
	("isolation_read_path_invalid", {"AI_ENGINE_ISOLATED_READ_PATHS": "/not-a-directory"}),
])
def test_read_isolation_rejects_invalid_mounts(sandbox: dict, reason: str, env: dict) -> None:
	if env.pop("pool_in_work", False):
		sandbox["pool"] = sandbox["work"] / "pool"
		(sandbox["pool"] / "tokens").mkdir(parents=True)
		env["CLAUDE_ENGINE_POOL_DIR"] = str(sandbox["pool"])
	_accounts(sandbox, A="TOK_OK")
	result = _claude_run(sandbox, "SECURITY_AUDIT", **env)
	assert _rc(result) == 75
	assert f"reason={reason}" in result.stderr
	assert not _docker_calls(sandbox)
	assert _calls(sandbox) == []


def test_read_isolation_rejects_session_mount_containing_pool(sandbox: dict) -> None:
	sandbox["pool"] = sandbox["runner_temp"]
	(sandbox["pool"] / "tokens").mkdir()
	_accounts(sandbox, A="TOK_OK")
	args = " ".join(shlex.quote(str(part)) for part in (
		"SECURITY_AUDIT", sandbox["prompt"], sandbox["tmp"] / "out.txt", sandbox["work"],
		"0123abcd-0000-4000-8000-00000000abcd",
	))
	script = (
		'command() { if [ "$1" = -v ] && [ "$2" = ps ]; then return 0; fi; builtin command "$@"; }; '
		'_ai_engine_isolation_image() { printf "%s\\n" test-image; }; '
		f'rc=0; claude_run {args} || rc=$?; echo "RC=${{rc}}"'
	)
	result = _bash(sandbox, script, CLAUDE_ENGINE_POOL_DIR=str(sandbox["pool"]))
	assert _rc(result) == 75, result.stderr
	assert "reason=isolation_pool_overlap" in result.stderr
	assert not _docker_calls(sandbox)


def test_read_isolation_mounts_only_selected_session(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	session = "0123abcd-0000-4000-8000-00000000abcd"
	sessions = sandbox["runner_temp"] / "claude-read-sessions"
	(sessions / "other-session" / "project").mkdir(parents=True)
	(sessions / "other-session" / "project" / "private.jsonl").write_text("foreign transcript")
	(sessions / session / "project").mkdir(parents=True)
	(sessions / session / "project" / f"{session}.jsonl").write_text("own transcript")
	result = _claude_run(sandbox, "SECURITY_AUDIT", session=session)
	assert _rc(result) == 0, result.stderr
	call = _docker_calls(sandbox)[0]
	assert f"type=bind,src={sessions / session},dst=/home/agent/.claude/projects" in call["argv"]
	assert call["session_files"] == [f"project/{session}.jsonl"]
	assert call["argv"][-1] == session and call["argv"][-2] == "--resume"
	assert "foreign transcript" not in json.dumps(call)


def test_read_isolation_rejects_symlinked_session(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	session = "0123abcd-0000-4000-8000-00000000abcd"
	sessions = sandbox["runner_temp"] / "claude-read-sessions"
	(sessions / "other-session").mkdir(parents=True)
	(sessions / session).symlink_to(sessions / "other-session", target_is_directory=True)
	result = _claude_run(sandbox, "SECURITY_AUDIT", session=session)
	assert _rc(result) == 75, result.stderr
	assert "reason=isolation_session_dir_unavailable" in result.stderr
	assert not _docker_calls(sandbox)


def test_read_isolation_missing_docker_falls_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	args = " ".join(shlex.quote(str(part)) for part in ("SECURITY_AUDIT", sandbox["prompt"], sandbox["tmp"] / "out.txt", sandbox["work"]))
	script = f'command() {{ if [ "$1" = -v ] && [ "$2" = docker ]; then return 1; fi; builtin command "$@"; }}; rc=0; claude_run {args} || rc=$?; echo "RC=${{rc}}"; '
	script += 'for name in AI_ENGINE_ISOLATION_WORKDIR AI_ENGINE_ISOLATION_FAILURE AI_ENGINE_ISOLATION_GUARD AI_ENGINE_ISOLATION_PATHS AI_ENGINE_ISOLATION_MASKS; do if declare -p "$name" >/dev/null 2>&1; then echo "leaked=$name"; fi; done'
	result = _bash(sandbox, script)
	assert _rc(result) == 75
	assert "reason=isolation_docker_missing" in result.stderr
	assert _calls(sandbox) == []
	assert "leaked=" not in result.stdout


def test_read_isolation_missing_python_falls_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	args = " ".join(shlex.quote(str(part)) for part in ("SECURITY_AUDIT", sandbox["prompt"], sandbox["tmp"] / "out.txt", sandbox["work"]))
	result = _bash(sandbox, f'command() {{ if [ "$1" = -v ] && [ "$2" = python3 ]; then return 1; fi; builtin command "$@"; }}; rc=0; claude_run {args} || rc=$?; echo "RC=${{rc}}"')
	assert _rc(result) == 75
	assert "reason=isolation_python_missing" in result.stderr
	assert _calls(sandbox) == []


def test_read_isolation_missing_support_falls_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	support_scripts = sandbox["tmp"] / "support-scripts"
	support_scripts.mkdir()
	(support_scripts / "claude_engine.py").write_bytes((REPO_ROOT / "scripts" / "claude_engine.py").read_bytes())
	args = " ".join(shlex.quote(str(part)) for part in ("SECURITY_AUDIT", sandbox["prompt"], sandbox["tmp"] / "out.txt", sandbox["work"]))
	result = _bash(sandbox, f'_AI_ENGINE_DIR={shlex.quote(str(support_scripts))}; rc=0; claude_run {args} || rc=$?; echo "RC=${{rc}}"')
	assert _rc(result) == 75
	assert "reason=policy_unavailable" in result.stderr
	assert _calls(sandbox) == []


def test_read_isolation_relay_unavailable_and_account_rotation(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK", B="TOK_OK")
	(sandbox["pool"] / "tokens" / "A").chmod(0o644)  # The host relay refuses insecure token files.
	result = _claude_run(sandbox, "SECURITY_AUDIT")
	assert _rc(result) == 0, result.stderr
	assert "reason=relay_unavailable" in result.stderr
	run_dir = Path(next(line[8:] for line in result.stdout.splitlines() if line.startswith("RUN_DIR=")))
	assert (run_dir / "successful-transcript.jsonl").resolve() == run_dir / "transcript-B.jsonl"
	(sandbox["pool"] / "tokens" / "A").chmod(0o600)
	(sandbox["tmp"] / "docker-mode").write_text("limit-once")
	result = _claude_run(sandbox, "SECURITY_AUDIT")
	assert _rc(result) == 0, result.stderr
	assert "outcome=usage_limit" in result.stderr
	assert _calls(sandbox) == []


def test_read_isolation_all_relays_unavailable_falls_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["pool"] / "tokens" / "A").chmod(0o644)
	result = _claude_run(sandbox, "SECURITY_AUDIT")
	assert _rc(result) == 75
	assert "reason=relay_unavailable" in result.stderr
	assert "AI_ENGINE_FALLBACK role=SECURITY_AUDIT reason=isolation_relay_unavailable" in result.stderr
	assert _calls(sandbox) == []


def test_read_isolation_hides_claude_md_without_moving_host_file(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	support = sandbox["tmp"] / "support"
	(support / ".github" / "ai").mkdir(parents=True)
	(support / ".github" / "ai" / "claude_engine.json").write_text('{"hide_claude_md": true}', encoding="utf-8")
	result = _claude_run(sandbox, "SECURITY_AUDIT", SUPPORT_ROOT_DIR=str(support))
	assert _rc(result) == 0, result.stderr
	assert (sandbox["work"] / "CLAUDE.md").read_text(encoding="utf-8") == "checkout CLAUDE.md\n"
	assert "CLAUDE.md" not in _docker_calls(sandbox)[0]["snapshot_files"]


def test_read_isolation_mounts_only_sanitized_extra_directories(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	extra = sandbox["tmp"] / "heal_source"
	extra.mkdir()
	(extra / "source.txt").write_text("safe\n", encoding="utf-8")
	(extra / "private.key").write_text("secret\n", encoding="utf-8")
	subprocess.run(["git", "init", "-q", str(extra)], check=True)
	subprocess.run(["git", "-C", str(extra), "add", "source.txt"], check=True)
	subprocess.run(["git", "-C", str(extra), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "heal-source"], check=True)
	subprocess.run(["git", "-C", str(extra), "config", "http.extraheader", "AUTHORIZATION: hidden"], check=True)
	(extra / "untracked.json").write_text("secret\n", encoding="utf-8")
	result = _claude_run(sandbox, "WORKFLOW_HEAL", AI_ENGINE_READ_EXTRA_DIRS=f"{extra}:{sandbox['pool']}")
	assert _rc(result) == 0, result.stderr
	assert "extra_dirs=1" in result.stderr
	call = _docker_calls(sandbox)[0]
	assert "source.txt" in call["extra_snapshot_files"]
	assert "private.key" not in call["extra_snapshot_files"]
	assert "untracked.json" not in call["extra_snapshot_files"]
	assert "extraheader" not in call["extra_snapshot_config"]
	argv = call["argv"]
	assert f"dst={extra},readonly" in " ".join(argv)
	assert str(sandbox["pool"]) not in " ".join(argv)


def test_read_isolation_hides_claude_md_in_extra_directories(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	extra = sandbox["tmp"] / "heal_source"
	extra.mkdir()
	(extra / "CLAUDE.md").write_text("private marker\n", encoding="utf-8")
	(extra / "source.txt").write_text("safe\n", encoding="utf-8")
	support = sandbox["tmp"] / "support"
	(support / ".github" / "ai").mkdir(parents=True)
	(support / ".github" / "ai" / "claude_engine.json").write_text('{"hide_claude_md": true}', encoding="utf-8")
	result = _claude_run(sandbox, "WORKFLOW_HEAL", AI_ENGINE_READ_EXTRA_DIRS=str(extra), SUPPORT_ROOT_DIR=str(support))
	assert _rc(result) == 0, result.stderr
	assert "CLAUDE.md" not in _docker_calls(sandbox)[0]["extra_snapshot_files"]
	assert (extra / "CLAUDE.md").read_text(encoding="utf-8") == "private marker\n"


@pytest.mark.parametrize("limit_name, limit_value", [
	("CLAUDE_READ_SNAPSHOT_MAX_FILES", "2"),
	("CLAUDE_READ_SNAPSHOT_MAX_BYTES", "24"),
])
def test_read_isolation_shares_snapshot_budget_with_extra_directories(sandbox: dict, limit_name: str, limit_value: str) -> None:
	_accounts(sandbox, A="TOK_OK")
	first = sandbox["tmp"] / "first-source"
	second = sandbox["tmp"] / "second-source"
	for source in (first, second):
		source.mkdir()
		(source / "source.txt").write_text("safe\n", encoding="utf-8")
	result = _claude_run(sandbox, "WORKFLOW_HEAL", AI_ENGINE_READ_EXTRA_DIRS=f"{first}:{second}", **{limit_name: limit_value})
	assert _rc(result) == 75, result.stderr
	assert "outcome=rejected reason=isolation_snapshot_failed files=2 git=none extra_dirs=1" in result.stderr
	assert not _docker_calls(sandbox)


def test_read_isolation_reports_prepared_extras_on_snapshot_failure(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	first = sandbox["tmp"] / "first-source"
	first.mkdir()
	(first / "source.txt").write_text("safe\n", encoding="utf-8")
	second = sandbox["tmp"] / "broken-source"
	(second / ".git").mkdir(parents=True)
	result = _claude_run(sandbox, "WORKFLOW_HEAL", AI_ENGINE_READ_EXTRA_DIRS=f"{first}:{second}")
	assert _rc(result) == 75, result.stderr
	assert "outcome=rejected reason=isolation_snapshot_failed files=2 git=none extra_dirs=1" in result.stderr
	assert not _docker_calls(sandbox)


def test_read_isolation_enforces_container_timeout(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["tmp"] / "docker-mode").write_text("hang")
	result = _claude_run(sandbox, "SECURITY_AUDIT", CLAUDE_READ_ISOLATION_MAX_SECS="1")
	assert _rc(result) == 75, result.stderr
	assert "AI_ENGINE_FALLBACK role=SECURITY_AUDIT reason=isolation_container_start_failed" in result.stderr
	assert _docker_calls(sandbox)


def test_read_isolation_relay_failure_falls_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	args = " ".join(shlex.quote(str(part)) for part in ("SECURITY_AUDIT", sandbox["prompt"], sandbox["tmp"] / "out.txt", sandbox["work"]))
	script = (
		'_ai_engine_py() { if [ "$1" = config ] && [ "$2" = --key ] && [ "$3" = probe_model ]; '
		'then echo invalid-model; else PYTHONDONTWRITEBYTECODE=1 python3 "${_AI_ENGINE_DIR}/claude_engine.py" "$@"; fi; }; '
		f'rc=0; claude_run {args} || rc=$?; echo "RC=${{rc}}"'
	)
	result = _bash(sandbox, script)
	assert _rc(result) == 75, result.stderr
	assert "account=A outcome=crashed reason=relay_unavailable" in result.stderr
	assert "reason=isolation_relay_unavailable" in result.stderr
	assert not _docker_calls(sandbox)


def test_read_isolation_masks_checkout_credential_and_claude_md(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	subprocess.run(["git", "init", "-q", str(sandbox["work"])], check=True)
	subprocess.run(["git", "-C", str(sandbox["work"]), "config", "http.https://github.com/.extraheader", "AUTHORIZATION: basic SECRET"], check=True)
	subprocess.run(["git", "-C", str(sandbox["work"]), "config", "http.extraheader", "AUTHORIZATION: basic TOP_LEVEL_SECRET"], check=True)
	for checkout_name in (".codex-workflow-src", ".codex-workflow-src-main"):
		checkout_path = sandbox["work"] / checkout_name
		subprocess.run(["git", "init", "-q", str(checkout_path)], check=True)
		subprocess.run(["git", "-C", str(checkout_path), "config", "http.https://github.com/.extraheader", "AUTHORIZATION: basic NESTED_SECRET"], check=True)
		assert "NESTED_SECRET" in (checkout_path / ".git" / "config").read_text()
	for push_url in ("https://user:PASS_ONE@example.com/repo", "https://user:PASS_TWO@example.com/repo"):
		subprocess.run(["git", "-C", str(sandbox["work"]), "config", "--add", "remote.origin.pushurl", push_url], check=True)
	subprocess.run(["git", "-C", str(sandbox["work"]), "add", "CLAUDE.md"], check=True)
	subprocess.run(["git", "-C", str(sandbox["work"]), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
	support = sandbox["tmp"] / "support"
	(support / ".github" / "ai").mkdir(parents=True)
	(support / ".github" / "ai" / "claude_engine.json").write_text('{"hide_claude_md": true}')
	result = _claude_run(sandbox, "SECURITY_AUDIT", SUPPORT_ROOT_DIR=str(support))
	assert _rc(result) == 0, result.stderr
	argv = _docker_calls(sandbox)[0]["argv"]
	assert "SECRET" not in json.dumps(_docker_calls(sandbox))
	assert "SECRET" in (sandbox["work"] / ".git" / "config").read_text()
	assert "PASS_TWO" in (sandbox["work"] / ".git" / "config").read_text()
	assert (sandbox["work"] / "CLAUDE.md").read_text() == "checkout CLAUDE.md\n"
	snapshot_mount = next(arg for arg in argv if arg.startswith("type=bind,src=") and f",dst={sandbox['work']},readonly" in arg)
	assert "/source-snapshot,dst=" in snapshot_mount
	assert "CLAUDE.md" not in _docker_calls(sandbox)[0]["snapshot_files"]
	assert not any(".codex-workflow-src" in path for path in _docker_calls(sandbox)[0]["snapshot_files"])
	assert "SECRET" not in _docker_calls(sandbox)[0]["snapshot_config"]
	assert not any("/git-mask/" in arg for arg in argv)


def test_read_isolation_fails_closed_if_git_config_cannot_be_masked(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	subprocess.run(["git", "init", "-q", str(sandbox["work"])], check=True)
	(sandbox["work"] / ".git" / "config").write_text("[http \"https://github.com/\"]\n\textraheader=SECRET\n[broken\n")
	args = " ".join(shlex.quote(str(part)) for part in ("SECURITY_AUDIT", sandbox["prompt"], sandbox["tmp"] / "out.txt", sandbox["work"]))
	result = _bash(sandbox, f'_ai_engine_isolation_image() {{ printf "%s\\n" fake-image; }}; rc=0; claude_run {args} || rc=$?; echo "RC=${{rc}}"')
	assert _rc(result) == 75
	assert "reason=isolation_snapshot_failed" in result.stderr
	assert not _docker_calls(sandbox)


def test_read_isolation_session_dir_failure_reports_its_cause(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["runner_temp"] / "claude-read-sessions").write_text("not a directory")
	args = " ".join(shlex.quote(str(part)) for part in (
		"SECURITY_AUDIT", sandbox["prompt"], sandbox["tmp"] / "out.txt", sandbox["work"],
		"0123abcd-0000-4000-8000-00000000abcd",
	))
	# This path fails before Docker starts or the reaper needs ps.
	test_script = (
		'command() { if [ "$1" = -v ] && [ "$2" = ps ]; then return 0; fi; builtin command "$@"; }; '
		'_ai_engine_isolation_image() { printf "%s\\n" test-image; }; '
		f'rc=0; claude_run {args} || rc=$?; echo "RC=${{rc}}"'
	)
	result = _bash(sandbox, test_script)
	assert _rc(result) == 75, result.stderr
	assert "AI_ENGINE_FALLBACK role=SECURITY_AUDIT reason=isolation_session_dir_unavailable" in result.stderr
	assert not _docker_calls(sandbox)


def test_read_isolation_pool_removed_after_preflight_reports_its_cause(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	args = " ".join(shlex.quote(str(part)) for part in (
		"SECURITY_AUDIT", sandbox["prompt"], sandbox["tmp"] / "out.txt", sandbox["work"],
		"0123abcd-0000-4000-8000-00000000abcd",
	))
	test_script = (
		'command() { if [ "$1" = -v ] && [ "$2" = ps ]; then return 0; fi; builtin command "$@"; }; '
		'_ai_engine_isolation_image() { mv -- "$CLAUDE_ENGINE_POOL_DIR" "${CLAUDE_ENGINE_POOL_DIR}.gone"; printf "%s\\n" test-image; }; '
		f'rc=0; claude_run {args} || rc=$?; echo "RC=${{rc}}"'
	)
	result = _bash(sandbox, test_script)
	assert _rc(result) == 75, result.stderr
	assert "AI_ENGINE_FALLBACK role=SECURITY_AUDIT reason=isolation_pool_unavailable" in result.stderr
	assert not _docker_calls(sandbox)


def test_read_isolation_rotates_accounts(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK", B="TOK_OK")
	# The stub emits a rate limit on the first attempt only.
	(sandbox["tmp"] / "docker-mode").write_text("limit-once")
	result = _claude_run(sandbox, "SECURITY_AUDIT")
	assert _rc(result) == 0, result.stderr
	assert len(_docker_calls(sandbox)) == 2
	assert "account=A outcome=usage_limit" in result.stderr
	assert "account=B outcome=success" in result.stderr


def test_read_isolation_reaps_container_on_parent_sigkill(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["tmp"] / "docker-mode").write_text("hang")
	args = " ".join(shlex.quote(str(part)) for part in ("SECURITY_AUDIT", sandbox["prompt"], sandbox["tmp"] / "out.txt", sandbox["work"]))
	process = subprocess.Popen(
		["bash", "-c", f'source {shlex.quote(str(sandbox["support"] / "scripts/ai_engine.sh"))}; claude_run {args}'],
		cwd=sandbox["tmp"], env=sandbox["env"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
		start_new_session=True,
	)
	try:
		deadline = time.monotonic() + 12
		while time.monotonic() < deadline and not _docker_calls(sandbox):
			time.sleep(0.1)
		assert _docker_calls(sandbox), "docker run was not reached"
		process.kill()
		process.wait(timeout=5)
		while time.monotonic() < deadline and not any(
			call["argv"][:2] == ["rm", "-f"] for call in _docker_calls(sandbox, "rm")
		):
			time.sleep(0.2)
		assert _docker_calls(sandbox, "rm"), "orphan reaper did not remove the container"
		while time.monotonic() < deadline:
			broker_processes = subprocess.run([str(sandbox["bin"] / "ps"), "-eo", "args="], capture_output=True, text=True, check=True).stdout
			if not any("claude_anthropic_relay.py broker" in line and str(sandbox["tmp"]) in line for line in broker_processes.splitlines()):
				break
			time.sleep(0.1)
		else:
			pytest.fail("orphan reaper did not stop the broker")
	finally:
		# A fake docker run process may still be sleeping after its client is killed.
		try:
			os.killpg(process.pid, signal.SIGKILL)
		except ProcessLookupError:
			pass
		process.wait(timeout=5)
		for manifest in sandbox["runner_temp"].glob("claude-run-*/support-lock.json"):
			subprocess.run(["python3", str(manifest.parent / "claude_engine.py"), "support-unlock", "--manifest", str(manifest)], check=True)


@pytest.mark.parametrize("role, read_only", [
	("SECURITY_AUDIT", False), ("RB_JUDGE", True), ("IMPLEMENT", False),
])
def test_read_profile_strips_credentials_from_claude_only(sandbox: dict, role: str, read_only: bool) -> None:
	_accounts(sandbox, A="TOK_OK")
	credential_names = (
		"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_PAT", "GH_HOST",
		"TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "TG_CHAT_ID", "OPENROUTER_API_KEY",
		"ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_RUNTIME_TOKEN",
	)
	inherited_gh_config = sandbox["tmp"] / "existing-gh-config"
	inherited_gh_config.mkdir()
	(inherited_gh_config / "hosts.yml").write_text("github.com: inherited-login\n", encoding="utf-8")
	result = _claude_run(sandbox, role, **dict.fromkeys(credential_names, "not-a-real-credential"),
		AI_ENGINE_READ_ONLY="true" if read_only else "false", GH_CONFIG_DIR=str(inherited_gh_config))
	assert _rc(result) == 0, result.stderr
	if role == "IMPLEMENT":
		call = _calls(sandbox)[0]
		assert call["credentials"] == dict.fromkeys(credential_names, False)
		assert call["token"] == "TOK_OK"
		assert call["gh_config_dir"] == ""
	else:
		assert _calls(sandbox) == []
		calls = _docker_calls(sandbox)
		assert calls and all(not any(name in call["env"] for name in credential_names) for call in calls)
		assert all(str(sandbox["pool"]) not in " ".join(call["argv"]) for call in calls)


@pytest.mark.parametrize("value, tools, mode", [
	("true", "Read,Grep,Glob,Bash", "dontAsk"),
	("false", "Read,Grep,Glob,Bash,Edit,Write", "bypassPermissions"),
	("yes", "Read,Grep,Glob,Bash,Edit,Write", "bypassPermissions"),
])
def test_read_only_switch_narrows_a_write_role(sandbox: dict, value: str, tools: str, mode: str) -> None:
	_accounts(sandbox, A="TOK_OK")
	result = _claude_run(sandbox, "RB_JUDGE", AI_ENGINE_READ_ONLY=value)
	assert _rc(result) == 0, result.stderr
	if value == "true":
		assert _calls(sandbox) == []
		argv = _docker_calls(sandbox)[0]["argv"]
		assert f"--tools {tools}" in argv[-2] and f"--permission-mode {mode}" in argv[-2]
	else:
		argv = _calls(sandbox)[0]["argv"]
		assert (argv[argv.index("--tools") + 1], argv[argv.index("--permission-mode") + 1]) == (tools, mode)


def _claude_run_selected(sandbox: dict, role: str, **extra_env: str) -> subprocess.CompletedProcess:
	out = sandbox["tmp"] / "out.txt"
	args = " ".join(shlex.quote(part) for part in (role, str(sandbox["prompt"]), str(out), str(sandbox["work"])))
	return _bash(sandbox, f'rc=0; claude_run_selected {args} || rc=$?; echo "RC=${{rc}}"', **extra_env)


def test_claude_run_selected_runs_only_a_role_resolved_to_claude(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	result = _claude_run_selected(sandbox, "VALIDATE", AI_ENGINE_RESOLVED_VALIDATE="claude")
	assert _rc(result) == 0, result.stderr
	assert len(_calls(sandbox)) == 1


@pytest.mark.parametrize("env", [{}, {"AI_ENGINE_RESOLVED_VALIDATE": "codex"}, {"AI_ENGINE_RESOLVED_VALIDATE": "Claude"}, {"AI_ENGINE_RESOLVED_PLAN": "claude"}])
def test_claude_run_selected_returns_75_without_running_otherwise(sandbox: dict, env: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	result = _claude_run_selected(sandbox, "VALIDATE", **env)
	assert _rc(result) == 75
	assert _calls(sandbox) == []
	assert "AI_ENGINE_FALLBACK" not in result.stderr


def test_claude_run_selected_rejects_an_invalid_role(sandbox: dict) -> None:
	result = _claude_run_selected(sandbox, "bad role", **{"AI_ENGINE_RESOLVED_VALIDATE": "claude"})
	assert _rc(result) == 75
	assert _calls(sandbox) == []


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
	assert "WebFetch" not in module.PROFILE_TOOLS["write"]
	assert "WebSearch" not in module.PROFILE_TOOLS["write"]


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
	run = _docker_calls(sandbox)[-1]
	assert f"coding-workflows.codex-isolated.root={root}" not in run["argv"]


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
