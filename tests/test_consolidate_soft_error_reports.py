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


def test_discover_oserror_on_top_level_iterdir_returns_empty():
	"""If the top-level `iterdir()` raises OSError, layout-1 discovery
	is skipped. Layout-2 then runs `glob("*.md")` independently — its
	own try/except handles glob failures. Combined with no flat-layout
	files, this returns [].

	Regression: an earlier version of this test used a `Path.iterdir`
	monkeypatch and asserted that layout-2 still recovered a flat file.
	That assertion happened to pass because CPython's `Path.glob` does
	NOT call `Path.iterdir()` internally — the patch only broke
	layout-1. To avoid testing CPython internals, this test now
	exercises the simpler invariant: when iterdir fails AND no
	flat-layout files exist, discovery returns empty (not crash).
	The per-child OSError path is covered by
	`test_discover_oserror_on_child_glob_skips_subdir` below.
	"""
	import unittest.mock as mock
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		original_iterdir = Path.iterdir

		def _broken_iterdir(self):
			if self == root:
				raise OSError("simulated transient FS error")
			return original_iterdir(self)

		with mock.patch.object(Path, "iterdir", _broken_iterdir):
			found = csr.discover_reports(root)
		assert found == []


def test_discover_oserror_on_child_glob_skips_subdir():
	"""Per-child OSError on layout-1's `child.glob("*.md")` must skip
	that subdir without aborting the whole walk — sibling subdirs and
	the layout-2 path keep working.
	"""
	import unittest.mock as mock
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		# Two layout-1 subdirs: one will simulate a glob OSError,
		# the other should be discovered cleanly.
		_write_per_phase_report(root, run_id="9", phase="clarify", findings="x", flatten=False)
		_write_per_phase_report(root, run_id="9", phase="plan", findings="y", flatten=False)
		clarify_dir = root / "soft-error-report-9-clarify"

		original_glob = Path.glob

		def _broken_glob(self, *args, **kwargs):
			if self == clarify_dir:
				raise OSError("simulated permission denied on subdir glob")
			return original_glob(self, *args, **kwargs)

		with mock.patch.object(Path, "glob", _broken_glob):
			found = csr.discover_reports(root)
		# The clarify subdir was skipped; plan was still discovered.
		phases = sorted(p for p, _ in found)
		assert phases == ["plan"], (
			f"expected ['plan'], got {phases!r} (clarify should have been "
			"skipped due to glob OSError)"
		)


def test_discover_oserror_on_is_dir_returns_empty():
	"""OSError from `input_dir.is_dir()` itself (e.g. stale NFS handle)
	must degrade to the empty stub rather than crash.
	"""
	import unittest.mock as mock
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		with mock.patch.object(Path, "is_dir", side_effect=OSError("stale handle")):
			found = csr.discover_reports(root)
		assert found == []


def test_discover_oserror_on_per_child_is_dir_skips_child():
	"""Per-child `is_dir()` OSError must skip that child without
	crashing — covers the inner `try/except` distinct from the outer
	one tested in `test_discover_oserror_on_is_dir_returns_empty`.
	"""
	import unittest.mock as mock
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		_write_per_phase_report(root, run_id="9", phase="clarify", findings="x", flatten=False)
		_write_per_phase_report(root, run_id="9", phase="plan", findings="y", flatten=False)
		clarify_dir = root / "soft-error-report-9-clarify"

		original_is_dir = Path.is_dir

		def _selective_is_dir_raise(self):
			if self == clarify_dir:
				raise OSError("simulated stat failure on child")
			return original_is_dir(self)

		with mock.patch.object(Path, "is_dir", _selective_is_dir_raise):
			found = csr.discover_reports(root)
		# clarify skipped due to per-child is_dir failure; plan recovered.
		phases = sorted(p for p, _ in found)
		assert phases == ["plan"], (
			f"expected ['plan'], got {phases!r} (clarify should have been "
			"skipped due to per-child is_dir OSError)"
		)


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


def test_findings_with_double_hash_in_body_not_truncated():
	"""Regression: a finding whose body legitimately contains a `##`
	line must not be truncated by `NEXT_HEADING_RE`. Multi-model PR
	review (minimax, grok-4) reproduced premature truncation when an
	LLM quoted a log heading inside a finding body. The fix anchors
	the next-heading regex to the two known stop sections from the
	analyser prompt (`## Patterns to watch`, `## Likely benign`).
	"""
	md = (
		"## Soft-error analyzer (status: `ok`, model: `m`, reasoning: `r`)\n\n"
		"## Soft errors (per phase)\n"
		"### clarify\n"
		"- LLM quoted a log heading verbatim:\n"
		"  ## Rate Limit Issue (this is INSIDE the finding body)\n"
		"- second finding line that previously got truncated\n"
		"\n## Patterns to watch\n"
		"- this content must NOT appear in the extracted findings\n"
	)
	body = csr.extract_findings_section(md)
	assert "## Rate Limit Issue" in body, (
		"finding body containing a `##` line must not be truncated"
	)
	assert "second finding line" in body, (
		"content after the inner `##` must survive extraction"
	)
	assert "Patterns to watch" not in body
	assert "this content must NOT appear" not in body


def test_real_finding_mentioning_placeholder_phrase_preserved():
	"""Regression: a legitimate short finding that *mentions* a
	placeholder phrase in prose ("no soft errors", "no findings",
	etc.) must NOT be filtered out. Five PR reviewers (deepseek,
	minimax, kimi-k2, grok-4, glm-5) reproduced the false-positive
	where the substring-based check dropped real findings under 160
	chars that happened to contain placeholder substrings.
	"""
	md = (
		"## Soft-error analyzer (status: `ok`, model: `m`, reasoning: `r`)\n\n"
		"## Soft errors (per phase)\n"
		"No soft errors were found in the rate limiting module, "
		"but the editor reviewer logged a noop-suspicious flip.\n"
	)
	body = csr.extract_findings_section(md)
	assert body != "", "real finding must not be filtered out"
	assert "rate limiting module" in body
	assert "noop-suspicious" in body


def test_pure_placeholder_body_still_filtered_with_bullet():
	"""Whole-line placeholder with a leading bullet still filtered."""
	md = (
		"## Soft-error analyzer (status: `ok`, model: `m`, reasoning: `r`)\n\n"
		"## Soft errors (per phase)\n"
		"- None observed.\n"
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


def test_zero_cap_disables_truncation():
	"""max_bytes=0 means the operator opted out of the cap entirely.

	The Python consolidator's `max_bytes <= 0` branch skips the
	truncation path and returns the full assembled summary. Verifies
	the contract Python documents (and the new shell sanitization
	relies on for `SOFT_ERROR_TELEGRAM_BYTES=0`).
	"""
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		# Make the summary > 2800 bytes so it would normally be truncated.
		_write_per_phase_report(
			root, run_id="9", phase="clarify",
			findings="### clarify\n" + ("X" * 4000),
		)
		_, summary = _run_consolidator(root, max_bytes=0)
		assert len(summary.encode("utf-8")) > 2800, "fixture must exceed default cap"
		assert "[truncated for Telegram cap" not in summary
		assert ("X" * 100) in summary


def test_truncate_helper_preserves_multibyte_content():
	"""Truncating after a multi-byte codepoint keeps it intact.

	Walking back over UTF-8 continuation bytes (top two bits == 0b10)
	must land on a codepoint boundary, so the resulting string round-
	trips cleanly through `encode("utf-8").decode("utf-8")` even when
	the requested cap straddles the middle of a 2- or 3-byte sequence.
	The `errors="replace"` decode setting is defence-in-depth: with
	the boundary walk in place no malformed sequences should reach
	the decode call, but if any do they surface as U+FFFD rather
	than silently disappearing (matching `_read_report_safe`'s
	`errors="replace"` policy).
	"""
	# 'αβγδε' is 5 codepoints × 2 bytes each = 10 bytes total.
	# Caps of 1..10 should all yield prefixes that decode cleanly.
	greek = "αβγδε"
	for cap in range(1, 12):
		out = csr._truncate_bytes_at_utf8_boundary(greek, cap)
		# Output must be valid UTF-8 (round-trip with strict decode).
		out.encode("utf-8").decode("utf-8")
		assert len(out.encode("utf-8")) <= cap


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
