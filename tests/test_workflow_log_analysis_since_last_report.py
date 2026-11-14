#!/usr/bin/env python3
"""Contract tests: the since_last_report collection window (plan P4).

workflow-log-analysis.yml:
- `Collect workflow logs` (id `collect`) gains the `since_last_report` dispatch
  input. When set, it starts the window at the largest `window_end` found in
  the first-line marker of an analysis doc added in the last 8 days of history
  (read with git only), clamped to 7 days, and falls back to lookback_days when
  no usable marker exists. It logs one `WORKFLOW_LOG_ANALYSIS_WINDOW` line and
  writes `window_start` / `window_end` step outputs.
- `Run workflow log analysis` (id `analyze`) writes the marker as the report's
  first line, before the data-gap line.

test-and-mark-stable.yml's gate dispatch uses since_last_report for this
repository, behind WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS (default true).

The steps' real `run:` bodies run with bash in a sandbox.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WF_PATH = REPO_ROOT / ".github" / "workflows" / "workflow-log-analysis.yml"
GATE_PATH = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
CHECKER = REPO_ROOT / "scripts" / "check_workflow_analysis_report.py"
ISO_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
MARKER_BEGIN = "# --- window marker (plan P4) begin ---"
MARKER_END = "# --- window marker (plan P4) end ---"

FAKE_COLLECTOR = """import json, os, sys
with open(os.environ["FAKE_COLLECT_ARGV"], "w", encoding="utf-8") as handle:
    json.dump(sys.argv[1:], handle)
"""


def _step_text(path: Path, name: str) -> str:
	text = path.read_text(encoding="utf-8")
	return text.split(f"      - name: {name}\n", 1)[1].split("\n      - name: ", 1)[0]


def _run_body(step: str) -> str:
	body = step.split("        run: |\n", 1)[1]
	lines = []
	for line in body.splitlines():
		lines.append(line[10:] if line.startswith("          ") else line.strip())
	return "\n".join(lines) + "\n"


def _iso(moment: dt.datetime) -> str:
	return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> dt.datetime:
	return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _marker(end: str, start: str = "unknown", run: str = "123") -> str:
	return f"<!-- workflow-log-analysis: window_start={start} window_end={end} run={run} -->"


def _clean_env() -> dict[str, str]:
	return {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "GITHUB_"))}


def _git(cwd: Path, *args: str, env: dict[str, str]) -> str:
	proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
	return proc.stdout.strip()


class _Sandbox:
	def __init__(self, tmp: Path, docs: list[tuple[str, str | None]]) -> None:
		"""docs: (path, content) in commit order; content None deletes the path."""
		self.tmp = tmp
		gitconfig = tmp / "gitconfig"
		gitconfig.write_text("[user]\n\tname = t\n\temail = t@example.invalid\n", encoding="utf-8")
		self.env = _clean_env()
		self.env.update({"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
		seed = tmp / "seed"
		seed.mkdir()
		_git(seed, "init", "-q", "-b", "main", env=self.env)
		(seed / "scripts").mkdir()
		(seed / "scripts" / "collect_workflow_logs.py").write_text(FAKE_COLLECTOR, encoding="utf-8")
		(seed / "analysis").mkdir()
		(seed / "analysis" / ".keep").write_text("", encoding="utf-8")
		_git(seed, "add", "-A", env=self.env)
		_git(seed, "commit", "-q", "-m", "base", env=self.env)
		for path, content in docs:
			if content is None:
				_git(seed, "rm", "-q", "--", path, env=self.env)
			else:
				(seed / path).write_text(content, encoding="utf-8")
				_git(seed, "add", "-A", env=self.env)
			_git(seed, "commit", "-q", "-m", f"doc {path}", env=self.env)
		self.origin = tmp / "origin.git"
		_git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin), env=self.env)
		_git(seed, "push", "-q", self.origin.as_uri(), "HEAD:refs/heads/main", env=self.env)
		self.head = _git(seed, "rev-parse", "HEAD", env=self.env)
		# A depth-1 clone like actions/checkout, so history needs the fetch.
		self.work = tmp / "work"
		_git(tmp, "clone", "-q", "--depth", "1", self.origin.as_uri(), str(self.work), env=self.env)
		self.repos = tmp / "repos.txt"
		self.repos.write_text("o/r\n", encoding="utf-8")
		self.output = tmp / "github_output"
		self.output.write_text("", encoding="utf-8")
		self.argv = tmp / "argv.json"
		self.runner_temp = tmp / "runner_temp"
		self.runner_temp.mkdir()

	def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
		body = _run_body(_step_text(WF_PATH, "Collect workflow logs"))
		assert body.count("/tmp/workflow-analysis-repos.txt") == 1
		body = body.replace("/tmp/workflow-analysis-repos.txt", str(self.repos))
		env = dict(self.env)
		env.update(
			{
				"GITHUB_OUTPUT": str(self.output),
				"GITHUB_SHA": self.head,
				"RUNNER_TEMP": str(self.runner_temp),
				"SINCE_INPUT": "",
				"LOOKBACK_DAYS_INPUT": "1",
				"SINCE_LAST_REPORT_INPUT": "true",
				"PAT_BUDGET_DAILY": "false",
				"FAKE_COLLECT_ARGV": str(self.argv),
				"PYTHONDONTWRITEBYTECODE": "1",
			}
		)
		env.update(extra)
		return subprocess.run(["bash", "-c", body], cwd=self.work, env=env, capture_output=True, text=True, check=False, timeout=120)

	def collect_argv(self) -> list[str]:
		return json.loads(self.argv.read_text(encoding="utf-8"))

	def outputs(self) -> dict[str, str]:
		result: dict[str, str] = {}
		for line in self.output.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			result[key] = value
		return result


def _window_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
	return [line for line in proc.stdout.splitlines() if line.startswith("WORKFLOW_LOG_ANALYSIS_WINDOW ")]


def _since(argv: list[str]) -> str:
	assert "--since" in argv, argv
	return argv[argv.index("--since") + 1]


def _run_case(docs: list[tuple[str, str | None]], **extra: str):
	raw = tempfile.TemporaryDirectory()
	box = _Sandbox(Path(raw.name), docs)
	proc = box.run(**extra)
	return raw, box, proc


DOC_A = "analysis/workflow-optimization-2026-11-01.md"
DOC_B = "analysis/workflow-optimization-2026-11-02-2.md"


def test_since_last_report_uses_a_recent_marker() -> None:
	end = _iso(_now() - dt.timedelta(hours=3))
	older = _iso(_now() - dt.timedelta(hours=30))
	raw, box, proc = _run_case([(DOC_A, _marker(older) + "\n# a\n"), (DOC_B, _marker(end, older, "456") + "\n# b\n")])
	with raw:
		assert proc.returncode == 0, proc.stderr + proc.stdout
		argv = box.collect_argv()
		assert _since(argv) == end
		assert "--lookback-days" not in argv
		assert _window_lines(proc) == [
			f"WORKFLOW_LOG_ANALYSIS_WINDOW source=last_report since={end} report={DOC_B} clamped=false reason=ok"
		]
		out = box.outputs()
		assert out["window_start"] == end
		assert ISO_RE.match(out["window_end"])
		assert out["window_end"] > end


def test_since_last_report_without_marker_falls_back_to_lookback() -> None:
	raw, box, proc = _run_case([(DOC_A, "# report without a marker\n")])
	with raw:
		assert proc.returncode == 0, proc.stderr + proc.stdout
		argv = box.collect_argv()
		assert argv[argv.index("--lookback-days") + 1] == "1"
		assert "--since" not in argv
		lines = _window_lines(proc)
		assert len(lines) == 1
		assert re.fullmatch(
			r"WORKFLOW_LOG_ANALYSIS_WINDOW source=lookback since=\S+Z report=none clamped=false reason=no_marker", lines[0]
		), lines
		assert ISO_RE.match(box.outputs()["window_start"])


def test_since_last_report_future_marker_falls_back() -> None:
	future = _iso(_now() + dt.timedelta(days=1))
	raw, box, proc = _run_case([(DOC_A, _marker(future) + "\n")])
	with raw:
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert "--lookback-days" in box.collect_argv()
		lines = _window_lines(proc)
		assert len(lines) == 1 and lines[0].endswith(f"report={DOC_A} clamped=false reason=future"), lines
		assert "source=lookback" in lines[0]


def test_since_last_report_unparseable_marker_falls_back() -> None:
	raw, box, proc = _run_case([(DOC_A, _marker("2026-13-45T99:99:99Z") + "\n")])
	with raw:
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert "--lookback-days" in box.collect_argv()
		lines = _window_lines(proc)
		assert len(lines) == 1 and lines[0].endswith("clamped=false reason=unparseable"), lines


def test_since_last_report_old_marker_is_clamped_to_seven_days() -> None:
	old = _iso(_now() - dt.timedelta(days=10))
	raw, box, proc = _run_case([(DOC_A, _marker(old) + "\n")])
	with raw:
		assert proc.returncode == 0, proc.stderr + proc.stdout
		since = _since(box.collect_argv())
		since_at = dt.datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
		assert abs((_now() - since_at) - dt.timedelta(days=7)) < dt.timedelta(minutes=5)
		assert _window_lines(proc) == [
			f"WORKFLOW_LOG_ANALYSIS_WINDOW source=last_report since={since} report={DOC_A} clamped=true reason=ok"
		]
		assert box.outputs()["window_start"] == since


def test_since_last_report_reads_a_doc_deleted_on_the_tip() -> None:
	end = _iso(_now() - dt.timedelta(hours=5))
	raw, box, proc = _run_case([(DOC_A, _marker(end) + "\n# a\n"), (DOC_A, None)])
	with raw:
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert not (box.work / DOC_A).exists()
		assert _since(box.collect_argv()) == end
		assert _window_lines(proc)[0].endswith(f"report={DOC_A} clamped=false reason=ok")


def test_since_last_report_ignores_markers_not_on_the_first_line_or_off_pattern() -> None:
	end = _iso(_now() - dt.timedelta(hours=2))
	raw, box, proc = _run_case(
		[
			(DOC_A, "# title\n" + _marker(end) + "\n"),
			("analysis/other-notes.md", _marker(end) + "\n"),
			(DOC_B, _marker(end) + " trailing\n"),
		]
	)
	with raw:
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert "--lookback-days" in box.collect_argv()
		assert _window_lines(proc)[0].endswith("report=none clamped=false reason=no_marker")


def test_explicit_since_wins_over_since_last_report() -> None:
	end = _iso(_now() - dt.timedelta(hours=3))
	raw, box, proc = _run_case([(DOC_A, _marker(end) + "\n")], SINCE_INPUT="2026-01-02T03:04:05Z")
	with raw:
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert _since(box.collect_argv()) == "2026-01-02T03:04:05Z"
		assert _window_lines(proc) == []
		assert box.outputs()["window_start"] == "2026-01-02T03:04:05Z"


def test_unparseable_explicit_since_records_unknown_start() -> None:
	raw, box, proc = _run_case([], SINCE_INPUT="not a date")
	with raw:
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert box.outputs()["window_start"] == "unknown"


def test_fetch_failure_reports_git_failed() -> None:
	end = _iso(_now() - dt.timedelta(hours=3))
	raw, box, proc = _run_case([(DOC_A, _marker(end) + "\n")], GITHUB_SHA="0" * 40)
	with raw:
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert "--lookback-days" in box.collect_argv()
		lines = _window_lines(proc)
		assert len(lines) == 1 and lines[0].endswith("report=none clamped=false reason=git_failed"), lines


def test_input_off_keeps_the_lookback_window_and_logs_nothing() -> None:
	end = _iso(_now() - dt.timedelta(hours=3))
	for value in ("false", "", "0", "maybe"):
		raw, box, proc = _run_case([(DOC_A, _marker(end) + "\n")], SINCE_LAST_REPORT_INPUT=value)
		with raw:
			assert proc.returncode == 0, proc.stderr + proc.stdout
			argv = box.collect_argv()
			assert argv[argv.index("--lookback-days") + 1] == "1", value
			assert _window_lines(proc) == []
			out = box.outputs()
			assert ISO_RE.match(out["window_start"]) and ISO_RE.match(out["window_end"])


def test_invalid_lookback_still_fails_after_a_fallback() -> None:
	raw, box, proc = _run_case([], LOOKBACK_DAYS_INPUT="$(touch x)")
	with raw:
		assert proc.returncode != 0
		assert "lookback_days must be a positive integer." in proc.stdout
		assert not (box.work / "x").exists()
		# window_end is written before anything can fail.
		assert ISO_RE.match(box.outputs()["window_end"])


def _marker_snippet() -> str:
	body = _run_body(_step_text(WF_PATH, "Run workflow log analysis"))
	start = body.index(MARKER_BEGIN)
	return body[start:body.index(MARKER_END, start) + len(MARKER_END)] + "\n"


def _run_marker(report: Path, **extra: str) -> subprocess.CompletedProcess[str]:
	env = _clean_env()
	env.update({"REPORT_FILE": str(report), "GITHUB_RUN_ID": "987"})
	env.update(extra)
	return subprocess.run(["bash", "-c", "set -euo pipefail\n" + _marker_snippet()], env=env, capture_output=True, text=True, check=False)


def test_analyze_step_writes_the_marker_once_and_it_round_trips() -> None:
	end = _iso(_now() - dt.timedelta(hours=1))
	with tempfile.TemporaryDirectory() as raw:
		report = Path(raw) / "workflow-optimization-2026-11-09.md"
		report.write_text("# Workflow optimization\n\nbody\n", encoding="utf-8")
		for _ in range(2):
			proc = _run_marker(report, WLA_WINDOW_START="unknown", WLA_WINDOW_END=end)
			assert proc.returncode == 0, proc.stderr + proc.stdout
		text = report.read_text(encoding="utf-8")
		assert text == _marker(end, "unknown", "987") + "\n# Workflow optimization\n\nbody\n"
		assert sorted(p.name for p in Path(raw).iterdir()) == [report.name]
		# The collect step's marker regex accepts what the analyze step writes.
		collect = _run_body(_step_text(WF_PATH, "Collect workflow logs"))
		marker_re = re.search(r"wla_marker_re='([^']+)'", collect)
		assert marker_re and re.fullmatch(marker_re.group(1), text.splitlines()[0])
		# The checker keeps the data-gap line under the marker.
		gap = subprocess.run(
			["python3", "-I", "-B", str(CHECKER), "--insert-data-gap", "--report", str(report)],
			capture_output=True,
			text=True,
			check=False,
		)
		assert gap.returncode == 0, gap.stderr
		lines = report.read_text(encoding="utf-8").splitlines()
		assert lines[0] == _marker(end, "unknown", "987")
		assert "> Data gap: full workflow logs were unavailable for this run." in lines[1:]


def test_analyze_step_skips_an_invalid_window_with_a_warning() -> None:
	for start, end, run in (
		("unknown", "", "1"),
		("unknown", "2026-11-09 00:00", "1"),
		("x -->", "2026-11-09T00:00:00Z", "1"),
		("unknown", "2026-11-09T00:00:00Z", "1;id"),
	):
		with tempfile.TemporaryDirectory() as raw:
			report = Path(raw) / "r.md"
			report.write_text("# r\n", encoding="utf-8")
			proc = _run_marker(report, WLA_WINDOW_START=start, WLA_WINDOW_END=end, GITHUB_RUN_ID=run)
			assert proc.returncode == 0, proc.stderr
			assert "::warning::" in proc.stdout
			assert report.read_text(encoding="utf-8") == "# r\n"


def test_analyze_step_skips_the_marker_for_a_repos_override_run() -> None:
	end = _iso(_now() - dt.timedelta(hours=1))
	for override, marked in (("o/r", False), (" o/a, o/b ", False), ("  ", True), ("", True)):
		with tempfile.TemporaryDirectory() as raw:
			report = Path(raw) / "r.md"
			report.write_text("# r\n", encoding="utf-8")
			proc = _run_marker(report, WLA_WINDOW_START="unknown", WLA_WINDOW_END=end, WLA_REPOS_OVERRIDE=override)
			assert proc.returncode == 0, proc.stderr
			text = report.read_text(encoding="utf-8")
			if marked:
				assert text == _marker(end, "unknown", "987") + "\n# r\n", override
			else:
				# A single-repo window must not become a full-roster start.
				assert text == "# r\n", override
				assert "::warning::repos_override limited this run" in proc.stdout


def test_marker_and_window_static_contract() -> None:
	wf = WF_PATH.read_text(encoding="utf-8")
	inputs = wf.split("    inputs:\n", 1)[1].split("\n  schedule:\n", 1)[0]
	block = inputs.split("      since_last_report:\n", 1)[1].split("\n      codex_mode:", 1)[0]
	assert '        default: "false"\n' in block and "        type: string" in block
	collect_step = _step_text(WF_PATH, "Collect workflow logs")
	assert "SINCE_LAST_REPORT_INPUT: ${{ github.event.inputs.since_last_report || 'false' }}" in collect_step
	assert "${{" not in _run_body(collect_step)
	job = wf.split("\n  collect-logs:\n", 1)[1].split("\n    steps:\n", 1)[0] + "\n"
	assert "      window_start: ${{ steps.collect.outputs.window_start }}\n" in job
	assert "      window_end: ${{ steps.collect.outputs.window_end }}\n" in job
	analyze = _step_text(WF_PATH, "Run workflow log analysis")
	assert "WLA_WINDOW_START: ${{ needs.collect-logs.outputs.window_start }}" in analyze
	assert "WLA_WINDOW_END: ${{ needs.collect-logs.outputs.window_end }}" in analyze
	assert "WLA_REPOS_OVERRIDE: ${{ github.event.inputs.repos_override || '' }}" in analyze
	body = _run_body(analyze)
	marker_at = body.index(MARKER_BEGIN)
	gap_at = body.index('if [ "${wla_logs_missing}" = "true" ]; then\n  if ! python3 -I -B scripts/check_workflow_analysis_report.py --insert-data-gap')
	assert marker_at < gap_at < body.index('echo "analysis_status=completed" >> "$GITHUB_OUTPUT"')
	checker = CHECKER.read_text(encoding="utf-8")
	assert 'WINDOW_MARKER_RE = re.compile(r"^<!-- workflow-log-analysis:.*-->$")' in checker


def test_gate_dispatch_uses_since_last_report_for_this_repo() -> None:
	step = _step_text(GATE_PATH, "Dispatch & snapshot — workflow-log-analysis")
	assert "WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS: ${{ vars.WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS || 'true' }}" in step
	assert "wla_gate_scope_fields=(--field since_last_report=true)" in step
	assert 'wla_gate_scope_fields=(--field "repos_override=${TEST_REPO}")' in step
	assert '"${wla_gate_scope_fields[@]}" \\' in step
	assert "--field lookback_days=1 \\" in step and "--field alert_msg_level=SILENT" in step
	assert "--dispatch-fail-open" in step
	# Run the body with a stub dispatcher that records its arguments.
	body = _run_body(step).replace("sleep 4\n", "")
	cases = (
		({}, "o/r", "since_last_report=true"),
		({"WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS": "OFF"}, "o/r", "repos_override=o/r"),
		({"WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS": "false"}, "o/r", "repos_override=o/r"),
		({}, "other/repo", "repos_override=other/repo"),
	)
	for extra, test_repo, expected in cases:
		with tempfile.TemporaryDirectory() as raw:
			root = Path(raw)
			(root / "scripts").mkdir()
			stub = root / "scripts" / "dispatch_and_watch_workflow_run.sh"
			stub.write_text('printf \'%s\\n\' "$@" > "${STUB_ARGS}"\n', encoding="utf-8")
			stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
			env = _clean_env()
			env.update({"TEST_REPO": test_repo, "GITHUB_REPOSITORY": "o/r", "WF_FILE": "workflow-log-analysis.yml",
				"STUB_ARGS": str(root / "args"), "WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS": "true"})
			env.update(extra)
			proc = subprocess.run(["bash", "-c", body], cwd=root, env=env, capture_output=True, text=True, check=False)
			assert proc.returncode == 0, proc.stderr
			args = (root / "args").read_text(encoding="utf-8").splitlines()
			fields = [args[i + 1] for i, arg in enumerate(args) if arg == "--field"]
			assert fields == ["lookback_days=1", expected, "alert_msg_level=SILENT"], (extra, test_repo, fields)


def main() -> int:
	for name, func in sorted(globals().items()):
		if name.startswith("test_") and callable(func):
			func()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
