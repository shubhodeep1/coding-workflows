#!/usr/bin/env python3
"""Tests for sticky repeat-finding annotation in review_autofix."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_annotate_sticky.sh"


def _run_annotator(
	workspace: Path,
	*,
	prior_text: str | None,
	bundle_text: str,
	autofix_iteration: int = 2,
	line_bucket: str | None = "5",
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
	runtime_dir = workspace / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)
	(runtime_dir / "reviewer_bundle.txt").write_text(bundle_text, encoding="utf-8")

	if prior_text is not None:
		prior_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / f"round-{autofix_iteration - 1}"
		prior_dir.mkdir(parents=True, exist_ok=True)
		(prior_dir / "consolidator_parsed.txt").write_text(prior_text, encoding="utf-8")

	sticky_json = workspace / ".ai" / "review_runtime" / "pr-4242" / f"round-{autofix_iteration}" / "sticky_findings.json"
	priors_file = runtime_dir / "sticky_findings_priors.txt"
	env = os.environ.copy()
	env.update(
		{
			"PYTHONDONTWRITEBYTECODE": "1",
			"RUNTIME_DIR": str(runtime_dir),
			"PR_NUMBER": "4242",
			"AUTOFIX_ITERATION": str(autofix_iteration),
			"STICKY_FINDINGS_ENABLED": "true",
		}
	)
	if line_bucket is None:
		env.pop("STICKY_LINE_BUCKET", None)
	else:
		env["STICKY_LINE_BUCKET"] = line_bucket
	result = subprocess.run(
		["bash", str(SCRIPT)],
		cwd=workspace,
		env=env,
		capture_output=True,
		text=True,
	)
	return result, runtime_dir, sticky_json, priors_file


def test_script_exists_and_is_executable() -> None:
	assert SCRIPT.exists(), f"missing script: {SCRIPT}"
	assert SCRIPT.stat().st_mode & 0o111, "script must be executable"


def test_round_two_repeat_hit_emits_sticky_artifacts_and_prior_notes_block() -> None:
	prior_text = """=== ISSUE 001 ===
FILE: src/module.py
LINES: 10-12
LENS: CORRECTNESS & LOGIC
SEVERITY: high
FLAGGED_BY: reviewer_alpha
CLASSIFICATION: non-actionable
REJECTION_KIND: already-fixed
EVIDENCE:
  reviewer_alpha> Missing nil guard around sticky cache refresh.
CURRENT_CODE:
  value = cache[key]
SUGGESTED_APPROACH:
  Add the missing guard.
NOTES:
  Prior round note explains the repeated unresolved guard.
=== END ISSUE 001 ===
"""
	bundle_text = """FILE_PATH: /tmp/review_alpha.txt
CONTENT_START
File: src/module.py
Line or code reference: line 15
Problem: Missing nil guard around sticky cache refresh.
Why it fails at runtime: First retry raises KeyError.
ISSUE_CONFIDENCE: 4
CONTENT_END

FILE_PATH: /tmp/review_beta.txt
CONTENT_START
File: src/module.py
Code: src/module.py:14
Problem: Missing nil guard around sticky cache refresh.
Why it fails at runtime: Recovery path crashes on empty cache.
ISSUE_CONFIDENCE: 5
CONTENT_END
"""

	with tempfile.TemporaryDirectory(prefix="sticky_repeat_hit_") as td:
		workspace = Path(td)
		result, _, sticky_json, priors_file = _run_annotator(
			workspace,
			prior_text=prior_text,
			bundle_text=bundle_text,
		)

		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert sticky_json.exists(), combined_output
		assert priors_file.exists(), combined_output
		assert "STICKY_FINDING_DETECTED issue=001 file=src/module.py" in combined_output
		assert "STICKY_FINDING_DETECTED count=1" in combined_output

		payload = json.loads(sticky_json.read_text(encoding="utf-8"))
		assert payload["current_round"] == 2
		assert payload["prior_round"] == 1
		assert payload["line_bucket"] == 5
		assert len(payload["matches"]) == 1
		match = payload["matches"][0]
		assert match["file"] == "src/module.py"
		assert match["prior_issue_id"] == "001"
		assert match["prior_classification"] == "non-actionable"
		assert match["prior_rejection_kind"] == "already-fixed"
		assert match["prior_notes"] == "Prior round note explains the repeated unresolved guard."
		assert match["current_reviewers"] == ["review_alpha", "review_beta"]
		assert match["current_lines"] == [14, 15]

		priors_text = priors_file.read_text(encoding="utf-8")
		assert "<sticky_findings_priors>" in priors_text
		assert "prior_issue_id: 001" in priors_text
		assert "prior_rejection_kind: already-fixed" in priors_text
		assert "prior_notes: Prior round note explains the repeated unresolved guard." in priors_text


def test_line_bucket_matches_plus_five_and_skips_plus_six() -> None:
	prior_text = """=== ISSUE 007 ===
FILE: src/module.py
LINES: 20-22
LENS: CORRECTNESS & LOGIC
SEVERITY: med
FLAGGED_BY: reviewer_alpha
CLASSIFICATION: unclassified
EVIDENCE:
  reviewer_alpha> Retry token is dropped before the fallback branch.
CURRENT_CODE:
  token = None
SUGGESTED_APPROACH:
  Preserve the token.
NOTES:
  Prior round note for the retry token regression.
=== END ISSUE 007 ===
"""
	bundle_text = """FILE_PATH: /tmp/review_alpha.txt
CONTENT_START
File: src/module.py
Line or code reference: line 27
Problem: Retry token is dropped before the fallback branch.
Why it fails at runtime: The retry cannot resume.
ISSUE_CONFIDENCE: 4
File: src/module.py
Line or code reference: line 28
Problem: Retry token is dropped before the fallback branch.
Why it fails at runtime: The retry cannot resume.
ISSUE_CONFIDENCE: 4
CONTENT_END
"""

	with tempfile.TemporaryDirectory(prefix="sticky_boundary_") as td:
		workspace = Path(td)
		result, _, sticky_json, priors_file = _run_annotator(
			workspace,
			prior_text=prior_text,
			bundle_text=bundle_text,
		)

		assert result.returncode == 0, result.stdout + result.stderr
		assert sticky_json.exists()
		assert priors_file.exists()
		payload = json.loads(sticky_json.read_text(encoding="utf-8"))
		assert len(payload["matches"]) == 1
		assert payload["matches"][0]["current_lines"] == [27]


def test_missing_prior_artifact_is_noop_and_leaves_no_sticky_outputs() -> None:
	bundle_text = """FILE_PATH: /tmp/review_alpha.txt
CONTENT_START
File: src/module.py
Line or code reference: line 15
Problem: Missing nil guard around sticky cache refresh.
Why it fails at runtime: First retry raises KeyError.
ISSUE_CONFIDENCE: 4
CONTENT_END
"""

	with tempfile.TemporaryDirectory(prefix="sticky_no_prior_") as td:
		workspace = Path(td)
		result, _, sticky_json, priors_file = _run_annotator(
			workspace,
			prior_text=None,
			bundle_text=bundle_text,
		)

		combined_output = result.stdout + result.stderr
		assert result.returncode == 0, combined_output
		assert "STICKY_ANNOTATOR_NOOP reason=prior_artifact_missing" in combined_output
		assert not sticky_json.exists()
		assert not priors_file.exists()


def test_unset_line_bucket_defaults_to_five() -> None:
	prior_text = """=== ISSUE 009 ===
FILE: src/module.py
LINES: 20-22
LENS: CORRECTNESS & LOGIC
SEVERITY: med
FLAGGED_BY: reviewer_alpha
CLASSIFICATION: unclassified
EVIDENCE:
  reviewer_alpha> Retry token is dropped before the fallback branch.
CURRENT_CODE:
  token = None
SUGGESTED_APPROACH:
  Preserve the token.
NOTES:
  Prior round note for the retry token regression.
=== END ISSUE 009 ===
"""
	bundle_text = """FILE_PATH: /tmp/review_alpha.txt
CONTENT_START
File: src/module.py
Line or code reference: line 27
Problem: Retry token is dropped before the fallback branch.
Why it fails at runtime: The retry cannot resume.
ISSUE_CONFIDENCE: 4
CONTENT_END
"""

	with tempfile.TemporaryDirectory(prefix="sticky_default_bucket_") as td:
		workspace = Path(td)
		result, _, sticky_json, _ = _run_annotator(
			workspace,
			prior_text=prior_text,
			bundle_text=bundle_text,
			line_bucket=None,
		)

		assert result.returncode == 0, result.stdout + result.stderr
		assert sticky_json.exists(), result.stdout + result.stderr
		payload = json.loads(sticky_json.read_text(encoding="utf-8"))
		assert payload["line_bucket"] == 5
		assert payload["matches"][0]["current_lines"] == [27]


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
			else:
				raise RuntimeError(f"unsupported test signature: {name}{inspect.signature(func)}")
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
