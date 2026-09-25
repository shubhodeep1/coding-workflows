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

import pytest


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


def test_workspace_guard_uses_only_workspace_matched_external_git_context() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		git_dir = root / "metadata"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(
			["git", "init", "-q", "--separate-git-dir", str(git_dir), str(workspace)],
			check=True,
		)
		(workspace / ".git").unlink()
		(workspace / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
		environment = os.environ.copy()
		environment.update({
			"GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(workspace),
			"RUNTIME_DIR": str(runtime), "UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1",
		})

		def sandbox_run(role: str, *command: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
			return subprocess.run(
				["bash", str(SANDBOX), "--role", role, "--workspace", str(workspace),
				 "--runtime-dir", str(runtime), "--", *command],
				env=environment if env is None else env,
				capture_output=True, text=True, check=False,
			)

		manifest = runtime / "manifest.json"
		guard_command = ("/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD))
		snapshot = sandbox_run(
			"workspace-guard", *guard_command, "snapshot", "--workspace", str(workspace),
			"--manifest", str(manifest),
		)
		assert snapshot.returncode == 0, snapshot.stderr
		(workspace / "ordinary.txt").write_text("ordinary\n", encoding="utf-8")
		accepted = sandbox_run(
			"workspace-guard", *guard_command, "reconcile", "--workspace", str(workspace),
			"--manifest", str(manifest), "--quarantine-dir", str(runtime / "accepted-quarantine"),
			"--changed-paths-out", str(runtime / "accepted-changes"), "--report", str(runtime / "accepted.json"),
		)
		assert accepted.returncode == 0, accepted.stderr
		assert (workspace / "ordinary.txt").read_text(encoding="utf-8") == "ordinary\n"
		assert json.loads((runtime / "accepted.json").read_text(encoding="utf-8"))["restored"] == ["ordinary.txt"]

		(workspace / ".git").write_text(f"gitdir: {root / 'forged-metadata'}\n", encoding="utf-8")
		(workspace / "ignored.txt").write_text("ignored\n", encoding="utf-8")
		rejected = sandbox_run(
			"workspace-guard", *guard_command, "reconcile", "--workspace", str(workspace),
			"--manifest", str(manifest), "--quarantine-dir", str(runtime / "rejected-quarantine"),
			"--changed-paths-out", str(runtime / "rejected-changes"), "--report", str(runtime / "rejected.json"),
		)
		assert rejected.returncode == 20, rejected.stderr
		assert not (workspace / "ignored.txt").exists()
		assert (runtime / "rejected-quarantine" / "ignored.txt").is_file()
		assert not (workspace / ".git").exists()
		assert "ignored-path" in [
			row["reason"] for row in json.loads((runtime / "rejected.json").read_text(encoding="utf-8"))["rejected"]
		]

		inspect_environment = ("/usr/bin/python3", "-I", "-S", "-c",
			"import json,os; print(json.dumps([os.getenv('GIT_DIR'),os.getenv('GIT_WORK_TREE')]))")
		for role in ("validator", "plan"):
			if role == "plan":
				config = root / "config"
				config.mkdir()
				(config / "config.toml").write_text('model = "openai/gpt-5.6-sol"\n', encoding="utf-8")
				credential = root / "provider-token"
				credential.write_text("synthetic", encoding="utf-8")
				command = ["bash", str(SANDBOX), "--role", role, "--workspace", str(workspace),
					"--config-format", "codex", "--config", str(config), "--runtime-dir", str(runtime),
					"--", *inspect_environment]
				environment["MODEL_PROVIDER_CREDENTIAL_FILE"] = str(credential)
				result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
			else:
				result = sandbox_run(role, *inspect_environment)
			assert result.returncode == 0, result.stderr
			assert json.loads(result.stdout) == [None, None]

		for invalid_environment in (
			{**environment, "GIT_WORK_TREE": ""},
			{**environment, "GIT_DIR": ""},
			{**environment, "GIT_WORK_TREE": str(root)},
			{**environment, "GIT_DIR": str(runtime)},
			{**environment, "GIT_DIR": str(workspace)},
		):
			invalid = sandbox_run("workspace-guard", *inspect_environment, env=invalid_environment)
			assert invalid.returncode != 0

		sandbox_text = SANDBOX.read_text(encoding="utf-8")
		assert 'append_git_metadata_path "${guard_git_dir}"' in sandbox_text
		assert '--property="ReadOnlyPaths=${protected_git_path}"' in sandbox_text


def test_workspace_guard_keeps_in_tree_git_discovery_without_pair() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		manifest = runtime / "manifest.json"
		environment = {key: value for key, value in os.environ.items()
			if key not in {"GIT_DIR", "GIT_WORK_TREE"}}
		environment["UNTRUSTED_PROCESS_SANDBOX_TEST_MODE"] = "1"
		command = ["bash", str(SANDBOX), "--role", "workspace-guard", "--workspace", str(workspace),
			"--runtime-dir", str(runtime), "--", "/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD)]
		snapshot = subprocess.run(
			[*command, "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			env=environment, capture_output=True, text=True, check=False,
		)
		assert snapshot.returncode == 0, snapshot.stderr
		(workspace / "ordinary.txt").write_text("ordinary\n", encoding="utf-8")
		reconcile = subprocess.run(
			[*command, "reconcile", "--workspace", str(workspace), "--manifest", str(manifest),
			 "--quarantine-dir", str(runtime / "quarantine"), "--changed-paths-out", str(runtime / "changed"),
			 "--report", str(runtime / "report.json")],
			env=environment, capture_output=True, text=True, check=False,
		)
		assert reconcile.returncode == 0, reconcile.stderr
		assert (workspace / "ordinary.txt").read_text(encoding="utf-8") == "ordinary\n"


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
		):
			assert forbidden_name not in isolated_environment
		assert isolated_environment["SANDBOX_PROVIDER_TOKEN"] == "sandbox-proxy"
		assert isolated_environment["GIT_TERMINAL_PROMPT"] == "0"


def test_sandbox_rejects_private_tmp_namespace_control_root() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		environment = os.environ.copy()
		environment.update(
			{
				"RUNNER_TEMP": "/tmp",
				"UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1",
			}
		)
		result = subprocess.run(
			[
				"bash", str(SANDBOX), "--role", "validator", "--workspace", str(workspace),
				"--runtime-dir", str(runtime), "--", "/usr/bin/python3", "-I", "-S", "-c", "print('unreachable')",
			],
			env=environment, capture_output=True, text=True, check=False,
		)
		assert result.returncode == 1
		assert "namespace control root cannot be beneath private temporary storage" in result.stderr


def test_sandbox_rejects_inaccessible_namespace_control_root() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		control_root = root / "control"
		workspace.mkdir()
		runtime.mkdir()
		control_root.mkdir()
		control_root.chmod(0)
		try:
			environment = os.environ.copy()
			environment.update({"RUNNER_TEMP": str(control_root), "UNTRUSTED_PROCESS_SANDBOX_TEST_MODE": "1"})
			result = subprocess.run(
				[
					"bash", str(SANDBOX), "--role", "validator", "--workspace", str(workspace),
					"--runtime-dir", str(runtime), "--", "/usr/bin/python3", "-I", "-S", "-c", "print('unreachable')",
				],
				env=environment, capture_output=True, text=True, check=False,
			)
			assert result.returncode == 1
			assert "namespace control root is unavailable" in result.stderr
		finally:
			control_root.chmod(0o700)


def test_sandbox_rejects_special_objects_in_external_writable_tree() -> None:
	with tempfile.TemporaryDirectory(dir=REPO_ROOT.parent) as control_directory:
		control_root = Path(control_directory)
		workspace = control_root / "workspace"
		runtime = control_root / "runtime"
		validator_output = runtime / "validator-output"
		workspace.mkdir()
		validator_output.mkdir(parents=True)
		os.mkfifo(validator_output / "blocked-fifo")
		environment = os.environ.copy()
		environment.update({"RUNNER_TEMP": str(control_root)})
		result = subprocess.run(
			[
				"bash", str(SANDBOX), "--role", "validator", "--workspace", str(workspace),
				"--runtime-dir", str(runtime), "--writable-output-dir", str(validator_output),
				"--", "/usr/bin/python3", "-I", "-S", "-c", "print('unreachable')",
			],
			env=environment, capture_output=True, text=True, check=False,
		)
		assert result.returncode == 1
		assert "external writable tree contains an unsupported object" in result.stderr


def test_production_sandbox_starts_with_private_tmp_runtime() -> None:
	require_smoke = os.environ.get("REQUIRE_PRODUCTION_SANDBOX_SMOKE") == "1"
	runner_temp_value = os.environ.get("RUNNER_TEMP", "")
	runner_temp = Path(runner_temp_value).resolve() if runner_temp_value else None
	if runner_temp is None or not runner_temp.is_dir() or runner_temp == Path("/tmp") or Path("/tmp") in runner_temp.parents:
		if require_smoke:
			raise AssertionError("RUNNER_TEMP must resolve outside /tmp for the production sandbox smoke")
		pytest.skip("namespace-visible RUNNER_TEMP is unavailable")

	with tempfile.TemporaryDirectory(prefix="sandbox-control-test-", dir=runner_temp) as control_directory:
		control_root = Path(control_directory)
		before_sandboxes = set(control_root.glob("agent-sandbox.*"))
		with tempfile.TemporaryDirectory(prefix="sandbox-private-runtime-", dir="/tmp") as runtime_directory:
			runtime = Path(runtime_directory)
			credential = runtime / "provider-token"
			credential.write_text("synthetic-provider-token", encoding="utf-8")
			credential.chmod(0o600)
			forwarded_output = runtime / "forwarded-output.txt"
			config_dir = control_root / "codex"
			config_dir.mkdir()
			(config_dir / "config.toml").write_text(
				'model = "openai/gpt-5.6-sol"\n'
				'[model_providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\nenv_key = "OPENROUTER_API_KEY"\n',
				encoding="utf-8",
			)
			environment = os.environ.copy()
			environment.update(
				{
					"GH_PAT": "synthetic-gh-pat",
					"MODEL_PROVIDER_CREDENTIAL_FILE": str(credential),
					"RUNTIME_DIR": str(runtime),
					"RUNNER_TEMP": str(control_root),
					"CODEX_THREAD_REUSE_OUTPUT_FILE": str(forwarded_output),
				}
			)
			environment.pop("UNTRUSTED_PROCESS_SANDBOX_TEST_MODE", None)
			probe = (
				"import glob,json,os,pathlib,sys; "
				"readable=[]; "
				"candidates=glob.glob(sys.argv[2] + '/agent-sandbox.*/provider-credential'); "
				"[(readable.append(path) if os.access(path, os.R_OK) else None) for path in candidates]; "
				"pathlib.Path(os.environ['CODEX_THREAD_REUSE_OUTPUT_FILE']).write_text('forwarded'); "
				"print(json.dumps({'source_exists': pathlib.Path(sys.argv[1]).exists(), "
				"'copied_credentials': candidates, 'readable_credentials': readable, "
				"'gh_pat': os.getenv('GH_PAT')}))"
			)
			result = subprocess.run(
				[
					"bash", str(SANDBOX), "--role", "plan", "--workspace", str(REPO_ROOT),
					"--config-format", "codex", "--config", str(config_dir),
					"--runtime-dir", str(runtime), "--", "python3", "-c", probe,
					str(credential), str(control_root),
				],
				env=environment, capture_output=True, text=True, check=False, timeout=60,
			)
			if result.returncode != 0 and not require_smoke:
				pytest.skip(f"production systemd sandbox unavailable: {result.stderr.strip()}")
			assert result.returncode == 0, result.stderr
			payload = json.loads(result.stdout)
			assert payload["source_exists"] is False
			assert payload["copied_credentials"]
			assert payload["readable_credentials"] == []
			assert payload["gh_pat"] is None
			assert forwarded_output.read_text(encoding="utf-8") == "forwarded"
			validator_output = runtime / "validator-output"
			validator_output.mkdir()
			validator_result = validator_output / "result.txt"
			validator_run = subprocess.run(
				[
					"bash", str(SANDBOX), "--role", "validator", "--workspace", str(REPO_ROOT),
					"--runtime-dir", str(runtime), "--writable-output-dir", str(validator_output),
					"--", "/usr/bin/python3", "-I", "-S", "-c",
					"import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('validator-forwarded')",
					str(validator_result),
				],
				env=environment, capture_output=True, text=True, check=False, timeout=60,
			)
			assert validator_run.returncode == 0, validator_run.stderr
			assert validator_result.read_text(encoding="utf-8") == "validator-forwarded"
		assert set(control_root.glob("agent-sandbox.*")) == before_sandboxes


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
	assert "PrivateTmp=yes" in sandbox_text
	assert "summary|audit" in sandbox_text
	assert "implement-repair|diagnose|reviewer" in sandbox_text
	assert 'find "${workspace}" -xdev -name .git -print0' in sandbox_text
	assert 'InaccessiblePaths=${protected_git_path}' in sandbox_text
	assert 'if [ -n "${guard_git_dir}" ]; then' in sandbox_text
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
	for role in ("implement", "repair"):
		assert f"post-agent-{role}-" in implement
	assert implement.count("post_agent_workspace_guard.py\" snapshot") >= 2
	assert implement.count("post_agent_workspace_guard.py\" reconcile") >= 2

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

	commit_step = (REPO_ROOT / ".github/workflows/review_autofix.yml").read_text(encoding="utf-8").split(
		"      - name: Commit changes\n", 1
	)[1].split("\n      - name:", 1)[0]
	assert "GH_PAT: ${{ secrets.GH_PAT }}" not in commit_step
	assert "refusing retry or partial-work salvage" in implement
	assert implement.count('echo "POST_AGENT_WORKSPACE_GUARD_FAILED=true" >> "$GITHUB_ENV"') >= 4
	assert implement.count("env.POST_AGENT_WORKSPACE_GUARD_FAILED != 'true'") >= 12
	assert "Review-blocked fix workspace guard rejected" in (
		REPO_ROOT / "scripts" / "review_rb_judge.sh"
	).read_text(encoding="utf-8")
	assert (
		REPO_ROOT / "scripts" / "review_rb_judge.sh"
	).read_text(encoding="utf-8").count("| run_review_rb_validator_python -c '") >= 2
	assert "terminating the poller before output parsing" in (
		REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
	).read_text(encoding="utf-8")
	assert 'RB_JUDGE_JSON="$(run_poller_isolated_python "${PWD}" -c "' in (
		REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
	).read_text(encoding="utf-8")
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


def test_review_editor_workspace_guard_runtime_is_independent_of_legacy_tmp_runtime() -> None:
	review_apply = (REPO_ROOT / "scripts" / "review_apply_fixes.sh").read_text(encoding="utf-8")
	assert 'runtime_base="${RUNNER_TEMP}"' in review_apply
	assert 'mktemp -d "${runtime_base_resolved%/}/review-editor-workspace-guard.XXXXXX"' in review_apply
	assert 'trusted workspace-guard runtime resolved inside the model-writable workspace' in review_apply
	assert '--runtime-dir "${editor_workspace_guard_runtime}"' in review_apply
	assert '--runtime-dir "${RUNTIME_DIR}"' not in review_apply
	assert 'runtime_mode="$(stat -c \'%a\' "${editor_workspace_guard_runtime}"' in review_apply
	assert 'if [ "${runtime_mode}" != "700" ]; then' in review_apply


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
