#!/usr/bin/env python3
"""Tracking issues stay under GitHub's 2500-comment cap (Q135, Q138-Q141).

Incident: tele-funtoken-msg-scoring#3928 reached 2500 comments (1791 state
chains, 447 `tg_cleanup` markers, 163 identical "self-healing resolved"
comments). Every later poll failed posting the judge verdict and the repo's
AI workflows were switched off.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
STATE_V2 = REPO_ROOT / "scripts" / "orchestrate_state_v2.py"
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
TG_HELPERS = REPO_ROOT / "scripts" / "tg_helpers.sh"
BOT = "pipeline-bot"
CAP_MESSAGE = "gh: Commenting is disabled on issues with more than 2500 comments (HTTP 403)"


def _chain(first_id: int, state: dict, parts: int = 1, login: str = BOT) -> list[dict]:
	"""V2 state comments for `state`, split into `parts` chunks, ids from first_id."""
	raw = json.dumps(state).encode("utf-8")
	manifest = hashlib.sha256(raw).hexdigest()
	encoded = base64.b64encode(raw).decode("ascii")
	size = -(-len(encoded) // parts)
	return [
		{"id": first_id + index, "user": {"login": login},
			"body": f"<!-- ORCHESTRATOR_STATE_V2 part={index + 1}/{parts} manifest={manifest} -->\n"
				f"{encoded[index * size:(index + 1) * size]}\nORCHESTRATOR_STATE_V2 -->"}
		for index in range(parts)
	]


def _plan(tmp_path: Path, comments: list[dict], *extra: str) -> dict:
	path = tmp_path / "comments.json"
	path.write_text(json.dumps(comments), encoding="utf-8")
	result = subprocess.run(
		["python3", str(STATE_V2), "prune-plan", "--comments-json", str(path), "--login", BOT, *extra],
		capture_output=True, text=True, check=False, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


# --- prune-plan -------------------------------------------------------------


def test_prune_plan_keeps_newest_and_newest_not_failed(tmp_path: Path) -> None:
	comments = (
		_chain(10, {"status": "in_progress", "n": 1}, parts=2)
		+ [{"id": 20, "user": {"login": BOT}, "body": "## Wave 2 Dispatched"}]
		+ _chain(30, {"status": "in_progress", "n": 2}, parts=2)
		+ _chain(40, {"status": "failed", "n": 3}, parts=2)
		+ [{"id": 45, "user": {"login": "mallory"}, "body": _chain(0, {"status": "x"})[0]["body"]}]
		+ _chain(50, {"status": "failed", "n": 4}, parts=2)
		# A newer write still in flight: part 1 of 2 only.
		+ _chain(60, {"status": "failed", "n": 5}, parts=2)[:1]
	)
	plan = _plan(tmp_path, comments, "--keep-recent", "1", "--keep-not-status", "failed")
	# Kept: 50-51 (newest complete), 30-31 (newest not failed), 60 (newer than any complete chain).
	assert plan == {"delete": [10, 11, 40, 41], "chains": 4, "kept": 2}


def test_prune_plan_without_keep_not_status_keeps_only_newest(tmp_path: Path) -> None:
	comments = _chain(10, {"status": "in_progress"}) + _chain(20, {"status": "failed"})
	assert _plan(tmp_path, comments, "--keep-recent", "1")["delete"] == [10]


def test_prune_plan_deletes_nothing_without_a_complete_chain(tmp_path: Path) -> None:
	comments = _chain(10, {"status": "x"}, parts=3)[:2]
	assert _plan(tmp_path, comments)["delete"] == []


def test_prune_plan_ignores_comments_that_only_quote_a_frame(tmp_path: Path) -> None:
	quoted = {"id": 5, "user": {"login": BOT}, "body": "Judge context:\n" + _chain(0, {"status": "x"})[0]["body"]}
	comments = [quoted] + _chain(10, {"status": "a"}) + _chain(20, {"status": "b"})
	assert _plan(tmp_path, comments)["delete"] == [10]


def test_prune_plan_keeps_extract_results(tmp_path: Path) -> None:
	comments = []
	for index, status in enumerate(("in_progress", "in_progress", "failed", "failed")):
		comments += _chain(100 * (index + 1), {"status": status, "n": index}, parts=2)
	plan = _plan(tmp_path, comments, "--keep-recent", "1", "--keep-not-status", "failed")
	kept = [c for c in comments if c["id"] not in set(plan["delete"])]

	def extract(items: list[dict], *flags: str) -> str:
		path = tmp_path / "extract.json"
		path.write_text(json.dumps(items), encoding="utf-8")
		return subprocess.run(["python3", str(STATE_V2), "extract", "--comments-json", str(path), *flags],
			capture_output=True, text=True, check=True).stdout

	assert extract(kept) == extract(comments)
	assert extract(kept, "--skip-status", "failed") == extract(comments, "--skip-status", "failed")


# --- poller harness -----------------------------------------------------------


def _function(text: str, name: str) -> str:
	start = text.index(f"{name}() {{")
	return text[start:text.index("\n}\n", start) + 3]


def _assignment(text: str, name: str) -> str:
	start = text.index(f"{name}='")
	return text[start:text.index("\n'\n", start) + 3]


HARNESS_MOCKS = r'''
set -euo pipefail
unblock_trusted_login() { UNBLOCK_TRUSTED_LOGIN="${MOCK_LOGIN}"; }
_mirror_task_state_files_from_state() { :; }
record_poll_progress_if_advanced() { :; }
# gh_retry_to_file <out> gh api --paginate <endpoint>: the cap-triggered re-read.
gh_retry_to_file() {
	echo "GET refresh" >> "${MOCK_DIR}/calls"
	[ "${MOCK_REFRESH_FAILS:-0}" = "0" ] || return 1
	printf '%s' "${MOCK_REFRESH_COMMENTS:-${COMMENTS}}" > "$1"
}
# gh_retry gh api <endpoint> [args...]: records "METHOD endpoint" and answers.
gh_retry() {
	shift 2
	local endpoint="" method=POST input=""
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--method|-X) method="$2"; shift 2 ;;
			--input) input="$2"; shift 2 ;;
			-f|-F) shift 2 ;;
			-*) shift ;;
			*) [ -n "${endpoint}" ] || endpoint="$1"; shift ;;
		esac
	done
	if [ "${endpoint#*/issues/comments/}" = "${endpoint}" ] && [ "${method}" = "POST" ] && [ -n "${input}" ]; then
		: "${MOCK_POST_FAILS:=0}"
		if [ "$(cat "${MOCK_DIR}/post_failures" 2>/dev/null || echo 0)" -lt "${MOCK_POST_FAILS}" ]; then
			echo $(( $(cat "${MOCK_DIR}/post_failures" 2>/dev/null || echo 0) + 1 )) > "${MOCK_DIR}/post_failures"
			echo "POST ${endpoint} cap" >> "${MOCK_DIR}/calls"
			echo "${MOCK_CAP_MESSAGE}" >&2
			return 1
		fi
	fi
	if [ "${method}" = "DELETE" ] && [[ " ${MOCK_DELETE_FAIL_ONCE:-} " == *" ${endpoint##*/} "* ]] \
		&& ! grep -qxF "${endpoint##*/}" "${MOCK_DIR}/delete_failed" 2>/dev/null; then
		echo "${endpoint##*/}" >> "${MOCK_DIR}/delete_failed"
		echo "DELETE ${endpoint} failed" >> "${MOCK_DIR}/calls"
		return 1
	fi
	echo "${method} ${endpoint}" >> "${MOCK_DIR}/calls"
	if [ -n "${input}" ]; then cp "${input}" "${MOCK_DIR}/last_${method}_body.json"; fi
	local next
	next=$(( $(cat "${MOCK_DIR}/next_id" 2>/dev/null || echo 9000) + 1 ))
	echo "${next}" > "${MOCK_DIR}/next_id"
	[ "${method}" = "DELETE" ] || printf '{"id": %s}\n' "${next}"
}
'''


def _run_poller_functions(tmp_path: Path, script: str, comments: list[dict], **env: str) -> tuple[subprocess.CompletedProcess, Path]:
	text = POLLER.read_text(encoding="utf-8")
	mock_dir = tmp_path / "mock"
	runtime = tmp_path / "runtime"
	mock_dir.mkdir(exist_ok=True)
	runtime.mkdir(exist_ok=True)
	harness = HARNESS_MOCKS + "\n".join([
		_assignment(text, "TRACKING_COMMENT_REPEAT_JQ_DEFS"),
		_function(text, "post_tracking_comment"),
		_function(text, "_tracking_comment_recent_file"),
		_function(text, "prune_state_comment_chains"),
		_function(text, "post_state_comment"),
		_function(text, "_post_state_comment_v2_chunk"),
	]) + "\n" + script
	full_env = dict(os.environ, MOCK_DIR=str(mock_dir), MOCK_LOGIN=BOT, MOCK_CAP_MESSAGE=CAP_MESSAGE,
		RUNTIME_DIR=str(runtime), TMPDIR=str(tmp_path), TRACKING_NUM="40", GITHUB_REPOSITORY="o/r",
		COMMENTS_FETCH_OK="true", COMMENTS_FOR_TRACKING_NUM="40", COMMENTS=json.dumps(comments),
		PYTHONDONTWRITEBYTECODE="1")
	full_env.update(env)
	result = subprocess.run(["bash", "-c", harness], cwd=REPO_ROOT, env=full_env, capture_output=True, text=True, check=False)
	return result, mock_dir


def _calls(mock_dir: Path) -> list[str]:
	path = mock_dir / "calls"
	return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _body(mock_dir: Path, method: str) -> str:
	return json.loads((mock_dir / f"last_{method}_body.json").read_text(encoding="utf-8"))["body"]


# --- repeated tracking comments (Q141) ---------------------------------------


def test_repeated_comment_updates_a_count_instead_of_posting(tmp_path: Path) -> None:
	body = "## ✅ Integration self-healing resolved\n\nIntegration conflicts are cleared."
	script = "\n".join([f"post_tracking_comment {shlex.quote(body)}"] * 3)
	result, mock = _run_poller_functions(tmp_path, script, [])
	assert result.returncode == 0, result.stderr
	assert _calls(mock) == ["POST repos/o/r/issues/40/comments", "PATCH repos/o/r/issues/comments/9001",
		"PATCH repos/o/r/issues/comments/9001"]
	patched = _body(mock, "PATCH")
	assert patched.startswith(body + "\n\n_Repeated 3 times, last ")
	assert patched.endswith(" UTC._")
	assert "TRACKING_COMMENT_REPEAT tracking_issue=40 comment=9001 count=3 outcome=updated" in result.stdout


def test_repeat_continues_the_count_of_an_earlier_run(tmp_path: Path) -> None:
	body = "## Wave 2 Dispatched"
	existing = [{"id": 77, "user": {"login": BOT}, "body": body + "\n\n_Repeated 4 times, last 2026-10-14 05:10 UTC._"}]
	result, mock = _run_poller_functions(tmp_path, f"post_tracking_comment {shlex.quote(body)}", existing)
	assert result.returncode == 0, result.stderr
	assert _calls(mock) == ["PATCH repos/o/r/issues/comments/77"]
	assert _body(mock, "PATCH").startswith(body + "\n\n_Repeated 5 times, last ")


@pytest.mark.parametrize("body,comments,env", [
	# A command must post: an edit would not trigger its workflow.
	("/judge_resume", [{"id": 1, "user": {"login": BOT}, "body": "/judge_resume"}], {}),
	# Someone else's identical comment is not the pipeline's.
	("same", [{"id": 1, "user": {"login": "mallory"}, "body": "same"}], {}),
	# The identical comment is older than the newest 20 pipeline comments.
	("same", [{"id": 1, "user": {"login": BOT}, "body": "same"}]
		+ [{"id": 10 + n, "user": {"login": BOT}, "body": f"other {n}"} for n in range(20)], {}),
	# COMMENTS belongs to another tracking issue.
	("same", [{"id": 1, "user": {"login": BOT}, "body": "same"}], {"COMMENTS_FOR_TRACKING_NUM": "41"}),
])
def test_non_repeats_post_a_new_comment(tmp_path: Path, body: str, comments: list[dict], env: dict) -> None:
	result, mock = _run_poller_functions(tmp_path, f"post_tracking_comment {shlex.quote(body)}", comments, **env)
	assert result.returncode == 0, result.stderr
	assert _calls(mock) == ["POST repos/o/r/issues/40/comments"]


def test_state_and_marker_comments_do_not_push_a_repeat_out_of_the_window(tmp_path: Path) -> None:
	comments = [{"id": 1, "user": {"login": BOT}, "body": "same"}]
	comments += [{"id": 10 + n, "user": {"login": BOT}, "body": f"<!-- tg_cleanup:{n} -->"} for n in range(25)]
	comments += _chain(100, {"status": "x"}) * 1
	result, mock = _run_poller_functions(tmp_path, 'post_tracking_comment "same"', comments)
	assert result.returncode == 0, result.stderr
	assert _calls(mock) == ["PATCH repos/o/r/issues/comments/1"]


# --- state chain pruning (Q140) ----------------------------------------------


def _three_old_chains() -> list[dict]:
	return (_chain(10, {"status": "in_progress"}, parts=2) + _chain(20, {"status": "in_progress"}, parts=2)
		+ _chain(30, {"status": "in_progress"}, parts=2))


def test_prune_deletes_superseded_chains_within_the_budget(tmp_path: Path) -> None:
	script = "prune_state_comment_chains\nprune_state_comment_chains\n"
	result, mock = _run_poller_functions(tmp_path, script, _three_old_chains(), ORCH_STATE_PRUNE_MAX_DELETES="3")
	assert result.returncode == 0, result.stderr
	# 30-31 is the newest chain; 10, 11, 20, 21 are superseded. The budget is 3
	# per tracking issue per run, so the second call deletes nothing more.
	assert _calls(mock) == [f"DELETE repos/o/r/issues/comments/{n}" for n in (10, 11, 20)]
	assert "STATE_PRUNE tracking_issue=40 deleted=3 failed=0 remaining=1 outcome=budget_exhausted" in result.stdout


@pytest.mark.parametrize("env", [
	{"ORCH_STATE_PRUNE_ENABLED": "false"},
	{"COMMENTS_FETCH_OK": "false"},
	{"COMMENTS_FOR_TRACKING_NUM": "41"},
	{"MOCK_LOGIN": ""},
])
def test_prune_does_nothing_when_off_or_unsure(tmp_path: Path, env: dict) -> None:
	result, mock = _run_poller_functions(tmp_path, "prune_state_comment_chains", _three_old_chains(), **env)
	assert result.returncode == 0, result.stderr
	assert _calls(mock) == []


def test_state_write_prunes_after_posting(tmp_path: Path) -> None:
	(tmp_path / "state.json").write_text(json.dumps({"status": "in_progress"}), encoding="utf-8")
	result, mock = _run_poller_functions(tmp_path, "post_state_comment", _three_old_chains(), STATE_FILE=str(tmp_path / "state.json"))
	assert result.returncode == 0, result.stderr
	assert _calls(mock) == ["POST repos/o/r/issues/40/comments"] + [f"DELETE repos/o/r/issues/comments/{n}" for n in (10, 11, 20, 21)]


def test_state_write_at_the_comment_cap_prunes_and_retries_once(tmp_path: Path) -> None:
	(tmp_path / "state.json").write_text(json.dumps({"status": "in_progress"}), encoding="utf-8")
	result, mock = _run_poller_functions(tmp_path, "post_state_comment", _three_old_chains(),
		STATE_FILE=str(tmp_path / "state.json"), MOCK_POST_FAILS="1")
	assert result.returncode == 0, result.stderr
	calls = _calls(mock)
	assert calls[0] == "POST repos/o/r/issues/40/comments cap"
	assert calls[1] == "GET refresh"
	assert calls[2:6] == [f"DELETE repos/o/r/issues/comments/{n}" for n in (10, 11, 20, 21)]
	assert calls[6] == "POST repos/o/r/issues/40/comments"
	assert "STATE_PRUNE tracking_issue=40 trigger=comment_cap" in result.stdout


def test_cap_prune_rereads_comments_written_after_the_plan(tmp_path: Path) -> None:
	# The run's COMMENTS has one chain, so the first prune caches an empty plan.
	# Chains written later in the run only show up in the re-read at the cap.
	start = _chain(10, {"status": "in_progress"})
	later = start + _chain(20, {"status": "in_progress"}) + _chain(30, {"status": "in_progress"})
	script = "prune_state_comment_chains\nprune_state_comment_chains refresh\n"
	result, mock = _run_poller_functions(tmp_path, script, start, MOCK_REFRESH_COMMENTS=json.dumps(later))
	assert result.returncode == 0, result.stderr
	assert _calls(mock) == ["GET refresh", "DELETE repos/o/r/issues/comments/10", "DELETE repos/o/r/issues/comments/20"]


def test_cap_prune_falls_back_to_run_comments_when_the_reread_fails(tmp_path: Path) -> None:
	result, mock = _run_poller_functions(tmp_path, "prune_state_comment_chains refresh", _three_old_chains(),
		MOCK_REFRESH_FAILS="1")
	assert result.returncode == 0, result.stderr
	assert _calls(mock) == ["GET refresh"] + [f"DELETE repos/o/r/issues/comments/{n}" for n in (10, 11, 20, 21)]


def test_cap_prune_does_not_retry_ids_deleted_earlier_in_the_run(tmp_path: Path) -> None:
	# An earlier prune deleted 10, 11, 20, 21; the cap re-read then fails and the
	# fallback COMMENTS snapshot still lists them, so they must not be retried.
	script = "prune_state_comment_chains\nprune_state_comment_chains refresh\n"
	result, mock = _run_poller_functions(tmp_path, script, _three_old_chains(), MOCK_REFRESH_FAILS="1")
	assert result.returncode == 0, result.stderr
	assert _calls(mock) == [f"DELETE repos/o/r/issues/comments/{n}" for n in (10, 11, 20, 21)] + ["GET refresh"]


def test_cap_prune_retries_ids_whose_delete_failed_earlier_in_the_run(tmp_path: Path) -> None:
	# The DELETE of 11 failed in the earlier prune, so the comment still exists;
	# the cap-time refresh must try it again while skipping the deleted ones.
	script = "prune_state_comment_chains\nprune_state_comment_chains refresh\n"
	result, mock = _run_poller_functions(tmp_path, script, _three_old_chains(), MOCK_REFRESH_FAILS="1",
		MOCK_DELETE_FAIL_ONCE="11")
	assert result.returncode == 0, result.stderr
	assert _calls(mock) == ["DELETE repos/o/r/issues/comments/10", "DELETE repos/o/r/issues/comments/11 failed",
		"DELETE repos/o/r/issues/comments/20", "DELETE repos/o/r/issues/comments/21", "GET refresh",
		"DELETE repos/o/r/issues/comments/11"]


def test_direct_tracking_posts_do_not_end_the_run() -> None:
	# Judge verdict, judge override and the two "Project Failed" posts (Q139).
	text = POLLER.read_text(encoding="utf-8")
	warning = '|| echo "::warning::Could not post a tracking comment on issue #${TRACKING_NUM}; continuing."'
	assert text.count(warning) >= 4
	for anchor in ('-f body="${JUDGE_COMMENT}" >/dev/null \\', "Judge verdict overridden:", "Manual intervention required.\" >/dev/null \\",
		"**Assessment:** ${JUDGE_ASSESSMENT}\" >/dev/null \\"):
		start = text.index(anchor)
		assert warning in text[start:start + 600], anchor


def test_state_write_stops_after_one_retry_at_the_cap(tmp_path: Path) -> None:
	(tmp_path / "state.json").write_text(json.dumps({"status": "in_progress"}), encoding="utf-8")
	result, mock = _run_poller_functions(tmp_path, "rc=0; post_state_comment || rc=$?; echo RC=$rc", _three_old_chains(),
		STATE_FILE=str(tmp_path / "state.json"), MOCK_POST_FAILS="5")
	assert "RC=1" in result.stdout
	assert [c for c in _calls(mock) if c.startswith("POST")] == ["POST repos/o/r/issues/40/comments cap"] * 2


# --- gh_retry and the Telegram markers (Q139, Q138) -------------------------


def test_comment_cap_is_a_permanent_failure() -> None:
	script = f'source {GH_HELPERS}; _is_gh_permanent_failure {json.dumps(CAP_MESSAGE)} && echo permanent'
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
	assert result.stdout.strip() == "permanent", result.stderr


def _run_tg(tmp_path: Path, script: str, comment_count: int, pages: dict[int, list[dict]]) -> tuple[subprocess.CompletedProcess, list[str]]:
	calls = tmp_path / "curl_calls"
	(tmp_path / "pages.json").write_text(json.dumps({str(k): v for k, v in pages.items()}), encoding="utf-8")
	harness = f'''
set -euo pipefail
cd {tmp_path}
source {TG_HELPERS}
curl_gh_api() {{
	local url="${{@: -1}}"
	echo "GET $url" >> "{calls}"
	case "$url" in
		*/comments\\?*) jq -c --arg p "${{url##*page=}}" '.[$p] // []' "{tmp_path}/pages.json" ;;
		*) printf '{{"comments": {comment_count}}}' ;;
	esac
}}
curl() {{
	local method="" url="" data=""
	while [ "$#" -gt 0 ]; do
		case "$1" in -X) method="$2"; shift 2 ;; -d) data="$2"; shift 2 ;; -H) shift 2 ;; -s) shift ;; *) url="$1"; shift ;; esac
	done
	echo "$method $url $(printf '%s' "$data" | jq -r .body)" >> "{calls}"
}}
{script}
'''
	env = dict(os.environ, GH_TOKEN="t", GITHUB_REPOSITORY="o/r")
	result = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, check=False)
	return result, calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def test_tg_marker_is_found_on_the_newest_page(tmp_path: Path) -> None:
	# The oldest page has no marker; the newest page (page 3 of 250 comments) has two.
	pages = {1: [{"id": 1, "body": "first"}], 3: [
		{"id": 201, "body": "<!-- tg_cleanup:5 -->"}, {"id": 202, "body": "text"}, {"id": 203, "body": "<!-- tg_cleanup:6,7 -->"},
	]}
	result, calls = _run_tg(tmp_path, "tg_store_msg_id 42 8", 250, pages)
	assert result.returncode == 0, result.stderr
	assert calls == [
		"GET https://api.github.com/repos/o/r/issues/42",
		"GET https://api.github.com/repos/o/r/issues/42/comments?per_page=100&page=3",
		"PATCH https://api.github.com/repos/o/r/issues/comments/203 <!-- tg_cleanup:6,7,8 -->",
	]


def test_tg_phase_marker_is_found_on_the_newest_page(tmp_path: Path) -> None:
	pages = {2: [{"id": 150, "body": "<!-- tg_phase:plan:3 -->"}]}
	result, calls = _run_tg(tmp_path, "tg_store_phase_msg_id 42 plan 4", 150, pages)
	assert result.returncode == 0, result.stderr
	assert calls[-1] == "PATCH https://api.github.com/repos/o/r/issues/comments/150 <!-- tg_phase:plan:3,4 -->"


def test_tg_marker_is_created_when_the_newest_page_has_none(tmp_path: Path) -> None:
	result, calls = _run_tg(tmp_path, "tg_store_msg_id 42 8", 0, {})
	assert result.returncode == 0, result.stderr
	assert calls[-1] == "POST https://api.github.com/repos/o/r/issues/42/comments <!-- tg_cleanup:8 -->"
