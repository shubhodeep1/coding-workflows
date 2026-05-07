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

Layout follows the rest of tests/ in this repo: zero-arg test functions
plus a manual `main()` runner so CI can execute the file with
`python3 tests/test_write_codex_config.py` (no pytest dependency).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WRITE_CODEX_CONFIG = REPO_ROOT / "scripts" / "write_codex_config.sh"


def _run(args: list[str], env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	# Strip ambient context that would otherwise auto-elevate. Tests opt
	# into elevation explicitly via env_overrides or --allow-elevation.
	for k in ("GITHUB_ACTIONS", "VALIDATE_FORCE_FULL_ACCESS"):
		full_env.pop(k, None)
	if env_overrides:
		full_env.update(env_overrides)
	return subprocess.run(
		["bash", str(WRITE_CODEX_CONFIG), *args],
		env=full_env,
		text=True,
		capture_output=True,
		check=False,
	)


def _read_config(cfg_path: Path) -> str:
	assert cfg_path.is_file(), f"helper did not write {cfg_path}"
	return cfg_path.read_text(encoding="utf-8")


def test_helper_emits_apply_patch_keys_under_github_actions() -> None:
	"""On a GH-hosted runner (GITHUB_ACTIONS=true) the helper MUST emit:
	  - model_catalog_json (when the catalog file exists)
	  - [projects."<workdir>"] trust_level = "trusted"
	  - approval_policy = "never"
	  - sandbox_mode = "danger-full-access"
	plus the static [model_providers.openrouter] / [sandbox_workspace_write]
	blocks. These four keys are the load-bearing fix for the recurring
	"codex narrates apply_patch but never invokes it" failure.
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		catalog = tmp / "catalog.json"
		catalog.write_text('{"models": []}', encoding="utf-8")
		cfg = tmp / "config.toml"
		project = tmp / "workdir"
		project.mkdir()
		result = _run(
			[
				"--model", "openai/gpt-5.3-codex",
				"--reasoning", "medium",
				"--catalog-path", str(catalog),
				"--project-path", str(project),
				"--config-path", str(cfg),
			],
			env_overrides={"GITHUB_ACTIONS": "true"},
		)
		assert result.returncode == 0, f"helper failed: stdout={result.stdout!r} stderr={result.stderr!r}"
		body = _read_config(cfg)
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


def test_helper_falls_back_to_safe_defaults_locally() -> None:
	"""Outside GH (no GITHUB_ACTIONS, no VALIDATE_FORCE_FULL_ACCESS) the
	helper MUST keep approval_policy/sandbox_mode at codex's safer
	workspace-write/on-request defaults. This is the standalone-safety
	gate Copilot's PR #2196 review asked for so a developer running
	validate_process.sh locally doesn't accidentally hand codex
	full-filesystem access.
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		cfg = tmp / "config.toml"
		result = _run(
			[
				"--model", "openai/gpt-5.4",
				"--reasoning", "low",
				"--catalog-path", str(tmp / "missing.json"),
				"--project-path", str(tmp),
				"--config-path", str(cfg),
			],
		)
		assert result.returncode == 0, f"helper failed: {result.stderr}"
		body = _read_config(cfg)
		assert 'approval_policy = "on-request"' in body, body
		assert 'sandbox_mode = "workspace-write"' in body, body
		# Catalog path was a non-existent file → MUST omit the line:
		assert "model_catalog_json" not in body, body


def test_helper_force_elevation_overrides_local_default() -> None:
	"""--allow-elevation force always elevates, ignoring GITHUB_ACTIONS.
	Used by callers that have already audited their own context and
	don't need the auto-detect.
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		cfg = tmp / "config.toml"
		result = _run(
			[
				"--model", "openai/gpt-5.4",
				"--reasoning", "high",
				"--catalog-path", str(tmp / "missing.json"),
				"--project-path", str(tmp),
				"--config-path", str(cfg),
				"--allow-elevation", "force",
			],
		)
		assert result.returncode == 0
		body = _read_config(cfg)
		assert 'approval_policy = "never"' in body
		assert 'sandbox_mode = "danger-full-access"' in body


def test_helper_forbid_elevation_keeps_safe_even_on_github() -> None:
	"""--allow-elevation forbid stays safe even when GITHUB_ACTIONS=true.
	Future-proofing: lets a caller opt OUT of elevation if a specific
	context (e.g. a future read-only summariser) needs it.
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		cfg = tmp / "config.toml"
		result = _run(
			[
				"--model", "openai/gpt-5.4",
				"--reasoning", "high",
				"--catalog-path", str(tmp / "missing.json"),
				"--project-path", str(tmp),
				"--config-path", str(cfg),
				"--allow-elevation", "forbid",
			],
			env_overrides={"GITHUB_ACTIONS": "true"},
		)
		assert result.returncode == 0
		body = _read_config(cfg)
		assert 'approval_policy = "on-request"' in body
		assert 'sandbox_mode = "workspace-write"' in body


def test_helper_validate_force_full_access_env_var() -> None:
	"""VALIDATE_FORCE_FULL_ACCESS=1 elevates locally without
	GITHUB_ACTIONS. This is the documented opt-in for developers
	running validate_process.sh locally on a sandboxed VM.
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		cfg = tmp / "config.toml"
		result = _run(
			[
				"--model", "openai/gpt-5.4",
				"--reasoning", "low",
				"--catalog-path", str(tmp / "missing.json"),
				"--project-path", str(tmp),
				"--config-path", str(cfg),
			],
			env_overrides={"VALIDATE_FORCE_FULL_ACCESS": "1"},
		)
		assert result.returncode == 0
		body = _read_config(cfg)
		assert 'approval_policy = "never"' in body
		assert 'sandbox_mode = "danger-full-access"' in body


def test_helper_rejects_invalid_inputs() -> None:
	"""Validation surface — every required-arg / enum-value check exits
	rc=2 with a ::error:: annotation so a typo in a caller's invocation
	surfaces in the GHA log instead of silently writing a malformed
	config.
	"""
	with tempfile.TemporaryDirectory() as td:
		cfg = Path(td) / "config.toml"

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


def test_helper_rejects_project_paths_needing_toml_escape() -> None:
	"""Per Copilot review on PR #2196 (write_codex_config.sh:209): the
	[projects."<path>"] header writes the path verbatim. A path
	containing a double-quote, backslash, or control byte (newline,
	CR, tab, etc.) would either close the TOML key string early or
	break the header onto two lines, corrupting the config and
	defeating the trust pre-seed (codex matches paths strictly per
	codex#14599 — silently quoting/escaping won't preserve the bypass).
	The helper MUST refuse such paths with rc=2 and a ::error::
	annotation rather than emit a malformed config.

	GH-hosted runner workdirs (`/home/runner/work/<repo>/<repo>`) and
	any sane developer workspace never contain these characters, so
	the rejection only fires on operator error or environment
	corruption — exactly when failing loud is correct.
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		cfg = tmp / "config.toml"

		def _expect_reject(path_value: str, label: str) -> None:
			result = _run(
				[
					"--model", "openai/gpt-5.4",
					"--reasoning", "low",
					"--catalog-path", str(tmp / "missing.json"),
					"--project-path", path_value,
					"--config-path", str(cfg),
					"--allow-elevation", "force",
				],
			)
			assert result.returncode == 2, (
				f"{label}: expected rc=2 (rejection), got rc={result.returncode}; "
				f"stderr={result.stderr!r}"
			)
			assert "::error::" in result.stderr, (
				f"{label}: expected ::error:: annotation in stderr, got {result.stderr!r}"
			)

		_expect_reject('has"quote', "double-quote")
		_expect_reject('has\\back', "backslash")
		_expect_reject('has\nnewline', "newline")
		_expect_reject('has\rcr', "carriage-return")
		_expect_reject('has\ttab', "tab")
		_expect_reject('has\x01control', "control-byte SOH (0x01)")
		_expect_reject('has\x7fdel', "control-byte DEL (0x7F)")


def test_helper_quotes_project_path_with_spaces() -> None:
	"""Spaces are NOT in the TOML basic-string escape set (see
	https://toml.io/en/v1.0.0#string), so a path with spaces should
	pass validation and round-trip exactly into the [projects."..."]
	header. Pinning this avoids accidentally over-tightening the
	rejection rule above to also reject spaces, which would break
	any consumer-repo path that legitimately contains them
	(e.g. `/home/runner/work/My Repo/My Repo` if a future runner
	image ever shipped one).
	"""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		weird = tmp / "work dir with spaces"
		weird.mkdir()
		cfg = tmp / "config.toml"
		result = _run(
			[
				"--model", "openai/gpt-5.4",
				"--reasoning", "low",
				"--catalog-path", str(tmp / "missing.json"),
				"--project-path", str(weird),
				"--config-path", str(cfg),
				"--allow-elevation", "force",
			],
		)
		assert result.returncode == 0, f"rejected unexpectedly: {result.stderr}"
		body = _read_config(cfg)
		# Exactly one [projects."..."] header, with the spaces verbatim.
		headers = re.findall(r'^\[projects\."([^"]+)"\]$', body, flags=re.MULTILINE)
		assert headers == [str(weird)], (
			f"expected one project header for {weird!r}, got {headers!r}\n--- body ---\n{body}"
		)


def test_no_codex_exec_site_uses_deprecated_full_auto_flag() -> None:
	"""Guard against re-introduction of `--full-auto` on any `codex exec`
	site in this repo.

	Per OpenAI's Codex CLI reference, `--full-auto` is a deprecated
	compatibility shim that forces `--sandbox workspace-write` — exactly
	the mode that triggers the apply_patch hangs / sandbox-refresh
	failures (codex#19020 / #16643) the elevated config in this helper
	is supposed to escape. CLI flags override config.toml, so passing
	`--full-auto` silently re-clamps `sandbox_mode` back to
	`workspace-write` regardless of what this helper writes.

	Run 25470900024's implement banner showed `sandbox: workspace-write`
	despite PR #2196's config setting `sandbox_mode = "danger-full-access"`
	— the codex_exec sites still passed `--full-auto` until the swap to
	`--ask-for-approval never --sandbox danger-full-access`.

	Two scripts intentionally use `--sandbox read-only` and are
	exempted: review_run_reviewers.sh (reviewers must not mutate the
	tree) and summarize_reviewer_consensus.sh (consolidator likewise).
	"""
	import subprocess

	result = subprocess.run(
		[
			"git", "-C", str(REPO_ROOT), "grep", "-n", "--",
			"--full-auto",
			"--",
			".github/workflows/",
			"scripts/",
		],
		capture_output=True,
		text=True,
		check=False,
	)
	# git grep returns 1 when no matches found — that's the success case
	# for this guard. rc=0 means at least one offending site exists.
	if result.returncode == 0:
		# Filter out the historical-context comment in write_codex_config.sh
		# (the script's docstring intentionally mentions the deprecated
		# flag to document why we moved away from it).
		offenders = [
			line for line in result.stdout.splitlines()
			if not line.startswith("scripts/write_codex_config.sh:")
		]
		assert not offenders, (
			"`--full-auto` is deprecated and forces sandbox=workspace-write, "
			"defeating the elevated config write_codex_config.sh produces. "
			"Use `--ask-for-approval never --sandbox danger-full-access` "
			"instead. Offending sites:\n  " + "\n  ".join(offenders)
		)
	else:
		assert result.returncode == 1, (
			f"git grep exited unexpectedly (rc={result.returncode}); "
			f"stderr={result.stderr!r}"
		)


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
