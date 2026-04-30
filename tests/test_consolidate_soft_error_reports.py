#!/usr/bin/env python3
"""Tests for scripts/consolidate_soft_error_reports.py.

Covers the four behaviours operators rely on for the release-gate
notification path:

1. Discovery across both `actions/download-artifact@v6` layouts
   (subdir-per-artifact and flattened/merge-multiple).
2. Canonical pipeline ordering with alphabetical fallback for
   unknown phases.
3. Placeholder filtering (`None observed`, `no findings`, etc.)
   excluding empty findings sections from the Telegram summary.
4. Section-aware truncation that respects `max_bytes` exactly and
   never splits a multi-byte UTF-8 codepoint.

Plus the download-status-driven stub variants (`success` →
neutral stub, `failure`/`cancelled` → DOWNLOAD FAILED stub,
everything else → neutral so a miswired pipeline can't fabricate
fake outage signal).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import consolidate_soft_error_reports as csr


def _write_per_phase_report(
	root: Path,
	*,
	run_id: str,
	phase: str,
	findings: str | None,
	status: str = "ok",
	flatten: bool = False,
) -> Path:
	"""Materialise a per-phase soft-error report under `root` in the
	chosen `actions/download-artifact@v6` layout. Returns the markdown
	file path that was written (callers point the consolidator at
	`root` itself, not at the returned path).
	"""
	artifact_name = f"soft-error-report-{run_id}-{phase}"
	if flatten:
		md_path = root / f"{artifact_name}.md"
	else:
		subdir = root / artifact_name
		subdir.mkdir(parents=True, exist_ok=True)
		md_path = subdir / f"{artifact_name}.md"
	body = (
		f"## Soft-error analyzer (status: `{status}`, model: `m`, "
		"reasoning: `r`)\n\n"
	)
	if findings is not None:
		body += "## Soft errors (per phase)\n" + findings + "\n"
	md_path.write_text(body, encoding="utf-8")
	return md_path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discover_subdir_layout():
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		_write_per_phase_report(root, run_id="9", phase="clarify", findings="x", flatten=False)
		_write_per_phase_report(root, run_id="9", phase="implement", findings="y", flatten=False)
		found = csr.discover_reports(root)
		phases = sorted(p for p, _ in found)
		assert phases == ["clarify", "implement"], phases


def test_discover_flat_layout():
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		_write_per_phase_report(root, run_id="9", phase="clarify", findings="x", flatten=True)
		_write_per_phase_report(root, run_id="9", phase="plan", findings="y", flatten=True)
		found = csr.discover_reports(root)
		phases = sorted(p for p, _ in found)
		assert phases == ["clarify", "plan"], phases


def test_discover_mixed_layout_no_duplicates():
	"""When the same phase appears in both layouts, dedup by phase name."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		_write_per_phase_report(root, run_id="9", phase="clarify", findings="x", flatten=False)
		_write_per_phase_report(root, run_id="9", phase="clarify", findings="y", flatten=True)
		_write_per_phase_report(root, run_id="9", phase="plan", findings="z", flatten=True)
		found = csr.discover_reports(root)
		phases = sorted(p for p, _ in found)
		assert phases == ["clarify", "plan"], phases


def test_discover_empty_dir_returns_empty():
	with tempfile.TemporaryDirectory() as tmp:
		assert csr.discover_reports(Path(tmp)) == []


def test_discover_missing_dir_returns_empty():
	assert csr.discover_reports(Path("/tmp/this-path-does-not-exist-xyz")) == []


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_canonical_pipeline_order():
	"""Canonical phases come back in pipeline order regardless of input order."""
	pairs = [
		("review_autofix", Path("/r")),
		("clarify", Path("/c")),
		("implement", Path("/i")),
		("plan", Path("/p")),
	]
	ordered = csr.order_reports(pairs)
	assert [p for p, _ in ordered] == ["clarify", "plan", "implement", "review_autofix"]


def test_unknown_phases_alphabetical_fallback():
	"""Phases not in CANONICAL_PHASE_ORDER sort alphabetically AFTER known ones."""
	pairs = [
		("zeta_phase", Path("/z")),
		("plan", Path("/p")),
		("alpha_phase", Path("/a")),
	]
	ordered = csr.order_reports(pairs)
	assert [p for p, _ in ordered] == ["plan", "alpha_phase", "zeta_phase"]


# ---------------------------------------------------------------------------
# Placeholder filtering
# ---------------------------------------------------------------------------

def test_findings_section_extracted():
	md = (
		"## Soft-error analyzer (status: `ok`, model: `m`, reasoning: `r`)\n\n"
		"## Soft errors (per phase)\n"
		"- Rate-limit retry on attempt 2.\n"
		"\n## Patterns to watch\n- Repeated 429s.\n"
	)
	body = csr.extract_findings_section(md)
	assert "Rate-limit retry" in body
	assert "Patterns to watch" not in body


def test_placeholder_none_observed_filtered():
	md = (
		"## Soft-error analyzer (status: `ok`, model: `m`, reasoning: `r`)\n\n"
		"## Soft errors (per phase)\nNone observed.\n"
	)
	assert csr.extract_findings_section(md) == ""


def test_placeholder_no_findings_filtered():
	md = (
		"## Soft-error analyzer (status: `ok`, model: `m`, reasoning: `r`)\n\n"
		"## Soft errors (per phase)\nNo findings.\n"
	)
	assert csr.extract_findings_section(md) == ""


def test_placeholder_no_issues_filtered():
	md = (
		"## Soft-error analyzer (status: `ok`, model: `m`, reasoning: `r`)\n\n"
		"## Soft errors (per phase)\nNo issues.\n"
	)
	assert csr.extract_findings_section(md) == ""


def test_missing_findings_section_returns_empty():
	md = "## Soft-error analyzer (status: `ok`, model: `m`, reasoning: `r`)\n\nbody.\n"
	assert csr.extract_findings_section(md) == ""


# ---------------------------------------------------------------------------
# Status header parsing
# ---------------------------------------------------------------------------

def test_parse_status_ok():
	md = "## Soft-error analyzer (status: `ok`, model: `m`, reasoning: `r`)\n"
	assert csr.parse_status(md) == "ok"


def test_parse_status_call_failed():
	md = "## Soft-error analyzer (status: `call_failed`, model: `m`, reasoning: `r`)\n"
	assert csr.parse_status(md) == "call_failed"


def test_parse_status_unknown_when_header_absent():
	assert csr.parse_status("garbage") == "unknown"


# ---------------------------------------------------------------------------
# Section-aware truncation
# ---------------------------------------------------------------------------

def _run_consolidator(input_dir: Path, *, max_bytes: int, download_status: str = "success") -> tuple[str, str]:
	"""Invoke the consolidator end-to-end and return (full_text, summary_text)."""
	with tempfile.TemporaryDirectory() as outdir:
		full = Path(outdir) / "full.md"
		summary = Path(outdir) / "summary.md"
		subprocess.run(
			[
				sys.executable,
				str(REPO_ROOT / "scripts" / "consolidate_soft_error_reports.py"),
				"--input-dir", str(input_dir),
				"--output-full", str(full),
				"--output-summary", str(summary),
				"--max-summary-bytes", str(max_bytes),
				"--download-status", download_status,
			],
			check=True,
			capture_output=True,
		)
		return full.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8")


def test_summary_respects_byte_cap_with_unicode():
	"""Multi-byte unicode payload truncated within an exact byte cap, decoding cleanly."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		# Greek letters are 2-byte UTF-8 each; ensure the budget straddles
		# a codepoint boundary so the boundary-walk logic is exercised.
		findings = "### clarify\n" + ("αβγδε" * 80)
		_write_per_phase_report(root, run_id="9", phase="clarify", findings=findings)
		for cap in (200, 300, 500):
			_, summary = _run_consolidator(root, max_bytes=cap)
			assert len(summary.encode("utf-8")) <= cap, (
				f"cap={cap} but summary is {len(summary.encode('utf-8'))} bytes"
			)
			# Round-trip decode must succeed (no replacement chars introduced).
			summary.encode("utf-8").decode("utf-8")


def test_summary_byte_cap_is_absolute_even_when_head_overflows():
	"""Degenerate: cap so small the status pill alone exceeds it.

	The contract `len(summary.encode('utf-8')) <= max_bytes` must hold
	absolutely — Telegram will reject the message otherwise. Earlier
	iterations of the truncation logic returned `head + marker` even
	when that combination already exceeded `max_bytes`.
	"""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		# Spread findings across many canonical phases so the status
		# pill itself becomes long.
		long_phase_set = ("clarify", "plan", "implement", "review_autofix", "orchestrate_poll", "cancel_on_pr_close")
		for phase in long_phase_set:
			_write_per_phase_report(root, run_id="9", phase=phase, findings=f"### {phase}\nminor.")
		# Cap below the pill+marker combined size to exercise the
		# last-resort truncation guard.
		for cap in (50, 80, 120):
			_, summary = _run_consolidator(root, max_bytes=cap)
			assert len(summary.encode("utf-8")) <= cap, (
				f"cap={cap} but summary is {len(summary.encode('utf-8'))} bytes"
			)
			# Round-trip must still be valid UTF-8.
			summary.encode("utf-8").decode("utf-8")


def test_summary_drops_whole_blocks_before_inline_truncation():
	"""When multiple findings exist, drop later blocks (canonical-order tail) first."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		# Two phases with sizable findings; canonical order puts clarify
		# before plan, so plan should be dropped first when budget tight.
		_write_per_phase_report(root, run_id="9", phase="clarify", findings="### clarify\n" + ("X" * 200))
		_write_per_phase_report(root, run_id="9", phase="plan", findings="### plan\n" + ("Y" * 200))
		_, summary = _run_consolidator(root, max_bytes=400)
		# Tight cap: clarify's content should appear, plan's should be dropped
		# before we resort to inline-slicing clarify.
		assert "X" * 50 in summary
		assert summary.count("Y") < 50, "plan block should have been dropped"


def test_summary_emits_no_findings_stub_when_all_placeholders():
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		_write_per_phase_report(root, run_id="9", phase="clarify", findings="None observed.")
		_write_per_phase_report(root, run_id="9", phase="plan", findings="No issues.")
		_, summary = _run_consolidator(root, max_bytes=2800)
		assert "No soft-error findings reported by any phase." in summary
		# Status pill must still surface every phase.
		assert "clarify=ok" in summary
		assert "plan=ok" in summary


# ---------------------------------------------------------------------------
# Download-status-driven stubs
# ---------------------------------------------------------------------------

def test_download_status_success_with_empty_dir_emits_neutral_stub():
	with tempfile.TemporaryDirectory() as tmp:
		full, summary = _run_consolidator(Path(tmp), max_bytes=2800, download_status="success")
		assert "DOWNLOAD FAILED" not in summary
		assert "DOWNLOAD FAILED" not in full
		assert "No per-phase soft-error artifacts were downloaded." in summary


def test_download_status_failure_emits_alarm_stub():
	with tempfile.TemporaryDirectory() as tmp:
		full, summary = _run_consolidator(Path(tmp), max_bytes=2800, download_status="failure")
		assert "DOWNLOAD FAILED" in summary
		assert "DOWNLOAD FAILED" in full


def test_download_status_cancelled_emits_alarm_stub():
	with tempfile.TemporaryDirectory() as tmp:
		_, summary = _run_consolidator(Path(tmp), max_bytes=2800, download_status="cancelled")
		assert "DOWNLOAD FAILED" in summary


def test_download_status_unknown_falls_through_to_neutral():
	"""A miswired env var must NOT fabricate fake outage signal."""
	with tempfile.TemporaryDirectory() as tmp:
		_, summary = _run_consolidator(Path(tmp), max_bytes=2800, download_status="unknown")
		assert "DOWNLOAD FAILED" not in summary


def test_download_status_skipped_falls_through_to_neutral():
	with tempfile.TemporaryDirectory() as tmp:
		_, summary = _run_consolidator(Path(tmp), max_bytes=2800, download_status="skipped")
		assert "DOWNLOAD FAILED" not in summary


# ---------------------------------------------------------------------------
# UTF-8 robustness
# ---------------------------------------------------------------------------

def test_corrupted_artifact_does_not_crash_consolidator():
	"""A non-UTF-8 byte sequence in one artifact must not abort consolidation."""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		_write_per_phase_report(root, run_id="9", phase="clarify", findings="### clarify\nfine.")
		# Corrupted artifact for plan: write raw non-UTF8 bytes alongside a
		# valid findings section so we can check the fallback.
		subdir = root / "soft-error-report-9-plan"
		subdir.mkdir(parents=True, exist_ok=True)
		(subdir / "soft-error-report-9-plan.md").write_bytes(
			b"## Soft-error analyzer (status: `ok`, model: `m`, reasoning: `r`)\n"
			b"\n\xff\xfe\xfd corrupted bytes\n"
			b"## Soft errors (per phase)\nfinding here\n"
		)
		full, summary = _run_consolidator(root, max_bytes=2800)
		# Both phases must have status entries; neither should crash.
		assert "clarify=" in summary
		assert "plan=" in summary
		# The full report contains the corrupted phase verbatim
		# (with U+FFFD replacements where invalid bytes lived).
		assert "Phase: `clarify`" in full
		assert "Phase: `plan`" in full


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

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
