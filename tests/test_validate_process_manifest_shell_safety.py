#!/usr/bin/env python3
"""Manifest shell-safety gate in run_template_validation_harness_renderer.

Finding validation-manifest-shell-injection-fallback: values from the untrusted
`.ai/validate.yml` were rendered unescaped into validation test scripts that
validate_driver.sh and validate_process.sh's fallback runner execute on the
host. The gate in scripts/validate_process.sh rejects shell-unsafe values
before the renderer writes anything.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
FAMILIES = (
	"python-mongo-flask",
	"python-repo-checks",
	"python-mongo-repo-checks",
	"node-runtime",
	"node-hardhat-solidity",
)


def _renderer_function_text() -> str:
	process_text = VALIDATE_PROCESS.read_text(encoding="utf-8")
	body = process_text.split("run_template_validation_harness_renderer()\n{", 1)[1].split("\n}\n", 1)[0]
	return "run_template_validation_harness_renderer()\n{" + body + "\n}\n"


def _manifest(family: str = "python-mongo-flask") -> dict:
	return {
		"type": family,
		"entry": "app.py",
		"port": 8000,
		"slots": {
			"project_name": "demo-project",
			"canary_tools": ["curl", "jq", "python3"],
			"tap_plan": 2,
		},
	}


def _run_gate(root: Path, manifest_text: str) -> subprocess.CompletedProcess[str]:
	"""Run the real function with a shim renderer interpreter.

	The shim passes the dependency probe, records a renderer invocation as a
	marker file, and hands the shell-safety gate to the real interpreter.
	"""
	runtime_dir = root / "runtime"
	(runtime_dir / "renderer-empty").mkdir(parents=True)
	(runtime_dir / "renderer-venv" / "bin").mkdir(parents=True)
	for asset in (
		"scripts/render_validation_templates.py",
		"scripts/templates/slot_manifest.schema.json",
		"workflow-templates/validation-harness/_shared/_lib/tap_helpers.sh.j2",
		"workflow-templates/validation-harness/_shared/tests/00_canary.sh.j2",
		"workflow-templates/validation-harness/_shared/tests/90_tap_report.sh.j2",
	):
		asset_path = root / asset
		asset_path.parent.mkdir(parents=True, exist_ok=True)
		asset_path.touch()
	manifest_path = root / ".ai" / "validate.yml"
	manifest_path.parent.mkdir(parents=True, exist_ok=True)
	manifest_path.write_text(manifest_text, encoding="utf-8")
	shim = runtime_dir / "renderer-venv" / "bin" / "python"
	shim.write_text(
		"#!/bin/sh\n"
		"if [ \"$1\" = '-I' ]; then shift; fi\n"
		"if [ \"$1\" = '-c' ]; then\n"
		"  case \"$2\" in\n"
		"    *'import importlib.util'*) exit 0;;\n"
		"  esac\n"
		"fi\n"
		"case \"$1\" in\n"
		f"  */scripts/render_validation_templates.py) touch {root}/renderer-invoked; exit 0;;\n"
		"esac\n"
		"exec \"$REAL_PYTHON3\" \"$@\"\n",
		encoding="utf-8",
	)
	shim.chmod(0o755)
	env = os.environ.copy()
	env.update({
		"RUNTIME_DIR": str(runtime_dir),
		"REAL_PYTHON3": sys.executable,
		"GENERATE_LOG_FILE": str(root / "renderer.log"),
		"VALIDATION_RENDERER_DEPENDENCIES_READY": "true",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	env.pop("BASH_ENV", None)
	(root / "renderer.log").touch()
	result = subprocess.run(
		["bash", "--noprofile", "--norc", "-c", _renderer_function_text() + "\nrun_template_validation_harness_renderer"],
		cwd=root, env=env, capture_output=True, text=True, timeout=60,
	)
	return result


def _assert_rejected(root: Path, manifest: dict | str, *, pointer: str, char_class: str, payload: str | None = None) -> None:
	text = manifest if isinstance(manifest, str) else yaml.safe_dump(manifest, sort_keys=False)
	result = _run_gate(root, text)
	log_text = (root / "renderer.log").read_text(encoding="utf-8")
	assert result.returncode == 14, result.stdout + result.stderr + log_text
	assert not (root / "renderer-invoked").exists()
	assert pointer in log_text, log_text
	assert char_class in log_text, log_text
	assert "Template manifest shell-safety check failed; renderer not invoked." in log_text
	assert "shell-unsafe values in shell-reachable fields" in result.stderr
	if payload is not None:
		assert payload not in log_text
	assert not (root / "pwned").exists()


def _assert_accepted(root: Path, manifest: dict | str) -> None:
	text = manifest if isinstance(manifest, str) else yaml.safe_dump(manifest, sort_keys=False)
	result = _run_gate(root, text)
	log_text = (root / "renderer.log").read_text(encoding="utf-8")
	assert result.returncode == 0, result.stdout + result.stderr + log_text
	assert (root / "renderer-invoked").exists()


@pytest.mark.parametrize("family", FAMILIES)
def test_project_name_command_substitution_rejected_for_every_family(tmp_path: Path, family: str) -> None:
	manifest = _manifest(family)
	manifest["slots"]["project_name"] = "$(touch pwned)"
	_assert_rejected(tmp_path, manifest, pointer="/slots/project_name", char_class="dollar", payload="touch pwned")


def test_canary_tools_backtick_rejected(tmp_path: Path) -> None:
	manifest = _manifest("node-hardhat-solidity")
	manifest["slots"]["canary_tools"] = ["node`id`"]
	_assert_rejected(tmp_path, manifest, pointer="/slots/canary_tools/0", char_class="backtick", payload="node`id`")


def test_extra_slot_double_quote_rejected(tmp_path: Path) -> None:
	manifest = _manifest("python-mongo-flask")
	manifest["slots"]["health_path"] = '/health"; touch pwned; "'
	_assert_rejected(tmp_path, manifest, pointer="/slots/health_path", char_class="quote")


def test_top_level_entry_rejected(tmp_path: Path) -> None:
	manifest = _manifest("python-repo-checks")
	manifest["entry"] = "scripts/run.sh $(touch pwned)"
	_assert_rejected(tmp_path, manifest, pointer="/entry", char_class="dollar")


def test_extra_top_level_newline_and_backslash_rejected(tmp_path: Path) -> None:
	manifest = _manifest("python-mongo-flask")
	manifest["requirements_file"] = "requirements.txt\ntouch pwned"
	manifest["mongo_db_name"] = "db\\name"
	result = _run_gate(tmp_path, yaml.safe_dump(manifest, sort_keys=False))
	log_text = (tmp_path / "renderer.log").read_text(encoding="utf-8")
	assert result.returncode == 14
	assert "/requirements_file: contains shell-unsafe character class control" in log_text
	assert "/mongo_db_name: contains shell-unsafe character class backslash" in log_text
	assert not (tmp_path / "renderer-invoked").exists()


def test_binary_value_rejected_as_unsupported_type(tmp_path: Path) -> None:
	text = yaml.safe_dump(_manifest(), sort_keys=False) + "requirements_file: !!binary JCh0b3VjaCBwd25lZCk=\n"
	_assert_rejected(tmp_path, text, pointer="/requirements_file", char_class="unsupported value type bytes")


def test_hostile_slot_key_is_sanitized_in_log(tmp_path: Path) -> None:
	manifest = _manifest()
	manifest["slots"]["x$(id)`y`"] = "$(touch pwned)"
	result = _run_gate(tmp_path, yaml.safe_dump(manifest, sort_keys=False))
	log_text = (tmp_path / "renderer.log").read_text(encoding="utf-8")
	assert result.returncode == 14
	assert "/slots/x?(id)?y?: contains shell-unsafe character class dollar" in log_text
	assert "$(id)" not in log_text


def test_exempt_keys_and_plain_values_pass(tmp_path: Path) -> None:
	manifest = _manifest("node-runtime")
	manifest["slots"]["project_name"] = "demo project.v2"
	manifest["entry"] = "npm test -- --grep=a:b@c+d?e&f=g"
	manifest["custom_tests"] = ["npm test && echo $HOME"]
	manifest["skip_tests"] = ["echo `date`"]
	manifest["env_overrides"] = {"SECRET_KEY": "a$b\"c"}
	manifest["health_check"] = "curl -f \"http://app:8000/health\""
	manifest["services"] = ["mongo:7"]
	manifest["port"] = "8000"
	_assert_accepted(tmp_path, manifest)


def test_unparseable_manifest_is_left_to_renderer(tmp_path: Path) -> None:
	# The renderer rejects parse errors with line/column detail; the gate must
	# not mask them, and the renderer still runs (and fails) on such input.
	_assert_accepted(tmp_path, "type: [unclosed\n")


def test_gate_runs_before_renderer_in_function() -> None:
	function_text = _renderer_function_text()
	gate_index = function_text.index("Manifest shell-safety gate")
	probe_index = function_text.index("import yaml, jsonschema, jinja2")
	renderer_index = function_text.index('"${renderer_python}" -I "${renderer_workspace}/${renderer_script}"')
	assert probe_index < gate_index < renderer_index
	assert "import importlib.util" not in function_text[gate_index:renderer_index]


def test_recursive_alias_terminates_and_is_checked(tmp_path: Path) -> None:
	# yaml.safe_load builds a cyclic dict from a recursive alias; the gate must
	# still terminate and check the values inside it.
	text = yaml.safe_dump(_manifest(), sort_keys=False) + "extra: &loop\n  name: \"$(touch pwned)\"\n  self: *loop\n"
	_assert_rejected(tmp_path, text, pointer="/extra/name", char_class="dollar", payload="touch pwned")


def test_recursive_alias_with_safe_values_is_accepted(tmp_path: Path) -> None:
	text = yaml.safe_dump(_manifest(), sort_keys=False) + "extra: &loop\n  name: plain\n  self: *loop\n"
	_assert_accepted(tmp_path, text)


def _load_refresh_runner():
	import importlib.util

	module_path = REPO_ROOT / "scripts" / "validation_refresh_runner.py"
	spec = importlib.util.spec_from_file_location("validation_refresh_runner_shell_safety", module_path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def test_refresh_runner_gate_matches_validate_process_gate(tmp_path: Path) -> None:
	runner = _load_refresh_runner()
	manifest_path = tmp_path / "validate.yml"
	manifest = _manifest("python-mongo-flask")
	manifest["slots"]["project_name"] = "$(touch pwned)"
	manifest["requirements_file"] = "requirements.txt\ntouch pwned"
	manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
	violations = runner.manifest_shell_safety_violations(manifest_path)
	assert "/slots/project_name: contains shell-unsafe character class dollar" in violations
	assert "/requirements_file: contains shell-unsafe character class control" in violations
	assert not any("touch pwned" in line for line in violations)

	safe = _manifest("node-runtime")
	safe["custom_tests"] = ["npm test && echo $HOME"]
	safe["env_overrides"] = {"SECRET_KEY": "a$b\"c"}
	manifest_path.write_text(yaml.safe_dump(safe, sort_keys=False), encoding="utf-8")
	assert runner.manifest_shell_safety_violations(manifest_path) == []

	manifest_path.write_text("slots: &loop\n  project_name: demo\n  self: *loop\n", encoding="utf-8")
	assert runner.manifest_shell_safety_violations(manifest_path) == []

	assert runner.manifest_shell_safety_violations(tmp_path / "missing.yml") == ["$: manifest unreadable (FileNotFoundError)"]


def test_refresh_pipeline_refuses_unsafe_manifest_before_render(tmp_path: Path) -> None:
	runner_module = _load_refresh_runner()

	class RecordingExecutor:
		def __init__(self) -> None:
			self.seen: list[list[str]] = []

		def run(self, command, **_kwargs):
			self.seen.append(list(command))
			raise AssertionError("no command may run for an unsafe manifest")

	repo_dir = tmp_path / "octo__demo"
	(repo_dir / ".ai").mkdir(parents=True)
	manifest_path = repo_dir / ".ai" / "validate.yml"
	manifest = _manifest()
	manifest["slots"]["project_name"] = "$(touch pwned)"
	manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
	executor = RecordingExecutor()
	runner = runner_module.ValidationRefreshRunner.__new__(runner_module.ValidationRefreshRunner)
	runner.source_root = REPO_ROOT
	runner.executor = executor
	green, diagnostics = runner._run_refresh_pipeline(repo_dir, manifest_path)
	assert green is False
	assert executor.seen == []
	assert diagnostics == [
		"manifest_shell_safety_failed: /slots/project_name: contains shell-unsafe character class dollar"
	]


def test_deeply_nested_manifest_reports_clean_violation(tmp_path: Path) -> None:
	# yaml.safe_load recurses per nesting level; a deep flow sequence exceeds
	# Python's recursion limit before the walk's depth check runs. Both gate
	# copies must report a clean violation instead of a traceback.
	text = "slots:\n  project_name: " + "[" * 5000 + "]" * 5000 + "\n"
	result = _run_gate(tmp_path, text)
	log_text = (tmp_path / "renderer.log").read_text(encoding="utf-8")
	assert result.returncode == 14, result.stdout + result.stderr + log_text
	assert "- $: manifest nesting too deep" in log_text
	assert "Traceback" not in log_text
	assert not (tmp_path / "renderer-invoked").exists()

	runner = _load_refresh_runner()
	manifest_path = tmp_path / "deep.yml"
	manifest_path.write_text(text, encoding="utf-8")
	assert runner.manifest_shell_safety_violations(manifest_path) == ["$: manifest nesting too deep"]


def test_gate_copies_share_constants_and_report_identical_violations(tmp_path: Path) -> None:
	# The gate exists twice (validate_process.sh's embedded Python and
	# validation_refresh_runner.manifest_shell_safety_violations); this keeps
	# the exempt keys, limits and per-class reporting of the two in step.
	import ast
	import re

	runner = _load_refresh_runner()
	function_text = _renderer_function_text()
	exempt_match = re.search(r"^EXEMPT_TOP_LEVEL_KEYS = frozenset\((\{.*\})\)$", function_text, re.MULTILINE)
	assert exempt_match is not None
	assert frozenset(ast.literal_eval(exempt_match.group(1))) == runner.MANIFEST_SHELL_SAFETY_EXEMPT_KEYS
	assert re.search(r"^MAX_BYTES = 2 \* 1024 \* 1024$", function_text, re.MULTILINE)
	assert runner.MANIFEST_SHELL_SAFETY_MAX_BYTES == 2 * 1024 * 1024
	depth_match = re.search(r"^MAX_DEPTH = (\d+)$", function_text, re.MULTILINE)
	assert depth_match is not None
	assert int(depth_match.group(1)) == runner.MANIFEST_SHELL_SAFETY_MAX_DEPTH

	manifest = _manifest()
	manifest["slots"]["a_dollar"] = "x$y"
	manifest["slots"]["b_backtick"] = "x`y"
	manifest["slots"]["c_quote"] = "x'y"
	manifest["slots"]["d_backslash"] = "x\\y"
	manifest["slots"]["e_control"] = "x\ty"
	manifest["slots"]["f_all"] = "$`\"\\\n"
	text = yaml.safe_dump(manifest, sort_keys=False)
	result = _run_gate(tmp_path, text)
	assert result.returncode == 14
	log_lines = (tmp_path / "renderer.log").read_text(encoding="utf-8").splitlines()
	shell_violations = [line[2:] for line in log_lines if line.startswith("- ")]
	manifest_path = tmp_path / "parity.yml"
	manifest_path.write_text(text, encoding="utf-8")
	assert shell_violations == runner.manifest_shell_safety_violations(manifest_path)
	assert "/slots/f_all: contains shell-unsafe character class dollar,backtick,quote,backslash,control" in shell_violations
