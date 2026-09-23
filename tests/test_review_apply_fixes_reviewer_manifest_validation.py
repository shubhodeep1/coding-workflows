#!/usr/bin/env python3
"""Runtime tests for the editor's reviewer-manifest validation in
scripts/review_apply_fixes.sh.

The editor summary must list every reviewer file under "Reviewer files
processed:" and "Review file issue audit:". The model also copies each
file's sha256 into the first section. Release gate run 35802596362 lost
a correct canary fix because the model garbled one hash per attempt
(`9e4636e1...` for `9e4638e1...`, a truncated grok hash), so a wrong or
missing checksum now warns instead of failing the attempt. A missing
entry, or a missing audit bullet, still fails it.

These tests extract the validation block verbatim from the script and
run it under bash, so they exercise the shipped code rather than a copy.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
BLOCK_START = "      reviewer_validation_ok=true\n      changes_lost_detected=false\n"
BLOCK_END = '      done < "${REVIEWER_MANIFEST_FILE}"\n'
WARNING_KEY = "EDITOR_REVIEWER_CHECKSUM_UNVERIFIED"


def _validation_block() -> str:
	text = SCRIPT.read_text(encoding="utf-8")
	start = text.index(BLOCK_START)
	end = text.index(BLOCK_END, start) + len(BLOCK_END)
	return text[start:end]


def _audit_line(path: Path) -> str:
	return (
		f"- `{path}` | total issues listed 1 | issues applied 1 | "
		"issues already applied 0 | issues ignored 0"
	)


def _run(tmp: Path, reviewer_files: list[Path], processed_lines: list[str], audit_lines: list[str]) -> tuple[str, str]:
	manifest = tmp / "reviewer_manifest.txt"
	manifest.write_text("".join(f"{p}\n" for p in reviewer_files), encoding="utf-8")
	output = tmp / "editor_output.txt"
	output.write_text(
		"Changes made:\n- tests/e2e_smoke_canary.txt restored\n\n"
		"Reviewer files processed:\n" + "\n".join(processed_lines) + "\n\n"
		"Review file issue audit:\n" + "\n".join(audit_lines) + "\n\n"
		"PR comment audit:\n- none\n",
		encoding="utf-8",
	)
	harness = tmp / "harness.sh"
	harness.write_text(
		"set -euo pipefail\n"
		f'tmp_output="{output}"\n'
		f'REVIEWER_MANIFEST_FILE="{manifest}"\n'
		"attempt=2\n"
		+ _validation_block()
		+ 'echo "RESULT reviewer_validation_ok=${reviewer_validation_ok}"\n',
		encoding="utf-8",
	)
	proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True, check=True)
	return proc.stdout, proc.stderr


def _reviewers(tmp: Path) -> list[Path]:
	files = []
	for name, body in (
		("review_deepseek_deepseek-v4-pro.txt", "tests/e2e_smoke_canary.txt:1-6 severity=critical\n"),
		("review_minimax_minimax-m3.txt", "I need to verify the actual repository state before writing the review.\n"),
	):
		path = tmp / "previous_reviews" / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(body, encoding="utf-8")
		files.append(path)
	return files


def _sha(path: Path) -> str:
	return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_checksums_pass_without_warning() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		files = _reviewers(tmp)
		processed = [f"- `{p}` — checksum `{_sha(p)}` — used" for p in files]
		stdout, _ = _run(tmp, files, processed, [_audit_line(p) for p in files])
		assert "RESULT reviewer_validation_ok=true" in stdout, stdout
		assert WARNING_KEY not in stdout, stdout


def test_garbled_checksum_warns_but_passes() -> None:
	"""Mirrors attempt 2 of review run 35803994060: one hex digit off."""
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		files = _reviewers(tmp)
		real = _sha(files[1])
		garbled = real[:5] + ("0" if real[5] != "0" else "1") + real[6:]
		processed = [
			f"- `{files[0]}` — checksum `{_sha(files[0])}` — used",
			f"- `{files[1]}` — checksum `{garbled}` — ignored; no finding",
		]
		stdout, _ = _run(tmp, files, processed, [_audit_line(p) for p in files])
		assert "RESULT reviewer_validation_ok=true" in stdout, stdout
		assert f"::warning::{WARNING_KEY} attempt=2 file={files[1]}" in stdout, stdout
		assert f"expected_sha={real}" in stdout
		assert "path_entries=1 checksum_matches=0" in stdout


def test_truncated_checksum_warns_but_passes() -> None:
	"""Mirrors attempt 3 of review run 35803994060: hash shortened."""
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		files = _reviewers(tmp)
		processed = [
			f"- `{files[0]}` — checksum `{_sha(files[0])[:40]}` — used",
			f"- `{files[1]}` — checksum `{_sha(files[1])}` — ignored",
		]
		stdout, _ = _run(tmp, files, processed, [_audit_line(p) for p in files])
		assert "RESULT reviewer_validation_ok=true" in stdout, stdout
		assert f"{WARNING_KEY} attempt=2 file={files[0]}" in stdout, stdout


def test_missing_processed_entry_still_fails() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		files = _reviewers(tmp)
		processed = [f"- `{files[0]}` — checksum `{_sha(files[0])}` — used"]
		stdout, _ = _run(tmp, files, processed, [_audit_line(p) for p in files])
		assert "RESULT reviewer_validation_ok=false" in stdout, stdout
		assert f"Reviewer validation failed for {files[1]}: no entry under Reviewer files processed names this file." in stdout


def test_missing_audit_entry_still_fails() -> None:
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		files = _reviewers(tmp)
		processed = [f"- `{p}` — checksum `{_sha(p)}` — used" for p in files]
		stdout, _ = _run(tmp, files, processed, [_audit_line(files[0])])
		assert "RESULT reviewer_validation_ok=false" in stdout, stdout
		assert f"Review file issue audit validation failed for {files[1]}" in stdout


def test_basename_only_entry_is_accepted() -> None:
	"""The path match already falls back to the basename; keep that."""
	with tempfile.TemporaryDirectory() as raw:
		tmp = Path(raw)
		files = _reviewers(tmp)
		processed = [f"- {p.name} — checksum {_sha(p)} — used" for p in files]
		audit = [
			f"- {p.name} | total issues listed 0 | issues applied 0 | issues already applied 0 | issues ignored 0"
			for p in files
		]
		stdout, _ = _run(tmp, files, processed, audit)
		assert "RESULT reviewer_validation_ok=true" in stdout, stdout
		assert WARNING_KEY not in stdout, stdout


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
