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
	result, calls = _invoke({"page-1.json": "FAIL"}, LOOKUP.format(title="Smoke"))
	assert result.returncode == 1
	assert result.stdout == ""
	assert len(calls) == 1


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
	# must stop before its first lookup instead.
	wf = _workflow()
	expected = {"wait-clarify": "clarify_failed", "wait-plan": "plan_failed", "wait-implement": "implement_failed"}
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
