"""Test doubles for scripts/codex_isolated_exec.sh.

`install_fake_docker(bin_dir, log_path)` writes a `docker` executable that
records every call (argv plus the environment the helper gave it) as one
JSON line in `log_path`, answers `build` with an image id, and emulates
`run` by executing the `codex` found on the caller's PATH with the
container's arguments, inside the host directory mounted at the container
workdir, with only the container's --env values plus PATH. That lets a test
use a fake `codex` exactly as before while the real helper still builds the
snapshot, starts the broker and copies workspace changes back.

The helper launches Docker through `env -i PATH=... HOME=...`, so the log
path is baked into the generated script instead of passed in the
environment.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

FAKE_DOCKER = r'''#!__PYTHON__
import json
import os
import subprocess
import sys

LOG = __LOG__
PASSTHROUGH = __PASSTHROUGH__
args = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as handle:
	handle.write(json.dumps({"argv": args, "env": dict(os.environ)}) + "\n")
if not args:
	sys.exit(0)
command = args[0]
if command == "build":
	if PASSTHROUGH.get("FAKE_DOCKER_BUILD_FAIL"):
		print("fake build failure", file=sys.stderr)
		sys.exit(1)
	print("sha256:" + "0" * 64)
	sys.exit(0)
if command in ("ps", "rm", "kill"):
	sys.exit(0)
if command != "run":
	sys.exit(0)
mounts = []
container_env = {}
workdir = None
container_name = ""
index = 1
while index < len(args):
	arg = args[index]
	if arg in ("--mount", "--env", "--workdir", "--name", "--label", "--user", "--network", "--cap-drop",
			"--security-opt", "--pids-limit", "--memory", "--cpus", "--tmpfs"):
		value = args[index + 1]
		if arg == "--mount":
			fields = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
			mounts.append(fields)
		elif arg == "--env":
			key, _, val = value.partition("=")
			container_env[key] = val
		elif arg == "--workdir":
			workdir = value
		elif arg == "--name":
			container_name = value
		index += 2
		continue
	if arg.startswith("-"):
		index += 1
		continue
	break
image = args[index]
inner = args[index + 1:]
# inner = ["/bin/bash", "-c", SCRIPT, NAME, *agent_args]; NAME picks the CLI.
agent = "claude" if inner[3:4] == ["claude-isolated"] else "codex"
codex_args = inner[4:]
host_cwd = None
socket_dir = None
for mount in mounts:
	if mount.get("dst") == workdir:
		host_cwd = mount.get("src")
	if mount.get("dst") == "/socket":
		socket_dir = mount.get("src")
if container_name.startswith("codex-isolated-deps"):
	if PASSTHROUGH.get("FAKE_DEPS_CREATE_NODE_MODULES") and workdir == "/codex-deps" and host_cwd:
		os.makedirs(os.path.join(host_cwd, "node_modules"), exist_ok=True)
		with open(os.path.join(host_cwd, "node_modules", "marker"), "w") as handle:
			handle.write("installed\n")
	sys.exit(1 if PASSTHROUGH.get("FAKE_DEPS_FAIL") else 0)
env = dict(container_env)
env.update(PASSTHROUGH)
env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
env["FAKE_CONTAINER_MOUNTS"] = json.dumps(mounts)
if agent == "claude" and socket_dir:
	# Test-only: the real token never enters the container. A fake CLI that
	# behaves per account learns it from the host relay serving this socket.
	sock = os.path.join(socket_dir, "provider.sock")
	for pid in os.listdir("/proc"):
		try:
			with open(f"/proc/{pid}/cmdline", "rb") as handle:
				argv = handle.read().split(b"\0")
		except OSError:
			continue
		names = [part.decode("utf-8", "replace") for part in argv]
		if "broker" in names and any(n.endswith("claude_anthropic_relay.py") for n in names) and sock in names:
			token_path = names[names.index(sock) + 1]
			with open(token_path, encoding="utf-8") as handle:
				env["FAKE_RELAY_TOKEN"] = handle.read().strip()
			break
proc = subprocess.run([PASSTHROUGH.get("FAKE_CLAUDE_BIN", agent) if agent == "claude" else agent, *codex_args], cwd=host_cwd, env=env)
sys.exit(proc.returncode)
'''


def short_temp_dir() -> Path:
	"""A short RUNNER_TEMP: the broker's Unix socket path must stay under 108 bytes."""
	return Path(tempfile.mkdtemp(prefix="cie-", dir="/tmp"))


_SHARED_SHORT_TEMP: list = []


def _shared_short_temp_dir() -> Path:
	"""One short RUNNER_TEMP per test process (sandbox roots are removed per run)."""
	if not _SHARED_SHORT_TEMP or not _SHARED_SHORT_TEMP[0].is_dir():
		_SHARED_SHORT_TEMP[:] = [short_temp_dir()]
	return _SHARED_SHORT_TEMP[0]


def install_fake_docker(bin_dir: Path, log_path: Path, passthrough_env: dict | None = None) -> Path:
	"""Write the recording fake. `passthrough_env` (test-only values such as a
	mock's MOCK_* settings) is baked in and added to the fake container env,
	because the helper starts Docker with an empty environment."""
	bin_dir.mkdir(parents=True, exist_ok=True)
	script = (
		FAKE_DOCKER.replace("__PYTHON__", sys.executable)
		.replace("__LOG__", repr(str(log_path)))
		.replace("__PASSTHROUGH__", repr(dict(passthrough_env or {})))
	)
	target = bin_dir / "docker"
	target.write_text(script, encoding="utf-8")
	target.chmod(0o755)
	return target


def read_docker_log(log_path: Path) -> list[dict]:
	import json

	if not log_path.exists():
		return []
	return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def docker_runs(log_path: Path) -> list[dict]:
	return [entry for entry in read_docker_log(log_path) if entry["argv"][:1] == ["run"]]


def isolation_support_files(scripts_dir: Path) -> list[str]:
	"""Files scripts/codex_isolated_exec.sh needs next to itself."""
	return [
		"codex_isolated_exec.sh",
		"codex_isolated_workspace.py",
		"clarify_openrouter_broker.py",
		"dependency_registry_proxy.py",
		"write_codex_config.sh",
		"codex_model_catalog.json",
		"claude_anthropic_relay.py",
	]


def copy_isolation_support(dest_scripts_dir: Path) -> None:
	"""Copy the helper and its support files into a test's scripts directory."""
	import shutil

	source = Path(__file__).resolve().parent.parent / "scripts"
	dest_scripts_dir.mkdir(parents=True, exist_ok=True)
	if dest_scripts_dir.resolve() == source.resolve():
		return  # Running the repository's own scripts: nothing to copy.
	for name in isolation_support_files(source):
		shutil.copy2(source / name, dest_scripts_dir / name)


def enable_fake_isolation(bin_dir: Path, scripts_dir: Path, env: dict, passthrough_prefixes=("MOCK_", "GH_MOCK_", "REAL_"), short_temp: bool = True) -> dict:
	"""Make a harness that stubs `codex` work through the real helper.

	Copies the helper's support files next to the scripts under test,
	installs the recording fake `docker` in `bin_dir` (which must be on the
	harness PATH), passes the harness's MOCK_*, GH_MOCK_* and REAL_* settings
	through to the fake container (mock `python3` / `git` wrappers find the
	real binaries through REAL_*; without them they resolve themselves on
	PATH and recurse), and points RUNNER_TEMP at a short directory (the broker's
	socket path must stay under 108 bytes). Returns the updated env.
	"""
	copy_isolation_support(scripts_dir)
	passthrough = {key: value for key, value in env.items() if key.startswith(tuple(passthrough_prefixes))}
	install_fake_docker(bin_dir, bin_dir.parent / "fake-docker.jsonl", passthrough)
	updated = dict(env)
	if short_temp:
		updated["RUNNER_TEMP"] = str(_shared_short_temp_dir())
	updated.setdefault("OPENROUTER_API_KEY", "test-openrouter-key")
	if not updated.get("OPENROUTER_API_KEY"):
		updated["OPENROUTER_API_KEY"] = "test-openrouter-key"
	return updated


SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "TG_BOT_SECRET")


def assert_no_secret_env(env: dict) -> None:
	leaked = sorted(key for key in SECRET_ENV if key in env)
	assert not leaked, f"secret environment reached the isolated agent: {leaked}"
	for value in env.values():
		assert "ghp_" not in value and "sk-or-" not in value, "a token value reached the isolated agent"


__all__ = [
	"assert_no_secret_env",
	"copy_isolation_support",
	"docker_runs",
	"enable_fake_isolation",
	"install_fake_docker",
	"isolation_support_files",
	"read_docker_log",
	"short_temp_dir",
]
