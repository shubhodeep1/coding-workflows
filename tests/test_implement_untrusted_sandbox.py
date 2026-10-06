"""Implement editor container boundary and workspace transfer contracts."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/implement_untrusted_sandbox.sh"
WORKSPACE = ROOT / "scripts/review_untrusted_workspace.py"
WORKFLOW = ROOT / ".github/workflows/implement.yml"


def test_implement_workspace_resync_and_protected_transfer(tmp_path: Path) -> None:
	host = tmp_path / "host"
	(host / "src").mkdir(parents=True)
	(host / "src/app.kt").write_text("one\n")
	(host / "src/other.kt").write_text("other\n")
	(host / "src/.env").write_text("not copied\n")
	(host / ".env.example").write_text("ROOT_EXAMPLE=one\n")
	(host / "src/.env.example").write_text("NESTED_EXAMPLE=one\n")
	(host / "src/.env.production").write_text("not copied\n")
	(host / "secrets").mkdir()
	(host / "secrets/private.kt").write_text("not copied\n")
	(host / "secrets/.env.example").write_text("not copied\n")
	(host / ".claude/commands").mkdir(parents=True)
	(host / ".claude/.env.example").write_text("not copied\n")
	(host / ".claude/commands/.env.example").write_text("not copied\n")
	(host / ".claude/hooks").mkdir(parents=True)
	(host / ".claude/hooks/.env.example").write_text("not copied\n")
	(host / ".github/ai").mkdir(parents=True)
	(host / ".github/ai/.env.example").write_text("not copied\n")
	subprocess.run(["git", "init", "-q", str(host)], check=True)
	subprocess.run(["git", "add", "src", "secrets", ".env.example", ".claude", ".github"], cwd=host, check=True)
	workspace = tmp_path / "source"
	workspace.mkdir()
	manifest = tmp_path / "baseline.json"
	env = {**os.environ, "UNTRUSTED_WORKSPACE_PROFILE": "implement", "PYTHONDONTWRITEBYTECODE": "1"}
	for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
		env.pop(key, None)

	def run(action: str, *extra: str) -> subprocess.CompletedProcess[str]:
		return subprocess.run([sys.executable, str(WORKSPACE), action, str(host), str(workspace), str(manifest), *extra], env=env, text=True, capture_output=True, check=False)

	assert run("snapshot").returncode == 0
	assert (workspace / "src/app.kt").read_text() == "one\n"
	assert (workspace / ".env.example").read_text() == "ROOT_EXAMPLE=one\n"
	assert (workspace / "src/.env.example").read_text() == "NESTED_EXAMPLE=one\n"
	assert not (workspace / "src/.env").exists()
	assert not (workspace / "src/.env.production").exists()
	assert not (workspace / "secrets/private.kt").exists()
	assert not (workspace / "secrets/.env.example").exists()
	for restricted_path in (".claude/.env.example", ".claude/commands/.env.example", ".claude/hooks/.env.example", ".github/ai/.env.example"):
		assert not (workspace / restricted_path).exists()
	(host / "src/app.kt").write_text("host changed\n")
	(host / ".env.example").write_text("ROOT_EXAMPLE=host\n")
	(host / "src/other.kt").unlink()
	(workspace / "src/.review-venv").mkdir()
	(workspace / "src/.review-venv/preserved").write_text("build data")
	assert run("resync").returncode == 0
	assert (workspace / "src/app.kt").read_text() == "host changed\n"
	assert (workspace / ".env.example").read_text() == "ROOT_EXAMPLE=host\n"
	assert not (workspace / "src/other.kt").exists()
	assert (workspace / "src/.review-venv/preserved").read_text() == "build data"
	(workspace / "src/app.kt").write_text("isolated edit\n")
	(workspace / ".env.example").write_text("ROOT_EXAMPLE=isolated\n")
	(workspace / "src/.env.example").write_text("NESTED_EXAMPLE=isolated\n")
	for restricted_path in (".claude/.env.example", ".claude/commands/.env.example", ".claude/hooks/.env.example", ".github/ai/.env.example"):
		(workspace / restricted_path).parent.mkdir(parents=True, exist_ok=True)
		(workspace / restricted_path).write_text("isolated edit\n")
	protected = tmp_path / "protected.txt"
	protected.write_text("src/app.kt\n")
	result = run("transfer", str(protected))
	assert result.returncode == 0, result.stderr
	assert "dropped_protected count=1" in result.stderr
	assert (host / "src/app.kt").read_text() == "host changed\n"
	assert (host / ".env.example").read_text() == "ROOT_EXAMPLE=isolated\n"
	assert (host / "src/.env.example").read_text() == "NESTED_EXAMPLE=isolated\n"
	for restricted_path in (".claude/.env.example", ".claude/commands/.env.example", ".claude/hooks/.env.example", ".github/ai/.env.example"):
		assert (host / restricted_path).read_text() == "not copied\n"
	assert run("resync").returncode == 0
	assert (workspace / "src/app.kt").read_text() == "host changed\n"


def test_runner_preflight_fails_closed(tmp_path: Path) -> None:
	env = {**os.environ, "RUNTIME_DIR": str(tmp_path), "RUNNER_TEMP": str(tmp_path), "IMPLEMENT_SANDBOX_SUPPORT_DIR": str(tmp_path / "missing")}
	result = subprocess.run(["bash", str(RUNNER), "prepare"], env=env, text=True, capture_output=True, check=False)
	assert result.returncode == 1
	assert "::error::Implement isolation" in result.stderr
	assert not (tmp_path / "github_env").exists()


def test_workflow_isolates_both_launches_and_cleans_up() -> None:
	text = WORKFLOW.read_text()
	assert text.index("name: Prepare implement editor isolation") < text.index("name: Run Codex implementation")
	assert text.count('CODEX_THREAD_REUSE_REAL_CODEX="${IMPLEMENT_SANDBOX_ROOT}/bin/codex"') == 2
	assert text.count('CODEX_THREAD_REUSE_CLAUDE_RUNNER="${IMPLEMENT_SANDBOX_SUPPORT_DIR}/scripts/implement_untrusted_sandbox.sh"') == 2
	assert text.count('CODEX_THREAD_REUSE_CLAUDE_HOME="${IMPLEMENT_SANDBOX_ROOT}/home"') == 2
	assert text.count('bash "${IMPLEMENT_SANDBOX_SUPPORT_DIR}/scripts/codex_thread_reuse.sh" direct-run') == 2
	assert text.count('[ -f "${RUNTIME_DIR}/implement_sandbox_transfer_failed" ]') >= 2
	assert "if: always() && env.IMPLEMENT_SANDBOX_ROOT != ''" in text
	assert "bash scripts/codex_thread_reuse.sh direct-run" not in text
	assert "--network none" in RUNNER.read_text()
	assert "--read-only --cap-drop ALL --security-opt no-new-privileges" in RUNNER.read_text()
	assert "--pids-limit 512" in RUNNER.read_text()


def test_codex_container_receives_only_broker_placeholder(tmp_path: Path) -> None:
	checkout = tmp_path / "checkout"
	checkout.mkdir()
	(checkout / "README.md").write_text("source\n")
	subprocess.run(["git", "init", "-q", str(checkout)], check=True)
	subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True)
	support = tmp_path / "support"
	(support / "scripts/review_sandbox").mkdir(parents=True)
	for name in ("implement_untrusted_sandbox.sh", "review_untrusted_workspace.py", "clarify_openrouter_broker.py"):
		shutil.copyfile(ROOT / "scripts" / name, support / "scripts" / name)
	(support / "scripts/claude_anthropic_relay.py").write_text("")
	(support / "scripts/review_sandbox/Dockerfile").write_text("FROM scratch\n")
	(support / "scripts/codex_model_catalog.json").write_text("{}")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log = tmp_path / "docker.jsonl"
	docker = bin_dir / "docker"
	docker.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(LOG_PLACEHOLDER, "a", encoding="utf-8") as stream:
    print(json.dumps({"args": sys.argv[1:], "env": dict(os.environ)}), file=stream)
if sys.argv[1] == "build":
    print("sha256:" + "a" * 64)
'''.replace("LOG_PLACEHOLDER", repr(str(log))))
	docker.chmod(0o755)
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	config = tmp_path / "codex-home/.codex"
	config.mkdir(parents=True)
	(config / "config.toml").write_text(
		'model_catalog_json = "/trusted/catalog.json"\n'
		'[model_providers.openrouter]\nbase_url = "https://openrouter.ai/api/v1"\n'
		'env_key = "OPENROUTER_API_KEY"\n[sandbox_workspace_write]\nnetwork_access = true\n'
		f'[projects."{checkout}"]\ntrust_level = "trusted"\n'
		'[mcp_servers.serena]\ncommand = "/some/host/path"\n',
	)
	github_env = tmp_path / "github-env"
	github_env.write_text("")
	env = {**os.environ, "PATH": str(bin_dir) + ":" + os.environ.get("PATH", ""),
		"RUNNER_TEMP": str(tmp_path.parent), "RUNTIME_DIR": str(runtime), "GITHUB_ENV": str(github_env),
		"GITHUB_WORKSPACE": str(checkout), "IMPLEMENT_SANDBOX_SUPPORT_DIR": str(support),
		"CODEX_HOME": str(config), "OPENROUTER_API_KEY": "dummy-test-key", "PYTHONDONTWRITEBYTECODE": "1"}
	for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "BASH_ENV", "WORKSPACE_PATH"):
		env.pop(key, None)
	prepare = subprocess.run(["bash", str(RUNNER), "prepare"], env=env, cwd=checkout, text=True, capture_output=True, check=False)
	assert prepare.returncode == 0, prepare.stderr
	root = github_env.read_text().split("IMPLEMENT_SANDBOX_ROOT=", 1)[1].strip()
	env["IMPLEMENT_SANDBOX_ROOT"] = root
	result = subprocess.run(["bash", str(RUNNER), "codex", "--ask-for-approval", "never", "exec", "--model", "openai/gpt-6-sol"],
		input="prompt\n", env=env, cwd=checkout, text=True, capture_output=True, check=False)
	assert result.returncode == 0, result.stderr
	assert 'base_url = "http://127.0.0.1:8765/api/v1"' in (Path(root) / "home/.codex/config.toml").read_text()
	assert '[projects."/source"]' in (Path(root) / "home/.codex/config.toml").read_text()
	assert "network_access = false" in (Path(root) / "home/.codex/config.toml").read_text()
	assert "mcp_servers" not in (Path(root) / "home/.codex/config.toml").read_text()
	assert not (runtime / "implement_sandbox_transfer_failed").exists()
	claude_prompt = tmp_path / "prompt"
	claude_prompt.write_text("edit source\n")
	unavailable = subprocess.run(["bash", str(RUNNER), "claude", "IMPLEMENT", str(claude_prompt), str(tmp_path / "output"), str(checkout), ""],
		env=env, cwd=checkout, text=True, capture_output=True, check=False)
	assert unavailable.returncode == 75
	assert "AI_ENGINE_FALLBACK role=IMPLEMENT reason=sandbox_not_prepared" in unavailable.stderr
	calls = [json.loads(line) for line in log.read_text().splitlines()]
	runs = [call for call in calls if call["args"][0] == "run"]
	assert len(runs) == 2
	for call in runs:
		assert "OPENROUTER_API_KEY" not in call["env"]
		assert "GH_PAT" not in call["env"]
		assert "ACTIONS_RUNTIME_TOKEN" not in call["env"]
	assert "--network" in runs[1]["args"] and "none" in runs[1]["args"]
	assert "--read-only" in runs[1]["args"] and "--pids-limit" in runs[1]["args"]
	assert all("/var/run/docker.sock" not in item and "/.git" not in item for item in runs[1]["args"])
	assert all(not any(secret in item for secret in ("GH_PAT", "GH_TOKEN", "GITHUB_TOKEN", "OPENROUTER_API_KEY", "ACTIONS_RUNTIME_TOKEN", "TG_BOT_SECRET")) for item in runs[1]["args"])
	assert all(str(github_env) not in item and str(config.parent) not in item for item in runs[1]["args"] if item.startswith("type=bind"))
	(config / "config.toml").write_text((config / "config.toml").read_text().replace("https://openrouter.ai/api/v1", "https://untrusted.example/v1"))
	rejected = subprocess.run(["bash", str(RUNNER), "codex", "exec", "--model", "openai/gpt-6-sol"],
		input="prompt\n", env=env, cwd=checkout, text=True, capture_output=True, check=False)
	assert rejected.returncode == 1 and "config_rejected" in rejected.stderr
	assert (runtime / "implement_sandbox_failed").exists()
	subprocess.run(["bash", str(RUNNER), "cleanup"], env=env, cwd=checkout, check=True, capture_output=True, text=True)
