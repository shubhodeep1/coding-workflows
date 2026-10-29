#!/usr/bin/env python3
"""Security regressions: manifest values must never execute in generated shell tests.

Covers the `slots.project_name` command-substitution finding
(`validation-project-name-shell-injection`): the renderer rejects shell-active
values independent of the schema a caller passes, and every manifest value a
generated shell test interpolates renders as one literal shell word.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_validation_templates.py"
SCHEMA_PATH = REPO_ROOT / "scripts" / "templates" / "slot_manifest.schema.json"
TEMPLATES_ROOT = REPO_ROOT / "workflow-templates" / "validation-harness"
FAMILIES = (
	"python-mongo-flask",
	"python-repo-checks",
	"node-hardhat-solidity",
	"node-runtime",
	"python-mongo-repo-checks",
)
HOSTILE_PROJECT_NAMES = (
	"x$(touch {sentinel})",
	"x`touch {sentinel}`",
	"a;touch {sentinel}",
	"a\ntouch {sentinel}",
	"a'b",
	'a"b',
	"a&&b",
	"$HOME",
	"",
	"-leading-dash",
)


def _load_renderer():
	spec = importlib.util.spec_from_file_location("render_validation_templates_under_test", SCRIPT_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


RENDERER = _load_renderer()


def _manifest(manifest_type: str = "python-mongo-flask", **slot_overrides) -> dict:
	slots = {"project_name": "demo-project", "canary_tools": ["curl", "jq", "python3"], "tap_plan": 2}
	slots.update(slot_overrides)
	return {"type": manifest_type, "entry": "app.py", "port": 8000, "slots": slots}


@pytest.mark.parametrize("raw_name", HOSTILE_PROJECT_NAMES)
def test_gate_rejects_shell_active_project_name(raw_name: str) -> None:
	manifest = _manifest(project_name=raw_name.format(sentinel="/tmp/pwned"))
	with pytest.raises(RENDERER.ManifestSafetyError) as excinfo:
		RENDERER.enforce_shell_safe_manifest(manifest)
	message = str(excinfo.value)
	assert "/slots/project_name" in message
	assert "touch" not in message  # the raw value is never echoed


@pytest.mark.parametrize("name", ["demo-project", "my app", "org/repo", "a.b_c@d+e:f", "x" * 128, "A1"])
def test_gate_accepts_plain_project_names(name: str) -> None:
	RENDERER.enforce_shell_safe_manifest(_manifest(project_name=name))


def test_gate_rejects_overlong_project_name_and_non_string() -> None:
	for value in ("x" * 129, 123, None, ["a"]):
		with pytest.raises(RENDERER.ManifestSafetyError):
			RENDERER.enforce_shell_safe_manifest(_manifest(project_name=value))


@pytest.mark.parametrize("tool", ["tool$(id)", "a b", "x;y", "`id`", "", "-flag", 5])
def test_gate_rejects_shell_active_canary_tools(tool) -> None:
	with pytest.raises(RENDERER.ManifestSafetyError) as excinfo:
		RENDERER.enforce_shell_safe_manifest(_manifest(canary_tools=["curl", tool]))
	assert "/slots/canary_tools/1" in str(excinfo.value)


@pytest.mark.parametrize("tap_plan", [True, 0, -1, "3", "3; id", 1.5])
def test_gate_rejects_non_integer_tap_plan(tap_plan) -> None:
	with pytest.raises(RENDERER.ManifestSafetyError):
		RENDERER.enforce_shell_safe_manifest(_manifest(tap_plan=tap_plan))


@pytest.mark.parametrize("port", [True, 0, 65536, "80; id", "$(id)", 80.5])
def test_gate_rejects_unsafe_port(port) -> None:
	manifest = _manifest()
	manifest["port"] = port
	with pytest.raises(RENDERER.ManifestSafetyError):
		RENDERER.enforce_shell_safe_manifest(manifest)


def test_shell_quote_filter_produces_single_literal_word() -> None:
	with tempfile.TemporaryDirectory() as td:
		sentinel = Path(td) / "PWNED"
		for payload in (f"x$(touch {sentinel})", f"x`touch {sentinel}`", "a'b", "a b; c", "\n"):
			script = f"value={RENDERER.shell_quote(payload)}\nprintf '%s' \"$value\"\n"
			result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
			assert result.returncode == 0, result.stderr
			assert result.stdout == payload
			assert not sentinel.exists()


def test_schema_pattern_matches_renderer_gate() -> None:
	schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
	project_name = schema["properties"]["slots"]["properties"]["project_name"]
	assert project_name["maxLength"] == RENDERER.PROJECT_NAME_MAX_LENGTH
	assert project_name["pattern"] == "^[A-Za-z0-9](?:[A-Za-z0-9 ._/@+:-]*[A-Za-z0-9._/@+:-])?$"


def test_every_shell_template_quotes_manifest_interpolations() -> None:
	"""Static contract: no shell test interpolates a raw manifest value."""
	offenders = []
	for template in sorted(TEMPLATES_ROOT.rglob("*.sh.j2")):
		for lineno, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
			if "{{" not in line:
				continue
			safe = (
				"| shell_quote }}" in line
				or "| length }}" in line
				# Per-tool loops already wrap each value in single quotes.
				or "replace(\"'\", \"'\\\"'\\\"'\")" in line
				# Canary entries are charset-gated by enforce_shell_safe_manifest.
				or "{{ slots.canary_tools | join(' ') }}" in line
			)
			if not safe:
				offenders.append(f"{template.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
	assert not offenders, "unquoted manifest interpolation(s):\n" + "\n".join(offenders)


def _require_render_deps():
	"""Skip (per test) when the renderer's runtime dependencies are not installed."""
	pytest.importorskip("jsonschema", reason="jsonschema is required to run the renderer CLI")
	pytest.importorskip("jinja2", reason="jinja2 is required to render templates")
	return pytest.importorskip("yaml", reason="PyYAML is required to run the renderer CLI")


def _run_cli(manifest: dict, root: Path, schema_path: Path = SCHEMA_PATH) -> subprocess.CompletedProcess[str]:
	yaml = _require_render_deps()
	manifest_path = root / "validate.yml"
	manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
	return subprocess.run(
		[
			sys.executable, str(SCRIPT_PATH), "--manifest", str(manifest_path), "--schema", str(schema_path),
			"--templates-root", str(TEMPLATES_ROOT), "--output-root", str(root / "out"),
		],
		capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
	)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("permissive_schema", [False, True])
def test_cli_rejects_command_substitution_project_name(family: str, permissive_schema: bool) -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		sentinel = root / "PWNED"
		schema_path = SCHEMA_PATH
		if permissive_schema:
			# A caller-supplied schema without the pattern must not bypass the gate.
			schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
			schema["properties"]["slots"]["properties"]["project_name"] = {"type": "string"}
			schema_path = root / "permissive.schema.json"
			schema_path.write_text(json.dumps(schema), encoding="utf-8")
		result = _run_cli(_manifest(family, project_name=f"x$(touch {sentinel})"), root, schema_path)
		assert result.returncode == 1
		assert "/slots/project_name" in result.stderr
		if permissive_schema:
			assert "Manifest safety check failed" in result.stderr
		assert not (root / "out").exists() or not any((root / "out").rglob("*"))
		assert not sentinel.exists()


def _render_hostile(family: str, root: Path, slots: dict, manifest_extra: dict) -> Path:
	"""Render bypassing the gate to prove the templates quote on their own."""
	_require_render_deps()
	manifest = {"type": family, "entry": "app.py", "port": 8000, "slots": slots, **manifest_extra}
	family_spec = RENDERER.resolve_family(manifest)
	specs = RENDERER.collect_templates(TEMPLATES_ROOT, family_spec)
	context = RENDERER.build_render_context(manifest, family_spec)
	context.setdefault("output_root_name", "validation")
	out = root / "validation"
	RENDERER.write_outputs(out, RENDERER.render_templates(specs, TEMPLATES_ROOT, context))
	return out


@pytest.mark.parametrize("family", FAMILIES)
def test_templates_render_hostile_values_as_literals(family: str) -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		sentinel = root / "PWNED"
		payload = f"x$(touch {sentinel})`touch {sentinel}`;touch {sentinel}"
		out = _render_hostile(
			family, root,
			{"project_name": payload, "canary_tools": ["curl"], "tap_plan": 1,
			 "health_path": payload, "test_host_header": payload},
			{"entry": payload},
		)
		rendered = sorted(out.rglob("*.sh"))
		assert rendered
		for script in rendered:
			syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=10)
			assert syntax.returncode == 0, f"{script}: {syntax.stderr}"
		marker = out / "tests" / "10_family_marker.sh"
		result = subprocess.run(["bash", str(marker)], capture_output=True, text=True, timeout=10, cwd=root)
		assert result.returncode == 0, result.stderr
		assert payload in result.stdout
		# Execute only the default-assignment heads of the other tests: every
		# `_default_*=` line must assign the literal payload without running it.
		for script in rendered:
			heads = [line for line in script.read_text(encoding="utf-8").splitlines() if line.startswith("_default_")]
			if not heads:
				continue
			probe = "\n".join(heads) + "\n" + "\n".join(
				f"printf '%s\\n' \"${{{line.split('=', 1)[0]}}}\"" for line in heads
			)
			result = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, timeout=10, cwd=root)
			assert result.returncode == 0, f"{script}: {result.stderr}"
		assert not sentinel.exists()


@pytest.mark.parametrize("family", FAMILIES)
def test_all_rendered_shell_passes_bash_syntax_check(family: str) -> None:
	if shutil.which("bash") is None:
		pytest.skip("bash unavailable")
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		result = _run_cli(_manifest(family), root)
		assert result.returncode == 0, result.stderr
		for script in sorted((root / "out").rglob("*.sh")):
			syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=10)
			assert syntax.returncode == 0, f"{script}: {syntax.stderr}"
