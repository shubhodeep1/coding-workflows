#!/usr/bin/env python3
"""Contract test for the summariser/consolidator sandbox-mode pin.

scripts/summarize_reviewer_consensus.sh invokes codex with
`--sandbox read-only` (line ~275). However, run 25370115370
demonstrated that the inherited config.toml's
`sandbox_mode = "workspace-write"` takes precedence over the CLI
flag — the consolidator session header reported `sandbox:
workspace-write [...] (network access enabled)` and the model used
its write access to overwrite tests/e2e_smoke_canary.txt via a
`printf >` shell redirect. That happened to fix the smoke fixture
this once, but is a latent foot-gun: a future consolidator run
could rewrite arbitrary repo files with no allowlist guard around it
(the editor has RESOLVER_ALLOWLIST_FILE / check_resolver_diff.sh,
the consolidator does not).

The fix sed-pins `sandbox_mode = "read-only"` in the cloned
config.toml (both the top-level and `.codex/` nested layouts) so the
inherited config no longer escalates the sandbox above the CLI
flag's intent.

This test pins:
1. The sed pin exists and runs after the model_reasoning_effort sed.
2. It handles both layouts (top-level config.toml and .codex/config.toml).
3. It works whether the inherited config already contains sandbox_mode
   (replace) or not (append).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SUMMARISER_SCRIPT = REPO_ROOT / "scripts" / "summarize_reviewer_consensus.sh"


def _summariser_text() -> str:
	return SUMMARISER_SCRIPT.read_text(encoding="utf-8")


def test_sandbox_mode_is_pinned_to_read_only_in_cloned_config() -> None:
	"""The cloned summariser CODEX_HOME must have sandbox_mode rewritten
	to 'read-only' so the inherited config.toml cannot override the
	CLI's --sandbox read-only flag."""
	src = _summariser_text()

	# The script must contain a sandbox_mode pin alongside the existing
	# model_reasoning_effort patch.
	assert 'sandbox_mode = "read-only"' in src, (
		"summariser must pin sandbox_mode = \"read-only\" in the cloned "
		"config.toml — the CLI's --sandbox read-only flag is overridden "
		"by the inherited config (run 25370115370 demonstrated). Without "
		"the pin a consolidator run can write arbitrary repo files."
	)

	# Both replace (sed) and append (printf >>) paths must exist so the
	# pin works whether the inherited config already mentions sandbox_mode
	# or not.
	assert re.search(
		r"sed -i 's\|.*sandbox_mode.*\|sandbox_mode = \"read-only\"\|'",
		src,
	), (
		"summariser must have a sed replacement for an existing "
		"sandbox_mode line in the cloned config.toml."
	)
	assert 'sandbox_mode = "read-only"' in src and 'printf' in src and '>>' in src, (
		"summariser must have an append fallback (printf '...' >> cfg) "
		"for cloned configs that don't already declare sandbox_mode."
	)

	# The pin must run inside the same loop that patches reasoning effort
	# so both top-level and .codex/ layouts get pinned.
	assert "summariser_codex_home" in src, "expected summariser_codex_home var"
	assert "model_reasoning_effort" in src, (
		"existing reasoning-effort patch must remain — the sandbox pin "
		"is added alongside it, not as a replacement."
	)


def test_sandbox_pin_replaces_existing_sandbox_mode_line() -> None:
	"""End-to-end check: extract the pin block from the script and run
	it against a synthetic config.toml that has
	sandbox_mode = "workspace-write" already set. The pin must rewrite
	it to "read-only"."""
	src = _summariser_text()

	# Extract the for-loop that patches the cloned config.
	loop_match = re.search(
		r"for cfg in \"\$\{summariser_codex_home\}/config\.toml\".*?\ndone",
		src,
		flags=re.DOTALL,
	)
	assert loop_match, "could not locate the cfg-patching for-loop in summariser"
	loop_body = loop_match.group(0)

	# We can't run the bash script directly in this test (it depends on
	# many external vars), but we can run just the sandbox-pin lines.
	# Pull out the if/grep/sed/else/printf block from the loop.
	sandbox_block_match = re.search(
		r"if grep -qE '\^\[\[:space:\]\]\*sandbox_mode.*?'.*?fi",
		loop_body,
		flags=re.DOTALL,
	)
	assert sandbox_block_match, (
		"could not locate the sandbox_mode if/grep/sed/printf block "
		"inside the cfg-patching for-loop"
	)
	sandbox_block = sandbox_block_match.group(0)

	# Test 1: existing sandbox_mode line → should be replaced.
	with tempfile.TemporaryDirectory() as tmp:
		cfg_path = Path(tmp) / "config.toml"
		cfg_path.write_text(
			'model = "openai/gpt-5.4-mini"\n'
			'sandbox_mode = "workspace-write"\n'
			'model_reasoning_effort = "medium"\n',
			encoding="utf-8",
		)
		# Wrap the block so it actually executes against this cfg.
		wrapper = f'cfg="{cfg_path}"\n' + sandbox_block
		result = subprocess.run(
			["bash", "-c", wrapper],
			capture_output=True,
			text=True,
		)
		assert result.returncode == 0, (
			f"sandbox-pin block failed on existing sandbox_mode case: "
			f"stderr={result.stderr!r}"
		)
		patched = cfg_path.read_text(encoding="utf-8")
		assert 'sandbox_mode = "read-only"' in patched, (
			f"sandbox-pin block must rewrite the existing line; got:\n{patched}"
		)
		assert 'sandbox_mode = "workspace-write"' not in patched, (
			f"original workspace-write line must be removed; got:\n{patched}"
		)


def test_sandbox_pin_appends_when_line_absent() -> None:
	"""End-to-end check: against a config.toml without any sandbox_mode
	line, the pin block must append `sandbox_mode = "read-only"`."""
	src = _summariser_text()
	loop_match = re.search(
		r"for cfg in \"\$\{summariser_codex_home\}/config\.toml\".*?\ndone",
		src,
		flags=re.DOTALL,
	)
	assert loop_match
	sandbox_block_match = re.search(
		r"if grep -qE '\^\[\[:space:\]\]\*sandbox_mode.*?'.*?fi",
		loop_match.group(0),
		flags=re.DOTALL,
	)
	assert sandbox_block_match
	sandbox_block = sandbox_block_match.group(0)

	with tempfile.TemporaryDirectory() as tmp:
		cfg_path = Path(tmp) / "config.toml"
		cfg_path.write_text(
			'model = "openai/gpt-5.4-mini"\n'
			'model_reasoning_effort = "medium"\n',
			encoding="utf-8",
		)
		wrapper = f'cfg="{cfg_path}"\n' + sandbox_block
		result = subprocess.run(
			["bash", "-c", wrapper],
			capture_output=True,
			text=True,
		)
		assert result.returncode == 0, (
			f"sandbox-pin block failed on absent-sandbox case: "
			f"stderr={result.stderr!r}"
		)
		patched = cfg_path.read_text(encoding="utf-8")
		assert 'sandbox_mode = "read-only"' in patched, (
			f"sandbox-pin block must append the line when absent; got:\n{patched}"
		)


def main() -> int:
	test_sandbox_mode_is_pinned_to_read_only_in_cloned_config()
	test_sandbox_pin_replaces_existing_sandbox_mode_line()
	test_sandbox_pin_appends_when_line_absent()
	print(
		"OK: summariser sandbox_mode read-only pin contract assertions hold"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
