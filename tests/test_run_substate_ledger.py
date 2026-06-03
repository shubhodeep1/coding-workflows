#!/usr/bin/env python3
"""Contract tests for the run-substate ledger helper, schema, and wiring.

Repo CI runs tests via explicit `python3 tests/<file>.py` allowlists rather
than pytest discovery, so this file keeps zero-arg test functions plus a small
manual runner.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
	import jsonschema
except ModuleNotFoundError as exc:  # pragma: no cover - direct-run fallback only.
	jsonschema = None
	_JSONSCHEMA_IMPORT_ERROR = exc
else:
	_JSONSCHEMA_IMPORT_ERROR = None

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT = REPO_ROOT / "scripts" / "ledger_emit_substate.sh"
SCHEMA_PATH = REPO_ROOT / "ai-memory" / "schemas" / "run_ledger_entry.v1.json"

STATIC_WIRING_CONTRACTS = {
	"scripts/review_run_reviewers.sh": (
		"ledger_emit_substate.sh",
		"emit_reviewer_substate",
		"PreparingWorkspace",
		"Succeeded",
		"Stalled",
	),
	"scripts/review_apply_fixes.sh": (
		"ledger_emit_substate.sh",
		"emit_editor_substate",
		"BuildingPrompt",
		"Succeeded",
		"Failed",
	),
	"scripts/review_conflict_resolve.sh": (
		"ledger_emit_substate.sh",
		"emit_conflict_resolver_substate",
		"TimedOut",
		"codex_stall_killed",
	),
	"scripts/self_heal_validation.sh": (
		"ledger_emit_substate.sh",
		"emit_self_heal_substate",
		"validate_self_heal",
		"Succeeded",
	),
	"scripts/review_rb_judge.sh": (
		"ledger_emit_substate.sh",
		"emit_review_rb_substate",
		"review_rb_judge",
		"review_rb_fix",
	),
	"scripts/validate_process.sh": (
		"ledger_emit_substate.sh",
		"emit_validate_substate",
		"validate_discover",
		"validate_diagnose",
	),
	".github/workflows/implement.yml": (
		"ledger_emit_substate.sh",
		"emit_implement_substate",
		"implement_repair",
		"codex_stall_killed",
	),
}


def _write_ai_memory_stub(path: Path) -> None:
	path.write_text(
		"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


def main() -> int:
	if len(sys.argv) < 2 or sys.argv[1] != \"record-run-event\":
		raise SystemExit(2)
	payload = {\"command\": sys.argv[1]}
	args = sys.argv[2:]
	index = 0
	while index < len(args):
		key = args[index]
		if not key.startswith(\"--\"):
			raise SystemExit(3)
		if index + 1 >= len(args):
			raise SystemExit(4)
		payload[key[2:].replace(\"-\", \"_\")] = args[index + 1]
		index += 2
	metadata = json.loads(payload.get(\"metadata_json\", \"{}\"))
	payload[\"metadata\"] = metadata
	payload.pop(\"metadata_json\", None)
	out_path = Path(os.environ[\"LEDGER_TEST_OUTPUT\"])
	fail_first = os.environ.get(\"LEDGER_TEST_FAIL_FIRST\") == \"1\"
	fail_marker = Path(os.environ.get("LEDGER_TEST_FAIL_MARKER", str(out_path) + ".fail-first"))
	if fail_first and not fail_marker.exists():
		fail_marker.write_text(\"1\", encoding=\"utf-8\")
		return 1
	if out_path.exists():
		events = json.loads(out_path.read_text(encoding=\"utf-8\"))
	else:
		events = []
	events.append(payload)
	out_path.write_text(json.dumps(events, indent=2, sort_keys=True), encoding=\"utf-8\")
	return 0


if __name__ == \"__main__\":
	raise SystemExit(main())
""",
		encoding="utf-8",
	)


def _run_helper(
	tmp_path: Path,
	*extra_args: str,
	extra_env: dict[str, str] | None = None,
	allow_stderr: bool = False,
) -> list[dict[str, object]]:
	stub_path = tmp_path / "ai_memory_stub.py"
	events_path = tmp_path / "events.json"
	seen_path = tmp_path / "seen.txt"
	_write_ai_memory_stub(stub_path)

	env = os.environ.copy()
	env.update(
		{
			"PYTHONDONTWRITEBYTECODE": "1",
			"LEDGER_AI_MEMORY_SCRIPT": str(stub_path),
			"LEDGER_TEST_OUTPUT": str(events_path),
			"LEDGER_SUBSTATES_SEEN_FILE": str(seen_path),
			"RUNNER_TEMP": str(tmp_path),
			"GITHUB_ACTOR": "codex-bot",
		}
	)
	if extra_env:
		env.update(extra_env)

	base_args = [
		"bash",
		str(HELPER_SCRIPT),
		"--run-id",
		"run-3070",
		"--workflow",
		"validate",
		"--phase",
		"validate_discover",
		"--mode",
		"discover",
		"--repo-root",
		str(tmp_path),
		"--actor",
		"codex-bot",
	]
	result = subprocess.run(
		base_args + list(extra_args),
		env=env,
		capture_output=True,
		text=True,
		check=True,
	)
	assert result.stdout == ""
	if not allow_stderr:
		assert result.stderr == ""
	if not events_path.exists():
		return []
	return json.loads(events_path.read_text(encoding="utf-8"))


def _schema() -> dict[str, object]:
	return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _base_entry(*, event_type: str, status: str, metadata: dict[str, object]) -> dict[str, object]:
	return {
		"entry_id": f"entry-{event_type}-{status}",
		"schema_version": "run_ledger_entry.v1",
		"run_id": "run-3070",
		"workflow": "validate",
		"issue_number": 3070,
		"pr_number": None,
		"event_type": event_type,
		"status": status,
		"message": f"message for {event_type}",
		"actor": "codex-bot",
		"metadata": metadata,
		"timestamp": "2026-06-03T02:00:00Z",
	}


def _require_jsonschema() -> None:
	if jsonschema is None:
		raise unittest.SkipTest(f"jsonschema unavailable: {_JSONSCHEMA_IMPORT_ERROR}")


def test_helper_suppresses_duplicates_within_one_attempt_but_allows_new_attempt_and_lane() -> None:
	with tempfile.TemporaryDirectory() as tmp_dir:
		tmp_path = Path(tmp_dir)
		events = _run_helper(
			tmp_path,
			"--substate",
			"PreparingWorkspace",
			"--attempt",
			"1",
		)
		assert len(events) == 1

		events = _run_helper(
			tmp_path,
			"--substate",
			"PreparingWorkspace",
			"--attempt",
			"1",
		)
		assert len(events) == 1, events

		events = _run_helper(
			tmp_path,
			"--substate",
			"PreparingWorkspace",
			"--attempt",
			"2",
		)
		events = _run_helper(
			tmp_path,
			"--substate",
			"PreparingWorkspace",
			"--attempt",
			"1",
			"--lane",
			"reviewer-b",
		)

		assert len(events) == 3, events
		assert [event["metadata"].get("attempt") for event in events] == [1, 2, 1]
		assert [event["metadata"].get("lane") for event in events] == [None, None, "reviewer-b"]


def test_failed_emit_does_not_consume_dedupe_key() -> None:
	with tempfile.TemporaryDirectory() as tmp_dir:
		tmp_path = Path(tmp_dir)
		events = _run_helper(
			tmp_path,
			"--substate",
			"PreparingWorkspace",
			"--attempt",
			"1",
			extra_env={"LEDGER_TEST_FAIL_FIRST": "1"},
			allow_stderr=True,
		)
		assert events == []

		events = _run_helper(
			tmp_path,
			"--substate",
			"PreparingWorkspace",
			"--attempt",
			"1",
		)
		assert len(events) == 1
		assert events[0]["metadata"]["run_substate"] == "PreparingWorkspace"


def test_helper_maps_terminal_substates_and_token_payloads() -> None:
	with tempfile.TemporaryDirectory() as tmp_dir:
		tmp_path = Path(tmp_dir)
		usage_log = tmp_path / "usage.log"
		usage_log.write_text(
			"INFO: openrouter usage phase=validate call=1 model=openai/gpt-5.4 prompt_tokens=8 completion_tokens=2 total_tokens=10\n",
			encoding="utf-8",
		)
		tokens_used_log = tmp_path / "tokens-used.log"
		tokens_used_log.write_text("tokens used\n1,234\n", encoding="utf-8")

		events = _run_helper(
			tmp_path,
			"--substate",
			"Succeeded",
			"--attempt",
			"1",
			"--tokens-input",
			"11",
			"--tokens-output",
			"13",
		)
		events = _run_helper(
			tmp_path,
			"--substate",
			"Failed",
			"--attempt",
			"2",
			"--tokens-total",
			"5",
		)
		events = _run_helper(
			tmp_path,
			"--substate",
			"TimedOut",
			"--attempt",
			"3",
			"--tokens-log-file",
			str(usage_log),
		)
		events = _run_helper(
			tmp_path,
			"--substate",
			"Stalled",
			"--attempt",
			"4",
			"--tokens-log-file",
			str(tokens_used_log),
		)
		events = _run_helper(
			tmp_path,
			"--event-type",
			"codex_stall_killed",
			"--attempt",
			"5",
		)

		by_attempt = {int(event["metadata"].get("attempt", 0)): event for event in events}
		assert by_attempt[1]["status"] == "ok"
		assert by_attempt[1]["metadata"]["tokens"] == {"input": 11, "output": 13, "total": 24}

		assert by_attempt[2]["status"] == "error"
		assert by_attempt[2]["metadata"]["tokens"] == {"total": 5}

		assert by_attempt[3]["status"] == "timeout"
		assert by_attempt[3]["metadata"]["tokens"] == {"input": 8, "output": 2, "total": 10}

		assert by_attempt[4]["status"] == "stalled"
		assert by_attempt[4]["metadata"]["tokens"] == {"total": 1234}

		assert by_attempt[5]["event_type"] == "codex_stall_killed"
		assert by_attempt[5]["status"] == "stalled"
		assert "run_substate" not in by_attempt[5]["metadata"]


def test_schema_accepts_legacy_and_new_substate_entries_additively() -> None:
	_require_jsonschema()
	validator = jsonschema.Draft202012Validator(_schema())
	legacy_entry = _base_entry(
		event_type="phase_started",
		status="info",
		metadata={"existing_key": "kept-open"},
	)
	new_run_substate_entry = _base_entry(
		event_type="run_substate",
		status="ok",
		metadata={
			"phase": "validate_discover",
			"mode": "discover",
			"run_substate": "StreamingTurn",
			"attempt": 2,
			"lane": "reviewer-b",
			"model": "openai/gpt-5.4",
			"tokens": {"input": 3, "output": 4, "total": 7, "cached_input": 1},
			"future_key": "still-open",
		},
	)
	stall_entry = _base_entry(
		event_type="codex_stall_killed",
		status="stalled",
		metadata={
			"phase": "review_rb_judge",
			"mode": "judge",
			"attempt": 1,
			"tokens": {"total": 12, "provider_total": 12},
		},
	)

	validator.validate(legacy_entry)
	validator.validate(new_run_substate_entry)
	validator.validate(stall_entry)


def test_scoped_callsites_reference_the_run_substate_helper() -> None:
	for relative_path, required_snippets in STATIC_WIRING_CONTRACTS.items():
		text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
		for snippet in required_snippets:
			assert snippet in text, (relative_path, snippet)


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	skipped = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except unittest.SkipTest as exc:
			print(f"  SKIP  {name}: {exc}")
			skipped += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {skipped} skipped, {failed} failed, {passed + skipped + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
