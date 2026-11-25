"""Unit tests for scripts/plan_autopilot.py (plan autopilot, issue #10091)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "plan_autopilot.py"
LINT = REPO_ROOT / "scripts" / "lint_pr_body_auto_close.py"

_spec = importlib.util.spec_from_file_location("plan_autopilot", SCRIPT)
pa = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(pa)

NOW = dt.datetime(2026, 11, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
REGISTRY = """# Scripts Pending Removal

Intro.

## Entries

### `scripts/old_helper.sh`

- **Type:** single-use
- **Removal trigger:** after X

### `scripts/keep_me.sh`

- **Type:** long-running
- **Removal trigger:** permanent — review annually
"""


class FakeGh:
	def __init__(self, routes: dict[str, Any] | None = None) -> None:
		self.routes = routes or {}
		self.calls: list[tuple[str, str, Any]] = []

	def __call__(self, path: str, method: str = "GET", body: Any = None) -> Any:
		self.calls.append((method, path, body))
		for key, value in self.routes.items():
			k_method, _, k_path = key.partition(" ")
			if k_method == method and path.startswith(k_path):
				if isinstance(value, Exception):
					raise value
				return value(path, body) if callable(value) else value
		if method == "GET":
			return []
		return {}


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("PLAN_AUTOPILOT_STATE_DIR", str(tmp_path / "state"))
	monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
	monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
	monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
	monkeypatch.setattr(pa, "_sleep", lambda _s: None)


def _workdir(tmp_path: Path, wrapper: str = "ai-orchestrate.yml", plans: tuple[str, ...] = ("a.md", "b.md")) -> Path:
	work = tmp_path / "repo"
	(work / "docs" / "plans").mkdir(parents=True)
	(work / "docs" / "completed").mkdir(parents=True)
	for name in plans:
		(work / "docs" / "plans" / name).write_text(f"# Plan {name}\n\nGoal.\n", encoding="utf-8")
	if wrapper:
		(work / ".github" / "workflows").mkdir(parents=True)
		(work / ".github" / "workflows" / wrapper).write_text("name: x\n", encoding="utf-8")
	return work


def _git_repo(tmp_path: Path) -> Path:
	work = _workdir(tmp_path)
	(work / "docs" / "scripts-pending-removal.md").write_text(REGISTRY, encoding="utf-8")
	(work / "scripts").mkdir()
	(work / "scripts" / "old_helper.sh").write_text("echo old\n", encoding="utf-8")
	(work / "scripts" / "keep_me.sh").write_text("echo keep\n", encoding="utf-8")
	(work / "README.md").write_text("readme\n", encoding="utf-8")
	for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init"]):
		subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True)
	return work


def _result(**overrides: Any) -> dict[str, Any]:
	base = {
		"classification": [{"path": "docs/plans/a.md", "status": "PARTIAL", "freshness": "CURRENT", "evidence": "x"}],
		"refreshed": [],
		"needs_decision": [],
		"applied_decisions": [],
		"archived": [],
		"removals": [],
		"kept_removals": [],
		"recommendation": {"kind": "none", "plans": [], "rationale": "r"},
		"summary": "s",
	}
	base.update(overrides)
	return base


def _stage(work: Path) -> list[dict[str, str]]:
	subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
	return pa.staged_changes(work)


def _check(work: Path, result: dict[str, Any], in_flight: list[str] | None = None) -> dict[str, list[str]]:
	changes = _stage(work)
	registry_new = (work / pa.REGISTRY_PATH).read_text(encoding="utf-8") if (work / pa.REGISTRY_PATH).exists() else None
	completed = {f"docs/completed/{p.name}" for p in (work / "docs" / "completed").glob("*.md")}
	return pa.check_diff(changes, result, in_flight or [], REGISTRY, registry_new, "changelog.d/plan-autopilot-20261125-abcdef0.md", completed)


# ---------------------------------------------------------------- gate

def _gate_routes(tracking=None, prs=None, runs=None, status=None) -> dict[str, Any]:
	return {
		"GET repos/o/r/issues?labels=ai:orchestrator-tracking": tracking or [],
		"GET repos/o/r/pulls?state=open": prs or [],
		"GET repos/o/r/actions/workflows/": {"workflow_runs": runs or []},
		"GET repos/o/r/issues?labels=ai:plan-autopilot": status or [],
	}


def test_gate_refuses_when_tracking_issue_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(pa, "gh_api", FakeGh(_gate_routes(tracking=[{"number": 5, "title": "p", "body": "docs/plans/a.md"}])))
	out = pa.run_gate("o/r", _workdir(tmp_path), 24, NOW)
	assert (out["proceed"], out["reason"], out["save_marker"]) == (False, "project_open", True)
	assert out["in_flight"] == ["docs/plans/a.md"]


def test_gate_refuses_while_maintenance_pr_open_and_flags_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	prs = [{"number": 9, "head": {"ref": "ai/plan-autopilot-20261120-abcdef0"}, "base": {"ref": "main"}, "created_at": "2026-11-23T00:00:00Z"}]
	monkeypatch.setattr(pa, "gh_api", FakeGh(_gate_routes(prs=prs)))
	out = pa.run_gate("o/r", _workdir(tmp_path), 24, NOW)
	assert (out["reason"], out["stale_pr"], out["save_marker"]) == ("maintenance_pr_open", "9", True)


def test_gate_fresh_maintenance_pr_is_not_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	prs = [{"number": 9, "head": {"ref": "ai/plan-autopilot-x"}, "created_at": "2026-11-25T11:00:00Z"}]
	monkeypatch.setattr(pa, "gh_api", FakeGh(_gate_routes(prs=prs)))
	out = pa.run_gate("o/r", _workdir(tmp_path), 24, NOW)
	assert out["reason"] == "maintenance_pr_open" and out["stale_pr"] == ""


def test_gate_refuses_while_dispatch_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(pa, "gh_api", FakeGh(_gate_routes(runs=[{"status": "completed"}, {"status": "queued"}])))
	out = pa.run_gate("o/r", _workdir(tmp_path), 24, NOW)
	assert out["reason"] == "dispatch_pending" and out["save_marker"] is True


def test_gate_no_plans_reads_only_the_status_issue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	status = [{"number": 2, "author_association": "OWNER", "body": pa.TRACKER_MARKER}]
	fake = FakeGh({"GET repos/o/r/issues?labels=ai:plan-autopilot": status})
	monkeypatch.setattr(pa, "gh_api", fake)
	out = pa.run_gate("o/r", _workdir(tmp_path, plans=()), 24, NOW)
	assert out["reason"] == "no_plans" and out["save_marker"] is True and out["status_issue"] == "2"
	assert [p for _, p, _ in fake.calls] and all(p.startswith("repos/o/r/issues?labels=ai:plan-autopilot") for _, p, _ in fake.calls)


def test_gate_flags_the_oldest_maintenance_pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	prs = [
		{"number": 11, "head": {"ref": "ai/plan-autopilot-b"}, "created_at": "2026-11-25T11:00:00Z"},
		{"number": 9, "head": {"ref": "ai/plan-autopilot-a"}, "created_at": "2026-11-20T00:00:00Z"},
	]
	monkeypatch.setattr(pa, "gh_api", FakeGh(_gate_routes(prs=prs)))
	assert pa.run_gate("o/r", _workdir(tmp_path), 24, NOW)["stale_pr"] == "9"


def test_gate_recent_recorded_dispatch_counts_as_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	marker = pa.render_state_marker({"dispatched_at": "2026-11-25T11:50:00Z"})
	status = [{"number": 2, "author_association": "OWNER", "body": f"{pa.TRACKER_MARKER}\n{marker}"}]
	monkeypatch.setattr(pa, "gh_api", FakeGh(_gate_routes(status=status)))
	out = pa.run_gate("o/r", _workdir(tmp_path), 24, NOW)
	assert (out["proceed"], out["reason"], out["save_marker"]) == (False, "dispatch_pending", False)


def test_gate_read_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	routes = _gate_routes()
	routes["GET repos/o/r/pulls?state=open"] = pa.ReadError("boom")
	monkeypatch.setattr(pa, "gh_api", FakeGh(routes))
	out = pa.run_gate("o/r", _workdir(tmp_path), 24, NOW)
	assert (out["proceed"], out["reason"], out["save_marker"]) == (False, "read_failed", False)


def test_gate_proceeds_when_idle_and_reads_trusted_status_issue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	marker = pa.render_state_marker({"last_audit_sha": "abc"})
	status = [
		{"number": 1, "author_association": "NONE", "body": f"{pa.TRACKER_MARKER}\n{marker}"},
		{"number": 2, "author_association": "OWNER", "body": f"{pa.TRACKER_MARKER}\n{marker}"},
	]
	monkeypatch.setattr(pa, "gh_api", FakeGh(_gate_routes(status=status)))
	out = pa.run_gate("o/r", _workdir(tmp_path), 24, NOW)
	assert out["proceed"] is True and out["status_issue"] == "2"
	assert out["prior_state"] == {"last_audit_sha": "abc"}
	assert out["wrapper"] == "ai-orchestrate.yml"


def test_wrapper_selection_prefers_consumer_wrapper(tmp_path: Path) -> None:
	work = _workdir(tmp_path, wrapper="internal-orchestrate.yml")
	assert pa.select_wrapper(work) == "internal-orchestrate.yml"
	(work / ".github" / "workflows" / "ai-orchestrate.yml").write_text("x", encoding="utf-8")
	assert pa.select_wrapper(work) == "ai-orchestrate.yml"
	assert pa.select_wrapper(_workdir(tmp_path / "n", wrapper="")) == ""


def test_stale_hours_validation() -> None:
	assert pa.stale_hours_from_env("") == 24
	assert pa.stale_hours_from_env("48") == 48
	assert pa.stale_hours_from_env("0") == 24
	assert pa.stale_hours_from_env("9999") == 24
	assert pa.stale_hours_from_env("$(x)") == 24


# ---------------------------------------------------------------- diff allowlist

def test_allowlist_rejects_outside_path(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	(work / "README.md").write_text("changed\n", encoding="utf-8")
	with pytest.raises(pa.ApplyError) as exc:
		_check(work, _result())
	assert exc.value.reason == "diff_outside_allowlist"


def test_allowlist_rejects_deletion_not_named_by_removed_entry(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	(work / "scripts" / "keep_me.sh").unlink()
	with pytest.raises(pa.ApplyError) as exc:
		_check(work, _result())
	assert exc.value.reason == "diff_outside_allowlist"


def test_allowlist_rejects_planted_registry_entry(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	registry = work / pa.REGISTRY_PATH
	registry.write_text(registry.read_text(encoding="utf-8") + "\n### `scripts/keep_me.sh` again\n\n- x\n", encoding="utf-8")
	with pytest.raises(pa.ApplyError):
		_check(work, _result())


def test_allowlist_rejects_in_flight_plan_edit(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	(work / "docs" / "plans" / "a.md").write_text("# Plan a\n\nRefreshed.\n", encoding="utf-8")
	result = _result(refreshed=[{"path": "docs/plans/a.md", "stale": "x", "changed": "y"}])
	with pytest.raises(pa.ApplyError) as exc:
		_check(work, result, in_flight=["docs/plans/a.md"])
	assert exc.value.reason == "diff_outside_allowlist"


def test_allowlist_rejects_symlink(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	(work / "docs" / "plans" / "c.md").symlink_to("/etc/passwd")
	with pytest.raises(pa.ApplyError) as exc:
		_check(work, _result())
	assert exc.value.reason == "diff_outside_allowlist"


def test_valid_refresh_archive_and_removal_are_accepted(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	(work / "docs" / "plans" / "a.md").write_text("# Plan a.md\n\nGoal, refreshed.\n", encoding="utf-8")
	content = (work / "docs" / "plans" / "b.md").read_text(encoding="utf-8")
	(work / "docs" / "plans" / "b.md").unlink()
	(work / "docs" / "completed" / "b.md").write_text(content, encoding="utf-8")
	registry = work / pa.REGISTRY_PATH
	text = registry.read_text(encoding="utf-8")
	start = text.index("### `scripts/old_helper.sh`")
	end = text.index("### `scripts/keep_me.sh`")
	registry.write_text(text[:start] + text[end:], encoding="utf-8")
	(work / "scripts" / "old_helper.sh").unlink()
	(work / "changelog.d").mkdir()
	(work / "changelog.d" / "plan-autopilot-20261125-abcdef0.md").write_text("removed\n", encoding="utf-8")
	result = _result(
		refreshed=[{"path": "docs/plans/a.md", "stale": "x", "changed": "y"}],
		archived=[{"from": "docs/plans/b.md", "to": "docs/completed/b.md", "refs_issue": 7}],
		removals=[{"entry_title": "scripts/old_helper.sh", "script_path": "scripts/old_helper.sh", "preflight": [{"check": "c", "result": "ok"}]}],
	)
	groups = _check(work, result)
	assert groups["refresh"] == ["docs/plans/a.md"]
	assert set(groups["archive"]) == {"docs/plans/b.md", "docs/completed/b.md"}
	assert set(groups["sweep"]) == {pa.REGISTRY_PATH, "scripts/old_helper.sh", "changelog.d/plan-autopilot-20261125-abcdef0.md"}


def _remove_entry(work: Path, heading: str, nxt: str | None) -> None:
	registry = work / pa.REGISTRY_PATH
	text = registry.read_text(encoding="utf-8")
	start = text.index(heading)
	end = text.index(nxt) if nxt else len(text)
	registry.write_text(text[:start] + text[end:], encoding="utf-8")


def test_allowlist_refuses_permanent_registry_entry(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	_remove_entry(work, "### `scripts/keep_me.sh`", None)
	(work / "scripts" / "keep_me.sh").unlink()
	result = _result(removals=[{"entry_title": "k", "script_path": "scripts/keep_me.sh", "preflight": [{"check": "c", "result": "ok"}]}])
	with pytest.raises(pa.ApplyError) as exc:
		_check(work, result)
	assert exc.value.reason == "diff_outside_allowlist"


def test_allowlist_refuses_protected_registry_paths() -> None:
	old = REGISTRY + "\n### `scripts/x.sh` + `.github/workflows/x.yml`\n\n- **Removal trigger:** after X\n"
	with pytest.raises(pa.ApplyError):
		pa.removed_registry_paths(old, REGISTRY)
	old = REGISTRY + "\n### `tools/thing/`\n\n- **Removal trigger:** after X\n"
	with pytest.raises(pa.ApplyError):
		pa.removed_registry_paths(old, REGISTRY)


def test_archive_must_carry_the_plan_content(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	(work / "docs" / "plans" / "b.md").unlink()
	(work / "docs" / "completed" / "b.md").write_text("# something else entirely\n\nunrelated text that is long enough\n", encoding="utf-8")
	result = _result(archived=[{"from": "docs/plans/b.md", "to": "docs/completed/b.md", "refs_issue": 7}])
	with pytest.raises(pa.ApplyError) as exc:
		_check(work, result)
	assert exc.value.reason == "diff_outside_allowlist"


def test_diff_must_match_result(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	(work / "docs" / "plans" / "a.md").write_text("# Plan a\n\nchanged\n", encoding="utf-8")
	with pytest.raises(pa.ApplyError) as exc:
		_check(work, _result())
	assert exc.value.reason == "result_mismatch"


def test_removal_without_registry_entry_is_a_mismatch(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	result = _result(removals=[{"entry_title": "x", "script_path": "scripts/old_helper.sh", "preflight": [{"check": "c", "result": "ok"}]}])
	(work / "docs" / "plans" / "a.md").write_text("# Plan a\n\nchanged\n", encoding="utf-8")
	result["refreshed"] = [{"path": "docs/plans/a.md", "stale": "x", "changed": "y"}]
	with pytest.raises(pa.ApplyError) as exc:
		_check(work, result)
	assert exc.value.reason == "result_mismatch"


# ---------------------------------------------------------------- result schema

def test_result_schema_accepts_valid_and_rejects_bad_kind_and_paths() -> None:
	assert pa.validate_result(_result()) == []
	bad = _result(recommendation={"kind": "many", "plans": [], "rationale": ""})
	assert pa.validate_result(bad)
	bad = _result(recommendation={"kind": "single", "plans": ["../etc/passwd"], "rationale": ""})
	assert pa.validate_result(bad)
	bad = _result(recommendation={"kind": "bundle", "plans": ["docs/plans/a.md"], "rationale": ""})
	assert pa.validate_result(bad)
	bad = _result(recommendation={"kind": "bundle", "plans": ["docs/plans/a.md", "docs/plans/b.md", "docs/plans/c.md", "docs/plans/d.md"], "rationale": ""})
	assert pa.validate_result(bad)
	bad = _result(archived=[{"from": "docs/plans/a.md", "to": "docs/other/a.md", "refs_issue": 1}])
	assert pa.validate_result(bad)
	bad = _result()
	bad["extra"] = 1
	assert pa.validate_result(bad)
	bad = _result(needs_decision=[{"path": "docs/plans/a.md", "questions": "Q1"}], recommendation={"kind": "single", "plans": ["docs/plans/a.md"], "rationale": ""})
	assert pa.validate_result(bad)


def test_extract_result_takes_last_fenced_object() -> None:
	text = "noise {\"summary\": 1}\n```json\n" + json.dumps(_result(summary="first")) + "\n```\nmore\n```json\n" + json.dumps(_result(summary="last")) + "\n```\n"
	assert pa.extract_result(text)["summary"] == "last"
	assert pa.extract_result("prefix " + json.dumps(_result(summary="bare")))["summary"] == "bare"
	assert pa.extract_result("no json here") is None


def test_audit_capacity_wait_maps_to_deferred(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	state: dict[str, Any] = {}
	assert pa.finish_audit(state, 80, tmp_path / "missing") == 0
	assert state["audit"] == {"completed": False, "capacity_wait": True}
	assert "AI_ENGINE_CAPACITY_WAIT role=PLAN_AUDIT outcome=deferred stage=plan_autopilot" in capsys.readouterr().out
	assert pa.finish_audit({}, 124, tmp_path / "missing") == 1


def test_audit_valid_output_completes(tmp_path: Path) -> None:
	out = tmp_path / "out.txt"
	out.write_text("```json\n" + json.dumps(_result()) + "\n```\n", encoding="utf-8")
	state: dict[str, Any] = {}
	assert pa.finish_audit(state, 0, out) == 0
	assert state["audit"]["completed"] is True


# ---------------------------------------------------------------- decisions

def test_untrusted_and_marker_answers_are_ignored() -> None:
	comments = [
		{"author_association": "NONE", "body": "Q1: B", "created_at": "2026-11-25T00:00:00Z"},
		{"author_association": "CONTRIBUTOR", "body": "Q1: C", "created_at": "2026-11-25T00:00:00Z"},
		{"author_association": "OWNER", "body": "Q1: A <!-- ai:x -->", "created_at": "2026-11-25T00:00:00Z"},
		{"author_association": "MEMBER", "body": "Q1: A", "created_at": "2026-11-25T00:00:00Z"},
		{"author_association": "OWNER", "body": "Q1: old", "created_at": "2026-11-20T00:00:00Z"},
	]
	assert pa.trusted_answers(comments, "2026-11-21T00:00:00Z") == ["Q1: A"]


def test_one_decision_issue_per_plan_matched_by_marker(monkeypatch: pytest.MonkeyPatch) -> None:
	listing = [
		{"number": 3, "author_association": "OWNER", "body": "<!-- ai:plan-decision:v1 plan=docs/plans/a.md -->"},
		{"number": 4, "author_association": "OWNER", "body": "<!-- ai:plan-decision:v1 plan=docs/plans/a.md -->"},
		{"number": 5, "author_association": "NONE", "body": "<!-- ai:plan-decision:v1 plan=docs/plans/b.md -->"},
	]
	found = pa.decision_issues_from_listing(listing)
	assert list(found) == ["docs/plans/a.md"] and found["docs/plans/a.md"]["number"] == 3
	fake = FakeGh()
	monkeypatch.setattr(pa, "gh_api", fake)
	state = {
		"audit": {"result": _result(needs_decision=[{"path": "docs/plans/a.md", "questions": "Q1: which? A) x B) y"}, {"path": "docs/plans/b.md", "questions": "Q1"}])},
		"collect": {"decision_issues": {"docs/plans/a.md": {"number": 3, "comments": 0}}},
	}
	pa.run_decisions("o/r", state)
	methods = [(m, p) for m, p, _ in fake.calls]
	assert ("PATCH", "repos/o/r/issues/3") in methods
	posts = [b for m, p, b in fake.calls if m == "POST" and p == "repos/o/r/issues"]
	assert len(posts) == 1 and "plan=docs/plans/b.md" in posts[0]["body"] and posts[0]["labels"] == ["ai:plan-decision"]
	assert "Do not start the comment with `/answer`" in posts[0]["body"]


def test_applied_decision_is_closed_with_pr_link(monkeypatch: pytest.MonkeyPatch) -> None:
	fake = FakeGh()
	monkeypatch.setattr(pa, "gh_api", fake)
	state = {
		"audit": {"result": _result(applied_decisions=[{"path": "docs/plans/a.md", "issue": 99}])},
		"collect": {"decision_issues": {"docs/plans/a.md": {"number": 3, "comments": 1}}, "answers": ["docs/plans/a.md"]},
		"apply": {"pr_number": "12"},
	}
	out = pa.run_decisions("o/r", state)
	assert out["closed"] == [3]
	assert ("PATCH", "repos/o/r/issues/3", {"state": "closed", "state_reason": "completed"}) in fake.calls
	assert any(m == "POST" and p == "repos/o/r/issues/3/comments" and "#12" in b["body"] for m, p, b in fake.calls)


def test_decision_without_collected_answer_stays_open(monkeypatch: pytest.MonkeyPatch) -> None:
	fake = FakeGh()
	monkeypatch.setattr(pa, "gh_api", fake)
	state = {
		"audit": {"result": _result(applied_decisions=[{"path": "docs/plans/a.md", "issue": 3}])},
		"collect": {"decision_issues": {"docs/plans/a.md": {"number": 3, "comments": 0}}, "answers": []},
		"apply": {"pr_number": "12"},
	}
	assert pa.run_decisions("o/r", state)["closed"] == [] and fake.calls == []


# ---------------------------------------------------------------- dispatch

def _dispatch_state(kind: str, plans: list[str], edited: list[str] | None = None) -> dict[str, Any]:
	return {
		"gate": {"wrapper": "ai-orchestrate.yml"},
		"audit": {"result": _result(recommendation={"kind": kind, "plans": plans, "rationale": "r"})},
		"apply": {"edited_plans": edited or []},
		"collect": {"input": {"head_sha": "a" * 40}},
	}


def test_dispatch_skipped_when_recommended_plan_edited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	fake = FakeGh()
	monkeypatch.setattr(pa, "gh_api", fake)
	out = pa.run_dispatch("o/r", _workdir(tmp_path), _dispatch_state("single", ["docs/plans/a.md"], ["docs/plans/a.md"]), NOW)
	assert out["skip_reason"] == "recommended_plan_edited" and fake.calls == []


def test_dispatch_bundle_is_one_dispatch_with_engine_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	runs = {"workflow_runs": [{"id": 77, "created_at": "2026-11-25T12:00:05Z"}]}
	fake = FakeGh({"GET repos/o/r/actions/workflows/ai-orchestrate.yml/runs": runs})
	monkeypatch.setattr(pa, "gh_api", fake)
	out = pa.run_dispatch("o/r", _workdir(tmp_path), _dispatch_state("bundle", ["docs/plans/a.md", "docs/plans/b.md"]), NOW)
	posts = [b for m, p, b in fake.calls if m == "POST"]
	assert len(posts) == 1
	assert posts[0]["inputs"]["engine"] == "claude"
	assert out["dispatched_run"] == "77" and out["dispatched_plans"] == ["docs/plans/a.md", "docs/plans/b.md"]


def test_dispatch_engine_fallback_labels_tracking_issue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	work = _workdir(tmp_path)
	state = _dispatch_state("single", ["docs/plans/a.md"])
	title = pa.compose_description(work, ["docs/plans/a.md"], "main", "a" * 40).splitlines()[0]
	attempts: list[Any] = []

	def dispatch(_path: str, body: Any) -> Any:
		attempts.append(body)
		if "engine" in body["inputs"]:
			raise pa.ReadError('POST dispatches: rc=1 {"message":"Unexpected inputs provided: [\\"engine\\"]"} HTTP 422')
		return {}

	fake = FakeGh({
		"POST repos/o/r/actions/workflows/ai-orchestrate.yml/dispatches": dispatch,
		"GET repos/o/r/actions/workflows/ai-orchestrate.yml/runs": {"workflow_runs": []},
		# The pre-dispatch re-check sees no project; the issue appears later.
		"GET repos/o/r/issues?labels=ai:orchestrator-tracking": lambda path, _b: [] if "&page=" in path else [{"number": 40, "title": title, "created_at": "2026-11-25T12:01:00Z"}],
	})
	monkeypatch.setattr(pa, "gh_api", fake)
	out = pa.run_dispatch("o/r", work, state, NOW)
	assert len(attempts) == 2 and "engine" not in attempts[1]["inputs"]
	assert ("POST", "repos/o/r/issues/40/labels", {"labels": ["ai:engine-claude"]}) in fake.calls
	assert out["engine_label_pending"] is False


def test_dispatch_rechecks_idle_state_after_the_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	fake = FakeGh({"GET repos/o/r/issues?labels=ai:orchestrator-tracking": [{"number": 5, "title": "p"}]})
	monkeypatch.setattr(pa, "gh_api", fake)
	out = pa.run_dispatch("o/r", _workdir(tmp_path), _dispatch_state("single", ["docs/plans/a.md"]), NOW)
	assert out["skip_reason"] == "project_open" and not [c for c in fake.calls if c[0] == "POST"]
	fake = FakeGh({"GET repos/o/r/actions/workflows/ai-orchestrate.yml/runs": {"workflow_runs": [{"status": "in_progress"}]}})
	monkeypatch.setattr(pa, "gh_api", fake)
	out = pa.run_dispatch("o/r", _workdir(tmp_path / "n"), _dispatch_state("single", ["docs/plans/a.md"]), NOW)
	assert out["skip_reason"] == "dispatch_pending" and not [c for c in fake.calls if c[0] == "POST"]


def test_project_description_title_first_and_no_auto_close(tmp_path: Path) -> None:
	work = _workdir(tmp_path)
	(work / "docs" / "plans" / "a.md").write_text("# Fixes #12 the poller\n", encoding="utf-8")
	text = pa.compose_description(work, ["docs/plans/a.md"], "main", "a" * 40)
	first = text.splitlines()[0]
	assert first.startswith("Implement plan:") and len(first) <= 200
	assert not pa.AUTO_CLOSE_RE.search(text)
	assert "`docs/plans/a.md`" in text and "Phases & Merge Strategy" in text


# ---------------------------------------------------------------- PR body, state, report

def test_pr_body_passes_auto_close_lint_and_lists_descoped(tmp_path: Path) -> None:
	result = _result(
		refreshed=[{"path": "docs/plans/a.md", "stale": "renamed script", "changed": "updated path"}],
		archived=[{"from": "docs/plans/b.md", "to": "docs/completed/b.md", "refs_issue": 7}],
	)
	refs = {7: {"labels": ["ai:orchestrator-tracking"], "unchecked": ["Phase 3: rollout"]}}
	body = pa.build_pr_body(result, refs, "5")
	assert "Refs #7" in body and "## De-scoped phases" in body and "Phase 3: rollout" in body
	body_file = tmp_path / "body.md"
	body_file.write_text(body, encoding="utf-8")
	env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
	proc = subprocess.run([sys.executable, "-I", "-B", str(LINT), "--pr-body-file", str(body_file), "--repo", "o/r"], capture_output=True, text=True, env=env)
	assert proc.returncode == 0, proc.stdout + proc.stderr


def test_pr_body_strips_auto_close_references() -> None:
	result = _result(refreshed=[{"path": "docs/plans/a.md", "stale": "Fixes #12 was renamed", "changed": "closes owner/repo#3"}])
	body = pa.build_pr_body(result, {}, "")
	assert not pa.AUTO_CLOSE_RE.search(body)


def test_state_marker_round_trip() -> None:
	data = {"last_audit_sha": "a" * 40, "dispatched_plans": ["docs/plans/a.md"], "note": "x --> y"}
	marker = pa.render_state_marker(data)
	assert marker.count("-->") == 1
	assert pa.parse_state_marker(f"text\n{marker}\n") == data


def test_log_line_format(capsys: pytest.CaptureFixture[str]) -> None:
	line = pa.log_line("skip", "project_open", pr="", dispatched=None, decisions_open=0)
	assert line == "PLAN_AUTOPILOT outcome=skip reason=project_open pr=none dispatched=none decisions_open=0"


def test_report_saves_marker_only_for_deterministic_skips_and_finished_audits(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(pa, "gh_api", FakeGh())
	skip = pa.run_report("o/r", {"gate": {"proceed": False, "reason": "project_open", "save_marker": True}}, NOW)
	assert skip["outcome"] == "skip" and skip["save_marker"] is True
	failed = pa.run_report("o/r", {"gate": {"proceed": False, "reason": "read_failed", "save_marker": False}}, NOW)
	assert failed["save_marker"] is False
	capacity = pa.run_report("o/r", {"gate": {"proceed": True}, "audit": {"completed": False, "capacity_wait": True}}, NOW)
	assert (capacity["outcome"], capacity["reason"], capacity["save_marker"]) == ("skip", "capacity_wait", False)
	error = pa.run_report("o/r", {"gate": {"proceed": True}, "audit": {"completed": True, "result": _result()}, "error": {"reason": "diff_outside_allowlist"}}, NOW)
	assert (error["outcome"], error["save_marker"]) == ("error", False)
	done = pa.run_report("o/r", {"gate": {"proceed": True}, "audit": {"completed": True, "result": _result(classification=[])}}, NOW)
	assert (done["outcome"], done["save_marker"]) == ("idle_all_done", True)
	blocked = pa.run_report("o/r", {"gate": {"proceed": True}, "audit": {"completed": True, "result": _result()}}, NOW)
	assert blocked["outcome"] == "idle_blocked"


def test_report_writes_state_marker_on_status_issue(monkeypatch: pytest.MonkeyPatch) -> None:
	fake = FakeGh()
	monkeypatch.setattr(pa, "gh_api", fake)
	state = {
		"gate": {"proceed": True, "status_issue": "8", "prior_state": {}},
		"audit": {"completed": True, "result": _result()},
		"collect": {"input": {"head_sha": "b" * 40}, "decision_issues": {}},
		"dispatch": {"dispatched_plans": ["docs/plans/a.md"], "dispatched_run": "77", "dispatched_at": "2026-11-25T12:00:00Z"},
	}
	out = pa.run_report("o/r", state, NOW)
	assert out["outcome"] == "dispatched"
	body = next(b["body"] for m, p, b in fake.calls if m == "PATCH" and p == "repos/o/r/issues/8")
	assert pa.TRACKER_MARKER in body
	marker = pa.parse_state_marker(body)
	assert marker["last_audit_sha"] == "b" * 40 and marker["dispatched_run"] == "77"


def test_gate_skip_keeps_the_last_audit_report(monkeypatch: pytest.MonkeyPatch) -> None:
	fake = FakeGh()
	monkeypatch.setattr(pa, "gh_api", fake)
	audited = {
		"gate": {"proceed": True, "status_issue": "8", "prior_state": {}},
		"audit": {"completed": True, "result": _result(summary="the audit")},
		"collect": {"input": {"head_sha": "b" * 40}},
	}
	pa.run_report("o/r", audited, NOW)
	prior = next(b["body"] for m, p, b in fake.calls if m == "PATCH")
	state = {"gate": {"proceed": False, "reason": "maintenance_pr_open", "save_marker": True, "stale_pr": "9", "status_issue": "8", "status_issue_body": prior, "prior_state": pa.parse_state_marker(prior)}}
	pa.run_report("o/r", state, NOW)
	body = [b["body"] for m, p, b in fake.calls if m == "PATCH"][-1]
	assert "## Classification" in body and "the audit" in body and "reason `maintenance_pr_open`" in body
	assert "Maintenance PR #9 has been open" in body and pa.parse_state_marker(body)["last_audit_sha"] == "b" * 40
	again = pa.run_report("o/r", {"gate": dict(state["gate"], status_issue_body=body, stale_pr="")}, NOW)
	final = [b["body"] for m, p, b in fake.calls if m == "PATCH"][-1]
	assert again["outcome"] == "skip" and "Maintenance PR #9 has been open" not in final and final.count("## Classification") == 1


def test_report_write_failure_holds_the_marker(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(pa, "gh_api", FakeGh({"PATCH repos/o/r/issues/8": pa.ReadError("boom")}))
	state = {"gate": {"proceed": True, "status_issue": "8"}, "audit": {"completed": True, "result": _result()}}
	assert pa.run_report("o/r", state, NOW)["save_marker"] is False


def test_no_plans_skip_creates_the_status_issue(monkeypatch: pytest.MonkeyPatch) -> None:
	fake = FakeGh()
	monkeypatch.setattr(pa, "gh_api", fake)
	pa.run_report("o/r", {"gate": {"proceed": False, "reason": "no_plans", "save_marker": True}}, NOW)
	assert any(m == "POST" and p == "repos/o/r/issues" and "reason `no_plans`" in b["body"] for m, p, b in fake.calls)


def test_docstring_documents_batching_contract() -> None:
	doc = pa.__doc__ or ""
	for word in ("Batching contract", "gate:", "collect:", "apply:", "dispatch:", "fails closed"):
		assert word in doc


# ---------------------------------------------------------------- review fixes (round 2)

def test_archive_never_overwrites_an_existing_completed_plan(tmp_path: Path) -> None:
	work = _git_repo(tmp_path)
	(work / "docs" / "completed" / "b.md").write_text("# Earlier archived plan\n\nkept history\n", encoding="utf-8")
	subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
	subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "c"], check=True)
	content = (work / "docs" / "plans" / "b.md").read_text(encoding="utf-8")
	(work / "docs" / "plans" / "b.md").unlink()
	(work / "docs" / "completed" / "b.md").write_text(content, encoding="utf-8")
	result = _result(archived=[{"from": "docs/plans/b.md", "to": "docs/completed/b.md", "refs_issue": 7}])
	with pytest.raises(pa.ApplyError) as exc:
		_check(work, result)
	assert exc.value.reason == "diff_outside_allowlist"


def test_pr_body_strips_url_form_auto_close_references() -> None:
	result = _result(refreshed=[{"path": "docs/plans/a.md", "stale": "Fixes https://github.com/o/r/issues/12 renamed", "changed": "resolves: https://github.com/o/r/issues/3"}])
	body = pa.build_pr_body(result, {}, "")
	assert "issues/12" not in body and "issues/3" not in body


def test_unrunnable_or_failed_preflight_is_rejected() -> None:
	for verdict in ("unrunnable (needs GitHub)", "FAILED: 2 callers", "could not run without a token"):
		bad = _result(removals=[{"entry_title": "x", "script_path": "scripts/old_helper.sh", "preflight": [{"check": "c", "result": verdict}]}])
		assert pa.validate_result(bad), verdict
	ok = _result(removals=[{"entry_title": "x", "script_path": "scripts/old_helper.sh", "preflight": [{"check": "c", "result": "passed: 0 callers"}]}])
	assert pa.validate_result(ok) == []


def test_skip_write_failure_holds_the_marker(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(pa, "gh_api", FakeGh({"PATCH repos/o/r/issues/8": pa.ReadError("boom")}))
	state = {"gate": {"proceed": False, "reason": "maintenance_pr_open", "save_marker": True, "stale_pr": "9", "status_issue": "8"}}
	assert pa.run_report("o/r", state, NOW)["save_marker"] is False


def test_failed_status_lookup_never_creates_a_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(pa, "gh_api", FakeGh({"GET repos/o/r/issues?labels=ai:plan-autopilot": pa.ReadError("boom")}))
	gate = pa.run_gate("o/r", _workdir(tmp_path, plans=()), 24, NOW)
	assert gate["reason"] == "no_plans" and gate["status_lookup_failed"] is True and gate["save_marker"] is False
	fake = FakeGh()
	monkeypatch.setattr(pa, "gh_api", fake)
	out = pa.run_report("o/r", {"gate": gate}, NOW)
	assert not [c for c in fake.calls if c[0] == "POST"] and out["save_marker"] is False


def test_decision_answer_window_starts_at_collect(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(pa, "gh_api", FakeGh())
	state = {
		"audit": {"result": _result(needs_decision=[{"path": "docs/plans/a.md", "questions": "Q1"}])},
		"collect": {"decision_issues": {"docs/plans/a.md": {"number": 3, "comments": 1}}, "collected_at": "2026-11-25T10:00:00Z"},
	}
	out = pa.run_decisions("o/r", state)
	assert out["decision_issues"]["docs/plans/a.md"]["asked_at"] == "2026-11-25T10:00:00Z"
	# An answer posted after collect read the comments survives the next run's cutoff.
	late = [{"author_association": "OWNER", "body": "Q1: A", "created_at": "2026-11-25T10:30:00Z"}]
	assert pa.trusted_answers(late, out["decision_issues"]["docs/plans/a.md"]["asked_at"]) == ["Q1: A"]
