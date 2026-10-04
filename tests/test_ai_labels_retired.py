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
			_EmptyResponse(),
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Aold-absent", 404, "Not Found"),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload == {"created": 0, "updated": 0, "unchanged": 2, "deleted": 1, "errors": []}
	assert [entry["method"] for entry in request_log] == ["GET", "GET", "DELETE", "DELETE"]
	assert request_log[2]["url"].endswith("/repos/octo-org/octo-repo/labels/ai%3Aold-present")
	assert "LABEL_SYNC_DELETED: repo=octo-org/octo-repo label=ai:old-present action=delete dry_run=false" in stderr_text
	assert "LABEL_SYNC_RETIRED_ABSENT: repo=octo-org/octo-repo label=ai:old-absent" in stderr_text


def test_sync_dry_run_only_reads_retired_labels() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-retired-dry-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [_FakeHTTPResponse({"name": "ai:old-present", "color": "000000"})]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses, dry_run=True)

	assert rc == 0
	assert payload["deleted"] == 1
	assert [entry["method"] for entry in request_log] == ["GET", "GET", "GET"]
	assert "action=delete dry_run=true" in stderr_text


def test_retired_label_failure_is_reported_but_does_not_fail_the_sync() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-retired-error-") as tmpdir:
		contract_path = _write_contract(Path(tmpdir), ["ai:old-present"])
		responses = _unchanged_responses() + [
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Aold-present", 403, "Forbidden"),
		]
		rc, payload, _stderr_text, _request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload["deleted"] == 0
	assert payload["errors"] == [
		{"action": "delete", "label": "ai:old-present", "message": "Forbidden", "status": 403}
	]


def main() -> int:
	for name in sorted(globals()):
		if name.startswith("test_") and callable(globals()[name]):
			globals()[name]()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
