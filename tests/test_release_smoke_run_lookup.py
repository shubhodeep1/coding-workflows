#!/usr/bin/env python3
"""Regression tests for the release smoke gate's run lookups.

Two release-gate failures in test-and-mark-stable.yml were harness bugs,
not pipeline defects:

- Run 36374918973 (and gate run 36504041362): capture_run_id read one page of
  100 runs from the repo-wide runs listing. The release window created more
  than 100 runs, the real Plan (Implement) run fell off that page behind
  newer `skipped` runs, and deep verify reported "run ID not found".
  find_latest_scoped_run_field now pages until it finds a match.
- Run 36389883126: Phase 7 exited lookup_failed on one HTTP 502 while the
  cancel-on-close run was already in progress. phase7_list_cancel_runs now
  retries up to 3 times.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "comprehensive_test_and_release_gh_api.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

# A fake `gh` that serves `page-<N>.json` from TEST_PAGES_DIR for
# `gh api repos/.../actions/runs?...&page=<N>&...`, fails when that file
# holds `FAIL`, and serves `default.json` (or an empty listing) otherwise.
# Every call is appended to TEST_GH_CALLS.
FAKE_GH = """#!/usr/bin/env python3
import os, re, sys
calls = os.environ["TEST_GH_CALLS"]
with open(calls, "a", encoding="utf-8") as fh:
	fh.write(" ".join(sys.argv[1:]) + "\\n")
path = next((a for a in sys.argv[2:] if not a.startswith("-")), "")
m = re.search(r"[?&]page=(\\d+)", path)
pages = os.environ["TEST_PAGES_DIR"]
name = f"page-{m.group(1)}.json" if m else "default.json"
target = os.path.join(pages, name)
if not os.path.exists(target):
	target = os.path.join(pages, "default.json")
body = open(target, encoding="utf-8").read() if os.path.exists(target) else '{"workflow_runs": []}'
if body.strip() == "FAIL":
	sys.stderr.write("gh: Server Error (HTTP 502)\\n")
	sys.exit(1)
sys.stdout.write(body)
"""

FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"


def _run(name: str, created_at: str, conclusion: str | None, run_id: int, title: str = "") -> dict:
	return {
		"id": run_id,
		"name": name,
		"display_title": title or name,
		"created_at": created_at,
		"status": "completed" if conclusion else "in_progress",
		"conclusion": conclusion,
	}


def _filler(count: int, start_id: int) -> list[dict]:
	return [_run("Internal: CI", f"2026-09-28T03:5{i % 10}:00Z", "success", start_id + i) for i in range(count)]


def _page(call: str) -> int:
	match = re.search(r"[?&]page=(\d+)", call)
	return int(match.group(1)) if match else 0


def _write_executable(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(0o755)


def _invoke(pages: dict[str, object], shell_body: str, source_helper: bool = True) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	with tempfile.TemporaryDirectory() as tempdir_name:
		tempdir = Path(tempdir_name)
		bin_dir = tempdir / "bin"
		pages_dir = tempdir / "pages"
		bin_dir.mkdir()
		pages_dir.mkdir()
		calls_file = tempdir / "calls.txt"
		_write_executable(bin_dir / "gh", FAKE_GH)
		_write_executable(bin_dir / "sleep", FAKE_SLEEP)
		for name, body in pages.items():
			text = body if isinstance(body, str) else json.dumps(body)
			(pages_dir / name).write_text(text, encoding="utf-8")
		env = os.environ.copy()
		env.update(
			{
				"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
				"TEST_GH_CALLS": str(calls_file),
				"TEST_PAGES_DIR": str(pages_dir),
				"TEST_HELPER": str(HELPER),
			}
		)
		prefix = 'source "${TEST_HELPER}"\n' if source_helper else ""
		result = subprocess.run(
			["bash", "-c", prefix + textwrap.dedent(shell_body)],
			capture_output=True,
			check=False,
			cwd=REPO_ROOT,
			env=env,
			text=True,
		)
		calls = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []
		return result, calls


LOOKUP = 'find_latest_scoped_run_field "owner/repo" "2026-09-28T03:46:08Z" "Plan" "{title}" "id"'


def test_match_on_first_page_costs_one_read() -> None:
	page1 = {"workflow_runs": _filler(99, 1000) + [_run("Internal: AI Plan", "2026-09-28T03:50:56Z", "success", 42, "Smoke")]}
	result, calls = _invoke({"page-1.json": page1}, LOOKUP.format(title="Smoke"))
	assert result.returncode == 0, result.stderr
	assert result.stdout == "42"
	assert len(calls) == 1
	assert _page(calls[0]) == 1 and "per_page=100" in calls[0] and "created=>2026-09-28T03:46:08Z" in calls[0]


def test_real_run_behind_skipped_runs_is_found_on_page_two() -> None:
	# The run 36374918973 shape: page 1 is full and its only Plan runs for the
	# smoke issue are `skipped` bot-comment triggers; the real run is on page 2.
	page1 = {
		"workflow_runs": [
			_run("Internal: AI Plan", "2026-09-28T03:56:40Z", "skipped", 36375676994, "Smoke"),
			_run("Internal: AI Plan", "2026-09-28T03:56:39Z", "skipped", 36375676099, "Smoke"),
		]
		+ _filler(98, 2000)
	}
	page2 = {"workflow_runs": [_run("Internal: AI Plan", "2026-09-28T03:50:56Z", "success", 36375309681, "Smoke")] + _filler(5, 3000)}
	result, calls = _invoke({"page-1.json": page1, "page-2.json": page2}, LOOKUP.format(title="Smoke"))
	assert result.returncode == 0, result.stderr
	assert result.stdout == "36375309681"
	assert [_page(c) for c in calls] == [1, 2]


def test_short_page_without_match_stops_after_one_read() -> None:
	page1 = {"workflow_runs": _filler(40, 1000)}
	result, calls = _invoke({"page-1.json": page1}, LOOKUP.format(title="Smoke"))
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert len(calls) == 1


def test_title_filter_ignores_other_issues_and_empty_title_matches_any() -> None:
	page1 = {"workflow_runs": [_run("Internal: AI Plan", "2026-09-28T03:55:00Z", "success", 7, "Other issue")]}
	result, _ = _invoke({"page-1.json": page1}, LOOKUP.format(title="Smoke"))
	assert result.stdout == ""
	result, _ = _invoke({"page-1.json": page1}, LOOKUP.format(title=""))
	assert result.stdout == "7"


def test_newer_run_for_another_issue_is_skipped() -> None:
	# The run 36374918973 shape: the harness recorded Clarify run 36375238437,
	# titled "Set second E2E smoke canary for run 36374918973", for its own
	# smoke issue. With the title passed, only the smoke issue's run matches.
	page1 = {
		"workflow_runs": [
			_run("Internal: AI Clarify", "2026-09-28T03:50:00Z", "success", 36375238437, "Set second E2E smoke canary for run 36374918973"),
			_run("Internal: AI Clarify", "2026-09-28T03:46:30Z", "success", 36374999999, "[E2E Smoke Test] Update smoke-test canary (run 36374918973)"),
		]
	}
	lookup = 'find_latest_scoped_run_field "owner/repo" "2026-09-28T03:46:08Z" "Clarif" "[E2E Smoke Test] Update smoke-test canary (run 36374918973)" "id"'
	result, calls = _invoke({"page-1.json": page1}, lookup)
	assert result.returncode == 0, result.stderr
	assert result.stdout == "36374999999"
	assert len(calls) == 1


def test_scan_is_capped_at_five_pages() -> None:
	result, calls = _invoke({"default.json": {"workflow_runs": _filler(100, 1000)}}, LOOKUP.format(title="Smoke"))
	assert result.returncode == 0, result.stderr
	assert result.stdout == ""
	assert len(calls) == 5
	assert [_page(c) for c in calls] == [1, 2, 3, 4, 5]


def test_read_failure_returns_one_and_prints_nothing() -> None:
	# The FAIL page answers HTTP 502, a transient error, so gh_api_safe uses
	# its whole 4-attempt budget on this read (the fake sleep is instant)
	# before the lookup reports the failure.
	result, calls = _invoke({"page-1.json": "FAIL"}, LOOKUP.format(title="Smoke"))
	assert result.returncode == 1
	assert result.stdout == ""
	assert len(calls) == 4
	assert all(_page(c) == 1 for c in calls)
	assert result.stderr.count("GH_API_SAFE_RETRY attempt=") == 3


def test_explicit_page_cap_returns_two_when_every_page_is_full() -> None:
	# The release gate's captures pass a cap and skip retrying a capped walk.
	lookup = LOOKUP.format(title="Smoke") + ' "10"'
	result, calls = _invoke({"default.json": {"workflow_runs": _filler(100, 1000)}}, lookup + '\necho "rc=$?"')
	assert result.stdout == "rc=2\n", result.stdout + result.stderr
	assert [_page(c) for c in calls] == list(range(1, 11))


def test_page_that_is_not_a_runs_listing_returns_one() -> None:
	result, calls = _invoke({"page-1.json": {"workflow_runs": _filler(100, 1000)}, "page-2.json": {"workflow_runs": None}}, LOOKUP.format(title="Smoke"))
	assert result.returncode == 1
	assert result.stdout == ""
	assert [_page(c) for c in calls] == [1, 2]


def _gh_stub(responses: list[tuple[str, str]]) -> str:
	# Shell-function `gh` (and `sleep`) defined after the helper is sourced.
	# Call N answers responses[N-1] (the last one repeats): ("err", msg)
	# writes msg to stderr and fails, ("ok", body) prints body.
	lines = [
		"gh() {",
		'\techo "gh $*" >> "${TEST_GH_CALLS}"',
		'\tlocal n',
		'\tn=$(grep -c "^gh " "${TEST_GH_CALLS}")',
		'\tcase "${n}" in',
	]
	for index, (kind, text) in enumerate(responses, start=1):
		label = "*" if index == len(responses) else str(index)
		if kind == "err":
			action = f"printf '%s\\n' {shlex.quote(text)} >&2; return 1"
		else:
			action = f"printf '%s' {shlex.quote(text)}; return 0"
		lines.append(f"\t\t{label}) {action} ;;")
	lines += [
		"\tesac",
		"}",
		'sleep() { echo "sleep $1" >> "${TEST_GH_CALLS}"; }',
	]
	return "\n".join(lines) + "\n"


def _safe_invoke(responses: list[tuple[str, str]], call: str) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
	result, calls = _invoke({}, _gh_stub(responses) + call)
	gh_calls = [c for c in calls if c.startswith("gh ")]
	sleeps = [c.split(" ", 1)[1] for c in calls if c.startswith("sleep ")]
	return result, gh_calls, sleeps


HTTP_502 = ("err", "gh: Server Error (HTTP 502)")


def test_gh_api_safe_retries_transient_502_on_get_and_returns_output() -> None:
	call = 'set -u\nRATE_LIMIT_BACKOFF=0\nif gh_api_safe repos/o/r/actions/runs/1; then printf "%s" "${GH_API_SAFE_OUTPUT}"; fi\necho " backoff=${RATE_LIMIT_BACKOFF}"\n'
	result, gh_calls, sleeps = _safe_invoke([HTTP_502, ("ok", '{"ok":1}')], call)
	assert result.returncode == 0, result.stderr
	assert result.stdout == '{"ok":1} backoff=0\n', result.stdout
	assert len(gh_calls) == 2
	assert sleeps == ["5"]
	assert result.stderr.count("GH_API_SAFE_RETRY attempt=1/4 class=transient") == 1
	assert result.stderr.count("GH_API_SAFE_RETRY") == 1


def test_gh_api_safe_print_returns_output_after_transient_retry() -> None:
	result, gh_calls, sleeps = _safe_invoke([HTTP_502, ("ok", '{"ok":1}')], "gh_api_safe_print repos/o/r/actions/runs/1\n")
	assert result.returncode == 0, result.stderr
	assert result.stdout == '{"ok":1}'
	assert len(gh_calls) == 2 and sleeps == ["5"]


def test_gh_api_safe_does_not_retry_transient_on_mutating_calls() -> None:
	for args in (
		"-X POST repos/o/r/dispatches",
		"--method=PATCH repos/o/r/issues/1",
		"--method patch repos/o/r/issues/1",
		"-XPOST repos/o/r/dispatches",
		"repos/o/r/issues/1/comments -f body=x",
		"-F a=1 repos/o/r/dispatches",
		"-fbody=x repos/o/r/issues/1/comments",
		"--raw-field body=x repos/o/r/issues/1/comments",
		"--field=a=1 repos/o/r/dispatches",
		"--input file.json repos/o/r/dispatches",
		"-X GET -f per_page=1 repos/o/r/actions/runs",
	):
		result, gh_calls, sleeps = _safe_invoke([HTTP_502, ("ok", "{}")], f"gh_api_safe {args}\necho rc=$?\n")
		assert "rc=1" in result.stdout, (args, result.stdout, result.stderr)
		assert len(gh_calls) == 1, (args, gh_calls)
		assert sleeps == [], (args, sleeps)
		assert "GH_API_SAFE_RETRY" not in result.stderr, args


def test_gh_api_safe_get_method_and_value_flags_still_count_as_reads() -> None:
	for args in (
		"-X GET repos/o/r/actions/runs",
		"--method=get repos/o/r/actions/runs",
		"repos/o/r/actions/runs --jq .total_count",
		"-H Accept:application/json -q '.[] | .id' repos/o/r/pulls",
		"--paginate repos/o/r/issues",
	):
		result, gh_calls, sleeps = _safe_invoke([HTTP_502, ("ok", "{}")], f"gh_api_safe {args}\necho rc=$?\n")
		assert "rc=0" in result.stdout, (args, result.stdout, result.stderr)
		assert len(gh_calls) == 2 and sleeps == ["5"], (args, gh_calls, sleeps)


def test_gh_api_safe_does_not_retry_404() -> None:
	result, gh_calls, sleeps = _safe_invoke([("err", "gh: Not Found (HTTP 404)"), ("ok", "{}")], "gh_api_safe repos/o/r/issues/1\necho rc=$?\n")
	assert "rc=1" in result.stdout, result.stdout
	assert len(gh_calls) == 1 and sleeps == []
	assert "::error::gh api call failed: repos/o/r/issues/1" in result.stdout
	assert "Not Found (HTTP 404)" in result.stderr
	assert "GH_API_SAFE_RETRY" not in result.stderr


def test_gh_api_safe_rate_limit_path_unchanged() -> None:
	for args in ("repos/o/r/actions/runs", "-X POST repos/o/r/dispatches"):
		result, gh_calls, sleeps = _safe_invoke([("err", "API rate limit exceeded for user"), ("ok", "{}")], f"gh_api_safe {args}\necho rc=$?\n")
		assert "rc=0" in result.stdout, (args, result.stdout, result.stderr)
		assert len(gh_calls) == 2 and sleeps == ["30"], (args, gh_calls, sleeps)
		assert "GH_API_SAFE_RETRY" not in result.stderr, args


def test_gh_api_safe_transient_exhaustion_uses_backoff_and_reports() -> None:
	result, gh_calls, sleeps = _safe_invoke([HTTP_502], "gh_api_safe repos/o/r/actions/runs\necho rc=$?\n")
	assert "rc=1" in result.stdout, result.stdout
	assert len(gh_calls) == 4
	assert sleeps == ["5", "10", "20"]
	for attempt in (1, 2, 3):
		assert f"GH_API_SAFE_RETRY attempt={attempt}/4 class=transient" in result.stderr
	assert "attempt=4/4" not in result.stderr
	assert result.stdout.count("::error::gh api call failed") == 1


def test_gh_api_safe_transient_patterns() -> None:
	for message in (
		"read tcp 1.2.3.4:443: connection reset by peer",
		"read tcp 1.2.3.4:443: i/o timeout",
		"net/http: TLS handshake timeout",
		"unexpected EOF",
		"Post https://api.github.com/graphql: EOF",
		"gh: HTTP 503",
		"HTTP 504: Gateway Timeout",
		"gh: Server Error (HTTP 500)",
		"net/http: timeout awaiting response headers",
	):
		result, gh_calls, sleeps = _safe_invoke([("err", message), ("ok", "{}")], "gh_api_safe repos/o/r/actions/runs\necho rc=$?\n")
		assert "rc=0" in result.stdout, (message, result.stdout, result.stderr)
		assert len(gh_calls) == 2 and sleeps == ["5"], (message, gh_calls, sleeps)
	for message in (
		"geoffrey failed",
		"boom failure",
		"run 15020 not found (HTTP 404)",
		"No commit found for SHA: abc502def0123456789 (HTTP 404)",
		"gh: Not Found (HTTP 404) sha=ab503_x",
	):
		result, gh_calls, sleeps = _safe_invoke([("err", message), ("ok", "{}")], "gh_api_safe repos/o/r/actions/runs\necho rc=$?\n")
		assert "rc=1" in result.stdout, (message, result.stdout)
		assert len(gh_calls) == 1 and sleeps == [], (message, gh_calls, sleeps)


def test_gh_api_safe_quiet_transient_exhaustion() -> None:
	result, gh_calls, sleeps = _safe_invoke([HTTP_502], "gh_api_safe_quiet repos/o/r/actions/runs\necho rc=$? >&2\n")
	assert result.stdout == ""
	assert len(gh_calls) == 4 and sleeps == ["5", "10", "20"]
	assert result.stderr.splitlines() == [
		"GH_API_SAFE_RETRY attempt=1/4 class=transient",
		"GH_API_SAFE_RETRY attempt=2/4 class=transient",
		"GH_API_SAFE_RETRY attempt=3/4 class=transient",
		"rc=1",
	], result.stderr


def _workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def test_capture_sites_use_the_paged_lookup() -> None:
	wf = _workflow()
	assert wf.count('find_latest_scoped_run_field "${TEST_REPO}"') == 3
	# Clarify and Implement scope by the smoke issue's title, like Plan does.
	assert wf.count('"${name_re}" "${ISSUE_TITLE:-}" "id"') == 2
	for step_id in ("wait-clarify", "wait-implement"):
		step = re.search(rf"id: {step_id}\n        env:\n(.*?)\n        run: \|", wf, re.S)
		assert step is not None, step_id
		assert "ISSUE_TITLE: ${{ steps.create-issue.outputs.title }}" in step.group(1), step_id
	for body in re.findall(r"capture_run_id\(\) \{.*?\n          \}", wf, re.S):
		assert "actions/runs?per_page=100" not in body
	plan_field = re.search(r"latest_scoped_run_field\(\) \{.*?\n          \}", wf, re.S)
	assert plan_field is not None
	assert 'find_latest_scoped_run_field "${TEST_REPO}"' in plan_field.group(0)
	assert '"$ISSUE_TITLE"' in plan_field.group(0)


def test_wait_steps_fail_fast_on_an_empty_issue_title() -> None:
	# An empty ISSUE_TITLE switches the lookup's title filter off, which is
	# the unscoped lookup of run 36374918973. Every step that scopes by title
	# must stop before its first lookup instead. wait-clarify and
	# wait-implement report the released `run_id_missing` (AD-9 of
	# docs/completed/issue-4723-require-plan-run-id-plan.md).
	wf = _workflow()
	expected = {"wait-clarify": "run_id_missing", "wait-plan": "plan_failed", "wait-implement": "run_id_missing"}
	for step_id, status in expected.items():
		step = re.search(rf"id: {step_id}\n.*?        run: \|\n(.*?)\n      - ", wf, re.S)
		assert step is not None, step_id
		body = step.group(1)
		guard = re.search(r"\n(          if \[ -z \"\$\{ISSUE_TITLE\}\" \]; then\n.*?\n          fi)\n", body, re.S)
		assert guard is not None, step_id
		assert guard.start() < body.index(". ./scripts/comprehensive_test_and_release_gh_api.sh"), step_id
		with tempfile.TemporaryDirectory() as tmp:
			output = Path(tmp) / "output"
			result = subprocess.run(
				["bash", "-c", "set -euo pipefail\n" + textwrap.dedent(guard.group(1)) + "\necho reached"],
				capture_output=True,
				text=True,
				env={**os.environ, "ISSUE_TITLE": "", "GITHUB_OUTPUT": str(output)},
			)
			assert result.returncode == 1, step_id
			assert "reached" not in result.stdout, step_id
			assert output.read_text(encoding="utf-8") == f"status={status}\n", step_id


def _phase7_helper() -> str:
	match = re.search(r"\n(          phase7_list_cancel_runs\(\) \{.*?\n          \})\n", _workflow(), re.S)
	assert match is not None, "phase7_list_cancel_runs() not found in test-and-mark-stable.yml"
	return textwrap.dedent(match.group(1))


def test_phase7_listing_survives_two_transient_failures() -> None:
	body = _phase7_helper() + textwrap.dedent(
		"""
		TEST_REPO=owner/repo
		gh() {
			echo "gh $*" >> "${TEST_GH_CALLS}"
			if [ "$(cat "${TEST_GH_CALLS}" | wc -l)" -le 2 ]; then
				echo "gh: Server Error (HTTP 502)" >&2
				return 1
			fi
			printf '{"workflow_runs":[{"id":99}]}'
		}
		sleep() { :; }
		phase7_list_cancel_runs 10
		echo "rc=$? json=${PHASE7_CANCEL_RUNS_JSON}"
		"""
	)
	result, calls = _invoke({}, body, source_helper=False)
	assert result.returncode == 0, result.stderr
	assert 'rc=0 json={"workflow_runs":[{"id":99}]}' in result.stdout
	assert len(calls) == 3
	assert all("internal-cancel-on-pr-close.yml/runs?per_page=10" in c for c in calls)
	assert result.stdout.count("::warning::") == 2


def test_phase7_listing_fails_after_three_consecutive_failures() -> None:
	body = _phase7_helper() + textwrap.dedent(
		"""
		TEST_REPO=owner/repo
		gh() {
			echo "gh $*" >> "${TEST_GH_CALLS}"
			echo "gh: Server Error (HTTP 502)" >&2
			return 1
		}
		sleep() { :; }
		if phase7_list_cancel_runs 1; then echo "rc=0"; else echo "rc=1"; fi
		"""
	)
	result, calls = _invoke({}, body, source_helper=False)
	assert "rc=1" in result.stdout, result.stdout + result.stderr
	assert len(calls) == 3


def test_phase7_run_listings_all_go_through_the_retry_helper() -> None:
	wf = _workflow()
	assert wf.count('"repos/${TEST_REPO}/actions/workflows/internal-cancel-on-pr-close.yml/runs?per_page=') == 1
	for per_page in ("50", "1", "10"):
		assert f"if ! phase7_list_cancel_runs {per_page}; then" in wf
	assert wf.count('echo "status=lookup_failed" >> "$GITHUB_OUTPUT"') >= 3


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
