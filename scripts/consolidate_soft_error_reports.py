#!/usr/bin/env python3
"""Consolidate per-phase soft-error reports into one deterministic markdown file.

The release-gate smoke test (`test-and-mark-stable.yml`) emits one soft-error
report artifact per phase via `.github/actions/run-soft-error-analyzer`. The
historical Phase 8 analyser then re-summarised the raw logs through OpenRouter
to produce a single combined report; that step (a) only saw 6 of 17 phases
because it lived inside `e2e-smoke-test`, and (b) re-ran the LLM with the
"<800 words" output cap, which dropped per-phase findings during
consolidation.

This script replaces that step with a pure-concat merge: it reads every
downloaded per-phase report, concatenates them in a deterministic order, and
extracts each report's `## Soft errors (per phase)` section to assemble a
section-aware Telegram summary that fits inside Telegram's 4096-char body cap
without byte-truncating mid-finding.

Inputs are filesystem-only; no network calls. Non-blocking: missing inputs or
unparseable reports yield warnings, never hard failures.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Sibling-script import: both this file and `truncate_to_utf8_byte_cap.py`
# live in scripts/, and the workflow runs us as `python3 scripts/...`,
# so scripts/ is on sys.path. The pattern matches `ai_memory.py`'s
# `from ai_memory_lib import ...`. Sharing the truncation algorithm
# keeps boundary handling in lockstep across both code paths — the
# tests that exercise the helper indirectly cover the consolidator
# too.
from truncate_to_utf8_byte_cap import truncate_bytes_to_utf8_cap

# Canonical phase order. Phases produced by the release-gate smoke test
# in pipeline order so the consolidated report reads top-to-bottom along
# the same axis an operator would mentally trace through the workflow.
# Anything not listed here is sorted alphabetically and appended.
CANONICAL_PHASE_ORDER = [
	"clarify",
	"plan",
	"implement",
	"review_autofix",
	"orchestrate_poll",
	"cancel_on_pr_close",
	"workflow_log_analysis",
	"validation_refresh",
	"update_workflows",
	"memory_maintenance",
	"orchestrate_decompose",
	"validate_standalone",
	"clarify_negative",
	"alt_clarify",
	"alt_plan",
	"alt_implement",
	"alt_review_autofix",
]

# Per-phase artifact names follow the pattern
# `soft-error-report-<run_id>-<phase>`. After download via
# `actions/download-artifact@v6` with `merge-multiple: false` (the default),
# each artifact lands in its own subdirectory named after the artifact;
# `merge-multiple: true` flattens them into the input dir. We support both.
ARTIFACT_NAME_RE = re.compile(r"^soft-error-report-\d+-(?P<phase>.+?)(?:\.md)?$")
REPORT_FILENAME_RE = re.compile(r"^soft-error-report-\d+-(?P<phase>.+?)\.md$")

# `## Soft errors (per phase)` section header emitted by the per-phase
# analyser prompt. We extract everything from this header up to (but not
# including) the next `## ` heading or end-of-file.
SECTION_HEADER_RE = re.compile(r"^##\s+Soft errors\s+\(per phase\)\s*$", re.MULTILINE)
NEXT_HEADING_RE = re.compile(r"^##\s+\S", re.MULTILINE)

# Header line emitted by analyze_soft_errors.py:
# `## Soft-error analyzer (status: <code>, model: ..., reasoning: ...)`
ANALYSER_HEADER_RE = re.compile(
	r"^##\s+Soft-error analyzer\s+\(status:\s*`(?P<status>[^`]+)`",
	re.MULTILINE,
)


def discover_reports(input_dir: Path) -> list[tuple[str, Path]]:
	"""Return [(phase, markdown_path)] for every per-phase report found.

	Tolerates both layouts produced by `actions/download-artifact@v6`:
	(a) merge-multiple=false (default): each artifact in its own
	subdirectory `<input_dir>/soft-error-report-<run>-<phase>/<file>.md`.
	(b) merge-multiple=true: flattened into `<input_dir>/<file>.md`.
	The artifact contents are written by the composite action as
	`/tmp/<artifact_name>.md`, so the inner filename matches the artifact
	name with a `.md` suffix.
	"""
	found: list[tuple[str, Path]] = []
	if not input_dir.is_dir():
		return found

	# Layout 1: subdir per artifact.
	for child in sorted(input_dir.iterdir()):
		if child.is_dir():
			m = ARTIFACT_NAME_RE.match(child.name)
			if not m:
				continue
			phase = m.group("phase")
			# Pick the first .md inside the subdir. The composite action
			# writes exactly one file per artifact, so this is unambiguous.
			md_files = sorted(child.glob("*.md"))
			if md_files:
				found.append((phase, md_files[0]))

	# Layout 2: flattened into input_dir.
	for md in sorted(input_dir.glob("*.md")):
		m = REPORT_FILENAME_RE.match(md.name)
		if not m:
			continue
		phase = m.group("phase")
		# Skip if we already picked this phase up via layout 1.
		if any(p == phase for p, _ in found):
			continue
		found.append((phase, md))

	return found


def order_reports(reports: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
	"""Sort reports by canonical pipeline order, then alphabetically."""
	canonical_index = {phase: i for i, phase in enumerate(CANONICAL_PHASE_ORDER)}
	return sorted(
		reports,
		key=lambda pair: (canonical_index.get(pair[0], len(canonical_index)), pair[0]),
	)


def extract_findings_section(markdown: str) -> str:
	"""Return the `## Soft errors (per phase)` section body, or empty string.

	Returns "" when the section is absent or contains only whitespace /
	"none" / "no findings" placeholders. The caller treats "" as
	"this phase had no findings worth surfacing in the summary".
	"""
	header_match = SECTION_HEADER_RE.search(markdown)
	if not header_match:
		return ""
	start = header_match.end()
	tail = markdown[start:]
	next_match = NEXT_HEADING_RE.search(tail)
	body = tail[: next_match.start()] if next_match else tail
	body = body.strip()
	if not body:
		return ""
	# Drop placeholder bodies the model emits when it found nothing.
	lower = body.lower()
	placeholder_signatures = (
		"no soft errors",
		"no findings",
		"none observed",
		"no issues",
		"(none)",
	)
	stripped_punct = re.sub(r"[\s\.\-_*`]+", " ", lower).strip()
	if stripped_punct in {"none", "n a", "na"}:
		return ""
	if any(sig in lower and len(body) < 160 for sig in placeholder_signatures):
		return ""
	return body


def _truncate_bytes_at_utf8_boundary(text: str, cap: int) -> str:
	"""Truncate `text` so its UTF-8 byte length is `<= cap`, on a codepoint
	boundary. Returns "" when `cap <= 0` (per the consolidator's
	`max_bytes <= 0` opt-out semantic). Wraps the canonical
	`truncate_bytes_to_utf8_cap` helper so both scripts share boundary
	handling — see that module's docstring for the algorithm.

	Decodes with `errors="replace"` so any malformed UTF-8 that survived
	an upstream `errors="replace"` read surfaces as visible U+FFFD
	characters rather than silently dropped — diagnostic behaviour stays
	consistent with `_read_report_safe`.
	"""
	if cap <= 0:
		return ""
	truncated = truncate_bytes_to_utf8_cap(text.encode("utf-8"), cap)
	return truncated.decode("utf-8", errors="replace")


def parse_status(markdown: str) -> str:
	m = ANALYSER_HEADER_RE.search(markdown)
	return m.group("status") if m else "unknown"


def _read_report_safe(path: Path) -> tuple[str | None, str | None]:
	"""Read a per-phase report, returning (text, error). Fail-open by design.

	A single malformed artifact must not crash the consolidator (the entry-
	point docstring promises non-blocking behaviour). `OSError` covers
	missing files / permission issues; reading with `errors="replace"`
	covers non-UTF-8 byte sequences (e.g. binary garbage in a corrupted
	artifact) by substituting U+FFFD rather than raising
	`UnicodeDecodeError`.
	"""
	try:
		# `errors="replace"` is intentional — see docstring above. Any
		# replacement chars in the input would already be present in the
		# original analyser report (which itself reads logs with
		# `errors="replace"`), so this only kicks in for genuinely
		# corrupted artifacts.
		return path.read_text(encoding="utf-8", errors="replace"), None
	except OSError as exc:
		return None, f"{type(exc).__name__}: {exc}"


def build_full_report(ordered: list[tuple[str, Path]]) -> str:
	"""Concatenate per-phase reports verbatim with phase-banner separators."""
	parts: list[str] = [
		"# Consolidated soft-error report",
		"",
		(
			"Pure concatenation of every per-phase soft-error analyser report "
			"emitted during this release-gate run. Each section below is the "
			"verbatim output of `scripts/analyze_soft_errors.py` for one "
			"phase; no further LLM summarisation is applied at the "
			"consolidation step."
		),
		"",
		f"Phases included ({len(ordered)}): "
		+ ", ".join(phase for phase, _ in ordered)
		+ ".",
		"",
		"---",
		"",
	]
	for phase, path in ordered:
		body, err = _read_report_safe(path)
		if body is None:
			body = f"_(could not read {path.name}: {err})_"
		parts.append(f"## Phase: `{phase}`")
		parts.append("")
		parts.append(body.rstrip())
		parts.append("")
		parts.append("---")
		parts.append("")
	return "\n".join(parts).rstrip() + "\n"


def build_summary(
	ordered: list[tuple[str, Path]],
	*,
	max_bytes: int,
) -> str:
	"""Build a section-aware Telegram summary.

	Iterates phases in canonical order, extracting each phase's
	`## Soft errors (per phase)` section and emitting it under a phase
	heading. If no phase has findings, returns the literal "no findings"
	stub so the operator can distinguish "analyser ran clean" from
	"analyser silently broke" (the latter still surfaces via the report
	header `status` codes which are included up-front).
	"""
	lines: list[str] = ["## Soft-error consolidation"]
	statuses: list[tuple[str, str]] = []
	finding_blocks: list[str] = []

	for phase, path in ordered:
		markdown, _err = _read_report_safe(path)
		if markdown is None:
			statuses.append((phase, "unreadable"))
			continue
		statuses.append((phase, parse_status(markdown)))
		findings = extract_findings_section(markdown)
		if findings:
			finding_blocks.append(f"### {phase}\n{findings}")

	# Status pill line — always emitted, lets operator spot analyser
	# crashes (call_failed / api_skipped / unreadable) at a glance.
	pill = " ".join(f"{phase}={status}" for phase, status in statuses)
	if pill:
		lines.append(f"_status:_ {pill}")
		lines.append("")

	if finding_blocks:
		lines.append("\n\n".join(finding_blocks))
	else:
		lines.append("_No soft-error findings reported by any phase._")

	summary = "\n".join(lines).rstrip() + "\n"

	if max_bytes > 0 and len(summary.encode("utf-8")) > max_bytes:
		# Section-aware truncation: drop whole finding blocks from the
		# tail until we fit. This preserves the most likely-relevant
		# (canonical-order earliest) phases over later supplemental
		# phases. The status pill always survives.
		head = "\n".join(lines[: 2 if pill else 1]) + "\n"
		kept_blocks: list[str] = []
		running = head
		# Track the running byte length incrementally to avoid re-encoding
		# the cumulative string on every iteration (O(n²) → O(n)).
		running_bytes = len(running.encode("utf-8"))
		truncation_marker = "\n\n…[truncated for Telegram cap; see consolidated artifact]\n"
		marker_bytes = len(truncation_marker.encode("utf-8"))
		for block in finding_blocks:
			# Block separator matches the original concat (`"\n" + block + "\n"`).
			separator_and_block_bytes = len(("\n" + block + "\n").encode("utf-8"))
			if running_bytes + separator_and_block_bytes + marker_bytes > max_bytes:
				break
			running += "\n" + block + "\n"
			running_bytes += separator_and_block_bytes
			kept_blocks.append(block)
		if kept_blocks:
			summary = running.rstrip() + truncation_marker
		else:
			# Even the first block doesn't fit. Truncate at a valid UTF-8
			# character boundary so we never split a multi-byte codepoint
			# (release-gate logs contain ✓/✗/⏳/—/etc.). Walking back over
			# any continuation bytes (top two bits == 0b10) lands `i` on
			# the start of the next codepoint. The `+1` accounts for the
			# `"\n"` separator we'll insert between `head` and `inline`
			# when assembling the final string — without it we would
			# overflow `max_bytes` by one byte.
			separator_bytes = len("\n".encode("utf-8"))
			budget = (
				max_bytes
				- len(head.encode("utf-8"))
				- separator_bytes
				- marker_bytes
			)
			if budget > 0 and finding_blocks:
				inline = _truncate_bytes_at_utf8_boundary(finding_blocks[0], budget)
				summary = head + "\n" + inline + truncation_marker
			else:
				summary = head + truncation_marker

	# Last-resort cap guard: if the assembled `head + marker` (or even
	# `head` alone, on a tiny cap) still overflows `max_bytes`, truncate
	# the whole summary on a UTF-8 boundary. This keeps the contract
	# `len(summary.encode('utf-8')) <= max_bytes` an absolute invariant
	# rather than a best-effort claim, so the Telegram send step can
	# rely on it without re-checking.
	if max_bytes > 0 and len(summary.encode("utf-8")) > max_bytes:
		summary = _truncate_bytes_at_utf8_boundary(summary, max_bytes)
	return summary


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--input-dir",
		required=True,
		type=Path,
		help="Directory containing downloaded per-phase artifacts.",
	)
	parser.add_argument(
		"--output-full",
		required=True,
		type=Path,
		help="Path to write the full concatenated markdown report.",
	)
	parser.add_argument(
		"--output-summary",
		required=True,
		type=Path,
		help="Path to write the section-aware Telegram summary.",
	)
	parser.add_argument(
		"--max-summary-bytes",
		type=int,
		default=2800,
		help=(
			"Byte cap for the Telegram summary. Set to 0 to disable. "
			"Default 2800 leaves headroom under Telegram's 4096-char "
			"body cap for the rest of the release-status message."
		),
	)
	parser.add_argument(
		"--download-status",
		default="success",
		help=(
			"Outcome of the upstream `actions/download-artifact` step "
			"(`success`, `failure`, `cancelled`, or `skipped`). Only "
			"`failure` and `cancelled` trigger the DOWNLOAD FAILED stub; "
			"any other value (including `unknown` from broken env "
			"plumbing) falls through to the neutral 'no artifacts' "
			"stub so a miswired pipeline cannot fabricate a fake outage."
		),
	)
	args = parser.parse_args()

	args.output_full.parent.mkdir(parents=True, exist_ok=True)
	args.output_summary.parent.mkdir(parents=True, exist_ok=True)

	reports = discover_reports(args.input_dir)
	if not reports:
		# Only escalate to a "DOWNLOAD FAILED" stub for the genuinely
		# negative GitHub Actions step outcomes — `failure` (the
		# download step failed under `continue-on-error`) and
		# `cancelled`. Anything else (`success`, `skipped`, `""`,
		# `unknown`, or a typo from broken env plumbing) falls through
		# to the neutral "no artifacts" stub. Default-deny on every
		# non-success token would make a routine env-var miswire look
		# identical to a real download outage in the consolidated
		# artifact, drowning real outage signal in noise.
		download_failed = args.download_status in ("failure", "cancelled")
		if download_failed:
			print(
				f"::warning::consolidate_soft_error_reports: no per-phase "
				f"reports found in {args.input_dir} AND download step "
				f"reported `{args.download_status}`; emitting "
				f"download-failure stub. Real findings may exist upstream.",
				file=sys.stderr,
			)
			full_body = (
				"# Consolidated soft-error report\n\n"
				f"**DOWNLOAD FAILED.** The upstream "
				f"`actions/download-artifact` step reported "
				f"`{args.download_status}`, so no per-phase soft-error "
				"artifacts could be retrieved for consolidation. Real "
				"findings may exist upstream — inspect the individual "
				"`soft-error-report-<run_id>-<phase>` artifacts directly "
				"in the run summary.\n"
			)
			summary_body = (
				"## Soft-error consolidation\n"
				f"_DOWNLOAD FAILED (`{args.download_status}`); per-phase "
				"artifacts could not be retrieved. Inspect them "
				"individually in the run's artifact list._\n"
			)
		else:
			print(
				f"::warning::consolidate_soft_error_reports: no per-phase "
				f"reports found in {args.input_dir}; emitting empty "
				f"consolidation.",
				file=sys.stderr,
			)
			full_body = (
				"# Consolidated soft-error report\n\n"
				"_No per-phase soft-error artifacts were downloaded._\n"
			)
			summary_body = (
				"## Soft-error consolidation\n"
				"_No per-phase soft-error artifacts were downloaded._\n"
			)
		args.output_full.write_text(full_body, encoding="utf-8")
		args.output_summary.write_text(summary_body, encoding="utf-8")
		return 0

	ordered = order_reports(reports)
	args.output_full.write_text(build_full_report(ordered), encoding="utf-8")
	args.output_summary.write_text(
		build_summary(ordered, max_bytes=args.max_summary_bytes),
		encoding="utf-8",
	)

	print(
		f"consolidate_soft_error_reports: merged {len(ordered)} phase reports "
		f"({', '.join(p for p, _ in ordered)}) -> {args.output_full}, "
		f"summary -> {args.output_summary}",
		file=sys.stderr,
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())
