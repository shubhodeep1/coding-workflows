#!/usr/bin/env python3
"""Regression tests for the shared-prelude prompt assembly path."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
SCRIPTS_DIR = REPO_ROOT / "scripts"
ASSEMBLE_PROMPT_SH = SCRIPTS_DIR / "assemble_prompt.sh"
RENDER_PROMPT_SH = SCRIPTS_DIR / "render_prompt.sh"


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["PROMPT_PERSONA_PREFIX_ENABLED"] = "false"
	return env


def _normalize_text(content: str) -> str:
	normalized = content.replace("\r\n", "\n").replace("\r", "\n")
	if not normalized.endswith("\n"):
		normalized += "\n"
	return normalized


def _copy_prompt_runtime_scripts(repo_root: Path) -> None:
	for script_name in ("render_prompt.py", "render_prompt.sh", "assemble_prompt.sh"):
		src = SCRIPTS_DIR / script_name
		dst = repo_root / "scripts" / script_name
		dst.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(src, dst)
		if dst.suffix == ".sh":
			dst.chmod(0o755)


def _active_mode_prompt_names() -> tuple[str, ...]:
	return tuple(sorted(path.name for path in PROMPTS_DIR.glob("mode-*.txt")))


def _template_mode_prompt_names() -> tuple[str, ...]:
	return tuple(sorted(path.name for path in (PROMPTS_DIR / "_templates").glob("mode-*.txt")))


def test_every_active_mode_prompt_has_a_template() -> None:
	active_prompt_names = _active_mode_prompt_names()
	template_prompt_names = set(_template_mode_prompt_names())
	missing_templates = sorted(set(active_prompt_names) - template_prompt_names)
	assert missing_templates == []


def test_assembled_templates_match_legacy_prompt_bytes() -> None:
	for prompt_name in _active_mode_prompt_names():
		legacy_prompt = PROMPTS_DIR / prompt_name
		template_prompt = PROMPTS_DIR / "_templates" / prompt_name
		assert template_prompt.is_file(), template_prompt

		proc = subprocess.run(
			["bash", str(ASSEMBLE_PROMPT_SH), str(legacy_prompt)],
			cwd=str(REPO_ROOT),
			env=_base_env(),
			text=True,
			capture_output=True,
			timeout=60,
		)

		assert proc.returncode == 0, f"{prompt_name}: {proc.stderr}"
		assert proc.stderr == ""
		assert proc.stdout == _normalize_text(legacy_prompt.read_text(encoding="utf-8"))


def test_assemble_prompt_reports_missing_fragment() -> None:
	with tempfile.TemporaryDirectory(prefix="assemble_prompt_missing_fragment_") as td:
		repo_root = Path(td)
		_copy_prompt_runtime_scripts(repo_root)
		(repo_root / "prompts" / "mode-sample.txt").parent.mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "_templates").mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "mode-sample.txt").write_text("legacy\n", encoding="utf-8")
		(repo_root / "prompts" / "_templates" / "mode-sample.txt").write_text(
			'{% include "_prelude_common.txt" %}\nbody\n',
			encoding="utf-8",
		)

		proc = subprocess.run(
			["bash", str(repo_root / "scripts" / "assemble_prompt.sh"), "prompts/mode-sample.txt"],
			cwd=str(repo_root),
			env=_base_env(),
			text=True,
			capture_output=True,
			timeout=60,
		)

		assert proc.returncode == 1
		assert proc.stdout == ""
		assert "Included prompt fragment not found" in proc.stderr
		assert "_prelude_common.txt" in proc.stderr


def test_render_prompt_sh_flag_true_rejects_unsupported_placeholder_expression() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_strict_template_expr_") as td:
		repo_root = Path(td)
		_copy_prompt_runtime_scripts(repo_root)
		(repo_root / "prompts" / "mode-sample.txt").parent.mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "_templates").mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "mode-sample.txt").write_text("legacy\n", encoding="utf-8")
		(repo_root / "prompts" / "_prelude_common.txt").write_text("template\n", encoding="utf-8")
		(repo_root / "prompts" / "_templates" / "mode-sample.txt").write_text(
			'{% include "_prelude_common.txt" %}\n{{FOO|lower}}\n',
			encoding="utf-8",
		)

		env = _base_env()
		env["PROMPT_PRELUDE_REFACTOR_ENABLED"] = "true"
		proc = subprocess.run(
			["bash", str(repo_root / "scripts" / "render_prompt.sh"), "prompts/mode-sample.txt"],
			cwd=str(repo_root),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 1
	assert proc.stdout == ""
	assert "Unsupported template syntax" in proc.stderr
	assert "{{FOO|lower}}" in proc.stderr


def test_render_prompt_sh_flag_false_uses_legacy_prompt_even_when_template_exists() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_flag_off_") as td:
		repo_root = Path(td)
		_copy_prompt_runtime_scripts(repo_root)
		(repo_root / "prompts" / "mode-sample.txt").parent.mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "_templates").mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "mode-sample.txt").write_text("docker inspect --format='{{.State.ExitCode}}'\n", encoding="utf-8")
		(repo_root / "prompts" / "_prelude_common.txt").write_text("template\n", encoding="utf-8")
		(repo_root / "prompts" / "_templates" / "mode-sample.txt").write_text(
			'{% include "_prelude_common.txt" %}\nbody\n',
			encoding="utf-8",
		)

		env = _base_env()
		env["PROMPT_PRELUDE_REFACTOR_ENABLED"] = "false"
		proc = subprocess.run(
			["bash", str(repo_root / "scripts" / "render_prompt.sh"), "prompts/mode-sample.txt"],
			cwd=str(repo_root),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	assert proc.stdout == "docker inspect --format='{{.State.ExitCode}}'\n"


def test_render_prompt_sh_flag_true_uses_assembled_template_when_present() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_flag_on_") as td:
		repo_root = Path(td)
		_copy_prompt_runtime_scripts(repo_root)
		(repo_root / "prompts" / "mode-sample.txt").parent.mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "_templates").mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "mode-sample.txt").write_text("legacy\n", encoding="utf-8")
		(repo_root / "prompts" / "_prelude_common.txt").write_text("template\n", encoding="utf-8")
		(repo_root / "prompts" / "_templates" / "mode-sample.txt").write_text(
			'{% include "_prelude_common.txt" %}\nbody\n',
			encoding="utf-8",
		)

		env = _base_env()
		env["PROMPT_PRELUDE_REFACTOR_ENABLED"] = "true"
		proc = subprocess.run(
			["bash", str(repo_root / "scripts" / "render_prompt.sh"), "prompts/mode-sample.txt"],
			cwd=str(repo_root),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	assert proc.stdout == "template\nbody\n"


def test_render_prompt_sh_flag_true_applies_workflow_overlay_once() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_overlay_once_") as td:
		repo_root = Path(td)
		_copy_prompt_runtime_scripts(repo_root)
		(repo_root / "prompts" / "mode-sample.txt").parent.mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "_templates").mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "mode-sample.txt").write_text("legacy\n", encoding="utf-8")
		(repo_root / "prompts" / "_prelude_common.txt").write_text("template\n", encoding="utf-8")
		(repo_root / "prompts" / "_templates" / "mode-sample.txt").write_text(
			'{% include "_prelude_common.txt" %}\nbody\n',
			encoding="utf-8",
		)
		(repo_root / "overlay.txt").write_text("overlay\n", encoding="utf-8")

		env = _base_env()
		env["PROMPT_PRELUDE_REFACTOR_ENABLED"] = "true"
		env["WORKFLOW_OVERLAY_REPO_ROOT"] = str(repo_root)
		env["WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON"] = json.dumps(
			[
				{
					"mode": "mode-sample",
					"append_path": "overlay.txt",
				}
			]
		)
		proc = subprocess.run(
			["bash", str(repo_root / "scripts" / "render_prompt.sh"), "prompts/mode-sample.txt"],
			cwd=str(repo_root),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	assert proc.stdout == "template\nbody\noverlay\n"
	assert list((repo_root / "prompts").glob(".mode-sample.txt.assembled.*")) == []


def test_render_prompt_sh_flag_true_cleans_up_assembled_temp_file() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_cleanup_") as td:
		repo_root = Path(td)
		_copy_prompt_runtime_scripts(repo_root)
		(repo_root / "prompts" / "mode-sample.txt").parent.mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "_templates").mkdir(parents=True, exist_ok=True)
		(repo_root / "prompts" / "mode-sample.txt").write_text("legacy\n", encoding="utf-8")
		(repo_root / "prompts" / "_prelude_common.txt").write_text("template\n", encoding="utf-8")
		(repo_root / "prompts" / "_templates" / "mode-sample.txt").write_text(
			'{% include "_prelude_common.txt" %}\nbody\n',
			encoding="utf-8",
		)

		env = _base_env()
		env["PROMPT_PRELUDE_REFACTOR_ENABLED"] = "true"
		proc = subprocess.run(
			["bash", str(repo_root / "scripts" / "render_prompt.sh"), "prompts/mode-sample.txt"],
			cwd=str(repo_root),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	assert proc.stdout == "template\nbody\n"
	assert list((repo_root / "prompts").glob(".mode-sample.txt.assembled.*")) == []


def test_render_prompt_sh_body_with_include_line_preserved_with_skip_syntax_validation() -> None:
	# Guards the shipped contract via the render_prompt.sh shim: a reviewer/editor
	# BODY that embeds a raw PR diff/context line `{% include "..." %}` must survive
	# verbatim when RENDER_PROMPT_SKIP_SYNTAX_VALIDATION is set — the env var marks
	# the input as an already-composed untrusted body, so include-assembly is skipped
	# and the standalone include line is treated as the diff's own content rather than
	# a fragment to expand. Mirrors the direct-invocation coverage in
	# tests/test_render_prompt_foundation.py for the bash entrypoint + env-var path.
	# (Renderer behavior locked in by PR #3647.)
	with tempfile.TemporaryDirectory(prefix="render_prompt_body_include_skip_syntax_") as td:
		repo_root = Path(td)
		_copy_prompt_runtime_scripts(repo_root)
		body_file = repo_root / "reviewer_prompt_body.txt"
		body_file.write_text(
			"Reviewer body. Overlay: {{MODEL_FAMILY_OVERLAY}}\n"
			"=== PR DIFF (untrusted) ===\n"
			"@@ -1,3 +1,3 @@\n"
			' {% include "_partials/site_footer.html" %}\n',
			encoding="utf-8",
		)

		env = _base_env()
		env.pop("RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED", None)
		env["RENDER_PROMPT_SKIP_SYNTAX_VALIDATION"] = "1"
		env["MODEL_FAMILY_OVERLAY"] = "X"
		proc = subprocess.run(
			["bash", str(repo_root / "scripts" / "render_prompt.sh"), str(body_file)],
			cwd=str(repo_root),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	# Static placeholder substitution still runs for the trusted scaffolding.
	assert "Reviewer body. Overlay: X\n" in proc.stdout
	# The untrusted include line survives as literal text (never expanded).
	assert ' {% include "_partials/site_footer.html" %}\n' in proc.stdout


def test_render_prompt_sh_input_already_assembled_keeps_include_line_literal() -> None:
	# The fix: with RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED=1 the body is not
	# re-run through include-assembly, so an embedded `{% include "..." %}` diff
	# line survives verbatim while static-scaffolding placeholders still render.
	with tempfile.TemporaryDirectory(prefix="render_prompt_body_include_fixed_") as td:
		repo_root = Path(td)
		_copy_prompt_runtime_scripts(repo_root)
		body_file = repo_root / "reviewer_prompt_body.txt"
		body_file.write_text(
			"Reviewer body. Overlay: {{MODEL_FAMILY_OVERLAY}}\n"
			"=== PR DIFF (untrusted) ===\n"
			"@@ -1,3 +1,3 @@\n"
			' {% include "_partials/site_footer.html" %}\n',
			encoding="utf-8",
		)

		env = _base_env()
		env.pop("RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED", None)
		env["RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED"] = "1"
		env["RENDER_PROMPT_SKIP_SYNTAX_VALIDATION"] = "1"
		env["MODEL_FAMILY_OVERLAY"] = "X"
		proc = subprocess.run(
			["bash", str(repo_root / "scripts" / "render_prompt.sh"), str(body_file)],
			cwd=str(repo_root),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

	assert proc.returncode == 0, proc.stderr
	assert proc.stderr == ""
	# Placeholder substitution still runs for the static scaffolding.
	assert "Reviewer body. Overlay: X\n" in proc.stdout
	# The untrusted include line survives as literal text (never expanded).
	assert ' {% include "_partials/site_footer.html" %}\n' in proc.stdout


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test_fn in tests:
		test_fn()
	print("OK: shared-prelude assembly path is byte-stable and flag-gated")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
