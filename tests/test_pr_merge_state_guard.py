"""Issue #10260 (re-issued from #10049): review merge paths re-read the PR's
live state and never merge a PR that was closed while the run waited.

Incident: tele-funtoken-msg-scoring PR #5800 was closed at 12:04:00 UTC and
squash-merged at 12:26:05 by `review / deterministic-skip-merge` of a review
run whose gate passed at 11:58, while the PR was still open.

Covers the shared helper `_pr_merge_open_guard` and its lease release
(scripts/pr_checks_lib.sh), a run of the deterministic-skip merge step of
.github/workflows/review_autofix.yml with a fake `gh`, a run of
scripts/review_enable_auto_merge.sh, and the call order at every merge site.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "scripts" / "pr_checks_lib.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "review_autofix.yml"
AUTO_MERGE = ROOT / "scripts" / "review_enable_auto_merge.sh"
RB_JUDGE = ROOT / "scripts" / "review_rb_judge.sh"
MERGE_TRAIN = ROOT / "scripts" / "review_merge_train.sh"
DET_SKIP_STEP = "Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge"

SHA = "fee7005234cc58358be7a9eaddb29c3e3e8ac3d1"
OTHER_SHA = "1" * 40
PR = "5800"

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
calls = os.environ["FAKE_CALLS"]
with open(calls, "a", encoding="utf-8") as fh:
	fh.write(" ".join(args) + "\n")
joined = " ".join(args)
if args[:2] == ["pr", "merge"]:
	sys.exit(int(os.environ.get("FAKE_MERGE_RC", "0")))
if args and args[0] == "label":
	sys.exit(0)
if args and args[0] == "api" and "graphql" in args:
	sys.exit(0)
if args and args[0] == "api" and "/labels" in joined:
	sys.exit(0)
pulls = [a for a in args if a.startswith("repos/") and "/pulls/" in a]
if args and args[0] == "api" and pulls:
	seq = os.environ["FAKE_PULLS_DIR"]
	counter = os.path.join(seq, "counter")
	n = int(open(counter).read()) if os.path.exists(counter) else 0
	n += 1
	open(counter, "w").write(str(n))
	path = os.path.join(seq, "pull.%d" % n)
	if not os.path.exists(path):
		names = sorted((p for p in os.listdir(seq) if p.startswith("pull.")), key=lambda p: int(p.split(".")[1]))
		path = os.path.join(seq, names[-1])
	body = open(path).read()
	if body.startswith("FAIL"):
		sys.stderr.write("gh: API rate limit exceeded (HTTP 403)\n")
		sys.exit(1)
	sys.stdout.write(body)
	sys.exit(0)
sys.exit(0)
'''


def _clean_env(**extra: str) -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update({
		"GIT_CONFIG_GLOBAL": "/dev/null",
		"GIT_CONFIG_NOSYSTEM": "1",
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	env.update(extra)
	return env


def _pull(state: str = "open", *, head: str = SHA, draft: bool = False, closed_at: str | None = None,
		merged_at: str | None = None) -> str:
	return json.dumps({
		"number": int(PR), "state": state, "draft": draft, "head": {"sha": head, "ref": "ai/issue-5797"},
		"base": {"ref": "main"}, "closed_at": closed_at, "merged_at": merged_at, "body": "",
	})


CLOSED_5800 = _pull("closed", closed_at="2026-11-24T12:04:00Z")


def _fake_gh(tmp_path: Path, pulls: list[str]) -> dict[str, str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	sleep = bin_dir / "sleep"
	sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
	sleep.chmod(0o755)
	seq = tmp_path / "pulls"
	seq.mkdir(exist_ok=True)
	for i, body in enumerate(pulls, start=1):
		(seq / f"pull.{i}").write_text(body, encoding="utf-8")
	calls = tmp_path / "calls.log"
	calls.write_text("", encoding="utf-8")
	return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_PULLS_DIR": str(seq), "FAKE_CALLS": str(calls)}


def _calls(tmp_path: Path) -> list[str]:
	path = tmp_path / "calls.log"
	return [line for line in path.read_text(encoding="utf-8").splitlines() if line] if path.exists() else []


# ---------------------------------------------------------------------------
# Helper unit cases
# ---------------------------------------------------------------------------

def _guard(tmp_path: Path, script: str, pulls: list[str] | None = None, **env: str) -> subprocess.CompletedProcess[str]:
	fake = _fake_gh(tmp_path, pulls or ["FAIL"])
	prelude = f'''
set -euo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{ gh api "$@"; }}
source "{LIB}"
'''
	return subprocess.run(["bash", "-c", prelude + script], cwd=tmp_path, capture_output=True, text=True, check=False,
		env=_clean_env(GITHUB_REPOSITORY="o/r", **fake, **env))


def _guard_once(tmp_path: Path, json_input: str = "", pulls: list[str] | None = None, head: str = SHA,
		**env: str) -> subprocess.CompletedProcess[str]:
	quoted_input = shlex.quote(json_input) if json_input else "''"
	script = (f"rc=0; _pr_merge_open_guard {PR} {head} pre_merge {quoted_input} || rc=$?\n"
		'echo "RC=${rc} REASON=${PR_MERGE_OPEN_GUARD_REASON} CLOSED_AT=${PR_MERGE_OPEN_GUARD_CLOSED_AT} BASE=${PR_MERGE_OPEN_GUARD_BASE_REF}"\n')
	return _guard(tmp_path, script, pulls, **env)


def _skip_lines(stdout: str) -> list[str]:
	return [line for line in stdout.splitlines() if line.startswith("AUTOFIX_MERGE_SKIPPED")]


def test_open_pr_at_bound_head_proceeds_without_log(tmp_path: Path) -> None:
	res = _guard_once(tmp_path, pulls=[_pull()])
	assert res.returncode == 0, res.stderr
	assert "RC=0 REASON= " in res.stdout
	assert _skip_lines(res.stdout) == []
	assert len([c for c in _calls(tmp_path) if "pulls/5800" in c]) == 1


def test_closed_pr_logs_the_exact_skip_line(tmp_path: Path) -> None:
	res = _guard_once(tmp_path, pulls=[CLOSED_5800])
	assert _skip_lines(res.stdout) == [
		f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason=pr_closed closed_at=2026-11-24T12:04:00Z"]
	assert "RC=1 REASON=pr_closed CLOSED_AT=2026-11-24T12:04:00Z BASE=main" in res.stdout


@pytest.mark.parametrize(("body", "reason"), [
	(_pull("closed", closed_at="2026-11-24T12:04:00Z", merged_at="2026-11-24T12:04:00Z"), "pr_merged"),
	(_pull(draft=True), "pr_draft"),
	(_pull(head=OTHER_SHA), "head_moved"),
	("not json", "pr_state_unavailable"),
	("[]", "pr_state_unavailable"),
	(json.dumps({"state": "weird", "head": {"sha": SHA}}), "pr_state_unavailable"),
	("FAIL", "pr_state_unavailable"),
])
def test_refusal_reasons(tmp_path: Path, body: str, reason: str) -> None:
	res = _guard_once(tmp_path, pulls=[body])
	assert f"RC=1 REASON={reason} " in res.stdout, res.stdout + res.stderr
	assert _skip_lines(res.stdout) == [f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason={reason}"]


def test_rate_limited_read_warns_with_the_phase(tmp_path: Path) -> None:
	res = _guard_once(tmp_path, pulls=["FAIL"])
	assert "::warning::" in res.stdout and "phase pre_merge" in res.stdout


def test_invalid_bound_sha_fails_closed_without_a_call(tmp_path: Path) -> None:
	res = _guard_once(tmp_path, pulls=[_pull()], head="abc")
	assert "RC=1 REASON=pr_state_unavailable" in res.stdout
	assert _calls(tmp_path) == []


def test_json_input_makes_no_call(tmp_path: Path) -> None:
	res = _guard_once(tmp_path, json_input=CLOSED_5800)
	assert "RC=1 REASON=pr_closed" in res.stdout
	assert _calls(tmp_path) == []


@pytest.mark.parametrize("value", ["false", "0", "no", "OFF"])
def test_switch_off_makes_no_call(tmp_path: Path, value: str) -> None:
	res = _guard_once(tmp_path, pulls=[CLOSED_5800], PR_MERGE_OPEN_GUARD_ENABLED=value)
	assert "RC=0 REASON= " in res.stdout
	assert _skip_lines(res.stdout) == []
	assert _calls(tmp_path) == []


def test_one_skip_line_per_job(tmp_path: Path) -> None:
	script = (f"_pr_merge_open_guard {PR} {SHA} start '' || true\n"
		f"_pr_merge_open_guard {PR} {SHA} pre_merge '' || true\n")
	res = _guard(tmp_path, script, [CLOSED_5800])
	assert len(_skip_lines(res.stdout)) == 1


# ---------------------------------------------------------------------------
# Lease release
# ---------------------------------------------------------------------------

LEASE_STUBS = r'''
_merge_lease_read() {
	[ "${FAKE_LEASE_READ:-ok}" = "fail" ] && return 1
	[ "${FAKE_LEASE_READ:-ok}" = "none" ] && return 2
	MERGE_LEASE_STATE=held; MERGE_LEASE_PR="${FAKE_LEASE_PRIMARY}"; MERGE_LEASE_HEAD="''' + SHA + r'''"
	MERGE_LEASE_HOLDER_ALL_ROWS="${FAKE_LEASE_ROWS}"; MERGE_LEASE_HOLDER_ROWS="${FAKE_LEASE_ROWS}"
	MERGE_LEASE_HOLDER_COUNT="$(printf '%s\n' "${FAKE_LEASE_ROWS}" | sed '/^$/d' | wc -l | tr -d ' ')"
	return 0
}
_merge_lease_write() { echo "LEASE_WRITE $1 $2 $3"; return 0; }
_merge_lease_write_holders() { echo "LEASE_WRITE_HOLDERS $1 $(printf '%s' "$2" | awk '{print $1}' | tr '\n' ',')"; return 0; }
_pr_merge_open_guard_release_lease 5800 main
'''


def _row(pr: str) -> str:
	return f"{pr} {SHA} 9999999999 100 100"


def test_release_single_holder_writes_released(tmp_path: Path) -> None:
	res = _guard(tmp_path, LEASE_STUBS, FAKE_LEASE_PRIMARY=PR, FAKE_LEASE_ROWS=_row(PR))
	assert "LEASE_WRITE main released 5800" in res.stdout
	assert "MERGE_LEASE pr=5800 base=main mode=release outcome=released reason=pr_closed" in res.stdout


def test_release_multi_holder_removes_only_its_row(tmp_path: Path) -> None:
	res = _guard(tmp_path, LEASE_STUBS, FAKE_LEASE_PRIMARY="42", FAKE_LEASE_ROWS=_row("42") + "\n" + _row(PR))
	assert "LEASE_WRITE_HOLDERS main 42," in res.stdout
	assert "LEASE_WRITE main released" not in res.stdout


def test_release_not_the_holder_writes_nothing(tmp_path: Path) -> None:
	res = _guard(tmp_path, LEASE_STUBS, FAKE_LEASE_PRIMARY="42", FAKE_LEASE_ROWS=_row("42"))
	assert "LEASE_WRITE" not in res.stdout and "MERGE_LEASE " not in res.stdout


def test_release_read_failure_only_warns(tmp_path: Path) -> None:
	res = _guard(tmp_path, LEASE_STUBS + "echo DONE\n", FAKE_LEASE_READ="fail", FAKE_LEASE_PRIMARY=PR, FAKE_LEASE_ROWS=_row(PR))
	assert res.returncode == 0
	assert "::warning::" in res.stdout and "DONE" in res.stdout
	assert "LEASE_WRITE" not in res.stdout


def test_release_lease_disabled_does_nothing(tmp_path: Path) -> None:
	res = _guard(tmp_path, LEASE_STUBS, MERGE_LEASE_ENABLED="false", FAKE_LEASE_PRIMARY=PR, FAKE_LEASE_ROWS=_row(PR))
	assert "LEASE_WRITE" not in res.stdout


# ---------------------------------------------------------------------------
# deterministic-skip-merge step run
# ---------------------------------------------------------------------------

def _step_run_body(step_name: str) -> str:
	text = WORKFLOW.read_text(encoding="utf-8")
	start = text.find(f"      - name: {step_name}\n")
	assert start != -1, step_name
	lines = text[start:].splitlines()
	run_idx = next(i for i, line in enumerate(lines) if line == "        run: |")
	body: list[str] = []
	for line in lines[run_idx + 1:]:
		if line.strip() and not line.startswith(" " * 10):
			break
		body.append(line[10:])
	return "\n".join(body) + "\n"


SHIM_LIB = r'''
source "${REAL_PR_CHECKS_LIB}"
_pr_base_fresh_for_merge() { echo "FRESH_CALL $*" >> "${FAKE_CALLS}"; PR_BASE_FRESHNESS_OUTCOME=fresh; return 0; }
_pr_wait_for_required_checks() { echo "WAIT_CALL $*" >> "${FAKE_CALLS}"; PR_CHECKS_WAIT_OUTCOME=ok; PR_CHECKS_WAIT_WAITED_S=240; return 0; }
_merge_lease_read() {
	MERGE_LEASE_STATE=held; MERGE_LEASE_PR=5800; MERGE_LEASE_HEAD=''' + SHA + r'''
	MERGE_LEASE_HOLDER_ALL_ROWS="5800 ''' + SHA + r''' 9999999999 1 1"; MERGE_LEASE_HOLDER_ROWS="${MERGE_LEASE_HOLDER_ALL_ROWS}"; MERGE_LEASE_HOLDER_COUNT=1
	return 0
}
_merge_lease_write() { echo "LEASE_WRITE $1 $2 $3" >> "${FAKE_CALLS}"; return 0; }
'''
SHIM_FOLLOWUP = 'det_skip_followup_merge_guard() { DET_SKIP_FOLLOWUP_GUARD_RESULT=allow; return 0; }\n'


def _support_checkout(tmp_path: Path) -> str:
	src = tmp_path / "work" / ".codex-freshness-src"
	(src / "scripts").mkdir(parents=True)
	(src / "scripts" / "pr_checks_lib.sh").write_text(SHIM_LIB, encoding="utf-8")
	(src / "scripts" / "review_autofix_step_det_skip_followup_guard.sh").write_text(SHIM_FOLLOWUP, encoding="utf-8")
	env = _clean_env()
	for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "support"]):
		subprocess.run(cmd, cwd=src, env=env, check=True, capture_output=True)
	return subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, env=env, check=True, capture_output=True,
		text=True).stdout.strip()


def _det_skip(tmp_path: Path, pulls: list[str], head_ref: str = "ai/issue-5797") -> subprocess.CompletedProcess[str]:
	support_sha = _support_checkout(tmp_path)
	fake = _fake_gh(tmp_path, pulls)
	work = tmp_path / "work"
	for name in ("summary", "output", "env"):
		(tmp_path / name).write_text("", encoding="utf-8")
	env = _clean_env(
		**fake,
		REAL_PR_CHECKS_LIB=str(LIB),
		FRESHNESS_HELPER_CHECKOUT_OUTCOME="success",
		REVIEW_SUPPORT_SHA=support_sha,
		PR_NUMBER=PR, PR_HEAD_SHA=SHA, PR_HEAD_REF=head_ref, PR_BASE_REF="main",
		REPOSITORY="o/r", GITHUB_REPOSITORY="o/r",
		ENABLE_AUTO_MERGE="true", FORWARD_MERGE_FALLBACK_AUTO_MERGE="true",
		ORCH_INTEGRATION_BRANCH_PATTERN="^orchestrator/project-",
		DET_SKIP_REASON="doc_only",
		GITHUB_STEP_SUMMARY=str(tmp_path / "summary"),
		GITHUB_OUTPUT=str(tmp_path / "output"),
		GITHUB_ENV=str(tmp_path / "env"),
		MERGE_LEASE_ENABLED="true",
	)
	return subprocess.run(["bash", "-c", _step_run_body(DET_SKIP_STEP)], cwd=work, env=env, capture_output=True,
		text=True, check=False)


def _merge_calls(tmp_path: Path) -> list[str]:
	return [c for c in _calls(tmp_path) if c.startswith("pr merge")]


def _label_calls(tmp_path: Path) -> list[str]:
	return [c for c in _calls(tmp_path) if "/labels" in c or c.startswith("label ")]


def test_det_skip_closed_pr_is_never_merged(tmp_path: Path) -> None:
	res = _det_skip(tmp_path, [CLOSED_5800])
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == []
	assert _label_calls(tmp_path) == []
	assert f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason=pr_closed closed_at=2026-11-24T12:04:00Z" in res.stdout
	calls = _calls(tmp_path)
	assert "LEASE_WRITE main released 5800" in calls
	# Closed at the start: no lease request, no branch update, no checks wait.
	assert not any(c.startswith(("FRESH_CALL", "WAIT_CALL")) for c in calls)
	assert "SKIPPED (pr_closed)" in (tmp_path / "summary").read_text(encoding="utf-8")


def test_det_skip_pr_closed_during_the_wait_is_never_merged(tmp_path: Path) -> None:
	res = _det_skip(tmp_path, [_pull(), CLOSED_5800])
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == []
	assert _label_calls(tmp_path) == []
	assert "reason=pr_closed closed_at=2026-11-24T12:04:00Z" in res.stdout
	calls = _calls(tmp_path)
	assert any(c.startswith("WAIT_CALL") for c in calls)
	# The pre-merge read comes before the post-wait freshness re-check.
	assert len([c for c in calls if c.startswith("FRESH_CALL")]) == 1
	assert "LEASE_WRITE main released 5800" in calls


def test_det_skip_open_pr_merges_exactly_as_before(tmp_path: Path) -> None:
	res = _det_skip(tmp_path, [_pull(), _pull()])
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == [f"pr merge {PR} --repo o/r --squash --auto --match-head-commit {SHA}"]
	assert "AUTOFIX_MERGE_SKIPPED" not in res.stdout
	assert any("labels[]=ai:review-skipped" in c for c in _calls(tmp_path))
	assert len([c for c in _calls(tmp_path) if "pulls/5800" in c]) == 2


def test_det_skip_forward_merge_branch_is_guarded_too(tmp_path: Path) -> None:
	res = _det_skip(tmp_path, [CLOSED_5800], head_ref="auto/forward-merge-stable-1-1")
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == []
	assert "reason=pr_closed" in res.stdout


def test_det_skip_rate_limited_pre_merge_read_does_not_merge(tmp_path: Path) -> None:
	res = _det_skip(tmp_path, [_pull(), "FAIL"])
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == []
	assert _label_calls(tmp_path) == []
	assert f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason=pr_state_unavailable" in res.stdout
	assert not any(c.startswith("LEASE_WRITE") for c in _calls(tmp_path))


def test_replay_pr_5800_timeline(tmp_path: Path) -> None:
	"""11:58 gate passes (open); 12:04:00 closed, no reopen; 12:20:38 merge job starts."""
	closed = json.dumps({
		"number": 5800, "state": "closed", "draft": False, "merged_at": None,
		"closed_at": "2026-11-24T12:04:00Z", "created_at": "2026-11-24T11:40:00Z",
		"head": {"sha": SHA, "ref": "ai/issue-5797"}, "base": {"ref": "main"},
	})
	res = _det_skip(tmp_path, [closed])
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == []
	assert _label_calls(tmp_path) == []
	assert [line for line in res.stdout.splitlines() if line.startswith("AUTOFIX_MERGE_SKIPPED")] == [
		"AUTOFIX_MERGE_SKIPPED pr=5800 head_sha=fee7005234cc58358be7a9eaddb29c3e3e8ac3d1 reason=pr_closed closed_at=2026-11-24T12:04:00Z"]


# ---------------------------------------------------------------------------
# review_enable_auto_merge.sh run
# ---------------------------------------------------------------------------

def _auto_merge(tmp_path: Path, pulls: list[str], shim: str = SHIM_LIB) -> subprocess.CompletedProcess[str]:
	scripts = tmp_path / "scripts"
	scripts.mkdir()
	shutil.copy(AUTO_MERGE, scripts / "review_enable_auto_merge.sh")
	# Production stages gh_helpers.sh next to the script; it provides
	# gh_retry and _safe_gh_jq, the library's transport.
	shutil.copy(ROOT / "scripts" / "gh_helpers.sh", scripts / "gh_helpers.sh")
	(scripts / "pr_checks_lib.sh").write_text(shim, encoding="utf-8")
	(scripts / "review_single_issue_security_pass.sh").write_text(
		"echo SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=allow\n", encoding="utf-8")
	fake = _fake_gh(tmp_path, pulls)
	(tmp_path / "env").write_text("", encoding="utf-8")
	env = _clean_env(**fake, REAL_PR_CHECKS_LIB=str(LIB), GITHUB_REPOSITORY="o/r", PR_NUMBER=PR,
		ENABLE_AUTO_MERGE="true", FORWARD_MERGE_FALLBACK_AUTO_MERGE="true",
		ORCH_INTEGRATION_BRANCH_PATTERN="^orchestrator/project-", INITIAL_HEAD_SHA=SHA,
		GITHUB_ENV=str(tmp_path / "env"), REVIEW_FINAL_CI_WORKFLOW="")
	return subprocess.run(["bash", str(scripts / "review_enable_auto_merge.sh")], cwd=tmp_path, env=env,
		capture_output=True, text=True, check=False)


def test_auto_merge_script_refuses_a_closed_pr_on_its_existing_read(tmp_path: Path) -> None:
	res = _auto_merge(tmp_path, [CLOSED_5800])
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == []
	assert "reason=pr_closed closed_at=2026-11-24T12:04:00Z" in res.stdout
	assert len([c for c in _calls(tmp_path) if "pulls/5800" in c]) == 1
	assert not any(c.startswith(("FRESH_CALL", "WAIT_CALL")) for c in _calls(tmp_path))
	assert "LEASE_WRITE main released 5800" in _calls(tmp_path)
	assert "AUTO_MERGE_READY_LABELS_ALLOWED=true" not in (tmp_path / "env").read_text(encoding="utf-8")


def test_auto_merge_script_refuses_a_pr_closed_during_the_wait(tmp_path: Path) -> None:
	res = _auto_merge(tmp_path, [_pull(), CLOSED_5800])
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == []
	assert "reason=pr_closed" in res.stdout
	assert "AUTO_MERGE_READY_LABELS_ALLOWED=true" not in (tmp_path / "env").read_text(encoding="utf-8")


def test_auto_merge_script_merges_an_open_pr(tmp_path: Path) -> None:
	res = _auto_merge(tmp_path, [_pull(), _pull()])
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == [f"pr merge {PR} --repo o/r --squash --auto --match-head-commit {SHA}"]
	assert "AUTO_MERGE_READY_LABELS_ALLOWED=true" in (tmp_path / "env").read_text(encoding="utf-8")


def test_auto_merge_script_rate_limited_pre_merge_read(tmp_path: Path) -> None:
	res = _auto_merge(tmp_path, [_pull(), "FAIL"])
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == []
	assert "reason=pr_state_unavailable" in res.stdout


# ---------------------------------------------------------------------------
# Call order at every merge site
# ---------------------------------------------------------------------------

def _det_skip_branches() -> list[str]:
	body = _step_run_body(DET_SKIP_STEP)
	fwd = body.index('action=merge_commit"')
	squash = body.index('action=squash"')
	return [body[fwd:squash], body[squash:]]


def test_det_skip_guard_order_in_both_branches() -> None:
	for branch in _det_skip_branches():
		start = branch.index("det_skip_merge_open_guard start")
		first_fresh = branch.index("_pr_base_fresh_for_merge")
		wait = branch.index("_pr_wait_for_required_checks")
		pre = branch.index("det_skip_merge_open_guard pre_merge")
		post_fresh = branch.index("_pr_base_fresh_for_merge", wait)
		merge = branch.index("gh pr merge")
		assert start < first_fresh < wait < pre < post_fresh < merge


def test_auto_merge_script_guard_order() -> None:
	text = AUTO_MERGE.read_text(encoding="utf-8")
	start = text.index("review_enable_auto_merge_open_guard start")
	first_fresh = text.index("PR_BASE_SYNC_INLINE_CALLER=true _pr_base_fresh_for_merge")
	wait = text.index('_pr_wait_for_required_checks "${PR_NUMBER}"')
	pre = text.index("review_enable_auto_merge_open_guard pre_merge")
	post_fresh = text.index("PR_BASE_SYNC_INLINE_CALLER=true _pr_base_fresh_for_merge", pre)
	merges = [m.start() for m in re.finditer(r"gh_retry gh pr merge", text)]
	assert len(merges) == 2
	assert start < first_fresh < wait < pre < post_fresh < min(merges)


def test_rb_judge_guard_precedes_every_merge() -> None:
	text = RB_JUDGE.read_text(encoding="utf-8")
	sites = [m.start() for m in re.finditer(r"elif ! rb_judge_merge_open_guard; then", text)]
	assert len(sites) == 2
	for site in sites:
		assert text.rindex("_pr_wait_for_required_checks", 0, site) > text.rindex("_pr_base_fresh_for_merge", 0, text.rindex("_pr_wait_for_required_checks", 0, site))
		assert site < text.index('success "review-blocked judge approved"', site) < text.index("gh pr merge", site)
		# The guard precedes the post-wait freshness/lease re-check (#10260).
		assert site < text.index("_pr_base_fresh_for_merge", site) < text.index("gh pr merge", site)
	sync = text.index('[ "${ENABLE_AUTO_MERGE}" = "true" ] && ! rb_judge_merge_open_guard; then')
	assert sync < text.index("rb_judge_followup_merge_guard sync", sync) < text.index("_match_head_arg=(--match-head-commit", sync)
	assert "judge_skip_reason=pr_not_open" in text[sync:sync + 1500]


def test_merge_train_resume_guard_order() -> None:
	text = MERGE_TRAIN.read_text(encoding="utf-8")
	guard = text.index('_pr_merge_open_guard "${resume_num}" "${resume_sha}" pre_merge')
	fresh = text.index('_pr_base_fresh_for_merge "${resume_num}"')
	merge = text.index('gh pr merge "${resume_num}"')
	assert text.index("_pr_wait_for_required_checks \"${resume_num}\"") < guard < fresh < merge


def test_skip_log_key_is_documented() -> None:
	fragment = (ROOT / "agents.d" / "10049-merge-open-pr-guard.md").read_text(encoding="utf-8")
	assert "LOG_PREFIX.name=AUTOFIX_MERGE_SKIPPED" in fragment


# ---------------------------------------------------------------------------
# Review follow-ups (#10260)
# ---------------------------------------------------------------------------

def test_auto_merge_script_logs_an_unreadable_initial_pr_read(tmp_path: Path) -> None:
	res = _auto_merge(tmp_path, ["FAIL"])
	assert res.returncode == 0, res.stdout + res.stderr
	assert _merge_calls(tmp_path) == []
	assert f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={SHA} reason=pr_state_unavailable" in res.stdout


INLINE_SHIM = SHIM_LIB.replace(
	'_pr_base_fresh_for_merge() { echo "FRESH_CALL $*" >> "${FAKE_CALLS}"; PR_BASE_FRESHNESS_OUTCOME=fresh; return 0; }',
	'_pr_base_fresh_for_merge() { echo "FRESH_CALL $*" >> "${FAKE_CALLS}"; '
	'if [ "$(grep -c "^FRESH_CALL" "${FAKE_CALLS}")" -ge 2 ]; then '
	'PR_BASE_FRESHNESS_OUTCOME=overlap; return 1; fi; PR_BASE_FRESHNESS_OUTCOME=fresh; return 0; }\n'
	'_pr_base_sync_inline_merge() { echo "INLINE_CALL $*" >> "${FAKE_CALLS}"; '
	f'PR_BASE_SYNC_INLINE_HEAD={OTHER_SHA}; return 0; }}',
)


def test_auto_merge_script_rechecks_after_a_post_wait_inline_merge(tmp_path: Path) -> None:
	# Post-wait freshness takes the inline merge (branch update + checks wait on
	# the update head); the PR is closed during that wait.
	assert INLINE_SHIM != SHIM_LIB
	closed_on_update_head = _pull("closed", head=OTHER_SHA, closed_at="2026-11-24T12:30:00Z")
	res = _auto_merge(tmp_path, [_pull(), _pull(), closed_on_update_head], shim=INLINE_SHIM)
	assert res.returncode == 0, res.stdout + res.stderr
	assert any(c.startswith("INLINE_CALL") for c in _calls(tmp_path))
	assert _merge_calls(tmp_path) == []
	assert f"AUTOFIX_MERGE_SKIPPED pr={PR} head_sha={OTHER_SHA} reason=pr_closed closed_at=2026-11-24T12:30:00Z" in res.stdout


def test_det_skip_head_check_reports_closed_at_and_merged() -> None:
	body = _step_run_body(DET_SKIP_STEP)
	fn = body[body.index("deterministic_skip_head_is_current()"):body.index("deterministic_skip_head_ref_is_integration_pr()")]
	assert '\\(.closed_at // "-") \\(.merged_at // "-")' in fn
	assert "reason=pr_closed closed_at=${ready_label_closed_at}" in fn
	assert "reason=pr_merged" in fn
