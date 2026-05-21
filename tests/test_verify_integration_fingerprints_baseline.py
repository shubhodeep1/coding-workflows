#!/usr/bin/env python3
"""Focused regression coverage for baseline-aware fingerprint verification."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "integration_fingerprints"


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
	td = Path(tempfile.mkdtemp(prefix="verifier-baseline-test-"))
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


def _empty_quarantine_payload() -> dict:
	return {"schema_version": "v1", "entries": []}


@contextlib.contextmanager
def _stub_quarantine_store(mod, initial_payload: dict | None = None, *, fail_load: bool = False, fail_persist: bool = False):
	store = json.loads(json.dumps(initial_payload or _empty_quarantine_payload()))
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


def test_verify_integration_fingerprints_baseline_capture_writes_schema_v1_json():
	mod = _verifier_module()
	files = {
		"scripts/example.py": "EXPECTED_LINE\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [
				{"file": "scripts/example.py", "regex": r"BANNED_LINE"},
			],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "nested" / "path" / "baseline.json"
		rc, out, err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		assert out == ""
		assert err == ""
		assert baseline_path.stat().st_mode & 0o777 == 0o644
		state = json.loads(baseline_path.read_text(encoding="utf-8"))
		assert state["schema_version"] == 1
		assert "captured_at" in state
		assert "branch" in state
		assert "head_sha" in state
		issue = state["fingerprints"]["1500"]
		assert issue["issue"] == 1500
		assert issue["pr"] == 1501
		assert issue["must_contain"] == [
			{
				"fp_key": ["scripts/example.py", "EXPECTED_LINE"],
				"file": "scripts/example.py",
				"regex": "EXPECTED_LINE",
				"satisfied": True,
			}
		]
		assert issue["must_not_contain"] == [
			{
				"fp_key": ["scripts/example.py", "BANNED_LINE"],
				"file": "scripts/example.py",
				"regex": "BANNED_LINE",
				"satisfied": True,
			}
		]


def test_verify_integration_fingerprints_baseline_capture_honours_ref_and_records_matching_head_sha():
	mod = _verifier_module()
	with tempfile.TemporaryDirectory(prefix="verifier-ref-baseline-") as td_root:
		repo = Path(td_root) / "repo"
		repo.mkdir()
		subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
		subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=repo, check=True)
		subprocess.run(["git", "config", "user.name", "CI"], cwd=repo, check=True)
		(repo / "scripts").mkdir()
		example = repo / "scripts" / "example.py"
		example.write_text("REF_LINE\n", encoding="utf-8")
		subprocess.run(["git", "add", "scripts/example.py"], cwd=repo, check=True)
		subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=repo, check=True)
		baseline_sha = subprocess.run(
			["git", "rev-parse", "HEAD"],
			cwd=repo,
			check=True,
			capture_output=True,
			text=True,
		).stdout.strip()
		subprocess.run(["git", "branch", "baseline-ref"], cwd=repo, check=True)
		example.write_text("WORKTREE_LINE\n", encoding="utf-8")
		subprocess.run(["git", "add", "scripts/example.py"], cwd=repo, check=True)
		subprocess.run(["git", "commit", "--quiet", "-m", "head"], cwd=repo, check=True)
		fingerprints = {
			"1500": {
				"issue": 1500,
				"pr": 1501,
				"must_contain": [
					{"file": "scripts/example.py", "regex": r"REF_LINE"},
				],
				"must_not_contain": [],
			}
		}
		fp_path = repo / "fingerprints.json"
		baseline_path = repo / "baseline.json"
		fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
		rc, out, err = _run_verifier(
			mod,
			[
				"--baseline-fingerprints-state",
				str(baseline_path),
				"--ref",
				"baseline-ref",
				str(fp_path),
			],
			repo,
		)
		assert rc == 0
		assert out == ""
		assert err == ""
		state = json.loads(baseline_path.read_text(encoding="utf-8"))
		assert state["head_sha"] == baseline_sha
		assert state["fingerprints"]["1500"]["must_contain"] == [
			{
				"fp_key": ["scripts/example.py", "REF_LINE"],
				"file": "scripts/example.py",
				"regex": "REF_LINE",
				"satisfied": True,
			}
		]


def test_verify_integration_fingerprints_baseline_capture_warns_but_exits_zero_on_output_write_failure():
	mod = _verifier_module()
	files = {
		"scripts/example.py": "EXPECTED_LINE\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_dir = sandbox / "baseline-dir"
		baseline_dir.mkdir()
		rc, out, err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_dir), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		assert "::warning::baseline capture failed:" in out
		assert err == ""


def test_verify_integration_fingerprints_compare_mode_passes_on_pre_existing_drift():
	mod = _verifier_module()
	files = {
		"scripts/example.py": "CURRENT_BRANCH_ONLY\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, _out, _err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		rc, out, err = _run_verifier(
			mod,
			["--compare-against-baseline", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		assert (
			"::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged "
			'fp_key=["scripts/example.py","EXPECTED_LINE"] issue=#1500 '
			'path="scripts/example.py" pattern="EXPECTED_LINE" kind=must_contain'
		) in out
		assert (
			"Integration fingerprint verification PASSED with pre-existing drift — resolver did not introduce any new regressions "
			"(pre_existing_drift_count=1; see PRE_EXISTING_FINGERPRINT_DRIFT_V1 markers above for triage)."
		) in out
		assert "Silent-regression detector" not in out
		assert err == ""


def test_verify_integration_fingerprints_capture_mode_skips_quarantined_fingerprint() -> None:
	mod = _verifier_module()
	files = {
		"scripts/example.py": "EXPECTED_LINE\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	initial_quarantine = {
		"schema_version": "v1",
		"entries": [
			{
				"fp_key": ["scripts/example.py", "EXPECTED_LINE"],
				"issue_key": "1500",
				"first_seen_run_id": "run-100",
				"last_seen_run_id": "run-101",
				"consecutive_unchanged_runs": 2,
			}
		],
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		with _stub_quarantine_store(mod, initial_quarantine), _set_env(
			FINGERPRINT_QUARANTINE_RUNS_M="2",
			GITHUB_RUN_ID="capture-skip-test-1",
		):
			rc, out, err = _run_verifier(
				mod,
				["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
				sandbox,
			)
		assert rc == 0
		assert '::warning::FINGERPRINT_QUARANTINED_V1 fp_key=["scripts/example.py","EXPECTED_LINE"] issue=#1500' in out
		state = json.loads(baseline_path.read_text(encoding="utf-8"))
		assert state["fingerprints"]["1500"]["must_contain"] == []
		assert err == ""


def test_verify_integration_fingerprints_quarantine_promotes_then_skips_next_run() -> None:
	mod = _verifier_module()
	files = {
		"scripts/example.py": "CURRENT_BRANCH_ONLY\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		with _stub_quarantine_store(mod) as get_store:
			rc, _out, _err = _run_verifier(
				mod,
				["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
				sandbox,
			)
			assert rc == 0

			with _set_env(FINGERPRINT_QUARANTINE_RUNS_M="2", GITHUB_RUN_ID="quarantine-promote-run-1"):
				rc, out_run1, err_run1 = _run_verifier(
					mod,
					["--compare-against-baseline", str(baseline_path), str(fp_path)],
					sandbox,
				)
			assert rc == 0
			assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" in out_run1
			assert "FINGERPRINT_QUARANTINED_V1" not in out_run1
			assert get_store()["entries"][0]["consecutive_unchanged_runs"] == 1
			assert err_run1 == ""

			with _set_env(FINGERPRINT_QUARANTINE_RUNS_M="2", GITHUB_RUN_ID="quarantine-promote-run-1"):
				rc, out_run1_repeat, err_run1_repeat = _run_verifier(
					mod,
					["--compare-against-baseline", str(baseline_path), str(fp_path)],
					sandbox,
				)
			assert rc == 0
			assert "FINGERPRINT_QUARANTINED_V1" not in out_run1_repeat
			assert get_store()["entries"][0]["consecutive_unchanged_runs"] == 1
			assert err_run1_repeat == ""

			with _set_env(FINGERPRINT_QUARANTINE_RUNS_M="2", GITHUB_RUN_ID="quarantine-promote-run-2"):
				rc, out_run2, err_run2 = _run_verifier(
					mod,
					["--compare-against-baseline", str(baseline_path), str(fp_path)],
					sandbox,
				)
			assert rc == 0
			assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" in out_run2
			assert "FINGERPRINT_QUARANTINED_V1" not in out_run2
			assert get_store()["entries"][0]["consecutive_unchanged_runs"] == 2
			assert err_run2 == ""

			with _set_env(FINGERPRINT_QUARANTINE_RUNS_M="2", GITHUB_RUN_ID="quarantine-promote-run-2"):
				rc, out_run2_repeat, err_run2_repeat = _run_verifier(
					mod,
					["--compare-against-baseline", str(baseline_path), str(fp_path)],
					sandbox,
				)
			assert rc == 0
			assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" not in out_run2_repeat
			assert "FINGERPRINT_QUARANTINED_V1" not in out_run2_repeat
			assert get_store()["entries"][0]["consecutive_unchanged_runs"] == 2
			assert err_run2_repeat == ""

			with _set_env(FINGERPRINT_QUARANTINE_RUNS_M="2", GITHUB_RUN_ID="quarantine-promote-run-3"):
				rc, out_run3, err_run3 = _run_verifier(
					mod,
					["--compare-against-baseline", str(baseline_path), str(fp_path)],
					sandbox,
				)
			assert rc == 0
			assert '::warning::FINGERPRINT_QUARANTINED_V1 fp_key=["scripts/example.py","EXPECTED_LINE"] issue=#1500' in out_run3
			assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" not in out_run3
			assert get_store()["entries"][0]["consecutive_unchanged_runs"] == 3
			assert err_run3 == ""

			with _set_env(FINGERPRINT_QUARANTINE_RUNS_M="2", GITHUB_RUN_ID="quarantine-promote-run-3"):
				rc, out_run3_repeat, err_run3_repeat = _run_verifier(
					mod,
					["--compare-against-baseline", str(baseline_path), str(fp_path)],
					sandbox,
				)
			assert rc == 0
			assert "FINGERPRINT_QUARANTINED_V1" not in out_run3_repeat
			assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" not in out_run3_repeat
			assert get_store()["entries"][0]["consecutive_unchanged_runs"] == 3
			assert err_run3_repeat == ""


def test_verify_integration_fingerprints_same_run_fix_clears_pending_quarantine() -> None:
	mod = _verifier_module()
	files = {
		"scripts/example.py": "CURRENT_BRANCH_ONLY\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		with _stub_quarantine_store(mod) as get_store:
			rc, _out, _err = _run_verifier(
				mod,
				["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
				sandbox,
			)
			assert rc == 0

			with _set_env(FINGERPRINT_QUARANTINE_RUNS_M="2", GITHUB_RUN_ID="quarantine-fix-run-1"):
				rc, _out, _err = _run_verifier(
					mod,
					["--compare-against-baseline", str(baseline_path), str(fp_path)],
					sandbox,
				)
			assert rc == 0
			assert get_store()["entries"][0]["consecutive_unchanged_runs"] == 1

			with _set_env(FINGERPRINT_QUARANTINE_RUNS_M="2", GITHUB_RUN_ID="quarantine-fix-run-2"):
				rc, out_run2, err_run2 = _run_verifier(
					mod,
					["--compare-against-baseline", str(baseline_path), str(fp_path)],
					sandbox,
				)
			assert rc == 0
			assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" in out_run2
			assert get_store()["entries"][0]["consecutive_unchanged_runs"] == 2
			assert err_run2 == ""

			(sandbox / "scripts" / "example.py").write_text("EXPECTED_LINE\n", encoding="utf-8")
			with _set_env(FINGERPRINT_QUARANTINE_RUNS_M="2", GITHUB_RUN_ID="quarantine-fix-run-2"):
				rc, out_fixed, err_fixed = _run_verifier(
					mod,
					["--compare-against-baseline", str(baseline_path), str(fp_path)],
					sandbox,
				)
			assert rc == 0
			assert "FINGERPRINT_QUARANTINED_V1" not in out_fixed
			assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" not in out_fixed
			assert get_store()["entries"] == []
			assert err_fixed == ""

			with _set_env(FINGERPRINT_QUARANTINE_RUNS_M="2", GITHUB_RUN_ID="quarantine-fix-run-3"):
				rc, out_run3, err_run3 = _run_verifier(
					mod,
					["--compare-against-baseline", str(baseline_path), str(fp_path)],
					sandbox,
				)
			assert rc == 0
			assert "FINGERPRINT_QUARANTINED_V1" not in out_run3
			assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 fixed_by_resolver" in out_run3
			assert err_run3 == ""


def test_verify_integration_fingerprints_quarantine_load_failure_fails_open() -> None:
	mod = _verifier_module()
	files = {
		"scripts/example.py": "CURRENT_BRANCH_ONLY\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, _out, _err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		with _stub_quarantine_store(mod, fail_load=True), _set_env(
			FINGERPRINT_QUARANTINE_RUNS_M="2",
			GITHUB_RUN_ID="quarantine-fail-open-1",
		):
			rc, out, err = _run_verifier(
				mod,
				["--compare-against-baseline", str(baseline_path), str(fp_path)],
				sandbox,
			)
		assert rc == 0
		assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" in out
		assert "FINGERPRINT_QUARANTINED_V1" not in out
		assert err == ""


def test_verify_integration_fingerprints_compare_failure_does_not_persist_quarantine_state() -> None:
	mod = _verifier_module()
	files = {
		"scripts/example.py": "STABLE_LINE\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"LEGACY_LINE"},
				{"file": "scripts/example.py", "regex": r"STABLE_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, _out, _err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		(sandbox / "scripts" / "example.py").write_text("BROKEN_LINE\n", encoding="utf-8")
		with _stub_quarantine_store(mod) as get_store:
			rc, out, err = _run_verifier(
				mod,
				["--compare-against-baseline", str(baseline_path), str(fp_path)],
				sandbox,
			)
		assert rc == 1
		assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" in out
		assert "Integration fingerprint verification FAILED" in out
		assert get_store()["entries"] == []
		assert err == ""


def test_verify_integration_fingerprints_ratio_failure_does_not_persist_quarantine_state() -> None:
	mod = _verifier_module()
	files = {
		"scripts/example.py": "STABLE_LINE\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"LEGACY_LINE"},
				{"file": "scripts/example.py", "regex": r"STABLE_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, _out, _err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		(sandbox / "scripts" / "example.py").write_text("BROKEN_LINE\n", encoding="utf-8")
		with _stub_quarantine_store(mod) as get_store:
			rc, out, err = _run_verifier(
				mod,
				[
					"--compare-against-baseline",
					str(baseline_path),
					"--verification-tier",
					"ratio",
					str(fp_path),
				],
				sandbox,
			)
		assert rc == 1
		assert "PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" in out
		assert "Integration fingerprint verification FAILED" in out
		assert get_store()["entries"] == []
		assert err == ""


def test_verify_integration_fingerprints_compare_mode_passes_on_pre_existing_check_error():
	mod = _verifier_module()
	with tempfile.TemporaryDirectory(prefix="verifier-pre-existing-check-error-") as outside_td:
		outside_path = Path(outside_td) / "secret.txt"
		outside_path.write_text("EXPECTED_LINE\n", encoding="utf-8")
		fingerprints = {
			"1500": {
				"issue": 1500,
				"pr": 1501,
				"must_contain": [
					{"file": str(outside_path), "regex": r"EXPECTED_LINE"},
				],
				"must_not_contain": [],
			}
		}
		with _sandbox({}, fingerprints) as (sandbox, fp_path):
			baseline_path = sandbox / "baseline.json"
			rc, _out, capture_err = _run_verifier(
				mod,
				["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
				sandbox,
			)
			assert rc == 0
			assert "fingerprint path resolves outside repository root" in capture_err
			rc, out, err = _run_verifier(
				mod,
				["--compare-against-baseline", str(baseline_path), str(fp_path)],
				sandbox,
			)
			assert rc == 0
			assert "::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" in out
			assert "issue=#1500" in out
			assert "kind=must_contain" in out
			assert "Integration fingerprint verification FAILED" not in out
			assert (
				"Integration fingerprint verification PASSED with pre-existing drift — resolver did not introduce any new regressions "
				"(pre_existing_drift_count=1; see PRE_EXISTING_FINGERPRINT_DRIFT_V1 markers above for triage)."
			) in out
			assert "fingerprint path resolves outside repository root" in err


def test_verify_integration_fingerprints_compare_mode_fails_on_resolver_introduced_regression():
	mod = _verifier_module()
	files = {
		"scripts/example.py": "EXPECTED_LINE\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, _out, _err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		(sandbox / "scripts" / "example.py").write_text("REGRESSED_LINE\n", encoding="utf-8")
		rc, out, err = _run_verifier(
			mod,
			["--compare-against-baseline", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 1
		assert "Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent:" in out
		assert "Refusing to create [ai-merge-resolve] commit." in out
		assert err == ""


def test_verify_integration_fingerprints_compare_mode_emits_fixed_by_resolver_notice():
	mod = _verifier_module()
	files = {
		"scripts/example.py": "CURRENT_BRANCH_ONLY\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, _out, _err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		(sandbox / "scripts" / "example.py").write_text("EXPECTED_LINE\n", encoding="utf-8")
		rc, out, err = _run_verifier(
			mod,
			["--compare-against-baseline", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		assert (
			"::notice::PRE_EXISTING_FINGERPRINT_DRIFT_V1 fixed_by_resolver "
			'fp_key=["scripts/example.py","EXPECTED_LINE"] issue=#1500 path="scripts/example.py"'
		) in out
		assert "Integration fingerprint verification PASSED — all merged sub-issue intent preserved." in out
		assert err == ""


def test_verify_integration_fingerprints_compare_mode_rejects_must_not_exist_regression():
	mod = _verifier_module()
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [],
			"must_not_contain": [],
			"must_not_exist": [{"file": "scripts/deleted.py"}],
		}
	}
	with _sandbox({}, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, _out, _err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		deleted_path = sandbox / "scripts" / "deleted.py"
		deleted_path.parent.mkdir(parents=True, exist_ok=True)
		deleted_path.write_text("reintroduced\n", encoding="utf-8")
		rc, out, err = _run_verifier(
			mod,
			["--compare-against-baseline", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 1
		assert "Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent:" in out
		assert "must_not_exist path 'scripts/deleted.py' reappeared after resolver" in out
		assert err == ""


def test_verify_integration_fingerprints_compare_mode_passes_on_pre_existing_must_not_exist_violation():
	mod = _verifier_module()
	files = {
		"scripts/deleted.py": "reintroduced\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [],
			"must_not_contain": [],
			"must_not_exist": [{"file": "scripts/deleted.py"}],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, _out, _err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		rc, out, err = _run_verifier(
			mod,
			["--compare-against-baseline", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		assert (
			"::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged "
			'fp_key=["scripts/deleted.py"] issue=#1500 path="scripts/deleted.py" kind=must_not_exist'
		) in out
		assert (
			"Integration fingerprint verification PASSED with pre-existing drift — resolver did not introduce any new regressions "
			"(pre_existing_drift_count=1; see PRE_EXISTING_FINGERPRINT_DRIFT_V1 markers above for triage)."
		) in out
		assert err == ""


def test_verify_integration_fingerprints_compare_mode_falls_back_to_absolute_check_for_current_only_fingerprints():
	mod = _verifier_module()
	files = {
		"scripts/stable.py": "STABLE_LINE\n",
		"scripts/new.py": "WRONG_LINE\n",
	}
	baseline_fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/stable.py", "regex": r"STABLE_LINE"},
			],
			"must_not_contain": [],
		}
	}
	current_fingerprints = {
		**baseline_fingerprints,
		"1600": {
			"issue": 1600,
			"pr": 1601,
			"must_contain": [
				{"file": "scripts/new.py", "regex": r"NEEDED_LINE"},
			],
			"must_not_contain": [],
		},
	}
	with _sandbox(files, baseline_fingerprints) as (sandbox, baseline_fp_path):
		current_fp_path = sandbox / "current-fingerprints.json"
		_write_json(current_fp_path, current_fingerprints)
		baseline_path = sandbox / "baseline.json"
		rc, _out, _err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(baseline_fp_path)],
			sandbox,
		)
		assert rc == 0
		rc, out, err = _run_verifier(
			mod,
			["--compare-against-baseline", str(baseline_path), str(current_fp_path)],
			sandbox,
		)
		assert rc == 1
		assert "issue #1600" in out
		assert "must_contain pattern missing from 'scripts/new.py'" in out
		assert err == ""


def test_verify_integration_fingerprints_compare_mode_falls_back_to_absolute_verify_on_malformed_baseline():
	mod = _verifier_module()
	files = {
		"scripts/example.py": "CURRENT_BRANCH_ONLY\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		bad_baseline = sandbox / "bad-baseline.json"
		bad_baseline.write_text("not json at all", encoding="utf-8")
		rc, out, err = _run_verifier(
			mod,
			["--compare-against-baseline", str(bad_baseline), str(fp_path)],
			sandbox,
		)
		assert rc == 1
		assert "::warning::baseline malformed (baseline JSON unparseable" in out
		assert "Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent:" in out
		assert err == ""


def test_verify_integration_fingerprints_rejects_mutually_exclusive_baseline_flags():
	mod = _verifier_module()
	files = {
		"scripts/example.py": "EXPECTED_LINE\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, out, err = _run_verifier(
			mod,
			[
				"--baseline-fingerprints-state",
				str(baseline_path),
				"--compare-against-baseline",
				str(baseline_path),
				str(fp_path),
			],
			sandbox,
		)
		assert rc == 2
		assert out == ""
		assert (
			err
			== "::error::verify_integration_fingerprints: --baseline-fingerprints-state and --compare-against-baseline are mutually exclusive\n"
		)


def test_verify_integration_fingerprints_pr1569_fixture_passes_in_compare_mode_but_fails_in_legacy_mode():
	mod = _verifier_module()
	fixture_path = FIXTURES_DIR / "pr1569_run_24872524074.json"
	files = {
		"scripts/review_conflict_resolve.sh": "preserved resolver line\nresolver body still present\n",
		"scripts/review_conflict_prepare.sh": "prepare guard remains\n",
	}
	with _sandbox(files, {}) as (sandbox, _fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, _out, _err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fixture_path)],
			sandbox,
		)
		assert rc == 0
		legacy_rc, legacy_out, legacy_err = _run_verifier(mod, [str(fixture_path)], sandbox)
		assert legacy_rc == 1
		assert "issue #1519" in legacy_out
		assert legacy_err == ""
		compare_rc, compare_out, compare_err = _run_verifier(
			mod,
			["--compare-against-baseline", str(baseline_path), str(fixture_path)],
			sandbox,
		)
		assert compare_rc == 0
		assert "::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged" in compare_out
		assert (
			"Integration fingerprint verification PASSED with pre-existing drift — resolver did not introduce any new regressions "
			"(pre_existing_drift_count=3; see PRE_EXISTING_FINGERPRINT_DRIFT_V1 markers above for triage)."
		) in compare_out
		assert compare_err == ""


def test_verify_integration_fingerprints_compare_mode_excludes_invalid_regexes_from_ratio():
	mod = _verifier_module()
	files = {
		"scripts/example.py": "EXPECTED_LINE\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/example.py", "regex": r"EXPECTED_LINE"},
				{"file": "scripts/example.py", "regex": r"["},
			],
			"must_not_contain": [],
		}
	}
	with _sandbox(files, fingerprints) as (sandbox, fp_path):
		baseline_path = sandbox / "baseline.json"
		rc, capture_out, capture_err = _run_verifier(
			mod,
			["--baseline-fingerprints-state", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		assert capture_out == ""
		assert "::warning::fingerprint regex compile failed" in capture_err
		rc, out, err = _run_verifier(
			mod,
			["--compare-against-baseline", str(baseline_path), str(fp_path)],
			sandbox,
		)
		assert rc == 0
		assert "::warning::fingerprint regex compile failed" not in out
		assert "must_contain satisfied 1/1 (100%)" in out
		assert "must_contain satisfied 1/2" not in out
		assert "::warning::fingerprint regex compile failed" in err


def test_verify_integration_fingerprints_rejects_out_of_tree_fingerprint_paths():
	mod = _verifier_module()
	with tempfile.TemporaryDirectory(prefix="verifier-out-of-tree-") as outside_td:
		outside_path = Path(outside_td) / "secret.txt"
		outside_path.write_text("EXPECTED_LINE\n", encoding="utf-8")
		fingerprints = {
			"1500": {
				"issue": 1500,
				"pr": 1501,
				"must_contain": [
					{"file": str(outside_path), "regex": r"EXPECTED_LINE"},
				],
				"must_not_contain": [],
			}
		}
		with _sandbox({}, fingerprints) as (sandbox, fp_path):
			rc, out, err = _run_verifier(mod, [str(fp_path)], sandbox)
			assert rc == 1
			assert "Integration fingerprint verification FAILED" in out
			assert "could not be checked — fingerprint path resolves outside repository root." in out
			assert "fingerprint verifier could not read" in err
			assert "fingerprint path resolves outside repository root" in err
			list_rc, list_out, list_err = _run_verifier(
				mod,
				["--list-violated-files", str(fp_path)],
				sandbox,
			)
			assert list_rc == 0
			assert list_out == ""
			assert "fingerprint verifier could not read" in list_err


if __name__ == "__main__":
	for _name, _value in sorted(globals().items()):
		if _name.startswith("test_") and callable(_value):
			_value()
	print("PASS")
