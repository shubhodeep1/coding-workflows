#!/usr/bin/env python3
"""Parser + reject-verifier tests for typed consolidator rejections."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PARSER_SCRIPT = REPO_ROOT / "scripts" / "review_parse_consolidator.sh"
VERIFIER_SCRIPT = REPO_ROOT / "scripts" / "review_reject_verify.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "review_pipeline"


def _seed_repo(workspace_dir: Path) -> Path:
	workspace_dir.mkdir(parents=True, exist_ok=True)
	runtime_dir = workspace_dir / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src").mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src" / "module.py").write_text(
		"\n".join([
			"def sample(x):",
			"    if x == None:",
			"        return",
			"    return x",
			"line5",
			"line6",
			"line7",
			"line8",
			"line9",
			"line10",
		])
		+ "\n",
		encoding="utf-8",
	)

	subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace_dir, check=True)
	for key, value in (
		("user.email", "test@local"),
		("user.name", "test"),
		("commit.gpgsign", "false"),
	):
		subprocess.run(["git", "config", key, value], cwd=workspace_dir, check=True)
	subprocess.run(["git", "add", "src/module.py"], cwd=workspace_dir, check=True)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
		cwd=workspace_dir,
		check=True,
	)

	shutil.copy2(FIXTURES / "reviewer_bundle.txt", runtime_dir / "reviewer_bundle.txt")
	(runtime_dir / "pr_diff.patch").write_text("", encoding="utf-8")
	(runtime_dir / "linked_issue_context.txt").write_text("", encoding="utf-8")
	return runtime_dir


def _issue_block(
	*,
	issue_id: str = "001",
	classification: str = "non-actionable",
	line_spec: str = "2-3",
	rejection_kind: str | None = None,
	typed_header: str | None = None,
	typed_body: str | None = None,
	notes: str = "Conservatively rejected with evidence.",
) -> str:
	lines = [
		f"=== ISSUE {issue_id} ===",
		"FILE: src/module.py",
		f"LINES: {line_spec}",
		"LENS: CORRECTNESS & LOGIC",
		"SEVERITY: med",
		"FLAGGED_BY: reviewer_alpha",
		f"CLASSIFICATION: {classification}",
	]
	if rejection_kind is not None:
		lines.append(f"REJECTION_KIND: {rejection_kind}")
	if typed_header is not None:
		lines.append(f"{typed_header}:")
		for raw_line in (typed_body or "").splitlines():
			lines.append(f"  {raw_line}")
	lines.extend([
		"EVIDENCE:",
		'  reviewer_alpha> "saw bug"',
		"CURRENT_CODE:",
		"  if x == None:",
		"    return",
		"SUGGESTED_APPROACH:",
		"  Keep the change minimal.",
	])
	if notes is not None:
		lines.extend([
			"NOTES:",
			f"  {notes}",
		])
	lines.extend([
		f"=== END ISSUE {issue_id} ===",
		"",
	])
	return "\n".join(lines)


def _run_parser(
	workspace_dir: Path,
	runtime_dir: Path,
	*,
	raw_text: str,
	schema_enabled: str,
) -> subprocess.CompletedProcess[str]:
	(runtime_dir / "consolidator_raw.txt").write_text(raw_text, encoding="utf-8")
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["REVIEW_PARSER_FAILOPEN"] = "1"
	env["CONSOLIDATOR_REJECT_SCHEMA_ENABLED"] = schema_enabled
	return subprocess.run(
		["bash", str(PARSER_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)


def _run_verifier(
	workspace_dir: Path,
	runtime_dir: Path,
	*,
	schema_enabled: str,
	pr_number: str = "4242",
	autofix_iteration: str = "1",
) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["REVIEW_ISSUES_FILE"] = str(runtime_dir / "review_issues.txt")
	env["PR_DIFF_FILE"] = str(runtime_dir / "pr_diff.patch")
	env["LINKED_ISSUE_CONTEXT_FILE"] = str(runtime_dir / "linked_issue_context.txt")
	env["PR_NUMBER"] = pr_number
	env["AUTOFIX_ITERATION"] = autofix_iteration
	env["CONSOLIDATOR_REJECT_SCHEMA_ENABLED"] = schema_enabled
	return subprocess.run(
		["bash", str(VERIFIER_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)


def _extract_issue_block(text: str, issue_id: str) -> str:
	start = f"=== ISSUE {issue_id} ==="
	end = f"=== END ISSUE {issue_id} ==="
	start_idx = text.index(start)
	end_idx = text.index(end, start_idx)
	return text[start_idx:end_idx + len(end)]


def _artifact_path(workspace_dir: Path, *, pr_number: str = "4242", round_number: str = "1") -> Path:
	return workspace_dir / ".ai" / "review_runtime" / f"pr-{pr_number}" / f"round-{round_number}" / "verified_rejections.json"


def _load_artifact(workspace_dir: Path, *, pr_number: str = "4242", round_number: str = "1") -> dict[str, object]:
	return json.loads(_artifact_path(workspace_dir, pr_number=pr_number, round_number=round_number).read_text(encoding="utf-8"))


def _write_pr_diff(runtime_dir: Path, workspace_dir: Path, new_lines: list[str]) -> None:
	(workspace_dir / "src" / "module.py").write_text("\n".join(new_lines) + "\n", encoding="utf-8")
	diff = subprocess.run(
		["git", "diff", "--", "src/module.py"],
		cwd=workspace_dir,
		check=True,
		capture_output=True,
		text=True,
	).stdout
	(runtime_dir / "pr_diff.patch").write_text(diff, encoding="utf-8")


def _write_linked_issue_context(runtime_dir: Path, files_touched: list[str] | None) -> None:
	if not files_touched:
		(runtime_dir / "linked_issue_context.txt").write_text("No files_touched block.\n", encoding="utf-8")
		return
	body = ["Implementation scope", "", "- files_touched:"]
	body.extend(f"  - {path}" for path in files_touched)
	body.append("")
	(runtime_dir / "linked_issue_context.txt").write_text("\n".join(body), encoding="utf-8")


def _write_prior_artifact(workspace_dir: Path, *, results: list[dict[str, object]], round_number: str = "1", pr_number: str = "4242") -> None:
	artifact_path = _artifact_path(workspace_dir, pr_number=pr_number, round_number=round_number)
	artifact_path.parent.mkdir(parents=True, exist_ok=True)
	artifact_path.write_text(
		json.dumps(
			{
				"pr_number": pr_number,
				"round": int(round_number),
				"schema_enabled": True,
				"results": results,
			},
			indent=2,
		)
		+ "\n",
		encoding="utf-8",
	)


def test_parser_schema_off_preserves_legacy_non_actionable_notes_only() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		result = _run_parser(
			workspace,
			runtime,
			raw_text=_issue_block(notes="Legacy rejection rationale."),
			schema_enabled="false",
		)
		assert result.returncode == 0, result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: non-actionable" in block
		assert "REJECTION_KIND:" not in block
		assert "CONSOLIDATOR_REJECT_EVIDENCE_MALFORMED" not in result.stderr


def test_parser_schema_on_missing_rejection_kind_demotes_to_unclassified() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		result = _run_parser(
			workspace,
			runtime,
			raw_text=_issue_block(notes="Typed schema is required in this mode."),
			schema_enabled="true",
		)
		assert result.returncode == 0, result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: unclassified" in block
		assert "CONSOLIDATOR_REJECT_EVIDENCE_MALFORMED issue=001 kind=missing reason=missing_rejection_kind" in result.stderr


def test_parser_schema_on_malformed_typed_evidence_demotes_to_unclassified() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		result = _run_parser(
			workspace,
			runtime,
			raw_text=_issue_block(
				rejection_kind="already-fixed",
				typed_header="EVIDENCE_DIFF_HUNK",
				typed_body="file: src/module.py",
			),
			schema_enabled="true",
		)
		assert result.returncode == 0, result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: unclassified" in block
		assert "REJECTION_KIND: already-fixed" in block
		assert "EVIDENCE_DIFF_HUNK:" in block
		assert "field=EVIDENCE_DIFF_HUNK reason=invalid_diff_hunk_lines" in result.stderr


def test_verifier_leaves_llm_only_rejections_inconclusive() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		raw_text = "".join([
			_issue_block(
				issue_id="001",
				rejection_kind="reviewer-wrong",
				typed_header="EVIDENCE_RUNTIME_PATH",
				typed_body="location: process_request:187\nrationale: Guard returns before the reviewer-described call path.",
			),
			_issue_block(
				issue_id="002",
				line_spec="4",
				rejection_kind="spec-doesnt-support",
				typed_header="EVIDENCE_SPEC_QUOTE",
				typed_body='source: docs/spec.md#validation\nquote: "The endpoint accepts duplicate dry-run keys."',
			),
		])
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text, schema_enabled="true")
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, schema_enabled="true")
		assert verify_result.returncode == 0, verify_result.stderr
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		assert issues.count("CLASSIFICATION: non-actionable") >= 2
		assert "REVERSAL_REASON:" not in issues
		artifact = _load_artifact(workspace)
		assert [row["verdict"] for row in artifact["results"]] == ["inconclusive", "inconclusive"]
		assert "Phase C PR-2 LLM verifier" in verify_result.stdout


def test_already_fixed_rejection_supports_when_pr_diff_matches() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		_write_pr_diff(
			runtime,
			workspace,
			[
				"def sample(x):",
				"    if x is None:",
				"        return None",
				"    return x",
				"line5",
				"line6",
				"line7",
				"line8",
				"line9",
				"line10",
			],
		)
		parse_result = _run_parser(
			workspace,
			runtime,
			raw_text=_issue_block(
				rejection_kind="already-fixed",
				typed_header="EVIDENCE_DIFF_HUNK",
				typed_body="file: src/module.py\nlines: 2-3\nexcerpt: if x is None:",
			),
			schema_enabled="true",
		)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, schema_enabled="true")
		assert verify_result.returncode == 0, verify_result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: non-actionable" in block
		assert "REVERSAL_REASON:" not in block
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "support"
		assert "CONSOLIDATOR_REJECT_VERIFIED issue=001 kind=already-fixed verdict=support" in verify_result.stdout


def test_already_fixed_rejection_reverses_when_pr_diff_missing_cited_hunk() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		_write_pr_diff(
			runtime,
			workspace,
			[
				"def sample(x):",
				"    if x == None:",
				"        return",
				"    return x",
				"line5",
				"line6",
				"line7",
				"line8",
				"line9 changed",
				"line10",
			],
		)
		parse_result = _run_parser(
			workspace,
			runtime,
			raw_text=_issue_block(
				rejection_kind="already-fixed",
				typed_header="EVIDENCE_DIFF_HUNK",
				typed_body="file: src/module.py\nlines: 2-3\nexcerpt: if x is None:",
			),
			schema_enabled="true",
		)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, schema_enabled="true")
		assert verify_result.returncode == 0, verify_result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: must-fix" in block
		assert "REVERSAL_REASON: PR diff does not contain a hunk covering src/module.py:2-3." in block
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "does-not-support"
		assert "CONSOLIDATOR_REJECT_REVERSED issue=001 kind=already-fixed" in verify_result.stdout


def test_out_of_scope_rejection_supports_when_file_absent_from_linked_issue_scope() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		_write_linked_issue_context(runtime, ["src/module.py", "tests/test_review_reject_verify.py"])
		parse_result = _run_parser(
			workspace,
			runtime,
			raw_text=_issue_block(
				rejection_kind="out-of-scope",
				typed_header="EVIDENCE_FILES_TOUCHED",
				typed_body="cited_path: docs/other.md\nfiles_touched: src/module.py, tests/test_review_reject_verify.py",
			),
			schema_enabled="true",
		)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, schema_enabled="true")
		assert verify_result.returncode == 0, verify_result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: non-actionable" in block
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "support"
		assert "CONSOLIDATOR_REJECT_VERIFIED issue=001 kind=out-of-scope verdict=support" in verify_result.stdout


def test_out_of_scope_rejection_reverses_when_file_is_in_scope() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		_write_linked_issue_context(runtime, ["src/module.py", "tests/test_review_reject_verify.py"])
		parse_result = _run_parser(
			workspace,
			runtime,
			raw_text=_issue_block(
				rejection_kind="out-of-scope",
				typed_header="EVIDENCE_FILES_TOUCHED",
				typed_body="cited_path: src/module.py\nfiles_touched: src/module.py, tests/test_review_reject_verify.py",
			),
			schema_enabled="true",
		)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, schema_enabled="true")
		assert verify_result.returncode == 0, verify_result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: must-fix" in block
		assert "Linked issue files_touched explicitly includes src/module.py." in block
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "does-not-support"


def test_prior_round_rejection_supports_when_cached_artifact_matches() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		_write_prior_artifact(
			workspace,
			results=[
				{
					"issue_id": "001",
					"file": "src/module.py",
					"lines": "2-3",
					"rejection_kind": "already-fixed",
					"verdict": "support",
					"reason": "prior support",
				}
			],
		)
		parse_result = _run_parser(
			workspace,
			runtime,
			raw_text=_issue_block(
				rejection_kind="already-rejected-with-evidence",
				typed_header="EVIDENCE_PRIOR_ROUND",
				typed_body="round: 1\nissue_id: 001\nrejection_kind: already-fixed\nsticky: true",
			),
			schema_enabled="true",
		)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, schema_enabled="true")
		assert verify_result.returncode == 0, verify_result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: non-actionable" in block
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "support"
		assert "CONSOLIDATOR_REJECT_VERIFIED issue=001 kind=already-rejected-with-evidence verdict=support" in verify_result.stdout


def test_prior_round_rejection_reverses_when_cached_artifact_disagrees() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		_write_prior_artifact(
			workspace,
			results=[
				{
					"issue_id": "001",
					"file": "src/module.py",
					"lines": "9",
					"rejection_kind": "already-fixed",
					"verdict": "support",
					"reason": "prior support on a different anchor",
				}
			],
		)
		parse_result = _run_parser(
			workspace,
			runtime,
			raw_text=_issue_block(
				rejection_kind="already-rejected-with-evidence",
				typed_header="EVIDENCE_PRIOR_ROUND",
				typed_body="round: 1\nissue_id: 001\nrejection_kind: already-fixed\nsticky: true",
			),
			schema_enabled="true",
		)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, schema_enabled="true")
		assert verify_result.returncode == 0, verify_result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: must-fix" in block
		assert "Prior-round verifier artifact points at a different line range." in block
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "does-not-support"


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
