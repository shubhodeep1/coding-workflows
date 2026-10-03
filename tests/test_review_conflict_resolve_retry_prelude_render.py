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

import json
import os
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
		assert result.stdout.strip() == str(branch_helper)

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
		assert source_result.returncode == 1
		assert source_result.stdout == ""

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


def _attempt_state_function() -> str:
	match = re.search(
		r"^_resolver_attempt_state\(\)\n\{\n.*?\n\}\n",
		_resolve_script_text(),
		flags=re.DOTALL | re.MULTILINE,
	)
	assert match is not None
	return match.group(0)


def _run_attempt_fixture(tmp_path: Path, actions: str) -> subprocess.CompletedProcess[str]:
	repo = tmp_path / "repo"
	repo.mkdir()
	git_env = {key: value for key, value in os.environ.items() if key not in (
		"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
		"GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "BASH_ENV", "ENV",
	)}
	subprocess.run(["git", "init", "-q", str(repo)], check=True, env=git_env)
	(repo / "allowed.txt").write_text("baseline\n", encoding="utf-8")
	(repo / "outside.txt").write_text("tracked\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(repo), "add", "allowed.txt", "outside.txt"], check=True, env=git_env)
	(repo / "existing-untracked.txt").write_text("untracked\n", encoding="utf-8")
	state_dir = tmp_path / "attempt-state"
	conflicts = tmp_path / "conflicts.txt"
	conflicts.write_text("allowed.txt\n", encoding="utf-8")
	program = (
		"set -euo pipefail\n"
		f"RESOLVER_ATTEMPT_TREE_DIR={str(state_dir)!r}\n"
		f"CONFLICTED_PATHS_FILE={str(conflicts)!r}\n"
		f"CHECK_RESOLVER_DIFF={str(REPO_ROOT / 'scripts/check_resolver_diff.sh')!r}\n"
		f"{_attempt_state_function()}\n"
		f"{actions}\n"
	)
	return subprocess.run(["bash", "-c", program], cwd=repo, text=True, capture_output=True, env=git_env)


def test_out_of_scope_attempt_restores_before_retry(tmp_path: Path) -> None:
	result = _run_attempt_fixture(tmp_path, """
	_resolver_attempt_state capture
	rm allowed.txt
	printf 'drift\\n' > outside.txt
	chmod +x outside.txt
	printf 'overwritten\\n' > existing-untracked.txt
	printf 'new\\n' > new-untracked.txt
	ln -s outside.txt outside-link
	if _resolver_attempt_state check; then exit 30; fi
	_resolver_attempt_state restore
	test "$(cat allowed.txt)" = baseline
	test "$(cat outside.txt)" = tracked
	test ! -x outside.txt
	test "$(cat existing-untracked.txt)" = untracked
	test ! -e new-untracked.txt
	test ! -L outside-link
	printf 'resolved\\n' > allowed.txt
	_resolver_attempt_state check
	printf 'allowed.txt\\n' > touched.txt
	# The accepted output must still pass the production final hard gate.
	"$CHECK_RESOLVER_DIFF" --conflicted-set "$CONFLICTED_PATHS_FILE" --touched-set touched.txt --repo-root "$PWD"
	""")
	# The touched-set fixture is created only after the attempt check.
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outside.txt" in result.stdout
	assert "new-untracked.txt" in result.stdout
	assert "check_resolver_diff: touched=1 conflicted=1" in result.stdout


def test_unsafe_attempt_restore_stops_before_second_attempt(tmp_path: Path) -> None:
	result = _run_attempt_fixture(tmp_path, """
	_resolver_attempt_state capture
	printf 'drift\\n' > outside.txt
	if _resolver_attempt_state check; then exit 30; fi
	git add outside.txt
	if _resolver_attempt_state restore; then exit 31; fi
	echo 'restore refused; no second attempt or commit'
	""")
	assert result.returncode == 0, result.stdout + result.stderr
	assert "resolver changed the merge index" in result.stderr
	assert "restore refused; no second attempt or commit" in result.stdout
	assert (tmp_path / "repo" / "outside.txt").read_text() == "drift\n"


def test_scope_check_precedes_retry_success_and_preserves_final_guard() -> None:
	src = _resolve_script_text()
	assert src.index('_resolver_attempt_state check || _scope_status=$?') < src.index(
		'echo "Conflict resolver succeeded on attempt ${attempt} (soft validation passed)."'
	)
	assert 'if ! "${SUPPORT_SCRIPTS_DIR}/check_resolver_diff.sh" \\' in src
	assert 'if ! _restore_attempt_base; then' in src


def _scope_fixture(tmp: Path) -> tuple[Path, dict[str, str]]:
	repo = tmp / "repo"
	repo.mkdir()
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	for name in ("conflict.txt", "outside.txt"):
		(repo / name).write_text("base\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(repo), "add", "--", "conflict.txt", "outside.txt"], check=True)
	subprocess.run(
		["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
		check=True,
	)
	(repo / "conflict.txt").write_text("pre-attempt merge conflict\n", encoding="utf-8")
	(repo / "previously-untracked.txt").write_text("keep me\n", encoding="utf-8")
	allowed = tmp / "conflicted_paths.txt"
	allowed.write_text("conflict.txt\n", encoding="utf-8")
	return repo, {
		"RESOLVER_SCOPE_SNAPSHOT_DIR": str(tmp / "snapshot"),
		"RESOLVER_SCOPE_VIOLATIONS_FILE": str(tmp / "violations.txt"),
		"CONFLICTED_PATHS_FILE": str(allowed),
	}


def _scope_action(repo: Path, env: dict[str, str], action: str) -> subprocess.CompletedProcess[str]:
	src = _resolve_script_text()
	start = src.index("_resolver_scope_state() {")
	end = src.index("\n}\n", start) + 2
	clean_env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "WORKSPACE_PATH")}
	return subprocess.run(
		["bash", "-c", src[start:end] + f"\n_resolver_scope_state {action}\n"],
		cwd=repo, env={**clean_env, **env}, capture_output=True, text=True, check=False,
	)


def test_scope_retry_restores_full_attempt_and_keeps_final_gate() -> None:
	src = _resolve_script_text()
	loop = src[src.index('attempt=1\nwhile '):src.index('\ndone\n', src.index('attempt=1\nwhile '))]
	assert loop.index("_resolver_scope_state capture") < loop.index('"${resolver_opencode_cmd[@]}"')
	assert loop.index("_resolver_scope_state check") < loop.index('if [ "${_codex_exit}" -ne 0 ]; then')
	assert loop.index("_resolver_scope_state check") < loop.index('if [ "${_marker_count}" -eq 0 ]')
	assert "_resolver_scope_state restore || ! _resolver_scope_state verify" in loop
	assert '"${SUPPORT_SCRIPTS_DIR}/check_resolver_diff.sh"' in src
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _scope_fixture(Path(directory))
		assert _scope_action(repo, env, "capture").returncode == 0
		(repo / "conflict.txt").write_text("bad resolution\n", encoding="utf-8")
		(repo / "outside.txt").write_text("unauthorized\n", encoding="utf-8")
		(repo / "outside.txt").chmod(0o755)
		(repo / "previously-untracked.txt").unlink()
		(repo / "new-untracked.txt").write_text("new\n", encoding="utf-8")
		assert _scope_action(repo, env, "check").returncode == 1
		assert set(Path(env["RESOLVER_SCOPE_VIOLATIONS_FILE"]).read_text().splitlines()) == {
			"outside.txt", "previously-untracked.txt", "new-untracked.txt",
		}
		assert _scope_action(repo, env, "restore").returncode == 0
		assert _scope_action(repo, env, "verify").returncode == 0
		assert (repo / "conflict.txt").read_text() == "pre-attempt merge conflict\n"
		assert (repo / "outside.txt").read_text() == "base\n"
		assert (repo / "outside.txt").stat().st_mode & 0o111 == 0
		assert (repo / "previously-untracked.txt").read_text() == "keep me\n"
		assert not (repo / "new-untracked.txt").exists()
		(repo / "outside.txt").chmod(0o755)
		assert _scope_action(repo, env, "check").returncode == 1
		assert _scope_action(repo, env, "restore").returncode == 0
		assert _scope_action(repo, env, "verify").returncode == 0
		# A clean second attempt is still subject to the unchanged final gate.
		(repo / "conflict.txt").write_text("good resolution\n", encoding="utf-8")
		assert _scope_action(repo, env, "check").returncode == 0
		touched = Path(directory) / "touched.txt"
		touched.write_text("conflict.txt\n", encoding="utf-8")
		result = subprocess.run(
			["bash", str(REPO_ROOT / "scripts/check_resolver_diff.sh"),
			 "--conflicted-set", env["CONFLICTED_PATHS_FILE"], "--touched-set", str(touched), "--repo-root", str(repo)],
			env={key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "WORKSPACE_PATH")},
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr


def test_scope_snapshot_restore_and_index_fail_closed() -> None:
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _scope_fixture(Path(directory))
		Path(env["CONFLICTED_PATHS_FILE"]).unlink()
		assert _scope_action(repo, env, "capture").returncode != 0
		Path(env["CONFLICTED_PATHS_FILE"]).write_text("conflict.txt\n")
		assert _scope_action(repo, env, "capture").returncode == 0
		(repo / "outside.txt").write_text("unauthorized\n")
		assert _scope_action(repo, env, "check").returncode == 1
		(Path(env["RESOLVER_SCOPE_SNAPSHOT_DIR"]) / "files/outside.txt").unlink()
		assert _scope_action(repo, env, "restore").returncode != 0
		assert _scope_action(repo, env, "verify").returncode != 0
		assert _scope_action(repo, env, "capture").returncode == 0
		subprocess.run(["git", "add", "--", "outside.txt"], cwd=repo, check=True)
		assert _scope_action(repo, env, "check").returncode == 2
		assert _scope_action(repo, env, "restore").returncode == 2


def _scope_and_scratch_source() -> str:
	src = _resolve_script_text()
	start = src.index("_resolver_scope_state() {")
	scope_end = src.index("\n}\n", start) + 2
	scratch_start = src.index("_resolver_prepare_scratch_index() {", scope_end)
	scratch_end = src.index("\n}\n", scratch_start) + 2
	return src[start:scope_end] + "\n" + src[scratch_start:scratch_end]


def _run_scope_script(repo: Path, env: dict[str, str], script: str) -> subprocess.CompletedProcess[str]:
	clean_env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "WORKSPACE_PATH")}
	return subprocess.run(
		["bash", "-c", _scope_and_scratch_source() + "\n" + script + "\n"],
		cwd=repo, env={**clean_env, **env}, capture_output=True, text=True, check=False,
	)


def test_scope_state_git_index_isolation_prevents_false_positive_and_still_guards_worktree() -> None:
	# Issue #4545: a model attempt that stages its (in-scope, conflict-
	# marker-free) resolution before the trusted stage_resolver_touched_
	# path_or_fail step changes the real Git index, and the post-attempt
	# check fails closed even though the content change is entirely
	# within the conflicted set. Reproduce that false positive first, with
	# no isolation.
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _scope_fixture(Path(directory))
		assert _run_scope_script(repo, env, "_resolver_scope_state capture").returncode == 0
		(repo / "conflict.txt").write_text("resolved without conflict markers\n", encoding="utf-8")
		result = _run_scope_script(repo, env, "git add -- conflict.txt\n_resolver_scope_state check")
		assert result.returncode == 2, (
			"expected the unisolated model-staged attempt to fail closed "
			f"(reproducing #4545); got returncode={result.returncode} "
			f"stderr={result.stderr!r}"
		)

	# With _resolver_prepare_scratch_index + GIT_INDEX_FILE isolation, the
	# same staging lands on the disposable copy only: the real index stays
	# byte-identical and the attempt is accepted.
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _scope_fixture(Path(directory))
		env = {**env, "RESOLVER_SCRATCH_INDEX": str(Path(directory) / "scratch_index")}
		real_index_rel = subprocess.run(
			["git", "-C", str(repo), "rev-parse", "--git-path", "index"],
			capture_output=True, text=True, check=True,
		).stdout.strip()
		real_index = repo / real_index_rel if not Path(real_index_rel).is_absolute() else Path(real_index_rel)
		assert _run_scope_script(repo, env, "_resolver_scope_state capture").returncode == 0
		index_before = real_index.read_bytes()
		(repo / "conflict.txt").write_text("resolved without conflict markers\n", encoding="utf-8")
		result = _run_scope_script(
			repo, env,
			'_resolver_prepare_scratch_index\n'
			'GIT_INDEX_FILE="${RESOLVER_SCRATCH_INDEX}" git add -- conflict.txt\n'
			'_resolver_scope_state check',
		)
		assert result.returncode == 0, result.stderr
		assert real_index.read_bytes() == index_before, (
			"the real index must stay byte-identical: the isolated `git add` "
			"should only have written RESOLVER_SCRATCH_INDEX"
		)
		assert Path(env["RESOLVER_SCRATCH_INDEX"]).is_file()

	# Isolation only scopes the index: an out-of-scope worktree edit is
	# still rejected exactly as before isolation was introduced.
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _scope_fixture(Path(directory))
		env = {**env, "RESOLVER_SCRATCH_INDEX": str(Path(directory) / "scratch_index")}
		assert _run_scope_script(repo, env, "_resolver_scope_state capture").returncode == 0
		(repo / "conflict.txt").write_text("resolved without conflict markers\n", encoding="utf-8")
		(repo / "outside.txt").write_text("unauthorized edit\n", encoding="utf-8")
		result = _run_scope_script(
			repo, env,
			'_resolver_prepare_scratch_index\n'
			'GIT_INDEX_FILE="${RESOLVER_SCRATCH_INDEX}" git add -- conflict.txt\n'
			'_resolver_scope_state check',
		)
		assert result.returncode == 1, result.stderr
		assert Path(env["RESOLVER_SCOPE_VIOLATIONS_FILE"]).read_text().splitlines() == ["outside.txt"]


def _index_isolation_blocks() -> tuple[str, str, str]:
	src = _resolve_script_text()
	loop = src[src.index('attempt=1\nwhile '):src.index('\ndone\n', src.index('attempt=1\nwhile '))]
	export_start = loop.index("    _resolver_index_isolated=false\n")
	export_end = loop.index("\n    fi\n", export_start) + len("\n    fi\n")
	unset_start = loop.index('    if [ "${_resolver_index_isolated}" = "true" ]; then\n')
	unset_end = loop.index("\n    fi\n", unset_start) + len("\n    fi\n")
	return loop, loop[export_start:export_end], loop[unset_start:unset_end]


def test_scope_index_isolation_wiring_scoped_to_model_attempt() -> None:
	# Issue #4545: GIT_INDEX_FILE is exported only around the model
	# invocation, after the scratch index is seeded, and unset (only when
	# this block exported it) before the post-attempt scope check runs.
	loop, export_block, unset_block = _index_isolation_blocks()
	assert loop.index("_resolver_prepare_scratch_index") < loop.index('export GIT_INDEX_FILE="${RESOLVER_SCRATCH_INDEX}"')
	assert loop.index('export GIT_INDEX_FILE="${RESOLVER_SCRATCH_INDEX}"') < loop.index('"${resolver_opencode_cmd[@]}"')
	assert loop.rindex('"${resolver_opencode_cmd[@]}"') < loop.index("unset GIT_INDEX_FILE")
	assert loop.index("unset GIT_INDEX_FILE") < loop.index("_resolver_scope_state check")
	probe = (
		export_block
		+ 'printf "during=%s\\n" "${GIT_INDEX_FILE:-<unset>}"\n'
		+ unset_block
		+ 'printf "after=%s\\n" "${GIT_INDEX_FILE:-<unset>}"\n'
	)
	clean_env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV", "GIT_INDEX_FILE")}
	with tempfile.TemporaryDirectory() as directory:
		scratch = Path(directory) / "scratch_index"
		scratch.write_bytes(b"scratch")
		result = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + probe],
			env={**clean_env, "IS_WORKFLOW_SOURCE_REPO": "true", "RESOLVER_SCRATCH_INDEX": str(scratch)},
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert result.stdout.splitlines() == [f"during={scratch}", "after=<unset>"]
		assert not scratch.exists()

		# Consumer repos never export it, so a GIT_INDEX_FILE the caller
		# already set must survive the attempt untouched.
		scratch.write_bytes(b"scratch")
		inherited = str(Path(directory) / "inherited_index")
		result = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + probe],
			env={**clean_env, "IS_WORKFLOW_SOURCE_REPO": "false", "RESOLVER_SCRATCH_INDEX": str(scratch),
			     "GIT_INDEX_FILE": inherited},
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert result.stdout.splitlines() == [f"during={inherited}", f"after={inherited}"]
		assert scratch.read_bytes() == b"scratch"


def test_scope_isolated_model_commit_still_fails_closed() -> None:
	# Issue #4545: the isolation covers the index only. A model-issued
	# `git commit` during the resolver's `git merge --no-commit` still
	# moves HEAD and removes MERGE_HEAD; the unchanged merge_state() check
	# must keep failing that attempt closed, while the real index stays
	# byte-identical.
	with tempfile.TemporaryDirectory() as directory:
		tmp = Path(directory)
		repo = tmp / "repo"
		repo.mkdir()
		identity = {
			"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
			"GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
		}
		git_env = {**{key: value for key, value in os.environ.items() if key not in (
			"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "BASH_ENV", "ENV",
		)}, **identity}

		def git(*args: str) -> subprocess.CompletedProcess[str]:
			return subprocess.run(["git", "-C", str(repo), *args], env=git_env, capture_output=True, text=True, check=False)

		subprocess.run(["git", "init", "-q", str(repo)], env=git_env, check=True)
		for name in ("conflict.txt", "outside.txt"):
			(repo / name).write_text("base\n", encoding="utf-8")
		assert git("add", "--", "conflict.txt", "outside.txt").returncode == 0
		assert git("commit", "-qm", "base").returncode == 0
		assert git("checkout", "-qb", "side").returncode == 0
		(repo / "conflict.txt").write_text("side\n", encoding="utf-8")
		assert git("commit", "-qam", "side").returncode == 0
		assert git("checkout", "-q", "-").returncode == 0
		(repo / "conflict.txt").write_text("main\n", encoding="utf-8")
		assert git("commit", "-qam", "main").returncode == 0
		assert git("merge", "--no-commit", "--no-ff", "side").returncode != 0
		merge_head = Path(git("rev-parse", "--absolute-git-dir").stdout.strip()) / "MERGE_HEAD"
		assert merge_head.is_file()
		allowed = tmp / "conflicted_paths.txt"
		allowed.write_text("conflict.txt\n", encoding="utf-8")
		env = {
			**identity,
			"RESOLVER_SCOPE_SNAPSHOT_DIR": str(tmp / "snapshot"),
			"RESOLVER_SCOPE_VIOLATIONS_FILE": str(tmp / "violations.txt"),
			"CONFLICTED_PATHS_FILE": str(allowed),
			"RESOLVER_SCRATCH_INDEX": str(tmp / "scratch_index"),
		}
		real_index = Path(git("rev-parse", "--absolute-git-dir").stdout.strip()) / "index"
		assert _run_scope_script(repo, env, "_resolver_scope_state capture").returncode == 0
		index_before = real_index.read_bytes()
		head_before = git("rev-parse", "HEAD").stdout
		(repo / "conflict.txt").write_text("resolved\n", encoding="utf-8")
		result = _run_scope_script(
			repo, env,
			'_resolver_prepare_scratch_index\n'
			'GIT_INDEX_FILE="${RESOLVER_SCRATCH_INDEX}" git add -- conflict.txt\n'
			'GIT_INDEX_FILE="${RESOLVER_SCRATCH_INDEX}" git commit -qm model-commit\n'
			'_resolver_scope_state check',
		)
		assert result.returncode == 2, result.stdout + result.stderr
		assert "Resolver scope check failed closed (ValueError)" in result.stderr
		assert "Reason code: index_drift." in result.stderr
		assert real_index.read_bytes() == index_before
		assert not merge_head.exists()
		assert git("rev-parse", "HEAD").stdout != head_before


def test_scope_failure_reason_codes_are_enumerated_and_path_free() -> None:
	# Issue #4545: the fail-closed error carries a reason code from a fixed
	# table, so index drift and an unsafe path are told apart without
	# printing untrusted path text.
	src = _resolve_script_text()
	start = src.index("_resolver_scope_state() {")
	body = src[start:src.index("\n}\n", start)]
	raised = set(re.findall(r'raise ValueError\("([^"]+)"\)', body))
	table_src = body[body.index("scope_failure_reasons = {"):]
	table_src = table_src[:table_src.index("\n    }\n")]
	table = dict(re.findall(r'^\s+"([^"]+)": "([^"]+)",$', table_src, re.MULTILINE))
	assert raised, "no ValueError messages found in _resolver_scope_state"
	assert raised == set(table), (sorted(raised - set(table)), sorted(set(table) - raised))
	codes = list(table.values()) + ["unclassified", "git_command_failed", "manifest_incomplete", "os_error"]
	assert len(codes) == len(set(codes))
	assert all(re.fullmatch(r"[a-z_]+", code) for code in codes)
	assert "Reason code: {scope_failure_reason}." in body

	with tempfile.TemporaryDirectory() as directory:
		repo, env = _scope_fixture(Path(directory))
		(repo / "leaky\tsecret-name.txt").write_text("x\n", encoding="utf-8")
		result = _scope_action(repo, env, "capture")
		assert result.returncode == 2, result.stdout + result.stderr
		assert "Resolver scope capture failed closed (ValueError). Reason code: unsafe_path." in result.stderr
		assert "leaky" not in result.stderr and "secret-name" not in result.stderr


def _disable_opencode_snapshot_source() -> str:
	src = _resolve_script_text()
	start = src.index("_resolver_disable_opencode_snapshot()\n{\n")
	end = src.index("\n}\n", start) + 3
	return src[start:end]


def test_scope_index_isolation_disables_opencode_snapshot() -> None:
	# Issue #4545 conformance: OpenCode's session snapshot runs
	# `git --git-dir <snapshot> --work-tree <checkout> add` with the
	# inherited environment, so under the GIT_INDEX_FILE isolation it
	# staged the conflicted files (markers included) into the scratch
	# index and hid the unmerged paths from the model. The source-repo
	# resolver turns snapshots off in its own config, after the writer
	# and before the bootstrap check, and never for consumer repos.
	src = _resolve_script_text()
	call = (
		'if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ] \\\n'
		'  && ! _resolver_disable_opencode_snapshot "${RESOLVER_OPENCODE_CONFIG}"; then\n'
	)
	assert src.count(call) == 1
	writer = src.index('if ! bash "${OPENCODE_CONFIG_WRITER_PATH}"')
	bootstrap = src.index("if ! opencode_require_bootstrap review_conflict_resolve writer")
	assert writer < src.index(call) < bootstrap < src.index('attempt=1\nwhile ')
	clean_env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV")}
	with tempfile.TemporaryDirectory() as directory:
		config = Path(directory) / "resolver_opencode.json"
		config.write_text('{"model": "openrouter/x/y", "share": "disabled"}\n', encoding="utf-8")
		result = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + _disable_opencode_snapshot_source()
			 + '_resolver_disable_opencode_snapshot "$1"\n', "probe", str(config)],
			env=clean_env, capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert json.loads(config.read_text(encoding="utf-8")) == {
			"model": "openrouter/x/y", "share": "disabled", "snapshot": False,
		}
		assert sorted(path.name for path in Path(directory).iterdir()) == ["resolver_opencode.json"]

		# A config it cannot parse fails the step and is left untouched.
		for broken in ("not json\n", "[]\n"):
			config.write_text(broken, encoding="utf-8")
			result = subprocess.run(
				["bash", "-c", "set -euo pipefail\n" + _disable_opencode_snapshot_source()
				 + '_resolver_disable_opencode_snapshot "$1"\n', "probe", str(config)],
				env=clean_env, capture_output=True, text=True, check=False,
			)
			assert result.returncode == 1
			assert "Cannot disable OpenCode snapshots" in result.stderr
			assert config.read_text(encoding="utf-8") == broken
			assert sorted(path.name for path in Path(directory).iterdir()) == ["resolver_opencode.json"]

	# PR #4606 review round 1: a failure after the temporary file is written
	# removes it, whether the handler catches the error (OSError fails the
	# step with the ::error:: line) or not (RuntimeError stands in for any
	# other exception). A sitecustomize module makes os.replace raise.
	for raised, handled in (("OSError", True), ("RuntimeError", False)):
		with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as hook_directory:
			config = Path(directory) / "resolver_opencode.json"
			original = '{"model": "openrouter/x/y"}\n'
			config.write_text(original, encoding="utf-8")
			(Path(hook_directory) / "sitecustomize.py").write_text(
				"import os\n\n\n"
				"def _failing_replace(source, target):\n"
				f"\traise {raised}('injected os.replace failure')\n\n\n"
				"os.replace = _failing_replace\n",
				encoding="utf-8",
			)
			hook_env = dict(clean_env, PYTHONPATH=hook_directory, PYTHONDONTWRITEBYTECODE="1")
			result = subprocess.run(
				["bash", "-c", "set -euo pipefail\n" + _disable_opencode_snapshot_source()
				 + '_resolver_disable_opencode_snapshot "$1"\n', "probe", str(config)],
				env=hook_env, capture_output=True, text=True, check=False,
			)
			assert result.returncode != 0
			assert "injected os.replace failure" in result.stderr
			assert ("Cannot disable OpenCode snapshots" in result.stderr) is handled
			assert config.read_text(encoding="utf-8") == original
			assert sorted(path.name for path in Path(directory).iterdir()) == ["resolver_opencode.json"]


def test_resolver_prompts_forbid_staging_and_committing() -> None:
	# Issue #4545 conformance: review_conflict_prepare.sh renders the
	# integration-sync template instead of the generic one on
	# `orchestrator/project-*` heads, and both run through the same
	# isolated model attempt, so both must carry the staging/committing
	# prohibition in the rules list that holds their other "Do not" rules.
	for name in ("conflict-resolver.txt", "integration-sync-conflict-resolver.txt"):
		text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
		rules = [block for block in text.split("\n\n") if "\n- Do not access the network.\n" in block + "\n"]
		assert len(rules) == 1, name
		rules = rules[0] + "\n"
		assert "- Do not run `git add`, `git rm --cached`, `git commit`, `git stage`, or any\n" in rules, name
		assert "A separate, trusted step stages and commits your file\n" in rules, name


def test_scope_symlink_restore_preserves_preexisting_target() -> None:
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _scope_fixture(Path(directory))
		(repo / "previously-untracked.txt").unlink()
		(repo / "previously-untracked.txt").symlink_to("outside.txt")
		assert _scope_action(repo, env, "capture").returncode == 0
		(repo / "previously-untracked.txt").unlink()
		(repo / "previously-untracked.txt").write_text("replaced link\n")
		assert _scope_action(repo, env, "check").returncode == 1
		assert _scope_action(repo, env, "restore").returncode == 0
		assert _scope_action(repo, env, "verify").returncode == 0
		assert (repo / "previously-untracked.txt").is_symlink()
		assert (repo / "previously-untracked.txt").readlink() == Path("outside.txt")


def test_scope_feedback_is_available_for_generic_resolver() -> None:
	src = _resolve_script_text()
	fn = src[src.index("_build_retry_prompt() {"):src.index("\n}\n", src.index("_build_retry_prompt() {")) + 2]
	assert fn.index('if [ "${_failure_kind}" = "scope" ]') < fn.index('if [ "${IS_INTEGRATION_SYNC:-false}" != "true" ]')
	assert '_retry_prompt_outcome="scope-prelude"' in fn
	assert '"${_prev_attempt_failure_kind:-validation}"' in src
	with tempfile.TemporaryDirectory() as directory:
		prompt = Path(directory) / "original.txt"
		retry = Path(directory) / "retry.txt"
		prompt.write_text("Only resolve the merge conflict.\n", encoding="utf-8")
		result = subprocess.run(
			["bash", "-c", fn + '\n_build_retry_prompt 1 /nonexistent /nonexistent scope; printf "%s" "${_retry_prompt_outcome}"'],
			env={**os.environ, "IS_INTEGRATION_SYNC": "false", "CONFLICT_RESOLVER_PROMPT_FILE": str(prompt),
			     "RESOLVER_RETRY_PROMPT_FILE": str(retry)}, capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert result.stdout == "scope-prelude"
		assert "outside the captured conflicted-paths set" in retry.read_text()
		assert retry.read_text().endswith(prompt.read_text())


_GIT_PINNING_ENV = (
	"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
	"GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "BASH_ENV", "ENV", "WORKSPACE_PATH",
)


def _clean_git_env() -> dict[str, str]:
	return {key: value for key, value in os.environ.items() if key not in _GIT_PINNING_ENV}


def _named_function_source(name: str) -> str:
	match = re.search(
		rf"^{re.escape(name)}\(\)\n\{{\n.*?\n\}}\n",
		_resolve_script_text(),
		flags=re.DOTALL | re.MULTILINE,
	)
	assert match is not None, f"{name} is missing from review_conflict_resolve.sh"
	return match.group(0)


def _scope_state_source() -> str:
	src = _resolve_script_text()
	start = src.index("_resolver_scope_state() {")
	return src[start:src.index("\n}\n", start) + 3]


def _model_index_wiring_block() -> str:
	"""The retry loop's own private-index block, verbatim (#5627)."""
	src = _resolve_script_text()
	start = src.index('  if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ]; then\n    if ! _resolver_model_index_prepare; then')
	end = src.index("\n  fi\n", start) + len("\n  fi\n")
	return src[start:end]


def _merge_conflict_fixture(tmp: Path) -> tuple[Path, dict[str, str]]:
	"""A real in-progress merge: conflict.txt is unmerged, outside.txt is not."""
	repo = tmp / "repo"
	repo.mkdir()
	env = _clean_git_env()
	git = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
	subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
	(repo / "conflict.txt").write_text("base\n", encoding="utf-8")
	(repo / "outside.txt").write_text("base\n", encoding="utf-8")
	subprocess.run([*git, "add", "--", "conflict.txt", "outside.txt"], check=True, env=env)
	subprocess.run([*git, "commit", "-qm", "base"], check=True, env=env)
	subprocess.run([*git, "checkout", "-qb", "theirs"], check=True, env=env)
	(repo / "conflict.txt").write_text("theirs\n", encoding="utf-8")
	subprocess.run([*git, "commit", "-qam", "theirs"], check=True, env=env)
	subprocess.run([*git, "checkout", "-q", "main"], check=True, env=env)
	(repo / "conflict.txt").write_text("ours\n", encoding="utf-8")
	subprocess.run([*git, "commit", "-qam", "ours"], check=True, env=env)
	merge = subprocess.run([*git, "merge", "-q", "theirs"], env=env, capture_output=True, text=True, check=False)
	assert merge.returncode != 0, "fixture must leave conflict.txt unmerged"
	allowed = tmp / "conflicted_paths.txt"
	allowed.write_text("conflict.txt\n", encoding="utf-8")
	return repo, {
		**env,
		"RESOLVER_SCOPE_SNAPSHOT_DIR": str(tmp / "scope-snapshot"),
		"RESOLVER_SCOPE_VIOLATIONS_FILE": str(tmp / "violations.txt"),
		"RESOLVER_ATTEMPT_TREE_DIR": str(tmp / "attempt-tree"),
		"CONFLICTED_PATHS_FILE": str(allowed),
		"RESOLVER_MODEL_INDEX_FILE": str(tmp / "runtime" / "resolver_model_index"),
		"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
		"GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
	}


def _run_model_attempt(repo: Path, env: dict[str, str], model_action: str, *, source_repo: bool = True) -> subprocess.CompletedProcess[str]:
	"""Capture both scope baselines, run a stub model through the loop's own
	private-index block, then report both scope checks (#5627)."""
	Path(env["RESOLVER_MODEL_INDEX_FILE"]).parent.mkdir(parents=True, exist_ok=True)
	program = (
		"set -euo pipefail\n"
		f"IS_WORKFLOW_SOURCE_REPO={'true' if source_repo else 'false'}\n"
		f"{_scope_state_source()}\n"
		f"{_named_function_source('_resolver_attempt_state')}\n"
		f"{_named_function_source('_resolver_model_index_prepare')}\n"
		"_resolver_attempt_state capture\n"
		"_resolver_scope_state capture\n"
		'resolver_opencode_cmd=(bash -c "${MODEL_ACTION}")\n'
		f"{_model_index_wiring_block()}"
		'"${resolver_opencode_cmd[@]}"\n'
		"_scope_rc=0\n_resolver_scope_state check || _scope_rc=$?\n"
		'echo "scope_rc=${_scope_rc}"\n'
		"_attempt_rc=0\n_resolver_attempt_state check || _attempt_rc=$?\n"
		'echo "attempt_rc=${_attempt_rc}"\n'
	)
	return subprocess.run(
		["bash", "-c", program], cwd=repo, env={**env, "MODEL_ACTION": model_action},
		capture_output=True, text=True, check=False,
	)


def _git_out(repo: Path, env: dict[str, str], *args: str) -> str:
	return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout


def _stage_and_commit_resolution(repo: Path, env: dict[str, str]) -> None:
	"""The script's own post-loop path on the real index: stage the touched
	conflicted path and create the merge commit."""
	subprocess.run(["git", "add", "--", "conflict.txt"], cwd=repo, env=env, check=True)
	assert _git_out(repo, env, "diff", "--name-only", "--diff-filter=U") == ""
	subprocess.run(["git", "commit", "-qm", "[ai-merge-resolve] resolve merge conflicts"], cwd=repo, env=env, check=True)


def test_model_staging_permitted_conflict_passes_scope_checks() -> None:
	"""Regression for #5627: a model that stages ONLY the permitted conflicted
	file used to change the live merge index, so `_resolver_scope_state check`
	failed closed (exit 2) and the run aborted. With the private model index the
	real index is untouched and both attempt-scope checks pass."""
	resolve_and_stage = "printf 'resolved\\n' > conflict.txt && git add -- conflict.txt"
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _merge_conflict_fixture(Path(directory))
		index_before = _git_out(repo, env, "ls-files", "--stage")
		result = _run_model_attempt(repo, env, resolve_and_stage)
		assert result.returncode == 0, result.stdout + result.stderr
		assert "scope_rc=0" in result.stdout, result.stdout + result.stderr
		assert "attempt_rc=0" in result.stdout, result.stdout + result.stderr
		assert _git_out(repo, env, "ls-files", "--stage") == index_before
		assert _git_out(repo, env, "diff", "--name-only", "--diff-filter=U") == "conflict.txt\n"
		_stage_and_commit_resolution(repo, env)
		assert _git_out(repo, env, "show", "HEAD:conflict.txt") == "resolved\n"
		assert len(_git_out(repo, env, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
	# Control: the same model action on the shared index is the #5627 failure.
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _merge_conflict_fixture(Path(directory))
		result = _run_model_attempt(repo, env, resolve_and_stage, source_repo=False)
		assert "scope_rc=2" in result.stdout, result.stdout + result.stderr
		assert "Resolver scope check failed closed (ValueError)." in result.stderr


def test_model_out_of_scope_staging_cannot_reach_commit() -> None:
	"""A model that edits and stages a file outside the conflicted set changes
	only its private index: the real index is untouched, the worktree edit is
	flagged and restored, and the merge commit carries no out-of-scope change.
	Index-only staging (`git rm --cached`) never reaches the commit either."""
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _merge_conflict_fixture(Path(directory))
		index_before = _git_out(repo, env, "ls-files", "--stage")
		result = _run_model_attempt(
			repo, env,
			"printf 'resolved\\n' > conflict.txt && printf 'unauthorized\\n' > outside.txt"
			" && git add -- conflict.txt outside.txt",
		)
		assert result.returncode == 0, result.stdout + result.stderr
		assert "scope_rc=1" in result.stdout, result.stdout + result.stderr
		assert "attempt_rc=1" in result.stdout, result.stdout + result.stderr
		assert Path(env["RESOLVER_SCOPE_VIOLATIONS_FILE"]).read_text(encoding="utf-8") == "outside.txt\n"
		assert _git_out(repo, env, "ls-files", "--stage") == index_before
		scope = subprocess.run(
			["bash", "-c", _scope_state_source() + "\n_resolver_scope_state restore\n_resolver_scope_state verify\n"],
			cwd=repo, env=env, capture_output=True, text=True, check=False,
		)
		assert scope.returncode == 0, scope.stdout + scope.stderr
		assert (repo / "outside.txt").read_text(encoding="utf-8") == "base\n"
		(repo / "conflict.txt").write_text("resolved\n", encoding="utf-8")
		_stage_and_commit_resolution(repo, env)
		assert _git_out(repo, env, "show", "HEAD:outside.txt") == "base\n"
		assert "outside.txt" not in _git_out(repo, env, "diff", "--name-only", "HEAD^1", "HEAD")
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _merge_conflict_fixture(Path(directory))
		result = _run_model_attempt(
			repo, env,
			"printf 'resolved\\n' > conflict.txt && git add -- conflict.txt && git rm -q --cached -- outside.txt",
		)
		assert "scope_rc=0" in result.stdout and "attempt_rc=0" in result.stdout, result.stdout + result.stderr
		_stage_and_commit_resolution(repo, env)
		assert _git_out(repo, env, "show", "HEAD:outside.txt") == "base\n"


def test_model_bypassing_private_index_still_fails_closed() -> None:
	"""The fail-closed guard is not weakened: a model that writes the real index
	despite GIT_INDEX_FILE still makes the scope check exit 2."""
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _merge_conflict_fixture(Path(directory))
		result = _run_model_attempt(
			repo, env,
			"printf 'resolved\\n' > conflict.txt && env -u GIT_INDEX_FILE git add -- conflict.txt",
		)
		assert "scope_rc=2" in result.stdout, result.stdout + result.stderr
		assert "Resolver scope check failed closed (ValueError)." in result.stderr


def test_model_index_is_fresh_per_attempt_and_fails_closed() -> None:
	"""Each attempt starts from the real merge index, and the helper refuses a
	relative copy path or a missing Git index."""
	prepare = _named_function_source("_resolver_model_index_prepare")
	with tempfile.TemporaryDirectory() as directory:
		repo, env = _merge_conflict_fixture(Path(directory))
		copy = Path(env["RESOLVER_MODEL_INDEX_FILE"])
		copy.parent.mkdir(parents=True)
		program = (
			f"set -euo pipefail\n{prepare}\n"
			"_resolver_model_index_prepare\n"
			'GIT_INDEX_FILE="${RESOLVER_MODEL_INDEX_FILE}" git add -- conflict.txt\n'
			': > "${RESOLVER_MODEL_INDEX_FILE}.lock"\n'
			'if cmp -s "$(git rev-parse --git-path index)" "${RESOLVER_MODEL_INDEX_FILE}"; then exit 30; fi\n'
			"_resolver_model_index_prepare\n"
			'cmp -s "$(git rev-parse --git-path index)" "${RESOLVER_MODEL_INDEX_FILE}"\n'
			'test ! -e "${RESOLVER_MODEL_INDEX_FILE}.lock"\n'
		)
		result = subprocess.run(["bash", "-c", program], cwd=repo, env=env, capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stdout + result.stderr
		relative = subprocess.run(
			["bash", "-c", f"{prepare}\n_resolver_model_index_prepare\n"], cwd=repo,
			env={**env, "RESOLVER_MODEL_INDEX_FILE": "resolver_model_index"}, capture_output=True, text=True, check=False,
		)
		assert relative.returncode != 0
		assert not (repo / "resolver_model_index").exists()
		copy_bytes = copy.read_bytes()
		inherited = subprocess.run(
			["bash", "-c", f"{prepare}\n_resolver_model_index_prepare\n"], cwd=repo,
			env={**env, "GIT_INDEX_FILE": str(copy)}, capture_output=True, text=True, check=False,
		)
		assert inherited.returncode != 0, "an inherited GIT_INDEX_FILE naming the copy must fail closed"
		assert copy.read_bytes() == copy_bytes, "the index the guards read must not be deleted"
	with tempfile.TemporaryDirectory() as directory:
		not_a_repo = subprocess.run(
			["bash", "-c", f"{prepare}\n_resolver_model_index_prepare\n"], cwd=directory,
			env={**_clean_git_env(), "GIT_CEILING_DIRECTORIES": directory,
			     "RESOLVER_MODEL_INDEX_FILE": str(Path(directory) / "copy")},
			capture_output=True, text=True, check=False,
		)
		assert not_a_repo.returncode != 0
		assert not (Path(directory) / "copy").exists()


def test_resolver_opencode_snapshot_opt_out() -> None:
	"""OpenCode's snapshot git calls inherit GIT_INDEX_FILE, so the resolver's
	own config turns snapshots off; every other key is kept, and a malformed
	config fails without being rewritten."""
	disable = _named_function_source("_resolver_disable_opencode_snapshot")
	with tempfile.TemporaryDirectory() as directory:
		config = Path(directory) / "resolver_opencode.json"
		original = {"model": "openrouter/x/y", "share": "disabled", "permission": "allow"}
		config.write_text(json.dumps(original), encoding="utf-8")
		config.chmod(0o600)
		result = subprocess.run(
			["bash", "-c", f"{disable}\n_resolver_disable_opencode_snapshot\n"],
			env={**_clean_git_env(), "RESOLVER_OPENCODE_CONFIG": str(config)},
			capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert json.loads(config.read_text(encoding="utf-8")) == {**original, "snapshot": False}
		assert config.stat().st_mode & 0o777 == 0o600
		for bad in ("{not json", "[]"):
			config.write_text(bad, encoding="utf-8")
			result = subprocess.run(
				["bash", "-c", f"{disable}\n_resolver_disable_opencode_snapshot\n"],
				env={**_clean_git_env(), "RESOLVER_OPENCODE_CONFIG": str(config)},
				capture_output=True, text=True, check=False,
			)
			assert result.returncode == 1
			assert "Cannot disable OpenCode snapshots in the resolver config" in result.stderr
			assert config.read_text(encoding="utf-8") == bad
			assert sorted(p.name for p in Path(directory).iterdir()) == ["resolver_opencode.json"]


def test_private_model_index_wiring() -> None:
	"""Source-level wiring (#5627): source-repo only; the snapshot opt-out runs
	before the bootstrap validates the config; the private index is prepared
	after the scope baseline and before the model command runs."""
	src = _resolve_script_text()
	opt_out = 'if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ] && ! _resolver_disable_opencode_snapshot; then'
	assert opt_out in src
	assert src.index('--config-path "${RESOLVER_OPENCODE_CONFIG}"') < src.index(opt_out)
	assert src.index(opt_out) < src.index('if ! opencode_require_bootstrap review_conflict_resolve writer "${MODEL_EDITOR}"')
	loop = src[src.index('attempt=1\nwhile '):src.index('\ndone\n', src.index('attempt=1\nwhile '))]
	block = _model_index_wiring_block()
	assert block in loop
	assert 'resolver_opencode_cmd=(env "GIT_INDEX_FILE=${RESOLVER_MODEL_INDEX_FILE}" "${resolver_opencode_cmd[@]}")' in block
	assert "refusing to invoke model." in block and "exit 1" in block
	assert loop.index("_resolver_scope_state capture") < loop.index(block)
	assert loop.index("resolver_opencode_cmd=(\n") < loop.index(block)
	assert loop.index(block) < loop.index('-- "${resolver_opencode_cmd[@]}" < "${_effective_prompt_file}"')
	assert 'RESOLVER_MODEL_INDEX_FILE="${RUNTIME_DIR}/resolver_model_index"' in src
	# The script's own staging stays on the real index: the helper and its
	# only call site never point GIT_INDEX_FILE at the model's private copy.
	staging_def = src.index("stage_resolver_touched_path_or_fail() {")
	staging_fn = src[staging_def:src.index("\n}\n", staging_def)]
	assert "GIT_INDEX_FILE" not in staging_fn
	assert "Never call it with\n# GIT_INDEX_FILE pointing at the model's private copy" in src[:staging_def]
	staging_call = src.index('if ! stage_resolver_touched_path_or_fail "${touched_path}"; then')
	assert "GIT_INDEX_FILE=" not in src[src.rindex("This staging runs on the real index on purpose", 0, staging_call):staging_call]


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
	test_scope_check_precedes_retry_success_and_preserves_final_guard()
	test_scope_retry_restores_full_attempt_and_keeps_final_gate()
	test_scope_snapshot_restore_and_index_fail_closed()
	test_scope_state_git_index_isolation_prevents_false_positive_and_still_guards_worktree()
	test_scope_index_isolation_wiring_scoped_to_model_attempt()
	test_scope_isolated_model_commit_still_fails_closed()
	test_scope_failure_reason_codes_are_enumerated_and_path_free()
	test_scope_index_isolation_disables_opencode_snapshot()
	test_resolver_prompts_forbid_staging_and_committing()
	test_scope_symlink_restore_preserves_preexisting_target()
	test_scope_feedback_is_available_for_generic_resolver()
	test_model_staging_permitted_conflict_passes_scope_checks()
	test_model_out_of_scope_staging_cannot_reach_commit()
	test_model_bypassing_private_index_still_fails_closed()
	test_model_index_is_fresh_per_attempt_and_fails_closed()
	test_resolver_opencode_snapshot_opt_out()
	test_private_model_index_wiring()
	print(
		"OK: review_conflict_resolve outcome-aware retry-prelude "
		"contract holds (validation + timeout preludes, "
		"_failure_kind dispatch, _retry_prompt_outcome wiring, "
		"reasoning-default `high`)"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
