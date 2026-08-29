#!/usr/bin/env python3
"""Contract tests for the resolver-loop reasoning step-down on
per-attempt-timer kills.

PR #2453 added the `timeout-aware` retry prelude so the model's
prompt acknowledges a previous attempt was killed by the
per-attempt timer.  But the prelude only changes the prompt — it
does not change codex's reasoning level.  The job log linked to
shubhodeep1/bitsafe.io PR #155 (workflow run dated 2026-05-11)
showed attempt 1 at `xhigh` consuming the entire 3000s per-
attempt budget without invoking `apply_patch`, and attempt 2
queued at the same `xhigh` level — the prelude alone could not
break the hung-thinking failure mode.

scripts/review_conflict_resolve.sh therefore walks the README.md
"Thinking levels" ladder one step down (xhigh → high → medium →
none) on every consecutive timeout-classified retry.  These
tests pin three contracts at the source level:

  1. `_apply_resolver_reasoning_effort` and
     `_next_lower_reasoning_effort` are defined and the selected level is
     passed to OpenCode as the per-attempt variant.
  2. The retry loop's step-down block is gated on
     `_prev_attempt_failure_kind == "timeout"` (so non-timeout
     failure kinds — exec_error, validation — do not falsely
     downgrade the level).
  3. The ladder itself returns the expected next level for every
     input (xhigh → high, high → medium, medium → none, none →
     none, invalid → high).  The ladder helper is extracted into
     a sourceable shell snippet and exercised in a subprocess
     because that catches regressions where the literal `case`
     arms drift from the README.md ladder.

Source-level contract pinning mirrors the pattern in
`test_review_conflict_resolve_retry_prelude_render.py` —
robust to renderer-style refactors that swap how the override
is applied without changing its semantics.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"


def _resolve_script_text() -> str:
	return RESOLVE_SCRIPT.read_text(encoding="utf-8")


def test_apply_helper_defined_and_selects_opencode_variant() -> None:
	"""The compatibility helper must validate and select the next variant."""
	src = _resolve_script_text()
	assert "_apply_resolver_reasoning_effort()" in src, (
		"scripts/review_conflict_resolve.sh must define "
		"`_apply_resolver_reasoning_effort()` so the retry loop "
		"can select a new OpenCode variant between attempts. If this helper is gone, "
		"step-down silently no-ops and we regress to the "
		"pre-step-down behaviour of retrying at the same level."
	)
	# The helper must update the tracker consumed by the OpenCode command.
	apply_idx = src.find("_apply_resolver_reasoning_effort()")
	# Bound the slice by the next top-level function header so an
	# accidental capture of unrelated tracker references
	# elsewhere in the file does not satisfy the contract.
	end_marker = "\n_next_lower_reasoning_effort()"
	end_idx = src.find(end_marker, apply_idx)
	assert end_idx != -1, (
		"Could not locate `_next_lower_reasoning_effort()` after "
		"`_apply_resolver_reasoning_effort()`; if the layout was "
		"refactored, update this test's slicing anchor."
	)
	body = src[apply_idx:end_idx]
	assert '_current_reasoning_effort="${_arr_level}"' in body, (
		"`_apply_resolver_reasoning_effort()` must update the current "
		"reasoning tracker consumed by OpenCode."
	)
	# Defence-in-depth: the helper must validate its input against
	# the same xhigh|high|medium|none allowlist used at startup,
	# so an in-loop bug that passes an unexpected level cannot
	# corrupt the TOML.
	assert "xhigh|high|medium|none" in body, (
		"`_apply_resolver_reasoning_effort()` must validate its "
		"level argument against the xhigh|high|medium|none "
		"allowlist before interpolating into sed — same defence "
		"applied to CONFLICT_RESOLVER_REASONING_EFFORT at startup."
	)
	command_idx = src.index("resolver_opencode_cmd=(")
	command_body = src[command_idx:command_idx + 600]
	assert "writer" in command_body
	assert '"${_current_reasoning_effort}"' in command_body
	assert '"${RESOLVER_OPENCODE_CONFIG}"' in command_body
	assert ".codex/config.toml" not in body


def test_next_lower_helper_defined() -> None:
	"""`_next_lower_reasoning_effort` must exist as a separate
	helper so it is independently testable and re-usable from any
	future caller (e.g. a related step-down on the editor side)."""
	src = _resolve_script_text()
	assert "_next_lower_reasoning_effort()" in src, (
		"scripts/review_conflict_resolve.sh must define "
		"`_next_lower_reasoning_effort()` — the retry loop reads "
		"its return value to decide the next attempt's level."
	)


def test_initial_apply_call_uses_current_effort_tracker() -> None:
	"""The once-per-step prelude must seed `_current_reasoning_effort`
	from the validated env-provided level AND call the helper.
	If either is missing, the retry loop's step-down has nothing
	to start from and the initial OpenCode invocation runs with the wrong
	variant."""
	src = _resolve_script_text()
	assert '_current_reasoning_effort="${_resolver_reasoning_effort}"' in src, (
		"`_current_reasoning_effort` must be seeded from the "
		"validated `_resolver_reasoning_effort` so the retry loop "
		"step-down ladder starts at the operator-chosen level."
	)
	assert '_apply_resolver_reasoning_effort "${_current_reasoning_effort}"' in src, (
		"The once-per-step prelude must call "
		"`_apply_resolver_reasoning_effort \"${_current_reasoning_effort}\"` "
		"to keep the initial config-rewrite behaviour after the "
		"refactor — without this call the resolver inherits the "
		"editor's reasoning level on attempt 1."
	)


def test_loop_step_down_is_gated_on_timeout_failure_kind() -> None:
	"""The step-down block must only fire when
	`_prev_attempt_failure_kind == "timeout"`. Triggering it on
	exec_error or validation failures would be actively harmful:
	those failure kinds are not evidence that thinking budget was
	the bottleneck, and downgrading after a transient codex crash
	would cascade the rest of the run into floor-level reasoning."""
	src = _resolve_script_text()
	# Locate the loop body — bounded by the resolver loop header
	# and the closing `done` — and check the step-down block is
	# inside it and gated on timeout.
	loop_header = (
		'while [ "${attempt}" -le "${INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS}" ]; do'
	)
	loop_idx = src.find(loop_header)
	assert loop_idx != -1, (
		"Resolver retry loop header not found; if the loop was "
		"renamed, update this test's slicing anchor."
	)
	# The step-down block must exist and live inside the loop body.
	gate = 'if [ "${_prev_attempt_failure_kind:-}" = "timeout" ]; then'
	gate_idx = src.find(gate, loop_idx)
	assert gate_idx != -1, (
		"Resolver loop must contain a step-down block gated on "
		"`_prev_attempt_failure_kind == \"timeout\"`. Without the "
		"gate the ladder would fire on exec_error / validation "
		"failures and falsely downgrade reasoning for transient "
		"codex crashes."
	)
	# Inside the gate, the next-level computation must be present.
	# Bound the slice by the gate's closing `fi` to keep this check
	# load-bearing.
	# The fi for this gate is the second-following `fi` after the
	# inner if-else; search forwards for the conservative pattern
	# `      fi` (six-space indent matching the loop body indent).
	# We use a lighter pattern: a forward search for the helper
	# invocation, bounded loosely by the next blank-line block.
	tail = src[gate_idx:gate_idx + 2000]
	assert '_next_lower_reasoning_effort "${_current_reasoning_effort}"' in tail, (
		"Step-down block must call `_next_lower_reasoning_effort` "
		"with the current level — passing a stale literal would "
		"not advance the ladder on consecutive timeouts."
	)
	assert '_apply_resolver_reasoning_effort "${_next_level}"' in tail, (
		"Step-down block must re-apply the new level via "
		"`_apply_resolver_reasoning_effort` so the next OpenCode "
		"invocation actually picks it up."
	)
	# Floor-handling: the block must log a distinct message when
	# the next-lower level equals the current level (ladder
	# exhausted at `none`). Without this branch the operator log
	# would silently no-op every time, masking the fact that the
	# resolver is stuck at floor.
	assert "ladder floor reached" in tail, (
		"Step-down block must explicitly log when the ladder "
		"floor (`none`) has been reached so the operator log "
		"shows the difference between a successful downgrade and "
		"a stuck-at-floor retry."
	)


def test_next_lower_ladder_returns_expected_levels() -> None:
	"""Behavioural test: extract `_next_lower_reasoning_effort`
	from the script and exercise it for every README.md level
	plus an invalid input. The ladder is xhigh → high → medium →
	none with `none` mapping to `none` (floor) and an invalid
	input falling back to `high` (the repo-wide default)."""
	src = _resolve_script_text()
	# Splice out the helper definition so we can source it
	# without running the rest of the script (which has
	# `set -euo pipefail` and unrelated side effects). Match
	# from the function header to the line that contains only
	# the closing brace — `[^}]*` cannot be used here because the
	# body legitimately contains `}` inside `${1:-}` parameter
	# expansions.
	match = re.search(
		r"^_next_lower_reasoning_effort\(\)\n\{\n.*?\n\}\n",
		src,
		flags=re.DOTALL | re.MULTILINE,
	)
	assert match is not None, (
		"Could not extract `_next_lower_reasoning_effort()` from "
		"the script for behavioural testing. The function body "
		"must use brace-on-next-line style (consistent with §9 "
		"code style) and end on a line containing only the "
		"closing brace."
	)
	helper_src = match.group(0)
	expectations = [
		("xhigh", "high"),
		("high", "medium"),
		("medium", "none"),
		("none", "none"),
		("garbage", "high"),
		("", "high"),
	]
	for level_in, level_out in expectations:
		script = f"{helper_src}\n_next_lower_reasoning_effort {level_in!r}\n"
		result = subprocess.run(
			["bash", "-c", script],
			check=True,
			capture_output=True,
			text=True,
		)
		got = result.stdout.strip()
		assert got == level_out, (
			f"_next_lower_reasoning_effort({level_in!r}) returned "
			f"{got!r}; expected {level_out!r}. The ladder is "
			"xhigh → high → medium → none, with `none` mapping "
			"to itself (floor) and unknown inputs falling back "
			"to `high` (the resolver's repo-wide default)."
		)


def main() -> int:
	"""Allow this file to run via `python3 tests/<file>` (the
	CI/release allowlist invocation pattern in .github/workflows/
	ci.yml, mark-stable.yml, and test-and-mark-stable.yml) without
	requiring pytest on the runner. Mirrors the sibling
	test_review_conflict_resolve_retry_prelude_render.py shape."""
	test_apply_helper_defined_and_selects_opencode_variant()
	test_next_lower_helper_defined()
	test_initial_apply_call_uses_current_effort_tracker()
	test_loop_step_down_is_gated_on_timeout_failure_kind()
	test_next_lower_ladder_returns_expected_levels()
	print(
		"OK: review_conflict_resolve reasoning-effort step-down "
		"contract holds (helper definitions, _current_reasoning_effort "
		"tracker, timeout-gated step-down, xhigh→high→medium→none "
		"ladder)"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
