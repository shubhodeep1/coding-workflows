#!/usr/bin/env python3
"""Behaviour tests for scripts/codex_isolated_exec.sh.

Docker is replaced by tests/codex_isolation_fakes.py's recording `docker`,
which runs a fake `codex` inside the mounted copy. Everything else is real:
the snapshot, the synthetic .git, the host-side model broker and the
workspace transfer. The assertions are the isolation contract: neither
Docker nor the agent ever receives GH_TOKEN / GITHUB_TOKEN / GH_PAT, the
OpenRouter key or the Telegram secret; the container has no network, a
read-only root and no capabilities; the host checkout and its .git are never
mounted; and Codex never falls back to running on the host.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_isolation_fakes import (  # noqa: E402
	assert_no_secret_env,
	copy_isolation_support,
	docker_runs,
	install_fake_docker,
	read_docker_log,
	short_temp_dir,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "ghp_isolationtesttoken000000000000000000"
OPENROUTER_KEY = "sk-or-isolation-test-key"

FAKE_CODEX = r'''#!__PYTHON__
import json, os, sys
prompt = sys.stdin.read()
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ), "prompt": prompt,
	"files": sorted(os.listdir("."))}
gitcfg = os.path.join(".git", "config")
record["gitconfig"] = open(gitcfg).read() if os.path.exists(gitcfg) else ""
with open(__LOG__, "a") as handle:
	handle.write(json.dumps(record) + "\n")
action = os.environ.get("CODEX_ISOLATED_WORKDIR") and __ACTION__
if action == "edit":
	open("src/app.py", "w").write("print('edited by agent')\n")
	open("new_file.py", "w").write("y = 2\n")
elif action == "symlink":
	os.symlink("/etc/passwd", "evil")
print("FAKE_CODEX_OUTPUT")
sys.exit(int(__EXIT__))
'''


def git(repo, *args):
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
	return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def sandbox(tmp_path):
	runner_temp = short_temp_dir()
	scripts = tmp_path / "support"
	copy_isolation_support(scripts)
	repo = runner_temp / "repo"
	repo.mkdir()
	git(repo, "init", "-q")
	git(repo, "remote", "add", "origin", f"https://x-access-token:{TOKEN}@github.com/o/r")
	(repo / "src").mkdir()
	(repo / "src" / "app.py").write_text("print('app')\n")
	git(repo, "add", "-A")
	git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
	bin_dir = tmp_path / "bin"
	docker_log = tmp_path / "docker.jsonl"
	codex_log = tmp_path / "codex.jsonl"
	install_fake_docker(bin_dir, docker_log)
	codex_home = tmp_path / "hostcodex"
	codex_home.mkdir()
	(codex_home / "config.toml").write_text('web_search = "disabled"\nmodel_reasoning_effort = "low"\n')
	yield {
		"runner_temp": runner_temp,
		"scripts": scripts,
		"repo": repo,
		"bin": bin_dir,
		"docker_log": docker_log,
		"codex_log": codex_log,
		"codex_home": codex_home,
	}
	shutil.rmtree(runner_temp, ignore_errors=True)


def write_fake_codex(sandbox, action="none", exit_code=0):
	script = (
		FAKE_CODEX.replace("__PYTHON__", sys.executable)
		.replace("__LOG__", repr(str(sandbox["codex_log"])))
		.replace("__ACTION__", repr(action))
		.replace("__EXIT__", repr(str(exit_code)))
	)
	target = sandbox["bin"] / "codex"
	target.write_text(script)
	target.chmod(0o755)


def run_helper(sandbox, *helper_args, env_extra=None, path=None, prompt="Prompt with untrusted issue text"):
	env = {
		"PATH": path if path is not None else f"{sandbox['bin']}:{os.environ['PATH']}",
		"HOME": str(sandbox["runner_temp"]),
		"RUNNER_TEMP": str(sandbox["runner_temp"]),
		"CODEX_HOME": str(sandbox["codex_home"]),
		"GH_TOKEN": TOKEN,
		"GITHUB_TOKEN": TOKEN,
		"GH_PAT": TOKEN,
		"OPENROUTER_API_KEY": OPENROUTER_KEY,
		"TG_BOT_SECRET": "tg-secret",
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	env.update(env_extra or {})
	return subprocess.run(
		["bash", str(sandbox["scripts"] / "codex_isolated_exec.sh"), *helper_args],
		cwd=sandbox["repo"],
		input=prompt,
		capture_output=True,
		text=True,
		env=env,
		timeout=120,
	)


CODEX_ARGS = ["--", "--ask-for-approval", "never", "exec", "--skip-git-repo-check", "--model", "openai/gpt-5.4", "--sandbox", "danger-full-access"]


def codex_records(sandbox):
	path = sandbox["codex_log"]
	return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def mounts_of(run_entry):
	argv = run_entry["argv"]
	return [argv[i + 1] for i, value in enumerate(argv) if value == "--mount"]


def test_read_only_run_has_no_secrets_no_network_and_no_host_checkout(sandbox):
	write_fake_codex(sandbox)
	proc = run_helper(sandbox, "run", "--mode", "read-only", *CODEX_ARGS)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.strip() == "FAKE_CODEX_OUTPUT"

	for entry in read_docker_log(sandbox["docker_log"]):
		# LC_CTYPE is added by the fake docker's own Python (PEP 538).
		assert set(entry["env"]) <= {"PATH", "HOME", "LC_CTYPE"}, entry["env"].keys()
	run_entry = docker_runs(sandbox["docker_log"])[0]
	argv = run_entry["argv"]
	for flag in ("--init", "--read-only", "-i"):
		assert flag in argv
	assert argv[argv.index("--network") + 1] == "none"
	assert argv[argv.index("--cap-drop") + 1] == "ALL"
	assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
	env_flags = [argv[i + 1] for i, value in enumerate(argv) if value == "--env"]
	assert "CODEX_ISOLATED_PROXY_KEY=isolated-placeholder" in env_flags
	assert not any(OPENROUTER_KEY in flag or TOKEN in flag for flag in env_flags)
	repo = str(sandbox["repo"])
	work_mounts = [m for m in mounts_of(run_entry) if f"dst={repo}" in m]
	assert len(work_mounts) == 1 and work_mounts[0].endswith(",readonly")
	assert f"src={repo}," not in work_mounts[0], "the host checkout itself must never be mounted"
	assert not any("/.git" in m or "docker.sock" in m for m in mounts_of(run_entry))

	record = codex_records(sandbox)[0]
	assert_no_secret_env(record["env"])
	assert record["cwd"] != repo
	assert TOKEN not in record["gitconfig"]
	assert record["prompt"] == "Prompt with untrusted issue text"
	assert record["argv"][-4:] == ["--model", "openai/gpt-5.4", "--sandbox", "danger-full-access"]
	assert record["env"]["CODEX_ISOLATED_REASONING"] == "low"
	assert record["env"]["CODEX_ISOLATED_WEB_SEARCH"] == "disabled"


def test_codex_exit_status_passes_through(sandbox):
	write_fake_codex(sandbox, exit_code=3)
	proc = run_helper(sandbox, "run", "--mode", "read-only", *CODEX_ARGS)
	assert proc.returncode == 3


def test_model_equals_form_is_forwarded_to_isolated_codex(sandbox):
	write_fake_codex(sandbox)
	proc = run_helper(sandbox, "run", "--mode", "read-only", "--", "exec", "--model=openai/gpt-5.4")
	assert proc.returncode == 0, proc.stderr
	assert codex_records(sandbox)[0]["argv"][-1] == "--model=openai/gpt-5.4"


def test_workspace_run_copies_agent_edits_back(sandbox):
	write_fake_codex(sandbox, action="edit")
	proc = run_helper(sandbox, "run", "--mode", "workspace", *CODEX_ARGS)
	assert proc.returncode == 0, proc.stderr
	repo = sandbox["repo"]
	assert (repo / "src" / "app.py").read_text() == "print('edited by agent')\n"
	assert (repo / "new_file.py").read_text() == "y = 2\n"
	assert TOKEN in (repo / ".git" / "config").read_text()
	run_entry = docker_runs(sandbox["docker_log"])[0]
	work_mount = [m for m in mounts_of(run_entry) if f"dst={repo}" in m][0]
	assert not work_mount.endswith(",readonly")


def test_workspace_symlink_result_is_refused(sandbox):
	write_fake_codex(sandbox, action="symlink")
	proc = run_helper(sandbox, "run", "--mode", "workspace", *CODEX_ARGS)
	assert proc.returncode == 1
	assert "workspace transfer rejected" in proc.stderr
	assert not (sandbox["repo"] / "evil").is_symlink()


def test_missing_docker_fails_closed_and_never_runs_host_codex(sandbox, tmp_path):
	write_fake_codex(sandbox)
	(sandbox["bin"] / "docker").unlink()
	# PATH holds the fake codex (standing in for the host binary) and every
	# system tool except Docker.
	no_docker = tmp_path / "no-docker-bin"
	no_docker.mkdir()
	for system_dir in ("/usr/local/bin", "/usr/bin", "/bin"):
		if not Path(system_dir).is_dir():
			continue
		for entry in Path(system_dir).iterdir():
			target = no_docker / entry.name
			if entry.name != "docker" and not target.exists():
				target.symlink_to(entry)
	path = f"{sandbox['bin']}:{no_docker}"
	proc = run_helper(sandbox, "run", "--mode", "read-only", *CODEX_ARGS, path=path)
	assert proc.returncode == 1
	assert "::error::CODEX_ISOLATION" in proc.stderr
	assert codex_records(sandbox) == []


def test_missing_openrouter_key_fails_closed(sandbox):
	write_fake_codex(sandbox)
	proc = run_helper(sandbox, "run", "--mode", "read-only", *CODEX_ARGS, env_extra={"OPENROUTER_API_KEY": ""})
	assert proc.returncode == 1
	assert codex_records(sandbox) == []


def test_comma_in_workdir_is_rejected_before_docker_mount(sandbox):
	unsafe = sandbox["repo"] / "comma,path"
	unsafe.mkdir()
	proc = run_helper(sandbox, "run", "--mode", "read-only", "--workdir", str(unsafe), *CODEX_ARGS)
	assert proc.returncode == 1 and "workdir path rejected" in proc.stderr
	assert not docker_runs(sandbox["docker_log"])


def test_comma_in_runner_temp_is_rejected_before_docker_mount(sandbox):
	unsafe = sandbox["runner_temp"] / "comma,path"
	unsafe.mkdir()
	proc = run_helper(sandbox, "run", "--mode", "read-only", *CODEX_ARGS, env_extra={"RUNNER_TEMP": str(unsafe)})
	assert proc.returncode == 1 and "sandbox parent path rejected" in proc.stderr
	assert not docker_runs(sandbox["docker_log"])


def test_include_is_mounted_read_only_at_its_own_path(sandbox):
	write_fake_codex(sandbox)
	runtime = sandbox["runner_temp"] / "runtime"
	runtime.mkdir()
	(runtime / "context.txt").write_text("context\n")
	proc = run_helper(sandbox, "run", "--mode", "read-only", "--include", str(runtime / "context.txt"), *CODEX_ARGS)
	assert proc.returncode == 0, proc.stderr
	run_entry = docker_runs(sandbox["docker_log"])[0]
	include_mounts = [m for m in mounts_of(run_entry) if f"dst={runtime}/context.txt" in m]
	assert len(include_mounts) == 1 and include_mounts[0].endswith(",readonly")


def test_persistent_root_is_reused_and_cleaned_up(sandbox):
	write_fake_codex(sandbox, action="edit")
	prep = run_helper(sandbox, "prepare", "--workdir", str(sandbox["repo"]))
	assert prep.returncode == 0, prep.stderr
	root = prep.stdout.strip()
	assert Path(root).name.startswith("codex-isolated-")
	proc = run_helper(sandbox, "run", "--mode", "workspace", "--root", root, *CODEX_ARGS)
	assert proc.returncode == 0, proc.stderr
	assert Path(root).is_dir(), "a persistent root survives the run"
	assert run_helper(sandbox, "cleanup", "--root", root).returncode == 0
	assert not Path(root).exists()


def test_prepare_deps_networked_container_sees_no_source(sandbox):
	(sandbox["repo"] / "package.json").write_text('{"name":"test"}\n')
	(sandbox["repo"] / "package-lock.json").write_text("{}\n")
	(sandbox["repo"] / ".yarnrc.yml").write_text("yarnPath: ./evil.cjs\n")
	prep = run_helper(sandbox, "prepare", "--deps")
	assert prep.returncode == 0, prep.stderr
	root = Path(prep.stdout.strip())
	try:
		(runner,) = docker_runs(sandbox["docker_log"])
		argv = runner["argv"]
		assert argv[argv.index("--network") + 1] == "none"
		assert argv[argv.index("--workdir") + 1] == "/codex-deps"
		mounts = mounts_of(runner)
		assert len(mounts) == 4
		assert any(f"src={root}/deps-stage,dst=/codex-deps" in m for m in mounts)
		assert any(m.endswith("dst=/socket") for m in mounts)
		assert any(m.endswith("dst=/support/dependency_registry_proxy.py,readonly") for m in mounts)
		assert not any(f"src={root}/work" in m or f"dst={sandbox['repo']}" in m for m in mounts)
		assert "src/app.py" not in json.dumps(runner)
		assert not (root / "deps-stage").exists()
	finally:
		assert run_helper(sandbox, "cleanup", "--root", str(root)).returncode == 0


@pytest.mark.parametrize("with_node,with_requirements", [(False, False), (True, False), (False, True), (True, True)])
def test_prepare_deps_pyproject_source_install_is_offline(sandbox, with_node, with_requirements):
	(sandbox["repo"] / "pyproject.toml").write_text('[project]\nname = "sample"\ndependencies = ["requests"]\n')
	if with_node:
		(sandbox["repo"] / "package.json").write_text('{}\n')
	if with_requirements:
		(sandbox["repo"] / "requirements.txt").write_text('pytest==8\n')
	prep = run_helper(sandbox, "prepare", "--deps")
	assert prep.returncode == 0, prep.stderr
	root = Path(prep.stdout.strip())
	try:
		networked, offline = docker_runs(sandbox["docker_log"])
		assert networked["argv"][networked["argv"].index("--network") + 1] == "none"
		assert "--network" in offline["argv"]
		assert offline["argv"][offline["argv"].index("--network") + 1] == "none"
		assert "--read-only" in offline["argv"] and "--tmpfs" in offline["argv"]
		assert any(f"src={root}/work,dst={sandbox['repo']}" in m for m in mounts_of(offline))
		assert not any(f"src={root}/work" in m for m in mounts_of(networked))
	finally:
		assert run_helper(sandbox, "cleanup", "--root", str(root)).returncode == 0


def test_prepare_deps_node_modules_remain_in_prep_root(sandbox):
	(sandbox["repo"] / "package.json").write_text("{}\n")
	install_fake_docker(sandbox["bin"], sandbox["docker_log"], {"FAKE_DEPS_CREATE_NODE_MODULES": "1"})
	prep = run_helper(sandbox, "prepare", "--deps")
	assert prep.returncode == 0, prep.stderr
	root = Path(prep.stdout.strip())
	try:
		assert (root / "work" / "node_modules" / "marker").read_text() == "installed\n"
		assert not (sandbox["repo"] / "node_modules").exists()
		assert "node_modules" in json.loads((root / "manifest.json").read_text())["prep_roots"]
		write_fake_codex(sandbox, action="edit")
		result = run_helper(sandbox, "run", "--mode", "workspace", "--root", str(root), *CODEX_ARGS)
		assert result.returncode == 0, result.stderr
		assert not (sandbox["repo"] / "node_modules").exists()
		assert (sandbox["repo"] / "new_file.py").exists()
	finally:
		assert run_helper(sandbox, "cleanup", "--root", str(root)).returncode == 0


def test_prepare_deps_failure_remains_unverified_warning(sandbox):
	install_fake_docker(sandbox["bin"], sandbox["docker_log"], {"FAKE_DEPS_FAIL": "1"})
	prep = run_helper(sandbox, "prepare", "--deps")
	assert prep.returncode == 0, prep.stderr
	assert "agent runs without preinstalled dependencies" in prep.stderr
	root = Path(prep.stdout.strip())
	assert (root / "manifest.json").exists()
	assert run_helper(sandbox, "cleanup", "--root", str(root)).returncode == 0


@pytest.mark.parametrize("mode", ["both", "pyproject"])
def test_dependency_fallback_reports_missing_dev_dependencies(tmp_path, mode):
	meta = tmp_path / ".codex-deps"
	meta.mkdir()
	(meta / "python").write_text(mode + "\n")
	shell_source = (REPO_ROOT / "scripts" / "codex_isolated_exec.sh").read_text()
	case_start = shell_source.index('case "$(cat .codex-deps/python)" in')
	case_end = shell_source.index('if [ -f .codex-deps/want-pytest ]', case_start)
	command = ('pip() { case "$*" in *dev.txt*) return 1 ;; *) return 0 ;; esac; }\n'
		'install_failed=false\n' + shell_source[case_start:case_end] + '\nprintf "%s\\n" "$install_failed"\n')
	proc = subprocess.run(["bash", "-c", command], cwd=tmp_path, capture_output=True, text=True, check=True)
	assert proc.stdout.strip() == "true"


@pytest.mark.parametrize("installable", [False, True])
def test_dependency_prepare_has_only_allowlisted_proxy_egress(sandbox, installable):
	(sandbox["repo"] / "requirements.txt").write_text("example==1\n")
	if installable:
		(sandbox["repo"] / "setup.py").write_text("from setuptools import setup\nsetup(name='sample')\n")
	prep = run_helper(sandbox, "prepare", "--workdir", str(sandbox["repo"]), "--deps")
	assert prep.returncode == 0, prep.stderr
	root = Path(prep.stdout.strip())
	runs = docker_runs(sandbox["docker_log"])
	assert len(runs) == (2 if installable else 1)
	argv = runs[0]["argv"]
	assert argv[argv.index("--network") + 1] == "none"
	assert any(f"src={root}/deps-stage,dst=/codex-deps" in mount for mount in mounts_of(runs[0]))
	assert not any(f"src={root}/work" in mount for mount in mounts_of(runs[0]))
	if installable:
		assert any(f"src={root}/work" in mount for mount in mounts_of(runs[1]))
		assert runs[1]["argv"][runs[1]["argv"].index("--network") + 1] == "none"
	assert any(mount.endswith("dst=/socket") for mount in mounts_of(runs[0]))
	assert any(mount.endswith("dst=/support/dependency_registry_proxy.py,readonly") for mount in mounts_of(runs[0]))
	assert "HTTPS_PROXY=http://127.0.0.1:3128" in argv
	assert "proxy=on" in prep.stderr
	assert not (root / "socket" / "registry.sock").exists()
	assert run_helper(sandbox, "cleanup", "--root", str(root)).returncode == 0


def test_missing_proxy_skips_dependency_container(sandbox):
	(sandbox["scripts"] / "dependency_registry_proxy.py").unlink()
	prep = run_helper(sandbox, "prepare", "--workdir", str(sandbox["repo"]), "--deps")
	assert prep.returncode == 0, prep.stderr
	assert "deps_skipped reason=proxy_support_missing" in prep.stderr
	assert "proxy=skipped" in prep.stderr
	assert not docker_runs(sandbox["docker_log"])
	assert run_helper(sandbox, "cleanup", "--root", prep.stdout.strip()).returncode == 0


def test_foreign_root_is_rejected(sandbox, tmp_path):
	write_fake_codex(sandbox)
	foreign = sandbox["runner_temp"] / "not-ours"
	foreign.mkdir()
	proc = run_helper(sandbox, "run", "--mode", "workspace", "--root", str(foreign), *CODEX_ARGS)
	assert proc.returncode == 1 and "sandbox root rejected" in proc.stderr
	proc = run_helper(sandbox, "cleanup", "--root", str(foreign))
	assert proc.returncode == 1 and foreign.exists()


def test_thread_reuse_direct_run_launches_through_the_helper(sandbox, tmp_path):
	write_fake_codex(sandbox)
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("Prompt\n")
	output = tmp_path / "out.txt"
	env = {
		"PATH": f"{sandbox['bin']}:{os.environ['PATH']}",
		"HOME": str(sandbox["runner_temp"]),
		"RUNNER_TEMP": str(sandbox["runner_temp"]),
		"CODEX_HOME": str(sandbox["codex_home"]),
		"GH_TOKEN": TOKEN,
		"OPENROUTER_API_KEY": OPENROUTER_KEY,
		"RUNTIME_DIR": str(tmp_path / "runtime"),
		"CODEX_ISOLATED_EXEC": str(sandbox["scripts"] / "codex_isolated_exec.sh"),
		"CODEX_ISOLATED_MODE": "read-only",
		"CODEX_THREAD_REUSE_STATE_KEY": "implement",
		"CODEX_THREAD_REUSE_PROMPT_FILE": str(prompt),
		"CODEX_THREAD_REUSE_OUTPUT_FILE": str(output),
		"CODEX_THREAD_REUSE_PHASE": "implement",
		"CODEX_THREAD_REUSE_MODEL": "openai/gpt-5.4",
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	proc = subprocess.run(
		["bash", str(REPO_ROOT / "scripts" / "codex_thread_reuse.sh"), "direct-run"],
		cwd=sandbox["repo"], env=env, capture_output=True, text=True, timeout=120,
	)
	assert proc.returncode == 0, proc.stderr
	assert output.read_text().strip() == "FAKE_CODEX_OUTPUT"
	assert docker_runs(sandbox["docker_log"]), "codex_thread_reuse.sh must launch through the helper"
	assert_no_secret_env(codex_records(sandbox)[0]["env"])


# --- the Claude engine (scripts/ai_engine.sh claude_run; CLAUDE.md answer Q16 A) -----

CLAUDE_TOKEN = "sk-ant-oat01-isolationtesttoken"

FAKE_CLAUDE = r'''#!__PYTHON__
import json, os, subprocess, sys
mounts = json.loads(os.environ.get("FAKE_CONTAINER_MOUNTS", "[]"))
support = next((m["src"] for m in mounts if m.get("dst") == "/support"), "")
show = subprocess.run(["git", "show", "HEAD:CLAUDE.md"], capture_output=True, text=True)
record = {"argv": sys.argv[1:], "env": dict(os.environ), "prompt": sys.stdin.read(),
	"files": sorted(os.listdir(".")), "support": sorted(os.listdir(support)) if support else [],
	"git_show_claude_md": show.returncode == 0}
with open(__LOG__, "a") as handle:
	handle.write(json.dumps(record) + "\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}))
'''


@pytest.fixture()
def claude_engine(sandbox, tmp_path):
	(sandbox["repo"] / "CLAUDE.md").write_text("repository instructions\n")
	git(sandbox["repo"], "add", "-A")
	git(sandbox["repo"], "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "claude md")
	script = FAKE_CLAUDE.replace("__PYTHON__", sys.executable).replace("__LOG__", repr(str(tmp_path / "claude.jsonl")))
	(sandbox["bin"] / "claude").write_text(script)
	(sandbox["bin"] / "claude").chmod(0o755)
	files = tmp_path / "claude-files"
	files.mkdir()
	token = sandbox["runner_temp"] / "token-A"
	token.write_text(CLAUDE_TOKEN + "\n")
	token.chmod(0o600)
	for name, text in (("settings.json", '{"permissions": {"deny": []}}'), ("guard.py", "print('guard')\n"), ("instructions.md", "instructions\n")):
		(files / name).write_text(text)
	args = [
		"run", "--engine", "claude", "--mode", "read-only",
		"--claude-token-file", str(token), "--claude-models", "claude-opus-5-5,claude-haiku-4-5",
		"--claude-cli-version", "2.1.289", "--claude-settings", str(files / "settings.json"),
		"--claude-guard-hook", str(files / "guard.py"), "--claude-instructions", str(files / "instructions.md"),
	]
	return {"args": args, "log": tmp_path / "claude.jsonl", "token": token}


CLAUDE_ARGS = ["--", "-p", "--model", "claude-opus-5-5", "--settings", "/support/settings.json"]


def claude_records(engine):
	return [json.loads(line) for line in engine["log"].read_text().splitlines()] if engine["log"].exists() else []


def test_claude_engine_runs_behind_the_relay_without_credentials(sandbox, claude_engine):
	proc = run_helper(sandbox, *claude_engine["args"], "--hide-claude-md", *CLAUDE_ARGS, env_extra={"OPENROUTER_API_KEY": ""})
	assert proc.returncode == 0, proc.stderr
	record = claude_records(claude_engine)[0]
	assert_no_secret_env(record["env"])
	assert record["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "isolated-placeholder"
	assert record["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8765"
	assert record["argv"] == CLAUDE_ARGS[1:]
	assert record["support"] == ["claude_anthropic_relay.py", "guard.py", "instructions.md", "settings.json"]
	# hide_claude_md: absent from the copy and from the synthetic git tree.
	assert "CLAUDE.md" not in record["files"] and "src" in record["files"]
	assert record["git_show_claude_md"] is False
	(run,) = docker_runs(sandbox["docker_log"])
	dumped = json.dumps(run)
	assert CLAUDE_TOKEN not in dumped and str(claude_engine["token"]) not in dumped
	assert TOKEN not in dumped and OPENROUTER_KEY not in dumped
	argv = run["argv"]
	assert argv[argv.index("--network") + 1] == "none"
	assert "--read-only" in argv
	assert all(".git" not in mount for mount in mounts_of(run))
	builds = [entry for entry in read_docker_log(sandbox["docker_log"]) if entry["argv"][:1] == ["build"]]
	assert any("CLAUDE_CLI_VERSION=2.1.289" in entry["argv"] for entry in builds)
	assert CLAUDE_TOKEN not in proc.stderr + proc.stdout


def test_claude_engine_model_must_be_allowed(sandbox, claude_engine):
	proc = run_helper(sandbox, *claude_engine["args"], "--", "-p", "--model", "claude-other-1")
	assert proc.returncode == 1
	assert "--model must be one of --claude-models" in proc.stderr
	assert docker_runs(sandbox["docker_log"]) == []


def test_claude_engine_without_docker_reports_unavailable(sandbox, claude_engine, tmp_path):
	bare = tmp_path / "bare-bin"
	bare.mkdir()
	for tool in ("bash", "python3", "realpath", "dirname", "basename", "mktemp", "env", "cat", "chmod", "mkdir", "rm", "id"):
		found = shutil.which(tool)
		if found:
			(bare / tool).symlink_to(found)
	proc = run_helper(sandbox, *claude_engine["args"], *CLAUDE_ARGS, path=str(bare))
	assert proc.returncode == 75, proc.stderr
	assert "CODEX_ISOLATION unavailable engine=claude reason=docker_missing" in proc.stderr
	assert claude_records(claude_engine) == []


def test_claude_engine_relay_failure_is_its_own_exit(sandbox, claude_engine):
	claude_engine["token"].chmod(0o644)  # claude_anthropic_relay.py refuses it
	proc = run_helper(sandbox, *claude_engine["args"], *CLAUDE_ARGS)
	assert proc.returncode == 73, proc.stderr
	assert "CODEX_ISOLATION relay_unavailable engine=claude" in proc.stderr
	assert claude_records(claude_engine) == []


def test_claude_home_is_mounted_as_the_session_store(sandbox, claude_engine, tmp_path):
	home = sandbox["runner_temp"] / "claude-home"
	proc = run_helper(sandbox, *claude_engine["args"], "--claude-home", str(home), *CLAUDE_ARGS)
	assert proc.returncode == 0, proc.stderr
	(run,) = docker_runs(sandbox["docker_log"])
	assert f"type=bind,src={home},dst=/home/agent/.claude" in mounts_of(run)
	assert oct(home.stat().st_mode & 0o777) == "0o700"


def test_claude_options_are_refused_for_codex_and_prepare(sandbox, claude_engine):
	proc = run_helper(sandbox, "prepare", "--engine", "claude", "--workdir", str(sandbox["repo"]))
	assert proc.returncode == 2
	write_fake_codex(sandbox)
	proc = run_helper(sandbox, "run", "--mode", "read-only", "--claude-home", str(sandbox["runner_temp"] / "h"), *CODEX_ARGS)
	assert proc.returncode == 1 and "--claude-home needs --engine claude" in proc.stderr
