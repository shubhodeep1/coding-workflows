#!/usr/bin/env python3
"""Contract tests for the smoke-only editor prompt override.

The smoke fixture appends a single bait line to tests/e2e_smoke_canary.txt
and expects review_autofix.yml's editor (currently `openai/gpt-5.6-sol`;
historically observed below on the legacy editor default)
to remove it via apply_patch. Across runs 25305535590 / 25308327160 /
25310399716 the editor consistently completed with 0-byte stdout — no
apply_patch invocation, no final assistant summary — because the
production editor prompt does not force the model to commit to a tool
call on a trivial single-line removal. The override is kept as
defense-in-depth on the default editor model.

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
		"keeps hitting the legacy editor default's empty-output failure mode."
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

	# Bait-presence guard — the override must additionally check that
	# the canary file actually contains an injected bait line before
	# rendering. internal-review.yml triggers review_autofix on
	# pull_request:opened, which fires the FIRST review_autofix run
	# the moment the smoke PR is created — BEFORE
	# test-and-mark-stable.yml's Phase 3c appends the bait commit. On
	# that first round the canary is the legitimate 3-line file and
	# the override would force a guaranteed-false apply_patch (Copilot
	# review on PR #2086). The grep must match the marker prefix
	# emitted by Phase 3c at L1007 and must scope to the canary path.
	assert "EDITOR_BODY_RENDER_SMOKE=false" in script, (
		"Smoke override must default to render=false and only flip "
		"true on a positive bait check — otherwise the pull_request:"
		"opened first round renders the override against a legitimate "
		"3-line canary."
	)
	assert (
		"grep -qE '^# E2E_EDITOR_BAIT_[0-9]+:' \"${CANARY_PATH}\""
	) in script, (
		"Smoke override must check for the bait marker prefix on "
		"tests/e2e_smoke_canary.txt before rendering. The marker "
		"shape (`# E2E_EDITOR_BAIT_<digits>:`) is fixed by "
		"test-and-mark-stable.yml's Phase 3c — drift between the "
		"two would either render the override on every smoke run "
		"(re-introducing the false-task bug) or never render it "
		"(re-introducing the empty-output failure)."
	)
	assert 'CANARY_PATH="tests/e2e_smoke_canary.txt"' in script, (
		"Smoke override must scope the bait check to the canary "
		"path so a stray match in a different file cannot trigger "
		"the override."
	)

	# Byte-identical-on-production guarantee — when the override does
	# not render, the editor prompt body must be byte-for-byte the
	# same as the pre-#2086 implementation. The structural way to
	# guarantee this is to assemble the body as
	#   { conditional smoke block; cat <<__EDITOR_PROMPT__ … }
	#     > "${EDITOR_PROMPT_BODY_FILE}"
	# rather than embedding $(if ...) inside the heredoc — the latter
	# expands to an empty string but leaves a trailing newline that
	# shifts every production prompt's bytes/sha (Copilot review on
	# PR #2086).
	assert '} > "${EDITOR_PROMPT_BODY_FILE}"' in script, (
		"Editor prompt body must be assembled via a brace group "
		"redirected to EDITOR_PROMPT_BODY_FILE, not via a heredoc "
		"with embedded $(if ...) — the latter leaks a leading blank "
		"line into production prompts and breaks the prompt-cache "
		"key stability."
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
	#
	# History: PR #2086 originally mandated `apply_patch`, but
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
		"prompt repeatedly exits without writing on this single-line "
		"removal (run 25310399716)."
	)
	assert "apply_patch" in script and "printf" in script, (
		"Smoke override must offer both apply_patch and a printf shell "
		"write as acceptable means — the legacy editor default flaked on the "
		"former (openai/codex#11151) but reliably executes the latter."
	)
	assert "Do NOT exit without writing the file" in script, (
		"Smoke override must explicitly forbid the empty-completion "
		"path so the 'I will apply_patch ...' / 'I will write ...' "
		"announce-without-invoke variant (run 25249170035 / PR #1982) "
		"is also caught."
	)
	assert "smoke-test FAILURE" in script, (
		"Smoke override must explain the consequence of skipping the "
		"write so the model treats the directive as a hard "
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
	# Anchor on the new INPUT FILE CONTENTS section header (replaced the
	# earlier "INPUT FILES / Read the following files:" anti-pattern with
	# embedded file contents to stop the legacy editor default from burning its tool
	# budget on exec reads — see PR linked in the section docstring).
	input_files_idx = script.find("INPUT FILE CONTENTS")
	assert override_idx != -1, "smoke override marker not found in script"
	assert input_files_idx != -1, "INPUT FILE CONTENTS anchor not found in script"
	assert override_idx < input_files_idx, (
		"Smoke override must appear BEFORE the INPUT FILE CONTENTS section "
		"of the editor prompt body so the directive is the first "
		"role-relevant content the model encounters in the dynamic "
		"prompt section."
	)


def _render_editor_prompt_body_in(
	tmp_p: Path,
	*,
	is_smoke: str | None,
	canary_contents: str,
) -> tuple[str, str]:
	"""Execute the prompt-body assembly block from review_apply_fixes.sh.

	Sets up minimal stub env vars that the heredoc references by name
	(the heredoc embeds `${VAR}` paths via the unquoted `<<__EDITOR_PROMPT__`
	form), runs the assembly block in `tmp_p`, and returns the
	resulting body text and its SHA-256.

	Caller-supplied `tmp_p` is load-bearing: the byte-identical
	assertions across multiple calls require RUNTIME_DIR (and every
	derived path embedded in the body) to be stable across calls,
	otherwise hash divergence reflects path differences rather than
	the override structure under test.
	"""
	import hashlib
	import os
	import subprocess

	runtime = tmp_p / "runtime"
	runtime.mkdir(exist_ok=True)
	(runtime / "previous_reviews").mkdir(exist_ok=True)
	(tmp_p / "tests").mkdir(exist_ok=True)
	(tmp_p / "tests" / "e2e_smoke_canary.txt").write_text(
		canary_contents, encoding="utf-8"
	)
	for name in ("floor_tags.txt", "review_issues.txt", "ledger_status.txt"):
		(runtime / name).touch()
	# Populate every input artifact the heredoc passes to _embed_input_file
	# with a recognisable sentinel.  This lets the body-rendering tests
	# verify that (a) the embedding helper is actually invoked and (b)
	# it emits the file content into the prompt body — without these
	# fixtures the helper would emit "(empty)" markers and a regression
	# in the inlining path could go undetected (Copilot review on PR #2149).
	_input_fixtures = {
		"pr_meta.json": (
			'{"title":"E2E sentinel PR","body":"PR_META_SENTINEL_VALUE",'
			'"state":"open"}\n'
		),
		"pr_diff.patch": "diff --git a/sentinel b/sentinel\nPR_DIFF_SENTINEL\n",
		"last_run_diff.patch": "LAST_RUN_DIFF_SENTINEL\n",
		"last_run_changed_files.txt": "LAST_RUN_CHANGED_SENTINEL\n",
		"pr_changed_files.txt": "PR_CHANGED_SENTINEL\n",
		"last_commit_stat.txt": "LAST_COMMIT_STAT_SENTINEL\n",
		"reviewer_consensus.txt": "REVIEWER_CONSENSUS_SENTINEL\n",
		"pr_all_comments_context.txt": "PR_ALL_COMMENTS_SENTINEL\n",
		"pr_check_runs_context.txt": "PR_CHECK_RUNS_SENTINEL\n",
		"symbol_diff_summary.txt": "SYMBOL_DIFF_SENTINEL\n",
		"linked_issue_context.txt": "LINKED_ISSUE_SENTINEL\n",
	}
	for fname, contents in _input_fixtures.items():
		(runtime / fname).write_text(contents, encoding="utf-8")
	(runtime / "reviewer_bundle.txt").write_text(
		"REVIEWER_BUNDLE_SENTINEL\n", encoding="utf-8"
	)
	body_file = runtime / "editor_prompt_body.txt"
	env = os.environ.copy()
	env.update({
		"RUNTIME_DIR": str(runtime),
		"PREVIOUS_REVIEWS_DIR": str(runtime / "previous_reviews"),
		"PR_META_FILE": str(runtime / "pr_meta.json"),
		"PR_DIFF_FILE": str(runtime / "pr_diff.patch"),
		"LAST_RUN_DIFF_FILE": str(runtime / "last_run_diff.patch"),
		"LAST_RUN_CHANGED_FILES_FILE": str(runtime / "last_run_changed_files.txt"),
		"PR_CHANGED_FILES_FILE": str(runtime / "pr_changed_files.txt"),
		"LAST_COMMIT_STAT_FILE": str(runtime / "last_commit_stat.txt"),
		"REVIEWER_CONSENSUS_FILE": str(runtime / "reviewer_consensus.txt"),
		"PR_ALL_COMMENTS_CONTEXT_FILE": str(runtime / "pr_all_comments_context.txt"),
		"PR_CHECK_RUNS_CONTEXT_FILE": str(runtime / "pr_check_runs_context.txt"),
		"SYMBOL_DIFF_SUMMARY_FILE": str(runtime / "symbol_diff_summary.txt"),
		"LINKED_ISSUE_CONTEXT_FILE": str(runtime / "linked_issue_context.txt"),
		"FLOOR_TAGS_FILE": str(runtime / "floor_tags.txt"),
		"REVIEW_ISSUES_FILE": str(runtime / "review_issues.txt"),
		"LEDGER_STATUS_FILE": str(runtime / "ledger_status.txt"),
		"EDITOR_PROMPT_BODY_FILE": str(body_file),
		"HAS_PR_DIFF": "true",
		"PR_DIFF_SOURCE": "gh_pr_diff",
	})
	if is_smoke is None:
		env.pop("IS_SMOKE_TEST", None)
	else:
		env["IS_SMOKE_TEST"] = is_smoke
	script_text = (REPO_ROOT / "scripts" / "review_apply_fixes.sh").read_text(
		encoding="utf-8"
	)
	lines = script_text.splitlines(keepends=True)
	start = next(
		i for i, ln in enumerate(lines)
		if ln.startswith('echo "Artifacts: floor_tags=$')
	)
	end = next(
		i for i, ln in enumerate(lines)
		if ln.startswith('} > "${EDITOR_PROMPT_BODY_FILE}"')
	) + 1
	# Source gh_helpers.sh in the subshell so the heredoc's
	# $(_embed_input_file ...) / $(_init_prompt_budget) / $(_cleanup_prompt_budget)
	# substitutions resolve to the production helpers — without this the
	# fragment is missing those definitions and the body would render with
	# empty inline sections, masking regressions in the embedding/budget
	# logic (Copilot review on PR #2149).
	helpers_path = REPO_ROOT / "scripts" / "gh_helpers.sh"
	preamble = f'set -euo pipefail\nsource {helpers_path!s}\n'
	block = preamble + "".join(lines[start:end])
	subprocess.run(
		["bash", "-c", block],
		cwd=str(tmp_p),
		env=env,
		check=True,
		capture_output=True,
	)
	body_text = body_file.read_text(encoding="utf-8")
	return body_text, hashlib.sha256(body_text.encode("utf-8")).hexdigest()


def test_production_prompt_body_byte_identical_when_smoke_unset_or_no_bait() -> None:
	"""Production runs MUST produce byte-identical editor prompt bodies.

	Two cases must yield the same SHA-256 because neither should
	render the smoke override:
	  1. IS_SMOKE_TEST unset (real PR autofix).
	  2. IS_SMOKE_TEST=true but canary has no bait line (the
	     pull_request:opened first round on a smoke PR — bait is
	     injected by Phase 3c only AFTER this round runs).

	Shared tempdir (and therefore identical RUNTIME_DIR / PR_META_FILE
	/ … paths embedded in the heredoc) is load-bearing: if each call
	got its own tempdir, hash divergence would reflect path
	differences, not the structural drift under test.

	If the cases diverge despite identical paths, OpenRouter prompt-
	cache keys will miss on the next production PR and "Editor prompt
	sha256" log lines stop being usable as drift detectors.
	"""
	import tempfile

	clean_canary = "status: ok\nrun_id: 12345\nupdated-by: ai-pipeline\n"
	with tempfile.TemporaryDirectory() as tmp:
		tmp_p = Path(tmp)
		body_prod, sha_prod = _render_editor_prompt_body_in(
			tmp_p, is_smoke=None, canary_contents=clean_canary
		)
		_, sha_smoke_no_bait = _render_editor_prompt_body_in(
			tmp_p, is_smoke="true", canary_contents=clean_canary
		)

		assert sha_prod == sha_smoke_no_bait, (
			f"Editor prompt body must be byte-identical between "
			f"production (IS_SMOKE_TEST unset) and the smoke-without-bait "
			f"first-round case. Got {sha_prod} vs {sha_smoke_no_bait}. "
			f"This means either the smoke override is leaking into the "
			f"first round (Comment 1) or the heredoc structure has the "
			f"$(if ...)-leaves-blank-line drift (Comment 2)."
		)
		assert body_prod.startswith("PROMPT INJECTION GUARD"), (
			f"Production editor prompt body must start with 'PROMPT INJECTION "
			f"GUARD' on line 1. Got first line: {body_prod.splitlines()[0]!r}. "
			f"A leading blank line indicates the $(if …) heredoc drift "
			f"is back."
		)


def test_embedded_input_sections_carry_file_contents_and_untrusted_fences() -> None:
	"""The inlining helpers MUST emit the actual fixture bytes into the
	prompt body, and PR-author-controlled artifacts MUST be wrapped in
	`=== BEGIN UNTRUSTED ... ===` fences.

	Without this assertion the prompt-body byte-equality test passes even
	when `_embed_input_file` / `_init_prompt_budget` are missing from the
	subshell — the heredoc would render empty `$(…)` substitutions on both
	sides and still hash-equal.  Copilot review on PR #2149 flagged that
	gap; this test closes it by checking that recognisable sentinel bytes
	from each fixture file land in the body, AND that PR_META_FILE and
	LINKED_ISSUE_CONTEXT_FILE are surrounded by UNTRUSTED fences (a
	regression to the pre-fix structure would inline those user-authored
	bytes outside the fence and reopen the prompt-injection vector).
	"""
	import tempfile

	clean_canary = "status: ok\nrun_id: 12345\nupdated-by: ai-pipeline\n"
	with tempfile.TemporaryDirectory() as tmp:
		tmp_p = Path(tmp)
		body, _ = _render_editor_prompt_body_in(
			tmp_p, is_smoke=None, canary_contents=clean_canary
		)

	# Each fixture's recognisable sentinel must appear in the rendered
	# body — confirms _embed_input_file actually ran and dumped the file.
	expected_sentinels = (
		"PR_META_SENTINEL_VALUE",
		"PR_DIFF_SENTINEL",
		"LAST_RUN_DIFF_SENTINEL",
		"LAST_RUN_CHANGED_SENTINEL",
		"PR_CHANGED_SENTINEL",
		"LAST_COMMIT_STAT_SENTINEL",
		"REVIEWER_CONSENSUS_SENTINEL",
		"PR_ALL_COMMENTS_SENTINEL",
		"PR_CHECK_RUNS_SENTINEL",
		"SYMBOL_DIFF_SENTINEL",
		"LINKED_ISSUE_SENTINEL",
		"REVIEWER_BUNDLE_SENTINEL",
	)
	for sentinel in expected_sentinels:
		assert sentinel in body, (
			f"Inlined input fixture {sentinel!r} did not appear in the "
			f"editor prompt body — _embed_input_file is not being called "
			f"or is emitting empty output for that file.  This regresses "
			f"the inlining contract that lets the editor skip exec-cat "
			f"reads."
		)

	# PR-author-controlled artifacts MUST be wrapped in UNTRUSTED fences
	# so the prompt-injection guard's contract holds for them too.
	# Anchor on file-basename markers so the test isn't sensitive to
	# absolute tempdir paths.
	for label in (
		"BEGIN UNTRUSTED",
		"END UNTRUSTED",
	):
		count = body.count(label)
		assert count >= 4, (
			f"Expected at least 4 occurrences of {label!r} in the "
			f"editor prompt body (PR_META_FILE + PR_ALL_COMMENTS + "
			f"PR_CHECK_RUNS + LINKED_ISSUE_CONTEXT_FILE wraps), got "
			f"{count}.  A drop below 4 means a previously-fenced "
			f"user-authored artifact is now inlined as ordinary prompt "
			f"text and prompt-injection content can steer the editor."
		)
	assert "BEGIN UNTRUSTED" in body and "pr_meta.json" in body, (
		"PR_META_FILE (PR title / description) must be wrapped in an "
		"UNTRUSTED fence — author-controlled prose."
	)
	assert "linked_issue_context.txt" in body, (
		"LINKED_ISSUE_CONTEXT_FILE must be inlined; missing entirely "
		"would mean the linked-issue body is no longer reaching the "
		"editor."
	)


def test_smoke_override_renders_only_when_bait_present() -> None:
	"""Smoke override must render iff IS_SMOKE_TEST=true AND canary has bait."""
	import tempfile

	clean_canary = "status: ok\nrun_id: 12345\nupdated-by: ai-pipeline\n"
	bait_canary = (
		clean_canary
		+ "# E2E_EDITOR_BAIT_99999: this line should be removed by the editor (smoke gate)\n"
	)
	with tempfile.TemporaryDirectory() as tmp:
		tmp_p = Path(tmp)
		body_with_bait, sha_with_bait = _render_editor_prompt_body_in(
			tmp_p, is_smoke="true", canary_contents=bait_canary
		)
		_, sha_no_bait = _render_editor_prompt_body_in(
			tmp_p, is_smoke="true", canary_contents=clean_canary
		)

		assert sha_with_bait != sha_no_bait, (
			"With IS_SMOKE_TEST=true and bait present, the body must "
			"differ from the no-bait body — the override should be "
			"prepended."
		)
		assert "=== E2E SMOKE TEST OVERRIDE — READ FIRST ===" in body_with_bait, (
			"Smoke run with bait present must include the override "
			"section marker in the rendered body."
		)
		assert body_with_bait.startswith("=== E2E SMOKE TEST OVERRIDE"), (
			"Override must be the very first content of the body when "
			"rendered — late-prompt placement weakens the directive."
		)


def main() -> int:
	test_smoke_detect_exports_is_smoke_test_env()
	test_editor_script_contains_smoke_only_block()
	test_smoke_block_position_before_input_files()
	test_production_prompt_body_byte_identical_when_smoke_unset_or_no_bait()
	test_smoke_override_renders_only_when_bait_present()
	print("OK: review_autofix smoke editor override contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
