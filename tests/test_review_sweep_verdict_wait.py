#!/usr/bin/env python3
"""The review sweep must not re-review a head that already has a verdict and
only waits to merge (issue #9991, re-issued as #10243).

On 2026-11-23 PR #9984 (a 2-line chore, head unchanged since 03:05) got 12
dispatched review runs between 04:37 and 10:31: each run enabled auto-merge
and ended, and the PR never merged because a required check was missing for
that head (#9939). The sweep dispatched it again every 30 minutes.

These tests run the sweep step's shell as extracted from the shipped workflow
against a fake `gh` (and a fake `curl` for Telegram), with the trusted support
scripts staged where the workflow sources them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
SCRIPTS = REPO_ROOT / "scripts"
STEP_MARKER = "- name: Enumerate open PRs and dispatch internal-review.yml"
RUN_HEADER = "        run: |\n"
RUN_INDENT = " " * 10
BLOCK_END_RE = re.compile(r"^(?: {0,9}#| {0,6}- | {0,8}[A-Za-z_][\w-]*:(?:\s|$))")
ALERT_CTX = "ai-review/verdict-wait-alert"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_GH_DIR"])
args = sys.argv[1:]
with open(root / "calls.log", "a") as log:
	log.write(json.dumps(args) + "\n")
def fixture(name, default):
	path = root / name
	return json.loads(path.read_text()) if path.exists() else default
def out(value):
	sys.stdout.write(value if isinstance(value, str) else json.dumps(value))
	sys.exit(0)
if args[:2] == ["workflow", "run"]:
	with open(root / "dispatches.log", "a") as log:
		log.write(" ".join(args) + "\n")
	sys.exit(0)
if args[:1] != ["api"]:
	sys.exit(1)
if args[1:2] == ["graphql"]:
	if (root / "graphql_fails").exists():
		sys.exit(1)
	nodes = []
	for pr in fixture("pulls.json", []):
		meta = fixture("meta.json", {}).get(str(pr["number"]), {})
		status = {}
		if meta.get("gate"):
			status["context"] = {"state": meta["gate"], "createdAt": meta.get("gate_at", "2026-11-01T00:00:00Z")}
		else:
			status["context"] = None
		if meta.get("judge"):
			status["judgeMergePending"] = {"state": meta["judge"], "createdAt": meta.get("judge_at", "2026-11-01T00:00:00Z")}
		if meta.get("alerted"):
			status["verdictWaitAlert"] = {"state": "SUCCESS"}
		node = {"number": pr["number"], "headRefOid": pr["head"]["sha"],
			"mergeable": meta.get("mergeable", "MERGEABLE"),
			"autoMergeRequest": ({"enabledAt": meta["auto_at"]} if meta.get("auto_at") else None),
			"commits": {"nodes": [{"commit": {"oid": pr["head"]["sha"], "committedDate": "2026-11-01T00:00:00Z", "status": status}}]}}
		nodes.append(node)
	out({"data": {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}})
endpoint = next((a for a in args[1:] if a.startswith("repos/")), "")
if "-X" in args and args[args.index("-X") + 1] == "POST" and "/statuses/" in endpoint:
	with open(root / "statuses.log", "a") as log:
		log.write(json.dumps(args) + "\n")
	if (root / "status_post_fails").exists():
		sys.exit(1)
	out({})
if "--jq" in args and args[args.index("--jq") + 1] == ".default_branch":
	out("main\n")
if endpoint.endswith("/pulls"):
	out(fixture("pulls.json", []))
if "/rules/branches/" in endpoint:
	out([{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ci/final"}]}}])
if endpoint.endswith("/status?per_page=100"):
	out({"statuses": []})
if "/check-runs" in endpoint:
	out([{"check_runs": []}])
if "/git/matching-refs/" in endpoint:
	out([{"ref": "refs/merge-lease/main", "object": {"sha": "c" * 40}}])
if "/git/commits/" in endpoint:
	out({"tree": {"sha": "f" * 40}, "message": "merge-lease v1\nstate=held\nbase=main\npr=77\nhead=" + "b" * 40 + "\nexpires=9999999999\nupdated=1\npaths:\nx.py"})
if endpoint.endswith("/actions/artifacts"):
	out("[]")
if "/actions/workflows/ci.yml/runs?head_sha=" in endpoint:
	sha = endpoint.split("head_sha=", 1)[1].split("&", 1)[0]
	out(fixture("final_ci.json", {}).get(sha, {"workflow_runs": []}))
if "/actions/workflows/" in endpoint or "/actions/runs" in endpoint:
	out({"total_count": 0, "workflow_runs": []})
sys.stderr.write("unexpected gh call: " + " ".join(args) + "\n")
sys.exit(1)
'''

FAKE_CURL = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_GH_DIR}/telegram.log"
if [ -f "${FAKE_GH_DIR}/telegram_fails" ]; then
	printf '%s' '{"ok":false,"description":"Bad Request"}'
	exit 0
fi
printf '%s' '{"ok":true,"result":{"message_id":42}}'
'''


def _extract_step_script() -> str:
	text = SWEEP_WF.read_text(encoding="utf-8")
	start = text.find(STEP_MARKER)
	assert start != -1, "could not locate the sweep step"
	run_at = text.find(RUN_HEADER, start)
	assert run_at != -1
	lines: list[str] = []
	for line in text[run_at + len(RUN_HEADER):].splitlines():
		if line.startswith(RUN_INDENT):
			lines.append(line[len(RUN_INDENT):])
		elif line.strip() == "":
			lines.append("")
		else:
			assert BLOCK_END_RE.match(line), f"run body ended on an unexpected line: {line!r}"
			break
	return "\n".join(lines).rstrip("\n") + "\n"


def _sha(n: int) -> str:
	return f"{n:040x}"


def _pr(number: int, labels: tuple[str, ...] = ()) -> dict:
	return {
		"number": number, "draft": False, "title": f"PR {number}", "body": "",
		"created_at": "2026-11-20T00:00:00Z", "user": {"login": "someone"},
		"head": {"ref": f"feature/{number}", "sha": _sha(number)}, "base": {"ref": "main"},
		"labels": [{"name": name} for name in labels],
	}


def _iso(hours_ago: float) -> str:
	return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours_ago * 3600))


def _run(tmp_path: Path, prs: list[dict], meta: dict[int, dict] | None = None, *, flags: tuple[str, ...] = (),
		final_ci: dict | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	for name, body in (("gh", FAKE_GH), ("curl", FAKE_CURL)):
		path = bin_dir / name
		path.write_text(body, encoding="utf-8")
		path.chmod(0o755)
	(tmp_path / "pulls.json").write_text(json.dumps(prs), encoding="utf-8")
	(tmp_path / "meta.json").write_text(json.dumps({str(k): v for k, v in (meta or {}).items()}), encoding="utf-8")
	(tmp_path / "final_ci.json").write_text(json.dumps(final_ci or {}), encoding="utf-8")
	for name in ("graphql_fails", "telegram_fails", "status_post_fails"):
		(tmp_path / name).unlink(missing_ok=True)
	for flag in flags:
		(tmp_path / flag).write_text("", encoding="utf-8")
	support = tmp_path / ".ci-rerun-support"
	if not support.exists():
		support.mkdir()
		(support / "scripts").symlink_to(SCRIPTS)
		(tmp_path / "scripts").symlink_to(SCRIPTS)
	full_env = {
		"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GH_DIR": str(tmp_path), "RUNNER_TEMP": str(tmp_path),
		"TMPDIR": str(tmp_path), "REPOSITORY": "o/r", "GH_TOKEN": "x", "READ_TOKEN": "x", "HEAD_REF_FILTER": "",
		"DRY_RUN": "false", "ALLOW_WORKFLOW_EDITS": "true", "SWEEP_STALE_QUEUED_MINUTES": "120",
		"GH_RETRY_MAX_ATTEMPTS": "1", "GH_API_RETRY_MAX_ATTEMPTS": "1",
	}
	full_env.update(env or {})
	return subprocess.run(["bash", "-c", _extract_step_script()], env=full_env, capture_output=True, text=True,
		cwd=tmp_path, check=False, timeout=240)


def _lines(tmp_path: Path, name: str) -> list[str]:
	path = tmp_path / name
	return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _dispatched(tmp_path: Path) -> list[str]:
	return [line.split("pr_number=")[1].split()[0] for line in _lines(tmp_path, "dispatches.log")]


def _sends(tmp_path: Path) -> int:
	return sum(1 for line in _lines(tmp_path, "telegram.log") if "sendMessage" in line)


def _reset_logs(tmp_path: Path) -> None:
	for name in ("calls.log", "dispatches.log", "statuses.log", "telegram.log"):
		(tmp_path / name).unlink(missing_ok=True)


TG = {"TG_BOT_SECRET": "secret", "TG_CHAT_ID": "1", "ALERT_MSG_LEVEL": "WARNING"}


def test_approved_head_with_auto_merge_is_not_dispatched(tmp_path: Path) -> None:
	result = _run(tmp_path, [_pr(1), _pr(2)], {1: {"gate": "SUCCESS", "auto_at": _iso(1)}, 2: {"gate": "SUCCESS"}})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["2"]
	assert f"VERDICT_WAIT_SKIP source=sweep pr=1 head={_sha(1)} reason=auto_merge_waiting" in result.stdout
	assert "AUTOFIX_SWEEP_SKIP pr=#1 reason=verdict_wait detail=auto_merge_waiting head_ref=feature/1" in result.stdout
	assert "skipped_verdict_wait=1" in result.stdout
	# The approved head's one final-CI runs read still runs, so a failed final
	# CI keeps its own skip (no extra read: the head was read before as well).
	assert sum(1 for line in _lines(tmp_path, "calls.log") if f"head_sha={_sha(1)}" in line) == 1


def _failed_final_ci(n: int) -> dict:
	return {_sha(n): {"workflow_runs": [{"path": ".github/workflows/ci.yml", "head_sha": _sha(n), "status": "completed",
		"conclusion": "failure", "display_title": f"CI [pr:{n}]"}]}}


def test_auto_merge_head_with_failed_final_ci_keeps_its_own_skip(tmp_path: Path) -> None:
	result = _run(tmp_path, [_pr(1)], {1: {"gate": "SUCCESS", "auto_at": _iso(1)}}, final_ci=_failed_final_ci(1))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert f"AUTOFIX_SWEEP_SKIP pr=#1 reason=approved_head_final_ci_failed head_ref=feature/1 head_sha={_sha(1)}" in result.stdout
	assert "skipped_approved_final_ci_failed=1" in result.stdout
	assert "VERDICT_WAIT_SKIP" not in result.stdout


def test_failed_final_ci_switch_off_re_reviews_the_auto_merge_head(tmp_path: Path) -> None:
	result = _run(tmp_path, [_pr(1)], {1: {"gate": "SUCCESS", "auto_at": _iso(1)}}, final_ci=_failed_final_ci(1),
		env={"SWEEP_SKIP_APPROVED_FINAL_CI_FAILED": "false"})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["1"]
	assert "VERDICT_WAIT_SKIP" not in result.stdout


def test_final_ci_in_flight_skip_does_not_depend_on_the_failed_ci_switch(tmp_path: Path) -> None:
	runs = {_sha(1): {"workflow_runs": [{"path": ".github/workflows/ci.yml", "head_sha": _sha(1), "status": "in_progress",
		"conclusion": None, "display_title": "CI [pr:1]"}]}}
	result = _run(tmp_path, [_pr(1)], {1: {"gate": "SUCCESS"}}, final_ci=runs,
		env={"SWEEP_SKIP_APPROVED_FINAL_CI_FAILED": "false"})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "reason=verdict_wait detail=final_ci_in_flight" in result.stdout


def test_final_ci_merge_pending_label_is_not_dispatched(tmp_path: Path) -> None:
	result = _run(tmp_path, [_pr(1, ("ai:final-ci-merge-pending",))], {1: {"gate": "SUCCESS"}})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "reason=verdict_wait detail=final_ci_merge_pending" in result.stdout


def test_final_ci_in_flight_is_not_dispatched(tmp_path: Path) -> None:
	runs = {_sha(1): {"workflow_runs": [{"path": ".github/workflows/ci.yml", "head_sha": _sha(1), "status": "in_progress",
		"conclusion": None, "display_title": "CI [pr:1]"}]}}
	result = _run(tmp_path, [_pr(1)], {1: {"gate": "SUCCESS"}}, final_ci=runs)
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "reason=verdict_wait detail=final_ci_in_flight" in result.stdout


@pytest.mark.parametrize("mergeable", ["CONFLICTING", "UNKNOWN", None])
def test_final_ci_in_flight_on_a_conflicted_head_is_dispatched(tmp_path: Path, mergeable: str | None) -> None:
	runs = {_sha(1): {"workflow_runs": [{"path": ".github/workflows/ci.yml", "head_sha": _sha(1), "status": "in_progress",
		"conclusion": None, "display_title": "CI [pr:1]"}]}}
	result = _run(tmp_path, [_pr(1)], {1: {"gate": "SUCCESS", "mergeable": mergeable}}, final_ci=runs)
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["1"]
	assert "VERDICT_WAIT_SKIP" not in result.stdout


def test_final_ci_merge_pending_label_is_swept_with_the_train_off(tmp_path: Path) -> None:
	"""Only the merge train removes ai:final-ci-merge-pending; with it off the sweep is the fallback."""
	result = _run(tmp_path, [_pr(1, ("ai:final-ci-merge-pending",))], {1: {"gate": "SUCCESS"}},
		env={"MERGE_TRAIN_ENABLED": "false"})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["1"]
	assert "VERDICT_WAIT_SKIP" not in result.stdout


def test_lease_waiter_is_skipped_and_logged(tmp_path: Path) -> None:
	result = _run(tmp_path, [_pr(1, ("ai:merge-lease-wait",))], {1: {"gate": "SUCCESS"}})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "AUTOFIX_SWEEP_SKIP pr=#1 reason=merge_lease_wait head_ref=feature/1" in result.stdout


@pytest.mark.parametrize("meta", [
	{"gate": "SUCCESS", "auto_at": "2026-11-01T00:00:00Z", "mergeable": "CONFLICTING"},
	{"gate": "SUCCESS", "auto_at": "2026-11-01T00:00:00Z", "mergeable": "UNKNOWN"},
	{"gate": "SUCCESS", "auto_at": "2026-11-01T00:00:00Z", "mergeable": None},
	{"gate": "PENDING", "auto_at": "2026-11-01T00:00:00Z"},
	{},
])
def test_conflicted_or_unapproved_heads_are_dispatched(tmp_path: Path, meta: dict) -> None:
	result = _run(tmp_path, [_pr(1)], {1: meta})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["1"]
	assert "VERDICT_WAIT_SKIP" not in result.stdout


@pytest.mark.parametrize("value", ["false", "0", "off", "no"])
def test_switch_off_restores_the_dispatch(tmp_path: Path, value: str) -> None:
	result = _run(tmp_path, [_pr(1)], {1: {"gate": "SUCCESS", "auto_at": _iso(10)}},
		env={"VERDICT_WAIT_DISPATCH_SKIP_ENABLED": value, **TG})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["1"]
	assert "VERDICT_WAIT" not in result.stdout
	assert not _lines(tmp_path, "telegram.log")


def test_failed_state_read_dispatches_as_before(tmp_path: Path) -> None:
	result = _run(tmp_path, [_pr(1)], {1: {"gate": "SUCCESS", "auto_at": _iso(10)}}, flags=("graphql_fails",), env=TG)
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["1"]
	assert not _lines(tmp_path, "telegram.log") and not _lines(tmp_path, "statuses.log")


def test_alert_once_after_the_wait_time_names_the_missing_check(tmp_path: Path) -> None:
	meta = {1: {"gate": "SUCCESS", "gate_at": _iso(8), "auto_at": _iso(7)}}
	result = _run(tmp_path, [_pr(1)], meta, env=TG)
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert re.search(rf"VERDICT_WAIT_ALERT pr=1 head={_sha(1)} waited_hours=8 threshold_hours=6 blocker=ci/final \(missing\) outcome=sent", result.stdout), result.stdout
	assert _sends(tmp_path) == 1
	posts = [json.loads(line) for line in _lines(tmp_path, "statuses.log")]
	assert len(posts) == 1 and f"context={ALERT_CTX}" in posts[0] and f"repos/o/r/statuses/{_sha(1)}" in posts[0]
	# Next tick: the marker status is read back by the same query; no second alert.
	_reset_logs(tmp_path)
	meta[1]["alerted"] = True
	result = _run(tmp_path, [_pr(1)], meta, env=TG)
	assert result.returncode == 0, result.stderr
	assert "VERDICT_WAIT_ALERT" not in result.stdout
	assert not _lines(tmp_path, "telegram.log") and not _lines(tmp_path, "statuses.log")
	assert _dispatched(tmp_path) == []


def test_no_alert_before_the_wait_time_or_when_off(tmp_path: Path) -> None:
	meta = {1: {"gate": "SUCCESS", "gate_at": _iso(2), "auto_at": _iso(1)}}
	result = _run(tmp_path, [_pr(1)], meta, env=TG)
	assert result.returncode == 0, result.stderr
	assert "VERDICT_WAIT_ALERT" not in result.stdout and not _lines(tmp_path, "telegram.log")
	_reset_logs(tmp_path)
	meta = {1: {"gate": "SUCCESS", "gate_at": _iso(20), "auto_at": _iso(20)}}
	result = _run(tmp_path, [_pr(1)], meta, env={**TG, "VERDICT_WAIT_ALERT_HOURS": "0"})
	assert "VERDICT_WAIT_ALERT" not in result.stdout and not _lines(tmp_path, "telegram.log")


def test_failed_send_posts_no_marker(tmp_path: Path) -> None:
	meta = {1: {"gate": "SUCCESS", "gate_at": _iso(8), "auto_at": _iso(8)}}
	result = _run(tmp_path, [_pr(1)], meta, flags=("telegram_fails",), env=TG)
	assert result.returncode == 0, result.stderr
	assert "outcome=not_delivered" in result.stdout
	assert not _lines(tmp_path, "statuses.log")


def test_failed_marker_post_is_logged_and_retried_next_tick(tmp_path: Path) -> None:
	"""Send first, then mark: a lost marker can repeat the alert, never lose it."""
	meta = {1: {"gate": "SUCCESS", "gate_at": _iso(8), "auto_at": _iso(8)}}
	result = _run(tmp_path, [_pr(1)], meta, flags=("status_post_fails",), env=TG)
	assert result.returncode == 0, result.stderr
	assert "outcome=marker_failed" in result.stdout
	assert _sends(tmp_path) == 1
	# gh_retry is loaded here (GH_RETRY_MAX_ATTEMPTS=1), so one POST attempt.
	assert len(_lines(tmp_path, "statuses.log")) >= 1
	assert _dispatched(tmp_path) == []


def test_unconfigured_telegram_posts_no_marker(tmp_path: Path) -> None:
	meta = {1: {"gate": "SUCCESS", "gate_at": _iso(8), "auto_at": _iso(8)}}
	result = _run(tmp_path, [_pr(1)], meta)
	assert result.returncode == 0, result.stderr
	assert "blocker=not_read outcome=telegram_unconfigured" in result.stdout
	assert not _lines(tmp_path, "statuses.log")
	# No sender, no blocker reads.
	assert not any("/rules/branches/" in line for line in _lines(tmp_path, "calls.log"))


def test_lease_waiter_alert_names_the_lease_holder(tmp_path: Path) -> None:
	meta = {1: {"judge": "SUCCESS", "judge_at": _iso(9)}, 2: {"judge": "SUCCESS", "judge_at": _iso(9)}}
	prs = [_pr(1, ("ai:merge-lease-wait",)), _pr(2, ("ai:merge-lease-wait",))]
	result = _run(tmp_path, prs, meta, env=TG)
	assert result.returncode == 0, result.stderr
	assert "blocker=merge lease held by PR #77 outcome=sent" in result.stdout, result.stdout
	assert result.stdout.count("VERDICT_WAIT_ALERT ") == 2
	# One lease read per base per tick.
	assert sum(1 for line in _lines(tmp_path, "calls.log") if "git/matching-refs" in line) == 1


def test_dry_run_sends_and_posts_nothing(tmp_path: Path) -> None:
	meta = {1: {"gate": "SUCCESS", "gate_at": _iso(8), "auto_at": _iso(8)}}
	result = _run(tmp_path, [_pr(1)], meta, env={**TG, "DRY_RUN": "true"})
	assert result.returncode == 0, result.stderr
	assert "outcome=dry_run" in result.stdout
	assert not _lines(tmp_path, "telegram.log") and not _lines(tmp_path, "statuses.log")


def test_skip_adds_no_api_call(tmp_path: Path) -> None:
	"""A verdict-wait skip costs no more calls than dispatching the same PR."""
	meta_on = {1: {"gate": "SUCCESS", "gate_at": _iso(1), "auto_at": _iso(1)}}
	result = _run(tmp_path, [_pr(1)], meta_on)
	assert result.returncode == 0, result.stderr
	skipped_calls = len(_lines(tmp_path, "calls.log"))
	_reset_logs(tmp_path)
	result = _run(tmp_path, [_pr(1)], meta_on, env={"VERDICT_WAIT_DISPATCH_SKIP_ENABLED": "false"})
	assert result.returncode == 0, result.stderr
	baseline_calls = len(_lines(tmp_path, "calls.log"))
	assert skipped_calls < baseline_calls
	assert sum(1 for line in _lines(tmp_path, "calls.log") if '"graphql"' in line) == 1


def test_workflow_wires_the_switches_with_defaults() -> None:
	text = SWEEP_WF.read_text(encoding="utf-8")
	assert "VERDICT_WAIT_DISPATCH_SKIP_ENABLED: ${{ vars.VERDICT_WAIT_DISPATCH_SKIP_ENABLED || 'true' }}" in text
	assert "VERDICT_WAIT_ALERT_HOURS: ${{ vars.VERDICT_WAIT_ALERT_HOURS || '6' }}" in text
	assert "skipped_verdict_wait=${skipped_verdict_wait}\"" in _extract_step_script()
