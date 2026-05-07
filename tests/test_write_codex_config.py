#!/usr/bin/env python3
"""Contract tests for scripts/write_codex_config.sh.

The shared helper is the single source of truth for ~/.codex/config.toml
across implement / clarify / plan / orchestrate / orchestrate_clarify_respond /
review_autofix / workflow-log-analysis / validate workflows AND scripts/
orchestrate_poll_process.sh + scripts/validate_process.sh. Drift in the
emitted TOML directly affects whether `apply_patch` is registered as a
tool for `gpt-5.3-codex` (PR openai/codex#11238 removed the offline
fallback) and whether the v0.113+ trust prompt fires (codex#14345). Pin
the contract here so a future edit to the helper that drops one of those
keys fails CI loudly.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WRITE_CODEX_CONFIG = REPO_ROOT / "scripts" / "write_codex_config.sh"


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	# Strip context that would otherwise auto-elevate. Tests opt into
	# elevation explicitly via env or --allow-elevation.
	for k in ("GITHUB_ACTIONS", "VALIDATE_FORCE_FULL_ACCESS"):
		full_env.pop(k, None)
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", str(WRITE_CODEX_CONFIG), *args],
		env=full_env,
		text=True,
		capture_output=True,
		check=False,
	)


def _read_config(tmpdir: Path) -> str:
	cfg = tmpdir / "config.toml"
	assert cfg.is_file(), f"helper did not write {cfg}"
	return cfg.read_text(encoding="utf-8")


def test_helper_emits_apply_patch_keys_under_github_actions(tmp_path: Path) -> None:
	"""On a GH-hosted runner (GITHUB_ACTIONS=true) the helper MUST emit:
	  - model_catalog_json (when the catalog file exists)
	  - [projects.<workdir>] trust_level = "trusted"
	  - approval_policy = "never"
	  - sandbox_mode = "danger-full-access"
	plus the static [model_providers.openrouter] / [sandbox_workspace_write]
	blocks. These four keys are the load-bearing fix for the recurring
	"codex narrates apply_patch but never invokes it" failure.
	"""
	catalog = tmp_path / "catalog.json"
	catalog.write_text('{"models": []}', encoding="utf-8")
	cfg = tmp_path / "config.toml"
	project = tmp_path / "workdir"
	project.mkdir()

	result = _run(
		[
			"--model", "openai/gpt-5.3-codex",
			"--reasoning", "medium",
			"--catalog-path", str(catalog),
			"--project-path", str(project),
			"--config-path", str(cfg),
		],
		env={"GITHUB_ACTIONS": "true"},
	)
	assert result.returncode == 0, f"helper failed: stdout={result.stdout!r} stderr={result.stderr!r}"

	body = _read_config(tmp_path)
	assert 'approval_policy = "never"' in body, body
	assert 'sandbox_mode = "danger-full-access"' in body, body
	assert 'model_provider = "openrouter"' in body, body
	assert 'model = "openai/gpt-5.3-codex"' in body, body
	assert 'model_reasoning_effort = "medium"' in body, body
	assert f'model_catalog_json = "{catalog}"' in body, body
	assert "[model_providers.openrouter]" in body, body
	assert "[sandbox_workspace_write]" in body, body
	assert 'network_access = true' in body, body
	assert f'[projects."{project}"]' in body, body
	assert 'trust_level = "trusted"' in body, body


def test_helper_falls_back_to_safe_defaults_locally(tmp_path: Path) -> None:
	"""Outside GH (no GITHUB_ACTIONS, no VALIDATE_FORCE_FULL_ACCESS) the
	helper MUST keep approval_policy/sandbox_mode at codex's safer
	workspace-write/on-request defaults. This is the standalone-safety
	gate Copilot's PR #2196 review asked for so a developer running
	validate_process.sh locally doesn't accidentally hand codex
	full-filesystem access.
	"""
	cfg = tmp_path / "config.toml"
	result = _run(
		[
			"--model", "openai/gpt-5.4",
			"--reasoning", "low",
			"--catalog-path", str(tmp_path / "missing.json"),
			"--project-path", str(tmp_path),
			"--config-path", str(cfg),
		],
	)
	assert result.returncode == 0, f"helper failed: {result.stderr}"
	body = _read_config(tmp_path)
	assert 'approval_policy = "on-request"' in body, body
	assert 'sandbox_mode = "workspace-write"' in body, body
	# Catalog path was a non-existent file, so the line MUST NOT appear:
	assert "model_catalog_json" not in body, body


def test_helper_force_elevation_overrides_local_default(tmp_path: Path) -> None:
	"""--allow-elevation force always elevates, ignoring GITHUB_ACTIONS.
	Used by callers that have already audited their own context (e.g.
	the workflow YAML steps know they run on GH-hosted runners and
	don't need the auto-detect).
	"""
	cfg = tmp_path / "config.toml"
	result = _run(
		[
			"--model", "openai/gpt-5.4",
			"--reasoning", "high",
			"--catalog-path", str(tmp_path / "missing.json"),
			"--project-path", str(tmp_path),
			"--config-path", str(cfg),
			"--allow-elevation", "force",
		],
	)
	assert result.returncode == 0
	body = _read_config(tmp_path)
	assert 'approval_policy = "never"' in body
	assert 'sandbox_mode = "danger-full-access"' in body


def test_helper_forbid_elevation_keeps_safe_even_on_github(tmp_path: Path) -> None:
	"""--allow-elevation forbid stays safe even when GITHUB_ACTIONS=true.
	Future-proofing: lets a caller opt OUT of elevation if a specific
	context (e.g. a future read-only summariser) needs it.
	"""
	cfg = tmp_path / "config.toml"
	result = _run(
		[
			"--model", "openai/gpt-5.4",
			"--reasoning", "high",
			"--catalog-path", str(tmp_path / "missing.json"),
			"--project-path", str(tmp_path),
			"--config-path", str(cfg),
			"--allow-elevation", "forbid",
		],
		env={"GITHUB_ACTIONS": "true"},
	)
	assert result.returncode == 0
	body = _read_config(tmp_path)
	assert 'approval_policy = "on-request"' in body
	assert 'sandbox_mode = "workspace-write"' in body


def test_helper_validate_force_full_access_env_var(tmp_path: Path) -> None:
	"""VALIDATE_FORCE_FULL_ACCESS=1 elevates locally without
	GITHUB_ACTIONS. This is the documented opt-in for developers
	running validate_process.sh locally on a sandboxed VM.
	"""
	cfg = tmp_path / "config.toml"
	result = _run(
		[
			"--model", "openai/gpt-5.4",
			"--reasoning", "low",
			"--catalog-path", str(tmp_path / "missing.json"),
			"--project-path", str(tmp_path),
			"--config-path", str(cfg),
		],
		env={"VALIDATE_FORCE_FULL_ACCESS": "1"},
	)
	assert result.returncode == 0
	body = _read_config(tmp_path)
	assert 'approval_policy = "never"' in body
	assert 'sandbox_mode = "danger-full-access"' in body


def test_helper_rejects_invalid_inputs(tmp_path: Path) -> None:
	"""Validation surface — every required-arg / enum-value check exits
	rc=2 with a ::error:: annotation so a typo in a caller's invocation
	surfaces in the GHA log instead of silently writing a malformed
	config.
	"""
	cfg = tmp_path / "config.toml"

	# missing --model
	result = _run(["--reasoning", "low", "--config-path", str(cfg)])
	assert result.returncode == 2
	assert "::error::" in result.stderr
	assert "--model is required" in result.stderr

	# invalid reasoning
	result = _run(
		["--model", "x", "--reasoning", "MEDIUM", "--config-path", str(cfg)],
	)
	assert result.returncode == 2
	assert "invalid --reasoning" in result.stderr

	# invalid web-search
	result = _run(
		[
			"--model", "x",
			"--reasoning", "low",
			"--web-search", "always",
			"--config-path", str(cfg),
		],
	)
	assert result.returncode == 2
	assert "invalid --web-search" in result.stderr

	# unknown flag
	result = _run(
		["--model", "x", "--reasoning", "low", "--bogus", "y", "--config-path", str(cfg)],
	)
	assert result.returncode == 2
	assert "unknown argument" in result.stderr


def test_helper_quotes_project_path_with_spaces(tmp_path: Path) -> None:
	"""Project paths with spaces/odd chars must be TOML-quoted exactly
	once. Codex matches the trust path strictly (no globs — see
	openai/codex#14599), so any quoting drift breaks the bypass.
	"""
	weird = tmp_path / "work dir with spaces"
	weird.mkdir()
	cfg = tmp_path / "config.toml"
	result = _run(
		[
			"--model", "openai/gpt-5.4",
			"--reasoning", "low",
			"--catalog-path", str(tmp_path / "missing.json"),
			"--project-path", str(weird),
			"--config-path", str(cfg),
			"--allow-elevation", "force",
		],
	)
	assert result.returncode == 0
	body = _read_config(tmp_path)
	# Exactly one [projects."..."] header, with the weird path verbatim.
	headers = re.findall(r'^\[projects\."([^"]+)"\]$', body, flags=re.MULTILINE)
	assert headers == [str(weird)], (
		f"expected one project header for {weird!r}, got {headers!r}\n--- body ---\n{body}"
	)
