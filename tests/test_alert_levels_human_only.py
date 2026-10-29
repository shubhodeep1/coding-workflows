#!/usr/bin/env python3
"""Telegram alerts page only when a person has to act.

Level convention (scripts/tg_helpers.sh): DEBUG means the pipeline is
already handling the event; WARNING and above mean a human must act. The
default ALERT_MSG_LEVEL threshold is WARNING, so DEBUG notices stay out of
Telegram unless an operator sets vars.ALERT_MSG_LEVEL=DEBUG.

These tests pin the default threshold, the absence of invalid level
strings, and the helpers that turn repeating or auto-handled alerts into a
single page when the pipeline is really stuck.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
TG_HELPERS = REPO_ROOT / "scripts" / "tg_helpers.sh"
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
OPENCODE_HELPERS = REPO_ROOT / "scripts" / "opencode_helpers.sh"
ACTIVATION_SCRIPT = REPO_ROOT / "scripts" / "activation_verify.sh"
WORKFLOW_DIRS = (REPO_ROOT / ".github" / "workflows", REPO_ROOT / "workflow-templates")

HEAD_A = "a" * 40
HEAD_B = "b" * 40

# Bash prologue shared by the function-level harnesses: records every
# Telegram send and every gh call instead of reaching the network.
HARNESS_PROLOGUE = (
	"set -euo pipefail\n"
	"GITHUB_REPOSITORY=o/r\n"
	'tg_send_msg() { [ "${TG_SEND_FAIL:-false}" != true ] || return 0; printf \'%s\\t%s\\n\' "${2:-CRITICAL}" "$1" >> "${TG_LOG}"; printf 123; }\n'
	'tg_notify() { tg_send_msg "$1" "${2:-CRITICAL}"; }\n'
	'gh_retry() { "$@"; }\n'
	# One log line per gh call: newlines inside a comment body become \\n.
	'gh() { local gh_call="$*"; printf \'%s\\n\' "${gh_call//$\'\\n\'/\\\\n}" >> "${GH_LOG}"; if [[ "${gh_call}" == *"alerted=1 -->"* ]] && [ "${GH_CONFIRM_FAIL:-false}" = true ]; then return 1; fi; printf \'{"id":55}\\n\'; }\n'
	'_gh_url() { printf \'https://github.com/o/r/%s\' "$1"; }\n'
)


def _require_jq() -> None:
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")


def _extract_bash_function(script: str, signature: str) -> str:
	"""Source of one top-level bash function, from ``signature`` through the
	first line that is exactly ``}``."""
	start = script.index(signature)
	end = script.index("\n}\n", start) + 3
	return script[start:end]


def _run_harness(body: str) -> tuple[list[tuple[str, str]], list[str], str]:
	"""Run ``body`` after the prologue; return (telegram sends, gh calls, stdout)."""
	with tempfile.TemporaryDirectory() as tmp:
		tg_log = Path(tmp) / "tg.log"
		gh_log = Path(tmp) / "gh.log"
		harness = Path(tmp) / "harness.sh"
		harness.write_text(
			f"TG_LOG={str(tg_log)!r}\nGH_LOG={str(gh_log)!r}\n" + HARNESS_PROLOGUE + body,
			encoding="utf-8",
		)
		result = subprocess.run(["bash", str(harness)], capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
		sends = []
		if tg_log.exists():
			for line in tg_log.read_text(encoding="utf-8").splitlines():
				if "\t" in line:
					level, message = line.split("\t", 1)
					sends.append((level, message))
		calls = gh_log.read_text(encoding="utf-8").splitlines() if gh_log.exists() else []
		return sends, calls, result.stdout


# ---------------------------------------------------------------------------
# Default threshold and level hygiene
# ---------------------------------------------------------------------------


def test_tg_helpers_default_threshold_is_warning():
	"""With ALERT_MSG_LEVEL unset, DEBUG is filtered and WARNING is sent."""
	harness = (
		f"source {str(TG_HELPERS)!r}\n"
		"unset ALERT_MSG_LEVEL\n"
		"_tg_should_send DEBUG && echo debug=sent || echo debug=filtered\n"
		"_tg_should_send WARNING && echo warning=sent || echo warning=filtered\n"
		"ALERT_MSG_LEVEL=DEBUG\n"
		"_tg_should_send DEBUG && echo debug_opt_in=sent || echo debug_opt_in=filtered\n"
	)
	result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "debug=filtered" in result.stdout
	assert "warning=sent" in result.stdout
	assert "debug_opt_in=sent" in result.stdout


def test_workflows_default_alert_threshold_to_warning():
	"""No workflow or template falls back to a DEBUG Telegram threshold."""
	offenders = []
	for directory in WORKFLOW_DIRS:
		for path in sorted(directory.glob("*.yml")):
			for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
				if "ALERT_MSG_LEVEL" in line and re.search(r"\|\|\s*'DEBUG'\s*}}", line):
					offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
				if re.search(r"\$\{ALERT_MSG_LEVEL:-DEBUG\}", line):
					offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
	assert not offenders, offenders


def test_no_invalid_alert_level_strings():
	"""Levels outside DEBUG/WARNING/ERROR/CRITICAL/SILENT map to DEBUG but
	render with the CRITICAL siren; none may be passed to a send helper."""
	call = re.compile(r'\b(tg_send_msg|tg_send_tracked|tg_send_phase_tracked|tg_send_phase_failure|tg_notify|tg_notify_issue)\b.*"(WARN|INFO|NOTICE|ERR|CRIT|FATAL)"')
	offenders = []
	paths = list((REPO_ROOT / "scripts").glob("*.sh"))
	for directory in WORKFLOW_DIRS:
		paths.extend(directory.glob("*.yml"))
	for path in sorted(paths):
		for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
			if call.search(line):
				offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
	assert not offenders, offenders


def test_gh_ratelimit_alert_is_debug_and_filtered_by_default():
	text = GH_HELPERS.read_text(encoding="utf-8")
	assert '_body="🔍 DEBUG: GitHub API rate limit hit' in text
	assert "\t\tDEBUG) : ;;\n\t\tWARNING|ERROR|CRITICAL|SILENT) return 0 ;;" in text


def test_opencode_runtime_failures_are_debug_and_setup_defects_error():
	function = _extract_bash_function(OPENCODE_HELPERS.read_text(encoding="utf-8"), "opencode_emit_failure_alert()\n{")
	assert 'local alert_level="DEBUG"' in function
	assert "config_*|binary_missing|version_mismatch|invalid_expected_version|isolation_unavailable|helpers_missing) alert_level=\"ERROR\" ;;" in function
	assert 'tg_send_msg "${payload}" "${alert_level}"' in function


def test_project_completion_alerts_stay_visible():
	"""Operator decision: final merge and project-complete messages keep
	paging at the default threshold, on every completion path."""
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	for message in (
		'MSG="Project #${TRACKING_NUM} completed after validation pass',
		'MSG="✅ Project #${TRACKING_NUM} completed (integration PR',
		'MSG="✅ Project #${TRACKING_NUM} completed successfully."',
		'MSG="Project #${TRACKING_NUM} completed! All waves merged and judge approved."',
	):
		start = script.index(message)
		send = script.index("tg_send_msg", start)
		assert script[send:script.index("\n", send)] == 'tg_send_msg "${MSG}" "CRITICAL" >/dev/null', message
	final_merge = script.index('tg_send_msg "${_final_merge_alert_msg}"')
	assert script[final_merge:script.index("\n", final_merge)].endswith('"CRITICAL" >/dev/null || true')


def test_reported_auto_handled_alerts_are_debug():
	"""The two alerts reported by the operator no longer page."""
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	line = next(l for l in script.splitlines() if "Standalone PR #${S_PR} has merge conflicts. Review workflow dispatched" in l)
	assert line.rstrip().endswith('"DEBUG" >/dev/null 2>&1 || true'), line
	stall = [l for l in script.splitlines() if 'tg_notify "Stall recovery:' in l or 'tg_notify_issue "${issue_num}" "Standalone stall recovery:' in l]
	assert stall
	# Human-action stall notices stay WARNING: the standalone ancestor-chain
	# no-op cap closes the issue with no judge behind it, and the plan
	# auto-approval hold (sent once per hold marker) waits for a trusted
	# files_touched grant or a human /approved.
	human_action = ("ancestor-chain no-op cap", "plan auto-approval withheld")
	paging = [l for l in stall if '"WARNING"' in l and not any(h in l for h in human_action)]
	assert not paging, paging


# ---------------------------------------------------------------------------
# Standalone conflict sweep: one WARNING after N attempts on one head
# ---------------------------------------------------------------------------


def _conflict_harness(*, marker: str, threshold: str = "3", head: str = HEAD_A, loaded: bool | str = True, login: str = "bot", write_fails: bool = False, send_fails: bool = False, confirmation_fails: bool = False) -> tuple:
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	function = _extract_bash_function(script, "_standalone_conflict_count_attempt() {")
	marker_constant = next(l for l in script.splitlines() if l.startswith("STANDALONE_CONFLICT_ATTEMPTS_MARKER="))
	body = (
		marker_constant + "\n"
		f"STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS={threshold}\n"
		f"UNBLOCK_TRUSTED_LOGIN={login!r}\n"
		f"TG_SEND_FAIL={'true' if send_fails else 'false'}\n"
		f"GH_CONFIRM_FAIL={'true' if confirmation_fails else 'false'}\n"
		"unblock_trusted_login() { printf '%s' \"${UNBLOCK_TRUSTED_LOGIN}\"; }\n"
		"declare -A _STANDALONE_CONFLICT_ATTEMPTS_STATE=()\n"
		"declare -A _STANDALONE_CONFLICT_STATE_LOADED=()\n"
		+ (f'_STANDALONE_CONFLICT_STATE_LOADED["7"]="{loaded if isinstance(loaded, str) else "1"}"\n' if loaded else "")
		# ANSI-C quoting so a multi-line candidate list keeps its newlines.
		+ "_STANDALONE_CONFLICT_ATTEMPTS_STATE[\"7\"]=$'" + marker.replace("\n", "\\n") + "'\n"
		+ ("gh() { return 1; }\n" if write_fails else "")
		+ function + "\n"
		f'_standalone_conflict_count_attempt 7 "{head}" 0\n'
	)
	return _run_harness(body)


def test_conflict_first_attempt_creates_marker_without_alert():
	_require_jq()
	sends, calls, out = _conflict_harness(marker="")
	assert sends == []
	assert len(calls) == 1 and calls[0].startswith("api repos/o/r/issues/7/comments -f body=")
	assert f"head={HEAD_A} attempts=1 alerted=0 -->" in calls[0]
	assert "outcome=counted" in out


def test_conflict_threshold_attempt_pages_once_and_patches_marker():
	_require_jq()
	sends, calls, out = _conflict_harness(marker=f"55|bot|{HEAD_A}|2|0")
	assert len(sends) == 1
	level, message = sends[0]
	assert level == "WARNING"
	assert "Standalone PR #7 is still in merge conflict after 3 automatic resolution attempt(s)" in message
	assert len(calls) == 2 and all(call.startswith("api repos/o/r/issues/comments/55 -X PATCH -f body=") for call in calls)
	assert f"head={HEAD_A} attempts=3 alerted=0 -->" in calls[0]
	assert f"head={HEAD_A} attempts=3 alerted=1 -->" in calls[1]
	assert "outcome=alerted" in out


def test_conflict_after_alert_keeps_counting_silently():
	_require_jq()
	sends, calls, _ = _conflict_harness(marker=f"55|bot|{HEAD_A}|3|1")
	assert sends == []
	assert f"head={HEAD_A} attempts=4 alerted=1 -->" in calls[0]


def test_conflict_does_not_page_until_alert_marker_is_saved():
	sends, calls, out = _conflict_harness(marker=f"55|bot|{HEAD_A}|2|0", write_fails=True)
	assert sends == [] and calls == []
	assert "outcome=write_failed" in out


def test_conflict_failed_telegram_delivery_leaves_marker_retryable():
	sends, calls, out = _conflict_harness(marker=f"55|bot|{HEAD_A}|2|0", send_fails=True)
	assert sends == []
	assert len(calls) == 1 and f"head={HEAD_A} attempts=3 alerted=0 -->" in calls[0]
	assert "outcome=send_failed" in out
	sends, calls, out = _conflict_harness(marker=f"55|bot|{HEAD_A}|3|0")
	assert len(sends) == 1 and sends[0][0] == "WARNING"
	assert f"head={HEAD_A} attempts=4 alerted=1 -->" in calls[1]
	assert "outcome=alerted" in out


def test_conflict_confirmation_failure_retries_delivery_next_tick():
	sends, calls, out = _conflict_harness(marker=f"55|bot|{HEAD_A}|2|0", confirmation_fails=True)
	assert len(sends) == 1
	assert len(calls) == 2 and "alerted=0 -->" in calls[0] and "alerted=1 -->" in calls[1]
	assert "outcome=confirmation_failed" in out


def test_conflict_threshold_one_creates_comment_before_confirming_delivery():
	_require_jq()
	sends, calls, out = _conflict_harness(marker="", threshold="1")
	assert len(sends) == 1 and sends[0][0] == "WARNING"
	assert len(calls) == 2 and calls[0].startswith("api repos/o/r/issues/7/comments -f body=")
	assert calls[1].startswith("api repos/o/r/issues/comments/55 -X PATCH -f body=")
	assert "outcome=alerted" in out


def test_conflict_new_head_restarts_count():
	_require_jq()
	sends, calls, _ = _conflict_harness(marker=f"55|bot|{HEAD_A}|5|1", head=HEAD_B)
	assert sends == []
	assert calls[0].startswith("api repos/o/r/issues/comments/55 -X PATCH")
	assert f"head={HEAD_B} attempts=1 alerted=0 -->" in calls[0]


def test_conflict_marker_from_other_author_is_ignored():
	_require_jq()
	sends, calls, _ = _conflict_harness(marker=f"55|someone-else|{HEAD_A}|9|0")
	assert sends == []
	assert calls[0].startswith("api repos/o/r/issues/7/comments -f body=")
	assert f"head={HEAD_A} attempts=1 alerted=0 -->" in calls[0]


def test_conflict_newer_untrusted_marker_does_not_shadow_trusted_one():
	"""A newer marker posted by someone else cannot reset the trusted count."""
	_require_jq()
	marker = f"60|intruder|{HEAD_A}|0|0\n55|bot|{HEAD_A}|2|0"
	sends, calls, out = _conflict_harness(marker=marker)
	assert len(sends) == 1 and sends[0][0] == "WARNING"
	assert len(calls) == 2 and all(call.startswith("api repos/o/r/issues/comments/55 -X PATCH") for call in calls)
	assert f"head={HEAD_A} attempts=3 alerted=0 -->" in calls[0]
	assert f"head={HEAD_A} attempts=3 alerted=1 -->" in calls[1]
	assert "outcome=alerted" in out


def test_conflict_truncated_window_without_trusted_marker_restarts_count():
	"""Older comments exist and the window holds no trusted marker (it may
	have scrolled out): a fresh marker is posted so later ticks keep counting
	and the stuck alert can still fire."""
	_require_jq()
	sends, calls, out = _conflict_harness(marker=f"60|intruder|{HEAD_A}|0|0", loaded="partial")
	assert sends == []
	assert len(calls) == 1 and calls[0].startswith("api repos/o/r/issues/7/comments -f body=")
	assert f"head={HEAD_A} attempts=1 alerted=0 -->" in calls[0]
	assert "outcome=restart reason=marker_outside_window" in out
	assert "outcome=counted" in out
	sends, calls, out = _conflict_harness(marker=f"55|bot|{HEAD_A}|2|0", loaded="partial")
	assert len(sends) == 1 and sends[0][0] == "WARNING"
	assert calls[0].startswith("api repos/o/r/issues/comments/55 -X PATCH")


def test_conflict_alert_disabled_and_fail_open_paths():
	_require_jq()
	sends, calls, out = _conflict_harness(marker=f"55|bot|{HEAD_A}|2|0", threshold="0")
	assert (sends, calls) == ([], [])
	assert "reason=disabled" in out
	sends, calls, out = _conflict_harness(marker="", loaded=False)
	assert (sends, calls) == ([], [])
	assert "reason=state_unavailable" in out
	sends, calls, out = _conflict_harness(marker="", login="")
	assert (sends, calls) == ([], [])
	assert "reason=identity_unavailable" in out


def test_conflict_sweep_reads_marker_from_existing_graphql_batch():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "comments(last: 100) { pageInfo { hasPreviousPage } nodes { body databaseId author { login } } }" in script
	assert '_standalone_conflict_count_attempt "${S_PR}" "${S_HEAD_SHA}" "${_dispatch_rc}"' in script
	assert 'STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS="${STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS:-3}"' in script
	workflow = (REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
	assert "STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS: ${{ vars.STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS || '3' }}" in workflow


def _sweep_alert_level_harness(env_lines: str) -> str:
	"""Apply the standalone sweep alert level, probe the real tg_helpers
	filter for a WARNING, restore, and print what ALERT_MSG_LEVEL is left."""
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	functions = (
		'_STANDALONE_SWEEP_PRIOR_ALERT_MSG_LEVEL_SET="false"\n'
		'_STANDALONE_SWEEP_PRIOR_ALERT_MSG_LEVEL=""\n'
		+ _extract_bash_function(script, "_standalone_sweep_alert_level_apply() {")
		+ _extract_bash_function(script, "_standalone_sweep_alert_level_restore() {")
	)
	body = (
		"set -euo pipefail\n"
		f"source {str(TG_HELPERS)!r}\n"
		+ env_lines
		+ functions
		+ "_standalone_sweep_alert_level_apply\n"
		"_tg_should_send WARNING && echo warning=sent || echo warning=filtered\n"
		"_standalone_sweep_alert_level_restore\n"
		'echo "restored=${ALERT_MSG_LEVEL-unset}"\n'
	)
	result = subprocess.run(["bash", "-c", body], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	return result.stdout


def test_standalone_sweeps_do_not_inherit_smoke_silent_level():
	"""Issue #7177: real standalone PRs page even when every tracking issue
	is a smoke fixture and the poller runs at SILENT."""
	out = _sweep_alert_level_harness("ALERT_MSG_LEVEL=SILENT\nSTANDALONE_SWEEP_ALERT_MSG_LEVEL=WARNING\n")
	assert "warning=sent" in out
	assert "Standalone PR sweeps alert level: WARNING (poller level: SILENT)" in out
	assert "restored=SILENT" in out
	# Levels are case-insensitive, as in tg_helpers.sh.
	out = _sweep_alert_level_harness("ALERT_MSG_LEVEL=SILENT\nSTANDALONE_SWEEP_ALERT_MSG_LEVEL=warning\n")
	assert "warning=sent" in out
	assert "restored=SILENT" in out


def test_standalone_sweep_alert_level_empty_or_invalid_keeps_poller_level():
	out = _sweep_alert_level_harness("ALERT_MSG_LEVEL=SILENT\nSTANDALONE_SWEEP_ALERT_MSG_LEVEL=\n")
	assert "warning=filtered" in out
	assert "restored=SILENT" in out
	out = _sweep_alert_level_harness("ALERT_MSG_LEVEL=SILENT\nSTANDALONE_SWEEP_ALERT_MSG_LEVEL='WARNING; rm -rf /'\n")
	assert "::warning::Ignoring invalid STANDALONE_SWEEP_ALERT_MSG_LEVEL" in out
	assert "warning=filtered" in out
	assert "restored=SILENT" in out
	out = _sweep_alert_level_harness("unset ALERT_MSG_LEVEL\nSTANDALONE_SWEEP_ALERT_MSG_LEVEL=ERROR\n")
	assert "warning=filtered" in out
	assert "restored=unset" in out


def test_conflict_marker_parser_lists_markers_newest_first():
	"""The jq program the sweep runs over its GraphQL batch."""
	_require_jq()
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	start = script.index("[.data.repository[$key].comments.nodes[] | select((.body // \"\") | contains($marker))] | reverse | .[]")
	program = script[start:script.index("' 2>/dev/null", start)]
	response = {"data": {"repository": {"p7": {"comments": {"nodes": [
		{"body": f"old\n<!-- ai:standalone-conflict-attempts:v1 head={HEAD_B} attempts=1 alerted=0 -->", "databaseId": 1, "author": {"login": "bot"}},
		{"body": "unrelated", "databaseId": 2, "author": {"login": "bot"}},
		{"body": f"new\n<!-- ai:standalone-conflict-attempts:v1 head={HEAD_A} attempts=2 alerted=0 -->", "databaseId": 3, "author": {"login": "bot"}},
	]}}, "p8": {"comments": {"nodes": []}}}}}
	for key, expected in (("p7", f"3|bot|{HEAD_A}|2|0\n1|bot|{HEAD_B}|1|0"), ("p8", "")):
		result = subprocess.run(
			["jq", "-r", "--arg", "key", key, "--arg", "marker", "<!-- ai:standalone-conflict-attempts:v1 ", program],
			input=json.dumps(response), capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert result.stdout.strip() == expected


# ---------------------------------------------------------------------------
# No-op force-merge gates: one page per PR head and gate
# ---------------------------------------------------------------------------


def _gate_harness(comments: list[dict]) -> tuple:
	function = _extract_bash_function(POLLER_SCRIPT.read_text(encoding="utf-8"), "_noop_gate_failure_alert() {")
	body = (
		"UNBLOCK_TRUSTED_LOGIN=bot\n"
		"unblock_trusted_login() { printf '%s' \"${UNBLOCK_TRUSTED_LOGIN}\"; }\n"
		f"N_PR=9\nN_HEAD_SHA={HEAD_A}\n"
		f"N_COMMENTS_JSON={json.dumps(comments)!r}\n"
		+ function + "\n"
		'_noop_gate_failure_alert "D_check" "gate D failed for PR #9"\n'
	)
	return _run_harness(body)


def test_noop_gate_failure_pages_once_per_head():
	_require_jq()
	sends, calls, out = _gate_harness([])
	assert sends == [("ERROR", "gate D failed for PR #9")]
	assert len(calls) == 1 and calls[0].startswith("api repos/o/r/issues/9/comments -f body=")
	marker = f"<!-- ai:noop-force-merge-gate-alert:v1 pr=9 head={HEAD_A} gate=D_check -->"
	assert marker in calls[0]
	assert "outcome=alerted" in out
	sends, calls, out = _gate_harness([{"body": "x\n\n" + marker, "user": {"login": "bot"}}])
	assert (sends, calls) == ([], [])
	assert "reason=already_alerted" in out
	sends, calls, _ = _gate_harness([{"body": marker, "user": {"login": "intruder"}}])
	assert sends == [("ERROR", "gate D failed for PR #9")]


# ---------------------------------------------------------------------------
# Merge deferral cap and project judge output failures
# ---------------------------------------------------------------------------


def test_merge_deferral_cap_alert_once_and_level_by_unblock_judge():
	function = _extract_bash_function(POLLER_SCRIPT.read_text(encoding="utf-8"), "_merge_deferral_cap_alert() {")
	for enabled, count, expected in (("true", 5, "DEBUG"), ("false", 5, "WARNING"), ("true", 6, None), ("false", 7, None)):
		sends, _, _ = _run_harness(
			f"MAX_MERGE_DEFERRALS=5\nUNBLOCK_JUDGE_ENABLED={enabled}\n" + function + "\n"
			f'_merge_deferral_cap_alert 12 34 "sibling merge-tree conflicts" {count}\n'
		)
		if expected is None:
			assert sends == []
		else:
			assert len(sends) == 1 and sends[0][0] == expected, sends
			assert "Human review required" in sends[0][1] if expected == "WARNING" else "Handed to the unblock judge." in sends[0][1]


def test_judge_output_failure_pages_only_at_cap():
	_require_jq()
	function = _extract_bash_function(POLLER_SCRIPT.read_text(encoding="utf-8"), "_judge_output_failure_alert() {")
	with tempfile.TemporaryDirectory() as tmp:
		for prior, expected in ((0, "DEBUG"), (1, "DEBUG"), (2, "CRITICAL")):
			state = Path(tmp) / f"state{prior}.json"
			state.write_text(json.dumps({"judge_output_failures": prior}), encoding="utf-8")
			sends, _, _ = _run_harness(
				f"STATE_FILE={str(state)!r}\nJUDGE_OUTPUT_FAILURE_MAX=3\n" + function + "\n"
				'_judge_output_failure_alert "Orchestrator Judge failed for #1."\n'
			)
			assert len(sends) == 1 and sends[0][0] == expected, (prior, sends)
			if expected == "CRITICAL":
				assert "Manual review needed" in sends[0][1]
			else:
				assert f"Retrying next poll ({prior + 1}/3)." in sends[0][1]


def test_release_callback_failure_pages_once_per_streak():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	start = script.index('msg="Comprehensive release callback failed for project #${TRACKING_NUM}."')
	block = script[start:script.index("\n    fi\n", script.index('tg_notify "${msg}" "CRITICAL"', start))]
	assert 'if [ "${dispatch_failure_alerted}" != "true" ]; then' in block
	assert "{dispatch_failure_alerted: true}" in block


# ---------------------------------------------------------------------------
# Activation verdicts
# ---------------------------------------------------------------------------


def test_activation_dormant_pages_only_when_a_person_must_act():
	text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")
	assert (
		'if [ "${verdict}" = "DORMANT" ] && { [ "${operator_gaps}" -gt 0 ] || [ "${activation_fix_lookup_ok}" = "false" ]; }; then\n'
		'\t\ttg_level="WARNING"'
	) in text
	assert 'tg_level="DEBUG"' in text
	assert 'the fix issue could not be opened; nothing will implement them until verification is re-run." "WARNING"' in text


def test_review_blocked_judge_successes_are_debug():
	"""Every finished judge outcome, merge_with_followup included, is DEBUG."""
	workflow = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	for action in ("merge", "fix", "close_and_reissue", "merge_with_followup"):
		start = workflow.index(f"              {action})\n", workflow.index('JUDGE_ALERT_LEVEL="CRITICAL"'))
		arm = workflow[start:workflow.index(";;", start)]
		assert 'JUDGE_ALERT_LEVEL="DEBUG"' in arm, action


def _judge_alert(env: dict[str, str]) -> list[tuple[str, str]]:
	"""Run the "Telegram review-blocked judge decision" step body with the
	given step outputs; return the (level, message) sent."""
	workflow = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	start = workflow.index("      - name: Telegram review-blocked judge decision")
	end = workflow.index("      - name: Check PR state before failure alerts", start)
	step = workflow[start:end]
	body = step[step.index("        run: |\n") + len("        run: |\n"):]
	body = "\n".join(line[10:] for line in body.splitlines())
	body = re.sub(r"\$\{\{[^}]*\}\}", "x", body)
	with tempfile.TemporaryDirectory() as tmp:
		log = Path(tmp) / "tg.log"
		(Path(tmp) / "tg_helpers.sh").write_text(
			'tg_send_tracked() { printf \'%s\\t%s\\n\' "$3" "${2//$\'\\n\'/ }" >> "${TG_LOG}"; }\n', encoding="utf-8"
		)
		run_env = {"PATH": "/usr/bin:/bin", "SUPPORT_SCRIPTS_DIR": tmp, "TG_LOG": str(log), "PR_NUMBER": "7", "MAX_AUTOFIX_ITERATIONS": "5"}
		run_env.update(env)
		result = subprocess.run(["bash", "-c", body], env=run_env, capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
		sends = []
		if log.exists():
			for line in log.read_text(encoding="utf-8").splitlines():
				level, message = line.split("\t", 1)
				sends.append((level, message))
		return sends


def test_review_blocked_judge_merge_alert_follows_merge_status():
	"""judge_action=merge pages unless the merge went through or retries on
	its own; a missing status (older judge script) still pages."""
	for status, expected in (
		("merge_requested", "DEBUG"),
		("already_merged", "DEBUG"),
		("ready_to_merge", "DEBUG"),
		("deferred_base_moved", "DEBUG"),
		("checks_not_ready", "DEBUG"),
		("mergeability_pending", "DEBUG"),
		("merge_conflict", "DEBUG"),
		("checks_failed", "CRITICAL"),
		("merge_failed", "CRITICAL"),
		("manual_merge_required", "CRITICAL"),
		("not_open", "CRITICAL"),
		("", "CRITICAL"),
	):
		sends = _judge_alert({"JUDGE_HANDLED": "true", "JUDGE_ACTION": "merge", "JUDGE_MERGE_STATUS": status})
		assert len(sends) == 1 and sends[0][0] == expected, (status, sends)
		if expected == "CRITICAL":
			assert "merged PR as-is" not in sends[0][1], (status, sends)


def test_review_blocked_judge_reissue_alert_follows_issue_creation():
	sends = _judge_alert({"JUDGE_HANDLED": "true", "JUDGE_ACTION": "close_and_reissue", "JUDGE_REISSUE_CREATED": "true"})
	assert sends[0][0] == "DEBUG" and "created replacement issue" in sends[0][1]
	for created in ("false", ""):
		sends = _judge_alert({"JUDGE_HANDLED": "true", "JUDGE_ACTION": "close_and_reissue", "JUDGE_REISSUE_CREATED": created})
		assert sends[0][0] == "CRITICAL" and "could not create the replacement issue" in sends[0][1]


def test_review_blocked_judge_base_moved_overlap_skip_is_debug():
	sends = _judge_alert({"JUDGE_HANDLED": "false", "JUDGE_SKIP_REASON": "base_moved_overlap"})
	assert sends[0][0] == "DEBUG" and "synchronize run re-validates" in sends[0][1]
	sends = _judge_alert({"JUDGE_HANDLED": "false", "JUDGE_SKIP_REASON": "sync_merge_failed"})
	assert sends[0][0] == "CRITICAL"


def test_review_rb_judge_emits_merge_and_reissue_status():
	script = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	assert script.count('echo "judge_action=merge" >> "$GITHUB_OUTPUT"') == script.count('judge_merge_status=')
	assert 'echo "judge_reissue_created=${RB_REISSUE_CREATED:-false}" >> "$GITHUB_OUTPUT"' in script
