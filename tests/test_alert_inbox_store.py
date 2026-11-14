"""Contract tests for scripts/alert_inbox_store.sh (alert inbox intake).

Alert triage plan, Phase 1. The intake validates one untrusted
alert_inbox.v1 record from $GITHUB_EVENT_PATH and writes it to the branch
`ai/alert-inbox` with the Contents API: one retry on HTTP 409, bootstrap of a
missing branch from a parentless root commit, a re-run of the same dispatch
reported as a duplicate, and never a PATCH, DELETE or force update.

The fake `curl` (shared with tests/test_tg_helpers_alert_inbox.py) answers
each endpoint from a scripted queue and logs every call.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from test_tg_helpers_alert_inbox import (  # noqa: E402
	GH_TOKEN_VALUE,
	SELF_REPO,
	_calls,
	_env,
	pytestmark,  # noqa: F401  (skip when jq is missing)
)

RECORD_ID = "0123456789abcdef0123456789abcdef"


def _iso(delta: timedelta = timedelta(0)) -> str:
	now = datetime.now(timezone.utc) + delta
	return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _record(**overrides) -> dict:
	repo = overrides.get("source_repo", SELF_REPO)
	run_id = overrides.get("run_id", "777")
	record = {
		"id": RECORD_ID,
		"captured_at": _iso(),
		"level": "ERROR",
		"source_repo": repo,
		"workflow": "CI",
		"run_id": run_id,
		"run_url": f"https://github.com/{repo}/actions/runs/{run_id}",
		"job": "build",
		"message": "deploy broke",
		"release_sha": "a" * 40,
		"sent_to_telegram": True,
	}
	record.update(overrides)
	return record


def _payload(record: dict | None = None, **extra) -> dict:
	payload = {"schema": "alert_inbox.v1", "record": record if record is not None else _record()}
	payload.update(extra)
	return {"action": "alert-inbox", "client_payload": payload}


def _store(tmp_path: Path, event: dict | None, queues: dict | None = None, **overrides):
	env, state = _env(tmp_path, None, GH_TOKEN=None, **overrides)
	(state / "queues.json").write_text(json.dumps(queues or {}), encoding="utf-8")
	event_path = tmp_path / "event.json"
	if event is not None:
		event_path.write_text(json.dumps(event), encoding="utf-8")
	env.update({"GITHUB_TOKEN": GH_TOKEN_VALUE, "GITHUB_EVENT_PATH": str(event_path)})
	result = subprocess.run(["bash", "scripts/alert_inbox_store.sh"], cwd=REPO_ROOT, env=env,
		capture_output=True, text=True, check=False, timeout=120)
	calls = _calls(state)
	assert GH_TOKEN_VALUE not in result.stdout + result.stderr
	for call in calls:
		assert call["method"] in ("GET", "PUT", "POST")
		assert all(GH_TOKEN_VALUE not in arg for arg in call["argv"])
		assert call["auth_ok"] is True
		assert "force" not in json.dumps(call["argv"]) + (call["body"] or "")
	return result, calls


def _seq(calls: list[dict]) -> list[str]:
	return [f"{c['method']} {c['kind']}" for c in calls]


def test_valid_record_is_stored_at_the_expected_path(tmp_path: Path) -> None:
	record = _record()
	result, calls = _store(tmp_path, _payload(record))
	assert result.returncode == 0, result.stderr
	assert "ALERT_INBOX store outcome=stored reason=ok" in result.stderr
	assert _seq(calls) == ["PUT contents"]
	ts = record["captured_at"]
	epoch_ms = int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp() * 1000 + 0.5)
	path = f"inbox/{ts[0:4]}/{ts[5:7]}/{ts[8:10]}/{ts[11:13]}/{epoch_ms}-shubhodeep1-coding-workflows-777-01234567.json"
	assert calls[0]["url"] == f"https://api.github.com/repos/{SELF_REPO}/contents/{path}"
	assert result.stdout.strip() == path
	body = json.loads(calls[0]["body"])
	assert body["branch"] == "ai/alert-inbox"
	assert body["message"] == "alert-inbox: shubhodeep1-coding-workflows run 777"
	assert sorted(body) == ["branch", "content", "message"]
	assert json.loads(base64.b64decode(body["content"])) == record


def test_consumer_repo_is_allowed(tmp_path: Path) -> None:
	consumer = json.loads((REPO_ROOT / ".github" / "ai" / "consumer_repos.json").read_text(encoding="utf-8"))[0]
	result, calls = _store(tmp_path, _payload(_record(source_repo=consumer)))
	assert result.returncode == 0, result.stderr
	slug = consumer.replace("/", "-")
	assert re.search(rf"/\d+-{re.escape(slug)}-777-01234567\.json$", calls[0]["url"])


def _drop(key: str) -> dict:
	record = _record()
	del record[key]
	return record


REJECTIONS = [
	({"action": "x"}, "payload"),
	(_payload(schema="alert_inbox.v2"), "schema"),
	({"client_payload": {"schema": "alert_inbox.v1", "record": "text"}}, "record"),
	(_payload(_record(extra="x")), "record_keys"),
	(_payload(_drop("job")), "record_keys"),
	(_payload(_record(id="0123456789ABCDEF0123456789abcdef")), "id"),
	(_payload(_record(id=RECORD_ID + "\n")), "id"),
	(_payload(_record(id=RECORD_ID[:31])), "id"),
	(_payload(_record(captured_at="2026-11-12 10:00:00")), "captured_at"),
	(_payload(_record(captured_at="2026-13-45T10:00:00.000Z")), "captured_at"),
	(_payload(_record(level="DEBUG")), "level"),
	(_payload(_record(level=3)), "level"),
	(_payload(_record(level="")), "level"),
	(_payload(_record(level="ERR")), "level"),
	(_payload(_record(level="WARN")), "level"),
	(_payload(_record(source_repo="a/b/c", run_url="https://github.com/a/b/c/actions/runs/777")), "source_repo"),
	(_payload(_record(source_repo="evil/repo")), "source_repo_not_allowed"),
	(_payload(_record(source_repo="../x")), "source_repo_not_allowed"),
	(_payload(_record(workflow="w" * 201)), "workflow"),
	(_payload(_record(job="j" * 101)), "job"),
	(_payload(_record(message="é" * 2001)), "message"),
	(_payload(_record(message=7)), "message"),
	(_payload(_record(run_id="12a", run_url=f"https://github.com/{SELF_REPO}/actions/runs/12a")), "run_id"),
	(_payload(_record(run_url="https://evil.example/actions/runs/777")), "run_url"),
	(_payload(_record(release_sha="abc")), "release_sha"),
	(_payload(_record(sent_to_telegram="true")), "sent_to_telegram"),
]


@pytest.mark.parametrize("event,reason", REJECTIONS)
def test_invalid_payload_is_rejected_without_api_calls(tmp_path: Path, event: dict, reason: str) -> None:
	result, calls = _store(tmp_path, event)
	assert result.returncode == 1
	assert f"ALERT_INBOX store outcome=rejected reason={reason}\n" in result.stderr
	assert calls == []


@pytest.mark.parametrize("delta,reason", [
	(timedelta(hours=-25), "captured_at_stale"),
	(timedelta(minutes=10), "captured_at_future"),
])
def test_captured_at_window(tmp_path: Path, delta: timedelta, reason: str) -> None:
	result, calls = _store(tmp_path, _payload(_record(captured_at=_iso(delta))))
	assert result.returncode == 1
	assert f"reason={reason}" in result.stderr
	assert calls == []


def test_missing_event_is_rejected(tmp_path: Path) -> None:
	result, calls = _store(tmp_path, None)
	assert result.returncode == 1
	assert "outcome=rejected reason=no_event" in result.stderr
	assert calls == []


def test_conflict_is_retried_once(tmp_path: Path) -> None:
	result, calls = _store(tmp_path, _payload(), {"contents": [[409, '{"message":"conflict"}']]})
	assert result.returncode == 0, result.stderr
	assert _seq(calls) == ["PUT contents", "PUT contents"]
	assert calls[0]["url"] == calls[1]["url"]


def test_second_conflict_fails(tmp_path: Path) -> None:
	result, calls = _store(tmp_path, _payload(), {"contents": [[409, "{}"], [409, "{}"]]})
	assert result.returncode == 1
	assert "ALERT_INBOX store outcome=failed reason=conflict" in result.stderr
	assert _seq(calls) == ["PUT contents", "PUT contents"]


@pytest.mark.parametrize("missing_status", [404, 422])
def test_missing_branch_is_bootstrapped_from_an_empty_root_commit(tmp_path: Path, missing_status: int) -> None:
	result, calls = _store(tmp_path, _payload(), {
		"contents": [[missing_status, '{"message":"Branch ai/alert-inbox not found"}']],
		"ref_get": [[404, '{"message":"Not Found"}']],
	})
	assert result.returncode == 0, result.stderr
	assert _seq(calls) == ["PUT contents", "GET contents_get", "GET ref_get", "POST commits", "POST refs", "PUT contents"]
	assert calls[2]["url"].endswith("/git/ref/heads/ai/alert-inbox")
	commit = json.loads(calls[3]["body"])
	assert commit["tree"] == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
	assert commit["parents"] == []
	ref = json.loads(calls[4]["body"])
	assert ref == {"ref": "refs/heads/ai/alert-inbox", "sha": "c" * 40}


def test_rejected_empty_tree_falls_back_to_a_marker_tree(tmp_path: Path) -> None:
	result, calls = _store(tmp_path, _payload(), {
		"contents": [[404, "{}"]],
		"ref_get": [[404, "{}"]],
		"commits": [[422, '{"message":"Tree SHA does not exist"}']],
	})
	assert result.returncode == 0, result.stderr
	assert _seq(calls) == ["PUT contents", "GET contents_get", "GET ref_get", "POST commits", "POST trees",
		"POST commits", "POST refs", "PUT contents"]
	tree = json.loads(calls[4]["body"])
	assert "base_tree" not in tree
	assert [entry["path"] for entry in tree["tree"]] == ["README.md"]
	commit = json.loads(calls[5]["body"])
	assert commit == {"message": "alert inbox root", "tree": "d" * 40, "parents": []}


def test_concurrently_created_branch_counts_as_created(tmp_path: Path) -> None:
	result, calls = _store(tmp_path, _payload(), {
		"contents": [[404, "{}"]],
		"ref_get": [[404, "{}"]],
		"refs": [[422, '{"message":"Reference already exists"}']],
	})
	assert result.returncode == 0, result.stderr
	assert _seq(calls)[-1] == "PUT contents"


def test_branch_bootstrap_failure_fails(tmp_path: Path) -> None:
	result, calls = _store(tmp_path, _payload(), {
		"contents": [[404, "{}"]],
		"ref_get": [[404, "{}"]],
		"refs": [[422, '{"message":"Validation Failed"}']],
	})
	assert result.returncode == 1
	assert "ALERT_INBOX store outcome=failed reason=branch_bootstrap_http_422" in result.stderr
	assert [c for c in calls if c["method"] == "PUT"] == calls[:1]


def test_existing_path_is_a_duplicate(tmp_path: Path) -> None:
	result, calls = _store(tmp_path, _payload(), {
		"contents": [[422, '{"message":"Invalid request. \\"sha\\" wasn\'t supplied."}']],
		"contents_get": [[200, '{"type":"file"}']],
	})
	assert result.returncode == 0, result.stderr
	assert "ALERT_INBOX store outcome=duplicate reason=exists" in result.stderr
	assert _seq(calls) == ["PUT contents", "GET contents_get"]
	assert calls[1]["url"].endswith("?ref=ai/alert-inbox")


def test_leading_zero_milliseconds_are_decimal(tmp_path: Path) -> None:
	ts = _iso()[:20] + "089Z"
	result, calls = _store(tmp_path, _payload(_record(captured_at=ts)))
	assert result.returncode == 0, result.stderr
	epoch_ms = int(datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()) * 1000 + 89
	assert f"/{epoch_ms}-shubhodeep1-coding-workflows-777-01234567.json" in calls[0]["url"]


def test_404_with_concurrently_created_branch_retries_once(tmp_path: Path) -> None:
	result, calls = _store(tmp_path, _payload(), {"contents": [[404, '{"message":"Branch not found"}']]})
	assert result.returncode == 0, result.stderr
	assert "ALERT_INBOX store outcome=stored reason=ok" in result.stderr
	assert _seq(calls) == ["PUT contents", "GET contents_get", "GET ref_get", "PUT contents"]


def test_second_404_after_branch_check_fails(tmp_path: Path) -> None:
	result, calls = _store(tmp_path, _payload(), {"contents": [[404, "{}"], [404, "{}"]]})
	assert result.returncode == 1
	assert "ALERT_INBOX store outcome=failed reason=http_404" in result.stderr
	assert _seq(calls) == ["PUT contents", "GET contents_get", "GET ref_get", "PUT contents", "GET contents_get"]


def test_unexplained_422_with_existing_branch_fails(tmp_path: Path) -> None:
	result, calls = _store(tmp_path, _payload(), {"contents": [[422, '{"message":"Validation Failed"}']]})
	assert result.returncode == 1
	assert "ALERT_INBOX store outcome=failed reason=http_422" in result.stderr
	assert _seq(calls) == ["PUT contents", "GET contents_get", "GET ref_get"]


@pytest.mark.parametrize("entry,reason", [([500, "{}"], "http_500"), ([403, "{}"], "http_403"), ("exit:28", "http_000")])
def test_other_failures_fail(tmp_path: Path, entry, reason: str) -> None:
	result, calls = _store(tmp_path, _payload(), {"contents": [entry]})
	assert result.returncode == 1
	assert f"ALERT_INBOX store outcome=failed reason={reason}" in result.stderr
	assert _seq(calls) == ["PUT contents"]
