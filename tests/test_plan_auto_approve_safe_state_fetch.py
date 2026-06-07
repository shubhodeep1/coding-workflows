#!/usr/bin/env python3
"""Contract tests for plan auto-approve issue-state fetch hardening."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"


def _workflow_text() -> str:
	return PLAN_WF.read_text(encoding="utf-8")


def _step_block(text: str, step_name: str) -> str:
	marker = f"- name: {step_name}"
	start = text.find(marker)
	assert start != -1, f"Missing workflow step: {step_name}"
	next_step = text.find("\n      - name:", start + len(marker))
	if next_step == -1:
		return text[start:]
	return text[start:next_step]


def test_auto_approve_step_uses_safe_issue_state_fetch() -> None:
	block = _step_block(_workflow_text(), "Auto-approve clear plan")

	assert 'source scripts/gh_helpers.sh 2>/dev/null || true' in block
	assert 'type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }' in block
	assert 'type _safe_gh_jq >/dev/null 2>&1 || _safe_gh_jq() {' in block
	assert 'if ! _tmpf=$(mktemp "${TMPDIR:-/tmp}/_safe_gh_jq.XXXXXX" 2>/dev/null); then' in block
	assert 'echo "::error::_safe_gh_jq: failed to create temp file (mktemp failed); aborting without running: $*" >&2' in block
	assert 'if gh api "$@" > "${_tmpf}"; then' in block
	assert '_tmpf="$(mktemp "${TMPDIR:-/tmp}/_safe_gh_jq.XXXXXX" 2>/dev/null)" || return 1' not in block
	assert 'if gh api "$@" > "${_tmpf}" 2>/dev/null; then' not in block
	assert (
		'CURRENT_STATE="$(gh_retry _safe_gh_jq "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" '
		'--jq \'.state // "open"\' 2>/dev/null || echo "open")"'
	) in block
	assert (
		'CURRENT_STATE="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" '
		'--jq \'.state // "open"\' 2>/dev/null || echo "open")"'
	) not in block


def test_auto_approve_step_preserves_outputs_and_comment_payload() -> None:
	block = _step_block(_workflow_text(), "Auto-approve clear plan")

	assert 'echo "auto_approved=false" >> "$GITHUB_OUTPUT"' in block
	assert 'if [ "${CURRENT_STATE}" = "closed" ]; then' in block
	assert 'echo "auto_approved=true" >> "$GITHUB_OUTPUT"' in block
	assert 'gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" \\' in block
	assert "-f body=$'/approved [auto-approved-by-plan]\\n\\nAuto approval was posted because AUTO_IMPLEMENT_ON_CLEAR_PLAN is enabled.'" in block
	assert 'echo "AUTO_IMPLEMENT_ON_CLEAR_PLAN disabled; skipping auto-approval comment."' in block


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
