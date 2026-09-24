#!/usr/bin/env python3
"""Security contracts for untrusted model-process isolation."""

from __future__ import annotations

import importlib.util
import http.client
import json
import os
import runpy
import subprocess
import socket
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "scripts" / "untrusted_process_sandbox.sh"
PROXY = REPO_ROOT / "scripts" / "model_provider_proxy.py"
PACKAGE_PROXY = REPO_ROOT / "scripts" / "package_download_proxy.py"
TRUSTED_GIT_WRITE = REPO_ROOT / "scripts" / "trusted_git_write.sh"
CAUSALITY = REPO_ROOT / "scripts" / "security_audit_causality.py"
WORKSPACE_GUARD = REPO_ROOT / "scripts" / "post_agent_workspace_guard.py"
RESOLVER_GUARD = REPO_ROOT / "scripts" / "check_resolver_diff.sh"
OPENCODE_HELPERS = REPO_ROOT / "scripts" / "opencode_helpers.sh"


def test_workspace_guard_quarantines_ignored_python_startup_payload() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		(workspace / ".gitignore").write_text("*.py\n", encoding="utf-8")
		(workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
		subprocess.run(["git", "-C", str(workspace), "add", ".gitignore", "tracked.txt"], check=True)
		subprocess.run(
			["git", "-C", str(workspace), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
			check=True,
		)
		manifest = runtime / "manifest.json"
		subprocess.run(
			["/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			check=True,
		)
		(workspace / "sitecustomize.py").write_text("raise RuntimeError('loaded')\n", encoding="utf-8")
		changed = runtime / "changed.txt"
		report = runtime / "report.json"
		quarantine = runtime / "quarantine"
		result = subprocess.run(
			[
				"/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "reconcile",
				"--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(quarantine), "--changed-paths-out", str(changed),
				"--report", str(report),
			],
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 20
		assert not (workspace / "sitecustomize.py").exists()
		assert (quarantine / "sitecustomize.py").is_file()
		assert "sitecustomize.py" in changed.read_text(encoding="utf-8").splitlines()
		assert json.loads(report.read_text(encoding="utf-8"))["rejected"][0]["reason"] == "python-startup-path"


def test_workspace_guard_quarantines_package_and_sourceless_startup_payloads() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		manifest = runtime / "manifest.json"
		subprocess.run(
			["/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			check=True,
		)
		startup_package = workspace / "sitecustomize"
		startup_package.mkdir()
		(startup_package / "__init__.py").write_text("raise RuntimeError('loaded')\n", encoding="utf-8")
		(workspace / "usercustomize.pyc").write_bytes(b"startup-bytecode")
		report = runtime / "report.json"
		result = subprocess.run(
			[
				"/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "reconcile",
				"--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(runtime / "quarantine"),
				"--changed-paths-out", str(runtime / "changed.txt"), "--report", str(report),
			],
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 20
		assert not startup_package.exists()
		assert not (workspace / "usercustomize.pyc").exists()
		assert (runtime / "quarantine" / "sitecustomize" / "__init__.py").is_file()
		assert (runtime / "quarantine" / "usercustomize.pyc").is_file()
		rejected_paths = {
			row["path"]: row["reason"]
			for row in json.loads(report.read_text(encoding="utf-8"))["rejected"]
		}
		assert rejected_paths["sitecustomize"] == "python-startup-path"
		assert rejected_paths["usercustomize.pyc"] == "python-startup-path"


def test_workspace_guard_restores_authorized_new_regular_file() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		manifest = runtime / "manifest.json"
		subprocess.run(
			["/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			check=True,
		)
		(workspace / "new-source.txt").write_text("safe\n", encoding="utf-8")
		changed = runtime / "changed.txt"
		report = runtime / "report.json"
		result = subprocess.run(
			[
				"/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "reconcile",
				"--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(runtime / "quarantine"), "--changed-paths-out", str(changed),
				"--report", str(report),
			],
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert (workspace / "new-source.txt").read_text(encoding="utf-8") == "safe\n"
		assert json.loads(report.read_text(encoding="utf-8"))["restored"] == ["new-source.txt"]


def test_workspace_guard_detects_symlink_to_git_metadata() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		manifest = runtime / "manifest.json"
		subprocess.run(
			["/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			check=True,
		)
		(workspace / "metadata-link").symlink_to(".git")
		changed = runtime / "changed.txt"
		report = runtime / "report.json"
		quarantine = runtime / "quarantine"
		result = subprocess.run(
			[
				"/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "reconcile",
				"--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(quarantine), "--changed-paths-out", str(changed),
				"--report", str(report),
			],
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 20
		assert not (workspace / "metadata-link").exists()
		assert (quarantine / "metadata-link").is_symlink()
		assert json.loads(report.read_text(encoding="utf-8"))["rejected"][0]["reason"] == "unsupported-object"


def test_workspace_guard_preserves_safe_file_when_rejecting_sibling() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		manifest = runtime / "manifest.json"
		subprocess.run(
			["/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			check=True,
		)
		(workspace / "safe.txt").write_text("safe\n", encoding="utf-8")
		(workspace / "sitecustomize.py").write_text("raise RuntimeError('loaded')\n", encoding="utf-8")
		report = runtime / "report.json"
		result = subprocess.run(
			[
				"/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "reconcile",
				"--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(runtime / "quarantine"),
				"--changed-paths-out", str(runtime / "changed.txt"), "--report", str(report),
			],
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 20
		assert (workspace / "safe.txt").read_text(encoding="utf-8") == "safe\n"
		assert not (workspace / "sitecustomize.py").exists()
		assert json.loads(report.read_text(encoding="utf-8"))["restored"] == ["safe.txt"]


def test_workspace_guard_rejects_nested_github_and_prior_symlink_type_change() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		(workspace / "tracked-link").symlink_to("target")
		manifest = runtime / "manifest.json"
		subprocess.run(
			["/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			check=True,
		)
		(workspace / "tracked-link").unlink()
		(workspace / "tracked-link").write_text("replacement\n", encoding="utf-8")
		(workspace / "src" / ".github").mkdir(parents=True)
		(workspace / "src" / ".github" / "payload.txt").write_text("hidden\n", encoding="utf-8")
		report = runtime / "report.json"
		result = subprocess.run(
			[
				"/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "reconcile",
				"--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(runtime / "quarantine"),
				"--changed-paths-out", str(runtime / "changed.txt"), "--report", str(report),
			],
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 20
		reasons = {row["path"]: row["reason"] for row in json.loads(report.read_text(encoding="utf-8"))["rejected"]}
		assert reasons["tracked-link"] == "unsupported-object"
		assert reasons["src/.github"] == "hidden-path"


def test_workspace_guard_reports_regular_file_replaced_by_directory() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		(workspace / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
		manifest = runtime / "manifest.json"
		subprocess.run(
			["/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			check=True,
		)
		(workspace / "config.py").unlink()
		(workspace / "config.py").mkdir()
		(workspace / "config.py" / "payload.py").write_text("VALUE = 2\n", encoding="utf-8")
		changed = runtime / "changed.txt"
		result = subprocess.run(
			[
				"/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "reconcile",
				"--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(runtime / "quarantine"), "--changed-paths-out", str(changed),
				"--report", str(runtime / "report.json"),
			],
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert "config.py" in changed.read_text(encoding="utf-8").splitlines()


def test_workspace_guard_restores_unreadable_directory_mode_before_blocking() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		protected = workspace / "protected"
		protected.mkdir()
		(protected / "base.txt").write_text("base\n", encoding="utf-8")
		manifest = runtime / "manifest.json"
		subprocess.run(
			["/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			check=True,
		)
		(protected / "sitecustomize.py").write_text("raise RuntimeError('loaded')\n", encoding="utf-8")
		protected.chmod(0)
		report = runtime / "report.json"
		result = subprocess.run(
			[
				"/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "reconcile",
				"--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(runtime / "quarantine"),
				"--changed-paths-out", str(runtime / "changed.txt"), "--report", str(report),
			],
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 20
		assert protected.stat().st_mode & 0o777 == 0o755
		assert not (protected / "sitecustomize.py").exists()
		reasons = {row["path"]: row["reason"] for row in json.loads(report.read_text(encoding="utf-8"))["rejected"]}
		assert reasons["protected"] == "directory-mode-changed"
		assert reasons["protected/sitecustomize.py"] == "python-startup-path"


def test_workspace_guard_normalizes_new_unreadable_directory_before_quarantine() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		manifest = runtime / "manifest.json"
		subprocess.run(
			["/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			check=True,
		)
		created = workspace / "output"
		created.mkdir()
		(created / "safe.txt").write_text("safe\n", encoding="utf-8")
		(created / "sitecustomize.py").write_text("raise RuntimeError('loaded')\n", encoding="utf-8")
		created.chmod(0)
		report = runtime / "report.json"
		result = subprocess.run(
			[
				"/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "reconcile",
				"--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(runtime / "quarantine"),
				"--changed-paths-out", str(runtime / "changed.txt"), "--report", str(report),
			],
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 20, result.stderr
		assert created.stat().st_mode & 0o700 == 0o700
		assert (created / "safe.txt").read_text(encoding="utf-8") == "safe\n"
		assert not (created / "sitecustomize.py").exists()
		assert (runtime / "quarantine" / "output" / "sitecustomize.py").is_file()
		reasons = {row["path"]: row["reason"] for row in json.loads(report.read_text(encoding="utf-8"))["rejected"]}
		assert reasons["output"] == "directory-mode-changed"
		assert reasons["output/sitecustomize.py"] == "python-startup-path"


def test_workspace_guard_quarantines_inside_unchanged_restrictive_directory() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		restricted = workspace / "restricted"
		restricted.mkdir()
		restricted.chmod(0)
		manifest = runtime / "manifest.json"
		subprocess.run(
			["/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			check=True,
		)
		restricted.chmod(0o700)
		(restricted / "sitecustomize.py").write_text("raise RuntimeError('loaded')\n", encoding="utf-8")
		restricted.chmod(0)
		result = subprocess.run(
			[
				"/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD), "reconcile",
				"--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(runtime / "quarantine"),
				"--changed-paths-out", str(runtime / "changed.txt"),
				"--report", str(runtime / "report.json"),
			],
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 20, result.stderr
		assert restricted.stat().st_mode & 0o777 == 0
		restricted.chmod(0o700)
		assert not (restricted / "sitecustomize.py").exists()
		assert (runtime / "quarantine" / "restricted" / "sitecustomize.py").is_file()


def test_validator_role_uses_isolated_no_site_python_outside_workspace() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		validator_output = runtime / "validator-output"
		validator_output.mkdir()
		validator_result = validator_output / "result.txt"
		sentinel = root / "startup-ran"
		(workspace / "sitecustomize.py").write_text(
			f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n",
			encoding="utf-8",
		)
		environment = os.environ.copy()
		environment.update(
			{
				"GH_PAT": "synthetic-secret",
				"PYTHONPATH": str(workspace),
				"RUNTIME_DIR": str(runtime),
				"UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1",
			}
		)
		result = subprocess.run(
			[
				"bash", str(SANDBOX), "--role", "validator", "--workspace", str(workspace),
				"--runtime-dir", str(runtime), "--writable-output-dir", str(validator_output),
				"--", "/usr/bin/python3", "-I", "-S", "-c",
				"import json,os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ok'); print(json.dumps([sys.flags.isolated,sys.flags.no_site,os.getcwd(),os.getenv('GH_PAT')]))",
				str(validator_result),
			],
			env=environment, capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		isolated, no_site, cwd, credential = json.loads(result.stdout)
		assert (isolated, no_site, credential) == (1, 1, None)
		assert Path(cwd) != workspace
		assert not sentinel.exists()
		assert validator_result.read_text(encoding="utf-8") == "ok"


def test_opencode_output_normalization_ignores_workspace_python_payloads() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		workspace.mkdir()
		sentinel = root / "startup-ran"
		payload = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n"
		(workspace / "sitecustomize.py").write_text(payload, encoding="utf-8")
		(workspace / "re.py").write_text(payload, encoding="utf-8")
		environment = os.environ.copy()
		environment["GH_PAT"] = "synthetic-secret"
		result = subprocess.run(
			[
				"bash", "-c",
				'source "$1"; printf "\\033[31mclean\\033[0m\\n" | opencode_strip_ansi',
				"opencode-output-test", str(OPENCODE_HELPERS),
			],
			cwd=workspace, env=environment, capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert result.stdout == "clean\n"
		assert not sentinel.exists()


def test_resolver_python_syntax_validation_is_read_only() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		(workspace / "valid.py").write_text("VALUE = 1\n", encoding="utf-8")
		conflicted = runtime / "conflicted.txt"
		touched = runtime / "touched.txt"
		conflicted.write_text("valid.py\n", encoding="utf-8")
		touched.write_text("valid.py\n", encoding="utf-8")
		environment = os.environ.copy()
		environment.update(
			{
				"POST_AGENT_VALIDATION_SANDBOX": str(SANDBOX),
				"POST_AGENT_VALIDATION_RUNTIME_DIR": str(runtime),
				"UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1",
			}
		)
		result = subprocess.run(
			[
				"bash", str(RESOLVER_GUARD), "--repo-root", str(workspace),
				"--conflicted-set", str(conflicted), "--touched-set", str(touched),
			],
			env=environment, capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert not (workspace / "__pycache__").exists()


def test_sandbox_scrubs_runner_credentials_and_shell_command_files() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		credential = root / "provider-token"
		credential.write_text("synthetic-provider-token", encoding="utf-8")
		config_dir = root / "codex"
		config_dir.mkdir()
		(config_dir / "config.toml").write_text(
			'model = "openai/gpt-5.6-sol"\n'
			'[model_providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\nenv_key = "OPENROUTER_API_KEY"\n',
			encoding="utf-8",
		)
		capture = root / "environment.json"
		environment = os.environ.copy()
		environment.update(
			{
				"BASH_ENV": str(root / "attacker-bash-env"),
				"ENV": str(root / "attacker-env"),
				"GH_TOKEN": "synthetic-gh-token",
				"GH_PAT": "synthetic-gh-pat",
				"GITHUB_ENV": str(root / "github-env"),
				"GITHUB_OUTPUT": str(root / "github-output"),
				"GIT_DIR": str(REPO_ROOT / ".git"),
				"GIT_WORK_TREE": str(REPO_ROOT),
				"MODEL_PROVIDER_CREDENTIAL_FILE": str(credential),
				"RUNTIME_DIR": str(root),
				"UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1",
			}
		)
		result = subprocess.run(
			[
				"bash", str(SANDBOX), "--role", "plan", "--workspace", str(REPO_ROOT),
				"--config-format", "codex", "--config", str(config_dir), "--runtime-dir", str(root),
				"--", "python3", "-c",
				f"import json,os; json.dump(dict(os.environ), open({str(capture)!r}, 'w'))",
			],
			cwd=REPO_ROOT,
			env=environment,
			capture_output=True,
			text=True,
			check=False,
		)
		assert result.returncode == 0, result.stderr
		isolated_environment = json.loads(capture.read_text(encoding="utf-8"))
		for forbidden_name in (
			"BASH_ENV", "ENV", "GH_TOKEN", "GH_PAT", "GITHUB_TOKEN", "GITHUB_ENV",
			"GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STATE", "SSH_AUTH_SOCK", "OPENROUTER_API_KEY",
			"GIT_DIR", "GIT_WORK_TREE",
		):
			assert forbidden_name not in isolated_environment
		assert isolated_environment["SANDBOX_PROVIDER_TOKEN"] == "sandbox-proxy"
		assert isolated_environment["GIT_TERMINAL_PROMPT"] == "0"


def test_sandbox_state_leaves_private_tmp_runtime_dir_for_runner_temp() -> None:
	# PrivateTmp=yes hides the host's /tmp from the unit, so ReadWritePaths= on a
	# scratch dir under a /tmp RUNTIME_DIR aborted every unit with status 226.
	with tempfile.TemporaryDirectory(dir="/tmp") as runtime_directory, \
		tempfile.TemporaryDirectory(dir=Path.home()) as runner_temp_directory:
		runtime_root = Path(runtime_directory)
		runner_temp_root = Path(runner_temp_directory).resolve()
		assert not str(runner_temp_root).startswith(("/tmp/", "/var/tmp/"))
		config_dir = runtime_root / "codex"
		config_dir.mkdir()
		(config_dir / "config.toml").write_text('model = "openai/gpt-5.6-sol"\n', encoding="utf-8")
		capture = runtime_root / "environment.json"
		environment = os.environ.copy()
		environment.pop("MODEL_PROVIDER_CREDENTIAL_FILE", None)
		environment.update(
			{
				"OPENROUTER_API_KEY": "synthetic-provider-token",
				"RUNNER_TEMP": str(runner_temp_root),
				"RUNTIME_DIR": str(runtime_root),
				"UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1",
			}
		)
		result = subprocess.run(
			[
				"bash", str(SANDBOX), "--role", "reviewer", "--workspace", str(REPO_ROOT),
				"--config-format", "codex", "--config", str(config_dir), "--runtime-dir", str(runtime_root),
				"--", "python3", "-c",
				f"import json,os; json.dump(dict(os.environ), open({str(capture)!r}, 'w'))",
			],
			cwd=REPO_ROOT,
			env=environment,
			capture_output=True,
			text=True,
			check=False,
		)
		assert result.returncode == 0, result.stderr
		isolated_environment = json.loads(capture.read_text(encoding="utf-8"))
		sandbox_runtime = Path(isolated_environment["RUNTIME_DIR"])
		assert sandbox_runtime.parent.parent == runner_temp_root
		assert sandbox_runtime.parent.name.startswith("agent-sandbox.")
		assert Path(isolated_environment["HOME"]).parent == sandbox_runtime.parent
		# The scratch dir and the temporary provider credential are both removed.
		assert list(runner_temp_root.iterdir()) == []
		assert not any(path.name.startswith(("agent-sandbox.", "provider-credential.")) for path in runtime_root.iterdir())


def test_sandbox_systemd_unit_reports_namespace_failures() -> None:
	sandbox_source = SANDBOX.read_text(encoding="utf-8")
	systemd_invocation = sandbox_source.split('"${systemd_run[@]}"', 1)[1].split("\n\n", 1)[0]
	assert "--quiet" not in systemd_invocation
	assert '--unit="${sandbox_unit_name}"' in systemd_invocation
	assert "--property=PrivateTmp=yes" in sandbox_source
	assert 'sandbox_dir="$(mktemp -d "${sandbox_state_root%/}/agent-sandbox.XXXXXX")"' in sandbox_source
	assert "sandbox_namespace_setup_failed role=${role} rc=226" in sandbox_source
	assert "sandbox_private_tmp_path role=${role}" in sandbox_source
	assert 'timeout --kill-after=2 10 "${sandbox_journal_cmd[@]}"' in sandbox_source


def test_workspace_guard_rejects_private_tmp_paths_before_execution() -> None:
	with tempfile.TemporaryDirectory(dir="/tmp") as runtime_directory, \
		tempfile.TemporaryDirectory(dir=Path.home()) as runner_temp_directory, \
		tempfile.TemporaryDirectory(dir=Path.home()) as workspace_directory:
		runner_temp_root = Path(runner_temp_directory).resolve()
		runtime_root = Path(runtime_directory).resolve()
		subprocess.run(["git", "init", "-q", workspace_directory], check=True)
		environment = os.environ.copy()
		environment.pop("POST_AGENT_WORKSPACE_GUARD_EXPECTED_SHA256", None)
		environment.update({"RUNNER_TEMP": str(runner_temp_root), "UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1"})
		command = [
			"bash", str(SANDBOX), "--role", "workspace-guard", "--workspace", workspace_directory,
			"--runtime-dir", str(runner_temp_root), "--", "/usr/bin/python3", "-I", "-S",
			"-c", "print('guard launched')",
		]
		unsafe_runtime_command = command.copy()
		unsafe_runtime_command[unsafe_runtime_command.index("--runtime-dir") + 1] = str(runtime_root)
		blocked_runtime = subprocess.run(unsafe_runtime_command, env=environment, capture_output=True, text=True)
		assert blocked_runtime.returncode != 0
		assert f"sandbox_private_tmp_path role=workspace-guard path={runtime_root}" in blocked_runtime.stderr
		assert "guard launched" not in blocked_runtime.stdout

		unsafe_manifest_command = command + ["--manifest", str(runtime_root / "manifest.json")]
		blocked_manifest = subprocess.run(unsafe_manifest_command, env=environment, capture_output=True, text=True)
		assert blocked_manifest.returncode != 0
		assert "sandbox_private_tmp_path role=workspace-guard" in blocked_manifest.stderr
		assert "guard launched" not in blocked_manifest.stdout

		allowed = subprocess.run(command, env=environment, capture_output=True, text=True)
		assert allowed.returncode == 0, allowed.stderr
		assert "guard launched" in allowed.stdout
		assert list(runner_temp_root.iterdir()) == []


def test_workspace_guard_rejects_symlinked_output_and_invalid_executable() -> None:
	with tempfile.TemporaryDirectory(dir="/tmp") as runtime_directory, \
		tempfile.TemporaryDirectory(dir=Path.home()) as runner_temp_directory, \
		tempfile.TemporaryDirectory(dir=Path.home()) as workspace_directory:
		runner_temp_root = Path(runner_temp_directory).resolve()
		subprocess.run(["git", "init", "-q", workspace_directory], check=True)
		redirect = runner_temp_root / "redirect"
		redirect.symlink_to(runtime_directory, target_is_directory=True)
		environment = os.environ.copy()
		environment.update({"RUNNER_TEMP": str(runner_temp_root), "UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1"})
		command = [
			"bash", str(SANDBOX), "--role", "workspace-guard", "--workspace", workspace_directory,
			"--runtime-dir", str(runner_temp_root), "--", "/usr/bin/python3", "-I", "-S",
			"-c", "print('guard launched')", "--report", str(redirect / "report.json"),
		]
		blocked = subprocess.run(command, env=environment, capture_output=True, text=True)
		assert blocked.returncode != 0
		assert "sandbox_private_tmp_path role=workspace-guard" in blocked.stderr
		command[command.index("--runtime-dir") + 1] = str(redirect)
		redirected_root = subprocess.run(command, env=environment, capture_output=True, text=True)
		assert redirected_root.returncode != 0
		assert "workspace guard runtime-dir must be canonical" in redirected_root.stderr
		command[command.index("--runtime-dir") + 1] = str(runner_temp_root)
		command[-1] = str(runner_temp_root.parent / "outside-report.json")
		outside = subprocess.run(command, env=environment, capture_output=True, text=True)
		assert outside.returncode != 0
		assert "workspace guard artifact is outside runtime-dir" in outside.stderr
		environment["POST_AGENT_WORKSPACE_GUARD_EXPECTED_SHA256"] = "0" * 64
		command[-1] = str(runner_temp_root / "report.json")
		mismatched = subprocess.run(command, env=environment, capture_output=True, text=True)
		assert mismatched.returncode != 0
		assert "workspace guard executable integrity check failed" in mismatched.stderr


def test_workspace_guard_reconciles_copied_workspace_with_external_git() -> None:
	with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
		root = Path(directory)
		repository = root / "repository"
		workspace = root / "workspace"
		runtime = root / "runtime"
		repository.mkdir()
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(repository)], check=True)
		(repository / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
		(workspace / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
		assert not (workspace / ".git").exists()
		environment = os.environ.copy()
		environment.update({
			"GIT_DIR": str(repository / ".git"), "GIT_WORK_TREE": str(workspace),
			"RUNNER_TEMP": str(runtime), "UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1",
		})
		manifest = runtime / "manifest.json"
		def run_guard(*guard_arguments: str) -> subprocess.CompletedProcess[str]:
			return subprocess.run(
				["bash", str(SANDBOX), "--role", "workspace-guard", "--workspace", str(workspace),
				 "--runtime-dir", str(runtime), "--", "/usr/bin/python3", "-I", "-S",
				 str(WORKSPACE_GUARD), *guard_arguments],
				env=environment, capture_output=True, text=True, check=False,
			)

		snapshot = run_guard("snapshot", "--workspace", str(workspace), "--manifest", str(manifest))
		assert snapshot.returncode == 0, snapshot.stderr
		(workspace / "allowed.txt").write_text("safe\n", encoding="utf-8")
		changed = runtime / "changed.txt"
		report = runtime / "report.json"
		quarantine = runtime / "quarantine"
		guard_arguments = (
			"reconcile", "--workspace", str(workspace), "--manifest", str(manifest),
			"--quarantine-dir", str(quarantine), "--changed-paths-out", str(changed),
			"--report", str(report),
		)
		accepted = run_guard(*guard_arguments)
		assert accepted.returncode == 0, accepted.stderr
		assert (workspace / "allowed.txt").read_text(encoding="utf-8") == "safe\n"
		assert json.loads(report.read_text(encoding="utf-8"))["restored"] == ["allowed.txt"]
		assert run_guard("snapshot", "--workspace", str(workspace), "--manifest", str(manifest)).returncode == 0
		(workspace / "blocked.ignored").write_text("bad\n", encoding="utf-8")
		reconcile = run_guard(*guard_arguments)
		assert reconcile.returncode == 20, reconcile.stderr
		assert (workspace / "allowed.txt").read_text(encoding="utf-8") == "safe\n"
		assert not (workspace / "blocked.ignored").exists()
		assert (quarantine / "blocked.ignored").is_file()
		assert json.loads(report.read_text(encoding="utf-8"))["rejected"][0]["reason"] == "ignored-path"
		assert "blocked.ignored" in changed.read_text(encoding="utf-8").splitlines()


def test_workspace_guard_rejects_invalid_external_git_and_keeps_other_roles_isolated() -> None:
	with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
		root = Path(directory)
		repository = root / "repository"
		workspace = root / "workspace"
		runtime = root / "runtime"
		repository.mkdir()
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(repository)], check=True)
		environment = os.environ.copy()
		environment.update({
			"GIT_DIR": str(repository / ".git"), "GIT_WORK_TREE": str(workspace),
			"RUNNER_TEMP": str(runtime), "UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1",
		})
		command = [
			"bash", str(SANDBOX), "--role", "workspace-guard", "--workspace", str(workspace),
			"--runtime-dir", str(runtime), "--", "/usr/bin/python3", "-I", "-S", "-c",
			"import json,os; print(json.dumps([os.getenv('GIT_DIR'), os.getenv('GIT_WORK_TREE')]))",
		]
		allowed = subprocess.run(command, env=environment, capture_output=True, text=True)
		assert allowed.returncode == 0, allowed.stderr
		assert json.loads(allowed.stdout) == [str(repository / ".git"), str(workspace)]
		for git_dir, work_tree in (
			(None, str(workspace)),
			(str(repository / ".git"), None),
			(str(repository / ".git"), str(repository)),
			(str(repository / ".git" / "missing"), str(workspace)),
		):
			invalid_environment = environment.copy()
			for name, value in (("GIT_DIR", git_dir), ("GIT_WORK_TREE", work_tree)):
				if value is None:
					invalid_environment.pop(name, None)
				else:
					invalid_environment[name] = value
			blocked = subprocess.run(command, env=invalid_environment, capture_output=True, text=True)
			assert blocked.returncode != 0
			assert "workspace guard" in blocked.stderr
			assert not blocked.stdout
		redirect = root / "metadata-link"
		redirect.symlink_to(repository / ".git", target_is_directory=True)
		environment["GIT_DIR"] = str(redirect)
		blocked = subprocess.run(command, env=environment, capture_output=True, text=True)
		assert blocked.returncode != 0
		assert "invalid workspace guard Git context" in blocked.stderr
		with tempfile.TemporaryDirectory(dir="/tmp") as private_directory:
			private_repository = Path(private_directory) / "repository"
			private_repository.mkdir()
			subprocess.run(["git", "init", "-q", str(private_repository)], check=True)
			environment["GIT_DIR"] = str(private_repository / ".git")
			blocked = subprocess.run(command, env=environment, capture_output=True, text=True)
			assert blocked.returncode != 0
			assert "sandbox_private_tmp_path role=workspace-guard" in blocked.stderr
		environment["GIT_DIR"] = str(repository / ".git")
		in_workspace_command = command.copy()
		in_workspace_command[in_workspace_command.index("--workspace") + 1] = str(repository)
		in_workspace_environment = environment.copy()
		in_workspace_environment.pop("GIT_DIR")
		in_workspace_environment.pop("GIT_WORK_TREE")
		in_workspace = subprocess.run(in_workspace_command, env=in_workspace_environment, capture_output=True, text=True)
		assert in_workspace.returncode == 0, in_workspace.stderr
		assert json.loads(in_workspace.stdout) == [None, None]
		validator_command = command.copy()
		validator_command[validator_command.index("workspace-guard")] = "validator"
		validator = subprocess.run(validator_command, env=environment, capture_output=True, text=True)
		assert validator.returncode == 0, validator.stderr
		assert json.loads(validator.stdout) == [None, None]
		config = root / "codex"
		config.mkdir()
		(config / "config.toml").write_text('model = "openai/gpt-5.6-sol"\n', encoding="utf-8")
		credential = root / "credential"
		credential.write_text("synthetic-provider-token", encoding="utf-8")
		environment["MODEL_PROVIDER_CREDENTIAL_FILE"] = str(credential)
		model_command = command.copy()
		model_command[model_command.index("workspace-guard")] = "plan"
		model_command[model_command.index("--runtime-dir"):model_command.index("--")] = [
			"--config-format", "codex", "--config", str(config), "--runtime-dir", str(runtime),
		]
		model = subprocess.run(model_command, env=environment, capture_output=True, text=True)
		assert model.returncode == 0, model.stderr
		assert json.loads(model.stdout) == [None, None]


def test_workspace_guard_external_git_context_keeps_ignore_classification() -> None:
	with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		git_dir = root / "metadata"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", "--separate-git-dir", str(git_dir), str(workspace)], check=True)
		(workspace / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
		(workspace / ".git").unlink()
		environment = os.environ.copy()
		environment.update({
			"GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(workspace),
			"RUNTIME_DIR": str(runtime), "UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1",
		})
		manifest = runtime / "manifest.json"
		command = [
			"bash", str(SANDBOX), "--role", "workspace-guard", "--workspace", str(workspace),
			"--runtime-dir", str(runtime), "--", "/usr/bin/python3", "-I", "-S",
			str(WORKSPACE_GUARD),
		]
		snapshot = subprocess.run(
			command + ["snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			env=environment, capture_output=True, text=True,
		)
		assert snapshot.returncode == 0, snapshot.stderr
		(workspace / "safe.txt").write_text("safe\n", encoding="utf-8")
		quarantine = runtime / "quarantine"
		reconcile_args = command + [
			"reconcile", "--workspace", str(workspace), "--manifest", str(manifest),
			"--quarantine-dir", str(quarantine), "--changed-paths-out", str(runtime / "changed.txt"),
			"--report", str(runtime / "report.json"),
		]
		safe_reconciled = subprocess.run(reconcile_args, env=environment, capture_output=True, text=True)
		assert safe_reconciled.returncode == 0, safe_reconciled.stderr
		assert (workspace / "safe.txt").read_text(encoding="utf-8") == "safe\n"
		snapshot = subprocess.run(
			command + ["snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			env=environment, capture_output=True, text=True,
		)
		assert snapshot.returncode == 0, snapshot.stderr
		(workspace / "payload.ignored").write_text("blocked\n", encoding="utf-8")
		reconciled = subprocess.run(reconcile_args, env=environment, capture_output=True, text=True)
		assert reconciled.returncode == 20, reconciled.stderr
		assert (workspace / "safe.txt").read_text(encoding="utf-8") == "safe\n"
		assert not (workspace / "payload.ignored").exists()
		assert (quarantine / "payload.ignored").read_text(encoding="utf-8") == "blocked\n"
		assert "git ignore classification failed" not in reconciled.stderr

		for invalid_context in (
			{"GIT_DIR": "", "GIT_WORK_TREE": ""},
			{"GIT_WORK_TREE": ""},
			{"GIT_DIR": str(root / "missing")},
			{"GIT_WORK_TREE": str(root)},
		):
			blocked = subprocess.run(
				reconcile_args, env=environment | invalid_context, capture_output=True, text=True,
			)
			assert blocked.returncode != 0
			assert "workspace guard" in blocked.stderr

		assert 'InaccessiblePaths=${protected_git_path}' in SANDBOX.read_text(encoding="utf-8")
		assert 'ReadOnlyPaths=${protected_git_path}' in SANDBOX.read_text(encoding="utf-8")


def test_provider_proxy_has_a_narrow_route_allowlist() -> None:
	module_text = PROXY.read_text(encoding="utf-8")
	assert '"POST": {"/chat/completions", "/responses", "/embeddings"}' in module_text
	assert '"GET": {"/models"}' in module_text
	assert 'prctl(4, 0, 0, 0, 0)' in module_text
	assert "openrouter.ai" in module_text
	assert "github.com" not in module_text
	sandbox_text = SANDBOX.read_text(encoding="utf-8")
	assert "-/var/run/docker.sock" in sandbox_text
	assert "_runner_file_commands" in sandbox_text
	assert "IPAddressDeny=any" in sandbox_text
	assert "summary|audit" in sandbox_text
	assert "implement-repair|diagnose|reviewer" in sandbox_text
	assert 'find "${workspace}" -xdev -name .git -print0' in sandbox_text
	assert 'InaccessiblePaths=${protected_git_path}' in sandbox_text
	assert 'common_env+=("GIT_DIR=${guard_git_dir}" "GIT_WORK_TREE=${workspace}")' in sandbox_text
	assert "ThreadingHTTPServer" not in module_text
	assert "class BoundedHTTPServer" in module_text
	assert "ThreadPoolExecutor" in module_text
	assert "settimeout(self.server.read_timeout_seconds)" in module_text
	assert 'sandbox_cgroup_controllers_file="/sys/fs/cgroup/cgroup.controllers"' in sandbox_text
	assert "for sandbox_required_cgroup_controller in cpu io memory pids; do" in sandbox_text
	assert "required cgroup controller is unavailable" in sandbox_text
	for resource_property in (
		"TasksMax", "MemoryMax", "MemorySwapMax", "CPUQuota", "IOReadBandwidthMax",
		"IOWriteBandwidthMax", "LimitNOFILE", "RuntimeMaxSec", "KillMode=control-group",
		"OOMPolicy=kill", "TimeoutStopSec",
	):
		assert resource_property in sandbox_text
	assert '--workers "${MODEL_PROVIDER_PROXY_WORKERS:-4}"' in sandbox_text
	assert '--queued-connections "${MODEL_PROVIDER_PROXY_QUEUED_CONNECTIONS:-8}"' in sandbox_text
	assert '--read-timeout-seconds "${MODEL_PROVIDER_PROXY_READ_TIMEOUT_SECONDS:-15}"' in sandbox_text


def test_provider_proxy_enforces_model_tokens_usage_and_accounting() -> None:
	proxy_namespace = runpy.run_path(str(PROXY))
	model_policy = proxy_namespace["ModelPolicy"](
		model_id="openai/allowed",
		context_length=1000,
		max_completion_tokens=200,
		prompt_price=Decimal("0.001"),
		completion_price=Decimal("0.002"),
		request_price=Decimal("0.01"),
		public_row={"id": "openai/allowed"},
	)
	prepare_request = proxy_namespace["_prepare_request"]
	prepared, reservation = prepare_request(
		"/chat/completions",
		json.dumps({"model": "openai/allowed", "messages": [], "max_tokens": 100}).encode(),
		{"openai/allowed": model_policy},
		150,
	)
	payload = json.loads(prepared)
	assert payload["usage"] == {"include": True}
	assert reservation == Decimal("1.21")
	for rejected_payload in (
		{"model": "openai/denied", "messages": []},
		{"model": "openai/allowed", "models": ["openai/denied"], "messages": []},
	):
		try:
			prepare_request(
				"/chat/completions", json.dumps(rejected_payload).encode(),
				{"openai/allowed": model_policy}, 150,
			)
		except PermissionError:
			pass
		else:
			raise AssertionError("unauthorized model selection was accepted")
	try:
		prepare_request(
			"/responses",
			json.dumps({"model": "openai/allowed", "input": "x", "max_output_tokens": 151}).encode(),
			{"openai/allowed": model_policy},
			150,
		)
	except ValueError:
		pass
	else:
		raise AssertionError("oversized output request was accepted")
	try:
		prepare_request(
			"/chat/completions",
			json.dumps({"model": "openai/allowed", "messages": [], "plugins": [{"id": "web"}]}).encode(),
			{"openai/allowed": model_policy},
			150,
		)
	except ValueError:
		pass
	else:
		raise AssertionError("provider-side paid plugin was accepted")

	usage_parser = proxy_namespace["_usage_cost_from_tail"]
	assert usage_parser(b'{"usage":{"cost":0.25}}') == Decimal("0.25")
	assert usage_parser(b'x' * 32 + b'"usage":{"cost":0.375}}') == Decimal("0.375")
	assert usage_parser(b'data: {"usage":{"cost":0.5}}\n\ndata: [DONE]\n') == Decimal("0.5")
	accounting = proxy_namespace["ProxyAccounting"](2, 1, Decimal("2"))
	assert accounting.reserve(Decimal("1")) is True
	assert accounting.reserve(Decimal("1.01")) is False
	accounting.settle(Decimal("1"), Decimal("0.5"))
	assert accounting.reserve(Decimal("1.5")) is True
	accounting.settle(Decimal("1.5"), None)
	assert accounting.reserve(Decimal("0.01")) is False


def test_provider_proxy_recovers_after_bounded_partial_connections() -> None:
	proxy_namespace = runpy.run_path(str(PROXY))
	server = proxy_namespace["BoundedHTTPServer"](
		("127.0.0.1", 0),
		proxy_namespace["ProviderProxy"],
		workers=1,
		queued_connections=1,
		read_timeout_seconds=0.1,
	)
	server.provider_credential = "synthetic"
	server.model_policies = {}
	server.max_output_tokens = 1
	server.accounting = proxy_namespace["ProxyAccounting"](20, 1, Decimal("1"))
	server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
	server_thread.start()
	partial_clients: list[socket.socket] = []
	try:
		for _index in range(8):
			client = socket.create_connection(server.server_address, timeout=1)
			partial_clients.append(client)
		time.sleep(0.35)
		connection = http.client.HTTPConnection(*server.server_address, timeout=1)
		connection.request("GET", "/healthz")
		response = connection.getresponse()
		assert response.status == 200
		assert json.loads(response.read()) == {"status": "ok"}
		connection.close()
	finally:
		for client in partial_clients:
			client.close()
		server.shutdown()
		server.server_close()
		server_thread.join(timeout=2)


def test_package_download_proxy_allows_only_pypi_tls_tunnels() -> None:
	package_proxy_spec = importlib.util.spec_from_file_location("package_download_proxy", PACKAGE_PROXY)
	assert package_proxy_spec is not None and package_proxy_spec.loader is not None
	package_proxy_module = importlib.util.module_from_spec(package_proxy_spec)
	package_proxy_spec.loader.exec_module(package_proxy_module)
	assert package_proxy_module._parse_connect_target("pypi.org:443") == ("pypi.org", 443)
	assert package_proxy_module._parse_connect_target("files.pythonhosted.org:443") == (
		"files.pythonhosted.org", 443
	)
	assert package_proxy_module._parse_connect_target("github.com:443") is None
	assert package_proxy_module._parse_connect_target("pypi.org.evil.example:443") is None
	assert package_proxy_module._parse_connect_target("pypi.org:80") is None
	stage_helper_text = (REPO_ROOT / "scripts" / "stage_workflow_support.sh").read_text(
		encoding="utf-8"
	)
	assert "package_download_proxy.py" in stage_helper_text


def test_workflows_do_not_give_github_tokens_to_primary_model_steps() -> None:
	plan = (REPO_ROOT / ".github/workflows/plan.yml").read_text(encoding="utf-8")
	implement = (REPO_ROOT / ".github/workflows/implement.yml").read_text(encoding="utf-8")
	review = (REPO_ROOT / ".github/workflows/review_autofix.yml").read_text(encoding="utf-8")
	plan_step = plan.split("      - name: Run Codex planning\n", 1)[1].split("\n      - name:", 1)[0]
	implement_step = implement.split("      - name: Run Codex implementation\n", 1)[1].split("\n      - name:", 1)[0]
	editor_step = review.split("      - name: Apply fixes with editor model\n", 1)[1].split("\n      - name:", 1)[0]
	for model_step in (plan_step, implement_step, editor_step):
		assert "GH_TOKEN: ${{" not in model_step
		assert "GH_PAT: ${{" not in model_step
	assert "untrusted_process_sandbox.sh" in (REPO_ROOT / "scripts/run_plan_codex.sh").read_text(encoding="utf-8")
	assert "untrusted_process_sandbox.sh" in implement_step
	assert "untrusted_process_sandbox.sh" in (REPO_ROOT / "scripts/opencode_helpers.sh").read_text(encoding="utf-8")
	assert 'BASH_ENV: ""' in implement_step
	assert 'BASH_ENV: ""' in editor_step


def test_every_writer_path_reconciles_complete_workspace_manifest() -> None:
	implement = (REPO_ROOT / ".github/workflows/implement.yml").read_text(encoding="utf-8")
	review = (REPO_ROOT / ".github/workflows/review_autofix.yml").read_text(encoding="utf-8")
	poller = (REPO_ROOT / ".github/workflows/orchestrate_poll.yml").read_text(encoding="utf-8")
	assert len(review.encode("utf-8")) < 480_000
	assert 'review_autofix_step_iteration_summary.sh' in review
	assert 'review_autofix_step_iteration_summary.sh' in (REPO_ROOT / "scripts/stage_workflow_support.sh").read_text(encoding="utf-8")
	for workflow, role_name in ((implement, "implement"), (review, "review"), (poller, "poller")):
		assert f'POST_AGENT_WORKSPACE_GUARD_RUNTIME_DIR="$(mktemp -d "${{RUNNER_TEMP:?}}/post-agent-{role_name}-' in workflow
		assert 'echo "POST_AGENT_WORKSPACE_GUARD_RUNTIME_DIR=${POST_AGENT_WORKSPACE_GUARD_RUNTIME_DIR}"' in workflow
		assert f'"${{RUNNER_TEMP:?}}/post-agent-{role_name}-${{GITHUB_RUN_ID}}-${{GITHUB_RUN_ATTEMPT}}."*' in workflow
		assert 'realpath -e "${POST_AGENT_WORKSPACE_GUARD_RUNTIME_DIR}"' in workflow
	for role in ("implement", "repair"):
		assert f"post-agent-{role}-" in implement
	assert implement.count("post_agent_workspace_guard.py\" snapshot") >= 2
	assert implement.count("post_agent_workspace_guard.py\" reconcile") >= 2
	assert implement.count('--runtime-dir "${POST_AGENT_WORKSPACE_GUARD_RUNTIME_DIR}"') >= 4
	assert 'install -m 0755 "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/post_agent_workspace_guard.py" "${POST_AGENT_WORKSPACE_GUARD_RUNTIME_DIR}/post_agent_workspace_guard.py"' in implement

	for script_name in (
		"review_apply_fixes.sh",
		"review_conflict_resolve.sh",
		"review_rb_judge.sh",
		"orchestrate_poll_process.sh",
	):
		script = (REPO_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
		assert "post_agent_workspace_guard.py" in script or "TRUSTED_POLLER_WORKSPACE_GUARD" in script
		assert "workspace-guard" in script
		assert "reconcile" in script
		assert "POST_AGENT_WORKSPACE_GUARD_RUNTIME_DIR" in script
		assert 'post-agent-' not in script or '"${RUNTIME_DIR}/post-agent-' not in script

	commit_step = (REPO_ROOT / ".github/workflows/review_autofix.yml").read_text(encoding="utf-8").split(
		"      - name: Commit changes\n", 1
	)[1].split("\n      - name:", 1)[0]
	assert "GH_PAT: ${{ secrets.GH_PAT }}" not in commit_step
	assert "refusing retry or partial-work salvage" in implement
	assert implement.count('echo "POST_AGENT_WORKSPACE_GUARD_FAILED=true" >> "$GITHUB_ENV"') >= 4
	assert '::error::Post-agent workspace guard rejected repair attempt ${attempt} after model failure;' in implement
	assert '::error::Post-agent workspace guard rejected repair attempt ${attempt};' in implement
	assert implement.count("env.POST_AGENT_WORKSPACE_GUARD_FAILED != 'true'") >= 12
	assert "Review-blocked fix workspace guard rejected" in (
		REPO_ROOT / "scripts" / "review_rb_judge.sh"
	).read_text(encoding="utf-8")
	assert (
		REPO_ROOT / "scripts" / "review_rb_judge.sh"
	).read_text(encoding="utf-8").count("| run_review_rb_validator_python -c '") >= 2
	poller_process = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
	for guard_failure in ("Review-blocked workspace snapshot failed for PR", "Review-blocked workspace guard rejected PR"):
		failure_branch = poller_process.split(guard_failure, 1)[1].split("\n          fi", 1)[0]
		assert "break" in failure_branch and "exit 78" not in failure_branch
	assert 'if [ "${RB_JUDGE_SUCCESS}" != "true" ]; then' in poller_process
	assert 'rb_cleanup_combined_workspace\n        continue' in poller_process
	assert 'RB_JUDGE_JSON="$(run_poller_isolated_python "${PWD}" -c "' in (
		REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
	).read_text(encoding="utf-8")
	resolver_process = (REPO_ROOT / "scripts" / "review_conflict_resolve.sh").read_text(encoding="utf-8")
	resolver_failure_branch = resolver_process.split("workspace_safety_violation; aborting before output parsing.", 1)[1].split("fi", 1)[0]
	assert 'emit_conflict_resolver_substate "Failed" "${attempt}"' in resolver_failure_branch
	for guarded_script_name, guard_rejection_message in (
		("review_apply_fixes.sh", "aborting before output parsing or retry"),
		("review_conflict_resolve.sh", "aborting before output parsing"),
		("review_rb_judge.sh", "aborting before output parsing or publication"),
	):
		guarded_script = (REPO_ROOT / "scripts" / guarded_script_name).read_text(encoding="utf-8")
		guard_rejection_at = guarded_script.index(guard_rejection_message)
		assert "exit 78" in guarded_script[guard_rejection_at:guard_rejection_at + 200]
	assert "--writable-output-dir" in (REPO_ROOT / "scripts" / "untrusted_process_sandbox.sh").read_text(
		encoding="utf-8"
	)
	assert "/usr/bin/python3 -I -S -c" in OPENCODE_HELPERS.read_text(encoding="utf-8")
	resolver_guard = RESOLVER_GUARD.read_text(encoding="utf-8")
	assert "ast.parse" in resolver_guard
	assert "-m py_compile" not in resolver_guard


def test_trusted_git_writer_never_executes_repository_hooks() -> None:
	with tempfile.TemporaryDirectory() as directory:
		repo = Path(directory) / "repo"
		repo.mkdir()
		subprocess.run(["git", "init", "-q", str(repo)], check=True)
		(repo / "tracked.txt").write_text("before\n", encoding="utf-8")
		subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
		subprocess.run(
			["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
			check=True,
		)
		sentinel = repo / "hook-ran"
		hook = repo / ".git" / "hooks" / "pre-commit"
		hook.write_text(f"#!/usr/bin/env bash\ntouch {sentinel}\nexit 1\n", encoding="utf-8")
		hook.chmod(0o755)
		(repo / "tracked.txt").write_text("after\n", encoding="utf-8")
		subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
		result = subprocess.run(
			["bash", str(TRUSTED_GIT_WRITE), "commit", "--repo", str(repo), "--message", "safe"],
			capture_output=True,
			text=True,
			check=False,
		)
		assert result.returncode == 0, result.stderr
		assert not sentinel.exists()


def test_causal_scope_includes_new_reverse_route_caller() -> None:
	with tempfile.TemporaryDirectory() as directory:
		repo = Path(directory) / "repo"
		repo.mkdir()
		subprocess.run(["git", "init", "-q", str(repo)], check=True)
		(repo / "sink.py").write_text(
			"def privileged_sink(user):\n\treturn user.secret\n", encoding="utf-8"
		)
		subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
		subprocess.run(
			["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
			check=True,
		)
		base_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
		(repo / "route.py").write_text(
			"from sink import privileged_sink\n\n"
			"@app.route('/public')\n"
			"def public_route():\n\treturn privileged_sink(None)\n",
			encoding="utf-8",
		)
		subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
		subprocess.run(
			["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "route"],
			check=True,
		)
		head_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
		result = subprocess.run(
			[
				"python3", str(CAUSALITY), "--repo", str(repo), "--file", "sink.py",
				"--line", "1", "--base", base_sha, "--head", head_sha,
			],
			capture_output=True,
			text=True,
			check=True,
		)
		payload = json.loads(result.stdout)
		assert payload["causal_scope_status"] == "complete"
		assert payload["causal_scope_files"] == ["route.py", "sink.py"]
		assert payload["causal_scope_changed_files"] == ["route.py"]
		assert payload["causal_scope_fingerprint"].startswith("sha256:")
		causality_text = CAUSALITY.read_text(encoding="utf-8")
		assert '["git", "archive", "--format=tar", ref, "--", *files]' in causality_text
		assert 'if not files:\n\t\treturn symbols, sources' in causality_text


def test_causal_scope_is_indeterminate_when_reverse_callers_exceed_depth_bound() -> None:
	with tempfile.TemporaryDirectory() as directory:
		repo = Path(directory) / "repo"
		repo.mkdir()
		subprocess.run(["git", "init", "-q", str(repo)], check=True)
		(repo / "app.py").write_text(
			"def privileged_sink(user):\n\treturn user.secret\n\n"
			"def caller_1(user):\n\treturn privileged_sink(user)\n\n"
			"def caller_2(user):\n\treturn caller_1(user)\n\n"
			"def caller_3(user):\n\treturn caller_2(user)\n\n"
			"def caller_4(user):\n\treturn caller_3(user)\n\n"
			"def caller_5(user):\n\treturn caller_4(user)\n",
			encoding="utf-8",
		)
		subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
		subprocess.run(
			["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
			check=True,
		)
		base_sha = subprocess.check_output(
			["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
		).strip()
		result = subprocess.run(
			[
				"python3", str(CAUSALITY), "--repo", str(repo), "--file", "app.py",
				"--line", "1", "--base", base_sha, "--head", base_sha,
			],
			capture_output=True,
			text=True,
			check=True,
		)
		payload = json.loads(result.stdout)
		assert payload["causal_scope_status"] == "indeterminate"


def test_causal_scope_tracks_module_level_route_guard_deletion() -> None:
	with tempfile.TemporaryDirectory() as directory:
		repo = Path(directory) / "repo"
		repo.mkdir()
		source_with_guard = (
			"def require_admin(user):\n\treturn user.is_admin\n\n"
			"def privileged_sink(user):\n\treturn user.secret\n\n"
			"ROUTES = {'/admin': (privileged_sink, require_admin)}\n"
		)
		(repo / "app.py").write_text(source_with_guard, encoding="utf-8")
		subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
		subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
		subprocess.run(
			["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
			check=True,
		)
		base_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
		(repo / "app.py").write_text(
			source_with_guard.replace("(privileged_sink, require_admin)", "(privileged_sink, None)"),
			encoding="utf-8",
		)
		subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
		subprocess.run(
			["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "remove guard"],
			check=True,
		)
		head_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
		result = subprocess.run(
			[
				"python3", str(CAUSALITY), "--repo", str(repo), "--file", "app.py",
				"--line", "4", "--base", base_sha, "--head", head_sha,
			],
			capture_output=True, text=True, check=True,
		)
		payload = json.loads(result.stdout)
		assert payload["causal_scope_status"] == "complete"
		assert payload["causal_scope_changed_files"] == ["app.py"]
		assert "app.py" in payload["causal_scope_files"]


def test_shared_waiver_revalidation_rejects_routing_config_changes() -> None:
	with tempfile.TemporaryDirectory() as directory:
		repo = Path(directory) / "repo"
		repo.mkdir()
		subprocess.run(["git", "init", "-q", str(repo)], check=True)
		(repo / "app.py").write_text("def privileged_sink(user):\n\treturn user.secret\n", encoding="utf-8")
		(repo / "caller_1.py").write_text(
			"from app import privileged_sink\n\ndef caller_1(user):\n\treturn privileged_sink(user)\n",
			encoding="utf-8",
		)
		(repo / "caller_2.py").write_text(
			"from caller_1 import caller_1\n\ndef caller_2(user):\n\treturn caller_1(user)\n",
			encoding="utf-8",
		)
		(repo / "caller_3.py").write_text(
			"from caller_2 import caller_2\n\ndef caller_3(user):\n\treturn caller_2(user)\n",
			encoding="utf-8",
		)
		(repo / "caller_4.py").write_text(
			"from caller_3 import caller_3\n\ndef caller_4(user):\n\treturn caller_3(user)\n",
			encoding="utf-8",
		)
		subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
		subprocess.run(
			["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
			check=True,
		)
		base_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
		scope_result = subprocess.run(
			[
				"python3", str(CAUSALITY), "--repo", str(repo), "--file", "app.py",
				"--line", "1", "--base", base_sha, "--head", base_sha,
			],
			capture_output=True, text=True, check=True,
		)
		scope = json.loads(scope_result.stdout)
		waiver_key = "sha256:" + "a" * 64
		finding = {
			"file": "app.py", "line": 1, "waiver_match_key": waiver_key,
			"causal_scope_schema": scope["causal_scope_schema"],
			"causal_scope_status": scope["causal_scope_status"],
			"causal_scope_fingerprint": scope["causal_scope_fingerprint"],
			"causal_scope_files": scope["causal_scope_files"],
		}
		waiver = dict(finding, audited_head_sha=base_sha)
		finding_path = repo / "finding.json"
		waiver_path = repo / "waiver.json"
		finding_path.write_text(json.dumps(finding), encoding="utf-8")
		waiver_path.write_text(json.dumps(waiver), encoding="utf-8")

		(repo / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")
		subprocess.run(["git", "-C", str(repo), "add", "unrelated.py"], check=True)
		subprocess.run(
			["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "unrelated"],
			check=True,
		)
		unrelated_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
		valid_result = subprocess.run(
			[
				"python3", str(CAUSALITY), "revalidate-waiver", "--repo", str(repo),
				"--audited-head", base_sha, "--current-head", unrelated_sha,
				"--finding-json", str(finding_path), "--waiver-json", str(waiver_path),
			],
			capture_output=True, text=True, check=True,
		)
		assert json.loads(valid_result.stdout)["valid"] is True

		(repo / "unrelated.py").write_text(
			"from caller_4 import caller_4 as final_caller\n\n"
			"def public_route(user):\n\treturn final_caller(user)\n",
			encoding="utf-8",
		)
		subprocess.run(["git", "-C", str(repo), "add", "unrelated.py"], check=True)
		subprocess.run(
			["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "new caller"],
			check=True,
		)
		caller_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
		caller_result = subprocess.run(
			[
				"python3", str(CAUSALITY), "revalidate-waiver", "--repo", str(repo),
				"--audited-head", base_sha, "--current-head", caller_sha,
				"--finding-json", str(finding_path), "--waiver-json", str(waiver_path),
			],
			capture_output=True, text=True, check=True,
		)
		assert json.loads(caller_result.stdout) == {
			"reason": "current causal scope is indeterminate", "valid": False,
		}

		(repo / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")
		(repo / "routes.yaml").write_text("public: privileged_sink\n", encoding="utf-8")
		subprocess.run(["git", "-C", str(repo), "add", "unrelated.py", "routes.yaml"], check=True)
		subprocess.run(
			["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "route"],
			check=True,
		)
		route_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
		invalid_result = subprocess.run(
			[
				"python3", str(CAUSALITY), "revalidate-waiver", "--repo", str(repo),
				"--audited-head", base_sha, "--current-head", route_sha,
				"--finding-json", str(finding_path), "--waiver-json", str(waiver_path),
			],
			capture_output=True, text=True, check=True,
		)
		assert json.loads(invalid_result.stdout)["valid"] is False
