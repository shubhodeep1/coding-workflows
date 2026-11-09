#!/usr/bin/env python3
"""Implementation-failed re-issue loop: create the replacement before closing.

BUG-002 (analysis/workflow-optimization-2026-10-07-2.md): the loop in
scripts/orchestrate_poll_process.sh relabelled and closed the source issue
before `gh issue create` ran. A failed issue read left the title empty, the
create failed, and the source stayed closed while the state still mapped the
wave entry to it, so the task was silently lost. The no-op counter was also
bumped before the create, so a failed create spent MAX_IMPL_NOOP_REISSUES.

These static contract cases pin the new order: an unreadable snapshot defers
with no mutation, the replacement is created first, and only a confirmed
create (numeric NEW_ISSUE_NUM) rewrites state, bumps the counter and
relabels/closes the source. ci.yml is outside the file grant of the issue
that added this file, so tests/test_alert_levels_human_only.py collects
these cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"

LOOP_START = "  while read -r if_issue; do\n"
LOOP_END = "  done < <(echo \"${WAVE_STATUS}\" | jq -r '.issues[] | select(.status == \"implementation-failed\") | .github_issue')"
REGION_START = 'IF_ISSUE_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${if_issue}"'
CONFIRMED = 'if [[ "${NEW_ISSUE_NUM}" =~ ^[0-9]+$ ]]; then\n'
CREATE = "gh_retry gh issue create"
SOURCE_CLOSE = 'gh issue close "${if_issue}"'
RELABEL_CLOSED = "--add-label 'ai:closed'"
GUARD_LINE = 'echo "::warning::IMPL_FAILED_REISSUE issue=${if_issue} outcome=deferred reason=source_unreadable"'
BUMP = 'bump_impl_noop_count "${IF_LOCAL_ID}"'
CREATE_WARNING = 'echo "::warning::Could not create replacement issue for #${if_issue}."'


def _once(text: str, anchor: str) -> int:
	assert text.count(anchor) == 1, f"anchor must occur exactly once: {anchor!r} (found {text.count(anchor)})"
	return text.index(anchor)


def _loop() -> str:
	script = POLLER.read_text(encoding="utf-8")
	end = _once(script, LOOP_END)
	start = script.rindex(LOOP_START, 0, end)
	return script[start:end + len(LOOP_END)]


def _region() -> str:
	loop = _loop()
	return loop[_once(loop, REGION_START):]


def _confirmed_branch() -> tuple[int, int, int]:
	"""(if index, else index, done index) of the confirmed-create branch in the region."""
	region = _region()
	start = _once(region, CONFIRMED)
	else_index = region.index("\n    else\n", start)
	return start, else_index, region.index(LOOP_END)


def _all_indexes(text: str, needle: str) -> list[int]:
	found, at = [], text.find(needle)
	while at >= 0:
		found.append(at)
		at = text.find(needle, at + 1)
	return found


def test_create_precedes_source_close_and_relabel():
	region = _region()
	create = _once(region, CREATE)
	closes = _all_indexes(region, SOURCE_CLOSE)
	relabels = _all_indexes(region, RELABEL_CLOSED)
	assert len(closes) == 2 and len(relabels) == 1
	assert all(create < index for index in closes + relabels)


def test_close_and_relabel_only_after_confirmed_create():
	region = _region()
	start, else_index, _ = _confirmed_branch()
	for index in _all_indexes(region, SOURCE_CLOSE) + _all_indexes(region, RELABEL_CLOSED) + _all_indexes(region, 'ensure_label_exists "ai:closed"'):
		assert start < index < else_index


def test_create_failure_branch_has_no_mutation():
	region = _region()
	_, else_index, done = _confirmed_branch()
	branch = region[else_index:done]
	assert CREATE_WARNING in branch
	assert 'echo "IMPL_FAILED_REISSUE issue=${if_issue} outcome=deferred reason=create_failed"' in branch
	assert branch.index(CREATE_WARNING) < branch.index("reason=create_failed")
	for forbidden in ("gh issue close", "gh issue edit", "ai:closed", "bump_impl_noop_count", "STATE_FILE", "IMPL_FAILED_STATE_CHANGED"):
		assert forbidden not in branch, forbidden


def test_unreadable_snapshot_continues_before_mutation():
	region = _region()
	guard = _once(region, GUARD_LINE)
	guard_if = region.rindex("\n    if ", 0, guard)
	condition = region[guard_if:guard]
	assert '[ -z "${IF_ISSUE_JSON}" ]' in condition
	assert '[ -z "${IF_TITLE}" ]' in condition
	assert '[ "${IF_TITLE}" = "null" ]' in condition
	cont = region.index("      continue\n", guard)
	for mutation in ("sync_implementation_fixup_blockers", "ensure_label_exists", "gh issue edit", "gh issue close", "gh issue create", "bump_impl_noop_count", "STATE_FILE"):
		assert mutation not in region[:cont], mutation
		assert cont < region.index(mutation), mutation


def test_noop_bump_after_confirmed_create():
	loop = _loop()
	bumps = _all_indexes(loop, BUMP)
	assert len(bumps) == 2
	cap_echo = _once(loop, "hit implementation no-op cap")
	cap_continue = loop.index("        continue\n", cap_echo)
	assert cap_echo < bumps[0] < cap_continue
	confirmed = _once(loop, CONFIRMED)
	rewrite = _once(loop, ".issue_number_map[$local_id] = $new_issue_num")
	assert confirmed < rewrite < bumps[1]
	assert bumps[1] < loop.index("\n    else\n", confirmed)
	reissue_echo = loop.index("Closing and re-issuing...")
	read = _once(loop, REGION_START)
	assert not any(reissue_echo < index < read for index in bumps)


def test_existing_log_and_templates_preserved():
	region = _region()
	_once(region, CREATE_WARNING)
	assert region.count("**⚠️ Re-issued from #${if_issue}** — the previous implementation attempt failed during post-Codex syntax/validation checks.") == 1
	assert region.count("**⚠️ Re-issued from #${if_issue}** — the previous implementation attempt produced no repository changes.") == 1
	guard = _once(region, GUARD_LINE)
	sync = _once(region, 'sync_implementation_fixup_blockers "${if_issue}"')
	assert guard < sync < region.index(CREATE)


def test_log_prefix_registered():
	# The release fold moves the fragment into agents.md and deletes it, so
	# read the rendered agents text (agents.md plus any pending fragments).
	sys.path.insert(0, str(Path(__file__).resolve().parent))
	from agents_doc import agents_text

	text = agents_text()
	assert "- `IMPL_FAILED_REISSUE`" in text
	assert "LOG_PREFIX.name=IMPL_FAILED_REISSUE" in text


def test_new_issue_num_reset_per_iteration():
	region = _region()
	reset = _once(region, '    NEW_ISSUE_NUM=""\n')
	parse = _once(region, 'NEW_ISSUE_NUM="$(basename "${NEW_ISSUE_URL_CLEAN%%[?#]*}")"')
	assert region.index(CREATE) < reset < parse < region.index(CONFIRMED)


def test_reissued_outcome_logged_after_close():
	region = _region()
	start, else_index, _ = _confirmed_branch()
	line = _once(region, 'echo "IMPL_FAILED_REISSUE issue=${if_issue} outcome=reissued new_issue=${NEW_ISSUE_NUM} mode=${IF_MODE}"')
	assert max(_all_indexes(region, SOURCE_CLOSE)) < line < else_index
	state_failed = _once(region, 'echo "::warning::IMPL_FAILED_REISSUE issue=${if_issue} outcome=state_update_failed new_issue=${NEW_ISSUE_NUM}"')
	assert start < state_failed < min(_all_indexes(region, SOURCE_CLOSE))
