#!/usr/bin/env python3
"""Unit tests for scripts/check_workflow_analysis_report.py (project #9116, P2)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_workflow_analysis_report.py"

HEADINGS = (
	"Executive Summary",
	"Speed Optimizations",
	"Cost Optimizations",
	"Reliability Improvements",
	"AI Memory Health",
	"GH API Call Audit",
	"Prompt Cache & Memory System",
	"Orchestrator Health",
	"Pipeline Flow Bottlenecks",
	"Per-Repo Breakdown",
	"Metrics Appendix",
)
REPO_A = "shubhodeep1/coding-workflows"
REPO_B = "owner/consumer-app"
FILLER = "Evidence and findings for this section. " * 6
GAP_LINE = "> Data gap: full workflow logs were unavailable for this run."


def _report(headings=HEADINGS, repos=(REPO_A, REPO_B), preamble: str = "", filler: str = FILLER) -> str:
	parts: list[str] = []
	if preamble:
		parts.append(preamble)
	parts.append("# Workflow optimization report\n")
	for title in headings:
		parts.append(f"## {title}\n")
		parts.append(f"{filler}\n")
		if title == "Per-Repo Breakdown":
			for repo in repos:
				parts.append(f"### {repo}\n")
				parts.append(f"{filler}\n")
	return "\n".join(parts)


def _collect(repos=(REPO_A, REPO_B), errors=None, coverage=None) -> dict:
	payload: dict = {"scope": {"repositories": list(repos)}, "errors": errors or []}
	if coverage is not None:
		payload["coverage"] = coverage
	return payload


def _run(tmp_path: Path, report: str | None, collect: dict | str | None, *extra: str):
	report_path = tmp_path / "report.md"
	if report is not None:
		report_path.write_text(report, encoding="utf-8")
	collect_path = tmp_path / "workflow_log_report.json"
	if isinstance(collect, dict):
		collect_path.write_text(json.dumps(collect), encoding="utf-8")
	elif isinstance(collect, str):
		collect_path.write_text(collect, encoding="utf-8")
	proc = subprocess.run(
		[sys.executable, "-I", "-B", str(SCRIPT), "--report", str(report_path), "--collect-report", str(collect_path), *extra],
		capture_output=True,
		text=True,
		check=False,
	)
	return proc, report_path


def _check_line(stdout: str) -> str:
	lines = [line for line in stdout.splitlines() if line.startswith("WORKFLOW_LOG_ANALYSIS_REPORT_CHECK ")]
	assert len(lines) == 1, stdout
	return lines[0]


def _expected(outcome: str, missing_headings: int, missing_repos: str, report: str, too_small: str = "false", exempt_repos: str = "none", exempt_source: str = "errors") -> str:
	return (
		f"WORKFLOW_LOG_ANALYSIS_REPORT_CHECK outcome={outcome} missing_headings={missing_headings}"
		f" missing_repos={missing_repos} bytes={len(report.encode('utf-8'))} too_small={too_small}"
		f" exempt_repos={exempt_repos} exempt_source={exempt_source}"
	)


def test_valid_report_passes(tmp_path: Path) -> None:
	report = _report()
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert _check_line(proc.stdout) == _expected("ok", 0, "none", report)
	assert "::warning::" not in proc.stdout


def test_missing_heading_is_invalid(tmp_path: Path) -> None:
	report = _report(headings=tuple(h for h in HEADINGS if h != "GH API Call Audit"))
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 1, "none", report)
	assert "::warning::" in proc.stdout
	assert "GH API Call Audit" in proc.stdout


def test_out_of_order_heading_is_invalid(tmp_path: Path) -> None:
	order = list(HEADINGS)
	order[1], order[2] = order[2], order[1]
	report = _report(headings=tuple(order))
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 1, "none", report)


def test_missing_repo_subsection_is_invalid(tmp_path: Path) -> None:
	report = _report(repos=(REPO_A,))
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 0, REPO_B, report)


def test_repo_match_is_case_insensitive_substring(tmp_path: Path) -> None:
	report = _report(repos=("Repo: SHUBHODEEP1/Coding-Workflows (source)", REPO_B))
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 0, proc.stdout
	assert _check_line(proc.stdout) == _expected("ok", 0, "none", report)


def test_too_small_report_is_invalid(tmp_path: Path) -> None:
	report = _report(filler="x")
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 0, "none", report, too_small="true")
	proc, _ = _run(tmp_path, report, _collect(), "--min-bytes", "10")
	assert proc.returncode == 0
	assert _check_line(proc.stdout) == _expected("ok", 0, "none", report)


def test_html_comment_marker_and_data_gap_preamble_pass(tmp_path: Path) -> None:
	report = _report(preamble="<!-- workflow-log-analysis: window=2026-11-01..2026-11-08 -->\n" + GAP_LINE + "\n")
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 0, proc.stdout
	assert _check_line(proc.stdout) == _expected("ok", 0, "none", report)


def test_heading_inside_code_fence_is_not_counted(tmp_path: Path) -> None:
	headings = tuple(h for h in HEADINGS if h != "Orchestrator Health")
	report = _report(headings=headings)
	report = report.replace("## Pipeline Flow Bottlenecks\n", "```\n## Orchestrator Health\n```\n\n## Pipeline Flow Bottlenecks\n", 1)
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 1, "none", report)


def test_repo_heading_inside_code_fence_is_not_counted(tmp_path: Path) -> None:
	report = _report(repos=(REPO_A,))
	report = report.replace(f"### {REPO_A}\n", f"### {REPO_A}\n```\n### {REPO_B}\n```\n", 1)
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 0, REPO_B, report)


def test_coverage_repositories_failed_exempts_repo(tmp_path: Path) -> None:
	report = _report(repos=(REPO_A,))
	coverage = {"repositories_failed": [{"repository": REPO_B, "message": "HTTP 404"}]}
	proc, _ = _run(tmp_path, report, _collect(coverage=coverage))
	assert proc.returncode == 0, proc.stdout
	assert _check_line(proc.stdout) == _expected("ok", 0, "none", report, exempt_repos=REPO_B, exempt_source="coverage")
	# Plain-string entries are accepted too, and coverage wins over errors[].
	errors = [{"repository": REPO_A, "scope": "runs", "message": "HTTP 500"}]
	proc, _ = _run(tmp_path, report, _collect(errors=errors, coverage={"repositories_failed": [REPO_B]}))
	assert proc.returncode == 0, proc.stdout
	assert _check_line(proc.stdout) == _expected("ok", 0, "none", report, exempt_repos=REPO_B, exempt_source="coverage")


def test_errors_fallback_exempts_failed_repo_but_not_max_runs_cap(tmp_path: Path) -> None:
	report = _report(repos=())
	errors = [
		{"repository": REPO_B, "scope": "runs", "message": "HTTP 403: Resource not accessible"},
		{"repository": REPO_A, "scope": "runs", "message": "Run collection stopped after max-runs=200"},
		{"repository": REPO_A, "scope": "logs", "message": "log download failed"},
	]
	proc, _ = _run(tmp_path, report, _collect(errors=errors))
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 0, REPO_A, report, exempt_repos=REPO_B, exempt_source="errors")


def test_unreadable_collect_report_skips_repo_check_but_checks_headings(tmp_path: Path) -> None:
	report = _report(repos=())
	proc, _ = _run(tmp_path, report, "{not json")
	assert proc.returncode == 0, proc.stdout
	assert _check_line(proc.stdout) == _expected("ok", 0, "none", report, exempt_source="unavailable")
	assert "::warning::" in proc.stdout
	report = _report(headings=HEADINGS[:-1], repos=())
	proc, _ = _run(tmp_path, report, None)
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 1, "none", report, exempt_source="unavailable")


def test_invalid_repo_names_in_scope_are_ignored(tmp_path: Path) -> None:
	report = _report(repos=(REPO_A,))
	collect = _collect(repos=(REPO_A, "::error::evil", "bad name/x", 7))
	proc, _ = _run(tmp_path, report, collect)
	assert proc.returncode == 0, proc.stdout
	assert _check_line(proc.stdout) == _expected("ok", 0, "none", report)
	assert "evil" not in proc.stdout + proc.stderr


def test_report_text_is_never_echoed(tmp_path: Path) -> None:
	report = _report(headings=HEADINGS[:-1]) + "\n::error::INJECTED-MARKER\n"
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 1
	assert "INJECTED-MARKER" not in proc.stdout + proc.stderr


def test_missing_report_file_exits_2(tmp_path: Path) -> None:
	proc, _ = _run(tmp_path, None, _collect())
	assert proc.returncode == 2
	assert "WORKFLOW_LOG_ANALYSIS_REPORT_CHECK outcome=" not in proc.stdout


def test_negative_min_bytes_is_usage_error(tmp_path: Path) -> None:
	proc, _ = _run(tmp_path, _report(), _collect(), "--min-bytes", "-1")
	assert proc.returncode == 2


def test_repo_with_no_runs_in_window_still_needs_a_subsection(tmp_path: Path) -> None:
	# Only failed repositories are exempt (plan P2); a quiet one is not.
	report = _report(repos=(REPO_A,))
	collect = _collect()
	collect["runs"] = [{"repository": REPO_A, "conclusion": "success"}]
	proc, _ = _run(tmp_path, report, collect)
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 0, REPO_B, report)


def test_host_path_repo_heading_counts(tmp_path: Path) -> None:
	report = _report(repos=(f"github.com/{REPO_A}", f"https://github.com/{REPO_B}"))
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 0, proc.stdout
	assert _check_line(proc.stdout) == _expected("ok", 0, "none", report)


def test_prefix_sharing_repo_does_not_cover_another(tmp_path: Path) -> None:
	report = _report(repos=("owner/app-two",))
	proc, _ = _run(tmp_path, report, _collect(repos=("owner/app", "owner/app-two")))
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 0, "owner/app", report)


def test_heading_inside_tilde_fence_is_not_counted(tmp_path: Path) -> None:
	headings = tuple(h for h in HEADINGS if h != "Orchestrator Health")
	report = _report(headings=headings)
	report = report.replace("## Pipeline Flow Bottlenecks\n", "~~~\n```\n## Orchestrator Health\n~~~\n\n## Pipeline Flow Bottlenecks\n", 1)
	proc, _ = _run(tmp_path, report, _collect())
	assert proc.returncode == 1
	assert _check_line(proc.stdout) == _expected("invalid", 1, "none", report)


def test_insert_data_gap_at_top_and_idempotent(tmp_path: Path) -> None:
	report = _report()
	proc, path = _run(tmp_path, report, None, "--insert-data-gap")
	assert proc.returncode == 0, proc.stderr
	assert path.read_text(encoding="utf-8") == f"{GAP_LINE}\n\n{report}"
	proc = subprocess.run(
		[sys.executable, "-I", "-B", str(SCRIPT), "--insert-data-gap", "--report", str(path)],
		capture_output=True,
		text=True,
		check=False,
	)
	assert proc.returncode == 0
	assert path.read_text(encoding="utf-8") == f"{GAP_LINE}\n\n{report}"
	assert sorted(p.name for p in tmp_path.iterdir()) == ["report.md"]


def test_insert_data_gap_under_window_marker(tmp_path: Path) -> None:
	marker = "<!-- workflow-log-analysis: window=2026-11-01..2026-11-08 -->"
	body = _report()
	proc, path = _run(tmp_path, f"{marker}\n{body}", None, "--insert-data-gap")
	assert proc.returncode == 0, proc.stderr
	assert path.read_text(encoding="utf-8") == f"{marker}\n{GAP_LINE}\n\n{body}"
	proc, path = _run(tmp_path, path.read_text(encoding="utf-8"), None, "--insert-data-gap")
	assert proc.returncode == 0
	assert path.read_text(encoding="utf-8") == f"{marker}\n{GAP_LINE}\n\n{body}"


def test_insert_data_gap_missing_report_exits_2(tmp_path: Path) -> None:
	proc, _ = _run(tmp_path, None, None, "--insert-data-gap")
	assert proc.returncode == 2


if __name__ == "__main__":
	raise SystemExit(pytest.main([__file__, "-q"]))
