#!/usr/bin/env python3
"""Orphan fix-up close: scripts/orphan_fixup_close.py and its poller wiring.

Background: the unblock judge files a fix-up issue for a blocked item and the
item waits on it. When the item is closed another way the fix-up stays open,
is blocked itself, and gets a fix-up of its own; the chains seen in consumer
repos were 13 to 17 issues deep and never ended. The sweep closes the open
fix-ups whose source issue is closed.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "orphan_fixup_close.py"
POLLER = ROOT / "scripts" / "orchestrate_poll_process.sh"
POLL_WORKFLOW = ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

SPEC = importlib.util.spec_from_file_location("orphan_fixup_close", SCRIPT)
sweep = importlib.util.module_from_spec(SPEC)
sys.modules["orphan_fixup_close"] = sweep
SPEC.loader.exec_module(sweep)

BOT = "pipeline-bot"
NOW = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)
GRACE = dt.timedelta(minutes=60)


def _iso(hours_ago: float) -> str:
	return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fixup(number: int, item: int, labels: list[str] | None = None, author: str = BOT, round_no: int = 1) -> dict:
	body = (
		f"<!-- ai:unblock-fixup:v1 item={item} round={round_no} -->\n## Unblock operator step for #{item}\n\n"
		"The unblock judge chose `operator_step`. Make exactly this change."
	)
	return {"number": number, "labels": ["ai:blocked"] if labels is None else labels, "author_login": author, "body": body}


def _plain(number: int, labels: list[str] | None = None, body: str = "A real task.", author: str = BOT) -> dict:
	return {"number": number, "labels": labels or [], "author_login": author, "body": body}


def _closed(reason: str = "COMPLETED", hours_ago: float = 48) -> dict:
	return {"type": "Issue", "state": "CLOSED", "state_reason": reason, "closed_at": _iso(hours_ago)}


def _select(issues: list[dict], parents: dict | None = None, limit: int = 5) -> dict:
	return sweep.select(issues, parents or {}, BOT, NOW, limit, GRACE)


def _chain(root: int, length: int) -> list[dict]:
	"""Fix-ups root+1 .. root+length, each naming the one before it."""
	return [_fixup(root + step, root + step - 1) for step in range(1, length + 1)]


# ---------------------------------------------------------------- marker


def test_marker_must_be_the_first_line_and_exact() -> None:
	assert sweep.fixup_item("<!-- ai:unblock-fixup:v1 item=144 round=1 -->\n## x") == 144
	assert sweep.fixup_item("<!-- ai:unblock-fixup:v1 item=144 round=12 -->\r\n## x") == 144
	assert sweep.fixup_item("## x\n<!-- ai:unblock-fixup:v1 item=144 round=1 -->") is None
	assert sweep.fixup_item("> <!-- ai:unblock-fixup:v1 item=144 round=1 -->") is None
	assert sweep.fixup_item("<!-- ai:unblock-fixup:v1 item=0 round=1 -->") is None
	assert sweep.fixup_item("<!-- ai:unblock-fixup-request:v1 item=144 id=unblock-144-r1 -->") is None
	assert sweep.fixup_item(None) is None


def test_other_link_kinds_are_not_fixups() -> None:
	"""Re-issues, accept-with-follow-up issues and Source/Parent links are new work."""
	for body in (
		"Re-issued from #144\nDo the thing.",
		"Accepted with this follow-up by the unblock judge (#144).\n\nFollow-up: x",
		"- Source issue: #144\n",
		"- Parent issue: #144\n",
		"- Replaces: #144\n",
	):
		result = _select([{"number": 200, "labels": ["ai:blocked"], "author_login": BOT, "body": body}], {"144": _closed()})
		assert result["close"] == [] and result["skipped"] == {} and result["need_parents"] == []


# ---------------------------------------------------------------- selection


def test_fixup_of_closed_item_is_closed_whatever_the_close_reason() -> None:
	for reason in ("COMPLETED", "NOT_PLANNED", "DUPLICATE"):
		result = _select([_fixup(148, 144)], {"144": _closed(reason)})
		assert result["close"] == [{"number": 148, "parent": 144, "root": 144, "depth": 1, "root_reason": reason.lower(), "chain": [148, 144]}]


def test_whole_chain_closes_in_one_pass_nearest_the_root_first() -> None:
	issues = list(reversed(_chain(144, 17)))
	result = _select(issues, {"144": _closed()}, limit=100)
	assert [row["number"] for row in result["close"]] == list(range(145, 162))
	assert [row["depth"] for row in result["close"]] == list(range(1, 18))
	assert all(row["root"] == 144 for row in result["close"])
	assert result["close"][-1]["chain"] == list(range(161, 143, -1))
	assert result["need_parents"] == []


def test_cap_keeps_the_nearest_to_the_root_and_counts_the_rest() -> None:
	result = _select(_chain(144, 17), {"144": _closed()}, limit=5)
	assert [row["number"] for row in result["close"]] == [145, 146, 147, 148, 149]
	assert result["skipped"] == {"over_cap": 12}
	assert _select(_chain(144, 3), {"144": _closed()}, limit=0)["close"] == []


def test_open_real_issue_keeps_its_fixup_and_the_fixups_below_it() -> None:
	issues = [_plain(71, ["ai:blocked"])] + _chain(71, 4)
	result = _select(issues)
	assert result["close"] == [] and result["skipped"] == {"parent_open": 4}


def test_chain_ending_at_an_in_flight_fixup_is_kept() -> None:
	issues = [_fixup(150, 144, labels=["ai:implementing"]), _fixup(151, 150), _fixup(152, 151)]
	result = _select(issues, {"144": _closed()})
	assert [row["number"] for row in result["close"]] == []
	assert result["skipped"] == {"in_flight": 1, "parent_open": 2}


def test_two_branches_share_one_closed_root() -> None:
	issues = [_fixup(148, 144), _fixup(149, 144), _fixup(150, 148)]
	result = _select(issues, {"144": _closed()})
	assert [(row["number"], row["depth"]) for row in result["close"]] == [(148, 1), (149, 1), (150, 2)]


def test_cyclic_markers_close_nothing() -> None:
	result = _select([_fixup(10, 11), _fixup(11, 10)])
	assert result["close"] == [] and result["need_parents"] == []
	assert _select([_fixup(10, 10)])["skipped"] == {"self_reference": 1}


# ---------------------------------------------------------------- safety rules


def test_only_the_pipeline_identity_counts() -> None:
	result = _select([_fixup(148, 144, author="someone-else"), _fixup(149, 144, author=BOT)], {"144": _closed()})
	assert [row["number"] for row in result["close"]] == [149]
	assert result["skipped"] == {"untrusted_author": 1}


def test_project_issues_are_left_to_the_orchestrator() -> None:
	for label in ("ai:orchestrator-tracking", "ai:orchestrator-managed"):
		result = _select([_fixup(148, 144, labels=["ai:blocked", label])], {"144": _closed()})
		assert result["close"] == [] and result["skipped"] == {"project_issue": 1}


@pytest.mark.parametrize(
	"label",
	[
		"ai:implementing",
		"ai:planning",
		"ai:clarification",
		"ai:awaiting-approval",
		"ai:validating",
		"ai:ready-to-merge",
		"ai:merged",
		"ai:review-blocked",
		"ai:operator-step",
		"needs-triage",
	],
)
def test_a_fixup_with_work_in_flight_or_an_unknown_label_is_kept(label: str) -> None:
	result = _select([_fixup(148, 144, labels=["ai:blocked", label])], {"144": _closed()})
	assert result["close"] == [] and result["skipped"] == {"in_flight": 1}


@pytest.mark.parametrize("labels", [[], ["ai:blocked"], ["ai:scope-blocked"], ["ai:destructive-blocked"], ["ai:needs-human", "ai:security"], ["ai:done"]])
def test_idle_labels_are_closable(labels: list[str]) -> None:
	assert len(_select([_fixup(148, 144, labels=labels)], {"144": _closed()})["close"]) == 1


def test_recently_closed_item_waits_out_the_grace_period() -> None:
	assert _select([_fixup(148, 144)], {"144": _closed(hours_ago=0.5)})["skipped"] == {"parent_closed_recently": 1}
	assert len(_select([_fixup(148, 144)], {"144": _closed(hours_ago=1.5)})["close"]) == 1
	assert _select([_fixup(148, 144)], {"144": {"type": "Issue", "state": "CLOSED"}})["close"] == []


def test_unknown_parent_is_requested_and_closes_nothing_until_read() -> None:
	result = _select([_fixup(148, 144), _fixup(149, 145)])
	assert result["close"] == [] and result["need_parents"] == [144, 145] and result["skipped"] == {"parent_unknown": 2}
	assert _select([_fixup(148, 144)], {"144": None})["need_parents"] == [144]


def test_parent_that_is_open_or_a_pull_request_keeps_the_fixup() -> None:
	assert _select([_fixup(148, 144)], {"144": {"type": "Issue", "state": "OPEN"}})["skipped"] == {"parent_open": 1}
	assert _select([_fixup(148, 144)], {"144": {"type": "PullRequest", "state": "MERGED"}})["skipped"] == {"parent_not_issue": 1}


def test_malformed_input_closes_nothing() -> None:
	assert sweep.select({"not": "a list"}, [], BOT, NOW, 5, GRACE) == {"close": [], "need_parents": [], "skipped": {}}
	assert _select([{"number": "x", "body": "y"}, "z", {"labels": []}])["close"] == []


def test_real_world_shapes_from_consumer_repos() -> None:
	"""digital_pa: #144 closed, #148..#165 a chain of blocked fix-ups; #135 an open tracker."""
	issues = [_plain(135, ["ai:security-audit"], "AI Security Audit Tracker"), _plain(149, [], "Operator steps waiting")] + _chain(144, 17)
	result = _select(issues, {"144": _closed("COMPLETED", 72)}, limit=100)
	assert len(result["close"]) == 17 and 135 not in {row["number"] for row in result["close"]}
	# hylifegroup.com #71: an open CI-failure issue with blocked fix-ups stays untouched.
	issues = [_plain(71, ["ai:blocked"], "CI failure: CI on PR #70")] + [_fixup(74, 71), _fixup(76, 74), _fixup(78, 76)]
	assert _select(issues, {"71": {"type": "Issue", "state": "OPEN"}})["close"] == []


# ---------------------------------------------------------------- timeline check


def _cross(number: int, state: object, pull_request: bool = True, lookup_failed: bool = False) -> dict:
	return {
		"event": "cross-referenced",
		"source": {"issue": {"number": number, "pull_request": {"url": f"u/{number}"} if pull_request else None, "state": state, "lookup_failed": lookup_failed}},
	}


def test_timeline_check_refuses_open_unreadable_and_reopened() -> None:
	check = sweep.timeline_check
	assert check([]) == {"ok": True, "reason": "ok"}
	assert check([_cross(7, "closed"), _cross(8, "closed")])["ok"] is True
	assert check([_cross(7, "closed"), _cross(8, "open")]) == {"ok": False, "reason": "open_linked_pr"}
	assert check([_cross(7, None)]) == {"ok": False, "reason": "linked_pr_state_unknown"}
	assert check([_cross(7, "closed", lookup_failed=True)]) == {"ok": False, "reason": "linked_pr_unreadable"}
	assert check([{"event": "closed"}]) == {"ok": False, "reason": "previously_closed"}
	# pull_request null, a state and no html_url is the GraphQL shape of a cross-repo pull request.
	assert check([_cross(9, "open", pull_request=False)]) == {"ok": False, "reason": "open_linked_pr"}
	assert check({"event": "closed"}) == {"ok": False, "reason": "timeline_invalid"}
	assert check(None) == {"ok": False, "reason": "timeline_invalid"}
	assert check(["x"]) == {"ok": False, "reason": "timeline_invalid"}
	# The timeline helper nulls `pull_request` for a pull request in another repository.
	cross_repo = {"event": "cross-referenced", "source": {"issue": {"number": 5, "pull_request": None, "state": "open", "html_url": "https://github.com/other/repo/pull/5"}}}
	assert check([cross_repo]) == {"ok": False, "reason": "open_linked_pr"}
	cross_repo["source"]["issue"]["state"] = "closed"
	assert check([cross_repo])["ok"] is True
	# The GraphQL shape (the path that runs first) has no html_url: a cross-repo
	# pull request keeps its state, an issue source has state null.
	graphql_pr = {"event": "cross-referenced", "source": {"issue": {"number": 5, "pull_request": None, "state": "open", "merged_at": None, "merged": False, "lookup_failed": False}}}
	assert check([graphql_pr]) == {"ok": False, "reason": "open_linked_pr"}
	graphql_pr["source"]["issue"]["state"] = "closed"
	assert check([graphql_pr])["ok"] is True
	graphql_issue = {"event": "cross-referenced", "source": {"issue": {"number": 6, "pull_request": None, "state": None, "merged_at": None, "merged": False, "lookup_failed": False}}}
	assert check([graphql_issue])["ok"] is True
	# A REST issue source (html_url without /pull/) with state open is not a pull request.
	rest_issue = {"event": "cross-referenced", "source": {"issue": {"number": 7, "pull_request": None, "state": "open", "html_url": "https://github.com/o/r/issues/7"}}}
	assert check([rest_issue])["ok"] is True
	# The GraphQL helper's placeholder for a source it could not resolve fails closed.
	unresolved = {"event": "cross-referenced", "source": {"issue": {"number": None, "pull_request": None, "state": None, "merged_at": None, "merged": False, "lookup_failed": False}}}
	assert check([unresolved]) == {"ok": False, "reason": "linked_pr_unreadable"}


# ---------------------------------------------------------------- CLI


def _cli(*args: str) -> subprocess.CompletedProcess:
	return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), *args], capture_output=True, text=True)


def test_cli_select_and_errors(tmp_path: Path) -> None:
	issues = tmp_path / "issues.json"
	issues.write_text(json.dumps(_chain(144, 2)))
	parents = tmp_path / "parents.json"
	parents.write_text(json.dumps({"144": _closed()}))
	base = ["select", "--issues-file", str(issues), "--trusted-login", BOT, "--now", "2026-10-17T12:00:00Z"]
	first = _cli(*base)
	assert first.returncode == 0 and json.loads(first.stdout)["need_parents"] == [144]
	second = _cli(*base, "--parents-file", str(parents), "--max", "1")
	assert second.returncode == 0
	assert [row["number"] for row in json.loads(second.stdout)["close"]] == [145]
	assert json.loads(second.stdout)["skipped"] == {"over_cap": 1}
	assert _cli(*base[:-2], "--now", "not-a-time").returncode == 1
	assert _cli("select", "--issues-file", str(issues), "--trusted-login", "bad login", "--now", "2026-10-17T12:00:00Z").returncode == 1
	assert _cli(*base, "--max", "-1").returncode == 1
	assert _cli("select", "--issues-file", str(tmp_path / "missing.json"), "--trusted-login", BOT, "--now", "2026-10-17T12:00:00Z").returncode == 2
	timeline = tmp_path / "timeline.json"
	timeline.write_text("[]")
	assert json.loads(_cli("timeline-check", "--timeline-file", str(timeline)).stdout) == {"ok": True, "reason": "ok"}


# ---------------------------------------------------------------- poller wiring


def _function_text(name: str) -> str:
	lines = POLLER.read_text(encoding="utf-8").splitlines()
	start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
	end = next(i for i in range(start, len(lines)) if lines[i] == "}")
	return "\n".join(lines[start : end + 1])


def test_run_unblock_scan_runs_the_sweep_before_the_judge_switch() -> None:
	text = _function_text("run_unblock_scan")
	call = text.index("close_orphan_fixup_chains_sweep ||")
	assert text.index("needs_human_digest_prune || true") < call < text.index('UNBLOCK_JUDGE_ENABLED:-true}" = "false"')
	assert 'ORPHAN_FIXUP_CLOSE outcome=skip reason=error rc=$?' in text


def test_poll_workflow_passes_the_switches_and_stages_the_helper() -> None:
	workflow = yaml.safe_load(POLL_WORKFLOW.read_text(encoding="utf-8"))
	steps = {step.get("name"): step for step in workflow["jobs"]["poll"]["steps"]} if "poll" in workflow["jobs"] else {}
	if not steps:
		steps = {step.get("name"): step for job in workflow["jobs"].values() for step in job.get("steps", [])}
	for name in ("Process each tracking issue", "Run unblock scan without active projects"):
		env = steps[name]["env"]
		assert env["ORPHAN_FIXUP_CLOSE_ENABLED"] == "${{ vars.ORPHAN_FIXUP_CLOSE_ENABLED || 'true' }}"
		assert env["ORPHAN_FIXUP_CLOSE_DRY_RUN"] == "${{ vars.ORPHAN_FIXUP_CLOSE_DRY_RUN || 'false' }}"
		assert env["ORPHAN_FIXUP_CLOSE_MAX_PER_TICK"] == "${{ vars.ORPHAN_FIXUP_CLOSE_MAX_PER_TICK || '5' }}"
		assert env["ORPHAN_FIXUP_CLOSE_GRACE_MINUTES"] == "${{ vars.ORPHAN_FIXUP_CLOSE_GRACE_MINUTES || '60' }}"
	assert "scripts/unblock_scan.py scripts/unblock_ledger.py scripts/orphan_fixup_close.py" in POLL_WORKFLOW.read_text(encoding="utf-8")


def test_ci_runs_this_file() -> None:
	assert "tests/test_orphan_fixup_close.py" in CI_WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------- the shell function, with a fake gh

FAKE_GH = r"""#!/usr/bin/env bash
# Fake gh: serves fixtures and records every call in $FAKE_LOG.
printf '%s\n' "$(printf '%s' "gh $*" | tr '\n' '~')" >> "${FAKE_LOG}"
if [ "$1" = "api" ] && [ "$2" = "graphql" ]; then
	[ -f "${FAKE_DIR}/graphql.json" ] || exit 1
	cat "${FAKE_DIR}/graphql.json"
	[ -f "${FAKE_DIR}/graphql_fails" ] && exit 1
	exit 0
fi
if [ "$1" = "api" ]; then
	path="$2"; shift 2
	jq_expr=""
	while [ $# -gt 0 ]; do
		if [ "$1" = "--jq" ]; then jq_expr="$2"; shift; fi
		shift
	done
	page="${path##*page=}"
	file="${FAKE_DIR}/page${page}.json"
	[ -f "${file}" ] || { echo "[]" > "${FAKE_DIR}/empty.json"; file="${FAKE_DIR}/empty.json"; }
	[ -f "${FAKE_DIR}/list_fails" ] && exit 1
	jq -c "${jq_expr:-.}" "${file}"
	exit 0
fi
if [ "$1" = "issue" ] && [ "$2" = "close" ]; then
	[ -f "${FAKE_DIR}/close_fails" ] && exit 1
	exit 0
fi
exit 1
"""


def _run_sweep(tmp_path: Path, pages: list[list[dict]], graphql: dict | None, timelines: dict[int, object] | None = None, env: dict[str, str] | None = None, list_fails: bool = False, close_fails: bool = False, graphql_fails: bool = False, timeline_fails: list[int] | None = None) -> tuple[subprocess.CompletedProcess, list[str]]:
	fake_dir = tmp_path / "fake"
	bin_dir = tmp_path / "bin"
	fake_dir.mkdir()
	bin_dir.mkdir()
	log = tmp_path / "calls.log"
	log.write_text("")
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH)
	gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
	for number, page in enumerate(pages, start=1):
		rest = [
			{"number": item["number"], "labels": [{"name": name} for name in item["labels"]], "user": {"login": item["author_login"]}, "body": item["body"], "pull_request": item.get("pull_request")}
			for item in page
		]
		(fake_dir / f"page{number}.json").write_text(json.dumps(rest))
	if graphql is not None:
		(fake_dir / "graphql.json").write_text(json.dumps(graphql))
	if list_fails:
		(fake_dir / "list_fails").write_text("")
	if close_fails:
		(fake_dir / "close_fails").write_text("")
	if graphql_fails:
		(fake_dir / "graphql_fails").write_text("")
	for number in timeline_fails or []:
		(fake_dir / f"timeline_fail_{number}").write_text("")
	for number, timeline in (timelines or {}).items():
		(fake_dir / f"timeline_{number}.json").write_text(json.dumps(timeline))
	funcs = tmp_path / "funcs.sh"
	funcs.write_text(_function_text("is_truthy") + "\n" + _function_text("close_orphan_fixup_chains_sweep") + "\n", encoding="utf-8")
	script = textwrap.dedent(
		f"""
		set -uo pipefail
		source "{funcs}"
		gh_retry() {{ "$@"; }}
		_safe_gh_jq() {{ gh api "$@"; }}
		unblock_trusted_login() {{ UNBLOCK_TRUSTED_LOGIN="{BOT}"; printf '%s' "${{UNBLOCK_TRUSTED_LOGIN}}"; }}
		_issue_timeline_with_cross_refs_json() {{
			[ -f "${{FAKE_DIR}}/timeline_fail_$1" ] && return 1
			if [ -f "${{FAKE_DIR}}/timeline_$1.json" ]; then cat "${{FAKE_DIR}}/timeline_$1.json"; else echo '[]'; fi
		}}
		export GITHUB_REPOSITORY="owner/repo"
		close_orphan_fixup_chains_sweep
		echo "rc=$?"
		"""
	)
	full_env = {k: v for k, v in os.environ.items() if not k.startswith("ORPHAN_FIXUP") and k not in ("BASH_ENV", "ENV", "GH_TOKEN", "GITHUB_TOKEN")}
	full_env.update({"PATH": f"{bin_dir}:{full_env['PATH']}", "FAKE_DIR": str(fake_dir), "FAKE_LOG": str(log), "RUNNER_TEMP": str(tmp_path / "runner"), "PYTHONDONTWRITEBYTECODE": "1"})
	full_env.update(env or {})
	(tmp_path / "runner").mkdir()
	result = subprocess.run(["bash", "-c", script], cwd=ROOT, env=full_env, capture_output=True, text=True)
	return result, log.read_text().splitlines()


def _gql(**parents: dict | None) -> dict:
	data = {}
	for key, value in parents.items():
		number = key.lstrip("p")
		data[f"p{number}"] = None if value is None else {"__typename": value.get("type", "Issue"), "state": value["state"], "stateReason": value.get("state_reason"), "closedAt": value.get("closed_at")}
	return {"data": {"repository": data}}


def _closes(calls: list[str]) -> list[str]:
	return [line for line in calls if line.startswith("gh issue close")]


def test_sweep_closes_a_chain_with_one_comment_each(tmp_path: Path) -> None:
	issues = [_plain(135, ["ai:security-audit"])] + _chain(144, 3)
	result, calls = _run_sweep(tmp_path, [issues], _gql(**{"p144": {"state": "CLOSED", "state_reason": "COMPLETED", "closed_at": _iso(72)}}))
	assert result.returncode == 0 and "rc=0" in result.stdout, result.stderr
	closes = _closes(calls)
	assert [line.split()[3] for line in closes] == ["145", "146", "147"]
	for line in closes:
		assert "--repo owner/repo --reason not planned -c Closing as not planned" in line
		assert "<!-- ai:orphan-fixup-closed:v1 item=" in line and "root=144 -->" in line
	assert "chain (#145 -> #144)" in closes[0] and "chain (#147 -> #146 -> #145 -> #144)" in closes[2]
	assert "ORPHAN_FIXUP_CLOSE item=147 parent=146 root=144 depth=3 outcome=closed" in result.stdout
	assert "selected=3 closed=3 held=0" in result.stdout and "outcome=done" in result.stdout
	# 1 list page + 1 aliased GraphQL query + 3 closes; the timelines go through the stubbed helper.
	assert len(calls) == 5 and sum(1 for line in calls if line.startswith("gh api graphql")) == 1


def test_sweep_with_nothing_to_do_costs_one_list_call(tmp_path: Path) -> None:
	issues = [_plain(71, ["ai:blocked"]), _fixup(74, 71), _fixup(76, 74)]
	result, calls = _run_sweep(tmp_path, [issues], None)
	assert "selected=0 closed=0 held=0" in result.stdout and len(calls) == 1 and _closes(calls) == []


def test_sweep_keeps_a_refused_fixup_and_everything_under_it(tmp_path: Path) -> None:
	"""A reopened fix-up stays open, and so do the fix-ups below it: a chain only closes from its closed root."""
	issues = _chain(144, 3)
	timelines = {146: [{"event": "closed"}]}
	result, calls = _run_sweep(tmp_path, [issues], _gql(**{"p144": {"state": "CLOSED", "state_reason": "NOT_PLANNED", "closed_at": _iso(72)}}), timelines)
	assert [line.split()[3] for line in _closes(calls)] == ["145"]
	assert "item=146 parent=145 root=144 outcome=skip reason=previously_closed" in result.stdout
	assert "item=147 parent=146 root=144 outcome=skip reason=parent_held held=146" in result.stdout
	assert "selected=3 closed=1 held=2" in result.stdout


def test_sweep_open_pr_on_the_nearest_fixup_holds_the_whole_chain(tmp_path: Path) -> None:
	timelines = {145: [_cross(900, "open")]}
	result, calls = _run_sweep(tmp_path, [_chain(144, 3)], _gql(**{"p144": {"state": "CLOSED", "state_reason": "COMPLETED", "closed_at": _iso(72)}}), timelines)
	assert _closes(calls) == []
	assert "item=145 parent=144 root=144 outcome=skip reason=open_linked_pr" in result.stdout
	assert "item=146 parent=145 root=144 outcome=skip reason=parent_held held=145" in result.stdout
	assert "selected=3 closed=0 held=3" in result.stdout


def test_sweep_held_fixups_do_not_use_up_the_cap(tmp_path: Path) -> None:
	"""Five siblings with open pull requests sit nearest the root; the sixth still closes."""
	issues = [_fixup(number, 144) for number in range(145, 151)]
	timelines = {number: [_cross(900 + number, "open")] for number in range(145, 150)}
	result, calls = _run_sweep(tmp_path, [issues], _gql(**{"p144": {"state": "CLOSED", "state_reason": "COMPLETED", "closed_at": _iso(72)}}), timelines)
	assert [line.split()[3] for line in _closes(calls)] == ["150"]
	assert "closed=1 held=5" in result.stdout


def test_sweep_failed_timeline_read_closes_nothing_this_tick(tmp_path: Path) -> None:
	result, calls = _run_sweep(tmp_path, [_chain(144, 3)], _gql(**{"p144": {"state": "CLOSED", "state_reason": "COMPLETED", "closed_at": _iso(72)}}), timeline_fails=[146])
	assert _closes(calls) == []
	assert "item=146 parent=145 root=144 outcome=skip reason=timeline_fetch_failed" in result.stdout
	assert "outcome=done" not in result.stdout and "rc=0" in result.stdout


def test_sweep_keeps_the_other_aliases_when_one_number_is_not_found(tmp_path: Path) -> None:
	graphql = _gql(**{"p144": {"state": "CLOSED", "state_reason": "COMPLETED", "closed_at": _iso(72)}, "p999": None})
	graphql["errors"] = [{"type": "NOT_FOUND", "path": ["repository", "p999"]}]
	issues = _chain(144, 1) + [_fixup(300, 999)]
	result, calls = _run_sweep(tmp_path, [issues], graphql, graphql_fails=True)
	assert [line.split()[3] for line in _closes(calls)] == ["145"]
	other = tmp_path / "other"
	other.mkdir()
	graphql["errors"] = [{"type": "RATE_LIMITED"}]
	result, calls = _run_sweep(other, [issues], graphql, graphql_fails=True)
	assert "outcome=skip reason=parent_lookup_failed" in result.stdout and _closes(calls) == []


def test_sweep_dry_run_and_kill_switch_change_nothing(tmp_path: Path) -> None:
	graphql = _gql(**{"p144": {"state": "CLOSED", "state_reason": "COMPLETED", "closed_at": _iso(72)}})
	result, calls = _run_sweep(tmp_path, [_chain(144, 2)], graphql, env={"ORPHAN_FIXUP_CLOSE_DRY_RUN": "true"})
	assert _closes(calls) == [] and result.stdout.count("outcome=would_close") == 2
	off = tmp_path / "off"
	off.mkdir()
	result, calls = _run_sweep(off, [_chain(144, 2)], graphql, env={"ORPHAN_FIXUP_CLOSE_ENABLED": "false"})
	assert calls == [] and "ORPHAN_FIXUP_CLOSE outcome=skip reason=disabled" in result.stdout


def test_sweep_honours_the_per_tick_cap_and_survives_bad_numbers(tmp_path: Path) -> None:
	graphql = _gql(**{"p144": {"state": "CLOSED", "state_reason": "COMPLETED", "closed_at": _iso(72)}})
	result, calls = _run_sweep(tmp_path, [_chain(144, 8)], graphql, env={"ORPHAN_FIXUP_CLOSE_MAX_PER_TICK": "2"})
	assert [line.split()[3] for line in _closes(calls)] == ["145", "146"]
	bad = tmp_path / "bad"
	bad.mkdir()
	result, calls = _run_sweep(bad, [_chain(144, 8)], graphql, env={"ORPHAN_FIXUP_CLOSE_MAX_PER_TICK": "lots", "ORPHAN_FIXUP_CLOSE_GRACE_MINUTES": "soon", "ORPHAN_FIXUP_CLOSE_MAX_PAGES": "0"})
	assert len(_closes(calls)) == 5


def test_sweep_fails_closed_on_failed_reads_and_failed_close(tmp_path: Path) -> None:
	result, calls = _run_sweep(tmp_path, [_chain(144, 2)], None, list_fails=True)
	assert "outcome=skip reason=list_failed page=1" in result.stdout and _closes(calls) == []
	lookup = tmp_path / "lookup"
	lookup.mkdir()
	result, calls = _run_sweep(lookup, [_chain(144, 2)], None)
	assert "outcome=skip reason=parent_lookup_failed" in result.stdout and _closes(calls) == []
	failing = tmp_path / "failing"
	failing.mkdir()
	graphql = _gql(**{"p144": {"state": "CLOSED", "state_reason": "COMPLETED", "closed_at": _iso(72)}})
	result, calls = _run_sweep(failing, [_chain(144, 2)], graphql, close_fails=True)
	assert "outcome=close_failed" in result.stdout and "closed=0" in result.stdout and result.returncode == 0


def test_sweep_pages_through_a_full_first_page(tmp_path: Path) -> None:
	filler = [_plain(1000 + number) for number in range(99)]
	page1 = filler + [_plain(1)]
	page2 = _chain(144, 2)
	graphql = _gql(**{"p144": {"state": "CLOSED", "state_reason": "COMPLETED", "closed_at": _iso(72)}})
	result, calls = _run_sweep(tmp_path, [page1, page2], graphql)
	assert sum(1 for line in calls if "issues?state=open&per_page=100&page=" in line) == 2
	assert [line.split()[3] for line in _closes(calls)] == ["145", "146"]
	capped = tmp_path / "capped"
	capped.mkdir()
	result, calls = _run_sweep(capped, [page1, page2], graphql, env={"ORPHAN_FIXUP_CLOSE_MAX_PAGES": "1"})
	assert sum(1 for line in calls if "issues?state=open&per_page=100&page=" in line) == 1 and _closes(calls) == []


def test_sweep_drops_pull_requests_from_the_open_list(tmp_path: Path) -> None:
	pull = _fixup(500, 144)
	pull["pull_request"] = {"url": "https://example.test/pull/500"}
	result, calls = _run_sweep(tmp_path, [[pull]], None)
	assert "selected=0" in result.stdout and len(calls) == 1
