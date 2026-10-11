#!/usr/bin/env python3
"""Generated validation tests must run without the job's credentials.

Pins `run_test_without_credentials` in scripts/validate_driver.sh and its copy
in the runtime driver heredoc of scripts/validate_process.sh: GitHub tokens,
model/Telegram keys, GIT_CONFIG_* injection (which can carry the checkout's
http.extraheader) and runner command files never reach a test script, while
the driver's synthetic TEST_* fixtures still do.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER = REPO_ROOT / "scripts" / "validate_driver.sh"
PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"

SECRET_ENV = {
	"GH_TOKEN": "ghs_fixture_token",
	"GH_PAT": "ghp_fixture_pat",
	"GITHUB_TOKEN": "ghs_fixture_github_token",
	"OPENROUTER_API_KEY": "sk-or-fixture",
	"TG_BOT_SECRET": "tg-fixture",
	"CUSTOM_DEPLOY_TOKEN": "custom-fixture-token",
	"VALIDATION_LEAK_SECRET": "validation-prefixed-secret",
	"ACTIONS_RUNTIME_TOKEN": "actions-fixture",
	"GITHUB_ENV": "/tmp/fixture-github-env",
	"GIT_CONFIG_COUNT": "1",
	"GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
	"GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic fixture-extraheader",
}
KEPT_ENV = {
	"TEST_USERNAME": "validation-user",
	"TEST_API_KEY": "validation-api-key",
	"VALIDATION_TEST_API_KEY": "validation-api-key",
	"APP_SERVICE": "app",
}

PROBE_TEST = """#!/usr/bin/env bash
env
echo "--- git config ---"
git config --list 2>/dev/null || true
echo "ok 1 - probe"
"""


def _extract_function(text: str, name: str, indent: str) -> str:
	match = re.search(rf"^{indent}{name}\(\)\n{indent}\{{\n.*?^{indent}\}}\n", text, re.M | re.S)
	assert match is not None, f"{name} not found"
	return match.group(0)


@pytest.mark.parametrize(
	"source,indent",
	[(DRIVER, ""), (PROCESS, "")],
	ids=["validate_driver", "runtime_driver_heredoc"],
)
def test_test_scripts_never_see_credentials(source: Path, indent: str) -> None:
	function_text = _extract_function(source.read_text(encoding="utf-8"), "run_test_without_credentials", indent)
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		probe = root / "probe.sh"
		probe.write_text(PROBE_TEST, encoding="utf-8")
		home = root / "home"
		home.mkdir()
		# A global git config with a credential helper must be masked too.
		(home / ".gitconfig").write_text("[credential]\n\thelper = store\n", encoding="utf-8")
		env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), **SECRET_ENV, **KEPT_ENV}
		result = subprocess.run(
			["bash", "-c", function_text + 'run_test_without_credentials "$1"', "harness", str(probe)],
			cwd=root, env=env, capture_output=True, text=True, timeout=30,
		)
		assert result.returncode == 0, result.stderr
		output = result.stdout
		for value in SECRET_ENV.values():
			if len(value) > 3:  # skip GIT_CONFIG_COUNT=1, which is not distinctive
				assert value not in output
		for name in SECRET_ENV:
			assert not re.search(rf"^{name}=", output, re.M), name
		assert "extraheader" not in output
		assert "credential.helper" not in output
		for name, value in KEPT_ENV.items():
			assert f"{name}={value}" in output
		assert "GIT_CONFIG_GLOBAL=/dev/null" in output
		assert "ok 1 - probe" in output


def test_driver_and_runtime_driver_run_tests_through_the_scrub() -> None:
	driver = DRIVER.read_text(encoding="utf-8")
	assert 'run_test_without_credentials "${test_file}" > "${test_log}" 2>&1' in driver
	assert 'bash "${test_file}" > "${test_log}" 2>&1' not in driver
	process = PROCESS.read_text(encoding="utf-8")
	assert 'run_test_without_credentials "${test_script}" > "${test_log}" 2>&1' in process
	assert 'bash "${test_script}" > "${test_log}" 2>&1' not in process
