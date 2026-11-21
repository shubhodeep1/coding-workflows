#!/usr/bin/env python3
"""Tests for the label contract's ``retired_labels`` list (scripts/ai_labels.py).

Retired labels are reserved names: the contract never declares them again, and
``sync-labels`` deletes them from a repository when they are still present.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import ai_labels  # noqa: E402
from test_ai_labels import (  # noqa: E402
	CONTRACT_PATH,
	SYNC_LABELS,
	_FakeHTTPResponse,
	_http_error,
	_request_payload,
	_run_sync_labels,
)


RETIRED_CLAUDE_LABELS = [
	"ai:claude",
	"ai:claude-handoff-failed",
	"ai:claude-blocked",
	"ai:claude-issue-queue",
	"ai:claude-issue-queue-stale",
	"ai:permission-prompt",
]


class _EmptyResponse:
	"""A 204 No Content response, as GitHub returns for DELETE."""

	def __enter__(self) -> "_EmptyResponse":
		return self

	def __exit__(self, *_exc: object) -> None:
		return None

	def read(self) -> bytes:
		return b""


def _write_contract(tmpdir: Path, retired: list[object]) -> Path:
	contract_path = tmpdir / "label_contract.retired.json"
	contract_path.write_text(
		json.dumps(
			{
				"schema_version": "label_contract.v1",
				"labels": SYNC_LABELS,
				"phase_groups": [
					{"name": "sync_test_phase", "members": list(SYNC_LABELS)[:2], "fallback": list(SYNC_LABELS)[0]}
				],
				"retired_labels": retired,
			},
			ensure_ascii=True,
			sort_keys=True,
		),
		encoding="utf-8",
	)
	return contract_path


def _unchanged_responses() -> list[object]:
	return [_FakeHTTPResponse({"name": name, **meta}) for name, meta in sorted(SYNC_LABELS.items())]


def test_repo_contract_retires_the_claude_session_labels() -> None:
	contract = ai_labels.load_label_contract(CONTRACT_PATH)
	assert contract["retired_labels"] == RETIRED_CLAUDE_LABELS
	for name in RETIRED_CLAUDE_LABELS:
		assert name not in contract["labels"], name
	# ai:codex keeps its meaning (plan decision D2).
	assert "ai:codex" in contract["labels"]
	assert "ai:engine-claude" in contract["labels"]
	assert "ai:engine-claude" not in contract["retired_labels"]


def test_label_helpers_no_longer_create_retired_labels() -> None:
	helper_text = (REPO_ROOT / "scripts" / "label_helpers.sh").read_text(encoding="utf-8")
	for name in RETIRED_CLAUDE_LABELS:
		assert f'["{name}"]' not in helper_text, name


def test_retired_labels_are_validated() -> None:
	for bad in (["ai:alpha"], ["ai:old", "ai:old"], [""], [" ai:old"], [7]):
		with tempfile.TemporaryDirectory(prefix="ai-label-retired-bad-") as tmpdir:
			contract_path = _write_contract(Path(tmpdir), bad)
			try:
				ai_labels.load_label_contract(contract_path)
			except ai_labels.LabelContractError:
				continue
			raise AssertionError(f"retired_labels {bad!r} was accepted")


def test_sync_deletes_present_retired_labels_and_skips_absent_ones() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-retired-delete-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present", "ai:old-absent"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([]),
			_EmptyResponse(),
			_FakeHTTPResponse([]),
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Aold-absent", 404, "Not Found"),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload == {"created": 0, "updated": 0, "unchanged": 2, "deleted": 1, "errors": []}
	assert [entry["method"] for entry in request_log] == ["GET", "GET", "GET", "DELETE", "GET", "DELETE"]
	assert request_log[3]["url"].endswith("/repos/octo-org/octo-repo/labels/ai%3Aold-present")
	assert "LABEL_SYNC_RETIRED_IN_USE" not in stderr_text
	assert "LABEL_SYNC_DELETED: repo=octo-org/octo-repo label=ai:old-present action=delete dry_run=false" in stderr_text
	assert "LABEL_SYNC_RETIRED_ABSENT: repo=octo-org/octo-repo label=ai:old-absent" in stderr_text


def test_sync_dry_run_only_reads_retired_labels() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-retired-dry-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse({"name": "ai:old-present", "color": "000000"}),
			_FakeHTTPResponse([]),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses, dry_run=True)

	assert rc == 0
	assert payload["deleted"] == 1
	assert [entry["method"] for entry in request_log] == ["GET", "GET", "GET", "GET"]
	assert "action=delete dry_run=true" in stderr_text


def test_retired_label_failure_is_reported_but_does_not_fail_the_sync() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-retired-error-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([]),
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Aold-present", 403, "Forbidden"),
		]
		rc, payload, _stderr_text, _request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload["deleted"] == 0
	assert payload["errors"] == [
		{"action": "delete", "label": "ai:old-present", "message": "Forbidden", "status": 403}
	]


# The in-use warning tests below pin the drain off: switch-off parity with the
# warn-then-delete path that predates RETIRED_LABEL_DRAIN_ENABLED.
DRAIN_OFF = {"RETIRED_LABEL_DRAIN_ENABLED": "false"}
ISSUES_URL_PREFIX = "https://api.github.com/repos/octo-org/octo-repo/issues?labels=ai%3Aold-present&state=open&per_page=100"


def _issue_items(count: int, first_number: int = 1) -> list[dict[str, int]]:
	return [{"number": first_number + offset} for offset in range(count)]


def test_in_use_retired_label_warns_with_count_and_numbers_and_still_deletes() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-retired-in-use-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [_FakeHTTPResponse(_issue_items(12, first_number=40)), _EmptyResponse()]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, env=DRAIN_OFF, responses=responses)

	assert rc == 0
	assert payload == {"created": 0, "updated": 0, "unchanged": 2, "deleted": 1, "errors": []}
	assert [entry["method"] for entry in request_log[2:]] == ["GET", "DELETE"]
	assert request_log[2]["url"] == ISSUES_URL_PREFIX
	assert (
		"LABEL_SYNC_RETIRED_IN_USE: repo=octo-org/octo-repo label=ai:old-present action=delete dry_run=false "
		"level=WARNING count=12 issues=40,41,42,43,44,45,46,47,48,49"
	) in stderr_text
	assert "issues=40,41,42,43,44,45,46,47,48,49,50" not in stderr_text
	assert "LABEL_SYNC_DELETED: repo=octo-org/octo-repo label=ai:old-present" in stderr_text


def test_page_full_of_in_use_issues_reports_the_capped_count() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-retired-cap-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [_FakeHTTPResponse(_issue_items(100)), _EmptyResponse()]
		rc, payload, stderr_text, _request_log = _run_sync_labels(contract_path, env=DRAIN_OFF, responses=responses)

	assert rc == 0
	assert payload["deleted"] == 1
	assert "LABEL_SYNC_RETIRED_IN_USE:" in stderr_text
	assert "count=100+" in stderr_text


def test_failed_in_use_read_fails_open_and_still_deletes() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-retired-read-fail-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present", "ai:old-other"])
		responses = _unchanged_responses() + [
			_http_error(ISSUES_URL_PREFIX, 403, "Forbidden"),
			_EmptyResponse(),
			_FakeHTTPResponse({"unexpected": "shape"}),
			_EmptyResponse(),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, env=DRAIN_OFF, responses=responses)

	assert rc == 0
	assert payload == {"created": 0, "updated": 0, "unchanged": 2, "deleted": 2, "errors": []}
	assert [entry["method"] for entry in request_log[2:]] == ["GET", "DELETE", "GET", "DELETE"]
	assert (
		"LABEL_SYNC_RETIRED_CHECK_FAILED: repo=octo-org/octo-repo label=ai:old-present action=in_use_check "
		"dry_run=false level=WARNING status=403 detail=Forbidden"
	) in stderr_text
	assert "LABEL_SYNC_RETIRED_CHECK_FAILED: repo=octo-org/octo-repo label=ai:old-other" in stderr_text
	assert "LABEL_SYNC_RETIRED_IN_USE" not in stderr_text
	assert stderr_text.count("LABEL_SYNC_DELETED:") == 2


def test_dry_run_reads_in_use_issues_but_never_deletes() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-retired-dry-in-use-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present", "ai:old-absent"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse({"name": "ai:old-present", "color": "000000"}),
			_FakeHTTPResponse(_issue_items(3, first_number=7)),
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Aold-absent", 404, "Not Found"),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, env=DRAIN_OFF, responses=responses, dry_run=True)

	assert rc == 0
	assert payload["deleted"] == 1
	assert [entry["method"] for entry in request_log] == ["GET"] * 5
	assert request_log[3]["url"] == ISSUES_URL_PREFIX
	assert (
		"LABEL_SYNC_RETIRED_IN_USE: repo=octo-org/octo-repo label=ai:old-present action=delete dry_run=true "
		"level=WARNING count=3 issues=7,8,9"
	) in stderr_text
	# The absent label is skipped before the issues read in dry-run (no extra call).
	assert "LABEL_SYNC_RETIRED_ABSENT: repo=octo-org/octo-repo label=ai:old-absent" in stderr_text


def test_in_use_page_without_issue_numbers_reports_check_failed_and_still_deletes() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-retired-malformed-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [_FakeHTTPResponse([{}, {"number": "7"}]), _EmptyResponse()]
		rc, payload, stderr_text, _request_log = _run_sync_labels(contract_path, env=DRAIN_OFF, responses=responses)

	assert rc == 0
	assert payload["deleted"] == 1
	assert payload["errors"] == []
	assert "LABEL_SYNC_RETIRED_IN_USE" not in stderr_text
	assert (
		"LABEL_SYNC_RETIRED_CHECK_FAILED: repo=octo-org/octo-repo label=ai:old-present action=in_use_check "
		"dry_run=false level=WARNING"
	) in stderr_text
	assert "LABEL_SYNC_DELETED: repo=octo-org/octo-repo label=ai:old-present" in stderr_text


# --- Retired-label drain (RETIRED_LABEL_DRAIN_ENABLED, default true) ---

API = "https://api.github.com/repos/octo-org/octo-repo"
MARKER = "<!-- ai:retired-label-drain:v1 label=ai:old-present -->"


def _list_url(page: int, label: str = "ai%3Aold-present") -> str:
	return f"{API}/issues?labels={label}&state=open&per_page=100&page={page}"


def _item(number: int, *, pr: bool = False, comments: int = 0) -> dict[str, object]:
	entry: dict[str, object] = {"number": number, "comments": comments}
	if pr:
		entry["pull_request"] = {"url": f"{API}/pulls/{number}"}
	return entry


def _drain_item_ok() -> list[object]:
	"""Marker check (no marker), comment POST, close PATCH."""
	return [_FakeHTTPResponse([]), _FakeHTTPResponse({"id": 1}), _FakeHTTPResponse({"state": "closed"})]


def _methods(request_log: list[dict[str, str]]) -> list[str]:
	return [entry["method"] for entry in request_log[2:]]


def test_drain_closes_items_with_one_comment_each_before_delete() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-drain-close-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([_item(5), _item(6, pr=True, comments=3)]),
			*_drain_item_ok(),
			_FakeHTTPResponse([{"body": "unrelated", "author_association": "OWNER"}]),
			_FakeHTTPResponse({"id": 2}),
			_FakeHTTPResponse({"state": "closed"}),
			_FakeHTTPResponse([]),
			_EmptyResponse(),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload == {"created": 0, "updated": 0, "unchanged": 2, "deleted": 1, "errors": []}
	assert _methods(request_log) == ["GET", "GET", "POST", "PATCH", "GET", "POST", "PATCH", "GET", "DELETE"]
	assert request_log[2]["url"] == _list_url(1)
	assert request_log[3]["url"] == f"{API}/issues/5/comments?per_page=100&page=1"
	assert request_log[4]["url"] == f"{API}/issues/5/comments"
	issue_body = _request_payload(request_log[4])["body"]
	assert issue_body.startswith(MARKER + "\nThis issue carries the retired label `ai:old-present`")
	assert request_log[5]["url"] == f"{API}/issues/5"
	assert _request_payload(request_log[5]) == {"state": "closed", "state_reason": "not_planned"}
	assert request_log[7]["url"] == f"{API}/issues/6/comments"
	assert _request_payload(request_log[7])["body"].startswith(MARKER + "\nThis pull request carries")
	assert request_log[8]["url"] == f"{API}/issues/6"
	assert _request_payload(request_log[8]) == {"state": "closed"}
	assert request_log[9]["url"] == _list_url(1)
	assert request_log[10]["url"].endswith("/labels/ai%3Aold-present")
	assert (
		"LABEL_SYNC_RETIRED_DRAINED: repo=octo-org/octo-repo label=ai:old-present action=close dry_run=false "
		"item=5 kind=issue comment=posted"
	) in stderr_text
	assert "item=6 kind=pr comment=posted" in stderr_text
	assert "LABEL_SYNC_RETIRED_IN_USE" not in stderr_text
	assert "LABEL_SYNC_DELETED: repo=octo-org/octo-repo label=ai:old-present" in stderr_text


def test_drain_trusted_marker_skips_the_comment_and_forged_marker_does_not() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-drain-marker-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([_item(5, comments=150), _item(6, comments=1), _item(7, comments=2)]),
			_FakeHTTPResponse([{"body": "earlier\n" + MARKER + "\ntext", "author_association": "MEMBER"}]),
			_FakeHTTPResponse({"state": "closed"}),
			_FakeHTTPResponse([{"body": MARKER, "author_association": "NONE", "user": {"login": "mallory"}}]),
			_FakeHTTPResponse({"id": 3}),
			_FakeHTTPResponse({"state": "closed"}),
			_FakeHTTPResponse([{"body": MARKER, "author_association": "NONE", "user": {"login": "github-actions[bot]"}}]),
			_FakeHTTPResponse({"state": "closed"}),
			_FakeHTTPResponse([]),
			_EmptyResponse(),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload["deleted"] == 1
	assert payload["errors"] == []
	assert _methods(request_log) == ["GET", "GET", "PATCH", "GET", "POST", "PATCH", "GET", "PATCH", "GET", "DELETE"]
	# 150 comments: the marker check reads the last page only.
	assert request_log[3]["url"] == f"{API}/issues/5/comments?per_page=100&page=2"
	assert request_log[6]["url"] == f"{API}/issues/6/comments"
	assert "item=5 kind=issue comment=marker_present" in stderr_text
	assert "item=6 kind=issue comment=posted" in stderr_text
	assert "item=7 kind=issue comment=marker_present" in stderr_text


def test_drain_comment_post_is_never_retried_and_marker_check_reads_previous_page() -> None:
	# A 502 on the comment POST may follow a write GitHub already applied, so
	# the POST is not retried (no duplicate marker); the item stays open.
	with tempfile.TemporaryDirectory(prefix="ai-label-drain-no-retry-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([_item(5)]),
			_FakeHTTPResponse([]),
			_http_error(f"{API}/issues/5/comments", 502, "Bad Gateway"),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)
	assert rc == 0
	assert payload["deleted"] == 0
	assert payload["errors"] == [
		{"action": "drain_comment", "label": "ai:old-present", "message": "item=#5: Bad Gateway", "status": 502}
	]
	assert _methods(request_log) == ["GET", "GET", "POST"]
	assert "reason=item_failed" in stderr_text

	# 101 comments: the last page holds no marker, the previous page does.
	with tempfile.TemporaryDirectory(prefix="ai-label-drain-prev-page-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([_item(5, comments=101)]),
			_FakeHTTPResponse([{"body": "newer", "author_association": "OWNER"}]),
			_FakeHTTPResponse([{"body": MARKER, "author_association": "OWNER"}]),
			_FakeHTTPResponse({"state": "closed"}),
			_FakeHTTPResponse([]),
			_EmptyResponse(),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)
	assert rc == 0
	assert payload["deleted"] == 1
	assert payload["errors"] == []
	assert _methods(request_log) == ["GET", "GET", "GET", "PATCH", "GET", "DELETE"]
	assert request_log[3]["url"] == f"{API}/issues/5/comments?per_page=100&page=2"
	assert request_log[4]["url"] == f"{API}/issues/5/comments?per_page=100&page=1"
	assert "item=5 kind=issue comment=marker_present" in stderr_text


def test_drain_comment_post_retries_a_rate_limit() -> None:
	# GitHub rejects a rate-limited write without applying it, so a 429 on the
	# single-attempt comment POST is still retried (a 502 is not, see above).
	with tempfile.TemporaryDirectory(prefix="ai-label-drain-429-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([_item(5)]),
			_FakeHTTPResponse([]),
			_http_error(f"{API}/issues/5/comments", 429, "Too Many Requests"),
			_FakeHTTPResponse({"id": 1}),
			_FakeHTTPResponse({"state": "closed"}),
			_FakeHTTPResponse([]),
			_EmptyResponse(),
		]
		original_sleep = ai_labels.time.sleep
		try:
			ai_labels.time.sleep = lambda _seconds: None
			rc, payload, _stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)
		finally:
			ai_labels.time.sleep = original_sleep
	assert rc == 0
	assert payload["deleted"] == 1
	assert payload["errors"] == []
	assert _methods(request_log) == ["GET", "GET", "POST", "POST", "PATCH", "GET", "DELETE"]


def test_drain_incomplete_or_failed_listing_keeps_the_label() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-drain-incomplete-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([_item(n) for n in range(1, 101)]),
			_FakeHTTPResponse([_item(n) for n in range(101, 201)]),
		]
		for _ in range(10):
			responses.extend(_drain_item_ok())
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload["deleted"] == 0
	assert payload["errors"] == []
	assert request_log[2]["url"] == _list_url(1)
	assert request_log[3]["url"] == _list_url(2)
	assert _methods(request_log)[2:] == ["GET", "POST", "PATCH"] * 10
	assert "DELETE" not in _methods(request_log)
	assert (
		"LABEL_SYNC_RETIRED_DRAIN_DEFERRED: repo=octo-org/octo-repo label=ai:old-present action=delete "
		"dry_run=false reason=incomplete_listing remaining=190+"
	) in stderr_text
	assert "LABEL_SYNC_DELETED" not in stderr_text

	for listing_response, expected_error in (
		(
			_http_error(_list_url(1), 403, "Forbidden"),
			{"action": "drain_list", "label": "ai:old-present", "message": "Forbidden", "status": 403},
		),
		(
			_FakeHTTPResponse([{"number": "5"}]),
			{
				"action": "drain_list",
				"label": "ai:old-present",
				"message": "issues response entry had no positive integer number",
			},
		),
	):
		with tempfile.TemporaryDirectory(prefix="ai-label-drain-list-fail-") as tmpdir:
			contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
			responses = _unchanged_responses() + [listing_response]
			rc, payload, _stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)
		assert rc == 0
		assert payload["deleted"] == 0
		assert payload["errors"] == [expected_error]
		assert _methods(request_log) == ["GET"]


def test_drain_failed_close_comment_or_final_listing_keeps_the_label() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-drain-close-fail-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([_item(5)]),
			_FakeHTTPResponse([]),
			_FakeHTTPResponse({"id": 1}),
			_http_error(f"{API}/issues/5", 403, "Forbidden"),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)
	assert rc == 0
	assert payload["deleted"] == 0
	assert payload["errors"] == [
		{"action": "drain_close", "label": "ai:old-present", "message": "item=#5: Forbidden", "status": 403}
	]
	assert _methods(request_log) == ["GET", "GET", "POST", "PATCH"]
	assert "reason=item_failed" in stderr_text

	with tempfile.TemporaryDirectory(prefix="ai-label-drain-comment-fail-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([_item(5), _item(6)]),
			_FakeHTTPResponse([]),
			_http_error(f"{API}/issues/5/comments", 403, "Locked"),
			*_drain_item_ok(),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)
	assert rc == 0
	assert payload["deleted"] == 0
	assert [error["action"] for error in payload["errors"]] == ["drain_comment"]
	# Item 5 is never closed without its comment; item 6 still drains; no DELETE.
	assert _methods(request_log) == ["GET", "GET", "POST", "GET", "POST", "PATCH"]
	assert not any(entry["url"] == f"{API}/issues/5" for entry in request_log)
	assert "reason=item_failed" in stderr_text

	with tempfile.TemporaryDirectory(prefix="ai-label-drain-final-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse([_item(5)]),
			*_drain_item_ok(),
			_FakeHTTPResponse([_item(9)]),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)
	assert rc == 0
	assert payload["deleted"] == 0
	assert payload["errors"] == []
	assert _methods(request_log) == ["GET", "GET", "POST", "PATCH", "GET"]
	assert "reason=final_listing_not_empty remaining=1" in stderr_text


def test_drain_honours_the_per_run_cap_of_ten_across_labels() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-drain-cap-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [_FakeHTTPResponse([_item(n) for n in range(1, 13)])]
		for _ in range(10):
			responses.extend(_drain_item_ok())
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)
	assert rc == 0
	assert payload["deleted"] == 0
	assert _methods(request_log).count("PATCH") == 10
	assert "DELETE" not in _methods(request_log)
	assert "reason=budget remaining=2" in stderr_text

	with tempfile.TemporaryDirectory(prefix="ai-label-drain-shared-cap-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-a", "ai:old-b"])
		responses = _unchanged_responses() + [_FakeHTTPResponse([_item(n) for n in range(1, 8)])]
		for _ in range(7):
			responses.extend(_drain_item_ok())
		responses += [_FakeHTTPResponse([]), _EmptyResponse(), _FakeHTTPResponse([_item(n) for n in range(20, 25)])]
		for _ in range(3):
			responses.extend(_drain_item_ok())
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)
	assert rc == 0
	assert payload["deleted"] == 1
	assert payload["errors"] == []
	patched = [entry["url"] for entry in request_log if entry["method"] == "PATCH"]
	assert len(patched) == 10
	assert patched[7:] == [f"{API}/issues/20", f"{API}/issues/21", f"{API}/issues/22"]
	assert [entry["url"] for entry in request_log if entry["method"] == "DELETE"] == [f"{API}/labels/ai%3Aold-a"]
	assert "label=ai:old-b action=delete dry_run=false reason=budget remaining=2" in stderr_text


def test_drain_dry_run_only_reads() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-drain-dry-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present", "ai:old-unused"])
		responses = _unchanged_responses() + [
			_FakeHTTPResponse({"name": "ai:old-present", "color": "000000"}),
			_FakeHTTPResponse([_item(5), _item(6, pr=True)]),
			_FakeHTTPResponse({"name": "ai:old-unused", "color": "000000"}),
			_FakeHTTPResponse([]),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses, dry_run=True)

	assert rc == 0
	assert payload["deleted"] == 1
	assert payload["errors"] == []
	assert [entry["method"] for entry in request_log] == ["GET"] * 6
	assert not any("/comments" in entry["url"] for entry in request_log)
	assert request_log[3]["url"] == _list_url(1)
	assert (
		"LABEL_SYNC_RETIRED_DRAIN: repo=octo-org/octo-repo label=ai:old-present action=plan dry_run=true "
		"count=2 complete=true would_process=2"
	) in stderr_text
	assert "LABEL_SYNC_DELETED: repo=octo-org/octo-repo label=ai:old-unused action=delete dry_run=true" in stderr_text
	assert "LABEL_SYNC_DELETED: repo=octo-org/octo-repo label=ai:old-present" not in stderr_text


def test_drain_switch_off_values_restore_warn_then_delete() -> None:
	for value in ("false", "0", "off", " OFF ", "No"):
		with tempfile.TemporaryDirectory(prefix="ai-label-drain-off-") as tmpdir:
			contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
			responses = _unchanged_responses() + [_FakeHTTPResponse(_issue_items(1, first_number=4)), _EmptyResponse()]
			rc, payload, stderr_text, request_log = _run_sync_labels(
				contract_path, env={"RETIRED_LABEL_DRAIN_ENABLED": value}, responses=responses
			)
		assert rc == 0, value
		assert payload["deleted"] == 1, value
		assert _methods(request_log) == ["GET", "DELETE"], value
		assert request_log[2]["url"] == ISSUES_URL_PREFIX, value
		assert "LABEL_SYNC_RETIRED_IN_USE:" in stderr_text, value
		assert "LABEL_SYNC_RETIRED_DRAIN" not in stderr_text, value

	for value, expect_config_warning in (("true", False), ("", False), ("maybe", True)):
		with tempfile.TemporaryDirectory(prefix="ai-label-drain-on-") as tmpdir:
			contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
			responses = _unchanged_responses() + [_FakeHTTPResponse([]), _EmptyResponse()]
			rc, payload, stderr_text, request_log = _run_sync_labels(
				contract_path, env={"RETIRED_LABEL_DRAIN_ENABLED": value}, responses=responses
			)
		assert rc == 0, value
		assert payload["deleted"] == 1, value
		assert request_log[2]["url"] == _list_url(1), value
		assert ("LABEL_SYNC_RETIRED_DRAIN_CONFIG: level=WARNING value=maybe" in stderr_text) is expect_config_warning


def test_drain_marker_quotes_the_label_name() -> None:
	assert ai_labels._retired_label_drain_marker("ai:old-present") == MARKER
	marker = ai_labels._retired_label_drain_marker("ai:x-->y <b>")
	assert marker == "<!-- ai:retired-label-drain:v1 label=ai:x--%3Ey%20%3Cb%3E -->"
	assert marker.count("-->") == 1


def _optional_yaml():
	"""Return the PyYAML module, or None where it is not installed.

	CI installs PyYAML and runs the parsed-structure assertions; the offline
	pre-push gate container has no PyYAML, so the workflow tests below fall
	back to exact-line assertions on the same contract instead of erroring.
	"""
	try:
		import yaml
	except ImportError:  # pragma: no cover - CI installs PyYAML
		return None
	return yaml


def _assert_lines_in_order(text: str, expected_lines: list[str]) -> None:
	lines = text.splitlines()
	position = 0
	for expected in expected_lines:
		try:
			position = lines.index(expected, position) + 1
		except ValueError:
			raise AssertionError(f"missing line (in order): {expected!r}") from None


def test_sync_workflow_plumbs_the_drain_switch_and_keeps_consumer_permissions() -> None:
	text = (REPO_ROOT / ".github" / "workflows" / "sync_ai_labels.yml").read_text(encoding="utf-8")
	yaml = _optional_yaml()
	if yaml is None:
		# Top-level permissions block: exactly contents: read + issues: write.
		lines = text.splitlines()
		start = lines.index("permissions:") + 1
		block = []
		for line in lines[start:]:
			if not line.startswith("  "):
				break
			block.append(line)
		assert block == ["  contents: read", "  issues: write"], block
		_assert_lines_in_order(
			text,
			[
				"      - name: Sync AI labels from contract",
				"        env:",
				"          RETIRED_LABEL_DRAIN_ENABLED: ${{ vars.RETIRED_LABEL_DRAIN_ENABLED || 'true' }}",
				"        run: |",
			],
		)
		return
	workflow = yaml.safe_load(text)
	# Consumer callers grant only these; a wider request would stop them starting.
	assert workflow["permissions"] == {"contents": "read", "issues": "write"}
	steps = workflow["jobs"]["sync-labels"]["steps"]
	sync_step = next(step for step in steps if step.get("name") == "Sync AI labels from contract")
	assert sync_step["env"]["RETIRED_LABEL_DRAIN_ENABLED"] == "${{ vars.RETIRED_LABEL_DRAIN_ENABLED || 'true' }}"


def test_internal_sync_workflow_runs_weekly_with_local_call() -> None:
	path = REPO_ROOT / ".github" / "workflows" / "internal-sync-ai-labels.yml"
	text = path.read_text(encoding="utf-8")
	assert "inherit" not in text
	yaml = _optional_yaml()
	if yaml is None:
		_assert_lines_in_order(
			text,
			[
				'"on":',
				"  schedule:",
				'    - cron: "23 5 * * 1"  # Mondays at 05:23 UTC',
				"  workflow_dispatch:",
				"      dry_run:",
				"        default: false",
				"        type: boolean",
				"permissions:",
				"  contents: read",
				"  issues: write",
				"  pull-requests: write",
				"concurrency:",
				"  group: internal-sync-ai-labels",
				"  cancel-in-progress: false",
				"jobs:",
				"  sync:",
				"    permissions:",
				"      contents: read",
				"      issues: write",
				"      pull-requests: write",
				"    uses: ./.github/workflows/sync_ai_labels.yml",
				"    with:",
				"      dry_run: ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || false }}",
				"    secrets:",
				"      GH_PAT: ${{ secrets.GH_PAT }}",
				"      TG_BOT_SECRET: ${{ secrets.TG_BOT_SECRET }}",
			],
		)
		assert text.count("    uses: ") == 1
		return
	workflow = yaml.safe_load(text)
	on = workflow.get("on", workflow.get(True))
	assert on["schedule"] == [{"cron": "23 5 * * 1"}]
	assert on["workflow_dispatch"]["inputs"]["dry_run"]["type"] == "boolean"
	assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is False
	expected_permissions = {"contents": "read", "issues": "write", "pull-requests": "write"}
	assert workflow["permissions"] == expected_permissions
	assert workflow["concurrency"] == {"group": "internal-sync-ai-labels", "cancel-in-progress": False}
	job = workflow["jobs"]["sync"]
	assert job["permissions"] == expected_permissions
	assert job["uses"] == "./.github/workflows/sync_ai_labels.yml"
	assert job["with"] == {"dry_run": "${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || false }}"}
	assert job["secrets"] == {"GH_PAT": "${{ secrets.GH_PAT }}", "TG_BOT_SECRET": "${{ secrets.TG_BOT_SECRET }}"}
	assert "inherit" not in text


def main() -> int:
	for name in sorted(globals()):
		if name.startswith("test_") and callable(globals()[name]):
			globals()[name]()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
