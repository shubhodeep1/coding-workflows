#!/usr/bin/env python3
"""Contract tests for the smoke-only conflict-resolver prompt override.

The smoke fixture lands a conflicting one-line edit on `main` so a
`git merge --no-ff origin/main` over the smoke PR's HEAD leaves Git
merge conflict markers in tests/e2e_smoke_canary.txt. review_autofix.yml's
conflict-resolver step (currently `openai/gpt-5.5`; observed below on
the legacy editor default) is then expected to remove
the markers and keep the HEAD-side `run_id:` value.

On run 25324565713 / PR #2094 the resolver hit the documented
the legacy editor default's empty-stdout failure mode in all 3 retry attempts: each
codex exec read the file, then exited cleanly with 0 bytes on stdout,
the soft-validation marker scan never ran, and the
[ai-merge-resolve] commit was aborted. PR #2059 had already pinned
the resolver to medium reasoning to prevent the editor's
reasoning=none/low override from starving it, but at medium reasoning
the legacy editor default still hit the failure mode on the
trivial canary conflict — the pattern PR #2086 documented (and fixed
for the editor) on the same model on the same fixture. The override
is kept as defense-in-depth on the default editor model.

This test pins the conflict-resolver analog of #2086:

1. scripts/review_conflict_prepare.sh gates a smoke-only block on
   IS_SMOKE_TEST=true AND the canary file having live Git conflict
   markers, and the block contains the literal "MUST invoke the
   apply_patch tool" directive plus a no-empty-exit prohibition.

2. The smoke block names tests/e2e_smoke_canary.txt explicitly so the
   model cannot mis-target another file.

3. Production runs (IS_SMOKE_TEST unset) and integration-sync runs
   that don't touch the canary produce byte-identical conflict-resolver
   prompts to the pre-fix output.

Without this test, a later refactor that drops the directive, removes
the IS_SMOKE_TEST gate, or weakens the canary marker check would
silently re-introduce the empty-output failure mode on smoke runs.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
PREPARE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"
CONFLICT_RESOLVER_TPL = REPO_ROOT / "prompts" / "conflict-resolver.txt"


def _workflow_text() -> str:
	return REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")


def _prepare_script_text() -> str:
	return PREPARE_SCRIPT.read_text(encoding="utf-8")


def _smoke_detect_step_text() -> str:
	"""Return the text of the 'Detect smoke test and tune LLM settings' step.

	Mirrors the same helper in test_review_autofix_smoke_editor_override.py
	so the IS_SMOKE_TEST export assertion can be scoped to the relevant
	step rather than passing on a raw substring match anywhere in the
	workflow file (a relocation to an unconditional step would otherwise
	silently false-pass).
	"""
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
	"""review_autofix.yml's smoke-detect step must export IS_SMOKE_TEST.

	The conflict-resolver step inherits IS_SMOKE_TEST from $GITHUB_ENV
	(the step's own env: block only sets GH_PAT and the reasoning-effort
	pin). If the export ever moves out of the smoke branch, every smoke
	run would re-introduce the empty-output failure mode in the resolver.

	The assertion is scoped to the smoke-detect step text — not a global
	substring match — so a relocation of the export into an
	unconditional step would fail this test rather than silently
	false-passing while breaking conditional scoping.
	"""
	step_text = _smoke_detect_step_text()
	assert 'echo "IS_SMOKE_TEST=true" >> "$GITHUB_ENV"' in step_text, (
		"review_autofix.yml's smoke-detect step must export IS_SMOKE_TEST=true "
		"so scripts/review_conflict_prepare.sh can render the smoke-only "
		"conflict-resolver prompt block. Without this export the resolver "
		"keeps hitting the legacy editor default's empty-output failure mode on canary "
		"conflicts (run 25324565713)."
	)
	assert 'if [ "$IS_SMOKE" = "true" ]' in step_text, (
		"Smoke-detect step must keep the IS_SMOKE conditional — the "
		"IS_SMOKE_TEST export is meaningless if it fires on every PR."
	)
	# Production PRs must NOT receive the override. Verify the export
	# does not appear in the else branch / outside the IS_SMOKE block.
	assert step_text.count('IS_SMOKE_TEST=true') == 1, (
		"IS_SMOKE_TEST=true must be exported exactly once and only "
		"inside the IS_SMOKE branch — multiple occurrences risk "
		"leaking the smoke override onto production PRs."
	)


def test_prepare_script_contains_smoke_only_block() -> None:
	"""scripts/review_conflict_prepare.sh must include a smoke-gated block.

	Pins the directive so a refactor cannot silently drop it.
	"""
	script = _prepare_script_text()

	# Gate clause — IS_SMOKE_TEST must be the trigger and must default
	# to false so unset on production runs leaves the block out.
	assert 'if [ "${IS_SMOKE_TEST:-false}" = "true" ]' in script, (
		"Smoke override must be gated on IS_SMOKE_TEST=true with a "
		"safe :-false default so production runs (where the env is "
		"unset) do not render the override."
	)

	# Marker-presence guard — the override must additionally check
	# that the canary file actually contains live Git conflict markers
	# before rendering. Integration-sync runs may add other files to
	# the resolver's working set via fingerprint expansion; without
	# this guard the override would render against a conflict that
	# doesn't touch the canary at all and mis-direct the model.
	assert "CONFLICT_RESOLVER_BODY_RENDER_SMOKE=false" in script, (
		"Smoke override must default to render=false and only flip "
		"true on a positive marker check — otherwise non-canary "
		"smoke conflicts would render the override against an "
		"unrelated conflict."
	)
	assert (
		"grep -qE '^(<<<<<<< |>>>>>>> )' \"${CONFLICT_RESOLVER_CANARY_PATH}\""
	) in script, (
		"Smoke override must check the canary for the canonical Git "
		"conflict-marker prefixes (`<<<<<<< ` / `>>>>>>> `) before "
		"rendering. The marker scan must scope to the canary path so "
		"a stray marker in an unrelated file cannot trigger the "
		"override."
	)
	assert (
		'CONFLICT_RESOLVER_CANARY_PATH="tests/e2e_smoke_canary.txt"'
		in script
	), (
		"Smoke override must scope the marker check to the canary "
		"path so the override only fires when the canary itself is "
		"the conflicted file."
	)

	# Section markers — give us a sanity anchor and let downstream
	# log-analysis tools grep for whether the override fired.
	assert "=== E2E SMOKE TEST OVERRIDE — READ FIRST ===" in script, (
		"Smoke override must keep its opening section marker so "
		"conflict_resolver_prompt.txt artifacts and live logs are "
		"searchable for whether the override was emitted."
	)
	assert "=== END E2E SMOKE TEST OVERRIDE ===" in script, (
		"Smoke override must keep its closing section marker."
	)

	# Mandatory directives — these are what actually move the model
	# off the empty-completion failure mode. Each is asserted by
	# substring so wording can drift slightly, but the load-bearing
	# phrases must remain.
	#
	# History: PR #2095 originally mandated `apply_patch`, but
	# openai/codex#11151 documents that the legacy editor slug doesn't
	# get matched into the apply_patch-providing branch in codex's
	# offline model_info fallback, so the directive could not be
	# satisfied even when the model wanted to. The override now allows
	# either `apply_patch` or a direct shell write (`printf '...' >
	# path`) — same escape hatch implement.yml's prompt has documented
	# since #1933 — so the model has a reliable path to the file.
	assert "MUST write tests/e2e_smoke_canary.txt to disk" in script, (
		"Smoke override must explicitly mandate writing the canary file "
		"this turn — without the directive codex on the production "
		"conflict-resolver prompt repeatedly exits without writing "
		"(run 25324565713 / run 25370025320)."
	)
	assert "apply_patch" in script and "printf" in script, (
		"Smoke override must offer both apply_patch and a printf shell "
		"write as acceptable means — the legacy editor default flaked on the "
		"former (openai/codex#11151) but reliably executes the latter."
	)
	assert "Do NOT exit without writing the file" in script, (
		"Smoke override must explicitly forbid the empty-completion "
		"path so the 'I will apply_patch ...' / 'I will write ...' "
		"announce-without-invoke variant is also caught."
	)
	assert "smoke-test FAILURE" in script, (
		"Smoke override must explain the consequence of skipping the "
		"write so the model treats the directive as a hard "
		"requirement, not a suggestion."
	)

	# Target file must be named explicitly so the model cannot
	# mis-target a sibling fixture.
	assert "tests/e2e_smoke_canary.txt" in script, (
		"Smoke override must name the canary file explicitly — "
		"without it the model could plausibly pick a different "
		"fixture path."
	)

	# Scope guard — the smoke fixture must not let the model fan out
	# into other files (the conflict is single-file).
	assert (
		"Do not modify any file other than tests/e2e_smoke_canary.txt"
		in script
	), (
		"Smoke override must restrict edits to the canary file so "
		"the resolver cannot opportunistically touch unrelated paths "
		"on a smoke run."
	)

	# Resolution direction must be unambiguous: keep HEAD's run_id.
	# Without this the model could keep both sides or pick the bait.
	assert "the HEAD side's `run_id:` value" in script, (
		"Smoke override must tell the model which side of the "
		"conflict to keep — the smoke fixture seeds an `alt-<run_id>` "
		"bait on the main side, so HEAD is the authentic value."
	)


def _render_conflict_resolver_prompt(
	tmp_p: Path,
	*,
	is_smoke: str | None,
	canary_contents: str,
) -> tuple[str, str]:
	"""Execute the prompt-render block from review_conflict_prepare.sh.

	Sets up the minimal env and working tree the rendering block needs,
	runs it in `tmp_p`, and returns the resulting prompt text and its
	SHA-256.

	Caller-supplied `tmp_p` is load-bearing: byte-identical assertions
	across multiple calls require RUNTIME_DIR (and the rendered prompt
	output path) to be stable across calls — divergence otherwise
	reflects path differences rather than the override structure.
	"""
	import hashlib
	import os
	import shutil
	import subprocess

	runtime = tmp_p / "runtime"
	runtime.mkdir(exist_ok=True)
	(tmp_p / "tests").mkdir(exist_ok=True)
	(tmp_p / "tests" / "e2e_smoke_canary.txt").write_text(
		canary_contents, encoding="utf-8"
	)
	prompts_dir = tmp_p / "prompts"
	prompts_dir.mkdir(exist_ok=True)
	# Copy only the generic conflict-resolver template; the integration-sync
	# template is exercised separately and the test pins TARGET_BRANCH to
	# something outside the orchestrator/project-* pattern.
	shutil.copy(CONFLICT_RESOLVER_TPL, prompts_dir / "conflict-resolver.txt")

	prompt_file = runtime / "conflict_resolver_prompt.txt"
	env = os.environ.copy()
	env.update({
		"SUPPORT_PROMPTS_DIR": str(prompts_dir),
		"RUNTIME_DIR": str(runtime),
		"CONFLICT_RESOLVER_PROMPT_FILE": str(prompt_file),
		# Empty / non-orchestrator values so the integration-sync
		# branch is skipped and the python one-liner renders the
		# generic template.
		"TARGET_BRANCH": "ai/issue-9999",
		"HEAD_REF": "ai/issue-9999",
		"INTEGRATION_TRACKING_NUM": "",
		"INTEGRATION_TRACKING_TITLE": "",
		"INTEGRATION_TRACKING_BODY": "",
		"INTEGRATION_MERGED_SUB_ISSUES_LIST": "",
		"INTEGRATION_MERGED_SUB_ISSUE_COUNT": "0",
		# CONFLICTED_FILES_COUNT/LIST are computed earlier in the
		# script; the rendering block only references them via the
		# python one-liner's env lookups, so we pin them here.
		"CONFLICTED_FILES_COUNT": "1",
		"CONFLICTED_FILES_LIST": "          - tests/e2e_smoke_canary.txt",
		"FP_VIOLATED_FILES_LIST": "",
		"INTEGRATION_FINGERPRINTS_FILE": "",
	})
	if is_smoke is None:
		env.pop("IS_SMOKE_TEST", None)
	else:
		env["IS_SMOKE_TEST"] = is_smoke

	script_text = PREPARE_SCRIPT.read_text(encoding="utf-8")
	lines = script_text.splitlines(keepends=True)
	# Slice the prompt-render block: from the env-prefixed `PROMPT_TPL=...`
	# line that drives the python one-liner through the end of the
	# smoke-override post-processing block. Anchored on stable lines
	# present in the committed script.
	start = next(
		i for i, ln in enumerate(lines)
		if ln.rstrip("\n") == 'PROMPT_TPL="${PROMPT_TPL}" \\'
	)
	end = next(
		i for i, ln in enumerate(lines[start:], start=start)
		if ln.rstrip("\n") == "fi"
		and i > start
		and "_smoke_override_tmp" in "".join(lines[start : i + 1])
	) + 1
	block = (
		'PROMPT_TPL="${SUPPORT_PROMPTS_DIR}/conflict-resolver.txt"\n'
		"set -euo pipefail\n"
		+ "".join(lines[start:end])
	)
	subprocess.run(
		["bash", "-c", block],
		cwd=str(tmp_p),
		env=env,
		check=True,
		capture_output=True,
	)
	body_text = prompt_file.read_text(encoding="utf-8")
	return body_text, hashlib.sha256(body_text.encode("utf-8")).hexdigest()


def test_production_prompt_byte_identical_when_smoke_unset_or_no_markers() -> None:
	"""Production runs MUST produce byte-identical conflict-resolver prompts.

	Two cases must yield the same SHA-256 because neither should render
	the smoke override:
	  1. IS_SMOKE_TEST unset (real PR autofix with conflicts).
	  2. IS_SMOKE_TEST=true but the canary has no Git conflict markers
	     (a smoke run whose conflict is in a different file — possible
	     in integration-sync's fingerprint-expanded resolver set).

	Shared tempdir is load-bearing: identical RUNTIME_DIR /
	CONFLICT_RESOLVER_PROMPT_FILE paths across calls keep hash
	divergence as a signal of structural drift, not path differences.
	"""
	import tempfile

	clean_canary = "status: ok\nrun_id: 12345\nupdated-by: ai-pipeline\n"
	with tempfile.TemporaryDirectory() as tmp:
		tmp_p = Path(tmp)
		_, sha_prod = _render_conflict_resolver_prompt(
			tmp_p, is_smoke=None, canary_contents=clean_canary
		)
		_, sha_smoke_no_markers = _render_conflict_resolver_prompt(
			tmp_p, is_smoke="true", canary_contents=clean_canary
		)
		assert sha_prod == sha_smoke_no_markers, (
			f"Conflict-resolver prompt must be byte-identical between "
			f"production (IS_SMOKE_TEST unset) and the smoke-without-canary-"
			f"conflict case. Got {sha_prod} vs {sha_smoke_no_markers}. The "
			f"smoke override is leaking into a non-canary conflict."
		)


def test_smoke_override_renders_only_when_canary_has_markers() -> None:
	"""Smoke override must render iff IS_SMOKE_TEST=true AND canary has markers."""
	import tempfile

	clean_canary = "status: ok\nrun_id: 12345\nupdated-by: ai-pipeline\n"
	conflict_canary = (
		"status: ok\n"
		"<<<<<<< HEAD\n"
		"run_id: 25324103531\n"
		"=======\n"
		"run_id: alt-25324103531\n"
		">>>>>>> origin/main\n"
		"updated-by: ai-pipeline\n"
	)
	with tempfile.TemporaryDirectory() as tmp:
		tmp_p = Path(tmp)
		body_with_markers, sha_with_markers = _render_conflict_resolver_prompt(
			tmp_p, is_smoke="true", canary_contents=conflict_canary
		)
		_, sha_no_markers = _render_conflict_resolver_prompt(
			tmp_p, is_smoke="true", canary_contents=clean_canary
		)

		assert sha_with_markers != sha_no_markers, (
			"With IS_SMOKE_TEST=true and conflict markers in the "
			"canary, the prompt must differ from the no-markers prompt "
			"— the override should be prepended."
		)
		assert (
			"=== E2E SMOKE TEST OVERRIDE — READ FIRST ===" in body_with_markers
		), (
			"Smoke run with markers in the canary must include the "
			"override section marker in the rendered prompt."
		)
		assert body_with_markers.startswith("=== E2E SMOKE TEST OVERRIDE"), (
			"Override must be the very first content of the prompt "
			"when rendered — late placement weakens the directive."
		)
		assert "apply_patch" in body_with_markers and "printf" in body_with_markers, (
			"Override must mention both apply_patch and the printf shell "
			"escape hatch in the rendered output — the legacy editor default needed "
			"the latter when the apply_patch tool is unavailable on its "
			"slug (openai/codex#11151)."
		)


def main() -> int:
	test_smoke_detect_exports_is_smoke_test_env()
	test_prepare_script_contains_smoke_only_block()
	test_production_prompt_byte_identical_when_smoke_unset_or_no_markers()
	test_smoke_override_renders_only_when_canary_has_markers()
	print(
		"OK: review_autofix smoke conflict-resolver override contract "
		"assertions hold"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
