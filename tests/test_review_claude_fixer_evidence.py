#!/usr/bin/env python3
"""Unit tests for scripts/review_claude_fixer_evidence.py.

Claude-fixer decisions (the checks-pending merge check, later the GPT judge)
trust only evidence read back from a workflow run verified through the API:
the run must belong to this repository, be started from the default branch or
the PR head branch without a changed caller workflow, run the library review
workflow at a trusted ref, be completed, and carry an evidence artifact for
the same PR and head. Everything else is "unverified".
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_claude_fixer_evidence.py"

_SPEC = importlib.util.spec_from_file_location("review_claude_fixer_evidence", SCRIPT)
evidence_mod = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["review_claude_fixer_evidence"] = evidence_mod
_SPEC.loader.exec_module(evidence_mod)

REPO = "o/r"
PR = 42
HEAD = "c" * 40
RUN_ID = "777"
PR_REF = "claude/implement-plan-demo-phase-1"
LIBRARY_REF = {"path": "shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@main", "ref": "refs/heads/main", "sha": "d" * 40}


def _evidence(**overrides) -> dict:
	payload = evidence_mod.build_evidence(
		pr=PR,
		head_sha=HEAD,
		round_number=2,
		outcome="checks-pending",
		ledger_sha256="a" * 64,
		finding_count=0,
		failed_checks=[],
	)
	payload.update(overrides)
	return payload


def _zip(payload: dict | None, name: str = "evidence.json") -> bytes:
	buf = io.BytesIO()
	with zipfile.ZipFile(buf, "w") as archive:
		if payload is not None:
			archive.writestr(name, json.dumps(payload))
		archive.writestr("reviewer_consensus.txt", "=== CONSENSUS FINDINGS ===\n")
	return buf.getvalue()


def _run(**overrides) -> dict:
	run = {
		"id": int(RUN_ID),
		"status": "completed",
		"conclusion": "success",
		"path": ".github/workflows/internal-review.yml",
		"head_branch": "main",
		"head_sha": "e" * 40,
		"repository": {"full_name": REPO},
		"head_repository": {"full_name": REPO},
		"referenced_workflows": [LIBRARY_REF],
	}
	run.update(overrides)
	return run


class FakeApi:
	def __init__(self, *, run=None, artifacts=None, zip_bytes=None, files=None, repo_meta=None):
		self.responses = {
			f"repos/{REPO}/actions/runs/{RUN_ID}": run if run is not None else _run(),
			f"repos/{REPO}/actions/runs/{RUN_ID}/artifacts?per_page=100": artifacts if artifacts is not None else {"artifacts": [{"id": 5, "name": f"claude-fixer-evidence-{RUN_ID}-1", "expired": False}]},
			f"repos/{REPO}/pulls/{PR}/files?per_page=100": files if files is not None else [[{"filename": "scripts/x.sh"}]],
			f"repos/{REPO}": repo_meta if repo_meta is not None else {"default_branch": "main"},
		}
		self.zip_bytes = zip_bytes if zip_bytes is not None else {5: _zip(_evidence())}
		self.calls: list[str] = []

	def __call__(self, path: str, paginate: bool = False):
		self.calls.append(path)
		if path.endswith("/zip"):
			artifact_id = int(path.split("/")[-2])
			return self.zip_bytes.get(artifact_id)
		value = self.responses.get(path)
		if value is None or value == "FAIL":
			return None
		return json.dumps(value).encode()


def _verify(api: FakeApi, **overrides) -> dict:
	kwargs = {
		"repo": REPO,
		"pr": PR,
		"head_sha": HEAD,
		"run_id": RUN_ID,
		"expect_outcome": "checks-pending",
		"pr_head_ref": PR_REF,
		"default_branch": "main",
		"api": api,
	}
	kwargs.update(overrides)
	return evidence_mod.verify_evidence(**kwargs)


def test_default_branch_caller_with_library_at_main_is_verified():
	api = FakeApi()
	result = _verify(api)
	assert result["verified"] is True and result["reason"] == "ok"
	assert result["evidence"]["outcome"] == "checks-pending" and result["evidence"]["round"] == 2
	# Budget: run, artifacts, download. No PR files read for a default-branch caller.
	assert api.calls == [f"repos/{REPO}/actions/runs/{RUN_ID}", f"repos/{REPO}/actions/runs/{RUN_ID}/artifacts?per_page=100", f"repos/{REPO}/actions/artifacts/5/zip"]


def test_direct_library_dispatch_from_default_branch_is_verified(monkeypatch):
	# The library dispatches review_autofix.yml itself (no referenced workflow).
	monkeypatch.setattr(evidence_mod, "LIBRARY_REPOSITORY", REPO)
	api = FakeApi(run=_run(path=".github/workflows/review_autofix.yml", referenced_workflows=[]))
	assert _verify(api)["verified"] is True
	# The same file run from the PR head branch is PR-controlled code.
	api = FakeApi(run=_run(path=".github/workflows/review_autofix.yml", referenced_workflows=[], head_branch=PR_REF, head_sha=HEAD))
	assert _verify(api)["reason"] == "untrusted_review_workflow"


def test_direct_dispatch_of_a_non_library_review_workflow_is_rejected():
	api = FakeApi(run=_run(path=".github/workflows/review_autofix.yml", referenced_workflows=[]))
	assert _verify(api)["reason"] == "untrusted_review_workflow"


def test_pr_head_branch_caller_is_verified_when_the_pr_leaves_the_caller_alone():
	api = FakeApi(run=_run(head_branch=PR_REF, head_sha=HEAD))
	result = _verify(api)
	assert result["verified"] is True
	assert f"repos/{REPO}/pulls/{PR}/files?per_page=100" in api.calls


def test_changed_files_file_replaces_the_pr_files_read():
	with tempfile.TemporaryDirectory() as td:
		changed = Path(td) / "changed.txt"
		changed.write_text(".github/workflows/internal-review.yml\n", encoding="utf-8")
		api = FakeApi(run=_run(head_branch=PR_REF, head_sha=HEAD))
		result = _verify(api, changed_files_file=str(changed))
	assert result["reason"] == "caller_workflow_changed_by_pr"
	assert f"repos/{REPO}/pulls/{PR}/files?per_page=100" not in api.calls


def test_pr_that_changes_the_caller_workflow_is_rejected():
	files = [[{"filename": "scripts/x.sh"}], [{"filename": ".github/workflows/other.yml", "previous_filename": ".github/workflows/internal-review.yml"}]]
	api = FakeApi(run=_run(head_branch=PR_REF, head_sha=HEAD), files=files)
	assert _verify(api)["reason"] == "caller_workflow_changed_by_pr"


def test_dispatch_from_another_branch_is_rejected():
	api = FakeApi(run=_run(head_branch="feature/forged", head_sha=HEAD))
	assert _verify(api)["reason"] == "untrusted_caller_ref"


def test_pr_head_branch_run_at_another_commit_is_rejected():
	api = FakeApi(run=_run(head_branch=PR_REF, head_sha="f" * 40))
	assert _verify(api)["reason"] == "untrusted_caller_ref"


def test_fork_head_repository_is_rejected():
	api = FakeApi(run=_run(head_branch=PR_REF, head_sha=HEAD, head_repository={"full_name": "fork/r"}))
	assert _verify(api)["reason"] == "untrusted_caller_ref"


def test_missing_head_repository_on_a_pr_head_run_is_rejected():
	# A deleted fork's run carries no head repository; that must not read as same-repo.
	for head_repository in (None, {}):
		api = FakeApi(run=_run(head_branch=PR_REF, head_sha=HEAD, head_repository=head_repository))
		assert _verify(api)["reason"] == "untrusted_caller_ref"


def test_library_at_an_untrusted_ref_is_rejected_and_release_pins_are_accepted():
	feature = {"path": "shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@feature", "ref": "refs/heads/feature"}
	assert _verify(FakeApi(run=_run(referenced_workflows=[feature])))["reason"] == "untrusted_review_workflow"
	other = {"path": "evil/lib/.github/workflows/review_autofix.yml@main", "ref": "refs/heads/main"}
	assert _verify(FakeApi(run=_run(referenced_workflows=[other])))["reason"] == "untrusted_review_workflow"
	for entry in (
		{"path": "shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable", "ref": "refs/tags/stable"},
		{"path": "shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@" + "1" * 40, "sha": "1" * 40},
	):
		assert _verify(FakeApi(run=_run(referenced_workflows=[entry])))["verified"] is True, entry


def test_wrong_repository_incomplete_run_and_api_failure_are_rejected():
	assert _verify(FakeApi(run=_run(repository={"full_name": "x/y"})))["reason"] == "run_repository_mismatch"
	assert _verify(FakeApi(run=_run(status="in_progress")))["reason"] == "run_not_completed"
	assert _verify(FakeApi(run="FAIL"))["reason"] == "run_unavailable"
	assert _verify(FakeApi(), run_id="0")["reason"] == "invalid_run_id"
	assert _verify(FakeApi(), head_sha="nothex")["reason"] == "invalid_head"


def test_default_branch_is_looked_up_when_not_passed():
	api = FakeApi(repo_meta={"default_branch": "trunk"}, run=_run(head_branch="trunk"))
	assert _verify(api, default_branch="")["verified"] is True
	assert f"repos/{REPO}" in api.calls
	assert _verify(FakeApi(repo_meta="FAIL"), default_branch="")["reason"] == "default_branch_unavailable"


def test_evidence_must_match_pr_head_and_outcome():
	for payload, reason in (
		(_evidence(pr=41), "evidence_pr_or_head_mismatch"),
		(_evidence(head_sha="d" * 40), "evidence_pr_or_head_mismatch"),
		(_evidence(outcome="findings"), "evidence_outcome_mismatch"),
		(_evidence(schema=2), "evidence_malformed"),
	):
		assert _verify(FakeApi(zip_bytes={5: _zip(payload)}))["reason"] == reason
	assert _verify(FakeApi(), expect_round=3)["reason"] == "evidence_round_mismatch"
	assert _verify(FakeApi(), expect_round=2)["verified"] is True
	assert _verify(FakeApi(), expect_outcome="")["verified"] is True


def test_missing_expired_or_broken_artifacts_are_rejected():
	assert _verify(FakeApi(artifacts={"artifacts": []}))["reason"] == "artifact_missing"
	expired = {"artifacts": [{"id": 5, "name": f"claude-fixer-evidence-{RUN_ID}-1", "expired": True}]}
	assert _verify(FakeApi(artifacts=expired))["reason"] == "artifact_missing"
	other_run = {"artifacts": [{"id": 5, "name": "claude-fixer-evidence-778-1", "expired": False}]}
	assert _verify(FakeApi(artifacts=other_run))["reason"] == "artifact_missing"
	assert _verify(FakeApi(zip_bytes={}))["reason"] == "artifact_download_failed"
	assert _verify(FakeApi(zip_bytes={5: b"not a zip"}))["reason"] == "evidence_malformed"
	assert _verify(FakeApi(zip_bytes={5: _zip(None)}))["reason"] == "evidence_malformed"


def test_newest_attempt_artifact_wins():
	artifacts = {"artifacts": [
		{"id": 5, "name": f"claude-fixer-evidence-{RUN_ID}-1", "expired": False},
		{"id": 6, "name": f"claude-fixer-evidence-{RUN_ID}-2", "expired": False},
	]}
	api = FakeApi(artifacts=artifacts, zip_bytes={5: _zip(_evidence(outcome="findings")), 6: _zip(_evidence())})
	assert _verify(api)["verified"] is True
	assert f"repos/{REPO}/actions/artifacts/6/zip" in api.calls


def test_build_evidence_rejects_bad_values():
	for kwargs in ({"outcome": "merged"}, {"head_sha": "short"}, {"ledger_sha256": "zz"}):
		base = {"pr": 1, "head_sha": HEAD, "round_number": 1, "outcome": "clean", "ledger_sha256": "", "finding_count": 0, "failed_checks": []}
		base.update(kwargs)
		try:
			evidence_mod.build_evidence(**base)
		except ValueError:
			continue
		raise AssertionError(kwargs)


def _cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
	return subprocess.run(
		[sys.executable, str(SCRIPT), *args],
		capture_output=True,
		text=True,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})},
	)


def test_write_cli_writes_evidence_and_ledger_copy():
	with tempfile.TemporaryDirectory() as td:
		ledger = Path(td) / "ledger.txt"
		ledger.write_text("ledger\n", encoding="utf-8")
		out = Path(td) / "evidence"
		proc = _cli("write", "--out-dir", str(out), "--pr", "42", "--head", HEAD, "--round", "3", "--outcome", "findings",
			"--ledger-sha256", "b" * 64, "--finding-count", "2", "--failed-checks", "ci / lint,,ci / test", "--ledger-file", str(ledger))
		assert proc.returncode == 0, proc.stderr
		payload = json.loads((out / "evidence.json").read_text(encoding="utf-8"))
		assert (out / "reviewer_consensus.txt").read_text(encoding="utf-8") == "ledger\n"
	assert payload == {
		"schema": 1, "pr": 42, "head_sha": HEAD, "round": 3, "outcome": "findings", "ledger_sha256": "b" * 64,
		"finding_count": 2, "failed_checks": ["ci / lint", "ci / test"], "judge": None,
	}


def test_write_cli_rejects_a_bad_head_and_verify_cli_fails_closed():
	with tempfile.TemporaryDirectory() as td:
		proc = _cli("write", "--out-dir", td, "--pr", "1", "--head", "bad", "--round", "1", "--outcome", "clean")
	assert proc.returncode == 2
	with tempfile.TemporaryDirectory() as td:
		# No gh on PATH: every API read fails, verify still exits 0 with verified=false.
		proc = _cli("verify", "--repo", REPO, "--pr", "42", "--head", HEAD, "--run-id", RUN_ID, "--default-branch", "main",
			env={"PATH": td})
	assert proc.returncode == 0, proc.stderr
	result = json.loads(proc.stdout)
	assert result == {"evidence": None, "reason": "run_unavailable", "verified": False}


# ---- Claude-fixer GPT judge additions ----

LEDGER_TEXT = "=== CONSENSUS FINDINGS ===\n- scripts/a.sh:10 | severity=high\n=== END CONSENSUS FINDINGS ===\n"
LEDGER_DIGEST = __import__("hashlib").sha256(LEDGER_TEXT.encode()).hexdigest()


def _zip_with_ledger(payload: dict, ledger: str | None) -> bytes:
	buf = io.BytesIO()
	with zipfile.ZipFile(buf, "w") as archive:
		archive.writestr("evidence.json", json.dumps(payload))
		if ledger is not None:
			archive.writestr("reviewer_consensus.txt", ledger)
	return buf.getvalue()


def _findings_evidence(**overrides) -> dict:
	payload = evidence_mod.build_evidence(
		pr=PR, head_sha=HEAD, round_number=2, outcome="findings",
		ledger_sha256=LEDGER_DIGEST, finding_count=1, failed_checks=[],
	)
	payload.update(overrides)
	return payload


def test_ledger_out_writes_the_verified_ledger_only_when_its_digest_matches():
	with tempfile.TemporaryDirectory() as td:
		out = Path(td) / "ledger.txt"
		api = FakeApi(zip_bytes={5: _zip_with_ledger(_findings_evidence(), LEDGER_TEXT)})
		result = _verify(api, expect_outcome="findings", expect_round=2, expect_ledger=LEDGER_DIGEST, ledger_out=str(out))
		assert result["verified"] is True, result
		assert out.read_text() == LEDGER_TEXT
	for zip_bytes, reason in (
		(_zip_with_ledger(_findings_evidence(), LEDGER_TEXT + "tampered\n"), "evidence_ledger_digest_mismatch"),
		(_zip_with_ledger(_findings_evidence(), None), "evidence_ledger_missing"),
	):
		with tempfile.TemporaryDirectory() as td:
			out = Path(td) / "ledger.txt"
			result = _verify(FakeApi(zip_bytes={5: zip_bytes}), expect_outcome="findings", ledger_out=str(out))
			assert result == {"verified": False, "reason": reason, "evidence": None}
			assert not out.exists()


def test_expect_ledger_rejects_a_digest_that_differs_from_the_handoff():
	api = FakeApi(zip_bytes={5: _zip_with_ledger(_findings_evidence(), LEDGER_TEXT)})
	result = _verify(api, expect_outcome="findings", expect_ledger="b" * 64)
	assert result["reason"] == "evidence_ledger_mismatch" and result["verified"] is False


def test_require_judge_needs_judge_rulings():
	rulings = {"decision": "merge", "rulings": [{"file": "scripts/a.sh", "line": 10, "ruling": "invalid"}]}
	api = FakeApi(zip_bytes={5: _zip_with_ledger(_findings_evidence(judge=rulings), LEDGER_TEXT)})
	result = _verify(api, expect_outcome="", require_judge=True)
	assert result["verified"] is True and result["evidence"]["judge"] == rulings
	api = FakeApi(zip_bytes={5: _zip_with_ledger(_findings_evidence(), LEDGER_TEXT)})
	assert _verify(api, expect_outcome="", require_judge=True)["reason"] == "evidence_judge_missing"


def test_write_records_judge_rulings_and_rejects_malformed_ones():
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		judge_file = tmp / "judge.json"
		judge_file.write_text(json.dumps({"decision": "hold", "rulings": [{"file": "a", "line": 1, "ruling": "upheld"}]}))
		proc = subprocess.run(
			[sys.executable, str(SCRIPT), "write", "--out-dir", str(tmp / "out"), "--pr", "42", "--head", HEAD, "--round", "3",
				"--outcome", "findings", "--ledger-sha256", "a" * 64, "--judge-file", str(judge_file)],
			capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		)
		assert proc.returncode == 0, proc.stderr
		evidence = json.loads((tmp / "out" / "evidence.json").read_text())
		assert evidence["judge"]["decision"] == "hold" and evidence["round"] == 3
		judge_file.write_text(json.dumps({"decision": "hold"}))
		proc = subprocess.run(
			[sys.executable, str(SCRIPT), "write", "--out-dir", str(tmp / "out2"), "--pr", "42", "--head", HEAD, "--round", "3",
				"--outcome", "findings", "--judge-file", str(judge_file)],
			capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		)
		assert proc.returncode == 2 and "rulings list" in proc.stderr
