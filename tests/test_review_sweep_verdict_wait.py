#!/usr/bin/env python3
"""The review sweep skips heads that already have a verdict and only wait to merge.

Issue #9991: on 2026-11-23 the sweep dispatched 181 re-reviews across 47 PRs
in seven hours. #9984 (an approved head, auto-merge enabled, a required check
missing) got 12 identical review runs; judge `merge` verdicts deferred by a
busy lease were re-run up to 21 times. Those repeat runs also counted as
review rounds and got waiting heads labelled `ai:needs-human`.

The cases run the sweep step's shell as extracted from the shipped workflow
against a fake `gh`, with the default-branch helpers staged where the
workflow sources them (`.ci-rerun-support/scripts`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
SCRIPTS = REPO_ROOT / "scripts"
STEP_MARKER = "- name: Enumerate open PRs and dispatch internal-review.yml"
RUN_HEADER = "        run: |\n"
RUN_INDENT = " " * 10
BLOCK_END_RE = re.compile(r"^(?: {0,9}#| {0,6}- | {0,8}[A-Za-z_][\w-]*:(?:\s|$))")
WF_PATH = ".github/workflows/internal-review.yml"
BASE = "b" * 40

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
root = Path(os.environ["FAKE_GH_DIR"])
# Default status times are recent, so no alert fires unless a test asks.
RECENT = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
	meta = fixture("meta.json", {})
	nodes = []
	for pr in fixture("pulls.json", []):
		m = meta.get(str(pr["number"]), {})
		status = {"context": {"state": m.get("gate", "PENDING"), "createdAt": m.get("gate_at", RECENT)}}
		if m.get("verdict"):
			status["verdict"] = {"state": "SUCCESS", "description": m["verdict"], "createdAt": m.get("verdict_at", RECENT)}
		if m.get("alerted"):
			status["mwalert"] = {"state": "SUCCESS"}
		nodes.append({"number": pr["number"], "headRefOid": pr["head"]["sha"],
			"baseRefOid": m.get("base", "b" * 40), "baseRefName": "main",
			"mergeStateStatus": m.get("ms", "BLOCKED"),
			"autoMergeRequest": {"enabledAt": "2026-11-23T03:05:00Z"} if m.get("auto") else None,
			"commits": {"nodes": [{"commit": {"oid": pr["head"]["sha"], "committedDate": "2026-11-01T00:00:00Z",
				"statusCheckRollup": {"state": m["rollup"]} if m.get("rollup") else None, "status": status}}]}})
	out({"data": {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}})
endpoint = next((a for a in args[1:] if a.startswith("repos/")), "")
if "/statuses/" in endpoint and "POST" in args:
	with open(root / "statuses.log", "a") as log:
		log.write(json.dumps(args) + "\n")
	out({})
if endpoint.endswith("/labels"):
	with open(root / "labels.log", "a") as log:
		log.write(" ".join(args) + "\n")
	out({})
if endpoint.endswith("/comments"):
	with open(root / "comments.log", "a") as log:
		log.write(json.dumps(args) + "\n")
	out({})
if "--jq" in args and args[args.index("--jq") + 1] == ".default_branch":
	out("main\n")
if endpoint.endswith("/pulls"):
	out(fixture("pulls.json", []))
if "/compare/" in endpoint:
	out({"ahead_by": 0, "commits": [], "files": []})
if "/rules/branches/" in endpoint:
	out([{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ci/final"}]}}])
if endpoint.split("?")[0].endswith("/status"):
	out({"statuses": []})
if "/check-runs" in endpoint:
	out([{"total_count": 0, "check_runs": []}] if "--slurp" in args else {"total_count": 0, "check_runs": []})
if "/actions/workflows/ci.yml/runs" in endpoint:
	sha = endpoint.split("head_sha=")[1].split("&")[0]
	runs = fixture("final_ci.json", {}).get(sha, [])
	out({"total_count": len(runs), "workflow_runs": runs})
if "/actions/workflows/" in endpoint and endpoint.endswith("/runs"):
	if "event=workflow_dispatch" in args:
		out(fixture("rounds.json", {"total_count": 0, "workflow_runs": []}))
	out({"total_count": 0, "workflow_runs": []})
sys.stderr.write("unexpected gh call: " + " ".join(args) + "\n")
sys.exit(1)
'''

# Test-only Telegram helpers: tg_send_msg records the message; the retry
# wrappers the ruleset blocker helper uses are plain pass-throughs.
FAKE_TG = r'''
_tg_should_send() { return 0; }
tg_send_msg() { printf '%s\n---\n' "$1" >> "${FAKE_GH_DIR}/telegram.log"; }
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
type _safe_gh_jq >/dev/null 2>&1 || _safe_gh_jq() { gh api "$@"; }
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


def _sha(number: int) -> str:
	return f"{number:040x}"


def _pr(number: int, labels: tuple[str, ...] = ()) -> dict:
	return {
		"number": number,
		"draft": False,
		"title": f"PR {number}",
		"body": "",
		"created_at": "2026-11-23T03:00:00Z",
		"user": {"login": "someone"},
		"head": {"ref": f"feature-{number}", "sha": _sha(number)},
		"labels": [{"name": name} for name in labels],
	}


def _ci_run(number: int, status: str = "completed", conclusion: str | None = "success") -> dict:
	return {"id": 1, "path": ".github/workflows/ci.yml", "head_sha": _sha(number), "status": status,
		"conclusion": conclusion, "display_title": f"CI [pr:{number}]"}


def _rounds(**per_pr: int) -> dict:
	runs = []
	for pr, count in per_pr.items():
		for i in range(count):
			runs.append({"id": 44000000000 + int(pr) * 100 + i, "event": "workflow_dispatch", "status": "completed",
				"conclusion": "success", "head_branch": "main", "path": WF_PATH,
				"display_title": f"Internal: AI Review & Autofix [pr:{pr}]", "created_at": "2026-11-23T05:00:00Z"})
	return {"total_count": len(runs), "workflow_runs": runs}


def _verdict(wait: str = "blocked", ms: str = "blocked", base: str = BASE) -> str:
	return f"decision=merge wait={wait} base={base} ms={ms}"


def _hours_ago(hours: float) -> str:
	return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_sweep(tmp_path: Path, prs: list[dict], meta: dict, *, final_ci: dict | None = None, rounds: dict | None = None,
		flags: tuple[str, ...] = (), env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	bin_dir = tmp_path / "bin"
	if not bin_dir.exists():
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(FAKE_GH, encoding="utf-8")
		gh.chmod(0o755)
		support = tmp_path / ".ci-rerun-support" / "scripts"
		support.mkdir(parents=True)
		for name in ("review_sweep_head_gate.sh", "review_sweep_round_cap.sh", "pr_checks_lib.sh"):
			shutil.copy(SCRIPTS / name, support / name)
		(support / "tg_helpers.sh").write_text(FAKE_TG, encoding="utf-8")
	(tmp_path / "pulls.json").write_text(json.dumps(prs), encoding="utf-8")
	(tmp_path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
	(tmp_path / "final_ci.json").write_text(json.dumps(final_ci or {}), encoding="utf-8")
	(tmp_path / "rounds.json").write_text(json.dumps(rounds or _rounds()), encoding="utf-8")
	for flag in flags:
		(tmp_path / flag).write_text("", encoding="utf-8")
	full_env = {
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"FAKE_GH_DIR": str(tmp_path),
		"RUNNER_TEMP": str(tmp_path),
		"REPOSITORY": "o/r",
		"GH_TOKEN": "x",
		"READ_TOKEN": "x",
		"HEAD_REF_FILTER": "",
		"DRY_RUN": "false",
		"ALLOW_WORKFLOW_EDITS": "true",
		"SWEEP_STALE_QUEUED_MINUTES": "120",
		"TG_BOT_SECRET": "secret",
		"TG_CHAT_ID": "1",
	}
	full_env.update(env or {})
	return subprocess.run(["bash", "-c", _extract_step_script()], env=full_env, capture_output=True, text=True,
		cwd=tmp_path, check=False)


def _lines(tmp_path: Path, name: str) -> list[str]:
	path = tmp_path / name
	return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _dispatched(tmp_path: Path) -> list[str]:
	return [line.split("pr_number=")[1].split()[0] for line in _lines(tmp_path, "dispatches.log")]


def _api_calls(tmp_path: Path) -> list[list[str]]:
	return [json.loads(line) for line in _lines(tmp_path, "calls.log") if json.loads(line)[:1] == ["api"]]


def _reset(tmp_path: Path) -> None:
	for name in ("calls.log", "dispatches.log", "statuses.log", "labels.log", "comments.log", "telegram.log"):
		(tmp_path / name).unlink(missing_ok=True)


def test_approved_head_waiting_for_the_merge_lease_is_not_dispatched(tmp_path: Path) -> None:
	result = _run_sweep(tmp_path, [_pr(10, ("ai:merge-lease-wait",))], {"10": {"gate": "SUCCESS"}})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "AUTOFIX_SWEEP_SKIP pr=#10 reason=merge_lease_wait" in result.stdout


def test_final_ci_merge_pending_head_is_skipped_without_a_runs_read(tmp_path: Path) -> None:
	result = _run_sweep(tmp_path, [_pr(11, ("ai:final-ci-merge-pending",))], {"11": {"gate": "SUCCESS", "auto": True}})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "AUTOFIX_SWEEP_SKIP pr=#11 reason=verdict_waiting wait=final_ci_merge_pending" in result.stdout
	assert not any("ci.yml/runs" in " ".join(call) for call in _api_calls(tmp_path))
	assert "skipped_verdict_waiting=1" in result.stdout


@pytest.mark.parametrize("run, wait", [
	(_ci_run(12), "auto_merge_checks_missing"),
	(_ci_run(12, status="in_progress", conclusion=None), "auto_merge_checks_pending"),
])
def test_auto_merge_head_with_pending_or_missing_checks_is_skipped(tmp_path: Path, run: dict, wait: str) -> None:
	result = _run_sweep(tmp_path, [_pr(12)], {"12": {"gate": "SUCCESS", "auto": True}}, final_ci={_sha(12): [run]})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert f"reason=verdict_waiting wait={wait} head_ref=feature-12 head_sha={_sha(12)}" in result.stdout
	# One final-CI runs read for the head, shared with the failed-head skip.
	assert sum("ci.yml/runs" in " ".join(call) for call in _api_calls(tmp_path)) == 1


def test_failed_final_ci_keeps_the_existing_skip(tmp_path: Path) -> None:
	result = _run_sweep(tmp_path, [_pr(13)], {"13": {"gate": "SUCCESS", "auto": True}},
		final_ci={_sha(13): [_ci_run(13, conclusion="failure")]})
	assert result.returncode == 0, result.stderr
	assert "reason=approved_head_final_ci_failed" in result.stdout
	assert "reason=verdict_waiting" not in result.stdout
	assert sum("ci.yml/runs" in " ".join(call) for call in _api_calls(tmp_path)) == 1


def test_old_verdict_alerts_once_per_head(tmp_path: Path) -> None:
	meta = {"14": {"gate": "SUCCESS", "auto": True, "gate_at": _hours_ago(7)}}
	result = _run_sweep(tmp_path, [_pr(14)], meta, final_ci={_sha(14): [_ci_run(14)]})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert re.search(r"VERDICT_MERGE_WAIT_ALERT pr=#14 head_sha=\w+ age_hours=7 reason=auto_merge_checks_missing blocker=ci/final_\(missing\) outcome=sent", result.stdout), result.stdout
	posts = [json.loads(line) for line in _lines(tmp_path, "statuses.log")]
	assert len(posts) == 1
	assert "context=ai-review/merge-wait-alert" in posts[0]
	assert f"repos/o/r/statuses/{_sha(14)}" in posts[0]
	telegram = (tmp_path / "telegram.log").read_text(encoding="utf-8")
	assert telegram.count("---") == 1 and "ci/final (missing)" in telegram
	# Next tick: the marker status is on the head, so no second alert.
	_reset(tmp_path)
	meta["14"]["alerted"] = True
	result = _run_sweep(tmp_path, [_pr(14)], meta, final_ci={_sha(14): [_ci_run(14)]})
	assert result.returncode == 0, result.stderr
	assert "reason=verdict_waiting" in result.stdout
	assert "VERDICT_MERGE_WAIT_ALERT" not in result.stdout
	assert _lines(tmp_path, "statuses.log") == [] and _lines(tmp_path, "telegram.log") == []


def test_recent_verdict_does_not_alert_and_zero_hours_disables(tmp_path: Path) -> None:
	meta = {"15": {"gate": "SUCCESS", "auto": True, "gate_at": _hours_ago(1)}}
	result = _run_sweep(tmp_path, [_pr(15)], meta, final_ci={_sha(15): [_ci_run(15)]})
	assert "VERDICT_MERGE_WAIT_ALERT" not in result.stdout
	_reset(tmp_path)
	meta["15"]["gate_at"] = _hours_ago(30)
	result = _run_sweep(tmp_path, [_pr(15)], meta, final_ci={_sha(15): [_ci_run(15)]}, env={"VERDICT_MERGE_WAIT_ALERT_HOURS": "0"})
	assert "VERDICT_MERGE_WAIT_ALERT" not in result.stdout
	assert _lines(tmp_path, "statuses.log") == []


def test_judge_verdict_head_is_skipped_and_not_capped(tmp_path: Path) -> None:
	meta = {"16": {"gate": "PENDING", "verdict": _verdict(), "ms": "BLOCKED"}}
	result = _run_sweep(tmp_path, [_pr(16)], meta, rounds=_rounds(**{"16": 40}))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "reason=verdict_waiting wait=rb_judge_merge_deferred" in result.stdout
	assert "reason=review_round_cap" not in result.stdout
	assert _lines(tmp_path, "labels.log") == []


@pytest.mark.parametrize("change", [{"ms": "CLEAN"}, {"verdict": "decision=fix wait=blocked base=" + BASE + " ms=blocked"}])
def test_judge_verdict_with_a_relevant_change_is_dispatched(tmp_path: Path, change: dict) -> None:
	meta = {"17": {"gate": "PENDING", "verdict": _verdict(), "ms": "BLOCKED", **change}}
	result = _run_sweep(tmp_path, [_pr(17)], meta)
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["17"]
	assert "reason=verdict_waiting" not in result.stdout


def test_head_that_is_not_approved_is_dispatched_and_capped_as_before(tmp_path: Path) -> None:
	result = _run_sweep(tmp_path, [_pr(18), _pr(19)], {"18": {"gate": "PENDING"}, "19": {"gate": "PENDING"}},
		rounds=_rounds(**{"19": 40}))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["18"]
	assert "AUTOFIX_SWEEP_SKIP pr=#19 reason=review_round_cap" in result.stdout
	assert any("ai:needs-human" in line for line in _lines(tmp_path, "labels.log"))


def test_switch_off_restores_the_dispatches(tmp_path: Path) -> None:
	prs = [_pr(20), _pr(21)]
	meta = {"20": {"gate": "SUCCESS", "auto": True}, "21": {"gate": "PENDING", "verdict": _verdict(), "ms": "BLOCKED"}}
	result = _run_sweep(tmp_path, prs, meta, final_ci={_sha(20): [_ci_run(20)]}, rounds=_rounds(**{"21": 40}),
		env={"VERDICT_WAIT_DISPATCH_SKIP_ENABLED": "false"})
	assert result.returncode == 0, result.stderr
	assert "reason=verdict_waiting" not in result.stdout
	assert _dispatched(tmp_path) == ["20"]
	# With the switch off the judge-verdict head is capped as before.
	assert "AUTOFIX_SWEEP_SKIP pr=#21 reason=review_round_cap" in result.stdout


def test_failed_annotation_dispatches_as_before(tmp_path: Path) -> None:
	meta = {"22": {"gate": "SUCCESS", "auto": True}, "23": {"gate": "PENDING", "verdict": _verdict()}}
	result = _run_sweep(tmp_path, [_pr(22), _pr(23)], meta, final_ci={_sha(22): [_ci_run(22)]}, flags=("graphql_fails",))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["22", "23"]
	assert "reason=verdict_waiting" not in result.stdout


def test_api_calls_per_tick_do_not_grow(tmp_path: Path) -> None:
	prs = [_pr(24), _pr(25), _pr(26)]
	meta = {"24": {"gate": "SUCCESS", "auto": True}, "25": {"gate": "PENDING", "verdict": _verdict()}, "26": {"gate": "PENDING"}}
	final_ci = {_sha(24): [_ci_run(24)]}
	result = _run_sweep(tmp_path, prs, meta, final_ci=final_ci, env={"VERDICT_WAIT_DISPATCH_SKIP_ENABLED": "false"})
	assert result.returncode == 0, result.stderr
	baseline = len(_api_calls(tmp_path))
	_reset(tmp_path)
	result = _run_sweep(tmp_path, prs, meta, final_ci=final_ci)
	assert result.returncode == 0, result.stderr
	assert len(_api_calls(tmp_path)) <= baseline
	assert "skipped_verdict_waiting=2" in result.stdout


def test_workflow_passes_the_knobs_with_defaults() -> None:
	text = SWEEP_WF.read_text(encoding="utf-8")
	assert "VERDICT_WAIT_DISPATCH_SKIP_ENABLED: ${{ vars.VERDICT_WAIT_DISPATCH_SKIP_ENABLED || 'true' }}" in text
	assert "VERDICT_MERGE_WAIT_ALERT_HOURS: ${{ vars.VERDICT_MERGE_WAIT_ALERT_HOURS || '6' }}" in text
	step = _extract_step_script()
	# The skip sits after the active-run preflight and before the round cap.
	assert step.find("reason=active_run") < step.find("reason=verdict_waiting") < step.find("reason=review_round_cap")


def test_gate_success_head_with_a_blocked_judge_verdict_is_skipped(tmp_path: Path) -> None:
	# The judge posts head-gate success before its merge attempt, so a
	# #9939 blocked-merge verdict head has gate success and no auto-merge.
	meta = {"27": {"gate": "SUCCESS", "verdict": _verdict(), "ms": "BLOCKED"}}
	result = _run_sweep(tmp_path, [_pr(27)], meta)
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "AUTOFIX_SWEEP_SKIP pr=#27 reason=verdict_waiting wait=rb_judge_merge_deferred" in result.stdout


def test_failed_check_rollup_is_a_relevant_change(tmp_path: Path) -> None:
	# `blocked` covers pending and failed required checks alike.
	meta = {"28": {"gate": "SUCCESS", "verdict": _verdict(), "ms": "BLOCKED", "rollup": "FAILURE"}}
	result = _run_sweep(tmp_path, [_pr(28)], meta)
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["28"]
	_reset(tmp_path)
	meta["28"]["rollup"] = "PENDING"
	result = _run_sweep(tmp_path, [_pr(28)], meta)
	assert _dispatched(tmp_path) == []


def test_moved_base_is_read_once_then_recorded(tmp_path: Path) -> None:
	new_base = "c" * 40
	meta = {"29": {"gate": "SUCCESS", "verdict": _verdict(), "ms": "BLOCKED", "base": new_base}}
	result = _run_sweep(tmp_path, [_pr(29)], meta)
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert sum("/compare/" in " ".join(call) for call in _api_calls(tmp_path)) == 1
	posts = [json.loads(line) for line in _lines(tmp_path, "statuses.log")]
	assert len(posts) == 1 and "context=ai-review/rb-judge-verdict" in posts[0]
	desc = next(a for a in posts[0] if a.startswith("description="))
	# The re-post carries the first verdict time, so the alert age does not restart.
	assert re.fullmatch(rf"description=decision=merge wait=blocked base={new_base} ms=blocked at=\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ", desc), desc
	# Next tick reads the refreshed verdict: no freshness reads, no write.
	_reset(tmp_path)
	meta["29"]["verdict"] = _verdict(base=new_base)
	result = _run_sweep(tmp_path, [_pr(29)], meta)
	assert _dispatched(tmp_path) == []
	assert not any("/compare/" in " ".join(call) for call in _api_calls(tmp_path))
	assert _lines(tmp_path, "statuses.log") == []


def test_lease_waiting_head_alerts_once(tmp_path: Path) -> None:
	meta = {"30": {"gate": "SUCCESS", "gate_at": _hours_ago(7)}}
	result = _run_sweep(tmp_path, [_pr(30, ("ai:merge-lease-wait",))], meta)
	assert result.returncode == 0, result.stderr
	assert "AUTOFIX_SWEEP_SKIP pr=#30 reason=merge_lease_wait head_ref=feature-30" in result.stdout
	assert re.search(r"VERDICT_MERGE_WAIT_ALERT pr=#30 head_sha=\w+ age_hours=7 reason=merge_lease_wait blocker=\S+ outcome=sent", result.stdout), result.stdout
	assert len(_lines(tmp_path, "statuses.log")) == 1


def test_unconfigured_telegram_writes_no_marker(tmp_path: Path) -> None:
	meta = {"31": {"gate": "SUCCESS", "auto": True, "gate_at": _hours_ago(7)}}
	result = _run_sweep(tmp_path, [_pr(31)], meta, final_ci={_sha(31): [_ci_run(31)]}, env={"TG_BOT_SECRET": ""})
	assert result.returncode == 0, result.stderr
	assert "outcome=telegram_unconfigured" in result.stdout
	assert _lines(tmp_path, "statuses.log") == []
	assert not any("/rules/branches/" in " ".join(call) for call in _api_calls(tmp_path))


def test_verdict_head_whose_lease_was_granted_is_dispatched_not_capped(tmp_path: Path) -> None:
	# G4: a lease_wait verdict head keeps a pending gate (the judge posts gate
	# success only right before a merge request). Once the lease is granted
	# (label removed) its verdict changed; it is dispatched, never labelled.
	meta = {"32": {"gate": "PENDING", "verdict": _verdict(wait="lease_wait"), "ms": "BLOCKED"}}
	result = _run_sweep(tmp_path, [_pr(32)], meta, rounds=_rounds(**{"32": 40}))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["32"]
	assert "reason=review_round_cap" not in result.stdout
	assert _lines(tmp_path, "labels.log") == []


def test_verdict_head_with_a_failing_rollup_stays_capped(tmp_path: Path) -> None:
	# A failing check is a change on every tick; the round cap still bounds it.
	meta = {"33": {"gate": "PENDING", "verdict": _verdict(), "ms": "BLOCKED", "rollup": "FAILURE"}}
	result = _run_sweep(tmp_path, [_pr(33)], meta, rounds=_rounds(**{"33": 40}))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "AUTOFIX_SWEEP_SKIP pr=#33 reason=review_round_cap" in result.stdout


def test_fresh_verdict_does_not_alert_on_an_older_gate_status(tmp_path: Path) -> None:
	# The pending gate status predates the verdict (posted at push): the age
	# is the verdict's own, so a fresh verdict raises no alert and keeps the
	# once-per-head marker for a real stall.
	meta = {"34": {"gate": "PENDING", "gate_at": _hours_ago(7), "verdict": _verdict(), "ms": "BLOCKED"}}
	result = _run_sweep(tmp_path, [_pr(34)], meta)
	assert result.returncode == 0, result.stderr
	assert "reason=verdict_waiting wait=rb_judge_merge_deferred" in result.stdout
	assert "VERDICT_MERGE_WAIT_ALERT" not in result.stdout
	assert _lines(tmp_path, "statuses.log") == []


def test_refreshed_verdict_keeps_its_first_verdict_time(tmp_path: Path) -> None:
	# A base refresh re-posts the verdict (recent createdAt) with at=<first
	# verdict time>; the alert measures from that, so it still fires.
	meta = {"35": {"gate": "PENDING", "gate_at": _hours_ago(9), "verdict": _verdict() + " at=" + _hours_ago(7),
		"verdict_at": _hours_ago(0.2), "ms": "BLOCKED"}}
	result = _run_sweep(tmp_path, [_pr(35)], meta)
	assert result.returncode == 0, result.stderr
	assert "reason=verdict_waiting wait=rb_judge_merge_deferred" in result.stdout
	assert re.search(r"VERDICT_MERGE_WAIT_ALERT pr=#35 head_sha=\w+ age_hours=7 reason=rb_judge_merge_deferred blocker=\S+ outcome=sent", result.stdout), result.stdout


@pytest.mark.parametrize("change, capped", [
	# Unknown base answer (freshness off): fail-open dispatch, still capped.
	({"base": "c" * 40}, True),
	# Merge state moved somewhere that cannot merge now: still capped.
	({"ms": "DIRTY"}, True),
	# Merge state moved to mergeable: like a head-gate success, not capped.
	({"ms": "CLEAN"}, False),
])
def test_changed_verdict_round_cap_follows_the_change_reason(tmp_path: Path, change: dict, capped: bool) -> None:
	meta = {"36": {"gate": "PENDING", "verdict": _verdict(), "ms": "BLOCKED", **change}}
	result = _run_sweep(tmp_path, [_pr(36)], meta, rounds=_rounds(**{"36": 40}),
		env={"MERGE_BASE_FRESHNESS_ENABLED": "false"})
	assert result.returncode == 0, result.stderr
	assert "reason=verdict_waiting" not in result.stdout
	assert ("AUTOFIX_SWEEP_SKIP pr=#36 reason=review_round_cap" in result.stdout) is capped, result.stdout
	assert (_dispatched(tmp_path) == ["36"]) is (not capped)


def test_changed_verdict_exemption_covers_one_answering_dispatch(tmp_path: Path) -> None:
	# A changed verdict skips the round cap only until a counted round is
	# newer than the verdict status; a verdict nothing re-posts (a re-run that
	# ended merge_failed, or an unlabelled lease wait) is then capped again.
	meta = {"37": {"gate": "PENDING", "verdict": _verdict(wait="lease_wait"), "ms": "BLOCKED",
		"verdict_at": "2026-11-22T00:00:00Z"}}
	result = _run_sweep(tmp_path, [_pr(37)], meta, rounds=_rounds(**{"37": 40}))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "AUTOFIX_SWEEP_SKIP pr=#37 reason=review_round_cap" in result.stdout
	# The same verdict newer than every counted round is still exempt.
	_reset(tmp_path)
	meta["37"]["verdict_at"] = "2026-11-23T06:00:00Z"
	result = _run_sweep(tmp_path, [_pr(37)], meta, rounds=_rounds(**{"37": 40}))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["37"]
	assert "reason=review_round_cap" not in result.stdout
