#!/usr/bin/env python3
"""Focused regression coverage for fingerprint verification tiers."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _verifier_module():
	spec = importlib.util.spec_from_file_location(
		"verify_integration_fingerprints",
		REPO_ROOT / "scripts" / "verify_integration_fingerprints.py",
	)
	assert spec is not None and spec.loader is not None
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


def _verifier_sandbox(files: dict[str, str], fingerprints: dict) -> tuple[Path, Path]:
	td = Path(tempfile.mkdtemp(prefix="verifier-tier-test-"))
	for rel, content in files.items():
		path = td / rel
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content, encoding="utf-8")
	fp_path = td / "fingerprints.json"
	fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
	return td, fp_path


@contextlib.contextmanager
def _sandbox(files: dict[str, str], fingerprints: dict):
	sandbox, fp_path = _verifier_sandbox(files, fingerprints)
	try:
		yield sandbox, fp_path
	finally:
		shutil.rmtree(sandbox, ignore_errors=True)


def _run_verifier(mod, argv: list[str], cwd: Path) -> tuple[int, str, str]:
	out_buf = io.StringIO()
	err_buf = io.StringIO()
	prev_cwd = os.getcwd()
	try:
		os.chdir(cwd)
		with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
			rc = mod.main(argv)
	finally:
		os.chdir(prev_cwd)
	return rc, out_buf.getvalue(), err_buf.getvalue()


def _write_json(path: Path, data: dict) -> None:
	path.write_text(json.dumps(data), encoding="utf-8")


def _must_contain_issue(issue: int, pr: int, regexes: list[str], file: str = "scripts/example.py") -> dict[str, object]:
	return {
		"issue": issue,
		"pr": pr,
		"must_contain": [{"file": file, "regex": regex} for regex in regexes],
		"must_not_contain": [],
		"must_not_exist": [],
	}


def _regexes(prefix: str, count: int) -> list[str]:
	return [f"{prefix}_{i}" for i in range(count)]


def test_verify_integration_fingerprints_default_and_explicit_strict_match() -> None:
	mod = _verifier_module()
	fingerprints = {
		"1500": _must_contain_issue(1500, 1501, ["EXPECTED_LINE"]),
	}
	with _sandbox({"scripts/example.py": "ACTUAL_LINE\n"}, fingerprints) as (sandbox, fp_path):
		default_result = _run_verifier(mod, [str(fp_path)], sandbox)
		strict_result = _run_verifier(mod, ["--verification-tier", "strict", str(fp_path)], sandbox)
		assert default_result == strict_result
		assert default_result[0] == 1


def test_verify_integration_fingerprints_ratio_passes_at_ninety_five_percent_per_issue() -> None:
	mod = _verifier_module()
	regexes = _regexes("LINE", 20)
	fingerprints = {
		"1500": _must_contain_issue(1500, 1501, regexes),
	}
	files = {"scripts/example.py": "\n".join(regexes[:-1]) + "\n"}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		rc, out, err = _run_verifier(mod, ["--verification-tier", "ratio", str(fp_path)], sandbox)
		assert rc == 0
		assert "must_contain satisfied 19/20 (95%)" in out
		assert "tier=ratio tolerated 1 must_contain violation(s)" in out
		assert "Integration fingerprint verification PASSED under tier 'ratio'" in out
		assert err == ""


def test_verify_integration_fingerprints_ratio_fails_below_ninety_five_percent() -> None:
	mod = _verifier_module()
	regexes = _regexes("LINE", 20)
	fingerprints = {
		"1500": _must_contain_issue(1500, 1501, regexes),
	}
	files = {"scripts/example.py": "\n".join(regexes[:18]) + "\n"}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		rc, out, err = _run_verifier(mod, ["--verification-tier", "ratio", str(fp_path)], sandbox)
		assert rc == 1
		assert "verification tier 'ratio' requires >=95% of eligible must_contain patterns" in out
		assert "18/20 matched after resolver" in out
		assert err == ""


def test_verify_integration_fingerprints_count_only_passes_when_each_issue_has_one_match() -> None:
	mod = _verifier_module()
	fingerprints = {
		"1500": _must_contain_issue(1500, 1501, ["ISSUE_A_PRESENT", "ISSUE_A_MISSING"]),
		"1501": _must_contain_issue(1501, 1502, ["ISSUE_B_MISSING", "ISSUE_B_PRESENT"]),
	}
	files = {"scripts/example.py": "ISSUE_A_PRESENT\nISSUE_B_PRESENT\n"}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		rc, out, err = _run_verifier(mod, ["--verification-tier", "count_only", str(fp_path)], sandbox)
		assert rc == 0
		assert "Integration fingerprint verification PASSED under tier 'count_only'" in out
		assert "tier=count_only tolerated 1 must_contain violation(s) for issue #1500" in out
		assert "tier=count_only tolerated 1 must_contain violation(s) for issue #1501" in out
		assert err == ""


def test_verify_integration_fingerprints_count_only_fails_when_any_issue_has_zero_matches() -> None:
	mod = _verifier_module()
	fingerprints = {
		"1500": _must_contain_issue(1500, 1501, ["ISSUE_A_PRESENT", "ISSUE_A_MISSING"]),
		"1501": _must_contain_issue(1501, 1502, ["ISSUE_B_MISSING_1", "ISSUE_B_MISSING_2"]),
	}
	files = {"scripts/example.py": "ISSUE_A_PRESENT\n"}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		rc, out, err = _run_verifier(mod, ["--verification-tier", "count_only", str(fp_path)], sandbox)
		assert rc == 1
		assert "issue #1501" in out
		assert "verification tier 'count_only' requires at least 1 eligible must_contain pattern" in out
		assert err == ""


def test_verify_integration_fingerprints_warn_only_emits_marker_and_suppresses_all_violations() -> None:
	mod = _verifier_module()
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [{"file": "scripts/example.py", "regex": "EXPECTED_LINE"}],
			"must_not_contain": [{"file": "scripts/example.py", "regex": "WRONG_LINE"}],
			"must_not_exist": [{"file": "scripts/deleted.py"}],
		},
	}
	files = {
		"scripts/example.py": "WRONG_LINE\n",
		"scripts/deleted.py": "reintroduced\n",
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		rc, out, err = _run_verifier(mod, ["--verification-tier", "warn_only", str(fp_path)], sandbox)
		assert rc == 0
		assert "FINGERPRINT_TIER_WARN_ONLY_V1" in out
		assert "warn_only tier suppressed fingerprint failures" in out
		assert "must_contain pattern missing from 'scripts/example.py'" in out
		assert "must_not_contain pattern reappeared in 'scripts/example.py'" in out
		assert "must_not_exist path 'scripts/deleted.py' reappeared" in out
		assert "Integration fingerprint verification FAILED" not in out
		assert err == ""


def test_verify_integration_fingerprints_compare_mode_ratio_excludes_pre_existing_drift() -> None:
	mod = _verifier_module()
	fingerprints = {
		"1500": _must_contain_issue(1500, 1501, ["KEEP_LINE", "MISSING_LINE"]),
	}
	with _sandbox({"scripts/example.py": "KEEP_LINE\n"}, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, out, err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		assert out == ""
		assert err == ""
		current_fp_path = sandbox / "current-fingerprints.json"
		_write_json(current_fp_path, fingerprints)
		rc, out, err = _run_verifier(
			mod,
			[
				"--compare-against-baseline",
				str(baseline_path),
				"--verification-tier",
				"ratio",
				str(current_fp_path),
			],
			sandbox,
		)
		assert rc == 0
		assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" in out
		assert 'fp_key=["scripts/example.py","MISSING_LINE"]' in out
		assert "must_contain satisfied 1/1 (100%)" in out
		assert "must_contain satisfied 1/2" not in out
		assert err == ""


def test_verify_integration_fingerprints_rejects_invalid_verification_tier() -> None:
	mod = _verifier_module()
	fingerprints = {
		"1500": _must_contain_issue(1500, 1501, ["EXPECTED_LINE"]),
	}
	with _sandbox({"scripts/example.py": "EXPECTED_LINE\n"}, fingerprints) as (sandbox, fp_path):
		rc, out, err = _run_verifier(mod, ["--verification-tier", "invalid-tier", str(fp_path)], sandbox)
		assert rc == 2
		assert out == ""
		assert "unsupported --verification-tier 'invalid-tier'" in err


if __name__ == "__main__":
	for _name, _value in sorted(globals().items()):
		if _name.startswith("test_") and callable(_value):
			_value()
	print("PASS")
