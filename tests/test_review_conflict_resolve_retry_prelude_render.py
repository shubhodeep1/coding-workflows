#!/usr/bin/env python3
"""Contract tests for resolver dependency fallback and outcome-aware
retry-prelude rendering in scripts/review_conflict_resolve.sh.

The dependency-fallback checks exercise main-first checkout selection,
consumer trust-boundary gating, source-repository workspace fallback, and
the fail-closed path when no trusted readable helper exists.

The retry-prelude path keeps the next-attempt reflexion prompt
accurate when the previous attempt was killed by `timeout` (exit
124 / 137), exited non-zero for another reason, or completed but
failed soft validation. Soft-validation never runs on the timeout
or exec_error paths, so the standard "you produced violations
last time, fix them" framing is misleading there.

`_build_retry_prompt` solves this by routing on a `_failure_kind`
positional arg:

  - `exec_error`  → copy the original prompt verbatim
                    (`_retry_prompt_outcome="verbatim:exec_error"`)
  - `timeout`     → render `integration-sync-conflict-resolver-
                    retry-timeout-prelude.txt`
                    (`_retry_prompt_outcome="timeout-prelude"`)
  - `validation`  → render `integration-sync-conflict-resolver-
                    retry-prelude.txt` (the original violations
                    template) (`_retry_prompt_outcome="validation-
                    prelude"`)
  - missing template / non-integration-sync run → copy original
                    prompt verbatim
                    (`_retry_prompt_outcome="verbatim:fallback"`)

Both prelude files are bootstrapped to consumer repos via the
resolver-tooling refresh list in
`scripts/orchestrate_poll_process.sh`. The retry-log dispatch
reads `_retry_prompt_outcome` so the log honestly reflects which
prelude (or verbatim fallback) was actually rendered.

These tests pin the contract at the source level: they assert the
files exist with the right placeholders, the dispatch branches
exist in the script, and the `_retry_prompt_outcome` values are
documented and used by the retry-log dispatch. A SOURCE-LEVEL
contract is more robust to renderer refactors than extracting the
inline python and re-running it; the existing
`test_review_conflict_resolve_smoke_deterministic.py` follows the
same pattern. Originating runs that motivated the timeout-aware
path: 25627236793 / 25627316961 (PRs
shubhodeep1/tele-funtoken-msg-scoring#2874 / #2867) on the
orchestrator/project-2840 stack, plus run 25629086684 / PR #2865.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
PROMPTS_DIR = REPO_ROOT / "prompts"
VALIDATION_PRELUDE = PROMPTS_DIR / "integration-sync-conflict-resolver-retry-prelude.txt"
TIMEOUT_PRELUDE = PROMPTS_DIR / "integration-sync-conflict-resolver-retry-timeout-prelude.txt"


def _resolve_script_text() -> str:
	return RESOLVE_SCRIPT.read_text(encoding="utf-8")


def _resolver_dependency_fallback_source() -> str:
	src = _resolve_script_text()
	match = re.search(
		r"^_resolver_dependency_fallback\(\)\n\{\n.*?\n\}\n",
		src,
		flags=re.DOTALL | re.MULTILINE,
	)
	assert match is not None, "resolver dependency fallback helper is missing"
	return match.group(0)


def _run_resolver_dependency_fallback(
	workspace_root: Path,
	*,
	workflow_source_repo: bool,
) -> subprocess.CompletedProcess[str]:
	script = (
		"set -euo pipefail\n"
		f"GITHUB_WORKSPACE={str(workspace_root)!r}\n"
		f"IS_WORKFLOW_SOURCE_REPO={'true' if workflow_source_repo else 'false'}\n"
		f"{_resolver_dependency_fallback_source()}\n"
		"_resolver_dependency_fallback opencode_helpers.sh\n"
	)
	return subprocess.run(
		["bash", "-c", script],
		check=False,
		capture_output=True,
		text=True,
	)


def test_dependency_fallback_prefers_main_then_script_ref_checkout() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		workspace_root = Path(tmp)
		main_helper = workspace_root / ".codex-workflow-src-main/scripts/opencode_helpers.sh"
		branch_helper = workspace_root / ".codex-workflow-src/scripts/opencode_helpers.sh"
		main_helper.parent.mkdir(parents=True)
		branch_helper.parent.mkdir(parents=True)
		main_helper.write_text(":\n", encoding="utf-8")
		branch_helper.write_text(":\n", encoding="utf-8")
		result = _run_resolver_dependency_fallback(
			workspace_root,
			workflow_source_repo=False,
		)
		assert result.returncode == 0
		assert result.stdout.strip() == str(main_helper)

	with tempfile.TemporaryDirectory() as tmp:
		workspace_root = Path(tmp)
		branch_helper = workspace_root / ".codex-workflow-src/scripts/opencode_helpers.sh"
		branch_helper.parent.mkdir(parents=True)
		branch_helper.write_text(":\n", encoding="utf-8")
		result = _run_resolver_dependency_fallback(
			workspace_root,
			workflow_source_repo=False,
		)
		assert result.returncode == 0
		assert result.stdout.strip() == str(branch_helper)


def test_dependency_fallback_gates_workspace_scripts_and_fails_closed() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		workspace_root = Path(tmp)
		workspace_helper = workspace_root / "scripts/opencode_helpers.sh"
		workspace_helper.parent.mkdir(parents=True)
		workspace_helper.write_text(":\n", encoding="utf-8")

		consumer_result = _run_resolver_dependency_fallback(
			workspace_root,
			workflow_source_repo=False,
		)
		assert consumer_result.returncode == 1
		assert consumer_result.stdout == ""

		source_result = _run_resolver_dependency_fallback(
			workspace_root,
			workflow_source_repo=True,
		)
		assert source_result.returncode == 0
		assert source_result.stdout.strip() == str(workspace_helper)

	with tempfile.TemporaryDirectory() as tmp:
		missing_result = _run_resolver_dependency_fallback(
			Path(tmp),
			workflow_source_repo=False,
		)
		assert missing_result.returncode == 1
		assert missing_result.stdout == ""


def test_dependency_fallback_wiring_warns_and_updates_helper_paths() -> None:
	src = _resolve_script_text()
	assert "opencode_helpers.sh not staged in SUPPORT_SCRIPTS_DIR" in src
	assert 'OPENCODE_HELPERS_PATH="${_resolver_fallback_path}"' in src
	assert "write_opencode_config.sh not staged in SUPPORT_SCRIPTS_DIR" in src
	assert 'OPENCODE_CONFIG_WRITER_PATH="${_resolver_fallback_path}"' in src


def _render_retry_template(template_text: str, env: dict[str, str]) -> str:
	return re.sub(
		r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}",
		lambda m: env.get(m.group(1).upper(), ""),
		template_text,
	)


def test_both_prelude_files_exist() -> None:
	"""Both prelude templates must be checked in. They are referenced
	by `_build_retry_prompt`'s `_prelude_basename` branching and
	would render `verbatim:fallback` with a `::warning::` if
	missing — fail-open is intentional but landing in upstream
	without either file is a regression."""
	assert VALIDATION_PRELUDE.is_file(), (
		f"Validation-path prelude missing at {VALIDATION_PRELUDE}; "
		"_build_retry_prompt's `_failure_kind=validation` branch "
		"falls open to a verbatim retry with `::warning::` when "
		"this file is absent."
	)
	assert TIMEOUT_PRELUDE.is_file(), (
		f"Timeout-path prelude missing at {TIMEOUT_PRELUDE}; "
		"_build_retry_prompt's `_failure_kind=timeout` branch "
		"falls open to a verbatim retry with `::warning::` when "
		"this file is absent."
	)


def test_validation_prelude_carries_violations_framing() -> None:
	"""The validation-path prelude must keep the "produced output
	that failed post-resolve validation" framing + per-violation
	markers / fingerprint sections — that wording is the
	whole point of the prelude on that path, and the in-loop
	soft-validation reads do populate the substitution values."""
	body = VALIDATION_PRELUDE.read_text(encoding="utf-8")
	assert "produced output" in body and "failed post-resolve validation" in body, (
		"Validation prelude should describe the previous attempt's "
		"output as having failed post-resolve validation; if this "
		"framing was removed, the model gets no context for what "
		"to fix on the retry."
	)
	assert "{{MARKER_VIOLATION_COUNT}}" in body
	assert "{{MARKER_VIOLATION_FILES}}" in body
	assert "{{FINGERPRINT_VIOLATION_COUNT}}" in body
	assert "{{FINGERPRINT_VIOLATION_DETAILS}}" in body
	assert "{{SERENA_TOOL_HINTS_RESOLVER}}" in body


def test_validation_prelude_optional_resolver_serena_hint_renders_cleanly() -> None:
	"""The retry-prelude path must render resolver-scoped Serena hints when
	bound and drop the placeholder entirely when unset/empty."""
	body = VALIDATION_PRELUDE.read_text(encoding="utf-8")
	hint_text = "\n".join((
		"Resolver Serena hints:",
		"- Serena MCP is available in this run. Prefer Serena read/navigation tools when they materially reduce shell reads while resolving a conflict (for example: activate_project, get_symbols_overview, find_symbol, find_referencing_symbols, search_for_pattern).",
		"- Use Serena for lookup/navigation only; keep repository writes in the normal apply_patch/shell paths rather than a broad symbol-write workflow.",
	))
	rendered_with_hint = _render_retry_template(body, {
		"SERENA_TOOL_HINTS_RESOLVER": hint_text,
	})
	assert hint_text in rendered_with_hint
	assert "{{SERENA_TOOL_HINTS_RESOLVER}}" not in rendered_with_hint
	rendered_without_hint = _render_retry_template(body, {})
	rendered_empty_hint = _render_retry_template(body, {
		"SERENA_TOOL_HINTS_RESOLVER": "",
	})
	for rendered in (rendered_without_hint, rendered_empty_hint):
		assert "Resolver Serena hints:" not in rendered
		assert "{{SERENA_TOOL_HINTS_RESOLVER}}" not in rendered


def test_validation_prelude_has_no_leaked_unprocessed_markers() -> None:
	"""The validation prelude must contain ONLY `{{KEY}}` placeholders
	whose identifier matches `[A-Z_][A-Z0-9_]*` — a stricter contract
	than the renderer's runtime regex (`\\{\\{\\s*[A-Za-z_]\\w*\\s*\\}\\}`,
	which also accepts spaced/lowercased forms like `{{ key }}` and
	uppercases the captured key for env lookup). The renderer is
	permissive at runtime so an in-flight template-style change
	cannot strand the model on literal braces, but every shipped
	template should stick to UPPER_SNAKE_CASE so reviewers have a
	single canonical spelling to grep for; this test pins that
	style invariant.

	Mustache-style conditional markers like `{{#IF_VIOLATIONS}}` and
	`{{/IF_VIOLATIONS}}` would survive the renderer's substitution
	loop regardless (the runtime regex does not match `#`/`/` chars)
	and leak into the rendered prompt as literal text. An earlier
	iteration of this PR shipped a template with `{{#IF_VIOLATIONS}}`
	/ `{{/IF_VIOLATIONS}}` wrappers; the upstream renderer was
	switched to a placeholder-auto-discovery design that does not
	strip mustache conditionals, so leaving those wrappers in the
	file would render the literal marker text into the model's
	prompt. This regression was caught by all six claude-branch
	reviewers at confidence 5; this test pins the contract so the
	leak cannot be re-introduced.

	`{{PREVIOUS_OUTCOME_NOTICE}}` was a placeholder used by the
	PR's earlier single-template design but is never populated on
	the upstream two-template design — leaving it would always
	render as an empty string. Its absence is a stronger contract
	than tolerating it as a no-op.
	"""
	body = VALIDATION_PRELUDE.read_text(encoding="utf-8")
	# Conditional markers — must not appear under any spelling
	# (with or without interior whitespace, with `#` or `/`).
	conditional_marker_pattern = re.compile(
		r"\{\{[ \t]*[#/][^}]*\}\}"
	)
	leaked = conditional_marker_pattern.findall(body)
	assert not leaked, (
		"Validation prelude contains mustache-style conditional "
		"markers that the renderer does not strip: "
		f"{leaked!r}. The renderer's runtime regex is "
		r"`\{\{\s*[A-Za-z_]\w*\s*\}\}` (auto-discovered from the "
		"template body) and it will not match `{{#…}}` or "
		"`{{/…}}` markers, so they survive verbatim into the "
		"rendered retry prompt. Remove the markers from the "
		"template (the violations body is unconditional on the "
		"validation path)."
	)
	# Vestigial single-template-design placeholder.
	assert "{{PREVIOUS_OUTCOME_NOTICE}}" not in body, (
		"`{{PREVIOUS_OUTCOME_NOTICE}}` is a vestigial placeholder "
		"from this PR's earlier single-template design; on the "
		"upstream two-template design `_build_retry_prompt` never "
		"sets PREVIOUS_OUTCOME_NOTICE, so the renderer substitutes "
		"the empty string and the placeholder is dead template "
		"baggage. Drop it from the validation prelude."
	)
	# Style invariant — stricter than the runtime regex.  Every
	# `{{...}}` token shipped in this template should use
	# UPPER_SNAKE_CASE with no interior whitespace, even though
	# the renderer would accept `{{ key }}` at runtime.  Pinning
	# the spelling here keeps reviewers from having to track
	# multiple grep-able forms of the same placeholder.
	all_tokens = re.findall(r"\{\{([^}]*)\}\}", body)
	bad = [t for t in all_tokens if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", t)]
	assert not bad, (
		"Validation prelude contains `{{…}}` tokens outside the "
		"UPPER_SNAKE_CASE style convention: "
		f"{bad!r}. The renderer's runtime regex "
		r"(`\{\{\s*[A-Za-z_]\w*\s*\}\}`) would accept these, but "
		"every shipped template should use the canonical "
		"`{{UPPER_KEY}}` spelling so reviewers have a single "
		"grep-able form. Either rename them or remove them."
	)


def test_timeout_prelude_has_no_leaked_unprocessed_markers() -> None:
	"""Same contract as the validation prelude: the timeout prelude
	must contain ONLY `{{UPPER_KEY}}` placeholders (the style
	convention enforced on every shipped template), with no mustache
	conditionals or other unrendered token shapes. The renderer's
	runtime regex (`\\{\\{\\s*[A-Za-z_]\\w*\\s*\\}\\}`) is more
	permissive, but pinning the canonical spelling here keeps both
	prelude files reviewable with a single grep."""
	body = TIMEOUT_PRELUDE.read_text(encoding="utf-8")
	conditional_marker_pattern = re.compile(
		r"\{\{[ \t]*[#/][^}]*\}\}"
	)
	leaked = conditional_marker_pattern.findall(body)
	assert not leaked, (
		"Timeout prelude contains mustache-style conditional "
		f"markers the renderer does not strip: {leaked!r}."
	)
	all_tokens = re.findall(r"\{\{([^}]*)\}\}", body)
	bad = [t for t in all_tokens if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", t)]
	assert not bad, (
		"Timeout prelude contains `{{…}}` tokens outside the "
		f"UPPER_SNAKE_CASE template style convention: {bad!r}."
	)


def test_timeout_prelude_carries_apply_patch_first_guidance() -> None:
	"""The timeout-path prelude must (a) name the previous attempt
	as KILLED by the per-attempt timer with the actual seconds
	substituted, (b) tell the model to be DECISIVE rather than
	re-investigate, and (c) NOT carry the misleading "produced
	output that failed validation" framing (soft validation never
	ran on this path). The `{{PER_ATTEMPT_TIMEOUT_SECS}}`
	substitution is what makes the budget actionable in the
	model's context."""
	body = TIMEOUT_PRELUDE.read_text(encoding="utf-8")
	assert "TIMED OUT" in body or "KILLED" in body, (
		"Timeout prelude should explicitly say the previous "
		"attempt was killed/timed out; without that framing the "
		"model has no signal that the working tree is at the "
		"post-`git merge` state, not its previous edits."
	)
	assert "{{PER_ATTEMPT_TIMEOUT_SECS}}" in body, (
		"Timeout prelude should interpolate the actual per-attempt "
		"budget so the model can pace itself — a hard-coded "
		"budget would drift from CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS."
	)
	assert "apply_patch" in body, (
		"Timeout prelude should explicitly mention apply_patch — "
		"the originating failure mode was Codex consuming the "
		"full budget investigating duplicates without ever "
		"calling apply_patch."
	)
	# The misleading "produced output that failed post-resolve
	# validation" framing belongs ONLY in the validation prelude.
	# If it leaks into the timeout prelude, the model is told its
	# previous (non-existent) output was rejected — exactly the
	# bug this design was meant to fix.
	assert "failed post-resolve validation" not in body, (
		"Timeout prelude must NOT carry the validation-path's "
		"'failed post-resolve validation' framing; soft validation "
		"never ran on the timeout path so that wording is misleading."
	)


def test_build_retry_prompt_dispatches_on_failure_kind() -> None:
	"""`_build_retry_prompt` must dispatch on its 4th positional
	arg (`_failure_kind`) and select the right prelude basename
	per failure mode. Without this dispatch, every retry would
	render the validation prelude, re-introducing the misleading
	"0 violations" framing on timeout-killed retries that
	originally motivated the split (runs 25627236793 /
	25627316961 / 25629086684)."""
	src = _resolve_script_text()
	assert "_build_retry_prompt()" in src
	# The function takes _failure_kind as a positional default.
	assert 'local _failure_kind="${4:-validation}"' in src, (
		"_build_retry_prompt should default _failure_kind to "
		"'validation' (the post-soft-validation retry path) and "
		"accept 'timeout' / 'exec_error' overrides from the "
		"retry loop. If this signature changed, update both the "
		"caller at the loop top AND this test."
	)
	# Timeout branch must pick the timeout-prelude basename.
	assert (
		'_prelude_basename="integration-sync-conflict-resolver-retry-timeout-prelude.txt"'
		in src
	), (
		"Timeout branch must select the timeout-specific prelude "
		"basename so the rendered retry prompt carries the "
		"apply_patch-first guidance."
	)
	# Validation (default) branch must pick the standard prelude.
	assert (
		'_prelude_basename="integration-sync-conflict-resolver-retry-prelude.txt"'
		in src
	), (
		"Validation branch must select the standard prelude "
		"basename so the violations-framing path is preserved."
	)
	assert 'SERENA_TOOL_HINTS_RESOLVER="${RESOLVER_SERENA_TOOL_HINTS:-}"' in src, (
		"_build_retry_prompt should pass the resolver-scoped Serena hint env var "
		"through the prelude renderer so integration-sync retries can render "
		"the optional guidance when SERENA_AVAILABLE=true and omit it otherwise."
	)


def test_build_retry_prompt_sets_retry_prompt_outcome() -> None:
	"""`_retry_prompt_outcome` must be set on every code path
	through `_build_retry_prompt` so the retry-log dispatch can
	honestly describe which prelude (or verbatim fallback) was
	actually rendered. Without this, the log claims a
	timeout-aware reflexion was sent even when the function fell
	back to a verbatim copy on a consumer-repo @stable pin that
	predates the new template file."""
	src = _resolve_script_text()
	# Every documented outcome value must appear as a literal
	# assignment in the function.
	for outcome in (
		'_retry_prompt_outcome="verbatim:exec_error"',
		'_retry_prompt_outcome="verbatim:fallback"',
		'_retry_prompt_outcome="timeout-prelude"',
		'_retry_prompt_outcome="validation-prelude"',
	):
		assert outcome in src, (
			f"_build_retry_prompt must set {outcome}; the "
			"retry-log dispatch switch in the main loop reads "
			"_retry_prompt_outcome to decide which message to "
			"emit, so a missing assignment would silently log "
			"the wrong path."
		)


def test_retry_loop_reads_retry_prompt_outcome_for_log_dispatch() -> None:
	"""The retry loop's log dispatch must branch on
	`_retry_prompt_outcome`, not on `_prev_attempt_failure_kind`
	alone. The two can disagree (e.g. failure_kind=timeout but the
	prelude file is missing on a consumer-repo pin, so the
	function fell back to verbatim), and the log should reflect
	the actual prompt fed to codex, not the intent."""
	src = _resolve_script_text()
	assert 'case "${_retry_prompt_outcome}" in' in src, (
		"Retry-log dispatch should switch on _retry_prompt_outcome "
		"so the log message honestly reflects which prelude (or "
		"fallback) was rendered. Branching on _prev_attempt_failure_kind "
		"alone causes the log to claim a timeout-aware reflexion "
		"was sent on consumer-repo pins where the template was "
		"missing and the function fell back to verbatim."
	)


def test_reasoning_default_lowered_to_high() -> None:
	"""CONFLICT_RESOLVER_REASONING_EFFORT default must be `high`,
	not `xhigh`. The lowering was the C half of the response to
	the orchestrator-stack hung-thinking failure mode (runs
	25627236793 / 25627316961). `xhigh` consumed the full
	per-attempt budget enumerating duplicate helpers without
	invoking apply_patch; `high` trades some depth for finishing
	inside the budget."""
	src = _resolve_script_text()
	assert '_resolver_reasoning_effort="${CONFLICT_RESOLVER_REASONING_EFFORT:-high}"' in src, (
		"Script-side default for CONFLICT_RESOLVER_REASONING_EFFORT "
		"should be `high` (lowered from `xhigh`). If a future "
		"refactor reverts this, document the rationale and update "
		"this test together — see the comment block on review_autofix.yml's "
		"CONFLICT_RESOLVER_REASONING_EFFORT env var."
	)
	# The invalid-value fallback must also use the new default.
	assert '_resolver_reasoning_effort="high"' in src and (
		"falling back to high" in src
	), (
		"The invalid-value fallback warning + assignment should "
		"reference `high`, not the old `xhigh`. Otherwise an "
		"operator-supplied bogus value silently restores the "
		"failure-mode default."
	)


def main() -> int:
	test_dependency_fallback_prefers_main_then_script_ref_checkout()
	test_dependency_fallback_gates_workspace_scripts_and_fails_closed()
	test_dependency_fallback_wiring_warns_and_updates_helper_paths()
	test_both_prelude_files_exist()
	test_validation_prelude_carries_violations_framing()
	test_validation_prelude_optional_resolver_serena_hint_renders_cleanly()
	test_validation_prelude_has_no_leaked_unprocessed_markers()
	test_timeout_prelude_has_no_leaked_unprocessed_markers()
	test_timeout_prelude_carries_apply_patch_first_guidance()
	test_build_retry_prompt_dispatches_on_failure_kind()
	test_build_retry_prompt_sets_retry_prompt_outcome()
	test_retry_loop_reads_retry_prompt_outcome_for_log_dispatch()
	test_reasoning_default_lowered_to_high()
	print(
		"OK: review_conflict_resolve outcome-aware retry-prelude "
		"contract holds (validation + timeout preludes, "
		"_failure_kind dispatch, _retry_prompt_outcome wiring, "
		"reasoning-default `high`)"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
