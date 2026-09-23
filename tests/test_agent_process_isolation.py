#!/usr/bin/env python3
"""Security contracts for untrusted model-process isolation."""

from __future__ import annotations

import importlib.util
import http.client
import json
import os
import runpy
import shutil
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
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
POLLER_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"


def _production_sandbox_environment() -> tuple[Path, dict[str, str]]:
	runner_temp_value = os.environ.get("RUNNER_TEMP", "")
	if os.environ.get("CI", "").lower() != "true":
		pytest.skip("production systemd sandbox coverage runs on the ubuntu-latest CI runner")
	assert runner_temp_value, "CI must provide RUNNER_TEMP for production sandbox coverage"
	runner_temp = Path(runner_temp_value).resolve(strict=True)
	assert runner_temp != Path("/tmp") and not runner_temp.is_relative_to(Path("/tmp"))
	assert shutil.which("systemd-run"), "CI runner must provide systemd-run"
	assert Path("/run/systemd/system").is_dir(), "CI runner must boot systemd"
	environment = os.environ.copy()
	environment.pop("UNTRUSTED_PROCESS_SANDBOX_TEST_MODE", None)
	return runner_temp, environment


def test_workspace_guard_runs_snapshot_and_reconcile_in_production_systemd_sandbox() -> None:
	runner_temp, environment = _production_sandbox_environment()
	with tempfile.TemporaryDirectory(dir=runner_temp, prefix="workspace-guard-systemd-") as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		manifest = runtime / "manifest.json"
		base_command = [
			"bash", str(SANDBOX), "--role", "workspace-guard", "--workspace", str(workspace),
			"--runtime-dir", str(runtime), "--", "/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD),
		]
		snapshot = subprocess.run(
			[*base_command, "snapshot", "--workspace", str(workspace), "--manifest", str(manifest)],
			env=environment, capture_output=True, text=True, check=False,
		)
		assert snapshot.returncode == 0, snapshot.stderr
		(workspace / "new-source.txt").write_text("safe\n", encoding="utf-8")
		changed = runtime / "changed.txt"
		report = runtime / "report.json"
		reconcile = subprocess.run(
			[
				*base_command, "reconcile", "--workspace", str(workspace), "--manifest", str(manifest),
				"--quarantine-dir", str(runtime / "quarantine"), "--changed-paths-out", str(changed),
				"--report", str(report),
			],
			env=environment, capture_output=True, text=True, check=False,
		)
		assert reconcile.returncode == 0, reconcile.stderr
		assert changed.read_text(encoding="utf-8").splitlines() == ["new-source.txt"]
		assert json.loads(report.read_text(encoding="utf-8"))["restored"] == ["new-source.txt"]


def test_workspace_guard_rejects_private_tmp_masked_runtime_before_systemd_start() -> None:
	runner_temp, environment = _production_sandbox_environment()
	with tempfile.TemporaryDirectory(dir=runner_temp, prefix="workspace-guard-workspace-") as workspace_directory:
		workspace = Path(workspace_directory)
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		with tempfile.TemporaryDirectory(dir="/tmp", prefix="workspace-guard-masked-") as runtime_directory:
			runtime = Path(runtime_directory)
			result = subprocess.run(
				[
					"bash", str(SANDBOX), "--role", "workspace-guard", "--workspace", str(workspace),
					"--runtime-dir", str(runtime), "--", "/usr/bin/python3", "-I", "-S", str(WORKSPACE_GUARD),
					"snapshot", "--workspace", str(workspace), "--manifest", str(runtime / "manifest.json"),
				],
				env=environment, capture_output=True, text=True, check=False,
			)
			assert result.returncode != 0
			assert "PrivateTmp masks workspace-guard runtime path" in result.stderr
			assert not (runtime / "manifest.json").exists()


def test_workspace_guard_workflow_runtimes_are_rooted_below_runner_temp() -> None:
	workflow_contracts = {
		REVIEW_WORKFLOW: "${RUNNER_TEMP%/}/codex-pr-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${RANDOM}",
		IMPLEMENT_WORKFLOW: "${RUNNER_TEMP%/}/codex-implement-${GITHUB_RUN_ID}",
		POLLER_WORKFLOW: "${RUNNER_TEMP%/}/codex-orchestrate-poll-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}",
	}
	for workflow_path, expected_runtime in workflow_contracts.items():
		workflow_text = workflow_path.read_text(encoding="utf-8")
		assert expected_runtime in workflow_text, workflow_path
		assert f'/tmp/{expected_runtime.split("/", 1)[1]}' not in workflow_text, workflow_path
	implement_text = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	assert '[[ "${runtime_name}" =~ ^codex-implement-[0-9]+$ ]]' in implement_text
	assert '[ "$(dirname -- "${RUNTIME_DIR:-/}")" = "${runtime_parent%/}" ]' in implement_text


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


def test_validator_role_uses_isolated_no_site_python_outside_workspace() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		workspace = root / "workspace"
		runtime = root / "runtime"
		workspace.mkdir()
		runtime.mkdir()
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
				"--runtime-dir", str(runtime), "--", "/usr/bin/python3", "-I", "-S", "-c",
				"import json,os,sys; print(json.dumps([sys.flags.isolated,sys.flags.no_site,os.getcwd(),os.getenv('GH_PAT')]))",
			],
			env=environment, capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		isolated, no_site, cwd, credential = json.loads(result.stdout)
		assert (isolated, no_site, credential) == (1, 1, None)
		assert Path(cwd) != workspace
		assert not sentinel.exists()


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
	assert "GIT_DIR|GIT_WORK_TREE" not in sandbox_text
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
