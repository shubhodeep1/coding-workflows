#!/usr/bin/env python3

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "orchestrate_state_v2.py"
HEAD_SHA = "a" * 40


def _environment() -> dict[str, str]:
	keyring = {
		"schema_version": "orchestrator_state_auth_keyring.v1",
		"active_key_id": "test-key",
		"keys": [{"key_id": "test-key", "key_base64": base64.b64encode(b"k" * 32).decode("ascii")}],
	}
	return {**os.environ, "ORCHESTRATOR_STATE_AUTH_KEYRING": json.dumps(keyring), "PYTHONDONTWRITEBYTECODE": "1"}


def _context_arguments() -> list[str]:
	return [
		"--repository", "owner/repo",
		"--tracking-issue", "3965",
		"--integration-branch", "orchestrator/project-3965",
		"--source-pr", "4070",
		"--head-sha", HEAD_SHA,
		"--producer-id", "123",
	]


def test_resolver_retry_envelope_round_trips_and_rejects_tampering(tmp_path: Path) -> None:
	candidate = tmp_path / "candidate.json"
	signed = tmp_path / "signed.json"
	verified = tmp_path / "verified.json"
	candidate.write_text(json.dumps({
		"generation": 1,
		"failure_signature_sha256": "b" * 64,
		"consecutive_failure_count": 5,
		"threshold": 5,
		"escalation_threshold": 20,
		"verification_tier": "ratio",
		"regressed_by_resolver_count": 1,
		"pre_existing_drift_count": 0,
		"regression_summary": ["[\"scripts/example.py\",\"needle\"]"],
		"drift_summary": [],
		"escalated": False,
		"escalated_at": "",
		"updated_at": "2026-09-09T19:00:00Z",
	}), encoding="utf-8")
	subprocess.run(
		["python3", str(HELPER), "sign-resolver-retry", "--candidate-file", str(candidate), "--out-file", str(signed), *_context_arguments()],
		env=_environment(),
		check=True,
	)
	subprocess.run(
		["python3", str(HELPER), "verify-resolver-retry", "--envelope-file", str(signed), "--out-file", str(verified), *_context_arguments()],
		env=_environment(),
		check=True,
	)
	document = json.loads(signed.read_text(encoding="utf-8"))
	document["verification_tier"] = "warn_only"
	signed.write_text(json.dumps(document), encoding="utf-8")
	result = subprocess.run(
		["python3", str(HELPER), "verify-resolver-retry", "--envelope-file", str(signed), *_context_arguments()],
		env=_environment(),
		check=False,
	)
	assert result.returncode == 1


def _sign_retry_generation(tmp_path: Path, generation: int) -> dict[str, object]:
	candidate = tmp_path / f"candidate-{generation}.json"
	signed = tmp_path / f"signed-{generation}.json"
	candidate.write_text(json.dumps({
		"generation": generation,
		"failure_signature_sha256": "b" * 64,
		"consecutive_failure_count": generation,
		"threshold": 5,
		"escalation_threshold": 20,
		"verification_tier": "ratio",
		"regressed_by_resolver_count": 1,
		"pre_existing_drift_count": 0,
		"regression_summary": [],
		"drift_summary": [],
		"escalated": False,
		"escalated_at": "",
		"updated_at": "2026-09-09T19:00:00Z",
	}), encoding="utf-8")
	subprocess.run(
		["python3", str(HELPER), "sign-resolver-retry", "--candidate-file", str(candidate), "--out-file", str(signed), *_context_arguments()],
		env=_environment(),
		check=True,
	)
	return json.loads(signed.read_text(encoding="utf-8"))


def test_selector_finds_evicted_marker_and_chooses_highest_verified_generation(tmp_path: Path) -> None:
	generation_one = _sign_retry_generation(tmp_path, 1)
	generation_three = _sign_retry_generation(tmp_path, 3)
	forged = dict(generation_three)
	forged["generation"] = 99
	comments = [{
		"id": 10,
		"user": {"id": 123},
		"body": f"<!-- AUTOFIX_RESOLVER_RETRY_STATE_V2\n{json.dumps(generation_one, separators=(',', ':'))}\n-->",
	}]
	comments.extend({"id": 1000 + index, "user": {"id": 999}, "body": "noise"} for index in range(105))
	comments.extend((
		{
			"id": 11,
			"user": {"id": 123},
			"body": f"<!-- AUTOFIX_RESOLVER_RETRY_STATE_V2\n{json.dumps(forged, separators=(',', ':'))}\n-->",
		},
		{
			"id": 12,
			"user": {"id": 123},
			"body": f"<!-- AUTOFIX_RESOLVER_RETRY_STATE_V2\n{json.dumps(generation_three, separators=(',', ':'))}\n-->",
		},
	))
	comments_file = tmp_path / "comments.json"
	selection_file = tmp_path / "selection.json"
	comments_file.write_text(json.dumps(comments), encoding="utf-8")
	subprocess.run(
		[
			"python3", str(HELPER), "select-resolver-retry",
			"--comments-json", str(comments_file), "--out-file", str(selection_file),
			*_context_arguments(),
		],
		env=_environment(),
		check=True,
	)
	selection = json.loads(selection_file.read_text(encoding="utf-8"))
	assert selection["comment_id"] == 12
	assert selection["envelope"]["generation"] == 3
