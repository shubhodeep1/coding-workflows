#!/usr/bin/env python3
from __future__ import annotations

import inspect
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_floor_rules.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "review_pipeline"


def _run_floor_rules(bundle: Path, out_path: Path, *, keyword_file: Path | None = None) -> tuple[str, str]:
	env = {
		"PATH": f"{Path('/usr/bin')}:{Path('/bin')}",
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	if keyword_file is not None:
		env["REVIEW_FLOOR_KEYWORDS_FILE"] = str(keyword_file)
	result = subprocess.run(
		["bash", str(SCRIPT), str(bundle), str(out_path)],
		cwd=REPO_ROOT,
		env=env,
		capture_output=True,
		text=True,
		check=True,
	)
	return result.stdout, result.stderr


def _lines(path: Path) -> list[str]:
	return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _parse_floor_tags(path: Path) -> list[tuple[str, list[str], str, str]]:
	rows = []
	for line in _lines(path):
		parts = line.split("\t")
		assert len(parts) == 4, f"expected 4 tab-separated fields, got {parts!r}"
		anchor, tags_csv, reviewer, excerpt = parts
		tags = tags_csv.split(",")
		assert all(tag for tag in tags), f"empty tag in {tags_csv!r}"
		rows.append((anchor, tags, reviewer, excerpt))
	return rows


def test_script_exists_and_is_executable() -> None:
	assert SCRIPT.exists(), f"missing script: {SCRIPT}"
	assert SCRIPT.stat().st_mode & 0o111, "script must be executable"


def test_rule_families_and_output_schema_with_fixture(tmp_path: Path) -> None:
	out_file = tmp_path / "floor_tags.txt"
	_, stderr = _run_floor_rules(FIXTURES / "reviewer_bundle_mixed.txt", out_file)
	expected = (FIXTURES / "floor_tags_mixed_expected.txt").read_text(encoding="utf-8")
	assert out_file.read_text(encoding="utf-8") == expected

	rows = _parse_floor_tags(out_file)
	assert rows, "expected at least one tagged finding"

	row_map = {(anchor, reviewer): (tags, excerpt) for anchor, tags, reviewer, excerpt in rows}

	assert ("src/service.py:100", "review_alpha") in row_map
	tags_alpha, excerpt_alpha = row_map[("src/service.py:100", "review_alpha")]
	assert "FLOOR_MULTI_REVIEWER" in tags_alpha
	assert "FLOOR_CRITICAL_KEYWORD:SECURITY" in tags_alpha
	assert "FLOOR_HIGH_CONFIDENCE" in tags_alpha
	assert len(excerpt_alpha) <= 240

	assert ("src/service.py:103", "review_beta") in row_map
	tags_beta, _ = row_map[("src/service.py:103", "review_beta")]
	assert "FLOOR_MULTI_REVIEWER" in tags_beta
	assert "FLOOR_CRITICAL_KEYWORD:SECURITY" in tags_beta
	assert "FLOOR_HIGH_CONFIDENCE" not in tags_beta

	assert ("src/db.py:45", "review_gamma") in row_map
	tags_db, _ = row_map[("src/db.py:45", "review_gamma")]
	assert tags_db == ["FLOOR_CRITICAL_KEYWORD:MONGO"]

	assert ("src/cache.py:22", "review_delta") in row_map
	tags_cache, _ = row_map[("src/cache.py:22", "review_delta")]
	assert tags_cache == ["FLOOR_HIGH_CONFIDENCE"]

	assert all(len(line.split("\t")) == 4 for line in _lines(out_file))
	assert "stage=floor_rules anchors_scanned=" in stderr


def test_tolerance_boundary_plus_minus_three_matches_plus_four_does_not(tmp_path: Path) -> None:
	out_file = tmp_path / "floor_tags.txt"
	_run_floor_rules(FIXTURES / "reviewer_bundle_tolerance.txt", out_file)
	rows = _parse_floor_tags(out_file)
	row_map = {(anchor, reviewer): tags for anchor, tags, reviewer, _ in rows}

	assert "FLOOR_MULTI_REVIEWER" in row_map[("src/tolerance_plus.py:40", "review_alpha")]
	assert "FLOOR_MULTI_REVIEWER" in row_map[("src/tolerance_plus.py:43", "review_beta")]
	assert "FLOOR_MULTI_REVIEWER" in row_map[("src/tolerance_minus.py:50", "review_gamma")]
	assert "FLOOR_MULTI_REVIEWER" in row_map[("src/tolerance_minus.py:47", "review_delta")]
	assert "FLOOR_MULTI_REVIEWER" not in row_map[("src/tolerance_far.py:40", "review_epsilon")]
	assert "FLOOR_MULTI_REVIEWER" not in row_map[("src/tolerance_far.py:44", "review_zeta")]


def test_deterministic_output_is_byte_identical_for_same_input(tmp_path: Path) -> None:
	out_a = tmp_path / "floor_a.txt"
	out_b = tmp_path / "floor_b.txt"
	_run_floor_rules(FIXTURES / "reviewer_bundle_mixed.txt", out_a)
	_run_floor_rules(FIXTURES / "reviewer_bundle_mixed.txt", out_b)
	assert out_a.read_bytes() == out_b.read_bytes()


def test_missing_keyword_override_falls_back_to_builtin_and_warns(tmp_path: Path) -> None:
	out_file = tmp_path / "floor_tags.txt"
	_, stderr = _run_floor_rules(
		FIXTURES / "reviewer_bundle_mixed.txt",
		out_file,
		keyword_file=tmp_path / "missing-keywords.txt",
	)
	rows = _parse_floor_tags(out_file)
	assert rows, "built-in keywords should still produce tagged output"
	assert "event=keyword_file_missing" in stderr


def test_keyword_override_replaces_builtin_catalog(tmp_path: Path) -> None:
	bundle = FIXTURES / "reviewer_bundle_override.txt"
	custom_keywords = FIXTURES / "floor_keywords_override.txt"
	out_file = tmp_path / "floor_tags.txt"
	_, stderr = _run_floor_rules(bundle, out_file, keyword_file=custom_keywords)
	rows = _parse_floor_tags(out_file)
	assert len(rows) == 1
	anchor, tags, reviewer, _ = rows[0]
	assert anchor == "src/override.py:12"
	assert reviewer == "review_override"
	assert tags == ["FLOOR_CRITICAL_KEYWORD:CUSTOM"]
	assert "event=keyword_file_missing" not in stderr


def test_fail_open_for_missing_bundle_writes_valid_empty_output(tmp_path: Path) -> None:
	out_file = tmp_path / "floor_tags.txt"
	_, stderr = _run_floor_rules(tmp_path / "does-not-exist.txt", out_file)
	assert out_file.exists()
	assert out_file.read_text(encoding="utf-8") == ""
	assert "event=bundle_missing_or_empty" in stderr


def test_missing_bundle_with_unwritable_output_path_still_fails_open(tmp_path: Path) -> None:
	blocked_parent = tmp_path / "occupied"
	blocked_parent.write_text("not-a-directory", encoding="utf-8")
	out_file = blocked_parent / "floor_tags.txt"

	_, stderr = _run_floor_rules(tmp_path / "does-not-exist.txt", out_file)
	assert "event=bundle_missing_or_empty" in stderr
	assert "event=fail_open" in stderr
	assert not out_file.exists()


def test_malformed_partial_bundle_is_tolerated_and_skipped_safely(tmp_path: Path) -> None:
	out_file = tmp_path / "floor_tags.txt"
	_run_floor_rules(FIXTURES / "reviewer_bundle_malformed.txt", out_file)
	rows = _parse_floor_tags(out_file)
	assert rows == []


def test_excerpt_truncation_caps_at_240_chars(tmp_path: Path) -> None:
	out_file = tmp_path / "floor_tags.txt"
	_run_floor_rules(FIXTURES / "reviewer_bundle_long_excerpt.txt", out_file)
	rows = _parse_floor_tags(out_file)
	assert len(rows) == 1
	_, _, _, excerpt = rows[0]
	assert len(excerpt) == 240


def test_fail_open_unhandled_error_emits_empty_valid_output(tmp_path: Path) -> None:
	bundle = tmp_path / "bundle.txt"
	bundle.write_text(
		"FILE_PATH: /tmp/review_fail.txt\n"
		"BYTES: 1\n"
		"SHA256: x\n"
		"CONTENT_START\n"
		"File: src/fail.sh\n"
		"Line or code reference: line 1\n"
		"Problem: command injection\n"
		"ISSUE_CONFIDENCE: 5\n"
		"CONTENT_END\n",
		encoding="utf-8",
	)

	broken_out_dir = tmp_path / "not_a_dir"
	broken_out_dir.write_text("occupied", encoding="utf-8")
	out_file = broken_out_dir / "floor_tags.txt"

	_, stderr = _run_floor_rules(bundle, out_file)
	assert "event=fail_open" in stderr
	assert "reason=unhandled_error" in stderr
	assert not out_file.exists() or out_file.read_text(encoding="utf-8") == ""


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	passed = 0
	failed = 0

	for func in test_funcs:
		name = func.__name__
		try:
			params = list(inspect.signature(func).parameters)
			if not params:
				func()
			elif params == ["tmp_path"]:
				with tempfile.TemporaryDirectory(prefix="review-floor-rules-") as td:
					func(Path(td))
			else:
				raise TypeError(f"unsupported test signature for {name}: {params}")
			print(f"  PASS  {name}")
			passed += 1
		except AssertionError as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
		except Exception as e:
			print(f"  ERROR {name}: {type(e).__name__}: {e}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
