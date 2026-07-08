#!/usr/bin/env python3
"""Focused tests for scripts/clarify_informal_detect.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "clarify_informal_detect.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "informal_issues"


def _load_fixture(name: str) -> dict[str, object]:
	return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _run_detector(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		[sys.executable, str(SCRIPT), *args],
		input=input_text,
		capture_output=True,
		text=True,
		timeout=10,
	)


def _parse_output(stdout: str) -> tuple[float, list[str]]:
	lines = stdout.splitlines()
	assert len(lines) == 1, f"expected exactly one stdout line, got {len(lines)}: {stdout!r}"
	line = lines[0]
	prefix = "CLARIFY_INFORMAL_SCORE: "
	assert line.startswith(prefix), f"unexpected output prefix: {line!r}"
	assert "; signals=" in line, f"missing signals separator: {line!r}"
	score_text, signals_text = line[len(prefix):].split("; signals=", 1)
	score = float(score_text)
	assert 0.0 <= score <= 1.0, f"score out of range: {score}"
	frac = score_text.split(".")
	assert len(frac) == 2 and len(frac[1]) == 3, f"score must have three decimals: {score_text!r}"
	signals = [signal for signal in signals_text.split(",") if signal]
	return score, signals


def _assert_fixture(name: str) -> None:
	fixture = _load_fixture(name)
	proc = _run_detector(["--issue-body-stdin"], input_text=str(fixture["body"]))
	assert proc.returncode == 0, f"{name} returned {proc.returncode}: {proc.stderr!r}"
	score, signals = _parse_output(proc.stdout)
	assert abs(score - float(fixture["expected_score"])) < 0.001, (
		f"{name} expected score {fixture['expected_score']}, got {score}"
	)
	assert signals == list(fixture["expected_signals"]), (
		f"{name} expected signals {fixture['expected_signals']}, got {signals}"
	)


def test_clean_issue_fixture_matches_expected_output():
	_assert_fixture("clean_issue.json")


def test_copy_pasted_template_fixture_matches_expected_output():
	_assert_fixture("copy_pasted_template.json")


def test_empty_body_fixture_matches_expected_output():
	_assert_fixture("empty_body.json")


def test_invalid_utf8_file_fails_open_with_parse_error_signal():
	with TemporaryDirectory() as tmpdir:
		invalid_file = Path(tmpdir) / "issue-body.txt"
		invalid_file.write_bytes(b"\xff\xfe\xfd")
		proc = _run_detector(["--issue-body-file", str(invalid_file)])

	assert proc.returncode == 0, f"expected fail-open exit 0, got {proc.returncode}: {proc.stderr!r}"
	score, signals = _parse_output(proc.stdout)
	assert score == 0.0, f"expected parse-error score 0.0, got {score}"
	assert signals == ["parse_error"], f"expected parse_error signal, got {signals}"


def main() -> int:
	test_funcs = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
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
