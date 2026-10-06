#!/usr/bin/env python3
"""scripts/ai_engine.sh: engine selection, D1 fallback and claude_run.

claude_run is exercised through a stub isolation helper and fake `claude`
executable. The real CLI and Docker are never called. Also covers the
`--engine` log label of the codex wrappers.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_ENGINE = REPO_ROOT / "scripts" / "ai_engine.sh"
STALL_GUARD = REPO_ROOT / "scripts" / "codex_stall_guard.sh"
HEARTBEAT = REPO_ROOT / "scripts" / "codex_heartbeat.sh"
INSTRUCTIONS = REPO_ROOT / "unattended_system_instructions.md"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_CLAUDE_LOG"]
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
account = os.environ.get("FAKE_UPSTREAM_ACCOUNT", "")
record = {
	"argv": sys.argv[1:],
	"stdin": sys.stdin.read(),
	"token": token,
	"environment": dict(os.environ),
	"cwd": os.getcwd(),
	"claude_md_visible": os.path.exists("CLAUDE.md"),
	"api_key_env": "ANTHROPIC_API_KEY" in os.environ,
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
emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.01, "usage": {"input_tokens": 1, "output_tokens": 2}})
'''

STUB_ISOLATION = r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" >> "${FAKE_HELPER_LOG}"
[ "${FAKE_HELPER_UNAVAILABLE:-}" != true ] || exit 75
token_file=""
workdir=""
hide=false
while [ "$#" -gt 0 ]; do
	case "$1" in
		--claude-token-file) token_file="$2"; shift 2 ;;
		--workdir) workdir="$2"; shift 2 ;;
		--hide-claude-md) hide=true; shift ;;
		--) shift; break ;;
		*) shift ;;
	esac
done
[ -f "${token_file}" ] && [ ! -L "${token_file}" ] && [ "$(stat -c %a "${token_file}")" = 600 ] || exit 73
case "$(< "${token_file}")" in
	TOK_LIMIT*) account=limit ;;
	TOK_AUTH*) account=auth ;;
	TOK_CRASH*) account=crash ;;
	TOK_WRITE_MD*) account=write_md ;;
	*) account=ok ;;
esac
# Simulate the host broker: only the category crosses into the fake model.
# The real helper passes neither the category nor the credential to Docker.
cd "${workdir}"
if [ "${hide}" = true ] && [ -f CLAUDE.md ]; then
	mv CLAUDE.md "${FAKE_HELPER_BACKUP}"
	trap 'if [ -e CLAUDE.md ]; then mv "${FAKE_HELPER_BACKUP}" CLAUDE.md.original.stub; else mv "${FAKE_HELPER_BACKUP}" CLAUDE.md; fi' EXIT
fi
env -i PATH="${PATH}" HOME="${HOME}" FAKE_CLAUDE_LOG="${FAKE_CLAUDE_LOG}" \
	FAKE_UPSTREAM_ACCOUNT="${account}" CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder \
	ANTHROPIC_BASE_URL=http://127.0.0.1:8765 python3 "${FAKE_CLAUDE_BIN}" "${@:2}"
'''


@pytest.fixture()
def sandbox(tmp_path: Path):
	fake_bin = tmp_path / "bin"
	fake_bin.mkdir()
	fake = fake_bin / "claude"
	fake.write_text(FAKE_CLAUDE, encoding="utf-8")
	fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
	support_scripts = tmp_path / "scripts"
	support_scripts.mkdir()
	for name in ("ai_engine.sh", "claude_engine.py", "claude_settings.json.tmpl"):
		shutil.copyfile(REPO_ROOT / "scripts" / name, support_scripts / name)
	shutil.copyfile(INSTRUCTIONS, tmp_path / "unattended_system_instructions.md")
	(support_scripts / "codex_isolated_exec.sh").write_text(STUB_ISOLATION, encoding="utf-8")
	hook = tmp_path / ".claude" / "hooks"
	hook.mkdir(parents=True)
	shutil.copyfile(REPO_ROOT / ".claude" / "hooks" / "gh_api_write_guard.py", hook / "gh_api_write_guard.py")
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
			"FAKE_CLAUDE_BIN": str(fake),
			"FAKE_HELPER_LOG": str(tmp_path / "helper-args"),
			"FAKE_HELPER_BACKUP": str(tmp_path / "claude-md-backup"),
			"PYTHONDONTWRITEBYTECODE": "1",
			"CODEX_HEARTBEAT_INTERVAL_SECS": "30",
			"ANTHROPIC_API_KEY": "must-not-reach-the-cli",
			"GH_TOKEN": "must-not-reach-the-cli",
			"OPENROUTER_API_KEY": "must-not-reach-the-cli",
		}
	)
	return {
		"tmp": tmp_path, "env": env, "pool": pool, "work": work, "prompt": prompt,
		"home": home, "bin": fake_bin, "ai_engine": support_scripts / "ai_engine.sh",
	}


def _accounts(sandbox: dict, **tokens: str) -> None:
	(sandbox["pool"] / "order").write_text("".join(f"{name}\n" for name in tokens), encoding="utf-8")
	for name, token in tokens.items():
		path = sandbox["pool"] / "tokens" / name
		path.write_text(f"{token}\n", encoding="utf-8")
		path.chmod(0o600)


def _bash(sandbox: dict, script: str, **extra_env: str) -> subprocess.CompletedProcess:
	env = dict(sandbox["env"], **extra_env)
	return subprocess.run(
		["bash", "-c", f"set -euo pipefail; source {shlex.quote(str(sandbox['ai_engine']))}; {script}"],
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


def test_missing_cli_falls_back(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	(sandbox["bin"] / "claude").unlink()
	env_path = ":".join(part for part in sandbox["env"]["PATH"].split(":") if not (Path(part) / "claude").exists())
	result = _claude_run(sandbox, PATH=env_path)
	# Claude is installed inside the image, not looked up on the runner.
	assert _rc(result) != 75
	assert "reason=cli_missing" not in result.stderr


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
	_accounts(sandbox, A="TOK_OK")
	result = _claude_run(sandbox, FAKE_HELPER_UNAVAILABLE="true")
	assert _rc(result) == 75
	assert "AI_ENGINE_FALLBACK role=PLAN reason=isolation_unavailable" in result.stderr
	assert _calls(sandbox) == []


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
	assert [call["token"] for call in calls] == ["isolated-placeholder"] * 3
	assert [call["environment"]["FAKE_UPSTREAM_ACCOUNT"] for call in calls] == ["limit", "auth", "ok"]
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
		"--setting-sources": "",
		"--tools": "Read,Grep,Glob,Bash,Edit,Write",
		"--permission-mode": "bypassPermissions",
		"--output-format": "stream-json",
	}
	for flag, value in expected_pairs.items():
		assert argv[argv.index(flag) + 1] == value, flag
	for flag in ("--strict-mcp-config", "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections", "--verbose"):
		assert flag in argv
	settings = next((sandbox["tmp"] / "rt").glob("claude-run-*/claude-settings.json"))
	policy = json.loads(settings.read_text(encoding="utf-8"))
	anchor = str(sandbox["work"].resolve()).lstrip("/")
	assert f"Edit(//{anchor}/.github/workflows/**)" in policy["permissions"]["deny"]
	# The real token never enters the CLI, Docker arguments or environment.
	assert "TOK_OK" not in " ".join(argv)
	assert "TOK_OK" not in (sandbox["tmp"] / "helper-args").read_text(encoding="utf-8")
	assert call["token"] == "isolated-placeholder"
	assert call["api_key_env"] is False
	for key in ("ANTHROPIC_API_KEY", "GH_TOKEN", "OPENROUTER_API_KEY"):
		assert key not in call["environment"]
	assert call["stdin"] == "do the thing\n"
	assert call["cwd"] == str(sandbox["work"].resolve())
	assert "TOK_OK" not in result.stderr + result.stdout
	assert "--claude-token-file" in (sandbox["tmp"] / "helper-args").read_text(encoding="utf-8")


def test_read_role_command_line(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	result = _claude_run(sandbox, "SECURITY_AUDIT", AI_ENGINE_MODEL_HINT="claude-sonnet-5-5")
	assert _rc(result) == 0, result.stderr
	argv = _calls(sandbox)[0]["argv"]
	assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,Bash"
	assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
	assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"


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
	policy = json.loads(next((sandbox["tmp"] / "rt").glob("claude-run-*/claude-settings.json")).read_text(encoding="utf-8"))
	assert not any("/.github/workflows/" in rule for rule in policy["permissions"]["deny"])


def test_session_id_starts_then_resumes(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	session = "0123abcd-0000-4000-8000-00000000abcd"
	_claude_run(sandbox, session=session)
	project = sandbox["tmp"] / "rt" / "claude-isolated-home" / "projects" / "p"
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


def test_hide_claude_md_moves_it_aside_and_restores_it(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_OK")
	support = sandbox["tmp"] / "support"
	(support / ".github" / "ai").mkdir(parents=True)
	(support / ".github" / "ai" / "claude_engine.json").write_text('{"hide_claude_md": true}', encoding="utf-8")
	result = _claude_run(sandbox, SUPPORT_ROOT_DIR=str(support))
	assert _rc(result) == 0, result.stderr
	assert _calls(sandbox)[0]["claude_md_visible"] is False
	assert (sandbox["work"] / "CLAUDE.md").read_text(encoding="utf-8") == "checkout CLAUDE.md\n"


def test_hide_claude_md_does_not_overwrite_new_file(sandbox: dict) -> None:
	_accounts(sandbox, A="TOK_WRITE_MD")
	support = sandbox["tmp"] / "support"
	(support / ".github" / "ai").mkdir(parents=True)
	(support / ".github" / "ai" / "claude_engine.json").write_text('{"hide_claude_md": true}', encoding="utf-8")
	result = _claude_run(sandbox, SUPPORT_ROOT_DIR=str(support))
	assert _rc(result) == 0, result.stderr
	assert (sandbox["work"] / "CLAUDE.md").read_text(encoding="utf-8") == "new instructions\n"
	backup_paths = list(sandbox["work"].glob("CLAUDE.md.original.*"))
	assert len(backup_paths) == 1
	assert backup_paths[0].read_text(encoding="utf-8") == "checkout CLAUDE.md\n"
	assert "--hide-claude-md" in (sandbox["tmp"] / "helper-args").read_text(encoding="utf-8")


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
