"""Merge-lease Phase 4: inline update-and-merge (issue #10198).

docs/plans/merge-lease-short-critical-section-plan.md, "Phase 4". Covers:
  * `_pr_base_sync_marker_post` keeps the marker byte-identical without the
    switch or the caller flag, and appends ` inline_run=<run id>` with both;
    the marker readers still accept it;
  * `_pr_base_sync_inline_merge`: each failing step returns 1 and dispatches
    the review exactly once; the success path returns 0 with the update head;
  * `review_head_gate.sh freshness-sync-inline-run`;
  * the gate's `gate_lease_inline_owner_active` (live owner -> skip; completed
    run, failed read, workflow_dispatch or switch off -> today's fast path);
  * the call sites pass the caller flag only on the listed calls.
All `gh` calls are stubbed; nothing touches the network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
LIB = SCRIPTS / "pr_checks_lib.sh"
HEAD_GATE = SCRIPTS / "review_head_gate.sh"
EVALUATE_GATE = SCRIPTS / "review_autofix_step_evaluate_gate.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
ENABLE_AUTO_MERGE = SCRIPTS / "review_enable_auto_merge.sh"
RB_JUDGE = SCRIPTS / "review_rb_judge.sh"

APPROVED = "a" * 40
BASE_TIP = "b" * 40
UPDATE = "c" * 40
RUN_ID = "4593949256"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq binary not available")


def _base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"HOME": str(tmp_path),
		"PYTHONDONTWRITEBYTECODE": "1",
		"RUNNER_TEMP": str(tmp_path),
		"CALL_LOG": str(tmp_path / "calls.log"),
		"GITHUB_RUN_ID": RUN_ID,
	}
	env.update(extra)
	return env


def _calls(tmp_path: Path) -> list[str]:
	log = tmp_path / "calls.log"
	return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


# --- the marker ---------------------------------------------------------------

LEAD = "LEAD SENTENCE."


def _golden_marker(field: str = "") -> str:
	return (
		"**Branch updated from `main` before merging** (update 1; warning threshold 2).\n\n"
		f"{LEAD} This head was already approved for merge: the next review run for update commit `{UPDATE}` only waits for CI "
		"and then merges, without running the reviewers again. Any other head is reviewed normally. If the base changes these "
		"files again before the merge, the branch is updated again and the merge waits for CI on the new combined tree.\n\n"
		f"<!-- ai:merge-base-sync:v1 pr=7 from={APPROVED} sync=1 base={BASE_TIP} merge={UPDATE}{field} -->"
	)


def _post_marker(tmp_path: Path, **env: str) -> tuple[str, str]:
	body_file = tmp_path / "body.txt"
	script = f"""
set -uo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
  local a
  for a in "$@"; do
    case "$a" in body=*) printf '%s' "${{a#body=}}" > {str(body_file)!r} ;; esac
  done
  return 0
}}
source {str(LIB)!r}
PR_BASE_FRESHNESS_BASE_REF=main PR_BASE_SYNC_MARKER_LEAD={LEAD!r} \\
  _pr_base_sync_marker_post 7 {APPROVED} 1 2 {BASE_TIP} {UPDATE}
echo "marked=${{PR_BASE_SYNC_INLINE_MARKED}}"
"""
	res = subprocess.run(["bash", "-c", script], env=_base_env(tmp_path, PR_CHECKS_REPOSITORY="o/r", **env),
		capture_output=True, text=True, timeout=60)
	assert res.returncode == 0, res.stderr
	return body_file.read_text(encoding="utf-8"), res.stdout


def test_marker_without_caller_flag_is_byte_identical(tmp_path: Path) -> None:
	body, out = _post_marker(tmp_path)
	assert body == _golden_marker()
	assert "marked=false" in out


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "off"])
def test_marker_with_switch_off_is_byte_identical(tmp_path: Path, value: str) -> None:
	body, out = _post_marker(tmp_path, PR_BASE_SYNC_INLINE_CALLER="true", MERGE_LEASE_INLINE_SYNC_ENABLED=value)
	assert body == _golden_marker()
	assert "marked=false" in out


def test_marker_with_bad_run_id_is_byte_identical(tmp_path: Path) -> None:
	body, _out = _post_marker(tmp_path, PR_BASE_SYNC_INLINE_CALLER="true", GITHUB_RUN_ID="12; rm -rf /")
	assert body == _golden_marker()


def test_marker_with_caller_flag_names_the_run(tmp_path: Path) -> None:
	body, out = _post_marker(tmp_path, PR_BASE_SYNC_INLINE_CALLER="true")
	assert body == _golden_marker(f" inline_run={RUN_ID}")
	assert body.endswith(f"merge={UPDATE} inline_run={RUN_ID} -->")
	assert "marked=true" in out


def test_sync_count_accepts_the_inline_run_field(tmp_path: Path) -> None:
	comments = [[
		{"user": {"login": "pipeline-bot"}, "body": _golden_marker(f" inline_run={RUN_ID}")},
		{"user": {"login": "pipeline-bot"}, "body": _golden_marker()},
		{"user": {"login": "someone-else"}, "body": _golden_marker(f" inline_run={RUN_ID}")},
	]]
	script = f"""
set -uo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
  case "$*" in
    user) printf '%s' '{{"login":"pipeline-bot"}}' ;;
    *comments*) printf '%s' "${{COMMENTS_JSON}}" ;;
    *) return 1 ;;
  esac
}}
source {str(LIB)!r}
_pr_base_sync_count 7 && echo "count=${{PR_BASE_SYNC_COUNT}}"
"""
	res = subprocess.run(["bash", "-c", script], env=_base_env(tmp_path, PR_CHECKS_REPOSITORY="o/r", COMMENTS_JSON=json.dumps(comments)),
		capture_output=True, text=True, timeout=60)
	assert "count=2" in res.stdout, res.stdout + res.stderr


# --- review_head_gate.sh ---------------------------------------------------------

FRESHNESS_FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as out:
    out.write("gh " + " ".join(args) + "\\n")
path = args[1] if len(args) > 1 else ""
if path.startswith("repos/o/r/commits/"):
    print(json.dumps({"parents": [os.environ["FAKE_FROM"], os.environ["FAKE_BASE"]], "verified": True, "committer": "web-flow"}))
    sys.exit(0)
if path.startswith("repos/o/r/compare/"):
    print("ahead")
    sys.exit(0)
if path.startswith("repos/o/r/actions/runs/"):
    status = os.environ.get("FAKE_RUN_STATUS", "")
    if status == "FAIL":
        sys.exit(1)
    print(status)
    sys.exit(0)
sys.exit(1)
'''


def _fake_gh_bin(tmp_path: Path) -> Path:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FRESHNESS_FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	return bin_dir


def _comments_file(tmp_path: Path, comments: list[dict]) -> Path:
	path = tmp_path / "comments.json"
	path.write_text(json.dumps(comments), encoding="utf-8")
	return path


def _gate_marker(field: str = "", author: str = "pipeline-bot", merge: str = UPDATE) -> dict:
	body = _golden_marker(field).replace(f"merge={UPDATE}", f"merge={merge}")
	return {"author_login": author, "body": body}


def test_freshness_sync_accepts_a_marker_with_inline_run(tmp_path: Path) -> None:
	bin_dir = _fake_gh_bin(tmp_path)
	comments = _comments_file(tmp_path, [_gate_marker(f" inline_run={RUN_ID}")])
	env = {**os.environ, **_base_env(tmp_path), "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_RETRY_MAX_ATTEMPTS": "1",
		"FAKE_FROM": APPROVED, "FAKE_BASE": BASE_TIP}
	res = subprocess.run(["bash", str(HEAD_GATE), "freshness-sync", "o/r", "7", UPDATE, "main", "pipeline-bot", str(comments)],
		env=env, capture_output=True, text=True, check=False)
	assert res.returncode == 0, res.stdout + res.stderr
	assert "outcome=skip reason=freshness_sync" in res.stdout


def _inline_run(tmp_path: Path, comments: list[dict], head: str = UPDATE) -> subprocess.CompletedProcess[str]:
	path = _comments_file(tmp_path, comments)
	return subprocess.run(["bash", str(HEAD_GATE), "freshness-sync-inline-run", "7", head, "pipeline-bot", str(path)],
		env=_base_env(tmp_path), capture_output=True, text=True, check=False)


def test_inline_run_subcommand_prints_the_trusted_run(tmp_path: Path) -> None:
	res = _inline_run(tmp_path, [_gate_marker(f" inline_run={RUN_ID}")])
	assert res.returncode == 0 and res.stdout.strip() == RUN_ID
	assert not (tmp_path / "calls.log").exists()


@pytest.mark.parametrize("comments", [
	[_gate_marker()],
	[_gate_marker(f" inline_run={RUN_ID}", author="intruder")],
	[_gate_marker(f" inline_run={RUN_ID}", merge="9" * 40)],
	[_gate_marker(" inline_run=0")],
	[_gate_marker(f" inline_run={RUN_ID}"), _gate_marker()],
	[],
])
def test_inline_run_subcommand_prints_nothing_otherwise(tmp_path: Path, comments: list[dict]) -> None:
	res = _inline_run(tmp_path, comments)
	assert res.returncode == 0 and res.stdout.strip() == ""


def test_inline_run_subcommand_rejects_invalid_input(tmp_path: Path) -> None:
	res = _inline_run(tmp_path, [_gate_marker(f" inline_run={RUN_ID}")], head="short")
	assert res.returncode == 2


# --- _pr_base_sync_inline_merge --------------------------------------------------

STUB_HEAD_GATE = '''#!/usr/bin/env bash
review_head_gate_post_status() {
	echo "POST_STATUS repo=$1 sha=$2 state=$3" >> "${CALL_LOG}"
	local n outcome=posted
	n="$(grep -c '^POST_STATUS ' "${CALL_LOG}")"
	[ "${n}" != "${POST_FAIL_ON:-0}" ] || outcome=failed
	echo "REVIEW_HEAD_GATE mode=status pr=${PR_NUMBER:-?} head=$2 state=$3 outcome=${outcome}"
	return 0
}
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	echo "FRESHNESS_SYNC $*" >> "${CALL_LOG}"
	[ -z "${FS_SAVE_COMMENTS:-}" ] || cp "$7" "${FS_SAVE_COMMENTS}"
	exit "${FS_RC:-0}"
fi
'''

STUB_PASS = '''#!/usr/bin/env bash
echo "SECURITY_PASS head=${PR_HEAD_SHA} checkout_head=$(git -C "${DET_SKIP_SECURITY_PASS_CHECKOUT}" rev-parse HEAD 2>/dev/null)" >> "${CALL_LOG}"
[ "$(git -C "${DET_SKIP_SECURITY_PASS_CHECKOUT}" rev-parse HEAD 2>/dev/null)" = "${PR_HEAD_SHA}" ] || { echo "hold=true" >> "${GITHUB_OUTPUT}"; exit 1; }
printf 'hold=%s\\nexhausted=%s\\n' "${PASS_HOLD:-false}" "${PASS_EXHAUSTED:-false}" >> "${GITHUB_OUTPUT}"
'''


def _lib_dir(tmp_path: Path, *, gh_helpers: bool = True) -> Path:
	lib = tmp_path / "support" / "scripts"
	lib.mkdir(parents=True)
	shutil.copy(LIB, lib / "pr_checks_lib.sh")
	(lib / "review_head_gate.sh").write_text(STUB_HEAD_GATE, encoding="utf-8")
	(lib / "review_autofix_step_deterministic_skip_security_pass.sh").write_text(STUB_PASS, encoding="utf-8")
	if gh_helpers:
		(lib / "gh_helpers.sh").write_text("#!/usr/bin/env bash\n:\n", encoding="utf-8")
	return lib


def _pull_json(update: str, *, labels: list[str] | None = None) -> str:
	return json.dumps({"head": {"sha": update, "ref": "feature/x", "repo": {"full_name": "o/r"}},
		"base": {"ref": "main"}, "labels": [{"name": n} for n in (labels or [])]})


def _inline(tmp_path: Path, *, update: str = UPDATE, marked: str = "true", quick: str = "true", ci_outcome: str = "dispatched",
		wait_rc: int = 0, gh_helpers: bool = True, pull_json: str | None = None, args: str | None = None,
		relative: bool = False, **env: str) -> tuple[int, str, list[str]]:
	lib = _lib_dir(tmp_path, gh_helpers=gh_helpers)
	# relative=True sources the library the way the deterministic-skip-merge
	# job does (`source .codex-freshness-src/scripts/pr_checks_lib.sh`).
	source_line = (f"cd {str(lib.parent)!r}\nsource scripts/pr_checks_lib.sh" if relative
		else f"source {str(lib / 'pr_checks_lib.sh')!r}")
	script = f"""
set -euo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
  echo "SAFE_GH $*" >> "${{CALL_LOG}}"
  case "$*" in
    user) printf '%s' '{{"login":"pipeline-bot"}}' ;;
    *comments*) if [ -n "${{INLINE_COMMENTS_JSON:-}}" ]; then printf '%s' "${{INLINE_COMMENTS_JSON}}"; else printf '%s' '[[{{"user":{{"login":"pipeline-bot"}},"body":"x"}}]]'; fi ;;
    *) return 1 ;;
  esac
}}
gh() {{
  echo "GH $*" >> "${{CALL_LOG}}"
  case "$*" in
    "workflow run"*) return "${{DISPATCH_RC:-0}}" ;;
  esac
  return 1
}}
{source_line}
_pr_wait_for_required_checks() {{ echo "WAIT $*" >> "${{CALL_LOG}}"; PR_CHECKS_WAIT_OUTCOME=x; return {wait_rc}; }}
PR_BASE_SYNC_INLINE_MARKED={marked}
PR_BASE_FRESHNESS_OUTCOME=overlap
PR_BASE_FRESHNESS_HEAD_SHA={APPROVED}
PR_BASE_SYNC_MERGE_SHA={update}
PR_BASE_SYNC_CI_QUICK={quick}
PR_BASE_SYNC_CI_OUTCOME={ci_outcome}
PR_BASE_SYNC_PULL_JSON="${{PULL_JSON}}"
rc=0
_pr_base_sync_inline_merge {args or f"o/r 7 {APPROVED} {update} main"} || rc=$?
echo "rc=${{rc}} inline_head=${{PR_BASE_SYNC_INLINE_HEAD:-}}"
"""
	full_env = _base_env(tmp_path, PULL_JSON=pull_json if pull_json is not None else _pull_json(update),
		MERGE_TRAIN_DISPATCH_WORKFLOWS="ai-review.yml review_autofix.yml", **env)
	res = subprocess.run(["bash", "-c", script], env=full_env, capture_output=True, text=True, timeout=120)
	assert res.returncode == 0, res.stdout + res.stderr
	rc_line = [line for line in res.stdout.splitlines() if line.startswith("rc=")][-1]
	return int(rc_line.split()[0][3:]), res.stdout + res.stderr, _calls(tmp_path)


def _dispatches(calls: list[str]) -> list[str]:
	return [c for c in calls if c.startswith("GH workflow run")]


def test_success_path_returns_the_update_head(tmp_path: Path) -> None:
	rc, out, calls = _inline(tmp_path)
	assert rc == 0
	assert f"inline_head={UPDATE}" in out
	assert f"MERGE_BASE_FRESHNESS pr=7 outcome=inline_merge reason=ok head={UPDATE} dispatch=none" in out
	assert _dispatches(calls) == []
	assert sum(1 for c in calls if c.startswith(f"POST_STATUS repo=o/r sha={UPDATE} state=success")) == 2
	assert any(c.startswith(f"FRESHNESS_SYNC freshness-sync o/r 7 {UPDATE} main pipeline-bot ") for c in calls)
	assert f"WAIT 7 {UPDATE} main" in calls


def test_success_path_with_a_relatively_sourced_library(tmp_path: Path) -> None:
	rc, out, calls = _inline(tmp_path, relative=True)
	assert rc == 0, out
	assert f"MERGE_BASE_FRESHNESS pr=7 outcome=inline_merge reason=ok head={UPDATE} dispatch=none" in out
	assert sum(1 for c in calls if c.startswith(f"POST_STATUS repo=o/r sha={UPDATE} state=success")) == 2
	assert _dispatches(calls) == []


def test_unmarked_call_returns_1_without_dispatch_or_log(tmp_path: Path) -> None:
	rc, out, calls = _inline(tmp_path, marked="false")
	assert rc == 1
	assert "outcome=inline_merge" not in out
	assert calls == []


def test_switch_off_returns_1_without_dispatch(tmp_path: Path) -> None:
	rc, out, calls = _inline(tmp_path, MERGE_LEASE_INLINE_SYNC_ENABLED="false")
	assert rc == 1
	assert "outcome=inline_merge" not in out
	assert calls == []


@pytest.mark.parametrize("kwargs,reason", [
	({"quick": "false"}, "not_quick"),
	({"ci_outcome": "dispatch_failed"}, "not_quick"),
	({"FS_RC": "1"}, "freshness_sync"),
	({"gh_helpers": False}, "head_gate_status"),
	({"POST_FAIL_ON": "1"}, "head_gate_status"),
	({"POST_FAIL_ON": "2"}, "head_gate_status"),
	({"SINGLE_ISSUE_SECURITY_PASS_ENABLED": "true"}, "security_pass"),
	({"wait_rc": 1}, "required_checks"),
	({"args": f"o/r 7 {APPROVED} {'d' * 40} main"}, "invalid_input"),
	({"pull_json": _pull_json("e" * 40)}, "security_pass"),
])
def test_each_failing_step_dispatches_the_review_once(tmp_path: Path, kwargs: dict, reason: str) -> None:
	rc, out, calls = _inline(tmp_path, **kwargs)
	assert rc == 1
	assert f"outcome=inline_merge reason={reason} " in out
	assert "dispatch=dispatched" in out
	dispatches = _dispatches(calls)
	assert len(dispatches) == 1, calls
	assert dispatches[0] == "GH workflow run ai-review.yml --repo o/r -f pr_number=7 -f allow_workflow_edits=true"
	assert re.search(r"^rc=1 inline_head=$", out, re.M), out


def test_inline_comments_keep_the_author_type_for_a_final_ci_failed_marker(tmp_path: Path) -> None:
	# PR #10152: the verifier trusts a github-actions[bot] final-CI marker only
	# with author_type "Bot", so the inline projection must keep .user.type.
	saved = tmp_path / "saved_comments.json"
	raw = [[
		{"user": {"login": "pipeline-bot", "type": "User"}, "body": _golden_marker()},
		{"user": {"login": "github-actions[bot]", "type": "Bot"},
			"body": f"Final CI failed.\n\n<!-- ai:final-ci-failed:v1 pr=7 head={UPDATE} -->"},
	]]
	_inline(tmp_path, INLINE_COMMENTS_JSON=json.dumps(raw), FS_SAVE_COMMENTS=str(saved))
	projected = json.loads(saved.read_text(encoding="utf-8"))
	assert projected[1] == {"author_login": "github-actions[bot]", "author_type": "Bot", "body": raw[0][1]["body"]}
	bin_dir = _fake_gh_bin(tmp_path)
	env = {**os.environ, **_base_env(tmp_path), "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_RETRY_MAX_ATTEMPTS": "1",
		"FAKE_FROM": APPROVED, "FAKE_BASE": BASE_TIP}
	res = subprocess.run(["bash", str(HEAD_GATE), "freshness-sync", "o/r", "7", UPDATE, "main", "pipeline-bot", str(saved)],
		env=env, capture_output=True, text=True, check=False)
	assert res.returncode == 1, res.stdout + res.stderr
	assert "outcome=review reason=final_ci_failed_on_head" in res.stdout


def test_failed_first_post_stops_before_the_checks_wait(tmp_path: Path) -> None:
	rc, out, calls = _inline(tmp_path, POST_FAIL_ON="1")
	assert rc == 1 and "reason=head_gate_status" in out
	assert not any(c.startswith("WAIT ") for c in calls)


def test_head_gate_status_switched_off_still_merges(tmp_path: Path) -> None:
	lib = tmp_path / "real" / "scripts"
	lib.mkdir(parents=True)
	shutil.copy(HEAD_GATE, lib / "review_head_gate.sh")
	(lib / "gh_helpers.sh").write_text("#!/usr/bin/env bash\n:\n", encoding="utf-8")
	script = f"""
set -euo pipefail
source {str(LIB)!r}
_pr_base_sync_inline_post_status {str(lib)!r} o/r 7 {UPDATE} && echo "post_rc=0" || echo "post_rc=1"
"""
	res = subprocess.run(["bash", "-c", script], env=_base_env(tmp_path, REVIEW_HEAD_GATE_STATUS_ENABLED="false"),
		capture_output=True, text=True, timeout=60)
	assert "outcome=disabled" in res.stdout and "post_rc=0" in res.stdout, res.stdout + res.stderr


def test_failed_dispatch_tries_each_workflow_and_logs_it(tmp_path: Path) -> None:
	rc, out, calls = _inline(tmp_path, wait_rc=1, DISPATCH_RC="1")
	assert rc == 1
	assert "reason=required_checks" in out and "dispatch=dispatch_failed" in out
	assert len(_dispatches(calls)) == 2


def _git(*args: str, cwd: Path) -> str:
	return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
		env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
			"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def _checkout_with_update_head(tmp_path: Path) -> tuple[Path, str]:
	origin = tmp_path / "origin"
	origin.mkdir()
	_git("init", "-q", cwd=origin)
	(origin / "f.txt").write_text("one\n", encoding="utf-8")
	_git("add", "f.txt", cwd=origin)
	_git("commit", "-q", "-m", "one", cwd=origin)
	first = _git("rev-parse", "HEAD", cwd=origin)
	(origin / "f.txt").write_text("two\n", encoding="utf-8")
	_git("commit", "-q", "-am", "two", cwd=origin)
	update = _git("rev-parse", "HEAD", cwd=origin)
	_git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=origin)
	checkout = tmp_path / "checkout"
	checkout.mkdir()
	_git("init", "-q", cwd=checkout)
	_git("remote", "add", "origin", f"file://{origin}", cwd=checkout)
	_git("fetch", "-q", "--depth=1", "origin", first, cwd=checkout)
	_git("checkout", "-q", "--detach", first, cwd=checkout)
	return checkout, update


def _pass_env(checkout: Path, **extra: str) -> dict[str, str]:
	return {"SINGLE_ISSUE_SECURITY_PASS_ENABLED": "true", "DEFAULT_BRANCH": "main", "AUTOFIX_SKIP_RUNS_SECURITY_PASS": "true",
		"DET_SKIP_SECURITY_PASS_CHECKOUT": str(checkout), "GIT_CONFIG_GLOBAL": os.devnull, **extra}


def test_security_pass_runs_on_a_worktree_of_the_update_head(tmp_path: Path) -> None:
	checkout, update = _checkout_with_update_head(tmp_path)
	rc, out, calls = _inline(tmp_path, update=update, **_pass_env(checkout))
	assert rc == 0, out
	assert f"SECURITY_PASS head={update} checkout_head={update}" in calls
	assert _dispatches(calls) == []
	# The worktree is removed again.
	assert len(_git("worktree", "list", cwd=checkout).splitlines()) == 1


def test_security_pass_hold_fails_and_dispatches_once(tmp_path: Path) -> None:
	checkout, update = _checkout_with_update_head(tmp_path)
	rc, out, calls = _inline(tmp_path, update=update, **_pass_env(checkout, PASS_HOLD="true"))
	assert rc == 1
	assert "reason=security_pass" in out and "dispatch=dispatched" in out
	assert len(_dispatches(calls)) == 1
	assert len(_git("worktree", "list", cwd=checkout).splitlines()) == 1


def test_exhausted_security_pass_does_not_dispatch_twice(tmp_path: Path) -> None:
	checkout, update = _checkout_with_update_head(tmp_path)
	rc, out, calls = _inline(tmp_path, update=update, **_pass_env(checkout, PASS_HOLD="true", PASS_EXHAUSTED="true"))
	assert rc == 1
	assert "reason=security_pass" in out and "dispatch=by_security_pass" in out
	assert _dispatches(calls) == []


@pytest.mark.parametrize("extra", [{"AUTOFIX_SKIP_RUNS_SECURITY_PASS": "false"}, {"DET_SKIP_SECURITY_PASS_CHECKOUT": "/nonexistent/checkout"}])
def test_required_security_pass_without_its_inputs_fails_closed(tmp_path: Path, extra: dict) -> None:
	checkout, update = _checkout_with_update_head(tmp_path)
	rc, out, calls = _inline(tmp_path, update=update, **_pass_env(checkout, **extra))
	assert rc == 1
	assert "reason=security_pass" in out
	assert len(_dispatches(calls)) == 1
	assert not any(c.startswith("WAIT ") for c in calls)


def test_security_pass_not_required_for_e2e_label(tmp_path: Path) -> None:
	rc, out, calls = _inline(tmp_path, pull_json=_pull_json(UPDATE, labels=["e2e-smoke-test"]),
		SINGLE_ISSUE_SECURITY_PASS_ENABLED="true", DEFAULT_BRANCH="main")
	assert rc == 0, out
	assert not any(c.startswith("SECURITY_PASS") for c in calls)


# --- the gate's owner check --------------------------------------------------------

def _owner_function() -> str:
	text = EVALUATE_GATE.read_text(encoding="utf-8")
	match = re.search(r"^  gate_lease_inline_owner_active\(\)\n  \{\n.*?^  \}\n", text, re.M | re.S)
	assert match, "gate_lease_inline_owner_active not found"
	return match.group(0)


def _owner(tmp_path: Path, comments: list[dict], **env: str) -> tuple[int, str, list[str]]:
	bin_dir = _fake_gh_bin(tmp_path)
	work = tmp_path / "work"
	(work / ".codex-head-gate-src" / "scripts").mkdir(parents=True)
	shutil.copy(HEAD_GATE, work / ".codex-head-gate-src" / "scripts" / "review_head_gate.sh")
	shutil.copy(SCRIPTS / "gh_helpers.sh", work / ".codex-head-gate-src" / "scripts" / "gh_helpers.sh")
	comments_file = _comments_file(tmp_path, comments)
	script = f"""
set -euo pipefail
REPOSITORY=o/r PR_NUMBER=7 pr_head_sha_gate={UPDATE}
gate_marker_fetch_state=ok gate_marker_author_login=pipeline-bot gate_marker_comments_file={str(comments_file)!r}
{_owner_function()}
rc=0
gate_lease_inline_owner_active || rc=$?
echo "rc=${{rc}}"
"""
	full_env = {**_base_env(tmp_path), "PATH": f"{bin_dir}:{os.environ['PATH']}", "EVENT_NAME": "pull_request", **env}
	res = subprocess.run(["bash", "-c", script], cwd=work, env=full_env, capture_output=True, text=True, timeout=60)
	assert res.returncode == 0, res.stdout + res.stderr
	rc = int([line for line in res.stdout.splitlines() if line.startswith("rc=")][-1][3:])
	return rc, res.stdout, _calls(tmp_path)


@pytest.mark.parametrize("status", ["queued", "in_progress", "requested", "waiting", "pending"])
def test_gate_skips_for_a_live_owner_run(tmp_path: Path, status: str) -> None:
	rc, _out, calls = _owner(tmp_path, [_gate_marker(f" inline_run={RUN_ID}")], FAKE_RUN_STATUS=status)
	assert rc == 0
	assert len(calls) == 1 and calls[0].startswith(f"gh api repos/o/r/actions/runs/{RUN_ID} --jq ")


@pytest.mark.parametrize("action", ["synchronize", "opened"])
def test_gate_keeps_the_owner_skip_when_auto_merge_was_enabled(tmp_path: Path, action: str) -> None:
	# The PR's auto-merge flag (read before the early head-gate call withdrew
	# it) never bypasses the owner run's status: a live owner keeps the head.
	rc, out, calls = _owner(tmp_path, [_gate_marker(f" inline_run={RUN_ID}")], FAKE_RUN_STATUS="in_progress",
		pr_auto_merge_enabled="true", EVENT_ACTION=action)
	assert rc == 0
	assert len(calls) == 1 and calls[0].startswith(f"gh api repos/o/r/actions/runs/{RUN_ID} --jq ")
	withdrawn = f"skip=true reason=lease_inline_owner_auto_merge_withdrawn run={RUN_ID}"
	assert (withdrawn in out) == (action == "synchronize")


def test_gate_names_no_withdrawal_when_the_withdraw_switch_is_off(tmp_path: Path) -> None:
	# With REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED=false the early call withdrew
	# nothing, so the line claiming a withdrawal is not printed; the skip stays.
	rc, out, calls = _owner(tmp_path, [_gate_marker(f" inline_run={RUN_ID}")], FAKE_RUN_STATUS="in_progress",
		pr_auto_merge_enabled="true", EVENT_ACTION="synchronize", REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED="false")
	assert rc == 0
	assert len(calls) == 1
	assert "lease_inline_owner_auto_merge_withdrawn" not in out


def test_gate_proceeds_when_the_run_status_is_empty(tmp_path: Path) -> None:
	rc, out, calls = _owner(tmp_path, [_gate_marker(f" inline_run={RUN_ID}")], FAKE_RUN_STATUS="")
	assert rc == 1
	assert len(calls) == 1
	# An empty answer is logged as a failed read, like a failed GET.
	assert f"AUTOFIX_GATE_DET_SKIP_EVAL pr=7 skip=false reason=lease_inline_owner_read_failed run={RUN_ID}" in out


def test_gate_proceeds_with_auto_merge_enabled_once_the_owner_completed(tmp_path: Path) -> None:
	rc, out, calls = _owner(tmp_path, [_gate_marker(f" inline_run={RUN_ID}")], FAKE_RUN_STATUS="completed",
		pr_auto_merge_enabled="true", EVENT_ACTION="synchronize")
	assert rc == 1
	assert len(calls) == 1
	assert "lease_inline_owner_auto_merge_withdrawn" not in out


def test_gate_proceeds_for_a_completed_run(tmp_path: Path) -> None:
	rc, _out, calls = _owner(tmp_path, [_gate_marker(f" inline_run={RUN_ID}")], FAKE_RUN_STATUS="completed")
	assert rc == 1
	assert len(calls) == 1


def test_gate_proceeds_when_the_run_read_fails(tmp_path: Path) -> None:
	rc, out, _calls_ = _owner(tmp_path, [_gate_marker(f" inline_run={RUN_ID}")], FAKE_RUN_STATUS="FAIL")
	assert rc == 1
	assert "AUTOFIX_GATE_DET_SKIP_EVAL pr=7 skip=false reason=lease_inline_owner_read_failed" in out


@pytest.mark.parametrize("comments,env", [
	([_gate_marker(f" inline_run={RUN_ID}")], {"EVENT_NAME": "workflow_dispatch"}),
	([_gate_marker(f" inline_run={RUN_ID}")], {"MERGE_LEASE_INLINE_SYNC_ENABLED": "false"}),
	([_gate_marker()], {}),
	([_gate_marker(f" inline_run={RUN_ID}", author="intruder")], {}),
])
def test_gate_makes_no_read_without_a_trusted_inline_marker(tmp_path: Path, comments: list[dict], env: dict) -> None:
	rc, _out, calls = _owner(tmp_path, comments, FAKE_RUN_STATUS="in_progress", **env)
	assert rc == 1
	assert calls == []


def test_gate_owner_branch_sets_the_skip_reason() -> None:
	text = EVALUATE_GATE.read_text(encoding="utf-8")
	block = text[text.index("  elif gate_freshness_sync_fast_path; then"):]
	block = block[:block.index("  else\n    # Size check")]
	assert "if gate_lease_inline_owner_active; then" in block
	assert 'SKIP_REASON="lease_inline_owner_active"' in block
	assert "reason=lease_inline_owner head=" in block
	owner = block[:block.index("    else\n")]
	assert 'DETERMINISTIC_SKIP="true"' not in owner


# --- call sites --------------------------------------------------------------------

def test_call_sites_pass_the_caller_flag_only_where_listed() -> None:
	workflow = WORKFLOW.read_text(encoding="utf-8")
	assert workflow.count("PR_BASE_SYNC_INLINE_CALLER=true _pr_base_fresh_for_merge") == 2
	assert workflow.count("type _pr_base_sync_inline_merge >/dev/null 2>&1 && _pr_base_sync_inline_merge") == 2
	assert workflow.count("MERGE_LEASE_INLINE_SYNC_ENABLED: ${{ vars.MERGE_LEASE_INLINE_SYNC_ENABLED || 'true' }}") == 4
	auto_merge = ENABLE_AUTO_MERGE.read_text(encoding="utf-8")
	assert auto_merge.count("PR_BASE_SYNC_INLINE_CALLER=true _pr_base_fresh_for_merge") == 2
	assert auto_merge.count('INITIAL_HEAD_SHA="${PR_BASE_SYNC_INLINE_HEAD}"') == 2
	judge = RB_JUDGE.read_text(encoding="utf-8")
	assert judge.count("PR_BASE_SYNC_INLINE_CALLER=true _pr_base_fresh_for_merge") == 3
	assert judge.count('RB_JUDGED_HEAD_SHA="${PR_BASE_SYNC_INLINE_HEAD}"') == 3
	for text in (workflow, auto_merge, judge):
		for line in text.splitlines():
			if "_pr_base_sync_inline_merge \"" in line:
				assert "type _pr_base_sync_inline_merge >/dev/null 2>&1" in line
	poller = (SCRIPTS / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
	train = (SCRIPTS / "review_merge_train.sh").read_text(encoding="utf-8")
	assert "PR_BASE_SYNC_INLINE_CALLER" not in poller and "_pr_base_sync_inline_merge" not in poller
	assert "PR_BASE_SYNC_INLINE_CALLER" not in train and "_pr_base_sync_inline_merge" not in train
