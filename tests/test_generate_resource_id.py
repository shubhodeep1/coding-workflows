#!/usr/bin/env python3
"""Focused tests for scripts/generate_resource_id.py."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "generate_resource_id.py"
CONTRACTUAL_RECORD_ID_PATTERN = re.compile(
	r"^(?P<prefix>[A-Za-z0-9:-](?:[A-Za-z0-9_.:-]*"
	r"[A-Za-z0-9:-])?)_(?P<timestamp>\d{14})_(?P<suffix>[0-9a-f]{10})$"
)
HEX_SUFFIX_PATTERN = re.compile(r"^[0-9a-f]{10}$")

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("generate_resource_id", MODULE_PATH)
assert spec is not None and spec.loader is not None
generate_resource_id = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = generate_resource_id
spec.loader.exec_module(generate_resource_id)


@contextlib.contextmanager
def _patched_module_attrs(module, **replacements):
	originals = {name: getattr(module, name) for name in replacements}
	try:
		for name, value in replacements.items():
			setattr(module, name, value)
		yield
	finally:
		for name, value in originals.items():
			setattr(module, name, value)


def _split_record_id(record_id: str) -> tuple[str, str, str]:
	prefix_part, timestamp_part, suffix_part = record_id.rsplit("_", 2)
	return prefix_part, timestamp_part, suffix_part


def _assert_record_id_tail(record_id: str) -> tuple[str, str, str]:
	assert CONTRACTUAL_RECORD_ID_PATTERN.fullmatch(record_id) is not None
	prefix_part, timestamp_part, suffix_part = _split_record_id(record_id)
	assert len(timestamp_part) == 14
	assert timestamp_part.isdigit()
	assert HEX_SUFFIX_PATTERN.fullmatch(suffix_part) is not None
	return prefix_part, timestamp_part, suffix_part


def _frozen_datetime(value: datetime):
	class _FrozenDateTime(datetime):
		@classmethod
		def now(cls, tz=None):
			assert tz == timezone.utc
			return cls(
				value.year,
				value.month,
				value.day,
				value.hour,
				value.minute,
				value.second,
				tzinfo=value.tzinfo,
			)

	return _FrozenDateTime


def test_generate_id_without_salt_delegates_to_make_record_id() -> None:
	seen_prefixes: list[str] = []

	def _fake_make_record_id(prefix: str) -> str:
		seen_prefixes.append(prefix)
		return "delegated_value"

	with _patched_module_attrs(generate_resource_id, make_record_id=_fake_make_record_id):
		assert generate_resource_id.generate_id("run_event") == "delegated_value"

	assert seen_prefixes == ["run_event"]


def test_generate_id_without_salt_matches_contract_and_varies() -> None:
	first = generate_resource_id.generate_id("run_event")
	second = generate_resource_id.generate_id("run_event")

	first_prefix, _, _ = _assert_record_id_tail(first)
	second_prefix, _, _ = _assert_record_id_tail(second)
	assert first_prefix == "run_event"
	assert second_prefix == "run_event"
	assert first != second


def test_generate_id_with_salt_is_deterministic_for_prefix_and_suffix() -> None:
	first_time = datetime(2026, 7, 9, 18, 1, 2, tzinfo=timezone.utc)
	second_time = datetime(2026, 7, 9, 18, 1, 3, tzinfo=timezone.utc)

	with _patched_module_attrs(generate_resource_id, datetime=_frozen_datetime(first_time)):
		first = generate_resource_id.generate_id(" Run Event ", salt="alpha")
	with _patched_module_attrs(generate_resource_id, datetime=_frozen_datetime(second_time)):
		second = generate_resource_id.generate_id(" Run Event ", salt="alpha")

	first_prefix, first_timestamp, first_suffix = _assert_record_id_tail(first)
	second_prefix, second_timestamp, second_suffix = _assert_record_id_tail(second)
	assert first_prefix == "Run_Event"
	assert second_prefix == "Run_Event"
	assert first_timestamp == "20260709180102"
	assert second_timestamp == "20260709180103"
	assert first_suffix == hashlib.sha256(b"alpha").hexdigest()[:10]
	assert second_suffix == first_suffix


def test_generate_id_with_empty_salt_uses_salted_branch() -> None:
	frozen_time = datetime(2026, 7, 9, 19, 20, 21, tzinfo=timezone.utc)
	with _patched_module_attrs(generate_resource_id, datetime=_frozen_datetime(frozen_time)):
		resource_id = generate_resource_id.generate_id("mem", salt="")

	prefix_part, timestamp_part, suffix_part = _assert_record_id_tail(resource_id)
	assert prefix_part == "mem"
	assert timestamp_part == "20260709192021"
	assert suffix_part == hashlib.sha256(b"").hexdigest()[:10]


def test_generate_id_with_salt_uses_live_prefix_sanitization() -> None:
	frozen_time = datetime(2026, 7, 9, 20, 30, 40, tzinfo=timezone.utc)
	with _patched_module_attrs(generate_resource_id, datetime=_frozen_datetime(frozen_time)):
		cases = (
			(" .Run.Event:part-1_ ", "Run.Event:part-1"),
			("!!!", "mem"),
		)
		for prefix_input, expected_prefix in cases:
			resource_id = generate_resource_id.generate_id(prefix_input, salt="beta")
			prefix_part, _, suffix_part = _assert_record_id_tail(resource_id)
			assert prefix_part == expected_prefix, f"{prefix_input!r} produced {prefix_part=}"
			assert suffix_part == hashlib.sha256(b"beta").hexdigest()[:10]


def main() -> int:
	test_funcs = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
