#!/usr/bin/env python3
"""Security #9465: render_prompt.sh must not let untrusted prompt bodies pull
environment values (GH_TOKEN, API keys, ...) into a rendered prompt."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
REVIEW_RUN_REVIEWERS_SH = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
REVIEW_APPLY_FIXES_SH = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
UNSET_LINE = "unset GH_TOKEN GITHUB_TOKEN GH_PAT"
UNTRUSTED_FLAG_SETS = (
	{"RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED": "1", "RENDER_PROMPT_SKIP_SYNTAX_VALIDATION": "1"},
	{"RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED": "1"},
	{"RENDER_PROMPT_SKIP_SYNTAX_VALIDATION": "true"},
)


def _base_env() -> dict[str, str]:
	return {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"HOME": os.environ.get("HOME", "/tmp"),
		"PYTHONDONTWRITEBYTECODE": "1",
		"PROMPT_PRELUDE_REFACTOR_ENABLED": "false",
	}


def _render(prompt_file: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["bash", str(RENDER_PROMPT_SH), str(prompt_file)],
		cwd=str(REPO_ROOT),
		env={**_base_env(), **extra_env},
		text=True,
		capture_output=True,
		timeout=60,
		check=False,
	)


def test_untrusted_body_does_not_substitute_env_values() -> None:
	sentinels = {
		"GH_TOKEN": "sentinel-gh-token-9465",
		"OPENROUTER_API_KEY": "sentinel-openrouter-9465",
		"CUSTOM_VAR": "sentinel-custom-var-9465",
	}
	body = "".join(f"line {name}={{{{{name}}}}}\n" for name in sentinels)
	with tempfile.TemporaryDirectory(prefix="render_prompt_guard_untrusted_") as td:
		prompt_file = Path(td) / "reviewer_prompt_body.txt"
		prompt_file.write_text(body, encoding="utf-8")
		for flags in UNTRUSTED_FLAG_SETS:
			proc = _render(prompt_file, {**sentinels, **flags})
			assert proc.returncode == 0, (flags, proc.stderr)
			for name, value in sentinels.items():
				assert value not in proc.stdout, (flags, name)
				assert value not in proc.stderr, (flags, name)
				assert f"line {name}={{{{{name}}}}}" in proc.stdout, (flags, name)


def test_untrusted_body_keeps_fixed_allowlist_substitution() -> None:
	# render_prompt.py substitutes SERENA_TOOL_HINTS, SEMBLE_PREFETCH and
	# WORKFLOW_EDIT_RESTRICTION only when they stand alone on a line.
	body = (
		"overlay={{MODEL_FAMILY_OVERLAY}}\n"
		"{{SERENA_TOOL_HINTS}}\n"
		"{{SEMBLE_PREFETCH}}\n"
		"{{WORKFLOW_EDIT_RESTRICTION}}\n"
	)
	with tempfile.TemporaryDirectory(prefix="render_prompt_guard_allowlist_") as td:
		prompt_file = Path(td) / "reviewer_prompt_body.txt"
		prompt_file.write_text(body, encoding="utf-8")
		for flags in UNTRUSTED_FLAG_SETS:
			proc = _render(
				prompt_file,
				{
					**flags,
					"MODEL_FAMILY_OVERLAY": "overlay-value",
					"SERENA_TOOL_HINTS": "serena-value",
					"SEMBLE_PREFETCH": "semble-value",
					"ALLOW_WORKFLOW_EDITS": "true",
				},
			)
			assert proc.returncode == 0, (flags, proc.stderr)
			assert "overlay=overlay-value" in proc.stdout
			assert "serena-value" in proc.stdout.splitlines()
			assert "semble-value" in proc.stdout.splitlines()
			assert "- CI workflow edits under .github/workflows/ are permitted" in proc.stdout
			assert "{{" not in proc.stdout


def test_trusted_template_denies_credential_like_env_placeholders() -> None:
	denied = {
		"GH_TOKEN": "sentinel-denied-gh-token-9465",
		"GITHUB_TOKEN": "sentinel-denied-github-token-9465",
		"GH_PAT": "sentinel-denied-gh-pat-9465",
		"OPENROUTER_API_KEY": "sentinel-denied-openrouter-9465",
		"TG_BOT_SECRET": "sentinel-denied-tg-secret-9465",
		"ACTIONS_RUNTIME_TOKEN": "sentinel-denied-actions-9465",
		"MY_PAT": "sentinel-denied-my-pat-9465",
		"GH_REPO_SLUG": "sentinel-denied-gh-prefix-9465",
		"SERVICE_SIGNINGKEY": "sentinel-denied-fused-key-9465",
		"MY_APIPAT": "sentinel-denied-fused-pat-9465",
	}
	allowed = {
		"CUSTOM_PLACEHOLDER": "custom-placeholder-value",
		"BUILD_PATH": "build-path-value",
	}
	names = [*denied, "1", *allowed]
	body = "".join(f"line {name}={{{{{name}}}}}\n" for name in names)
	with tempfile.TemporaryDirectory(prefix="render_prompt_guard_trusted_") as td:
		prompt_file = Path(td) / "mode-guardtest.txt"
		prompt_file.write_text(body, encoding="utf-8")
		proc = _render(prompt_file, {**denied, **allowed})

	assert proc.returncode == 0, proc.stderr
	for name, value in denied.items():
		assert value not in proc.stdout, name
		assert value not in proc.stderr, name
		assert f"line {name}={{{{{name}}}}}" in proc.stdout, name
		assert f"{{{{{name}}}}}" in proc.stderr, name
	# A double-braced `1` must not resolve to the script's positional
	# parameter (the prompt path) via `${!name}`. Built by concatenation so
	# this file never carries a contiguous braced token an older renderer
	# would fill when the file is embedded in a reviewer prompt (#9465).
	positional_token = "{{" + "1" + "}}"
	assert f"line 1={positional_token}" in proc.stdout
	assert str(prompt_file) not in proc.stdout
	assert positional_token in proc.stderr
	for name, value in allowed.items():
		assert f"line {name}={value}" in proc.stdout, name
		assert f"{{{{{name}}}}}" not in proc.stderr, name


def _render_subshells(script_text: str) -> list[str]:
	"""Return each `( ... )` subshell body that invokes render_prompt.sh."""
	lines = script_text.splitlines()
	bodies: list[str] = []
	for index, line in enumerate(lines):
		if "render_prompt.sh" not in line or "RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED=1" not in line:
			continue
		start = index
		while start > 0 and not re.search(r"\(\s*$", lines[start]):
			start -= 1
		bodies.append("\n".join(lines[start : index + 1]))
	return bodies


def test_review_render_subshells_unset_github_tokens() -> None:
	expected_counts = {REVIEW_RUN_REVIEWERS_SH: 2, REVIEW_APPLY_FIXES_SH: 1}
	for script, expected in expected_counts.items():
		bodies = _render_subshells(script.read_text(encoding="utf-8"))
		assert len(bodies) == expected, (script.name, len(bodies))
		for body in bodies:
			assert UNSET_LINE in body, (script.name, body)
			unset_pos = body.index(UNSET_LINE)
			render_pos = body.index("render_prompt.sh")
			assert unset_pos < render_pos, (script.name, body)
