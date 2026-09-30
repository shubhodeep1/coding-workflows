"""Contract for scripts/claude_checker_restart.py — the dead-checker restart
the hourly Claude issue pickup runs (issue #4910, operator rule Q63)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "claude_checker_restart.py"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("claude_checker_restart", SCRIPT_PATH)
restart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(restart)

NOW = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.timezone.utc)
REPO = "o/r"
PICKUP = "session_pickup"
SLUG = "issue-7-fix-thing"
CHECKER = "session_checker7"
LONG_AGO = "2026-09-28T06:00:00Z"  # 30h before NOW
HOURS_AGO = "2026-09-29T09:00:00Z"  # 3h before NOW
MINUTES_AGO = "2026-09-29T11:30:00Z"  # 30 minutes before NOW


def _ago(**delta) -> str:
	return (NOW - dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session(
	session_id,
	title,
	status="SESSION_STATUS_IDLE",
	bucket="SESSION_STATUS_BUCKET_REVIEW_READY",
	category="review_ready",
	created=LONG_AGO,
	updated=LONG_AGO,
	parent="",
	branch=None,
	tags=(),
	repo=REPO,
):
	return {
		"id": session_id,
		"title": title,
		"session_status": status,
		"status_bucket": bucket,
		"created_at": created,
		"updated_at": updated,
		"parent_session_id": parent,
		"tags": list(tags),
		"session_context": {"sources": [{"git_repository": {"url": f"https://github.com/{repo}"}}]},
		"post_turn_summary": {"status_category": category},
		"external_metadata": {"current_branches": {"": branch}},
	}


def _checker(session_id=CHECKER, slug=SLUG, **kwargs):
	return _session(session_id, f"implement-plan {slug} — checker", **kwargs)


def _trigger(trigger_id, name, session, prompt="", enabled=True):
	return {"id": trigger_id, "name": name, "enabled": enabled, "persistent_session_id": session, "derived_state": {"prompt": prompt}}


def _safety_net(stage, checker=CHECKER, slug=SLUG, trigger_id="trig_net"):
	return _trigger(
		trigger_id,
		f"implement-plan {slug}: safety net",
		stage,
		f"Safety net for /implement-plan-claude docs/plans/{slug}-plan.md: the next stage never started. "
		f"get_session {checker}; if it is blocked, archived, or idle with no pending check-in, …",
	)


_PAGE_RECORDS: dict = {}  # raw page records of the last `_state`, for `_decide`'s lookups


def _state(sessions=(), triggers=(), open_issues=None, log_projects=(), has_more=False):
	_PAGE_RECORDS.clear()
	_PAGE_RECORDS.update({raw["id"]: raw for raw in sessions})
	return {
		"repo": REPO,
		"self": PICKUP,
		"sessions": [restart.session_view(raw) for raw in sessions],
		"triggers": [restart.trigger_view(raw) for raw in triggers],
		"triggers_has_more": has_more,
		"open_issues": {"7": ["ai:claude"]} if open_issues is None else open_issues,
		"log_projects": list(log_projects),
		"errors": [],
	}


@pytest.fixture
def no_api(monkeypatch):
	"""Fail on any gh api call the test did not expect."""
	calls = []

	def fake(path, paginate=False):
		calls.append(path)
		raise AssertionError(f"unexpected gh api call: {path}")

	monkeypatch.setattr(restart, "gh_api", fake)
	return calls


def _stub(monkeypatch, responses):
	calls = []

	def fake(path, paginate=False):
		calls.append(path)
		for prefix, payload in responses.items():
			if path.startswith(prefix):
				if isinstance(payload, Exception):
					raise payload
				return payload
		raise AssertionError(f"unexpected gh api call: {path}")

	monkeypatch.setattr(restart, "gh_api", fake)
	return calls


def _decide(state, lookups=None, look_up_page=True):
	"""Decide as the pickup does: every page-listed id `lookups_needed` names is looked up, and the
	lookup returns the session's page record unchanged. `lookups` adds or overrides entries."""
	merged = {}
	if look_up_page:
		for checker in restart.lookups_needed(state, NOW):
			if checker in _PAGE_RECORDS:
				merged[checker] = {"ccr": _PAGE_RECORDS[checker]}
	merged.update(lookups or {})
	return restart.decide(state, merged, NOW)


def _reasons(result):
	return {item["checker"]: item["reason"] for item in result["skipped"]}


# --- the restart rule: all five conditions -------------------------------------------------


def test_dead_checker_with_nothing_active_is_restarted(no_api):
	result = _decide(_state([_checker()]))
	assert result["skipped"] == []
	[entry] = result["restart"]
	assert entry["checker"] == CHECKER
	assert entry["slug"] == SLUG
	assert entry["repo"] == REPO
	assert entry["trigger_name"] == f"implement-plan {SLUG}: check-in"
	assert entry["prompt"] == (
		"Check-in from the Claude issue pickup: no check-in is pending for you and no stage of this project "
		"has been active for 90 minutes. Repeat the steps in your most recent checker-instructions message "
		"now, starting at step 1 (step 0's stale-wake check still applies)."
	)
	assert entry["tag_add"] == "ai-checker-restart:20260929T1200Z"
	assert entry["tag_remove"] == []
	assert no_api == []  # issue 7 is in the open-issue list: no extra read


def test_condition_1_a_pending_trigger_keeps_the_checker(no_api):
	trigger = _trigger("trig_c", f"implement-plan {SLUG}: check-in", CHECKER)
	result = _decide(_state([_checker()], [trigger]))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == "has_pending_trigger"


def test_condition_1_a_disabled_trigger_does_not_count(no_api):
	trigger = _trigger("trig_c", f"implement-plan {SLUG}: check-in", CHECKER, enabled=False)
	assert len(_decide(_state([_checker()], [trigger]))["restart"]) == 1


def test_an_incomplete_trigger_page_restarts_nothing(no_api):
	result = _decide(_state([_checker()], has_more=True))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == "triggers_page_incomplete"


@pytest.mark.parametrize(
	("open_issues", "reason"),
	[({"7": ["ai:claude", "ai:claude-blocked"]}, "issue_blocked")],
)
def test_condition_2_a_blocked_issue_keeps_the_checker(no_api, open_issues, reason):
	result = _decide(_state([_checker()], open_issues=open_issues))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == reason


def test_condition_2_an_issue_outside_the_open_list_is_read_once(monkeypatch):
	calls = _stub(monkeypatch, {"repos/o/r/issues/7": {"state": "closed", "labels": []}})
	result = _decide(_state([_checker()], open_issues={}))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == "issue_closed"
	assert calls == ["repos/o/r/issues/7"]


def test_condition_2_an_open_issue_without_the_label_still_restarts(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/issues/7": {"state": "open", "labels": [{"name": "bug"}]}})
	assert len(_decide(_state([_checker()], open_issues={}))["restart"]) == 1


def test_condition_2_a_failed_issue_read_keeps_the_checker(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/issues/7": restart.ReadError("gh api repos/o/r/issues/7 failed: HTTP 403")})
	result = _decide(_state([_checker()], open_issues={}))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == "issue_read_failed"
	assert "HTTP 403" in result["errors"][0]


def test_condition_2_does_not_apply_to_a_plan_slug(no_api):
	result = _decide(_state([_checker(slug="heal-autofix")]))
	assert [entry["slug"] for entry in result["restart"]] == ["heal-autofix"]


@pytest.mark.parametrize(
	("kwargs", "reason"),
	[
		({"category": "need_input", "bucket": "SESSION_STATUS_BUCKET_BLOCKED"}, "checker_not_idle"),
		({"category": "need_input"}, "checker_needs_input"),
		({"status": "SESSION_STATUS_REQUIRES_ACTION", "bucket": "SESSION_STATUS_BUCKET_BLOCKED"}, "checker_not_idle"),
		({"status": "SESSION_STATUS_RUNNING", "bucket": "SESSION_STATUS_BUCKET_WORKING"}, "checker_not_idle"),
	],
)
def test_condition_3_a_waiting_or_running_checker_is_kept(no_api, kwargs, reason):
	result = _decide(_state([_checker(**kwargs)]))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER].startswith(reason)


def test_condition_4_the_4787_case_an_unmatched_child_stage_created_30_minutes_ago(no_api):
	"""Issue #4910 evidence: lineage, not the title, ties the stage to the project."""
	child = _session("session_stage", "conformance stage (renamed)", created=MINUTES_AGO, updated=MINUTES_AGO, parent=CHECKER)
	result = _decide(_state([child, _checker()]))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == "project_session_active: session_stage"


def test_condition_4_an_idle_session_created_30_minutes_ago_counts_as_active(no_api):
	child = _session("session_stage", "x", created=MINUTES_AGO, updated=HOURS_AGO, parent=CHECKER)
	assert _decide(_state([child, _checker()]))["restart"] == []


@pytest.mark.parametrize(
	"member",
	[
		{"parent": CHECKER},
		{"branch": f"claude/implement-plan-{SLUG}"},
		{"branch": f"claude/implement-plan-{SLUG}-phase-1"},
		{"branch": f"claude/implement-plan-{SLUG}-conformance-fix-2"},
		{"title": f"implement-plan {SLUG} — phase 1/1 — review round"},
		{"title": f"#7 · PR #9 — implement-plan {SLUG} — conformance 1/3"},
		{"title": "#7 · PR #9 — fixer"},
		{"title": "Issue #7 — implement"},
		{"title": "issue o/r#7 — implement"},
		{"title": "implement-issue-claude — #7"},
	],
)
def test_condition_4_membership_by_lineage_branch_and_title(no_api, member):
	title = member.pop("title", "unrelated")
	stage = _session("session_stage", title, status="SESSION_STATUS_RUNNING", bucket="SESSION_STATUS_BUCKET_WORKING", **member)
	result = _decide(_state([stage, _checker()]))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == "project_session_active: session_stage"


@pytest.mark.parametrize(
	"title",
	["Issue #70 — implement", "#70 · PR #9 — x", "implement-plan issue-7-fix-thing-else — phase 1/1", "PR o/r#7 — fix review"],
)
def test_condition_4_other_projects_do_not_block(no_api, title):
	stage = _session("session_other", title, status="SESSION_STATUS_RUNNING", bucket="SESSION_STATUS_BUCKET_WORKING")
	assert len(_decide(_state([stage, _checker()]))["restart"]) == 1


@pytest.mark.parametrize(
	"kwargs",
	[
		{"status": "SESSION_STATUS_RUNNING", "bucket": "SESSION_STATUS_BUCKET_WORKING"},
		{"status": "SESSION_STATUS_REQUIRES_ACTION", "bucket": "SESSION_STATUS_BUCKET_BLOCKED"},
		{"category": "need_input", "bucket": "SESSION_STATUS_BUCKET_BLOCKED"},
		{"updated": MINUTES_AGO},
	],
)
def test_condition_4_active_states(no_api, kwargs):
	stage = _session("session_stage", f"implement-plan {SLUG} — phase 1/1", **kwargs)
	assert _decide(_state([stage, _checker()]))["restart"] == []


def test_condition_4_archived_and_quiet_project_sessions_do_not_block(no_api):
	archived = _session("session_old", f"implement-plan {SLUG} — phase 1/1", status="SESSION_STATUS_ARCHIVED", category="need_input", updated=MINUTES_AGO)
	quiet = _session("session_quiet", f"implement-plan {SLUG} — phase 1/1", updated=HOURS_AGO, created=HOURS_AGO)
	assert len(_decide(_state([archived, quiet, _checker()]))["restart"]) == 1


def test_condition_4_the_pickup_itself_never_counts(no_api):
	pickup = _session(PICKUP, "Claude issue pickup — last wake", status="SESSION_STATUS_RUNNING", parent=CHECKER)
	assert len(_decide(_state([pickup, _checker()]))["restart"]) == 1


@pytest.mark.parametrize("kwargs", [{"updated": MINUTES_AGO}, {"created": MINUTES_AGO, "updated": MINUTES_AGO}])
def test_condition_4_the_checker_own_recent_activity_keeps_it(no_api, kwargs):
	"""A checker that re-armed after step 1's trigger page was read looks idle with no trigger."""
	result = _decide(_state([_checker(**kwargs)]))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == f"checker_active_recently: {MINUTES_AGO}"


def test_condition_4_the_checker_own_activity_older_than_the_window_does_not_count(no_api):
	assert len(_decide(_state([_checker(updated=_ago(minutes=91))]))["restart"]) == 1


def test_condition_4_a_looked_up_checker_that_handed_off_after_the_page_is_kept(no_api):
	"""Live case (2026-09-30, #4723): the checker started its next stage after the session page
	was read, then went idle. The new stage is not on the page and no trigger is bound to the
	checker, so only the checker's own `updated_at` shows it is alive."""
	stage = _session("session_stage", f"implement-plan {SLUG} — final-merge 1/1", updated=HOURS_AGO)
	state = _state([stage], [_safety_net("session_stage")])
	assert restart.lookups_needed(state, NOW) == [CHECKER]
	result = _decide(state, {CHECKER: {"ccr": _checker(updated=_ago(minutes=2))}})
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == f"checker_active_recently: {_ago(minutes=2)}"


def test_condition_5_a_recent_restart_tag_keeps_the_checker(no_api):
	result = _decide(_state([_checker(tags=["config:session-created", "ai-checker-restart:20260929T1000Z"])]))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == "restarted_recently: 20260929T1000Z"


def test_condition_5_an_old_restart_tag_is_replaced(no_api):
	tags = ["ai-checker-restart:20260929T0800Z", "ai-checker-restart:20260928T0100Z", "ai-checker-restart:bogus"]
	[entry] = _decide(_state([_checker(tags=tags)]))["restart"]
	assert entry["tag_add"] == "ai-checker-restart:20260929T1200Z"
	assert entry["tag_remove"] == tags


def test_a_zombie_checker_with_a_live_sibling_is_skipped(no_api):
	live = _checker("session_live", updated=HOURS_AGO)
	trigger = _trigger("trig_c", f"implement-plan {SLUG}: check-in", "session_live")
	result = _decide(_state([live, _checker()], [trigger]))
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == "sibling_checker_alive: session_live"


# --- checker titles -----------------------------------------------------------------------


@pytest.mark.parametrize(
	("title", "slug"),
	[
		(f"implement-plan {SLUG} — checker", SLUG),
		(f"#7 · PR #9 — implement-plan {SLUG} — checker", SLUG),
		(f"#7 · implement-plan {SLUG} — checker", SLUG),
		(f"PR #9 — implement-plan {SLUG} — checker", SLUG),
		("implement-plan heal.autofix_v2 — checker", "heal.autofix_v2"),
		(f"implement-plan {SLUG} — checker (old)", ""),
		(f"implement-plan {SLUG} — waiting: PR #9", ""),
		(f"implement-plan {SLUG} — phase 1/1", ""),
		("PR #9 status check-in", ""),
	],
)
def test_checker_titles(title, slug):
	assert restart.checker_slug(title) == slug


def test_archived_checkers_are_not_candidates(no_api):
	result = _decide(_state([_checker(status="SESSION_STATUS_ARCHIVED")]))
	assert result == {"restart": [], "requeue": [], "skipped": [], "errors": []}


# --- older checkers: safety nets, logs, lookups --------------------------------------------


def test_a_safety_net_names_a_checker_older_than_the_page():
	stage = _session("session_stage", f"implement-plan {SLUG} — phase 1/1", updated=HOURS_AGO)
	state = _state([stage], [_safety_net("session_stage")])
	assert restart.lookups_needed(state, NOW) == [CHECKER]
	result = _decide(state, {CHECKER: {"ccr": _checker()}})
	assert [entry["checker"] for entry in result["restart"]] == [CHECKER]
	assert "safety_net" in result["restart"][0]["reason"]


def test_a_looked_up_checker_is_held_to_the_same_rules(no_api):
	state = _state([], [_safety_net("session_stage")])
	result = _decide(state, {CHECKER: {"ccr": _checker(category="need_input")}})
	assert result["restart"] == []
	assert _reasons(result)[CHECKER] == "checker_needs_input"


def test_a_lookup_that_is_not_a_checker_is_skipped(no_api):
	state = _state([], [_safety_net("session_stage")])
	result = _decide(state, {CHECKER: _session(CHECKER, "Issue #7 — implement")})
	assert _reasons(result)[CHECKER] == "not_a_checker_title"


def test_a_missing_or_failed_lookup_keeps_the_checker(no_api):
	state = _state([], [_safety_net("session_stage")])
	assert _reasons(_decide(state))[CHECKER] == "not_looked_up"
	failed = _decide(state, {CHECKER: {"error": "classifier unavailable"}})
	assert _reasons(failed)[CHECKER] == "not_looked_up"
	assert failed["errors"] == [f"{CHECKER}: get_session failed: classifier unavailable"]
	assert _reasons(_decide(state, {CHECKER: "not_found"}))[CHECKER] == "checker_not_found"


@pytest.mark.parametrize(
	"kwargs",
	[
		{"updated": MINUTES_AGO},
		{"status": "SESSION_STATUS_RUNNING"},
		{"category": "need_input"},
		{"bucket": "SESSION_STATUS_BUCKET_BLOCKED"},
		{"tags": ["ai-checker-restart:20260929T1000Z"]},
		{"status": "SESSION_STATUS_ARCHIVED"},
	],
)
def test_no_lookup_for_a_page_listed_checker_whose_record_already_keeps_it(kwargs):
	state = _state([_checker(**kwargs)], [_safety_net("session_stage")])
	assert restart.lookups_needed(state, NOW) == []
	assert restart.lookups_needed(_state([_checker()], has_more=True), NOW) == []


def test_a_page_listed_checker_that_would_restart_is_looked_up():
	assert restart.lookups_needed(_state([_checker()]), NOW) == [CHECKER]


def test_a_page_listed_checker_is_restarted_only_from_a_fresh_record(no_api):
	"""Review round 1 on PR #5598: the page is read before the lookups, so its record misses a
	turn the checker took since. Without a lookup the checker is kept, and a lookup showing that
	turn keeps it too."""
	state = _state([_checker()])
	kept = _decide(state, look_up_page=False)
	assert kept["restart"] == []
	assert _reasons(kept)[CHECKER] == "not_looked_up"
	fresh = _decide(state, {CHECKER: {"ccr": _checker(updated=_ago(minutes=2))}})
	assert fresh["restart"] == []
	assert _reasons(fresh)[CHECKER] == f"checker_active_recently: {_ago(minutes=2)}"
	stale = _decide(state, {CHECKER: {"ccr": _checker()}})
	assert [entry["checker"] for entry in stale["restart"]] == [CHECKER]


def test_a_failed_lookup_of_a_page_listed_checker_keeps_it(no_api):
	state = _state([_checker()])
	failed = _decide(state, {CHECKER: {"error": "classifier unavailable"}})
	assert failed["restart"] == []
	assert _reasons(failed)[CHECKER] == "not_looked_up"
	assert failed["errors"] == [f"{CHECKER}: get_session failed: classifier unavailable"]
	gone = _decide(state, {CHECKER: "not_found"})
	assert gone["restart"] == [] and gone["requeue"] == []
	assert _reasons(gone)[CHECKER] == "not_looked_up"
	assert gone["errors"] == [f"{CHECKER}: get_session reports not found for a session on the page"]


def test_no_lookup_for_checkers_bound_or_blocked():
	bound =_state([], [_safety_net("session_stage"), _trigger("trig_c", "x", CHECKER)])
	assert restart.lookups_needed(bound, NOW) == []
	blocked = _state([], [_safety_net("session_stage")], open_issues={"7": ["ai:claude", "ai:claude-blocked"]})
	assert restart.lookups_needed(blocked, NOW) == []
	active = _state(
		[_session("session_stage", f"implement-plan {SLUG} — phase 1/1", status="SESSION_STATUS_RUNNING")],
		[_safety_net("session_stage")],
	)
	assert restart.lookups_needed(active, NOW) == []


def test_lookups_are_capped_and_rotated_by_the_hour():
	nets = [_safety_net(f"session_s{n}", checker=f"session_c{n:02d}", slug=f"plan-{n}", trigger_id=f"trig_{n}") for n in range(12)]
	state = _state([], nets)
	first = restart.lookups_needed(state, NOW)
	later = restart.lookups_needed(state, NOW + dt.timedelta(hours=1))
	assert len(first) == len(later) == restart.LOOKUP_CAP == 8
	assert first != later
	seen = set()
	for hour in range(12):
		seen.update(restart.lookups_needed(state, NOW + dt.timedelta(hours=hour)))
	assert len(seen) == 12


def test_safety_net_prompt_parsing():
	state = _state([], [_safety_net("session_stage", slug="plan-x")])
	state["triggers"][0]["prompt"] = state["triggers"][0]["prompt"].replace("docs/plans/plan-x-plan.md", "docs/completed/plan-x-plan.md")
	assert restart.collect_candidates(state) == {CHECKER: {"slug": "plan-x", "sources": ["safety_net"], "issue": None}}


def test_hand_back_and_other_routines_name_no_checker():
	hand_back = _trigger("trig_h", f"implement-plan {SLUG}: hand-back", "session_stage", f"Hand-back for /implement-plan-claude docs/plans/{SLUG}-plan.md, PR #9")
	other = _trigger("trig_o", "PR #9 status check-in", "session_x", "get_session session_zzz")
	assert restart.collect_candidates(_state([], [hand_back, other])) == {}


# --- re-queue: a logged checker that no longer exists --------------------------------------


LOG_PROJECT = {"slug": SLUG, "issue": 7, "branch": f"claude/implement-plan-{SLUG}", "checker": CHECKER}


def _requeue_state(**kwargs):
	return _state(log_projects=[LOG_PROJECT], **kwargs)


def test_a_missing_logged_checker_re_queues_the_issue_once(monkeypatch):
	calls = _stub(monkeypatch, {"repos/o/r/issues/7/comments": []})
	result = _decide(_requeue_state(), {CHECKER: "not_found"})
	[entry] = result["requeue"]
	assert entry["repo"] == REPO and entry["issue"] == 7 and entry["slug"] == SLUG and entry["checker"] == CHECKER
	body = entry["comment_body"]
	assert body.startswith("/reclarify\n")
	assert f"<!-- ai:claude-checker-requeue:v1 checker={CHECKER} slug={SLUG} -->" in body
	assert "'" not in body  # the pickup may pass it in a single-quoted shell argument
	assert calls == ["repos/o/r/issues/7/comments?since=2026-09-28T12:00:00Z&per_page=100"]
	assert result["restart"] == []


@pytest.mark.parametrize(
	("comment", "blocks"),
	[
		({"user": {"type": "User"}, "author_association": "OWNER", "body": "/reclarify", "created_at": "2026-09-29T01:00:00Z"}, True),
		({"user": {"type": "User"}, "author_association": "NONE", "body": "/reclarify", "created_at": "2026-09-29T01:00:00Z"}, False),
		({"user": {"type": "Bot"}, "author_association": "OWNER", "body": "/reclarify", "created_at": "2026-09-29T01:00:00Z"}, False),
		({"user": {"type": "User"}, "author_association": "OWNER", "body": "please /reclarify", "created_at": "2026-09-29T01:00:00Z"}, False),
		({"user": {"type": "User"}, "author_association": "OWNER", "body": "/reclarify", "created_at": "2026-09-28T11:00:00Z"}, False),
	],
)
def test_re_queue_at_most_once_per_24_hours(monkeypatch, comment, blocks):
	_stub(monkeypatch, {"repos/o/r/issues/7/comments": [comment]})
	result = _decide(_requeue_state(), {CHECKER: "not_found"})
	if blocks:
		assert result["requeue"] == []
		assert _reasons(result)[CHECKER] == "requeued_recently"
	else:
		assert len(result["requeue"]) == 1


@pytest.mark.parametrize(
	("kwargs", "reason"),
	[
		({"open_issues": {"7": ["ai:claude", "ai:claude-blocked"]}}, "issue_blocked"),
		({"open_issues": {}}, "issue_not_open_claude"),
		({"sessions": [_session("session_new", "Issue #7 — implement", created=MINUTES_AGO)]}, "project_session_active: session_new"),
		({"sessions": [_checker("session_new", updated=HOURS_AGO)]}, "other_checker_exists: session_new"),
		({"triggers": [_trigger("trig_x", "implement-plan issue-7-fix-th…", "session_s")]}, "project_trigger_pending: trig_x"),
		({"triggers": [_trigger("trig_y", "anything", "session_s", f"Hand-back for /implement-plan-claude docs/plans/{SLUG}-plan.md")]}, "project_trigger_pending: trig_y"),
	],
)
def test_re_queue_guards(no_api, kwargs, reason):
	result = _decide(_requeue_state(**kwargs), {CHECKER: "not_found"})
	assert result["requeue"] == []
	assert _reasons(result)[CHECKER] == reason


@pytest.mark.parametrize(
	"triggers",
	[
		[],
		[_trigger("trig_other", "PR #12 status check-in", "session_s")],
		# A project trigger on the page too: the incomplete-page guard runs first, so a reordering is caught.
		[_trigger("trig_x", "implement-plan issue-7-fix-th…", "session_s")],
	],
)
def test_an_incomplete_trigger_page_re_queues_nothing(no_api, triggers):
	"""A project trigger on the next page (a stage start, a hand-back) cannot be ruled out (§1, AD-8)."""
	result = _decide(_requeue_state(triggers=triggers, has_more=True), {CHECKER: "not_found"})
	assert result["requeue"] == []
	assert _reasons(result)[CHECKER] == "triggers_page_incomplete"


def test_re_queue_comment_read_failure_keeps_the_issue(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/issues/7/comments": restart.ReadError("gh api failed: HTTP 502")})
	result = _decide(_requeue_state(), {CHECKER: "not_found"})
	assert result["requeue"] == []
	assert _reasons(result)[CHECKER] == "comment_read_failed"
	assert result["errors"]


def test_a_logged_checker_that_exists_follows_the_restart_rule(no_api):
	result = _decide(_requeue_state(), {CHECKER: {"ccr": _checker()}})
	assert result["requeue"] == []
	assert [entry["checker"] for entry in result["restart"]] == [CHECKER]


def test_only_a_logged_checker_is_re_queued(no_api):
	result = _decide(_state([], [_safety_net("session_stage")]), {CHECKER: "not_found"})
	assert result["requeue"] == []


def test_two_logs_for_one_issue_re_queue_it_once(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/issues/7/comments": []})
	second = dict(LOG_PROJECT, slug="issue-7-fix-thing-2", branch="claude/implement-plan-issue-7-fix-thing-2", checker="session_gone2")
	state = _state(log_projects=[LOG_PROJECT, second])
	result = _decide(state, {CHECKER: "not_found", "session_gone2": "not_found"})
	assert len(result["requeue"]) == 1
	assert "requeue_already_listed" in _reasons(result).values()


# --- scan: the open-issue list and the logs ------------------------------------------------


LOG_TEXT = f"""# Implement-Plan Log — Fix thing

- Plan: docs/plans/{SLUG}-plan.md
- Source issue: o/r#7 (https://github.com/o/r/issues/7)
- Status: IN_PROGRESS
- Stage: final-merge
- Activation: not started
- Check-in: checker {CHECKER}   safety net and hand-back in the stage report
"""


def test_parse_log():
	assert restart.parse_log(LOG_TEXT) == {"source_issue": "o/r#7 (https://github.com/o/r/issues/7)", "status": "IN_PROGRESS", "activation": "not started", "checker": CHECKER}
	assert restart.parse_log("- Check-in: none — see Notes")["checker"] == ""
	assert restart.parse_log("- Check-in: none (project checker session_01Abc kept idle)")["checker"] == "session_01Abc"


def _scan(monkeypatch, issues, branches, logs):
	calls = _stub(monkeypatch, {"repos/o/r/issues?labels=ai:claude&state=open&per_page=100": issues})
	requested = []
	monkeypatch.setattr(restart, "list_project_branches", lambda: list(branches))

	def fake_logs(wanted, errors=None):
		requested.append(list(wanted))
		return {branch: logs[branch] for branch in wanted if branch in logs}

	monkeypatch.setattr(restart, "read_project_logs", fake_logs)
	errors = []
	open_issues, projects = restart.scan_logs(REPO, errors)
	return open_issues, projects, errors, calls, requested


def test_scan_reads_logs_of_open_unblocked_claude_issues_only(monkeypatch):
	issues = [
		{"number": 7, "labels": [{"name": "ai:claude"}]},
		{"number": 8, "labels": [{"name": "ai:claude"}, {"name": "ai:claude-blocked"}]},
		{"number": 9, "labels": [{"name": "ai:claude"}], "pull_request": {}},
	]
	branches = [f"claude/implement-plan-{SLUG}", "claude/implement-plan-issue-8-blocked", "claude/implement-plan-issue-70-other"]
	open_issues, projects, errors, calls, requested = _scan(monkeypatch, issues, branches, {f"claude/implement-plan-{SLUG}": LOG_TEXT})
	assert open_issues == {"7": ["ai:claude"], "8": ["ai:claude", "ai:claude-blocked"]}
	assert requested == [[f"claude/implement-plan-{SLUG}"]]
	assert projects == [LOG_PROJECT]
	assert errors == []
	assert len(calls) == 1


@pytest.mark.parametrize(
	"edit",
	[
		("- Status: IN_PROGRESS", "- Status: BLOCKED"),
		("- Activation: not started", "- Activation: LIVE"),
		("- Activation: not started", "- Activation: n/a (base stable)"),
		("- Activation: not started", "- Activation: deploy-activate started (session_x)"),
		("o/r#7", "o/r#77"),
		("o/r#7", "other/repo#7"),
	],
)
def test_scan_skips_blocked_finished_and_foreign_logs(monkeypatch, edit):
	text = LOG_TEXT.replace(*edit)
	_, projects, _, _, _ = _scan(monkeypatch, [{"number": 7, "labels": [{"name": "ai:claude"}]}], [f"claude/implement-plan-{SLUG}"], {f"claude/implement-plan-{SLUG}": text})
	assert projects == []


def test_scan_keeps_a_complete_log_waiting_at_final_merge(monkeypatch):
	"""The #4552 incident: Status COMPLETE, Stage final-merge, the final PR not merged yet."""
	text = LOG_TEXT.replace("IN_PROGRESS", "COMPLETE").replace("not started", "pending verify-activation")
	_, projects, _, _, _ = _scan(monkeypatch, [{"number": 7, "labels": [{"name": "ai:claude"}]}], [f"claude/implement-plan-{SLUG}"], {f"claude/implement-plan-{SLUG}": text})
	assert projects == [LOG_PROJECT]


def test_scan_fails_open(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/issues?": restart.ReadError("HTTP 502")})
	errors = []
	assert restart.scan_logs(REPO, errors) == ({}, [])
	assert errors == ["open issue list: HTTP 502"]

	def broken():
		raise restart.ReadError("git ls-remote failed: exit 128")

	_stub(monkeypatch, {"repos/o/r/issues?": [{"number": 7, "labels": [{"name": "ai:claude"}]}]})
	monkeypatch.setattr(restart, "list_project_branches", broken)
	errors = []
	open_issues, projects = restart.scan_logs(REPO, errors)
	assert open_issues == {"7": ["ai:claude"]} and projects == []
	assert errors == ["project logs: git ls-remote failed: exit 128"]


def test_list_project_branches_drops_sub_branches(monkeypatch):
	listing = "\n".join(
		f"abc\trefs/heads/claude/implement-plan-{name}"
		for name in (
			SLUG,
			f"{SLUG}-phase-1",
			f"{SLUG}-phase-1-2",
			f"{SLUG}-conformance-fix-2",
			f"{SLUG}-validation-fix-1",
			f"{SLUG}-activation-fix-1",
			f"{SLUG}-complete",
			f"{SLUG}-decision-changes",
			"issue-8-other",
		)
	)
	monkeypatch.setattr(restart, "_git", lambda args, timeout=180: listing)
	assert restart.list_project_branches() == [f"claude/implement-plan-{SLUG}", "claude/implement-plan-issue-8-other"]


def test_read_project_logs_uses_one_ls_remote_and_one_fetch(monkeypatch):
	calls = []

	def fake_git(args, timeout=180):
		calls.append(args[0])
		if args[0] == "ls-remote":
			return "sha1\trefs/heads/claude/implement-plan-a\nsha2\trefs/heads/claude/implement-plan-b\n"
		if args[0] == "fetch":
			return ""
		if args[0] == "ls-tree":
			return ""
		if args[1] == "sha1:docs/implement-plan/a.md":
			return "log a"
		raise restart.ReadError("git show failed: path does not exist")

	monkeypatch.setattr(restart, "_git", fake_git)
	errors = []
	assert restart.read_project_logs(["claude/implement-plan-a", "claude/implement-plan-b", "claude/implement-plan-gone"], errors) == {"claude/implement-plan-a": "log a"}
	assert calls == ["ls-remote", "fetch", "show", "show", "ls-tree"]
	assert errors == []
	assert restart.read_project_logs([]) == {}


def test_read_project_logs_reports_a_failed_show_of_an_existing_log(monkeypatch):
	calls = []

	def fake_git(args, timeout=180):
		calls.append(args)
		if args[0] == "ls-remote":
			return "sha1\trefs/heads/claude/implement-plan-a\nsha2\trefs/heads/claude/implement-plan-b\nsha3\trefs/heads/claude/implement-plan-c\n"
		if args[0] == "fetch":
			return ""
		if args[0] == "ls-tree":
			if args[2] == "sha3":
				raise restart.ReadError("git ls-tree failed: fatal: not a tree object")
			return f"{args[-1]}\n"
		if args[1] == "sha1:docs/implement-plan/a.md":
			return "log a"
		raise restart.ReadError(f"git show {args[1]} failed: fatal: bad object")

	monkeypatch.setattr(restart, "_git", fake_git)
	errors = []
	branches = ["claude/implement-plan-a", "claude/implement-plan-b", "claude/implement-plan-c"]
	assert restart.read_project_logs(branches, errors) == {"claude/implement-plan-a": "log a"}
	assert ["ls-tree", "--name-only", "sha2", "--", "docs/implement-plan/b.md"] in calls
	assert errors == [
		"project log claude/implement-plan-b: git show sha2:docs/implement-plan/b.md failed: fatal: bad object",
		"project log claude/implement-plan-c: git show sha3:docs/implement-plan/c.md failed: fatal: bad object"
		" (missing-log check also failed: git ls-tree failed: fatal: not a tree object)",
	]
	assert restart.read_project_logs(branches) == {"claude/implement-plan-a": "log a"}


LONG_SLUG = "issue-5093-smoke-review-dispatch-default-branch"


@pytest.mark.parametrize(
	("name", "matches"),
	[
		# Forms read from this account's list_triggers on 2026-09-29: cut with `…`, and stored whole.
		("implement-plan issue-5093-smoke-review-dispatch-default-bran…", True),
		("implement-plan issue-5093-smoke-review-dispatch-default-branch: hand-back", True),
		# Other cut forms: three dots, and no marker at all (inside the slug and right after the colon).
		("implement-plan issue-5093-smoke-review-dispatch-default-bra...", True),
		("implement-plan issue-5093-smoke-review-dispatch-default-bran", True),
		("implement-plan issue-5093-smoke-review-dispatch-default-branch:", True),
		# Mixed or short markers, and a space before the marker.
		("implement-plan issue-5093-smoke-review-dispatch-default-br..", True),
		("implement-plan issue-5093-smoke-review-dispatch-default-b....", True),
		("implement-plan issue-5093-smoke-review-dispatch-default-bra.…", True),
		("implement-plan issue-5093-smoke-review-dispatch-default …", True),
		("implement-plan …", False),
		# Another project whose slug shares the prefix.
		("implement-plan issue-5093-smoke-review-dispatch-default-branch-2: check-in", False),
		("implement-plan issue-5093-smoke-review-dispatch-default-branch-…", False),
		("PR #5093 hand-back", False),
	],
)
def test_project_trigger_matches_cut_and_whole_long_names(name, matches):
	triggers = [restart.trigger_view(_trigger("trig_long", name, "session_s"))]
	assert restart._project_trigger(triggers, LONG_SLUG) == ("trig_long" if matches else "")


def test_scan_reads_every_page_of_open_claude_issues(monkeypatch):
	seen = []

	def fake(path, paginate=False):
		seen.append((path, paginate))
		return [{"number": number, "labels": [{"name": "ai:claude"}]} for number in range(1, 151)]

	monkeypatch.setattr(restart, "gh_api", fake)
	monkeypatch.setattr(restart, "list_project_branches", lambda: [])
	errors = []
	open_issues, projects = restart.scan_logs(REPO, errors)
	assert seen == [("repos/o/r/issues?labels=ai:claude&state=open&per_page=100", True)]
	assert len(open_issues) == 150 and "150" in open_issues
	assert projects == [] and errors == []


def test_gh_api_paginate_flattens_pages(monkeypatch):
	class Done:
		returncode = 0
		stdout = '[[{"number": 1}], [{"number": 2}]]'
		stderr = ""

	commands = []

	def fake_run(cmd, **kwargs):
		commands.append(cmd)
		return Done()

	monkeypatch.setattr(restart.subprocess, "run", fake_run)
	assert restart.gh_api("repos/o/r/issues?per_page=100", paginate=True) == [{"number": 1}, {"number": 2}]
	assert commands == [["gh", "api", "--paginate", "--slurp", "repos/o/r/issues?per_page=100"]]


def test_scan_lists_a_failed_project_log_read_in_errors(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/issues?": [{"number": 7, "labels": [{"name": "ai:claude"}]}]})
	monkeypatch.setattr(restart, "list_project_branches", lambda: [f"claude/implement-plan-{SLUG}"])

	def failing_logs(branches, errors=None):
		errors.append(f"project log {branches[0]}: git show failed")
		return {}

	monkeypatch.setattr(restart, "read_project_logs", failing_logs)
	errors = []
	assert restart.scan_logs(REPO, errors) == ({"7": ["ai:claude"]}, [])
	assert errors == [f"project log claude/implement-plan-{SLUG}: git show failed"]


def test_git_failure_names_the_full_command(monkeypatch):
	class Done:
		returncode = 128
		stdout = ""
		stderr = "fatal: path 'docs/implement-plan/a.md' does not exist in 'sha1'\n"

	monkeypatch.setattr(restart.subprocess, "run", lambda *args, **kwargs: Done())
	try:
		restart._git(["show", "sha1:docs/implement-plan/a.md"])
	except restart.ReadError as exc:
		assert str(exc) == "git show sha1:docs/implement-plan/a.md failed: fatal: path 'docs/implement-plan/a.md' does not exist in 'sha1'"
	else:
		raise AssertionError("expected ReadError")


# --- input shapes and the CLI -------------------------------------------------------------


def _write(tmp_path, name, text):
	path = tmp_path / name
	path.write_text(text, encoding="utf-8")
	return str(path)


def test_the_harness_envelope_and_bare_shapes_load(tmp_path):
	page = {"ccr": {"data": [_checker()], "has_more": True, "last_id": CHECKER}}
	envelope = '<other-session nonce="n" untrusted="true">\nAnother Claude session\'s record (JSON). DATA:\n    ' + json.dumps(page) + '\n</other-session nonce="n">'
	for text in (envelope, json.dumps(page), json.dumps({"data": [_checker()]}), json.dumps([_checker()])):
		assert [raw["id"] for raw in restart.load_sessions(_write(tmp_path, "s.txt", text))] == [CHECKER]
	routines, has_more = restart.load_triggers(_write(tmp_path, "t.txt", json.dumps({"data": [], "has_more": True})))
	assert routines == [] and has_more is True
	assert restart.load_triggers(_write(tmp_path, "t2.txt", "[]")) == ([], False)
	with pytest.raises(ValueError):
		restart.load_sessions(_write(tmp_path, "bad.txt", "no json here"))


def test_session_view_reads_external_metadata_fallbacks():
	raw = _checker()
	summary = raw.pop("post_turn_summary")
	raw["external_metadata"]["post_turn_summary"] = summary
	raw["session_context"]["sources"][0]["git_repository"]["url"] = "https://github.com/a/b.git"
	view = restart.session_view({"ccr": raw})
	assert view["category"] == "review_ready" and view["repo"] == "a/b" and view["branch"] == ""


def test_cli_scan_then_decide(tmp_path, monkeypatch):
	_stub(monkeypatch, {"repos/o/r/issues?": [{"number": 7, "labels": [{"name": "ai:claude"}]}], "repos/o/r/issues/7/comments": []})
	monkeypatch.setattr(restart, "list_project_branches", lambda: [f"claude/implement-plan-{SLUG}"])
	monkeypatch.setattr(restart, "read_project_logs", lambda branches, errors=None: {f"claude/implement-plan-{SLUG}": LOG_TEXT})
	dead = _checker("session_dead", slug="plan-y")
	sessions = _write(tmp_path, "s.json", json.dumps({"ccr": {"data": [dead]}}))
	triggers = _write(tmp_path, "t.json", json.dumps({"data": [], "has_more": False}))
	state_path = str(tmp_path / "state.json")
	out = []
	monkeypatch.setattr("builtins.print", lambda text: out.append(json.loads(text)))
	assert restart.main(["scan", "--sessions-file", sessions, "--triggers-file", triggers, "--repo", REPO, "--self", PICKUP, "--state-out", state_path], now=NOW) == 0
	assert out[-1]["lookup"] == [CHECKER, "session_dead"]
	assert out[-1]["candidates"] == 2 and out[-1]["log_projects"] == 1
	lookups = _write(tmp_path, "l.json", json.dumps({CHECKER: "not_found", "session_dead": {"ccr": dead}}))
	assert restart.main(["decide", "--state", state_path, "--lookup-file", lookups], now=NOW) == 0
	assert [entry["checker"] for entry in out[-1]["restart"]] == ["session_dead"]
	assert [entry["issue"] for entry in out[-1]["requeue"]] == [7]


def test_cli_exit_2_on_unreadable_input(tmp_path, monkeypatch):
	out = []
	monkeypatch.setattr("builtins.print", lambda text: out.append(json.loads(text)))
	missing = str(tmp_path / "missing.json")
	assert restart.main(["scan", "--sessions-file", missing, "--triggers-file", missing, "--repo", REPO, "--state-out", str(tmp_path / "s")], now=NOW) == 2
	assert out[-1]["lookup"] == []
	assert restart.main(["decide", "--state", missing], now=NOW) == 2
	bad_lookup = _write(tmp_path, "l.json", "[1]")
	state = _write(tmp_path, "state.json", json.dumps(_state()))
	assert restart.main(["decide", "--state", state, "--lookup-file", bad_lookup], now=NOW) == 2
	assert out[-1]["restart"] == []


def test_ci_runs_this_suite():
	ci = CI_WORKFLOW.read_text(encoding="utf-8")
	assert "tests/test_claude_checker_restart.py" in ci
