#!/usr/bin/env python3
"""Inline the step bodies that review_autofix.yml sources from scripts/.

GitHub refuses to start runs for a workflow file over 512,000 bytes: the
push that crosses the limit gets a zero-job "workflow file issue" run named
after the file path. To keep ``.github/workflows/review_autofix.yml`` under
that limit, the largest ``run:`` bodies live in
``scripts/review_autofix_step_*.sh``. Each of those steps keeps its
``name:``, ``if:``, ``env:`` and ``continue-on-error:`` in the workflow and
its ``run:`` block is a short wrapper that resolves the script (staged
support bundle, then the verified ``.codex-workflow-src/scripts``) and
``source``s it in the step shell.

Contract tests that grep or execute those step bodies read the workflow
through :func:`expanded_review_autofix_text`, which swaps each wrapper back
for the script body at the original indentation. The result is the workflow
as it read before the move, so the tests keep checking the exact code that
runs.

Steps in the ``gate`` job run before support staging, so their wrapper
cannot use ``SUPPORT_SCRIPTS_DIR`` or ``.codex-workflow-src``. Those scripts
are listed in :data:`REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS`; their wrapper
resolves only the SHA-verified ``.codex-head-gate-src`` sparse checkout and
requires ``HEAD_GATE_HELPER_VERIFIED=true``.

``.github/workflows/test-and-mark-stable.yml`` uses the same approach for its
largest ``e2e-smoke-test`` step bodies (#7991): ``scripts/stable_e2e_step_*.sh``
are read from a ``github.sha``-pinned sparse checkout, copied to
``RUNNER_TEMP`` and ``source``d once ``STABLE_E2E_STEP_SCRIPTS_VERIFIED=true``.
:func:`expanded_test_and_mark_stable_text` inlines them again for tests.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
REVIEW_AUTOFIX_STEP_SCRIPTS_DIR = REPO_ROOT / "scripts"
TEST_AND_MARK_STABLE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

# Step name -> script under scripts/, and whether a missing script fails the
# step ("error") or skips it ("warning"). The always() steps fail open
# because they also run after support staging itself failed.
REVIEW_AUTOFIX_STEP_SCRIPTS: dict[str, tuple[str, str]] = {
	"Pre-review deterministic merge-topology gate": ("review_autofix_step_merge_topology_gate.sh", "error"),
	"Detect editor-claimed-but-uncommitted changes": ("review_autofix_step_editor_uncommitted_changes.sh", "error"),
	"Detect merge conflicts": ("review_autofix_step_detect_merge_conflicts.sh", "error"),
	"Post partial finalize comment and persist runtime marker": ("review_autofix_step_partial_finalize.sh", "warning"),
	"Append review pipeline iteration summary": ("review_autofix_step_iteration_summary.sh", "warning"),
	"Re-trigger review via workflow_dispatch": ("review_autofix_step_post_commit_retrigger.sh", "error"),
	"Re-dispatch review on editor-changes-lost": ("review_autofix_step_changes_lost_redispatch.sh", "error"),
	"Count autofix iterations": ("review_autofix_step_count_iterations.sh", "error"),
	"Assemble failure evidence": ("review_autofix_step_assemble_failure_evidence.sh", "warning"),
	"Record model-provider outage": ("review_autofix_step_provider_outage_record.sh", "warning"),
	"Mark linked issues review-blocked (workflow failure)": ("review_autofix_step_mark_review_blocked_workflow_failure.sh", "error"),
	"Post review-blocked comment on PR (workflow failure)": ("review_autofix_step_failure_comment.sh", "error"),
	"Record Claude pool capacity": ("review_autofix_step_claude_pool_capacity.sh", "warning"),
	"Defer review on Claude capacity": ("review_autofix_step_claude_capacity_wait.sh", "error"),
	"Record Claude capacity wait": ("review_autofix_step_claude_capacity_wait.sh", "warning"),
	"Apply fixes with editor model": ("review_autofix_step_apply_fixes_editor.sh", "error"),
	"Restore same-head partial resume state": ("review_autofix_step_restore_partial_resume.sh", "error"),
	"Generate diff context": ("review_autofix_step_generate_diff_context.sh", "error"),
	"Preflight: Verify required files before reviewer invocation": ("review_autofix_step_preflight_required_files.sh", "error"),
	"Decide partial-finalize validation/push safety": ("review_autofix_step_partial_finalize_safety.sh", "error"),
	"Validate editor no-op disposition": ("review_autofix_step_editor_noop_disposition.sh", "error"),
	"Stage codex logs for upload (failure or empty-editor)": ("review_autofix_step_stage_codex_logs.sh", "warning"),
	"Initialize runtime workspace": ("review_autofix_step_init_runtime_workspace.sh", "error"),
	"Post review-blocked comment on PR (autofix exhaustion)": ("review_autofix_step_post_review_blocked_comment.sh", "error"),
	"Post editor summary comment": ("review_autofix_step_post_editor_summary.sh", "error"),
	"Mark linked issues ready to merge": ("review_autofix_step_mark_ready_to_merge.sh", "error"),
	"Detect smoke test and tune LLM settings": ("review_autofix_step_detect_smoke_test.sh", "error"),
	"Evaluate review gate": ("review_autofix_step_evaluate_gate.sh", "error"),
}

# Gate-job step scripts -> the verified checkout directory their wrapper reads.
# The gate job runs before support staging, so these are loaded only from the
# SHA-pinned head-gate sparse checkout, never from SUPPORT_SCRIPTS_DIR,
# .codex-workflow-src or a PR-head copy.
REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS: dict[str, str] = {
	"review_autofix_step_evaluate_gate.sh": ".codex-head-gate-src",
}

# test-and-mark-stable.yml step name -> script under scripts/. A missing or
# unverified script always fails the step.
TEST_AND_MARK_STABLE_STEP_SCRIPTS: dict[str, str] = {
	"Phase 4: Wait for review & autofix to complete": "stable_e2e_step_wait_review.sh",
	"Phase 4b: Verify editor restored canary (pytest + retry)": "stable_e2e_step_verify_bait_removed.sh",
}

_WRAPPER_START_RE = re.compile(
	r'^(?P<indent> *)REVIEW_AUTOFIX_STEP_SCRIPT="(?:\$\{SUPPORT_SCRIPTS_DIR:-\}|\$\{GITHUB_WORKSPACE\}/\.codex-head-gate-src/scripts)/(?P<script>review_autofix_step_[a-z0-9_]+\.sh)"$'
)
_WRAPPER_END = 'source "${REVIEW_AUTOFIX_STEP_SCRIPT}"'
_STABLE_E2E_WRAPPER_START_RE = re.compile(
	r'^(?P<indent> *)STABLE_E2E_STEP_SCRIPT="\$\{RUNNER_TEMP\}/stable-e2e-step-scripts/(?P<script>stable_e2e_step_[a-z0-9_]+\.sh)"$'
)
_STABLE_E2E_WRAPPER_END = 'source "${STABLE_E2E_STEP_SCRIPT}"'


def _step_script_body(path: Path) -> str:
	"""Return a step script file without its leading comment header."""
	lines = path.read_text(encoding="utf-8").split("\n")
	idx = 0
	while idx < len(lines) and lines[idx].startswith("#"):
		idx += 1
	body = "\n".join(lines[idx:])
	return body[:-1] if body.endswith("\n") else body


def review_autofix_step_script_body(script_name: str) -> str:
	"""Return a step script without its leading comment header.

	The header is the shebang plus the comment block before the first
	non-comment line; everything after it is the step body verbatim.
	"""
	return _step_script_body(REVIEW_AUTOFIX_STEP_SCRIPTS_DIR / script_name)


def _expand_step_scripts(workflow_text: str, start_re: re.Pattern[str], end_line: str) -> str:
	lines = workflow_text.split("\n")
	out: list[str] = []
	idx = 0
	while idx < len(lines):
		match = start_re.match(lines[idx])
		if not match:
			out.append(lines[idx])
			idx += 1
			continue
		indent = match.group("indent")
		end = idx
		while end < len(lines) and lines[end] != indent + end_line:
			end += 1
		if end >= len(lines):
			raise AssertionError(f"Unterminated step-script wrapper for {match.group('script')}")
		body = _step_script_body(REVIEW_AUTOFIX_STEP_SCRIPTS_DIR / match.group("script"))
		for body_line in body.split("\n"):
			out.append(indent + body_line if body_line else "")
		idx = end + 1
	return "\n".join(out)


def expand_review_autofix_step_scripts(workflow_text: str) -> str:
	"""Replace every step-script wrapper in *workflow_text* with its body."""
	return _expand_step_scripts(workflow_text, _WRAPPER_START_RE, _WRAPPER_END)


def expanded_review_autofix_text() -> str:
	"""review_autofix.yml with each moved step body inlined again."""
	return expand_review_autofix_step_scripts(REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8"))


def expand_test_and_mark_stable_step_scripts(workflow_text: str) -> str:
	"""Replace every stable_e2e_step wrapper in *workflow_text* with its body."""
	return _expand_step_scripts(workflow_text, _STABLE_E2E_WRAPPER_START_RE, _STABLE_E2E_WRAPPER_END)


def expanded_test_and_mark_stable_text() -> str:
	"""test-and-mark-stable.yml with each moved step body inlined again."""
	return expand_test_and_mark_stable_step_scripts(TEST_AND_MARK_STABLE_WORKFLOW_PATH.read_text(encoding="utf-8"))
