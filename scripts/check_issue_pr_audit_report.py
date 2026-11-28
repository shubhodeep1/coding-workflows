#!/usr/bin/env python3
"""Structure check for the daily issue/PR audit report (#10153).

The report body is model output, so it is untrusted: this script reads it as
text and never echoes it. It prints exactly one line to stdout:

	ISSUE_PR_AUDIT_REPORT_CHECK outcome=<ok|invalid> marker=<true|false>
	missing_headings=<n> order_ok=<true|false> missing_urls=<n>
	dangling_aud=<n> bytes=<n> too_small=<true|false>

(one line; wrapped here). The report is valid when:

* line 1 is the trusted marker
  ``<!-- issue-pr-audit: window_start=<ISO> window_end=<ISO> run=<n> repos=<n> -->``;
* the nine ``## `` headings appear, in order (REPORT_HEADINGS);
* every deep-reviewed flagged item and every new blocked item of
  ``--digest`` (its URL, or its ``private:<hash>#<n>`` label) is mentioned
  outside the Metrics Appendix (entries marked ``omitted`` at the model input
  size cap are not required); a URL must not be followed by another digit;
* every ``AUD-###`` named on an item line (``- `` lines of the Unnecessary,
  Incorrectly Resolved and Blocked sections) has a ``### AUD-###:`` heading
  under Fix Plans;
* it is at least ``--min-bytes`` (default 1,500) bytes.

``--errors-out FILE`` writes a fixed-vocabulary summary (headings and counts,
never report text) the run script appends to the retry prompt.

Exit codes: 0 valid, 1 invalid, 2 the report or digest could not be read.
Stdlib only; no network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPORT_HEADINGS = (
	"Executive Summary",
	"Coverage",
	"Unnecessary Items",
	"Incorrectly Resolved Items",
	"Blocked Items",
	"Fix Plans",
	"Observations",
	"Clean Items",
	"Metrics Appendix",
)
ITEM_SECTIONS = ("Unnecessary Items", "Incorrectly Resolved Items", "Blocked Items")
MARKER_RE = re.compile(
	r"^<!-- issue-pr-audit: window_start=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z "
	r"window_end=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z run=[0-9]+ repos=[0-9]+ -->$"
)
HEADING_RE = re.compile(r"^## +(.+?)\s*$")
AUD_RE = re.compile(r"\bAUD-[0-9]{3}\b")
PLAN_RE = re.compile(r"^### +(AUD-[0-9]{3}):", re.M)
DEFAULT_MIN_BYTES = 1500


def split_sections(text: str) -> dict[str, str]:
	sections: dict[str, list[str]] = {}
	current = None
	for line in text.splitlines():
		match = HEADING_RE.match(line)
		if match:
			current = match.group(1)
			sections.setdefault(current, [])
			continue
		if current is not None:
			sections[current].append(line)
	return {name: "\n".join(lines) for name, lines in sections.items()}


def required_refs(digest: dict) -> list[str]:
	# Entries render_model_input left out at the input size cap are not required.
	refs = [entry.get("display") for entry in digest.get("items") or [] if isinstance(entry, dict) and entry.get("deep") and not entry.get("omitted")]
	refs += [entry.get("display") for entry in digest.get("blocked") or [] if isinstance(entry, dict) and entry.get("new") and not entry.get("omitted")]
	return [ref for ref in dict.fromkeys(refs) if isinstance(ref, str) and ref]


def check(text: str, digest: dict, min_bytes: int = DEFAULT_MIN_BYTES) -> dict:
	lines = text.splitlines()
	marker = bool(lines) and bool(MARKER_RE.match(lines[0]))
	found = [match.group(1) for match in (HEADING_RE.match(line) for line in lines) if match and match.group(1) in REPORT_HEADINGS]
	missing = [heading for heading in REPORT_HEADINGS if heading not in found]
	positions = [found.index(heading) for heading in REPORT_HEADINGS if heading in found]
	order_ok = not missing and positions == sorted(positions) and len(found) == len(REPORT_HEADINGS)
	sections = split_sections(text)
	body = text.split("\n## Metrics Appendix", 1)[0]
	missing_refs = [ref for ref in required_refs(digest) if not re.search(re.escape(ref) + r"(?![0-9])", body)]
	plans = set(PLAN_RE.findall(sections.get("Fix Plans", "")))
	referenced: set[str] = set()
	for name in ITEM_SECTIONS:
		for line in sections.get(name, "").splitlines():
			if line.startswith("- "):
				referenced.update(AUD_RE.findall(line))
	dangling = sorted(referenced - plans)
	size = len(text.encode("utf-8"))
	too_small = size < min_bytes
	valid = marker and not missing and order_ok and not missing_refs and not dangling and not too_small
	return {
		"valid": valid,
		"marker": marker,
		"missing_headings": missing,
		"order_ok": order_ok,
		"missing_refs": missing_refs,
		"dangling_aud": dangling,
		"bytes": size,
		"too_small": too_small,
	}


def summary_line(result: dict) -> str:
	return (
		f"ISSUE_PR_AUDIT_REPORT_CHECK outcome={'ok' if result['valid'] else 'invalid'} marker={'true' if result['marker'] else 'false'} "
		f"missing_headings={len(result['missing_headings'])} order_ok={'true' if result['order_ok'] else 'false'} "
		f"missing_urls={len(result['missing_refs'])} dangling_aud={len(result['dangling_aud'])} bytes={result['bytes']} "
		f"too_small={'true' if result['too_small'] else 'false'}"
	)


def retry_notes(result: dict) -> str:
	"""Feedback for the retry prompt: fixed headings, digest refs and AUD ids only."""
	notes = ["The previous report failed the structure check:"]
	if result["missing_headings"] or not result["order_ok"]:
		notes.append("- Use exactly these `## ` headings, in this order: " + ", ".join(REPORT_HEADINGS[:-1]) + ".")
	for ref in result["missing_refs"][:100]:
		notes.append(f"- Not mentioned in any section: {ref}")
	for aud in result["dangling_aud"][:50]:
		notes.append(f"- {aud} is referenced on an item line but has no `### {aud}:` heading under Fix Plans.")
	if result["too_small"]:
		notes.append("- The report is too short; give every item its evidence.")
	return "\n".join(notes) + "\n"


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--report", required=True)
	parser.add_argument("--digest", required=True)
	parser.add_argument("--min-bytes", type=int, default=DEFAULT_MIN_BYTES)
	parser.add_argument("--errors-out", default="")
	args = parser.parse_args(argv)
	try:
		text = Path(args.report).read_text(encoding="utf-8", errors="replace")
		digest = json.loads(Path(args.digest).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		print("ISSUE_PR_AUDIT_REPORT_CHECK outcome=error reason=unreadable")
		return 2
	if not isinstance(digest, dict):
		print("ISSUE_PR_AUDIT_REPORT_CHECK outcome=error reason=unreadable")
		return 2
	result = check(text, digest, args.min_bytes)
	print(summary_line(result))
	if args.errors_out and not result["valid"]:
		Path(args.errors_out).write_text(retry_notes(result), encoding="utf-8")
	return 0 if result["valid"] else 1


if __name__ == "__main__":
	sys.exit(main())
