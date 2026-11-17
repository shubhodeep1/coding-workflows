#!/usr/bin/env python3
"""Standalone conflict sweep outcome telemetry.

Poller run 36947018303 counted PR #5360 as "fixed" after update-branch
returned HTTP 422 and the review it dispatched did nothing: the legacy
CONFLICT_SWEEP_FIXED counter adds both update-branch successes and review
dispatches. The sweep now also logs one STANDALONE_CONFLICT_DISPATCH_OUTCOME
line per conflicted PR and one STANDALONE_CONFLICT_SWEEP_SUMMARY line per
tick. These tests run the real loop slice from
scripts/orchestrate_poll_process.sh with stubbed API helpers.

CI runs these cases through tests/test_alert_levels_human_only.py, which
collects them; keep the test names unique across both files.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"

HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40
HEAD_D = "d" * 40

LOOP_START = "for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do"
LOOP_END = 'echo "Standalone conflict sweep complete.'

OUTCOME_RE = re.compile(
	r"^STANDALONE_CONFLICT_DISPATCH_OUTCOME pr=(?P<pr>[0-9]+) head=(?P<head>[0-9a-f]{40}|unknown) "
	r"update_branch=(?P<update_branch>ok|failed|skipped_no_sha) dispatch=(?P<dispatch>ok|inflight|failed|not_attempted) "
	r"prior_attempts_same_head=(?P<prior>[0-9]+|unknown) resolution=(?P<resolution>pending|unresolved_same_head|branch_updated)$"
)
SUMMARY_RE = re.compile(
	r"^STANDALONE_CONFLICT_SWEEP_SUMMARY listed=(?P<listed>[0-9]+) prefiltered_draft=(?P<prefiltered_draft>[0-9]+) "
	r"skipped_integration=(?P<skipped_integration>[0-9]+) batch_clean=(?P<batch_clean>[0-9]+) "
	r"rest_fetched=(?P<rest_fetched>[0-9]+) api_updated=(?P<api_updated>[0-9]+) dispatched=(?P<dispatched>[0-9]+) "
	r"dispatch_inflight=(?P<dispatch_inflight>[0-9]+) dispatch_failed=(?P<dispatch_failed>[0-9]+) "
	r"unresolved_same_head=(?P<unresolved_same_head>[0-9]+) list_limit=3000 list_truncated=(?P<list_truncated>true|false) list_failed=(?P<list_failed>true|false)$"
)


def _require_jq_for_sweep_outcome() -> None:
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")


def _bash_function(script: str, signature: str) -> str:
	start = script.index(signature)
	end = script.index("\n}\n", start) + 3
	return script[start:end]


def _sweep_section() -> str:
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	return script[script.index('echo "Standalone PR conflict sweep"'):script.index(LOOP_END)]


def _pr(number: int, head_ref: str, oid: str, *, base: str = "main", draft: bool = False) -> dict:
	return {"number": number, "headRefName": head_ref, "headRefOid": oid, "baseRefName": base, "isDraft": draft}


def _dirty(head_ref: str, sha: str, labels: list[str] | None = None) -> dict:
	return {
		"state": "open",
		"mergeable_state": "dirty",
		"head": {"ref": head_ref, "sha": sha},
		"labels": [{"name": name} for name in (labels or [])],
	}


def _run_sweep(
	prs: list[dict],
	pr_json: dict[int, dict],
	*,
	update_ok: tuple[int, ...] = (),
	dispatch_rc: dict[int, int] | None = None,
	clean: dict[int, str] | None = None,
	attempts_state: dict[int, str] | None = None,
	loaded: dict[int, str] | None = None,
	login: str | None = "bot",
	declare_maps: bool = True,
) -> dict:
	"""Run the real sweep loop slice; return stdout lines and call logs."""
	_require_jq_for_sweep_outcome()
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	loop = script[script.index(LOOP_START):script.index(LOOP_END)]
	helpers = (
		_bash_function(script, "_linked_pr_is_merge_queued()\n{")
		+ _bash_function(script, "_linked_pr_is_capacity_waiting()\n{")
	)
	dispatch_rc = dispatch_rc or {}
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		pr_dir = tmp_path / "prs"
		pr_dir.mkdir()
		for number, payload in pr_json.items():
			(pr_dir / f"{number}.json").write_text(json.dumps(payload), encoding="utf-8")
		gh_log = tmp_path / "gh.log"
		rest_log = tmp_path / "rest.log"
		tg_log = tmp_path / "tg.log"
		update_cases = "".join(f'*"pulls/{n}/update-branch"*) return 0 ;; ' for n in update_ok)
		dispatch_cases = "".join(f"{n}) return {rc} ;; " for n, rc in dispatch_rc.items())
		lines = [
			"set -euo pipefail",
			"GITHUB_REPOSITORY=o/r",
			f"GH_LOG={shlex.quote(str(gh_log))}",
			f"REST_LOG={shlex.quote(str(rest_log))}",
			f"TG_LOG={shlex.quote(str(tg_log))}",
			f"PR_DIR={shlex.quote(str(pr_dir))}",
			"tg_send_msg() { printf '%s\\n' \"${2:-}\" >> \"${TG_LOG}\"; printf 1; }",
			'gh_retry() { "$@"; }',
			"_gh_url() { printf 'https://github.com/o/r/%s' \"$1\"; }",
			'_safe_gh_jq() { printf \'%s\\n\' "$1" >> "${REST_LOG}"; local f="${PR_DIR}/${1##*/}.json"; if [ -f "${f}" ]; then cat "${f}"; fi; }',
			'gh() { printf \'%s\\n\' "$*" >> "${GH_LOG}"; case "$*" in ' + update_cases + "esac; return 1; }",
			'_dispatch_review_for_conflicts() { echo "DISPATCH pr=$1"; case "$1" in ' + dispatch_cases + "esac; return 1; }",
			'_standalone_conflict_count_attempt() { echo "COUNT pr=$1 head=$2 rc=$3"; }',
			"declare -A _STANDALONE_CLEAN_PRS=()",
			f"STANDALONE_PRS={shlex.quote(json.dumps(prs))}",
			f"STANDALONE_COUNT={len(prs)}",
			# Like the existing sliced harnesses, only the legacy counter is predefined.
			"CONFLICT_SWEEP_FIXED=0",
		]
		if login is not None:
			lines.append(f"UNBLOCK_TRUSTED_LOGIN={shlex.quote(login)}")
		if declare_maps:
			lines.append("declare -A _STANDALONE_CONFLICT_ATTEMPTS_STATE=()")
			lines.append("declare -A _STANDALONE_CONFLICT_STATE_LOADED=()")
		for number, oid in (clean or {}).items():
			lines.append(f'_STANDALONE_CLEAN_PRS["{number}"]={shlex.quote(oid)}')
		for number, raw in (attempts_state or {}).items():
			lines.append(f'_STANDALONE_CONFLICT_ATTEMPTS_STATE["{number}"]={shlex.quote(raw)}')
		for number, value in (loaded or {}).items():
			lines.append(f'_STANDALONE_CONFLICT_STATE_LOADED["{number}"]={shlex.quote(value)}')
		harness = tmp_path / "harness.sh"
		harness.write_text("\n".join(lines) + "\n" + helpers + loop + 'echo "FIXED=${CONFLICT_SWEEP_FIXED}"\n', encoding="utf-8")
		result = subprocess.run(["bash", str(harness)], capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
		out = result.stdout.splitlines()
		outcomes = {}
		for line in out:
			if line.startswith("STANDALONE_CONFLICT_DISPATCH_OUTCOME "):
				match = OUTCOME_RE.match(line)
				assert match, line
				assert match["pr"] not in outcomes, line
				outcomes[match["pr"]] = match.groupdict()
		summaries = [line for line in out if line.startswith("STANDALONE_CONFLICT_SWEEP_SUMMARY ")]
		assert len(summaries) == 1, out
		summary = SUMMARY_RE.match(summaries[0])
		assert summary, summaries[0]
		return {
			"out": out,
			"outcomes": outcomes,
			"summary": summary.groupdict(),
			"gh": gh_log.read_text(encoding="utf-8").splitlines() if gh_log.exists() else [],
			"rest": rest_log.read_text(encoding="utf-8").splitlines() if rest_log.exists() else [],
		}


def test_sweep_outcome_update_branch_and_dispatch_outcomes():
	prs = [
		_pr(7, "ai/issue-7", HEAD_A),
		_pr(8, "ai/issue-8", HEAD_B),
		_pr(9, "ai/issue-9", HEAD_C),
		_pr(10, "ai/issue-10", HEAD_D),
	]
	pr_json = {
		7: _dirty("ai/issue-7", HEAD_A),
		8: _dirty("ai/issue-8", HEAD_B),
		9: _dirty("ai/issue-9", HEAD_C),
		10: _dirty("ai/issue-10", HEAD_D),
	}
	result = _run_sweep(prs, pr_json, update_ok=(7,), dispatch_rc={8: 0, 9: 2, 10: 1})
	outcomes = result["outcomes"]
	assert outcomes["7"] == {"pr": "7", "head": HEAD_A, "update_branch": "ok", "dispatch": "not_attempted", "prior": "unknown", "resolution": "branch_updated"}
	assert (outcomes["8"]["update_branch"], outcomes["8"]["dispatch"], outcomes["8"]["resolution"]) == ("failed", "ok", "pending")
	assert (outcomes["9"]["update_branch"], outcomes["9"]["dispatch"]) == ("failed", "inflight")
	assert (outcomes["10"]["update_branch"], outcomes["10"]["dispatch"]) == ("failed", "failed")
	assert "DISPATCH pr=7" not in result["out"]
	# Existing behaviour pinned: the legacy "Fixed" count still adds the
	# update-branch success and the ok dispatch, nothing else.
	assert "FIXED=2" in result["out"]
	# Existing attempt counting is unchanged: ok and failed dispatches count, inflight does not.
	assert f"COUNT pr=8 head={HEAD_B} rc=0" in result["out"]
	assert f"COUNT pr=10 head={HEAD_D} rc=1" in result["out"]
	assert not any(line.startswith("COUNT pr=9 ") for line in result["out"])
	summary = result["summary"]
	assert summary["listed"] == "4" and summary["rest_fetched"] == "4"
	assert (summary["api_updated"], summary["dispatched"], summary["dispatch_inflight"], summary["dispatch_failed"]) == ("1", "1", "1", "1")
	assert summary["unresolved_same_head"] == "0" and summary["list_truncated"] == "false"


def test_sweep_outcome_missing_head_sha_skips_update_branch():
	prs = [_pr(11, "ai/issue-11", HEAD_A)]
	result = _run_sweep(prs, {11: _dirty("ai/issue-11", "")}, dispatch_rc={11: 0})
	outcome = result["outcomes"]["11"]
	assert (outcome["head"], outcome["update_branch"], outcome["dispatch"]) == ("unknown", "skipped_no_sha", "ok")
	assert outcome["prior"] == "unknown" and outcome["resolution"] == "pending"
	assert not any("update-branch" in call for call in result["gh"])


def test_sweep_outcome_same_head_trusted_marker_is_unresolved():
	prs = [_pr(12, "ai/issue-12", HEAD_A)]
	result = _run_sweep(
		prs,
		{12: _dirty("ai/issue-12", HEAD_A)},
		dispatch_rc={12: 0},
		attempts_state={12: f"55|bot|{HEAD_A}|2|0"},
		loaded={12: "1"},
	)
	outcome = result["outcomes"]["12"]
	assert outcome["prior"] == "2" and outcome["resolution"] == "unresolved_same_head"
	assert result["summary"]["unresolved_same_head"] == "1"


def test_sweep_outcome_newer_untrusted_marker_does_not_shadow_trusted_one():
	prs = [_pr(13, "ai/issue-13", HEAD_A)]
	result = _run_sweep(
		prs,
		{13: _dirty("ai/issue-13", HEAD_A)},
		dispatch_rc={13: 1},
		attempts_state={13: f"60|intruder|{HEAD_A}|0|0\n55|bot|{HEAD_A}|3|1"},
		loaded={13: "1"},
	)
	assert result["outcomes"]["13"]["prior"] == "3"
	assert result["outcomes"]["13"]["resolution"] == "unresolved_same_head"


def test_sweep_outcome_partial_window_is_unknown_even_with_trusted_marker():
	# Issue #8847: partial state is reported as unknown / pending, never inferred.
	prs = [_pr(18, "ai/issue-18", HEAD_A)]
	result = _run_sweep(
		prs,
		{18: _dirty("ai/issue-18", HEAD_A)},
		dispatch_rc={18: 0},
		attempts_state={18: f"55|bot|{HEAD_A}|3|1"},
		loaded={18: "partial"},
	)
	assert (result["outcomes"]["18"]["prior"], result["outcomes"]["18"]["resolution"]) == ("unknown", "pending")
	assert result["summary"]["unresolved_same_head"] == "0"


def test_sweep_outcome_untrusted_or_other_head_markers_do_not_count():
	prs = [_pr(14, "ai/issue-14", HEAD_A), _pr(15, "ai/issue-15", HEAD_B), _pr(16, "ai/issue-16", HEAD_C)]
	pr_json = {14: _dirty("ai/issue-14", HEAD_A), 15: _dirty("ai/issue-15", HEAD_B), 16: _dirty("ai/issue-16", HEAD_C)}
	result = _run_sweep(
		prs,
		pr_json,
		dispatch_rc={14: 0, 15: 0, 16: 0},
		attempts_state={
			14: f"60|intruder|{HEAD_A}|9|0",
			15: f"61|intruder|{HEAD_B}|9|0",
			16: f"62|bot|{HEAD_A}|5|1",
		},
		loaded={14: "1", 15: "partial", 16: "1"},
	)
	outcomes = result["outcomes"]
	# Whole thread seen, no trusted marker: no earlier attempts.
	assert (outcomes["14"]["prior"], outcomes["14"]["resolution"]) == ("0", "pending")
	# Truncated window, no trusted marker: the count may have scrolled out.
	assert (outcomes["15"]["prior"], outcomes["15"]["resolution"]) == ("unknown", "pending")
	# Trusted marker for another head: the count restarts on this head.
	assert (outcomes["16"]["prior"], outcomes["16"]["resolution"]) == ("0", "pending")
	assert result["summary"]["unresolved_same_head"] == "0"


def test_sweep_outcome_state_or_login_missing_is_unknown():
	prs = [_pr(17, "ai/issue-17", HEAD_A)]
	pr_json = {17: _dirty("ai/issue-17", HEAD_A)}
	marker = {17: f"55|bot|{HEAD_A}|4|0"}
	for kwargs in (
		{"attempts_state": marker},  # state never loaded for this PR
		{"attempts_state": marker, "loaded": {17: "1"}, "login": None},  # login not resolved yet
		{"attempts_state": marker, "loaded": {17: "1"}, "login": ""},
		{"declare_maps": False, "login": None},  # maps absent, as in older sliced harnesses
	):
		result = _run_sweep(prs, pr_json, dispatch_rc={17: 0}, **kwargs)
		outcome = result["outcomes"]["17"]
		assert (outcome["prior"], outcome["resolution"]) == ("unknown", "pending"), kwargs
		assert result["summary"]["unresolved_same_head"] == "0"


def test_sweep_outcome_prefilters_are_counted_without_rest_reads():
	prs = [
		_pr(20, "claude/fix-20", HEAD_A, draft=True),
		_pr(21, "orchestrator/project-21", HEAD_B),
		_pr(22, "ai/issue-22", HEAD_C, base="orchestrator/project-99"),
		_pr(23, "ai/issue-23", HEAD_D),
		_pr(24, "ai/issue-24", HEAD_A),
		_pr(25, "ai/issue-25", HEAD_B),
	]
	pr_json = {
		20: _dirty("claude/fix-20", HEAD_A),
		24: {"state": "open", "mergeable_state": "clean", "head": {"ref": "ai/issue-24", "sha": HEAD_A}, "labels": []},
		25: _dirty("ai/issue-25", HEAD_B),
	}
	result = _run_sweep(prs, pr_json, clean={23: HEAD_D}, dispatch_rc={25: 2})
	assert result["rest"] == ["repos/o/r/pulls/24", "repos/o/r/pulls/25"]
	assert not any(call.endswith("/pulls/20") for call in result["rest"])
	assert set(result["outcomes"]) == {"25"}
	assert result["summary"] == {
		"listed": "6",
		"prefiltered_draft": "1",
		"skipped_integration": "2",
		"batch_clean": "1",
		"rest_fetched": "2",
		"api_updated": "0",
		"dispatched": "0",
		"dispatch_inflight": "1",
		"dispatch_failed": "0",
		"unresolved_same_head": "0",
		"list_truncated": "false",
		"list_failed": "false",
	}


def test_sweep_outcome_empty_list_summary():
	result = _run_sweep([], {})
	assert result["outcomes"] == {}
	assert result["summary"]["listed"] == "0"
	assert all(value == "0" for key, value in result["summary"].items() if key not in ("listed", "list_truncated", "list_failed"))
	assert result["summary"]["list_truncated"] == "false"


def _run_summary_block(standalone_count: str, list_failed: str | None = None) -> str:
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	end = script.index(LOOP_END)
	block = script[script.index('CONFLICT_SWEEP_LISTED="${STANDALONE_COUNT:-}"', script.index(LOOP_START)):end]
	body = "set -euo pipefail\n" + (f"STANDALONE_COUNT={shlex.quote(standalone_count)}\n" if standalone_count else "")
	if list_failed is not None:
		body += f"_conflict_sweep_list_failed={shlex.quote(list_failed)}\n"
	body += block
	result = subprocess.run(["bash", "-c", body], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	return result.stdout.strip()


def test_sweep_outcome_summary_flags_truncation_at_list_limit():
	for count, listed, truncated in (("3000", "3000", "true"), ("3500", "3500", "true"), ("2999", "2999", "false"), ("", "0", "false"), ("null", "0", "false")):
		match = SUMMARY_RE.match(_run_summary_block(count))
		assert match, count
		assert (match["listed"], match["list_truncated"]) == (listed, truncated), count
		assert match["list_failed"] == "false", count


def test_sweep_outcome_summary_reports_failed_list_read():
	match = SUMMARY_RE.match(_run_summary_block("0", list_failed="true"))
	assert match
	assert (match["listed"], match["list_failed"]) == ("0", "true")
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	fetch = script[script.index('_conflict_sweep_list_failed="false"'):script.index('STANDALONE_COUNT="$(echo "${STANDALONE_PRS}" | jq')]
	assert 'if ! STANDALONE_PRS="$(gh_retry gh pr list' in fetch
	assert 'STANDALONE_PRS="[]"' in fetch and '_conflict_sweep_list_failed="true"' in fetch


def test_sweep_outcome_static_contract():
	section = _sweep_section()
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	# Legacy counter and its line are unchanged.
	assert 'echo "Standalone conflict sweep complete. Fixed: ${CONFLICT_SWEEP_FIXED}."' in script
	assert section.count("CONFLICT_SWEEP_FIXED=$(( CONFLICT_SWEEP_FIXED + 1 ))") == 2
	# The summary's list_limit mirrors the gh pr list limit.
	assert "--limit 3000" in section and "list_limit=3000" in section
	# Outcome values are sanitised: only enums, integers and a 40-hex head.
	for line in section.splitlines():
		if 'echo "STANDALONE_CONFLICT_DISPATCH_OUTCOME' in line or 'echo "STANDALONE_CONFLICT_SWEEP_SUMMARY' in line:
			assert "S_HEAD_REF" not in line and "S_HEAD}" not in line and "S_HEAD_SHA" not in line, line
	for value in re.findall(r'_conflict_sweep_update_branch="([^"]*)"', section):
		assert re.fullmatch(r"ok|failed|skipped_no_sha", value), value
	for value in re.findall(r'_conflict_sweep_dispatch="([^"]*)"', section):
		assert re.fullmatch(r"ok|inflight|failed|not_attempted", value), value
	for value in re.findall(r'_conflict_sweep_resolution="([^"]*)"', section):
		assert re.fullmatch(r"pending|unresolved_same_head|branch_updated", value), value
	# Outcome telemetry never resolves the login itself (that may call the API).
	loop = script[script.index(LOOP_START):script.index(LOOP_END)]
	code = [line for line in loop.splitlines() if not line.lstrip().startswith("#")]
	assert not any("unblock_trusted_login" in line for line in code)
