#!/usr/bin/env python3
"""Build issue-level consensus from multiple parallel reviewer outputs.

Parses review_*.txt files, deduplicates issues across reviewers via file
path + line proximity and keyword Jaccard similarity, and emits a
backward-compatible plain-text consensus summary.  Exit 0 on all paths.
"""
from __future__ import annotations

import argparse, re, sys
from dataclasses import dataclass, field
from pathlib import Path

_LINE_RE = re.compile(r"(?:line|lines?)[:\s~#]*(\d+)", re.IGNORECASE)
_CONF_RE = re.compile(r"ISSUE_CONFIDENCE\s*:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_CODE_RE = re.compile(r"(?:Code|Line or code reference|Line)\s*:\s*(.+)", re.IGNORECASE)
_PROB_RE = re.compile(r"(?:Problem|Why it fails at runtime|Runtime impact)\s*:\s*(.+)", re.IGNORECASE)
_STOPWORDS = frozenset(
	"a an the is are was were be been being in on at to for of with by "
	"from as it its this that not no or and if but may can could will "
	"would should".split()
)

@dataclass
class Issue:
	file_path: str
	code_ref: str
	problem: str
	line_number: int | None
	confidence: float | None
	reviewer: str

@dataclass
class MergedIssue:
	file_path: str
	code_ref: str
	problem: str
	line_number: int | None
	confidences: list[float] = field(default_factory=list)
	reviewers: list[str] = field(default_factory=list)

	@property
	def avg_confidence(self) -> float | None:
		vals = [c for c in self.confidences if c is not None]
		return round(sum(vals) / len(vals), 1) if vals else None

def _extract_line(text: str) -> int | None:
	m = _LINE_RE.search(text)
	if m:
		try:
			return int(m.group(1))
		except ValueError:
			pass
	return None

def _reviewer_name(p: Path) -> str:
	"""Reverse safe_name encoding: ``___`` -> ``:``, ``__`` -> ``/``."""
	return p.stem.removeprefix("review_").replace("___", ":").replace("__", "/")

def _normalize_path(fp: str) -> str:
	return re.sub(r"\s*\(.*\)\s*$", "", fp).strip()

def _tokenize(text: str) -> set[str]:
	return set(re.findall(r"[a-z0-9_]+", text.lower())) - _STOPWORDS

def _jaccard(a: set[str], b: set[str]) -> float:
	return len(a & b) / len(a | b) if a and b else 0.0

# -- Parsing ----------------------------------------------------------------

def parse_review_file(path: Path) -> list[Issue]:
	"""Extract issues from one reviewer file.  Tolerant of format variations."""
	try:
		text = path.read_text(encoding="utf-8", errors="replace")
	except OSError as exc:
		print(f"WARNING: cannot read {path}: {exc}", file=sys.stderr)
		return []
	reviewer = _reviewer_name(path)
	issues: list[Issue] = []
	for block in re.split(r"(?m)^(?=File\s*:)", text):
		block = block.strip()
		fm = re.match(r"File\s*:\s*(.+)", block) if block else None
		if not fm:
			continue
		fp = fm.group(1).strip()
		if not fp:
			continue
		code_ref, problem_parts = "", []
		confidence: float | None = None
		line_number: int | None = None
		for raw in block[fm.end():].strip().splitlines():
			ln = raw.strip()
			if not ln:
				continue
			cm = _CODE_RE.match(ln)
			if cm:
				code_ref = cm.group(1).strip()
				line_number = line_number or _extract_line(code_ref)
				continue
			cfm = _CONF_RE.match(ln)
			if cfm:
				try:
					confidence = float(cfm.group(1))
				except ValueError:
					pass
				continue
			pm = _PROB_RE.match(ln)
			if pm:
				problem_parts.append(pm.group(1).strip())
				continue
			if len(ln) > 5:
				problem_parts.append(ln)
		if line_number is None:
			line_number = _extract_line(fp)
		issues.append(Issue(
			file_path=fp, code_ref=code_ref,
			problem="; ".join(problem_parts) if problem_parts else "(no description)",
			line_number=line_number, confidence=confidence, reviewer=reviewer,
		))
	return issues

# -- Deduplication ----------------------------------------------------------

def deduplicate_issues(all_issues: list[Issue], threshold: float = 0.3) -> list[MergedIssue]:
	"""Merge issues from different reviewers that target the same problem.

	Within the same normalized file path, issues match when line numbers
	are within +-10 lines OR problem-text Jaccard similarity >= threshold.
	"""
	by_file: dict[str, list[Issue]] = {}
	for iss in all_issues:
		by_file.setdefault(_normalize_path(iss.file_path), []).append(iss)
	merged: list[MergedIssue] = []
	for norm_path, issues in by_file.items():
		clusters: list[MergedIssue] = []
		for iss in issues:
			tokens = _tokenize(iss.problem)
			hit = None
			for cl in clusters:
				if iss.reviewer in cl.reviewers:
					continue
				lines_close = (
					iss.line_number is not None
					and cl.line_number is not None
					and abs(iss.line_number - cl.line_number) <= 10
				)
				if lines_close or _jaccard(tokens, _tokenize(cl.problem)) >= threshold:
					hit = cl
					break
			if hit:
				hit.reviewers.append(iss.reviewer)
				if iss.confidence is not None:
					hit.confidences.append(iss.confidence)
				if len(iss.problem) > len(hit.problem):
					hit.problem = iss.problem
				if hit.line_number is None and iss.line_number is not None:
					hit.line_number = iss.line_number
			else:
				clusters.append(MergedIssue(
					file_path=norm_path, code_ref=iss.code_ref,
					problem=iss.problem, line_number=iss.line_number,
					confidences=[iss.confidence] if iss.confidence is not None else [],
					reviewers=[iss.reviewer],
				))
		merged.extend(clusters)
	return merged

# -- Output -----------------------------------------------------------------

def _fmt(idx: int, mi: MergedIssue) -> str:
	lh = f" (line ~{mi.line_number})" if mi.line_number else ""
	cs = str(mi.avg_confidence) if mi.avg_confidence is not None else "n/a"
	return (
		f"  [{idx}] File: {mi.file_path}{lh}\n"
		f"      Problem: {mi.problem}\n"
		f"      Confidence: avg {cs}\n"
		f"      Flagged by: {', '.join(mi.reviewers)}"
	)

def build_output(merged: list[MergedIssue]) -> str:
	high = [m for m in merged if len(m.reviewers) >= 2]
	low = [m for m in merged if len(m.reviewers) < 2]
	out: list[str] = [
		"ISSUE-LEVEL CONSENSUS SUMMARY",
		f"Total unique issues: {len(merged)}",
		f"Issues with multi-reviewer agreement: {len(high)}",
		"",
		"HIGH CONFIDENCE (flagged by >=2 reviewers):",
	]
	idx = 1
	for mi in (high or []):
		out += [_fmt(idx, mi), ""]
		idx += 1
	if not high:
		out += ["  (none)", ""]
	out.append("LOW CONFIDENCE (flagged by 1 reviewer only):")
	for mi in (low or []):
		out += [_fmt(idx, mi), ""]
		idx += 1
	if not low:
		out += ["  (none)", ""]
	# Backward-compatible FILE-LEVEL SUMMARY
	high_files = sorted({m.file_path for m in high})
	low_files = sorted({m.file_path for m in low} - set(high_files))
	out.append("FILE-LEVEL SUMMARY (backward compatible):")
	out.append("High confidence files (mentioned by >=2 reviewers):")
	out.extend(high_files or ["(none)"])
	out.append("")
	out.append("Low confidence files (mentioned by 1 reviewer):")
	out.extend(low_files or ["(none)"])
	return "\n".join(out) + "\n"

# -- CLI --------------------------------------------------------------------

def main() -> None:
	ap = argparse.ArgumentParser(description="Build issue-level consensus from reviewer outputs.")
	ap.add_argument("--reviews-dir", required=True, help="Directory with review_*.txt files.")
	ap.add_argument("--output", required=True, help="Output file path.")
	args = ap.parse_args()
	reviews_dir, output_path = Path(args.reviews_dir), Path(args.output)

	def _write_empty(reason: str) -> None:
		print(f"WARNING: {reason}", file=sys.stderr)
		output_path.parent.mkdir(parents=True, exist_ok=True)
		output_path.write_text(build_output([]), encoding="utf-8")

	if not reviews_dir.is_dir():
		_write_empty(f"reviews directory does not exist: {reviews_dir}")
		sys.exit(0)
	review_files = sorted(reviews_dir.glob("review_*.txt"))
	if not review_files:
		_write_empty("no review_*.txt files found")
		sys.exit(0)
	all_issues: list[Issue] = []
	for rf in review_files:
		try:
			parsed = parse_review_file(rf)
			print(f"Parsed {len(parsed)} issue(s) from {rf.name}")
			all_issues.extend(parsed)
		except Exception as exc:
			print(f"WARNING: failed to parse {rf.name}: {exc}", file=sys.stderr)
	merged = deduplicate_issues(all_issues)
	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(build_output(merged), encoding="utf-8")
	hc = sum(1 for m in merged if len(m.reviewers) >= 2)
	print(f"Consensus: {len(merged)} unique issues, {hc} with multi-reviewer agreement.")
	sys.exit(0)

if __name__ == "__main__":
	main()
