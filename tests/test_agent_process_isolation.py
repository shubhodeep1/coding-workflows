#!/usr/bin/env python3
"""Security contracts for untrusted model-process isolation."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "scripts" / "untrusted_process_sandbox.sh"
PROXY = REPO_ROOT / "scripts" / "model_provider_proxy.py"
PACKAGE_PROXY = REPO_ROOT / "scripts" / "package_download_proxy.py"
TRUSTED_GIT_WRITE = REPO_ROOT / "scripts" / "trusted_git_write.sh"
CAUSALITY = REPO_ROOT / "scripts" / "security_audit_causality.py"


def test_sandbox_scrubs_runner_credentials_and_shell_command_files() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		credential = root / "provider-token"
		credential.write_text("synthetic-provider-token", encoding="utf-8")
		config_dir = root / "codex"
		config_dir.mkdir()
		(config_dir / "config.toml").write_text(
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
	assert 'find "${workspace}" -xdev -name .git -print0' in sandbox_text
	assert 'InaccessiblePaths=${protected_git_path}' in sandbox_text
	assert "GIT_DIR|GIT_WORK_TREE" not in sandbox_text


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
