#!/usr/bin/env python3

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "orchestrate_state_v2.py"
PRODUCER_ID = 41898282


def _environment() -> dict[str, str]:
	keyring = {
		"schema_version": "orchestrator_state_auth_keyring.v1",
		"active_key_id": "test-key",
		"keys": [{"key_id": "test-key", "key_base64": base64.b64encode(b"k" * 32).decode("ascii")}],
	}
	return {
		**os.environ,
		"ORCHESTRATOR_STATE_AUTH_KEYRING": json.dumps(keyring),
		"PYTHONDONTWRITEBYTECODE": "1",
	}


def _candidate() -> dict[str, object]:
	return {
		"source_doc": "analysis/workflow-optimization-2026-09-01.md",
		"role": "verifying",
		"dispatcher_run_id": 777,
		"smoke_run_id": 500,
		"smoke_actor_id": 1234,
		"smoke_workflow_path": ".github/workflows/test-and-mark-stable.yml",
		"smoke_event": "workflow_dispatch",
		"smoke_display_title": "Test & Mark Stable Release [cycle:777]",
		"smoke_inputs": {"gate_only": "true", "gate_cycle_id": "777"},
		"smoke_conclusion": "success",
		"smoke_head_sha": "b" * 40,
		"cycle_baseline_sha": "c" * 40,
		"promote_sha": "a" * 40,
		"proving_merge_sha": "d" * 40,
	}


def _sign(tmp_path: Path) -> dict[str, object]:
	candidate_path = tmp_path / "candidate.json"
	envelope_path = tmp_path / "envelope.json"
	candidate_path.write_text(json.dumps(_candidate()), encoding="utf-8")
	subprocess.run(
		[
			"python3", str(HELPER), "sign-comprehensive-marker",
			"--candidate-file", str(candidate_path),
			"--repository", "owner/repo",
			"--producer-id", str(PRODUCER_ID),
			"--out-file", str(envelope_path),
		],
		check=True,
		env=_environment(),
	)
	return json.loads(envelope_path.read_text(encoding="utf-8"))


def _comment(envelope: dict[str, object], producer_id: int = PRODUCER_ID) -> dict[str, object]:
	return {
		"body": (
			"apply-analysis-source-doc: analysis/workflow-optimization-2026-09-01.md\n"
			"<!-- COMPREHENSIVE_CYCLE_MARKER_V1\n"
			+ json.dumps(envelope, sort_keys=True, separators=(",", ":"))
			+ "\nCOMPREHENSIVE_CYCLE_MARKER_V1 -->"
		),
		"user": {"id": producer_id, "login": "github-actions[bot]"},
	}


def _select(tmp_path: Path, comments: list[dict[str, object]]) -> tuple[int, dict[str, object]]:
	comments_path = tmp_path / "comments.json"
	output_path = tmp_path / "selected.json"
	comments_path.write_text(json.dumps(comments), encoding="utf-8")
	result = subprocess.run(
		[
			"python3", str(HELPER), "select-comprehensive-marker",
			"--comments-json", str(comments_path),
			"--repository", "owner/repo",
			"--producer-id", str(PRODUCER_ID),
			"--out-file", str(output_path),
		],
		check=False,
		env=_environment(),
	)
	return result.returncode, json.loads(output_path.read_text(encoding="utf-8"))


def test_comprehensive_marker_round_trips_and_selects_exact_producer(tmp_path: Path) -> None:
	envelope = _sign(tmp_path)
	returncode, selected = _select(tmp_path, [_comment(envelope)])
	assert returncode == 0
	assert selected["marker"] == envelope
	assert selected["untrusted_marker"] is False

	returncode, wrong_producer = _select(tmp_path, [_comment(envelope, producer_id=123)])
	assert returncode == 0
	assert wrong_producer == {"marker": None, "untrusted_marker": True}


def test_comprehensive_marker_rejects_tampering_and_unsigned_legacy(tmp_path: Path) -> None:
	envelope = _sign(tmp_path)
	envelope["smoke_head_sha"] = "e" * 40
	returncode, tampered = _select(tmp_path, [_comment(envelope)])
	assert returncode == 0
	assert tampered == {"marker": None, "untrusted_marker": True}

	unsigned = {
		"body": "apply-analysis-source-doc: analysis/workflow-optimization-2026-09-01.md",
		"user": {"id": PRODUCER_ID, "login": "github-actions[bot]"},
	}
	returncode, legacy = _select(tmp_path, [unsigned])
	assert returncode == 0
	assert legacy == {"marker": None, "untrusted_marker": True}
