#!/usr/bin/env python3
""".github/actions/install-claude: config_path resolution.

implement.yml's "Activate workspace shell context" step sets BASH_ENV to a
file that cds every later bash step into the materialized WORKSPACE_PATH copy,
and that copy has no .codex-workflow-src. The action's bash step read the
relative config_path from that cwd, missed it, and every IMPLEMENT run fell
back to codex with `AI_ENGINE_FALLBACK reason=cli_missing`. These tests run
the action's install step with a fake npm and claude under the same BASH_ENV
and check that a relative config_path is read from GITHUB_WORKSPACE.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTION = REPO_ROOT / ".github" / "actions" / "install-claude" / "action.yml"
CONFIG_RELATIVE = ".codex-workflow-src/.github/ai/claude_engine.json"
VERSION = "9.8.7"

FAKE_NPM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_NPM_LOG}"
"""

FAKE_CLAUDE = f"""#!/usr/bin/env bash
echo "{VERSION} (Claude Code)"
"""


def _install_step_script() -> str:
	data = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
	for step in data["runs"]["steps"]:
		if step.get("id") == "install":
			return step["run"]
	raise AssertionError("install-claude has no step with id: install")


def _run(tmp_path: Path, config_input: str) -> subprocess.CompletedProcess[str]:
	workspace = tmp_path / "work"
	config = workspace / CONFIG_RELATIVE
	config.parent.mkdir(parents=True)
	config.write_text(f'{{"cli_version": "{VERSION}"}}\n', encoding="utf-8")

	# The materialized copy implement.yml cds into: no .codex-workflow-src.
	materialized = tmp_path / "materialized"
	materialized.mkdir()
	bash_env = tmp_path / "workspace-shell.env"
	bash_env.write_text(f'cd "{materialized}"\n', encoding="utf-8")

	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	for name, body in (("npm", FAKE_NPM), ("claude", FAKE_CLAUDE)):
		path = bin_dir / name
		path.write_text(body, encoding="utf-8")
		path.chmod(0o755)

	script = tmp_path / "install.sh"
	script.write_text(_install_step_script(), encoding="utf-8")
	output = tmp_path / "github_output"
	output.touch()
	env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "WORKSPACE_PATH")}
	env.update({
		"PATH": f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}",
		"BASH_ENV": str(bash_env),
		"GITHUB_WORKSPACE": str(workspace),
		"GITHUB_OUTPUT": str(output),
		"CLAUDE_VERSION_INPUT": "",
		"CLAUDE_ENGINE_CONFIG_PATH": config_input,
		"FAKE_NPM_LOG": str(tmp_path / "npm.log"),
	})
	return subprocess.run(["bash", str(script)], cwd=workspace, env=env, capture_output=True, text=True, check=False)


def test_relative_config_path_resolves_against_github_workspace_under_bash_env(tmp_path):
	result = _run(tmp_path, CONFIG_RELATIVE)
	assert result.returncode == 0, result.stderr
	assert f"@anthropic-ai/claude-code@{VERSION}" in (tmp_path / "npm.log").read_text(encoding="utf-8")
	assert f"version={VERSION}" in (tmp_path / "github_output").read_text(encoding="utf-8")


def test_absolute_config_path_is_used_unchanged(tmp_path):
	result = _run(tmp_path, str(tmp_path / "work" / CONFIG_RELATIVE))
	assert result.returncode == 0, result.stderr
	assert f"version={VERSION}" in (tmp_path / "github_output").read_text(encoding="utf-8")


def test_missing_config_still_fails_with_the_resolved_path(tmp_path):
	result = _run(tmp_path, ".codex-workflow-src/.github/ai/absent.json")
	assert result.returncode == 2
	assert f"{tmp_path / 'work'}/.codex-workflow-src/.github/ai/absent.json is missing" in result.stderr
	assert not (tmp_path / "npm.log").exists()


def test_implement_passes_the_engine_config_to_both_claude_actions():
	workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "implement.yml").read_text(encoding="utf-8"))
	steps = workflow["jobs"]["implement"]["steps"]
	names = [step.get("name") for step in steps]
	activate = names.index("Activate workspace shell context")
	for name, uses in (
		("Install Claude Code CLI", "./.codex-workflow-src/.github/actions/install-claude"),
		("Resolve Claude credential", "./.codex-workflow-src/.github/actions/claude-pool-token"),
	):
		step = steps[names.index(name)]
		assert names.index(name) > activate, f"{name} runs before BASH_ENV is set; this test no longer covers it"
		assert step["uses"] == uses
		assert step["with"]["config_path"] == CONFIG_RELATIVE
