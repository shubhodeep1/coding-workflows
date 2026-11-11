#!/usr/bin/env python3
"""Contract tests: the report purge keeps analysis docs nobody has processed.

Step `Commit and push report` (id `commit_report`) of workflow-log-analysis.yml
purges `analysis/workflow-optimization-YYYY-MM-DD(-N).md` docs older than the
retention window. Plan P5 (decision D5) limits that to docs the recommendation
processing report (`APPLY_ANALYSIS_REPORT_PATH`, default
`analysis/recommendation-processing-report.md`) records:

- a doc past the window that the report does not list is kept and logged
  `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc=<path> reason=unprocessed`;
- when the report is missing or unreadable nothing is purged and the step logs
  `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED reason=processing_report_unavailable`;
- the in-flight guard (issue #7022) still runs first.

The step's real `run:` body runs in the sandbox from
tests/test_workflow_log_analysis_report_pr.py, in legacy push mode.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import tempfile
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
PROCESSING_REPORT = "analysis/recommendation-processing-report.md"
PROCESSED = "analysis/workflow-optimization-2020-01-01.md"
UNPROCESSED = "analysis/workflow-optimization-2020-01-02.md"
IN_FLIGHT = "analysis/workflow-optimization-2020-01-03.md"


def _report_pr_cases():
	spec = importlib.util.spec_from_file_location(
		"workflow_log_analysis_report_pr_cases_for_purge",
		TESTS_DIR / "test_workflow_log_analysis_report_pr.py",
	)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


CASES = _report_pr_cases()


def _recent_doc() -> str:
	now = dt.datetime.now(dt.timezone.utc)
	stamp = (now - dt.timedelta(days=2)).strftime("%Y-%m-%d")
	doc = f"analysis/workflow-optimization-{stamp}.md"
	# On 2026-11-11 "two days ago" equals CASES.REPORT: _seed() then committed
	# the "old report" at the path box.write_report() overwrites, breaking the
	# setup. Fall back to one day ago, which is still inside the window.
	if doc == CASES.REPORT:
		stamp = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
		doc = f"analysis/workflow-optimization-{stamp}.md"
	return doc


def _seed(box, *, processing_report: str | None, in_flight: str = "") -> None:
	work = box.work
	for doc in (PROCESSED, UNPROCESSED, IN_FLIGHT, _recent_doc()):
		(work / doc).write_text("# old report\n", encoding="utf-8")
	report = work / PROCESSING_REPORT
	if processing_report is None:
		report.unlink()
	else:
		report.write_text(processing_report, encoding="utf-8")
	stub = "#!/usr/bin/env bash\n"
	if in_flight:
		stub += f"printf '%s\\n' '{in_flight}'\n"
	stub += "exit 0\n"
	(work / "scripts" / "apply_analysis_on_main.sh").write_text(stub, encoding="utf-8")
	CASES._git(work, "add", "-A", env=box.git_env)
	CASES._git(work, "commit", "-q", "-m", "seed docs", env=box.git_env)
	CASES._git(work, "push", "-q", "origin", "HEAD:main", env=box.git_env)


def _skip_lines(stdout: str) -> list[str]:
	return [line for line in stdout.splitlines() if line.startswith("WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED ")]


def test_purge_removes_only_processed_docs_past_the_window() -> None:
	listing = (
		"# Recommendation processing report\n\n"
		"- workflow-optimization-2020-01-01.md: applied\n"
		# The in-flight doc is listed too: the in-flight guard alone keeps it.
		"- workflow-optimization-2020-01-03.md: applied\n"
		f"- {Path(_recent_doc()).name}: applied\n"
	)
	with tempfile.TemporaryDirectory() as raw:
		box = CASES._Sandbox(Path(raw))
		_seed(box, processing_report=listing, in_flight=IN_FLIGHT)
		box.write_report()
		proc = box.run(WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR="false")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert box.origin_has("refs/heads/main", CASES.REPORT)
		assert not box.origin_has("refs/heads/main", PROCESSED)
		assert box.origin_has("refs/heads/main", UNPROCESSED)
		assert box.origin_has("refs/heads/main", IN_FLIGHT)
		assert box.origin_has("refs/heads/main", _recent_doc())
		assert box.origin_has("refs/heads/main", PROCESSING_REPORT)
		skips = _skip_lines(proc.stdout)
		assert f"WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc={UNPROCESSED} reason=unprocessed" in skips
		assert f"WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc={IN_FLIGHT} reason=in_flight_project" in skips
		# The in-flight guard runs first, so that doc is never also unprocessed.
		assert f"WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc={IN_FLIGHT} reason=unprocessed" not in skips
		# Docs inside the retention window are never checked or logged.
		assert not any(_recent_doc() in line for line in skips), skips
		assert "reason=processing_report_unavailable" not in proc.stdout
		assert "INFO: purged 1 workflow-optimization report(s)" in proc.stdout


def test_purge_keeps_everything_when_the_processing_report_is_missing() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = CASES._Sandbox(Path(raw))
		_seed(box, processing_report=None)
		box.write_report()
		proc = box.run(WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR="false")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		for doc in (PROCESSED, UNPROCESSED, IN_FLIGHT, _recent_doc()):
			assert box.origin_has("refs/heads/main", doc), doc
		assert _skip_lines(proc.stdout) == ["WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED reason=processing_report_unavailable"]
		assert "INFO: purged" not in proc.stdout
		# The cutoff warning is not printed for this case.
		assert "Unable to compute report purge cutoff date" not in proc.stdout


def test_purge_reads_the_processing_report_from_apply_analysis_report_path() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = CASES._Sandbox(Path(raw))
		_seed(box, processing_report="# empty\n")
		(box.work / "analysis" / "custom-report.md").write_text("- workflow-optimization-2020-01-02.md\n", encoding="utf-8")
		box.write_report()
		proc = box.run(WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR="false", APPLY_ANALYSIS_REPORT_PATH="analysis/custom-report.md")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert not box.origin_has("refs/heads/main", UNPROCESSED)
		assert box.origin_has("refs/heads/main", PROCESSED)
		assert f"WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc={PROCESSED} reason=unprocessed" in _skip_lines(proc.stdout)


def test_purge_guard_order_and_identifiers_static() -> None:
	step = CASES._step_text()
	in_flight_at = step.index("APPLY_ANALYSIS_IN_FLIGHT_DOCS=true bash scripts/apply_analysis_on_main.sh")
	report_at = step.index('purge_processing_report="${APPLY_ANALYSIS_REPORT_PATH:-analysis/recommendation-processing-report.md}"')
	unavailable_at = step.index('echo "WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED reason=processing_report_unavailable"')
	cutoff_at = step.index('if [ "${purge_guard_ok}" = "true" ] && cutoff_date=')
	in_flight_skip_at = step.index('reason=in_flight_project"')
	unprocessed_at = step.index('if ! grep -qF -- "${filename}" "${purge_processing_report}"; then')
	git_rm_at = step.index('git rm -- "${old_report}"')
	assert in_flight_at < report_at < unavailable_at < cutoff_at
	assert in_flight_skip_at < unprocessed_at < git_rm_at
	# The unprocessed check sits inside the past-the-cutoff branch only.
	assert step.index('if [[ "${stamp}" < "${cutoff_date}" ]]; then') < unprocessed_at
	assert 'retention_raw="${REPORT_RETENTION_DAYS:-30}"' in step
	assert "REPORT_RETENTION_DAYS: ${{ vars.WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS || '30' }}" in CASES._workflow_text()


def main() -> int:
	test_purge_removes_only_processed_docs_past_the_window()
	test_purge_keeps_everything_when_the_processing_report_is_missing()
	test_purge_reads_the_processing_report_from_apply_analysis_report_path()
	test_purge_guard_order_and_identifiers_static()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
