#!/usr/bin/env python3
"""Contract tests for the smoke-only editor prompt override.

The smoke fixture appends a single bait line to tests/e2e_smoke_canary.txt
and expects review_autofix.yml's editor (gpt-5.3-codex) to remove it via
apply_patch. Across runs 25305535590 / 25308327160 / 25310399716 the
editor consistently completed with 0-byte stdout — no apply_patch
invocation, no final assistant summary — because the production editor
prompt does not force the model to commit to a tool call on a trivial
single-line removal.

This test pins three pieces of the smoke-only directive added in the
review_autofix branch:

1. .github/workflows/review_autofix.yml's smoke-detect step exports
   IS_SMOKE_TEST=true to $GITHUB_ENV when (and only when) the PR carries
   the [E2E Smoke Test] marker.

2. scripts/review_apply_fixes.sh gates a smoke-only block on
   IS_SMOKE_TEST=true and the block contains the literal
   "MUST invoke the apply_patch tool" directive plus a no-empty-exit
   prohibition.

3. The smoke block names tests/e2e_smoke_canary.txt explicitly so the
   model cannot mis-target another file.

Without this test, a later refactor that drops the directive, removes
the IS_SMOKE_TEST export, or renames the gate variable would silently
re-introduce the empty-output failure mode. Only the smoke fixture sees
this block — production PR autofixes never see the override and run
under the unchanged production prompt.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
EDITOR_SCRIPT = REPO_ROOT / "scripts" / "review_apply_fixes.sh"


def _workflow_text() -> str:
	return REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")


def _editor_script_text() -> str:
	return EDITOR_SCRIPT.read_text(encoding="utf-8")


def _smoke_detect_step_text() -> str:
	"""Return the text of the 'Detect smoke test and tune LLM settings' step."""
	lines = _workflow_text().splitlines()
	needle = "- name: Detect smoke test and tune LLM settings"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		step_indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:"):
				indent = len(candidate) - len(candidate.lstrip(" "))
				if indent == step_indent:
					end = j
					break
		return "\n".join(lines[idx:end])
	raise AssertionError(
		"'Detect smoke test and tune LLM settings' step missing from "
		"review_autofix.yml"
	)


def test_smoke_detect_exports_is_smoke_test_env() -> None:
	"""The smoke-detect step must export IS_SMOKE_TEST=true to $GITHUB_ENV.

	Without this export the editor script can't tell smoke runs from
	production runs, and the smoke-only prompt block won't render —
	silently re-introducing the empty-output failure mode.
	"""
	step_text = _smoke_detect_step_text()

	# The export must live inside the IS_SMOKE branch of the step (the
	# block guarded by `if [ "$IS_SMOKE" = "true" ]`). We don't try to
	# parse shell scope; instead we assert that the export exists AND
	# the step still has its IS_SMOKE branch — drift in either would
	# break the contract.
	assert 'echo "IS_SMOKE_TEST=true" >> "$GITHUB_ENV"' in step_text, (
		"Smoke-detect step must export IS_SMOKE_TEST=true so "
		"scripts/review_apply_fixes.sh can render the smoke-only "
		"editor prompt block. Without this export the smoke fixture "
		"keeps hitting gpt-5.3-codex's empty-output failure mode."
	)
	assert 'if [ "$IS_SMOKE" = "true" ]' in step_text, (
		"Smoke-detect step must keep the IS_SMOKE conditional — the "
		"IS_SMOKE_TEST export is meaningless if it fires on every PR."
	)
	# Production PRs must NOT receive the override. Verify the export
	# does not appear in the else branch / outside the IS_SMOKE block.
	# Cheap proxy: count occurrences and confirm only one (inside the
	# branch checked above).
	assert step_text.count('IS_SMOKE_TEST=true') == 1, (
		"IS_SMOKE_TEST=true must be exported exactly once and only "
		"inside the IS_SMOKE branch — multiple occurrences risk "
		"leaking the smoke override onto production PRs."
	)


def test_editor_script_contains_smoke_only_block() -> None:
	"""scripts/review_apply_fixes.sh must include a smoke-gated prompt block.

	Pins the directive so a refactor cannot silently drop it.
	"""
	script = _editor_script_text()

	# Gate clause — IS_SMOKE_TEST must be the trigger and must default
	# to false so unset on production runs leaves the block out.
	assert 'if [ "${IS_SMOKE_TEST:-false}" = "true" ]' in script, (
		"Smoke override must be gated on IS_SMOKE_TEST=true with a "
		"safe :-false default so production runs (where the env is "
		"unset) do not render the override."
	)

	# Section markers — give us a sanity anchor and let downstream
	# log-analysis tools grep for whether the override fired.
	assert "=== E2E SMOKE TEST OVERRIDE — READ FIRST ===" in script, (
		"Smoke override must keep its opening section marker so "
		"editor_prompt.txt artifacts and live logs are searchable for "
		"whether the override was emitted on a given run."
	)
	assert "=== END E2E SMOKE TEST OVERRIDE ===" in script, (
		"Smoke override must keep its closing section marker."
	)

	# Mandatory directives — these are what actually move the model
	# off the empty-completion failure mode. Each is asserted by
	# substring so wording can drift slightly without breaking the
	# test, but the load-bearing phrases must remain.
	assert "MUST invoke the apply_patch tool" in script, (
		"Smoke override must explicitly mandate apply_patch — codex "
		"on the production prompt repeatedly exits without invoking "
		"the tool on this single-line removal (run 25310399716)."
	)
	assert "Do NOT exit without calling apply_patch" in script, (
		"Smoke override must explicitly forbid the empty-completion "
		"path so the 'I will apply_patch ...' announce-without-invoke "
		"variant (run 25249170035 / PR #1982) is also caught."
	)
	assert "smoke-test FAILURE" in script, (
		"Smoke override must explain the consequence of skipping "
		"apply_patch so the model treats the directive as a hard "
		"requirement, not a suggestion."
	)

	# Target file must be named explicitly so the model cannot
	# mis-target a sibling fixture (there is only ever one canary).
	assert "tests/e2e_smoke_canary.txt" in script, (
		"Smoke override must name the canary file explicitly — "
		"without it the model could plausibly pick a different "
		"e2e fixture path."
	)

	# Scope guard — the smoke fixture must not let the model fan out
	# into other files (the bait is single-line, single-file).
	assert (
		"Do not modify any file other than tests/e2e_smoke_canary.txt"
		in script
	), (
		"Smoke override must restrict edits to the canary file so "
		"the editor cannot opportunistically touch unrelated paths "
		"on a smoke run."
	)


def test_smoke_block_position_before_input_files() -> None:
	"""The smoke override must precede INPUT FILES in the editor prompt body.

	Codex models bias toward instructions near the top of the dynamic
	prompt and the very end. Putting the override before INPUT FILES
	(rather than buried below the reviewer / consolidator sections)
	keeps it adjacent to the cached pre_assembled_static.txt block —
	the first thing the model encounters in the dynamic prompt section.
	"""
	script = _editor_script_text()
	override_idx = script.find("=== E2E SMOKE TEST OVERRIDE — READ FIRST ===")
	# We want the LAST occurrence of `INPUT FILES\nRead the following`
	# in the editor heredoc (there are unrelated ones elsewhere).
	# Anchoring on `Read the following files:` from line 151 is
	# specific enough.
	input_files_idx = script.find("INPUT FILES\nRead the following files:")
	assert override_idx != -1, "smoke override marker not found in script"
	assert input_files_idx != -1, "INPUT FILES anchor not found in script"
	assert override_idx < input_files_idx, (
		"Smoke override must appear BEFORE the INPUT FILES section "
		"of the editor prompt body so the directive is the first "
		"role-relevant content the model encounters in the dynamic "
		"prompt section."
	)


def main() -> int:
	test_smoke_detect_exports_is_smoke_test_env()
	test_editor_script_contains_smoke_only_block()
	test_smoke_block_position_before_input_files()
	print("OK: review_autofix smoke editor override contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
