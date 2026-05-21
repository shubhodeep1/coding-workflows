#!/usr/bin/env python3
"""Tests for verifier-side fingerprint quarantine helpers."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "verify_integration_fingerprints.py"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))


spec = importlib.util.spec_from_file_location("verify_integration_fingerprints", MODULE_PATH)
assert spec is not None and spec.loader is not None
verifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)


@contextlib.contextmanager
def _set_env(**updates: str | None):
	original = {key: os.environ.get(key) for key in updates}
	try:
		for key, value in updates.items():
			if value is None:
				os.environ.pop(key, None)
			else:
				os.environ[key] = value
		yield
	finally:
		for key, value in original.items():
			if value is None:
				os.environ.pop(key, None)
			else:
				os.environ[key] = value


@contextlib.contextmanager
def _stub_quarantine_store(mod, initial_payload: dict | None = None, *, fail_load: bool = False, fail_persist: bool = False):
	store = json.loads(json.dumps(initial_payload or {"schema_version": "v1", "entries": []}))
	original_load = getattr(mod, "_ai_memory_load_quarantine_list", None)
	original_persist = getattr(mod, "_ai_memory_persist_quarantine_list", None)

	def _load_quarantine_list(**_kwargs):
		if fail_load:
			raise RuntimeError("synthetic quarantine load failure")
		return {
			"ok": True,
			"enabled": True,
			"quarantine": json.loads(json.dumps(store)),
		}

	def _persist_quarantine_list(*, payload, **_kwargs):
		nonlocal store
		if fail_persist:
			raise RuntimeError("synthetic quarantine persist failure")
		store = json.loads(json.dumps(payload))
		return {
			"ok": True,
			"enabled": True,
			"stored": True,
			"quarantine": json.loads(json.dumps(store)),
		}

	mod._ai_memory_load_quarantine_list = _load_quarantine_list
	mod._ai_memory_persist_quarantine_list = _persist_quarantine_list
	try:
		yield lambda: json.loads(json.dumps(store))
	finally:
		mod._ai_memory_load_quarantine_list = original_load
		mod._ai_memory_persist_quarantine_list = original_persist


def test_load_quarantine_runtime_fails_open_to_default_payload() -> None:
	with _stub_quarantine_store(verifier, fail_load=True), _set_env(
		FINGERPRINT_QUARANTINE_RUNS_M="3",
		GITHUB_RUN_ID="quarantine-load-fail-open",
	):
		runtime = verifier._load_quarantine_runtime()
	assert runtime["payload"] == {"schema_version": "v1", "entries": []}
	assert runtime["index"] == {}
	assert runtime["threshold"] == 3
	assert runtime["run_id"] == "quarantine-load-fail-open"


def test_persist_quarantine_runtime_round_trip_sorts_entries() -> None:
	with _stub_quarantine_store(verifier) as get_store, _set_env(
		FINGERPRINT_QUARANTINE_RUNS_M="2",
		GITHUB_RUN_ID="quarantine-round-trip-run-1",
	):
		runtime = verifier._load_quarantine_runtime()
		unchanged_keys = {
			("2000", ("scripts/example.py", "EXPECTED_TWO")),
			("1000", ("scripts/example.py", "EXPECTED_ONE")),
		}
		verifier._persist_quarantine_runtime(
			runtime,
			observed_keys=set(unchanged_keys),
			unchanged_keys=set(unchanged_keys),
		)

		stored = get_store()
		assert stored == {
			"schema_version": "v1",
			"entries": [
				{
					"fp_key": ["scripts/example.py", "EXPECTED_ONE"],
					"issue_key": "1000",
					"first_seen_run_id": "quarantine-round-trip-run-1",
					"last_seen_run_id": "quarantine-round-trip-run-1",
					"consecutive_unchanged_runs": 1,
				},
				{
					"fp_key": ["scripts/example.py", "EXPECTED_TWO"],
					"issue_key": "2000",
					"first_seen_run_id": "quarantine-round-trip-run-1",
					"last_seen_run_id": "quarantine-round-trip-run-1",
					"consecutive_unchanged_runs": 1,
				},
			],
		}

	with _stub_quarantine_store(verifier, stored) as get_store, _set_env(
		FINGERPRINT_QUARANTINE_RUNS_M="2",
		GITHUB_RUN_ID="quarantine-round-trip-run-2",
	):
		runtime = verifier._load_quarantine_runtime()
		unchanged_keys = {
			("2000", ("scripts/example.py", "EXPECTED_TWO")),
			("1000", ("scripts/example.py", "EXPECTED_ONE")),
		}
		verifier._persist_quarantine_runtime(
			runtime,
			observed_keys=set(unchanged_keys),
			unchanged_keys=set(unchanged_keys),
		)
		stored = get_store()
		assert stored["entries"] == [
			{
				"fp_key": ["scripts/example.py", "EXPECTED_ONE"],
				"issue_key": "1000",
				"first_seen_run_id": "quarantine-round-trip-run-1",
				"last_seen_run_id": "quarantine-round-trip-run-2",
				"consecutive_unchanged_runs": 2,
			},
			{
				"fp_key": ["scripts/example.py", "EXPECTED_TWO"],
				"issue_key": "2000",
				"first_seen_run_id": "quarantine-round-trip-run-1",
				"last_seen_run_id": "quarantine-round-trip-run-2",
				"consecutive_unchanged_runs": 2,
			},
		]


def test_quarantine_skip_and_suppress_threshold_semantics() -> None:
	entry = {
		"issue_key": "1500",
		"fp_key": ["scripts/example.py", "EXPECTED_LINE"],
		"first_seen_run_id": "run-1",
		"last_seen_run_id": "run-3",
		"consecutive_unchanged_runs": 3,
	}
	assert verifier._quarantine_entry_is_skipped(entry, 3, "run-4") is True
	assert verifier._quarantine_entry_is_skipped(entry, 4, "run-4") is False
	assert verifier._quarantine_entry_is_suppressed(entry, 3, "run-3") is True
	assert verifier._quarantine_entry_is_suppressed(entry, 3, "run-4") is False
	assert verifier._quarantine_entry_is_skipped(None, 3, "run-4") is False
	assert verifier._quarantine_entry_is_suppressed(None, 3, "run-4") is False


def main() -> int:
	test_load_quarantine_runtime_fails_open_to_default_payload()
	test_persist_quarantine_runtime_round_trip_sorts_entries()
	test_quarantine_skip_and_suppress_threshold_semantics()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
