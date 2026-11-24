#!/usr/bin/env python3
"""Per-PR cap on dispatched review rounds in the review sweep.

Between 2026-11-10 and 2026-11-17 `Internal: AI Review & Autofix` ran 3,778
times over 398 PRs (median 4 per PR, 90th percentile 26, 81 on PR #9178), and
2,430 of those runs were dispatched: the sweep re-dispatched every open PR
without an active review run, every 30 minutes, with no bound per PR.
scripts/review_sweep_round_cap.sh counts the dispatched review runs per PR in
a time window from one listing per tick; a PR at the cap whose head is not
approved gets `ai:needs-human` and one comment, and no dispatch.

The sweep cases run the step's shell as extracted from the shipped workflow
against a fake `gh`, with the helper staged where the workflow sources it.
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
SWEEP_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
HELPER = REPO_ROOT / "scripts" / "review_sweep_round_cap.sh"
HEAD_GATE_HELPER = REPO_ROOT / "scripts" / "review_sweep_head_gate.sh"
STEP_MARKER = "- name: Enumerate open PRs and dispatch internal-review.yml"
RUN_HEADER = "        run: |\n"
RUN_INDENT = " " * 10
BLOCK_END_RE = re.compile(r"^(?: {0,9}#| {0,6}- | {0,8}[A-Za-z_][\w-]*:(?:\s|$))")
WF_PATH = ".github/workflows/internal-review.yml"

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
	# Head-gate annotation (scripts/review_sweep_head_gate.sh).
	if (root / "graphql_fails").exists():
		sys.exit(1)
	gate = fixture("gate.json", {})
	# Head commit dates; by default older than every round in the fixtures.
	commit_dates = fixture("commit_dates.json", {})
	nodes = []
	for pr in fixture("pulls.json", []):
		state = gate.get(str(pr["number"]), "PENDING")
		status = None if state == "ABSENT" else {"context": {"state": state}}
		# Review-blocked judge merge verdict on the head (issue #9991).
		verdict = fixture("verdicts.json", {}).get(str(pr["number"]))
		if verdict and status is not None:
			status["verdict"] = {"state": "SUCCESS", "description": verdict, "createdAt": "2026-11-23T09:00:00Z"}
		commit = {"oid": pr["head"]["sha"], "status": status}
		committed = commit_dates.get(str(pr["number"]), "2026-11-01T00:00:00Z")
		if committed is not None:
			commit["committedDate"] = committed
		nodes.append({"number": pr["number"], "headRefOid": pr["head"]["sha"],
			"commits": {"nodes": [{"commit": commit}]}})
	out({"data": {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}})
endpoint = next((a for a in args[1:] if a.startswith("repos/")), "")
if endpoint.endswith("/labels"):
	if (root / "label_fails").exists():
		sys.exit(1)
	with open(root / "labels.log", "a") as log:
		log.write(" ".join(args) + "\n")
	out({})
if endpoint.endswith("/comments"):
	if (root / "comment_fails").exists():
		sys.exit(1)
	with open(root / "comments.log", "a") as log:
		log.write(json.dumps(args) + "\n")
	out({})
if "--jq" in args and args[args.index("--jq") + 1] == ".default_branch":
	out("main\n")
if endpoint.endswith("/pulls"):
	out(fixture("pulls.json", []))
if "/actions/workflows/" in endpoint and endpoint.endswith("/runs"):
	if "event=workflow_dispatch" in args:
		if (root / "rounds_fail").exists():
			sys.stderr.write("HTTP 502\n")
			sys.exit(1)
		pages = fixture("rounds.json", [{"total_count": 0, "workflow_runs": []}])
		sys.stdout.write("".join(json.dumps(page) for page in pages))
		# A later page failed: gh has printed the earlier pages and exits non-zero.
		sys.exit(1 if (root / "rounds_partial_fail").exists() else 0)
	status = next((a.split("=", 1)[1] for a in args if a.startswith("status=")), "")
	if (root / "active_fails").exists():
		sys.stderr.write("HTTP 502\n")
		sys.exit(1)
	runs = [r for r in fixture("active.json", []) if r.get("status") == status and endpoint.split("/")[-2] == "internal-review.yml"]
	out({"total_count": len(runs), "workflow_runs": runs})
sys.stderr.write("unexpected gh call: " + " ".join(args) + "\n")
sys.exit(1)
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


def _pr(number: int, labels: tuple[str, ...] = ()) -> dict:
	return {
		"number": number,
		"draft": False,
		"title": f"PR {number}",
		"body": "",
		"created_at": "2026-11-09T14:15:57Z",
		"user": {"login": "pipeline-bot"},
		"head": {"ref": f"ai/issue-{number + 1000}", "sha": "0" * 40},
		"labels": [{"name": name} for name in labels],
	}


def _round(pr: int, **overrides: object) -> dict:
	run = {
		"id": 44000000000 + pr,
		"event": "workflow_dispatch",
		"status": "completed",
		"conclusion": "success",
		"head_branch": "main",
		"path": WF_PATH,
		"display_title": f"Internal: AI Review & Autofix [pr:{pr}]",
		"created_at": "2026-11-17T13:23:36Z",
	}
	run.update(overrides)
	return run


def _page(*runs: dict) -> dict:
	return {"total_count": len(runs), "workflow_runs": list(runs)}


def _setup(tmp_path: Path, *, prs: list[dict] | None = None, rounds: list[dict] | None = None, active: list[dict] | None = None,
		helper: bool = True, flags: tuple[str, ...] = (), gate: dict[str, str] | None = None,
		commit_dates: dict[str, str | None] | None = None) -> Path:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	if prs is not None:
		(tmp_path / "pulls.json").write_text(json.dumps(prs), encoding="utf-8")
	if rounds is not None:
		(tmp_path / "rounds.json").write_text(json.dumps(rounds), encoding="utf-8")
	if active is not None:
		(tmp_path / "active.json").write_text(json.dumps(active), encoding="utf-8")
	if gate is not None:
		(tmp_path / "gate.json").write_text(json.dumps(gate), encoding="utf-8")
	if commit_dates is not None:
		(tmp_path / "commit_dates.json").write_text(json.dumps(commit_dates), encoding="utf-8")
	for flag in flags:
		(tmp_path / flag).write_text("", encoding="utf-8")
	# The head-gate helper is always staged (it supplies head_gate_state, which
	# the cap needs); the fake answers its GraphQL read with PENDING by default.
	support = tmp_path / ".ci-rerun-support" / "scripts"
	support.mkdir(parents=True)
	shutil.copy(HEAD_GATE_HELPER, support / "review_sweep_head_gate.sh")
	if helper:
		shutil.copy(HELPER, support / "review_sweep_round_cap.sh")
	return bin_dir


def _env(tmp_path: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
	env = {
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
	}
	env.update(extra)
	return env


def _lines(tmp_path: Path, name: str) -> list[str]:
	path = tmp_path / name
	return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _dispatched(tmp_path: Path) -> list[str]:
	return [line.split("pr_number=")[1].split()[0] for line in _lines(tmp_path, "dispatches.log")]


def _run_sweep(tmp_path: Path, **kwargs: object) -> subprocess.CompletedProcess:
	env_extra = kwargs.pop("env", {})
	bin_dir = _setup(tmp_path, **kwargs)
	return subprocess.run(["bash", "-c", _extract_step_script()], env=_env(tmp_path, bin_dir, **env_extra),
		capture_output=True, text=True, cwd=tmp_path, check=False)


def _run_counts(tmp_path: Path, *args: str, **kwargs: object) -> subprocess.CompletedProcess:
	bin_dir = _setup(tmp_path, helper=False, **kwargs)
	script = f'source "{HELPER}"; review_sweep_round_counts "$@"'
	return subprocess.run(["bash", "-c", script, "counts", *args], env=_env(tmp_path, bin_dir),
		capture_output=True, text=True, cwd=tmp_path, check=False)


# ---------------------------------------------------------------------------
# review_sweep_round_counts
# ---------------------------------------------------------------------------


def test_counts_dispatched_rounds_per_pr_across_pages(tmp_path: Path) -> None:
	pages = [
		_page(_round(9134), _round(9134), _round(9178)),
		_page(_round(9134, head_branch=None), _round(9178, status="in_progress", conclusion=None), _round(9570, path=WF_PATH + "@refs/heads/main")),
	]
	result = _run_counts(tmp_path, "o/r", "main", "72", rounds=pages)
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == {"9134": 3, "9178": 2, "9570": 1}
	assert "REVIEW_SWEEP_ROUND_CAP outcome=counted window_hours=72 runs=6 prs=3 truncated=false" in result.stderr
	# One paginated listing for all PRs, filtered at the API to dispatched runs in the window.
	calls = [json.loads(line) for line in _lines(tmp_path, "calls.log")]
	assert len(calls) == 1
	assert "--paginate" in calls[0] and "event=workflow_dispatch" in calls[0] and "per_page=100" in calls[0]
	assert any(re.fullmatch(r"created=>=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", arg) for arg in calls[0])
	assert "repos/o/r/actions/workflows/internal-review.yml/runs" in calls[0]


def test_counts_only_trusted_dispatched_runs(tmp_path: Path) -> None:
	runs = [
		_round(7),
		# Not counted: cancelled, another event, a branch copy of the workflow,
		# another workflow file, a title that only resembles the marker.
		_round(7, conclusion="cancelled"),
		_round(7, event="pull_request"),
		_round(7, head_branch="feature/copy"),
		_round(7, path=".github/workflows/review_autofix.yml"),
		_round(7, display_title="Internal: AI Review & Autofix [pr:7] extra"),
		_round(7, display_title="x Internal: AI Review & Autofix [pr:7]"),
		_round(7, display_title="Internal: AI Review & Autofix [pr:07]"),
		_round(7, display_title=None),
	]
	result = _run_counts(tmp_path, "o/r", "main", "72", rounds=[_page(*runs)])
	assert json.loads(result.stdout) == {"7": 1}


def test_counts_empty_window_is_an_empty_object(tmp_path: Path) -> None:
	result = _run_counts(tmp_path, "o/r", "main", "72", rounds=[_page()])
	assert json.loads(result.stdout) == {}
	assert "outcome=counted window_hours=72 runs=0 prs=0 truncated=false" in result.stderr


@pytest.mark.parametrize("args, flags, rounds, reason", [
	(("o/r", "main", "72"), ("rounds_fail",), None, "listing"),
	(("o/r", "main", "72"), (), [{"message": "Server Error"}], "parse"),
	(("o/r", "main", "72"), (), [_page(_round(1)), {"workflow_runs": "x"}], "parse"),
	(("o/r/x", "main", "72"), (), None, "invalid_input"),
	(("o/..", "main", "72"), (), None, "invalid_input"),
	(("o/r", "ma in", "72"), (), None, "invalid_input"),
	(("o/r", "main", "0"), (), None, "invalid_input"),
	(("o/r", "main", "three"), (), None, "invalid_input"),
])
def test_counts_fail_open_prints_nothing(tmp_path: Path, args: tuple[str, ...], flags: tuple[str, ...], rounds: list | None, reason: str) -> None:
	result = _run_counts(tmp_path, *args, rounds=rounds, flags=flags)
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert f"REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason={reason}" in result.stderr


# ---------------------------------------------------------------------------
# The sweep step
# ---------------------------------------------------------------------------


def _rounds(**per_pr: int) -> list[dict]:
	return [_page(*[_round(int(pr)) for pr, count in per_pr.items() for _ in range(count)])]


def test_pr_at_the_cap_is_escalated_once_and_not_dispatched(tmp_path: Path) -> None:
	result = _run_sweep(tmp_path, prs=[_pr(5), _pr(6)], rounds=_rounds(**{"5": 10, "6": 9}))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["6"]
	assert "REVIEW_SWEEP_ROUND_CAP outcome=escalated pr=#5 rounds=10 max=10 window_hours=48" in result.stdout
	assert "AUTOFIX_SWEEP_SKIP pr=#5 reason=review_round_cap rounds=10 max=10 window_hours=48 head_ref=ai/issue-1005" in result.stdout
	assert "skipped_round_cap=1" in result.stdout and "dispatched=1" in result.stdout
	labels = _lines(tmp_path, "labels.log")
	assert labels == ["api repos/o/r/issues/5/labels -f labels[]=ai:needs-human"]
	comments = [json.loads(line) for line in _lines(tmp_path, "comments.log")]
	assert len(comments) == 1 and "repos/o/r/issues/5/comments" in comments[0]
	body = next(arg for arg in comments[0] if arg.startswith("body="))
	assert "10 dispatched review rounds in the last 48 hours (limit 10)" in body
	assert "its head is still not approved" in body and "The label stays until a person removes it" in body
	assert "`ai:needs-human`" in body and body.rstrip().endswith("<!-- ai:review-round-cap:v1 -->")
	# PR #9839 follow-up: the head the cap was hit on, so the poller can
	# release the label once the head moves.
	assert f"<!-- ai:review-round-cap-head:v1 head={'0' * 40} -->\n<!-- ai:review-round-cap:v1 -->" in body
	assert "or the PR moves to a new head" in body
	# CLAUDE.md §19: the comment names no issue or PR number.
	assert not re.search(r"#[0-9]", body)


def test_escalate_head_marker_is_optional_and_wired_from_the_sweep() -> None:
	text = (REPO_ROOT / "scripts" / "review_sweep_round_cap.sh").read_text(encoding="utf-8")
	assert 'cap_head_sha="${8:-}"' in text
	assert '[[ "${cap_head_sha}" =~ ^[0-9a-f]{40}$ ]]' in text
	sweep = (REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml").read_text(encoding="utf-8")
	assert "\"$(printf '%s' \"${row}\" | jq -r '.head_sha // \"\"' 2>/dev/null || true)\" || true" in sweep


def test_already_escalated_pr_costs_no_write(tmp_path: Path) -> None:
	result = _run_sweep(tmp_path, prs=[_pr(5, labels=("ai:engine-claude", "ai:needs-human"))], rounds=_rounds(**{"5": 30}))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "REVIEW_SWEEP_ROUND_CAP outcome=already_escalated pr=#5 rounds=30 max=10 window_hours=48" in result.stdout
	assert "AUTOFIX_SWEEP_SKIP pr=#5 reason=review_round_cap" in result.stdout
	assert not _lines(tmp_path, "labels.log") and not _lines(tmp_path, "comments.log")


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
	result = _run_sweep(tmp_path, prs=[_pr(5), _pr(6)], rounds=_rounds(**{"5": 10}), env={"DRY_RUN": "true"})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert "REVIEW_SWEEP_ROUND_CAP outcome=dry_run pr=#5" in result.stdout
	assert "AUTOFIX_SWEEP_DRY_RUN pr=#6" in result.stdout and "AUTOFIX_SWEEP_DRY_RUN pr=#5" not in result.stdout
	assert not _lines(tmp_path, "labels.log") and not _lines(tmp_path, "comments.log")


def test_knobs_set_the_cap_and_the_window(tmp_path: Path) -> None:
	result = _run_sweep(tmp_path, prs=[_pr(5), _pr(6)], rounds=_rounds(**{"5": 3, "6": 2}),
		env={"SWEEP_REVIEW_MAX_ROUNDS": "3", "SWEEP_REVIEW_ROUND_WINDOW_HOURS": "24"})
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["6"]
	assert "reason=review_round_cap rounds=3 max=3 window_hours=24" in result.stdout
	assert "outcome=counted window_hours=24" in result.stderr


@pytest.mark.parametrize("env, warning", [
	({"SWEEP_REVIEW_MAX_ROUNDS": "0"}, "SWEEP_REVIEW_MAX_ROUNDS is not a positive integer; using 10."),
	({"SWEEP_REVIEW_MAX_ROUNDS": "lots"}, "SWEEP_REVIEW_MAX_ROUNDS is not a positive integer; using 10."),
	({"SWEEP_REVIEW_ROUND_WINDOW_HOURS": "-1"}, "SWEEP_REVIEW_ROUND_WINDOW_HOURS is not a positive integer; using 48."),
])
def test_invalid_knobs_fall_back_to_the_defaults(tmp_path: Path, env: dict[str, str], warning: str) -> None:
	result = _run_sweep(tmp_path, prs=[_pr(5), _pr(6)], rounds=_rounds(**{"5": 10, "6": 9}), env=env)
	assert result.returncode == 0, result.stderr
	assert warning in result.stdout
	assert _dispatched(tmp_path) == ["6"]
	assert "reason=review_round_cap rounds=10 max=10 window_hours=48" in result.stdout


@pytest.mark.parametrize("kwargs", [
	{"env": {"SWEEP_REVIEW_ROUND_CAP_ENABLED": "false"}},
	{"helper": False},
	{"flags": ("rounds_fail",)},
	# A page failed after earlier pages were printed: not a shorter, complete count.
	{"flags": ("rounds_partial_fail",)},
	# No head-gate annotation: nothing proves the head is not approved.
	{"flags": ("graphql_fails",)},
])
def test_cap_off_or_unreadable_dispatches_as_before(tmp_path: Path, kwargs: dict) -> None:
	result = _run_sweep(tmp_path, prs=[_pr(5), _pr(6)], rounds=_rounds(**{"5": 40}), **kwargs)
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == ["5", "6"]
	assert "reason=review_round_cap" not in result.stdout
	assert "skipped_round_cap=0" in result.stdout
	assert not _lines(tmp_path, "labels.log") and not _lines(tmp_path, "comments.log")
	if kwargs.get("env") or kwargs.get("helper") is False or kwargs.get("flags") == ("graphql_fails",):
		# No listing at all when the cap is off, the helper is missing or no head date is known.
		assert not any("event=workflow_dispatch" in line for line in _lines(tmp_path, "calls.log"))


@pytest.mark.parametrize("flag, reason, commented", [
	("label_fails", "label_write", True),
	("comment_fails", "comment_write", False),
])
def test_failed_escalation_write_still_skips_the_dispatch(tmp_path: Path, flag: str, reason: str, commented: bool) -> None:
	result = _run_sweep(tmp_path, prs=[_pr(5)], rounds=_rounds(**{"5": 12}), flags=(flag,))
	assert result.returncode == 0, result.stderr
	assert _dispatched(tmp_path) == []
	assert f"REVIEW_SWEEP_ROUND_CAP outcome=escalate_failed reason={reason} pr=#5" in result.stdout
	assert "AUTOFIX_SWEEP_SKIP pr=#5 reason=review_round_cap" in result.stdout
	# The comment goes first: a failed comment write adds no label, so the
	# next tick (which sees no label) retries both.
	assert bool(_lines(tmp_path, "comments.log")) is commented
	assert not _lines(tmp_path, "labels.log")


@pytest.mark.parametrize("committed, capped", [
	# The head was pushed after every round: the rounds were an earlier head's.
	("2026-11-17T14:00:00Z", False),
	# The same instant in the committer offset is still after the rounds.
	("2026-11-17T06:00:00-08:00", False),
	# 05:00-08:00 is 13:00Z, before the rounds at 13:23Z: they count.
	("2026-11-17T05:00:00-08:00", True),
	# No commit date: nothing proves the rounds were this head's.
	(None, False),
])
def test_only_rounds_since_the_current_head_count(tmp_path: Path, committed: str | None, capped: bool) -> None:
	result = _run_sweep(tmp_path, prs=[_pr(5)], rounds=_rounds(**{"5": 40}), commit_dates={"5": committed})
	assert result.returncode == 0, result.stderr
	assert ("reason=review_round_cap" in result.stdout) is capped, result.stdout
	assert (_dispatched(tmp_path) == []) is capped
	assert bool(_lines(tmp_path, "labels.log")) is capped


def test_counts_with_a_snapshot_start_at_each_head_commit(tmp_path: Path) -> None:
	runs = [
		_round(5, created_at="2026-11-17T12:00:00Z"), _round(5, created_at="2026-11-17T14:00:00Z"),
		_round(6), _round(7),
	]
	snapshot = [{"number": 5, "head_committed_at": "2026-11-17T13:00:00Z"}, {"number": 6},
		{"number": 7, "head_committed_at": "2026-11-17"}]
	result = _run_counts(tmp_path, "o/r", "main", "72", json.dumps(snapshot), rounds=[_page(*runs)])
	assert result.returncode == 0, result.stderr
	# PR 5: only the run after its head commit; PRs 6 and 7: no usable date, left out.
	assert json.loads(result.stdout) == {"5": 1}


def test_counts_skip_the_listing_when_no_row_has_a_head_date(tmp_path: Path) -> None:
	# The head-gate read failed open: no row can be counted, so no listing.
	snapshot = [{"number": 5}, {"number": 6, "head_committed_at": None}]
	result = _run_counts(tmp_path, "o/r", "main", "72", json.dumps(snapshot), rounds=[_page(_round(5))])
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert "REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=head_date_unavailable window_hours=72" in result.stderr
	assert not _lines(tmp_path, "calls.log")


def test_counts_reject_a_snapshot_that_is_not_an_array(tmp_path: Path) -> None:
	result = _run_counts(tmp_path, "o/r", "main", "72", '{"5": 1}', rounds=[_page(_round(5))])
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert "REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=invalid_input" in result.stderr


@pytest.mark.parametrize("args", [
	("o/r", "5", "x", "10", "48"),
	("o/r", "5", "12", "0", "48"),
	("o/r", "5", "12", "10", "48 hours"),
])
def test_escalate_rejects_non_numeric_arguments(tmp_path: Path, args: tuple[str, ...]) -> None:
	bin_dir = _setup(tmp_path, helper=False)
	script = f'source "{HELPER}"; review_sweep_round_cap_escalate "$@" "[]" false'
	result = subprocess.run(["bash", "-c", script, "escalate", *args], env=_env(tmp_path, bin_dir),
		capture_output=True, text=True, cwd=tmp_path, check=False)
	assert result.returncode == 0, result.stderr
	assert "REVIEW_SWEEP_ROUND_CAP outcome=escalate_failed reason=invalid_input" in result.stdout
	assert not _lines(tmp_path, "labels.log") and not _lines(tmp_path, "comments.log")


def test_pr_with_an_active_review_run_is_not_escalated(tmp_path: Path) -> None:
	active = [_round(5, status="in_progress", conclusion=None)]
	result = _run_sweep(tmp_path, prs=[_pr(5)], rounds=_rounds(**{"5": 12}), active=active)
	assert result.returncode == 0, result.stderr
	assert "AUTOFIX_SWEEP_SKIP pr=#5 reason=active_run" in result.stdout
	assert "reason=review_round_cap" not in result.stdout
	assert not _lines(tmp_path, "labels.log") and not _lines(tmp_path, "comments.log")


def test_one_listing_per_tick_whatever_the_number_of_prs(tmp_path: Path) -> None:
	result = _run_sweep(tmp_path, prs=[_pr(n) for n in range(1, 9)], rounds=_rounds(**{"3": 12}))
	assert result.returncode == 0, result.stderr
	assert sum(1 for line in _lines(tmp_path, "calls.log") if "event=workflow_dispatch" in line) == 1
	assert _dispatched(tmp_path) == ["1", "2", "4", "5", "6", "7", "8"]


@pytest.mark.parametrize("state, capped", [
	("SUCCESS", False),
	("PENDING", True),
	("FAILURE", True),
	("ERROR", True),
	("ABSENT", True),
])
def test_only_a_head_that_is_not_approved_is_capped(tmp_path: Path, state: str, capped: bool) -> None:
	# An approved head waiting for CI or a merge draws cheap same-head gate
	# skips every tick; those must not read as failed review rounds.
	result = _run_sweep(tmp_path, prs=[_pr(5)], rounds=_rounds(**{"5": 40}), gate={"5": state})
	assert result.returncode == 0, result.stderr
	assert ("reason=review_round_cap" in result.stdout) is capped, result.stdout
	assert (_dispatched(tmp_path) == []) is capped
	assert bool(_lines(tmp_path, "labels.log")) is capped


def test_incomplete_active_snapshot_reads_no_counts(tmp_path: Path) -> None:
	result = _run_sweep(tmp_path, prs=[_pr(5)], rounds=_rounds(**{"5": 40}), flags=("active_fails",))
	assert result.returncode == 0, result.stderr
	assert "reason=active_snapshot_incomplete" in result.stdout
	assert not any("event=workflow_dispatch" in line for line in _lines(tmp_path, "calls.log"))
	assert _dispatched(tmp_path) == [] and not _lines(tmp_path, "labels.log")


def test_counts_flag_a_listing_github_truncated(tmp_path: Path) -> None:
	page = {"total_count": 1040, "workflow_runs": [_round(5), _round(5)]}
	result = _run_counts(tmp_path, "o/r", "main", "72", rounds=[page])
	assert json.loads(result.stdout) == {"5": 2}
	assert "outcome=counted window_hours=72 runs=2 prs=1 truncated=true" in result.stderr


def test_counts_reject_a_listing_that_failed_on_a_later_page(tmp_path: Path) -> None:
	result = _run_counts(tmp_path, "o/r", "main", "48", rounds=[_page(_round(5))], flags=("rounds_partial_fail",))
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert "REVIEW_SWEEP_ROUND_CAP outcome=read_failed reason=listing window_hours=48 rc=1" in result.stderr


def test_counts_use_the_callers_retry_wrapper_and_project_each_page(tmp_path: Path) -> None:
	bin_dir = _setup(tmp_path, helper=False, rounds=[_page(_round(5))])
	script = f'gh_api_retry() {{ echo wrapped >> "{tmp_path}/wrapper.log"; gh api "$@"; }}; source "{HELPER}"; review_sweep_round_counts o/r main 48'
	result = subprocess.run(["bash", "-c", script], env=_env(tmp_path, bin_dir), capture_output=True, text=True, cwd=tmp_path, check=False)
	assert json.loads(result.stdout) == {"5": 1}
	assert _lines(tmp_path, "wrapper.log") == ["wrapped"]
	call = json.loads(_lines(tmp_path, "calls.log")[0])
	assert "--jq" in call and "display_title" in call[call.index("--jq") + 1]


def test_workflow_passes_the_cap_knobs_with_defaults() -> None:
	# Read the step's env block from the workflow text (PyYAML is not
	# available in every test container, so the module must not need it).
	text = SWEEP_WF.read_text(encoding="utf-8")
	start = text.find(STEP_MARKER)
	assert start != -1, "could not locate the sweep step"
	run_at = text.find(RUN_HEADER, start)
	assert run_at != -1
	env: dict[str, str] = {}
	for line in text[start:run_at].splitlines():
		match = re.fullmatch(r" {10}([A-Z][A-Z0-9_]*): (.*)", line)
		if match:
			env[match.group(1)] = match.group(2)
	assert env["SWEEP_REVIEW_ROUND_CAP_ENABLED"] == "${{ vars.SWEEP_REVIEW_ROUND_CAP_ENABLED || 'true' }}"
	assert env["SWEEP_REVIEW_MAX_ROUNDS"] == "${{ vars.SWEEP_REVIEW_MAX_ROUNDS || '10' }}"
	assert env["SWEEP_REVIEW_ROUND_WINDOW_HOURS"] == "${{ vars.SWEEP_REVIEW_ROUND_WINDOW_HOURS || '48' }}"
	# The helper comes from the trusted default-branch checkout, like the head gate.
	assert "source .ci-rerun-support/scripts/review_sweep_round_cap.sh" in _extract_step_script()


@pytest.mark.parametrize("switch, capped", [("true", True), ("false", True)])
def test_unevaluated_judge_merge_verdict_head_stays_capped(tmp_path: Path, switch: str, capped: bool) -> None:
	# Issue #9991: a head whose judge verdict still waits is skipped before
	# the cap and creates no run. Without pr_checks_lib.sh staged the sweep
	# cannot evaluate the verdict, so it fails open: it would dispatch, and
	# the round cap still bounds that path.
	(tmp_path / "verdicts.json").write_text(json.dumps({"5": "decision=merge wait=lease_wait base=" + "b" * 40 + " ms=blocked"}), encoding="utf-8")
	result = _run_sweep(tmp_path, prs=[_pr(5)], rounds=_rounds(**{"5": 40}), gate={"5": "PENDING"},
		env={"VERDICT_WAIT_DISPATCH_SKIP_ENABLED": switch})
	assert result.returncode == 0, result.stderr
	assert ("reason=review_round_cap" in result.stdout) is capped, result.stdout
	assert bool(_lines(tmp_path, "labels.log")) is capped
	assert (_dispatched(tmp_path) == ["5"]) is (not capped)


def test_counts_write_the_latest_round_per_pr_only_when_asked(tmp_path: Path) -> None:
	# Issue #9991: the newest counted round per PR bounds the verdict round-cap
	# exemption; stdout keeps the counts shape, and nothing is written by default.
	pages = [_page(_round(5, created_at="2026-11-17T12:00:00Z"), _round(5, created_at="2026-11-17T14:00:00Z"), _round(6))]
	bin_dir = _setup(tmp_path, helper=False, rounds=pages)
	latest = tmp_path / "latest.json"
	script = f'source "{HELPER}"; REVIEW_SWEEP_ROUND_LATEST_FILE="$1" review_sweep_round_counts o/r main 72 ""'
	result = subprocess.run(["bash", "-c", script, "counts", str(latest)], env=_env(tmp_path, bin_dir),
		capture_output=True, text=True, cwd=tmp_path, check=False)
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == {"5": 2, "6": 1}
	assert json.loads(latest.read_text(encoding="utf-8"))["5"] == "2026-11-17T14:00:00Z"
	assert set(json.loads(latest.read_text(encoding="utf-8"))) == {"5", "6"}
