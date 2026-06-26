#!/usr/bin/env python3
"""Tests for scripts/ai_labels.py contract and repair behavior."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ai_labels


CONTRACT_PATH = REPO_ROOT / ".github" / "ai" / "label_contract.v1.json"
HELPER_PATH = REPO_ROOT / "scripts" / "label_helpers.sh"


FAILURE_LABELS = [
	"ai:clarify-failed",
	"ai:clarify-respond-failed",
	"ai:plan-failed",
	"ai:implement-diagnose-failed",
	"ai:review-autofix-failed",
	"ai:validate-failed",
	"ai:integration-judge-failed",
	"ai:log-analysis-failed",
	"ai:memory-maintenance-failed",
]

RESOLVER_ESCALATED_LABEL = "ai:resolver-escalated"
ADDITIVE_LABELS = [
	RESOLVER_ESCALATED_LABEL,
	"ai:harness-broken",
	"ai:force-merge",
	"ai:integration-backpressure",
	"ai:retro",
	"ai:security-audit",
	"ai:security",
]


SYNC_LABELS = {
	"ai:alpha": {"color": "123abc", "description": "Alpha label"},
	"ai:beta": {"color": "abcdef", "description": "Beta label"},
}


def _repair(issue_labels: str) -> dict[str, object]:
	args = argparse.Namespace(contract_file=str(CONTRACT_PATH), issue_labels=issue_labels)
	output_lines: list[str] = []
	original = ai_labels._print_json
	try:
		ai_labels._print_json = lambda payload: output_lines.append(json.dumps(payload, ensure_ascii=True, sort_keys=True))
		rc = ai_labels.cmd_repair_labels(args)
	finally:
		ai_labels._print_json = original
	assert rc == 0
	assert len(output_lines) == 1
	return json.loads(output_lines[0])


def _extract_helper_catalog() -> tuple[dict[str, str], dict[str, str]]:
	helper_body = HELPER_PATH.read_text(encoding="utf-8")
	colors_block_match = re.search(r"declare -A _AI_LABEL_COLORS=\((.*?)\n\)", helper_body, flags=re.S)
	descs_block_match = re.search(r"declare -A _AI_LABEL_DESCS=\((.*?)\n\)", helper_body, flags=re.S)
	assert colors_block_match, "Could not parse _AI_LABEL_COLORS from label_helpers.sh"
	assert descs_block_match, "Could not parse _AI_LABEL_DESCS from label_helpers.sh"
	colors = dict(re.findall(r'\["([^"]+)"\]="([^"]+)"', colors_block_match.group(1)))
	descs = dict(re.findall(r'\["([^"]+)"\]="([^"]+)"', descs_block_match.group(1)))
	return colors, descs


class _FakeHTTPResponse:
	def __init__(self, payload: object) -> None:
		self._body = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")

	def read(self) -> bytes:
		return self._body

	def __enter__(self) -> "_FakeHTTPResponse":
		return self

	def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
		return False


class _FakeRawHTTPResponse:
	def __init__(self, body: bytes) -> None:
		self._body = body

	def read(self) -> bytes:
		return self._body

	def __enter__(self) -> "_FakeRawHTTPResponse":
		return self

	def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
		return False


def _set_env(overrides: dict[str, str | None]) -> dict[str, str | None]:
	previous: dict[str, str | None] = {}
	for key, value in overrides.items():
		previous[key] = os.environ.get(key)
		if value is None:
			os.environ.pop(key, None)
		else:
			os.environ[key] = value
	return previous


def _restore_env(previous: dict[str, str | None]) -> None:
	for key, value in previous.items():
		if value is None:
			os.environ.pop(key, None)
		else:
			os.environ[key] = value


def _make_sync_contract(tmpdir: Path, labels: dict[str, dict[str, str]]) -> Path:
	assert len(labels) >= 2
	contract_path = tmpdir / "label_contract.sync.json"
	contract_path.write_text(
		json.dumps(
			{
				"schema_version": "label_contract.v1",
				"labels": labels,
				"phase_groups": [
					{
						"name": "sync_test_phase",
						"members": list(labels.keys())[:2],
						"fallback": list(labels.keys())[0],
					}
				],
			},
			ensure_ascii=True,
			sort_keys=True,
		),
		encoding="utf-8",
	)
	return contract_path


def _http_error(
	url: str,
	code: int,
	message: str,
	body: object | None = None,
	headers: dict[str, str] | None = None,
) -> urllib.error.HTTPError:
	payload = body if body is not None else {"message": message}
	return urllib.error.HTTPError(
		url,
		code,
		message,
		hdrs=headers,
		fp=io.BytesIO(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")),
	)


def _run_sync_labels(
	contract_path: Path,
	*,
	responses: list[object],
	dry_run: bool = False,
	token: str | None = "test-token",
	use_main: bool = False,
) -> tuple[int, dict[str, object], str, list[dict[str, str]]]:
	request_log: list[dict[str, str]] = []
	stdout_lines: list[str] = []
	stderr_buffer = io.StringIO()
	response_iter = iter(responses)

	def _fake_urlopen(request: object, timeout: int = 120) -> _FakeHTTPResponse:
		assert timeout == 120
		assert isinstance(request, ai_labels.urllib.request.Request)
		request_log.append(
			{
				"method": request.get_method(),
				"url": request.full_url,
				"data": request.data.decode("utf-8") if request.data else "",
			}
		)
		response = next(response_iter)
		if isinstance(response, Exception):
			raise response
		assert hasattr(response, "read")
		return response

	previous_env = _set_env({"GH_TOKEN": token, "GITHUB_TOKEN": None})
	original_print = ai_labels._print_json
	original_urlopen = ai_labels.urllib.request.urlopen
	try:
		ai_labels._print_json = lambda payload: stdout_lines.append(json.dumps(payload, ensure_ascii=True, sort_keys=True))
		ai_labels.urllib.request.urlopen = _fake_urlopen
		with contextlib.redirect_stderr(stderr_buffer):
			if use_main:
				argv = [
					"sync-labels",
					"--contract-file",
					str(contract_path),
					"--repo",
					"octo-org/octo-repo",
				]
				if dry_run:
					argv.append("--dry-run")
				rc = ai_labels.main(argv)
			else:
				args = argparse.Namespace(
					contract_file=str(contract_path),
					repo="octo-org/octo-repo",
					dry_run=dry_run,
				)
				rc = ai_labels.cmd_sync_labels(args)
	finally:
		ai_labels._print_json = original_print
		ai_labels.urllib.request.urlopen = original_urlopen
		_restore_env(previous_env)

	assert len(stdout_lines) == 1
	return rc, json.loads(stdout_lines[0]), stderr_buffer.getvalue(), request_log


def _request_payload(entry: dict[str, str]) -> dict[str, object]:
	assert entry["data"]
	return json.loads(entry["data"])


def test_contract_includes_failure_phase_group_and_members():
	contract = ai_labels.load_label_contract(CONTRACT_PATH)
	labels = set(contract["labels"].keys())
	assert set(FAILURE_LABELS).issubset(labels)

	groups = {group["name"]: group for group in contract["phase_groups"]}
	assert "failure_phase" in groups
	failure_group = groups["failure_phase"]
	assert "fallback" not in failure_group
	assert failure_group["members"] == FAILURE_LABELS


def test_repair_labels_enforces_failure_phase_exclusivity():
	payload = _repair("ai:planning,ai:clarify-failed,ai:plan-failed,ai:log-analysis-failed")
	assert payload["add"] == []
	assert payload["remove"] == ["ai:clarify-failed", "ai:plan-failed"]


def test_repair_labels_does_not_add_failure_phase_when_absent():
	payload = _repair("ai:planning")
	assert payload["add"] == []
	assert payload["remove"] == []


def test_contract_labels_match_helper_color_and_description_catalogs():
	contract = ai_labels.load_label_contract(CONTRACT_PATH)
	contract_labels = contract["labels"]
	helper_colors, helper_descs = _extract_helper_catalog()
	assert set(helper_colors) == set(contract_labels)
	assert set(helper_descs) == set(contract_labels)
	assert helper_colors == {name: metadata["color"] for name, metadata in contract_labels.items()}
	assert helper_descs == {name: metadata["description"] for name, metadata in contract_labels.items()}


def test_additive_labels_are_present_and_non_phase():
	contract = ai_labels.load_label_contract(CONTRACT_PATH)
	for label in ADDITIVE_LABELS:
		assert label in contract["labels"]
		for group in contract["phase_groups"]:
			assert label not in group.get("members", [])
			assert label != group.get("fallback")


def test_additive_labels_survive_repair_alongside_phase_label():
	for label in ADDITIVE_LABELS:
		payload = _repair(f"ai:planning,{label}")
		assert payload["add"] == []
		assert payload["remove"] == []


def test_contract_validate_command_succeeds_with_failure_phase():
	args = argparse.Namespace(contract_file=str(CONTRACT_PATH))
	output_lines: list[str] = []
	original = ai_labels._print_json
	try:
		ai_labels._print_json = lambda payload: output_lines.append(json.dumps(payload, ensure_ascii=True, sort_keys=True))
		rc = ai_labels.cmd_contract_validate(args)
	finally:
		ai_labels._print_json = original
	assert rc == 0
	payload = json.loads(output_lines[0])
	assert payload["ok"] is True
	assert payload["schema_version"] == "label_contract.v1"
	assert set(FAILURE_LABELS).issubset(set(payload["labels"]))


def test_sync_labels_creates_missing_labels() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-create-") as tmpdir:
		contract_path = _make_sync_contract(Path(tmpdir), SYNC_LABELS)
		responses = [
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Aalpha", 404, "Not Found"),
			_FakeHTTPResponse({"name": "ai:alpha", **SYNC_LABELS["ai:alpha"]}),
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Abeta", 404, "Not Found"),
			_FakeHTTPResponse({"name": "ai:beta", **SYNC_LABELS["ai:beta"]}),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload == {"created": 2, "updated": 0, "unchanged": 0, "errors": []}
	assert [entry["method"] for entry in request_log] == ["GET", "POST", "GET", "POST"]
	assert _request_payload(request_log[1]) == {"color": "123abc", "description": "Alpha label", "name": "ai:alpha"}
	assert _request_payload(request_log[3]) == {"color": "abcdef", "description": "Beta label", "name": "ai:beta"}
	assert stderr_text.count("LABEL_SYNC_CREATED:") == 2


def test_sync_labels_leaves_matching_labels_unchanged() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-unchanged-") as tmpdir:
		contract_path = _make_sync_contract(Path(tmpdir), SYNC_LABELS)
		responses = [
			_FakeHTTPResponse({"name": "ai:alpha", **SYNC_LABELS["ai:alpha"]}),
			_FakeHTTPResponse({"name": "ai:beta", **SYNC_LABELS["ai:beta"]}),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload == {"created": 0, "updated": 0, "unchanged": 2, "errors": []}
	assert [entry["method"] for entry in request_log] == ["GET", "GET"]
	assert stderr_text.count("LABEL_SYNC_UNCHANGED:") == 2


def test_sync_labels_updates_mismatched_labels() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-update-") as tmpdir:
		contract_path = _make_sync_contract(Path(tmpdir), SYNC_LABELS)
		responses = [
			_FakeHTTPResponse({"name": "ai:alpha", "color": "000000", "description": "Old alpha"}),
			_FakeHTTPResponse({"name": "ai:alpha", **SYNC_LABELS["ai:alpha"]}),
			_FakeHTTPResponse({"name": "ai:beta", "color": "ffffff", "description": "Old beta"}),
			_FakeHTTPResponse({"name": "ai:beta", **SYNC_LABELS["ai:beta"]}),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload == {"created": 0, "updated": 2, "unchanged": 0, "errors": []}
	assert [entry["method"] for entry in request_log] == ["GET", "PATCH", "GET", "PATCH"]
	assert _request_payload(request_log[1]) == {"color": "123abc", "description": "Alpha label"}
	assert _request_payload(request_log[3]) == {"color": "abcdef", "description": "Beta label"}
	assert stderr_text.count("LABEL_SYNC_UPDATED:") == 2


def test_sync_labels_records_partial_errors_and_continues() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-partial-") as tmpdir:
		contract_path = _make_sync_contract(Path(tmpdir), SYNC_LABELS)
		responses = [
			_FakeHTTPResponse({"name": "ai:alpha", **SYNC_LABELS["ai:alpha"]}),
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Abeta", 404, "Not Found"),
			_http_error(
				"https://api.github.com/repos/octo-org/octo-repo/labels",
				422,
				"Unprocessable Entity",
				{"message": "Validation Failed", "errors": [{"field": "color", "code": "invalid"}]},
			),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload["created"] == 0
	assert payload["updated"] == 0
	assert payload["unchanged"] == 1
	assert payload["errors"] == [
		{
			"action": "create",
			"label": "ai:beta",
			"message": 'Validation Failed: [{"code": "invalid", "field": "color"}]',
			"status": 422,
		}
	]
	assert [entry["method"] for entry in request_log] == ["GET", "GET", "POST"]
	assert "LABEL_SYNC_UNCHANGED:" in stderr_text
	assert "LABEL_SYNC_ERROR:" in stderr_text


def test_sync_labels_treats_matching_already_exists_create_conflict_as_created() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-create-conflict-") as tmpdir:
		contract_path = _make_sync_contract(Path(tmpdir), {"ai:alpha": SYNC_LABELS["ai:alpha"], "ai:beta": SYNC_LABELS["ai:beta"]})
		responses = [
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Aalpha", 404, "Not Found"),
			_http_error(
				"https://api.github.com/repos/octo-org/octo-repo/labels",
				422,
				"Unprocessable Entity",
				{"message": "Validation Failed", "errors": [{"field": "name", "code": "already_exists"}]},
			),
			_FakeHTTPResponse({"name": "ai:alpha", **SYNC_LABELS["ai:alpha"]}),
			_FakeHTTPResponse({"name": "ai:beta", **SYNC_LABELS["ai:beta"]}),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload == {"created": 1, "updated": 0, "unchanged": 1, "errors": []}
	assert [entry["method"] for entry in request_log] == ["GET", "POST", "GET", "GET"]
	assert "resolved already_exists conflict by re-reading label" in stderr_text


def test_sync_labels_returns_nonzero_when_every_label_fails() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-fail-") as tmpdir:
		contract_path = _make_sync_contract(Path(tmpdir), SYNC_LABELS)
		responses = [
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Aalpha", 401, "Unauthorized"),
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Abeta", 403, "Forbidden"),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses, use_main=True)

	assert rc == 1
	assert payload["created"] == 0
	assert payload["updated"] == 0
	assert payload["unchanged"] == 0
	assert len(payload["errors"]) == 2
	assert [entry["method"] for entry in request_log] == ["GET", "GET"]
	assert stderr_text.count("LABEL_SYNC_ERROR:") == 2


def test_sync_labels_retries_rate_limited_requests() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-retry-") as tmpdir:
		contract_path = _make_sync_contract(Path(tmpdir), SYNC_LABELS)
		responses = [
			_http_error(
				"https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Aalpha",
				429,
				"Too Many Requests",
				headers={"Retry-After": "60"},
			),
			_FakeHTTPResponse({"name": "ai:alpha", **SYNC_LABELS["ai:alpha"]}),
			_FakeHTTPResponse({"name": "ai:beta", **SYNC_LABELS["ai:beta"]}),
		]
		sleep_calls: list[float] = []
		original_sleep = ai_labels.time.sleep
		original_uniform = ai_labels.random.uniform
		try:
			ai_labels.time.sleep = lambda seconds: sleep_calls.append(seconds)
			ai_labels.random.uniform = lambda _start, _end: 0.0
			rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)
		finally:
			ai_labels.time.sleep = original_sleep
			ai_labels.random.uniform = original_uniform

	assert rc == 0
	assert payload == {"created": 0, "updated": 0, "unchanged": 2, "errors": []}
	assert [entry["method"] for entry in request_log] == ["GET", "GET", "GET"]
	assert sleep_calls == [60.0]
	assert stderr_text.count("LABEL_SYNC_UNCHANGED:") == 2


def test_sync_labels_records_non_json_success_bodies() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-non-json-") as tmpdir:
		contract_path = _make_sync_contract(Path(tmpdir), SYNC_LABELS)
		responses = [
			_FakeRawHTTPResponse(b"<html>ok</html>"),
			_FakeHTTPResponse({"name": "ai:beta", **SYNC_LABELS["ai:beta"]}),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload["created"] == 0
	assert payload["updated"] == 0
	assert payload["unchanged"] == 1
	assert payload["errors"] == [
		{
			"action": "get",
			"label": "ai:alpha",
			"message": "GitHub API returned non-JSON response for GET repos/octo-org/octo-repo/labels/ai%3Aalpha: <html>ok</html>",
		}
	]
	assert [entry["method"] for entry in request_log] == ["GET", "GET"]
	assert "LABEL_SYNC_ERROR:" in stderr_text


def test_sync_labels_dry_run_skips_mutations() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-dry-run-") as tmpdir:
		contract_path = _make_sync_contract(Path(tmpdir), SYNC_LABELS)
		responses = [
			_http_error("https://api.github.com/repos/octo-org/octo-repo/labels/ai%3Aalpha", 404, "Not Found"),
			_FakeHTTPResponse({"name": "ai:beta", "color": "000000", "description": "Old beta"}),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(
			contract_path,
			responses=responses,
			dry_run=True,
			token=None,
		)

	assert rc == 0
	assert payload == {"created": 1, "updated": 1, "unchanged": 0, "errors": []}
	assert [entry["method"] for entry in request_log] == ["GET", "GET"]
	assert "LABEL_SYNC_CREATED:" in stderr_text
	assert "LABEL_SYNC_UPDATED:" in stderr_text


def test_sync_labels_rejects_overlong_label_name_before_api_calls() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-label-name-") as tmpdir:
		label_name = "ai:" + ("x" * 48)
		contract_path = _make_sync_contract(Path(tmpdir), {
			label_name: {"color": "123abc", "description": "Alpha label"},
			"ai:beta": SYNC_LABELS["ai:beta"],
		})
		responses = [
			_FakeHTTPResponse({"name": "ai:beta", **SYNC_LABELS["ai:beta"]}),
		]
		rc, payload, stderr_text, request_log = _run_sync_labels(contract_path, responses=responses)

	assert rc == 0
	assert payload["created"] == 0
	assert payload["updated"] == 0
	assert payload["unchanged"] == 1
	assert payload["errors"] == [
		{
			"action": "validate",
			"label": label_name,
			"message": "label name exceeds GitHub's 50-character limit (51)",
		}
	]
	assert [entry["method"] for entry in request_log] == ["GET"]
	assert "LABEL_SYNC_ERROR:" in stderr_text


def test_sync_labels_rejects_invalid_repo_before_api_calls() -> None:
	with tempfile.TemporaryDirectory(prefix="ai-label-sync-invalid-repo-") as tmpdir:
		contract_path = _make_sync_contract(Path(tmpdir), SYNC_LABELS)
		stderr_buffer = io.StringIO()
		urlopen_called = False
		previous_env = _set_env({"GH_TOKEN": "test-token", "GITHUB_TOKEN": None})
		original_urlopen = ai_labels.urllib.request.urlopen
		try:
			def _unexpected_urlopen(request: object, timeout: int = 120) -> _FakeHTTPResponse:
				nonlocal urlopen_called
				urlopen_called = True
				raise AssertionError(f"urlopen should not be called for invalid repo: {request!r} {timeout!r}")

			ai_labels.urllib.request.urlopen = _unexpected_urlopen
			with contextlib.redirect_stderr(stderr_buffer):
				rc = ai_labels.main(
					[
						"sync-labels",
						"--contract-file",
						str(contract_path),
						"--repo",
						"octo-org",
					]
				)
		finally:
			ai_labels.urllib.request.urlopen = original_urlopen
			_restore_env(previous_env)

	assert rc == 2
	assert urlopen_called is False
	assert "repo must be in 'owner/name' format" in stderr_buffer.getvalue()


def main() -> int:
	for name in sorted(globals()):
		if name.startswith("test_") and callable(globals()[name]):
			globals()[name]()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
