#!/usr/bin/env python3
"""Tests for reviewer reasoning-effort behaviour in review_autofix.yml.

The cycle-based schedule machinery (REVIEW_REASONING_SCHEDULE,
REVIEW_AUTODOWNGRADE_DISABLED) has been removed. Non-smoke-test runs use
the configured THINKING_LEVEL_* for every cycle. Smoke test runs override
reviewer reasoning to ``low`` and editor reasoning to ``medium`` (split
introduced after run 25308327160 showed the legacy editor default at reasoning=low
produces empty output — same failure mode as reasoning=none).
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
CHECK_RUNS_HELPER = REPO_ROOT / "scripts" / "collect_pr_check_runs_context.py"
REVIEWERS_SCRIPT = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
OPENCODE_HELPERS = REPO_ROOT / "scripts" / "opencode_helpers.sh"


def _workflow() -> str:
	return REVIEW_AUTOFIX_WF.read_text(encoding="utf-8")


def _step_block(step_name: str) -> str:
	wf = _workflow()
	match = re.search(rf'(?ms)^([ \t]*)- name: {re.escape(step_name)}\n.*?(?=^\1- |\Z)', wf)
	assert match is not None, step_name
	return match.group(0)


def test_no_reasoning_schedule_env_vars() -> None:
	wf = _workflow()
	assert "REVIEW_REASONING_SCHEDULE" not in wf
	assert "REVIEW_AUTODOWNGRADE_DISABLED" not in wf


def test_no_cycle_selector_step() -> None:
	wf = _workflow()
	assert "Select reviewer reasoning effort for current cycle" not in wf
	assert "schedule_source" not in wf


def test_reviewer_attempt_reasoning_is_passed_as_opencode_variant() -> None:
	reviewers = REVIEWERS_SCRIPT.read_text(encoding="utf-8")
	helpers = OPENCODE_HELPERS.read_text(encoding="utf-8")
	assert '"${attempt_reasoning}"' in reviewers
	assert 'opencode_run_cmd "$@"' in reviewers
	assert '--variant "${variant}"' in helpers
	assert 'reviewer_prepare_reasoning_configs()' in reviewers
	prepare_block = reviewers.split('reviewer_prepare_reasoning_configs()', 1)[1].split('\n}', 1)[0]
	assert "reviewer_patch_reasoning_config_file" not in prepare_block
	assert "per-call --variant argument" in prepare_block


def test_smoke_test_reasoning_split() -> None:
	"""Smoke runs pin reviewer to low and editor to medium (split since run 25308327160)."""
	wf = _workflow()
	assert 'REVIEWER_REASONING_EFFORT=low' in wf
	assert 'EDITOR_REASONING_EFFORT=medium' in wf
	# The reviewer-config sed still patches to low; editor re-patches separately.
	assert 'model_reasoning_effort = "low"' in wf
	# Ensure the old all-low assignment is gone.
	assert 'EDITOR_REASONING_EFFORT=low' not in wf


def test_no_pr_claude_branch_review_uses_lightweight_reviewer_profile() -> None:
	block = _step_block("Use lightweight reviewer profile for no-PR claude-branch-review")
	assert "if: env.CLAUDE_BRANCH_REVIEW_MODE == 'true' && env.PR_NUMBER == ''" in block
	assert 'echo "ENABLE_REVIEWER_TWO_PASS=false" >> "$GITHUB_ENV"' in block
	assert 'echo "REVIEWER_REASONING_EFFORT=low" >> "$GITHUB_ENV"' in block
	assert "mapfile -t reviewer_models < <(" in block
	assert "head -n 3" in block
	assert "REVIEWER_MODELS<<__NO_PR_REVIEWER_MODELS__" in block
	assert "printf '%s\\n' \"${reviewer_models[@]}\"" in block
	assert 'sed -i \'s/^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*".*"/model_reasoning_effort = "low"/\' ~/.codex/config.toml' in block
	assert 'CLAUDE_BRANCH_REVIEW_LIGHT_PROFILE mode=no_pr reviewer_reasoning=low reviewer_two_pass=false reviewer_count=${#reviewer_models[@]}' in block


def test_check_runs_wait_timeout_default_and_fallback_are_aligned() -> None:
	wf = _workflow()
	helper = CHECK_RUNS_HELPER.read_text(encoding="utf-8")
	assert "CHECK_RUNS_WAIT_TIMEOUT_SECS: ${{ vars.CHECK_RUNS_WAIT_TIMEOUT_SECS || '300' }}" in wf
	assert "DEFAULT_WAIT_TIMEOUT_SECS = 300" in helper
	assert "MAX_WAIT_TIMEOUT_SECS = 3600" in helper
	assert "CHECK_RUNS_WAIT_TIMEOUT_SECS: ${{ vars.CHECK_RUNS_WAIT_TIMEOUT_SECS || '900' }}" not in wf
	assert "DEFAULT_WAIT_TIMEOUT_SECS = 900" not in helper


def test_editor_switch_replaces_any_reasoning_value() -> None:
	wf = _workflow()
	assert 'sed -i "s/^model_reasoning_effort = \\".*\\"/model_reasoning_effort = \\"${EDITOR_REASONING_EFFORT}\\"/" ~/.codex/config.toml' in wf
	assert 'sed -i "s/model_reasoning_effort = \\"${REVIEWER_REASONING_EFFORT}\\"/model_reasoning_effort = \\"${EDITOR_REASONING_EFFORT}\\"/" ~/.codex/config.toml' not in wf


def test_conflict_resolver_reasoning_env_wired() -> None:
	"""Resolver step must pass CONFLICT_RESOLVER_REASONING_EFFORT to
	review_conflict_resolve.sh so the smoke-test editor's reasoning=none
	override doesn't starve the resolver (PR #2058 / run 25300219172).
	"""
	wf = _workflow()
	assert "CONFLICT_RESOLVER_REASONING_EFFORT:" in wf
	assert "vars.THINKING_LEVEL_CONFLICT_RESOLVER" in wf
	resolver_script = (REPO_ROOT / "scripts" / "review_conflict_resolve.sh").read_text(encoding="utf-8")
	assert "CONFLICT_RESOLVER_REASONING_EFFORT" in resolver_script
	# Validation must match README's documented levels (xhigh|high|medium|none)
	# — `low` is not a documented level and must not be silently accepted.
	assert "xhigh|high|medium|none)" in resolver_script
	assert "xhigh|high|medium|low|none" not in resolver_script
	# grep/sed must tolerate whitespace + unquoted variants so a non-canonical
	# config doesn't silently no-op and leave the resolver on stale `none`.
	assert "[[:space:]]*model_reasoning_effort[[:space:]]*=" in resolver_script
