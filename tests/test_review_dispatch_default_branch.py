#!/usr/bin/env python3
"""Issue #4701: the remaining review dispatches run from the default branch.

Issue #4618 moved ``review_autofix_sweep.yml`` off the PR head ref: a
``--ref <PR head branch>`` dispatch ran that branch's unmerged copy of the
review workflow with ``secrets: inherit`` and write permissions. Issue #4701
does the same for the three dispatch sites that were left:

1. ``_dispatch_review_for_conflicts`` in ``scripts/orchestrate_poll_process.sh``;
2. ``_mt_dispatch_review`` in ``scripts/review_merge_train.sh``;
3. the fallback-PR dispatch in ``forward-merge-stable-to-main.yml``
   (covered in ``test_conflict_dispatch_active_run_visibility.py``).

A default-branch run has the default branch as its head, so the poller finds
it by the name the review wrappers give a dispatched run
(``Internal: AI Review & Autofix [pr:<N>]`` / ``AI Review [pr:<N>]``). These
tests extract the production shell functions, source them with a stubbed
``gh``, and check the dispatch arguments and every PR-named lookup.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
MERGE_TRAIN_SCRIPT = REPO_ROOT / "scripts" / "review_merge_train.sh"
AI_REVIEW_TEMPLATE = REPO_ROOT / "workflow-templates" / "ai-review.yml"
INTERNAL_REVIEW_WF = REPO_ROOT / ".github" / "workflows" / "internal-review.yml"


def _iso_minutes_ago(minutes: int) -> str:
	return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_fn(script: Path, name: str) -> str:
	"""Return one shell function, from ``name()`` at column 0 to the first ``}`` at column 0."""
	lines = script.read_text(encoding="utf-8").splitlines()
	out: list[str] = []
	inside = False
	for line in lines:
		if not inside and re.match(rf"^{re.escape(name)}\(\)", line):
			inside = True
		if inside:
			out.append(line)
			if line == "}":
				break
	assert out and out[-1] == "}", f"could not extract {name} from {script}"
	return "\n".join(out) + "\n"


# gh stub: log every call (one line per call, args joined by a tab) to
# $GH_LOG, then answer:
#   gh workflow run ...        -> exit $GH_WORKFLOW_RUN_RC (default 0)
#   gh run list ... --branch   -> $GH_BRANCH_LIST (applied through --jq when given)
#   gh api repos/<repo> --jq . -> $GH_DEFAULT_BRANCH (default main), or exit 1
#                                 when GH_DEFAULT_BRANCH_FAIL=1
#   gh api .../actions/workflows/<wrapper>/runs?...&per_page=P&page=N
#                              -> page N of $GH_WRAPPER_RUNS[<wrapper>] in REST shape
#                                 (see _API_STUB), applied through --jq
_STUBS = r"""
gh_retry() { "$@"; }
_safe_gh_jq() { gh api "$@"; }
gh() {
	local IFS=$'\t'
	printf '%s\n' "$*" >> "${GH_LOG}"
	if [ "${1:-}" = "workflow" ] && [ "${2:-}" = "run" ]; then
		return "${GH_WORKFLOW_RUN_RC:-0}"
	fi
	if [ "${1:-}" = "api" ] && [ "${2:-}" = "repos/${GITHUB_REPOSITORY}" ]; then
		[ "${GH_DEFAULT_BRANCH_FAIL:-0}" = "1" ] && return 1
		printf '%s\n' "${GH_DEFAULT_BRANCH-main}"
		return 0
	fi
	if [ "${1:-}" = "api" ]; then
		python3 "${GH_API_STUB}" "$@"
		return $?
	fi
	if [ "${1:-}" = "run" ] && [ "${2:-}" = "list" ]; then
		local arg jq_filter="" prev="" kind=""
		for arg in "$@"; do
			[ "${prev}" = "--jq" ] && jq_filter="${arg}"
			[ "${arg}" = "--branch" ] && kind="branch"
			prev="${arg}"
		done
		local payload="[]"
		if [ "${kind}" = "branch" ]; then
			payload="${GH_BRANCH_LIST:-[]}"
		fi
		if [ -n "${jq_filter}" ]; then
			printf '%s' "${payload}" | jq -r "${jq_filter}"
		else
			printf '%s\n' "${payload}"
		fi
		return 0
	fi
	return 1
}
"""


# The REST listing of one review wrapper's workflow_dispatch runs (issue #4927).
# $GH_WRAPPER_RUNS maps a wrapper file to runs in REST shape (as _named_run
# builds them); they are served as listed, per_page at a time.
# Knobs: $GH_API_404 (comma-separated wrappers that answer 404),
# $GH_API_FAIL (comma-separated "<wrapper>:<page>" that answer HTTP 502),
# $GH_API_MALFORMED (a wrapper whose pages lack total_count),
# $GH_API_TOTAL (JSON map of wrapper -> reported total_count), and
# $GH_API_FILLER ("<wrapper>:<n>", n completed runs of an unrelated PR served
# after the listed ones, too many to pass through the environment).
_API_STUB = r"""
import json, os, re, subprocess, sys

args = sys.argv[2:]
path = next(a for a in args if a.startswith("repos/"))
jq_filter = args[args.index("--jq") + 1] if "--jq" in args else None
m = re.search(r"/actions/workflows/([^/?]+)/runs\?(.*)$", path)
if not m:
	sys.exit(1)
wrapper = m.group(1)
query = dict(pair.split("=", 1) for pair in m.group(2).split("&"))
page = int(query.get("page", "1"))
per_page = int(query.get("per_page", "30"))
if wrapper in os.environ.get("GH_API_404", "").split(","):
	print('{"message":"Not Found","status":"404"}')
	print("gh: Not Found (HTTP 404)", file=sys.stderr)
	sys.exit(1)
if f"{wrapper}:{page}" in os.environ.get("GH_API_FAIL", "").split(","):
	print("gh: Server Error (HTTP 502)", file=sys.stderr)
	sys.exit(1)
runs = json.loads(os.environ.get("GH_WRAPPER_RUNS") or "{}").get(wrapper, [])
rest = list(runs)
filler_wrapper, _, filler_count = os.environ.get("GH_API_FILLER", "").partition(":")
if filler_wrapper == wrapper:
	rest += [{
		"id": 500000 + i, "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
		"display_title": "Internal: AI Review & Autofix [pr:900]",
		"head_branch": "main", "path": ".github/workflows/internal-review.yml",
		"created_at": "2026-09-28T00:00:00Z", "run_started_at": "2026-09-28T00:00:00Z",
	} for i in range(int(filler_count))]
total = json.loads(os.environ.get("GH_API_TOTAL") or "{}").get(wrapper, len(rest))
body = {"total_count": total, "workflow_runs": rest[(page - 1) * per_page:page * per_page]}
if os.environ.get("GH_API_MALFORMED") == wrapper:
	body = {"workflow_runs": []}
if jq_filter:
	proc = subprocess.run(["jq", "-c", jq_filter], input=json.dumps(body), capture_output=True, text=True)
	sys.stdout.write(proc.stdout)
	sys.exit(proc.returncode)
print(json.dumps(body))
"""


class _ShellHarness(unittest.TestCase):
	functions: tuple[tuple[Path, str], ...] = ()

	def setUp(self) -> None:
		self.tmp = Path(tempfile.mkdtemp(prefix="review-dispatch-default-branch-"))
		self.gh_log = self.tmp / "gh.log"
		self.gh_log.touch()
		body = "".join(_extract_fn(script, name) for script, name in self.functions)
		self.helpers = self.tmp / "helpers.sh"
		self.helpers.write_text(_STUBS + body, encoding="utf-8")
		self.api_stub = self.tmp / "gh_api_stub.py"
		self.api_stub.write_text(_API_STUB, encoding="utf-8")

	def run_bash(self, snippet: str, **env: str) -> subprocess.CompletedProcess:
		full_env = os.environ.copy()
		full_env.pop("BASH_ENV", None)
		full_env.pop("ENV", None)
		full_env.update({
			"GH_LOG": str(self.gh_log),
			"GH_API_STUB": str(self.api_stub),
			"GITHUB_REPOSITORY": "owner/repo",
			"PYTHONDONTWRITEBYTECODE": "1",
		})
		full_env.update(env)
		script = f"set -uo pipefail\nsource {self.helpers}\n{textwrap.dedent(snippet)}"
		return subprocess.run(["bash", "-c", script], env=full_env, capture_output=True, text=True, cwd=self.tmp)

	def gh_calls(self) -> list[list[str]]:
		return [line.split("\t") for line in self.gh_log.read_text(encoding="utf-8").splitlines() if line]


class ConflictDispatchDefaultBranch(_ShellHarness):
	functions = ((POLLER_SCRIPT, "_dispatch_review_for_conflicts"),)

	def _dispatch(self, pr: str, **env: str) -> subprocess.CompletedProcess:
		tracker = self.tmp / "tracker"
		tracker.touch()
		return self.run_bash(
			f"""
			_CONFLICT_DISPATCH_TRACKER="{tracker}"
			_has_active_autofix_run() {{ return 1; }}
			rc=0
			_dispatch_review_for_conflicts "{pr}" "ai/issue-7" || rc=$?
			echo "rc=${{rc}}"
			""",
			**env,
		)

	def test_dispatch_passes_no_ref_and_only_the_pr_number(self) -> None:
		result = self._dispatch("4701", ALLOW_WORKFLOW_EDITS="false")
		self.assertIn("rc=0", result.stdout, result.stderr)
		calls = self.gh_calls()
		self.assertEqual(len(calls), 1, calls)
		self.assertEqual(calls[0][:3], ["workflow", "run", "ai-review.yml"])
		self.assertNotIn("--ref", calls[0])
		self.assertNotIn("ai/issue-7", calls[0])
		self.assertIn("pr_number=4701", calls[0])
		self.assertIn("allow_workflow_edits=false", calls[0])
		fields = [calls[0][i + 1] for i, arg in enumerate(calls[0]) if arg == "-f"]
		self.assertEqual(fields, ["pr_number=4701", "allow_workflow_edits=false"])
		self.assertIn("from the default branch for head ai/issue-7", result.stdout)

	def test_every_fallback_workflow_is_dispatched_without_a_ref(self) -> None:
		result = self._dispatch("12", GH_WORKFLOW_RUN_RC="1")
		self.assertIn("rc=1", result.stdout, result.stderr)
		calls = self.gh_calls()
		self.assertEqual([c[2] for c in calls], ["ai-review.yml", "internal-review.yml", "review_autofix.yml"])
		for call in calls:
			self.assertNotIn("--ref", call)

	def test_invalid_pr_number_is_refused_before_any_call(self) -> None:
		for bad in ("", "0", "012", "12;rm", "-1", "12 13"):
			with self.subTest(pr=bad):
				self.gh_log.write_text("", encoding="utf-8")
				result = self._dispatch(bad)
				self.assertIn("rc=1", result.stdout, result.stderr)
				self.assertIn("invalid pr_number", result.stdout)
				self.assertEqual(self.gh_calls(), [])


_INTERNAL_REVIEW_PATH = ".github/workflows/internal-review.yml"
_AI_REVIEW_PATH = ".github/workflows/ai-review.yml"


def _named_run(pr: int, *, title: str | None = None, status: str = "in_progress", conclusion: str | None = None,
		created: str | None = None, run_id: int = 1, event: str = "workflow_dispatch",
		head_branch: str = "main", path: str | None = None) -> dict:
	"""One REST ``actions/runs`` entry, as the default-branch dispatch of a review wrapper reports it.

	``path`` defaults to the wrapper that sets ``title``: ``ai-review.yml``
	for an ``AI Review [pr:<N>]`` title, ``internal-review.yml`` otherwise.
	"""
	display_title = title if title is not None else f"Internal: AI Review & Autofix [pr:{pr}]"
	if path is None:
		path = _AI_REVIEW_PATH if display_title.startswith("AI Review") else _INTERNAL_REVIEW_PATH
	return {
		"id": run_id,
		"name": display_title,
		"event": event,
		"status": status,
		"conclusion": conclusion,
		"display_title": display_title,
		"head_branch": head_branch,
		"path": path,
		"created_at": created or _iso_minutes_ago(5),
		"run_started_at": created or _iso_minutes_ago(5),
	}


def _wrapper_runs(runs: list[dict]) -> str:
	"""Serve each run from the wrapper whose run name it carries."""
	by_wrapper: dict[str, list[dict]] = {"internal-review.yml": [], "ai-review.yml": []}
	for run in runs:
		wrapper = "ai-review.yml" if str(run.get("display_title", "")).startswith("AI Review") else "internal-review.yml"
		by_wrapper[wrapper].append(run)
	return json.dumps(by_wrapper)


def _api_calls(calls: list[list[str]]) -> list[list[str]]:
	"""The wrapper listing calls (the default-branch read is counted by _default_branch_calls)."""
	return [call for call in calls if call[:3] == ["api", "-X", "GET"]]


def _default_branch_calls(calls: list[list[str]]) -> list[list[str]]:
	return [call for call in calls if call[:2] == ["api", "repos/owner/repo"]]


class PrNamedReviewDefaultBranch(_ShellHarness):
	"""Issue #5094: the provenance branch is resolved once per shell and never guessed."""

	functions = ((POLLER_SCRIPT, "_pr_named_review_default_branch"),)

	def test_resolves_once_and_caches_in_the_calling_shell(self) -> None:
		result = self.run_bash(
			"""
			_pr_named_review_default_branch >/dev/null
			_pr_named_review_default_branch
			_pr_named_review_default_branch
			""",
			GH_DEFAULT_BRANCH="trunk",
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(result.stdout.split(), ["trunk", "trunk"])
		self.assertEqual(len(_default_branch_calls(self.gh_calls())), 1, self.gh_calls())

	def test_never_reuses_the_guessed_default_branch_variable(self) -> None:
		result = self.run_bash("_pr_named_review_default_branch", DEFAULT_BRANCH="main", GH_DEFAULT_BRANCH="trunk")
		self.assertEqual(result.stdout.strip(), "trunk")

	def test_unresolved_branch_prints_empty_and_logs(self) -> None:
		result = self.run_bash(
			"""
			_pr_named_review_default_branch
			_pr_named_review_default_branch
			""",
			GH_DEFAULT_BRANCH_FAIL="1",
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(result.stdout.strip(), "")
		self.assertEqual(result.stderr.count("PR_NAMED_REVIEW_PROVENANCE"), 1, result.stderr)
		self.assertIn("outcome=default_branch_unresolved", result.stderr)
		self.assertEqual(len(_default_branch_calls(self.gh_calls())), 1, self.gh_calls())

	def test_main_flow_primes_the_cache_before_processing_projects(self) -> None:
		poller = POLLER_SCRIPT.read_text(encoding="utf-8")
		prime = "\n_pr_named_review_default_branch >/dev/null || true\n"
		self.assertEqual(poller.count(prime), 1)
		self.assertLess(poller.index("\n_pr_named_review_default_branch()\n{"), poller.index(prime))
		self.assertLess(poller.index(prime), poller.index('TRACKING_ISSUES="$(cat "${RUNTIME_DIR}/tracking_issues.json")"'))
		self.assertLess(poller.index(prime), poller.index("\nrun_standalone_stall_recovery\n"))


class PrNamedReviewDispatchRuns(_ShellHarness):
	functions = (
		(POLLER_SCRIPT, "_pr_named_review_default_branch"),
		(POLLER_SCRIPT, "_pr_named_review_dispatch_runs"),
	)

	def _call(self, pr: str, runs: list[dict] | None = None, *, lookback: str | None = None, **env: str) -> subprocess.CompletedProcess:
		env.setdefault("GH_WRAPPER_RUNS", _wrapper_runs(runs or []))
		lookback_arg = "" if lookback is None else f' "{lookback}"'
		return self.run_bash(
			f"""
			rc=0
			out="$(_pr_named_review_dispatch_runs "{pr}"{lookback_arg})" || rc=$?
			printf '%s\n' "${{out}}"
			echo "rc=${{rc}}"
			""",
			**env,
		)

	def _runs(self, pr: str, runs: list[dict] | None = None, *, expect_rc: int = 0, lookback: str | None = None, **env: str) -> list[dict]:
		result = self._call(pr, runs, lookback=lookback, **env)
		lines = result.stdout.strip().splitlines()
		self.assertEqual(lines[-1], f"rc={expect_rc}", result.stderr)
		return json.loads(lines[0])

	def test_matches_both_wrapper_names_exactly(self) -> None:
		runs = [
			_named_run(12, run_id=1, created="2026-09-28T01:00:00Z"),
			_named_run(12, title="AI Review [pr:12]", run_id=2, created="2026-09-28T02:00:00Z"),
			_named_run(12, title="Internal: AI Review & Autofix [pr:123]", run_id=3),
			_named_run(12, title="Internal: AI Review & Autofix [pr:1]", run_id=4),
			_named_run(12, title="AI Review [pr:12] ", run_id=5),
			_named_run(12, title="Internal: AI Review & Autofix", run_id=6),
			_named_run(12, event="pull_request", run_id=7),
		]
		self.assertEqual([r["databaseId"] for r in self._runs("12", runs)], [2, 1])

	def test_output_keeps_the_camel_case_shape(self) -> None:
		run = _named_run(12, run_id=41, status="completed", conclusion="failure", created="2026-09-28T01:00:00Z")
		self.assertEqual(self._runs("12", [run]), [{
			"databaseId": 41, "event": "workflow_dispatch", "status": "completed", "conclusion": "failure",
			"displayTitle": "Internal: AI Review & Autofix [pr:12]",
			"createdAt": "2026-09-28T01:00:00Z", "startedAt": "2026-09-28T01:00:00Z",
		}])

	def test_rejects_runs_that_only_borrow_the_name(self) -> None:
		# Issue #5094: the name is evaluated from the workflow file at the
		# dispatched ref, so it proves nothing without identity (path) and
		# provenance (a default-branch head).
		spoofs = {
			"branch-dispatched wrapper": _named_run(12, run_id=11, head_branch="attacker/branch"),
			"branch-dispatched consumer wrapper": _named_run(12, title="AI Review [pr:12]", run_id=12, head_branch="attacker/branch"),
			"other workflow file": _named_run(12, run_id=13, path=".github/workflows/evil.yml"),
			"path suffix only": _named_run(12, run_id=14, path=".github/workflows/not-internal-review.yml"),
			"title of the other wrapper": _named_run(12, run_id=15, path=_AI_REVIEW_PATH),
			"consumer title from the internal wrapper": _named_run(12, title="AI Review [pr:12]", run_id=16, path=_INTERNAL_REVIEW_PATH),
			"push event": _named_run(12, run_id=17, event="push"),
			"missing head branch": _named_run(12, run_id=18, head_branch=""),
		}
		for label, run in spoofs.items():
			with self.subTest(spoof=label):
				self.assertEqual(self._runs("12", [run]), [])
		genuine = _named_run(12, run_id=19)
		self.assertEqual([r["databaseId"] for r in self._runs("12", list(spoofs.values()) + [genuine])], [19])

	def test_provenance_uses_the_resolved_default_branch(self) -> None:
		runs = [_named_run(12, run_id=1, head_branch="main"), _named_run(12, run_id=2, head_branch="trunk")]
		self.assertEqual([r["databaseId"] for r in self._runs("12", runs, GH_DEFAULT_BRANCH="trunk")], [2])

	def test_listing_is_not_branch_filtered_and_reads_the_branch_once(self) -> None:
		# The head_branch check runs on the listed rows, so the wrapper
		# listing keeps its URL (issue #4927) and adds one default-branch read.
		self._runs("12", [_named_run(12, head_branch="release/v2")], GH_DEFAULT_BRANCH="release/v2")
		calls = self.gh_calls()
		self.assertEqual(len(_default_branch_calls(calls)), 1, calls)
		self.assertEqual(len(_api_calls(calls)), 2, calls)
		self.assertEqual(len(calls), 3, calls)
		for call in _api_calls(calls):
			self.assertNotIn("branch=", call[3])

	def test_primed_cache_costs_only_the_runs_call(self) -> None:
		result = self.run_bash(
			"""
			_pr_named_review_default_branch >/dev/null
			: > "${GH_LOG}"
			_pr_named_review_dispatch_runs 12 >/dev/null
			_pr_named_review_dispatch_runs 12 >/dev/null
			""",
			GH_WRAPPER_RUNS=_wrapper_runs([_named_run(12)]),
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		calls = self.gh_calls()
		self.assertEqual(len(_api_calls(calls)), 4, calls)
		self.assertEqual(_default_branch_calls(calls), [])

	def test_unresolved_default_branch_is_incomplete_without_a_listing_call(self) -> None:
		# No run can be vouched for, and a missing run proves nothing, so the
		# callers skip their dispatch or push (issues #4927, #5094).
		for env in ({"GH_DEFAULT_BRANCH_FAIL": "1"}, {"GH_DEFAULT_BRANCH": ""}):
			with self.subTest(env=env):
				self.gh_log.write_text("", encoding="utf-8")
				result = self._call("12", [_named_run(12)], **env)
				lines = result.stdout.strip().splitlines()
				self.assertEqual(lines[-1], "rc=1", result.stderr)
				self.assertEqual(json.loads(lines[0]), [])
				self.assertIn(
					"PR_NAMED_REVIEW_RUNS pr=12 outcome=incomplete reason=default_branch_unavailable wrapper=none page=0 read=0 total=0",
					result.stderr,
				)
				self.assertEqual(_api_calls(self.gh_calls()), [])

	def test_lists_each_review_wrapper_within_the_review_window(self) -> None:
		# Issue #4927: no global page of every workflow's dispatches; one
		# REST listing per wrapper, bounded to REVIEW_RUN_MAX_RUNTIME_MINUTES.
		before = datetime.now(timezone.utc)
		self._runs("12", [_named_run(12)], REVIEW_RUN_MAX_RUNTIME_MINUTES="240")
		after = datetime.now(timezone.utc)
		calls = _api_calls(self.gh_calls())
		self.assertEqual(len(calls), 2, self.gh_calls())
		self.assertEqual(len(self.gh_calls()), 3, "no gh run list call is left; one default-branch read")
		wrappers = []
		for call in calls:
			self.assertEqual(call[1:3], ["-X", "GET"])
			path = call[3]
			m = re.fullmatch(
				r"repos/owner/repo/actions/workflows/([a-z-]+\.yml)/runs\?event=workflow_dispatch"
				r"&created=>=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)&per_page=100&page=1",
				path,
			)
			self.assertIsNotNone(m, path)
			wrappers.append(m.group(1))
			cutoff = datetime.strptime(m.group(2), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
			self.assertGreaterEqual(cutoff, before - timedelta(minutes=240, seconds=2))
			self.assertLessEqual(cutoff, after - timedelta(minutes=240) + timedelta(seconds=2))
			self.assertIn("--jq", call)
		self.assertEqual(wrappers, ["internal-review.yml", "ai-review.yml"])

	def test_default_window_is_250_minutes(self) -> None:
		self._runs("12", [])
		path = _api_calls(self.gh_calls())[0][3]
		cutoff = datetime.strptime(re.search(r"created=>=([^&]+)", path).group(1), "%Y-%m-%dT%H:%M:%SZ")
		age = datetime.now(timezone.utc).replace(tzinfo=None) - cutoff
		self.assertAlmostEqual(age.total_seconds(), 250 * 60, delta=5)

	def _cutoff_age_minutes(self) -> list[float]:
		ages = []
		for call in _api_calls(self.gh_calls()):
			cutoff = datetime.strptime(re.search(r"created=>=([^&]+)", call[3]).group(1), "%Y-%m-%dT%H:%M:%SZ")
			ages.append((datetime.now(timezone.utc).replace(tzinfo=None) - cutoff).total_seconds() / 60)
		return ages

	def test_lookback_argument_sets_the_cutoff(self) -> None:
		# The failed-autofix redispatch passes the review budget plus one
		# stall threshold (conformance fix 2, AD-8).
		self._runs("12", [], lookback="370", REVIEW_RUN_MAX_RUNTIME_MINUTES="240")
		ages = self._cutoff_age_minutes()
		self.assertEqual(len(ages), 2, self.gh_calls())
		for age in ages:
			self.assertAlmostEqual(age, 370, delta=0.1)

	def test_invalid_lookback_falls_back_to_the_review_window(self) -> None:
		for lookback in ("", "0", "-5", "12m", "3 60"):
			with self.subTest(lookback=lookback):
				self.gh_log.write_text("", encoding="utf-8")
				self._runs("12", [], lookback=lookback, REVIEW_RUN_MAX_RUNTIME_MINUTES="240")
				ages = self._cutoff_age_minutes()
				self.assertEqual(len(ages), 2, self.gh_calls())
				for age in ages:
					self.assertAlmostEqual(age, 240, delta=0.1)

	def test_invalid_review_window_env_falls_back_to_250(self) -> None:
		# The env fallback is checked like the argument: a misconfigured
		# REVIEW_RUN_MAX_RUNTIME_MINUTES never yields an unusable cutoff.
		for window in ("0", "-3", "abc", "1.5", "0250"):
			for lookback in (None, "x"):
				with self.subTest(window=window, lookback=lookback):
					self.gh_log.write_text("", encoding="utf-8")
					self._runs("12", [], lookback=lookback, REVIEW_RUN_MAX_RUNTIME_MINUTES=window)
					ages = self._cutoff_age_minutes()
					self.assertEqual(len(ages), 2, self.gh_calls())
					for age in ages:
						self.assertAlmostEqual(age, 250, delta=0.1)

	def test_pages_until_every_reported_run_is_read(self) -> None:
		# 150 unrelated wrapper runs are newer than the one for PR 12; the
		# old single page of 100 would have lost it.
		runs = [_named_run(900 + i, run_id=1000 + i, created=_iso_minutes_ago(1)) for i in range(150)]
		runs.append(_named_run(12, run_id=7, created=_iso_minutes_ago(30)))
		self.assertEqual([r["databaseId"] for r in self._runs("12", runs)], [7])
		pages = [re.search(r"page=(\d+)$", c[3]).group(1) for c in _api_calls(self.gh_calls()) if "internal-review.yml" in c[3]]
		self.assertEqual(pages, ["1", "2"])

	def test_absent_wrapper_404_is_complete_and_empty(self) -> None:
		self.assertEqual(
			[r["databaseId"] for r in self._runs("12", [_named_run(12, run_id=5)], GH_API_404="ai-review.yml")],
			[5],
		)
		self.assertEqual(self._runs("12", [], GH_API_404="ai-review.yml,internal-review.yml"), [])

	def test_page_failure_is_incomplete(self) -> None:
		result = self._call("12", [_named_run(12)], GH_API_FAIL="ai-review.yml:1")
		self.assertTrue(result.stdout.strip().endswith("rc=1"), result.stdout)
		self.assertIn("PR_NAMED_REVIEW_RUNS pr=12 outcome=incomplete reason=page_failed wrapper=ai-review.yml", result.stderr)
		self.assertIn("HTTP 502", result.stderr)

	def test_a_404_after_the_first_page_is_incomplete(self) -> None:
		runs = [_named_run(900 + i, run_id=1000 + i) for i in range(150)]
		result = self._call("12", runs, GH_API_FAIL="internal-review.yml:2")
		self.assertTrue(result.stdout.strip().endswith("rc=1"), result.stdout)
		self.assertIn("reason=page_failed wrapper=internal-review.yml page=2 read=100 total=150", result.stderr)

	def test_malformed_page_is_incomplete(self) -> None:
		result = self._call("12", [], GH_API_MALFORMED="internal-review.yml")
		self.assertTrue(result.stdout.strip().endswith("rc=1"), result.stdout)
		self.assertIn("reason=malformed_page", result.stderr)

	def test_short_page_before_total_count_is_incomplete(self) -> None:
		# The listing reports 5 runs but serves 2: it shifted while being read.
		runs = [_named_run(12, run_id=1), _named_run(12, run_id=2, status="queued")]
		result = self._call("12", runs, GH_API_TOTAL=json.dumps({"internal-review.yml": 5}))
		lines = result.stdout.strip().splitlines()
		self.assertEqual(lines[-1], "rc=1")
		self.assertIn("reason=listing_shifted", result.stderr)
		# The matches read so far are still printed.
		self.assertEqual(sorted(r["databaseId"] for r in json.loads(lines[0])), [1, 2])

	def test_more_runs_than_ten_pages_hold_is_truncated(self) -> None:
		result = self._call(
			"12", [], GH_API_FILLER="internal-review.yml:1000", GH_API_TOTAL=json.dumps({"internal-review.yml": 1001}),
		)
		self.assertTrue(result.stdout.strip().endswith("rc=1"), result.stdout)
		self.assertIn("reason=truncated wrapper=internal-review.yml page=11 read=1000 total=1001", result.stderr)
		self.assertEqual(len(_api_calls(self.gh_calls())), 10)

	def test_invalid_pr_number_prints_empty_list_without_a_call(self) -> None:
		for bad in ("", "0", "07", "1]", "x"):
			with self.subTest(pr=bad):
				self.assertEqual(self._runs(bad, [_named_run(1)]), [])
		self.assertEqual(self.gh_calls(), [])

	def test_every_outcome_is_safe_under_set_e(self) -> None:
		# The poller runs with `set -euo pipefail`; no outcome may abort it.
		cases = {
			"complete": {},
			"absent": {"GH_API_404": "ai-review.yml,internal-review.yml"},
			"page_failed": {"GH_API_FAIL": "internal-review.yml:1"},
			"malformed_page": {"GH_API_MALFORMED": "ai-review.yml"},
			"listing_shifted": {"GH_API_TOTAL": json.dumps({"internal-review.yml": 9})},
			"truncated": {"GH_API_FILLER": "internal-review.yml:1000", "GH_API_TOTAL": json.dumps({"internal-review.yml": 1001})},
			"default_branch_unavailable": {"GH_DEFAULT_BRANCH_FAIL": "1"},
		}
		for name, env in cases.items():
			with self.subTest(case=name):
				result = self.run_bash(
					"""
					set -e
					rc=0
					out="$(_pr_named_review_dispatch_runs 12)" || rc=$?
					echo "survived rc=${rc}"
					""",
					GH_WRAPPER_RUNS=_wrapper_runs([_named_run(12)]),
					**env,
				)
				expected = "0" if name in ("complete", "absent") else "1"
				self.assertIn(f"survived rc={expected}", result.stdout, result.stderr)

	def test_complete_listing_logs_nothing(self) -> None:
		result = self._call("12", [_named_run(12)])
		self.assertNotIn("PR_NAMED_REVIEW_RUNS", result.stderr)


class HasActiveAutofixRunPrNamed(_ShellHarness):
	functions = (
		(POLLER_SCRIPT, "_has_active_autofix_run"),
		(POLLER_SCRIPT, "_pr_named_review_default_branch"),
		(POLLER_SCRIPT, "_pr_named_review_dispatch_runs"),
	)

	def _check(self, pr: str, runs: list[dict], branch_count: str = "0", **env: str) -> tuple[subprocess.CompletedProcess, str]:
		result = self.run_bash(
			f"""
			rc=0
			_has_active_autofix_run "{pr}" "ai/issue-7" || rc=$?
			echo "rc=${{rc}}"
			""",
			GH_WRAPPER_RUNS=_wrapper_runs(runs),
			GH_BRANCH_LIST=json.dumps([{"status": "in_progress"}] * int(branch_count)),
			**env,
		)
		return result, result.stdout

	def test_consumer_ai_review_name_counts_as_active(self) -> None:
		for status in ("in_progress", "queued", "pending"):
			with self.subTest(status=status):
				_result, out = self._check("33", [_named_run(33, title="AI Review [pr:33]", status=status)])
				self.assertIn("rc=0", out)
				self.assertIn("workflow_dispatch run named for PR #33", out)

	def test_completed_or_other_pr_runs_do_not_count(self) -> None:
		_result, out = self._check("33", [
			_named_run(33, status="completed", conclusion="failure"),
			_named_run(34, title="AI Review [pr:34]"),
		])
		self.assertIn("rc=1", out)

	def test_spoofed_pr_named_run_does_not_suppress_the_dispatch(self) -> None:
		# Issue #5094: a run that only borrows the PR's review name must not
		# make the guard skip the conflict dispatch.
		_result, out = self._check("33", [
			_named_run(33, run_id=1, head_branch="attacker/branch"),
			_named_run(33, run_id=2, path=".github/workflows/evil.yml"),
		])
		self.assertIn("rc=1", out)
		self.assertNotIn("Skipping dispatch", out)

	def test_incomplete_listing_counts_as_active(self) -> None:
		# Issue #4927: a listing that could not be read in full cannot prove
		# that no review run is active, so the dispatch is skipped.
		_result, out = self._check("33", [], GH_API_FAIL="internal-review.yml:1")
		self.assertIn("rc=0", out)
		self.assertIn("Review dispatch run listing incomplete (PR-named lookup)", out)

	def test_pr_named_call_only_after_head_branch_lookups_miss(self) -> None:
		_result, out = self._check("33", [_named_run(33)], branch_count="1")
		self.assertIn("rc=0", out)
		self.assertEqual(_api_calls(self.gh_calls()), [])
		self.gh_log.write_text("", encoding="utf-8")
		self._check("33", [], branch_count="0")
		self.assertEqual(len(_api_calls(self.gh_calls())), 2, self.gh_calls())


class DirectInflightFallbackPrNamed(_ShellHarness):
	functions = (
		(POLLER_SCRIPT, "_direct_inflight_review_run_on_branch"),
		(POLLER_SCRIPT, "_pr_named_review_default_branch"),
		(POLLER_SCRIPT, "_pr_named_review_dispatch_runs"),
	)

	def _run(self, args: str, runs: list[dict], **env: str) -> subprocess.CompletedProcess:
		return self.run_bash(
			f"_direct_inflight_review_run_on_branch {args}",
			GH_BRANCH_LIST="[]",
			GH_WRAPPER_RUNS=_wrapper_runs(runs),
			REVIEW_RUN_MAX_RUNTIME_MINUTES="240",
			**env,
		)

	def test_finds_fresh_pr_named_run_the_branch_listing_missed(self) -> None:
		result = self._run('"ai/issue-7" "55"', [_named_run(55, run_id=9055)])
		self.assertEqual(result.stdout.strip(), "9055", result.stderr)
		self.assertIn("outcome=pr_named_review_run", result.stderr)

	def test_ignores_stale_or_finished_pr_named_runs(self) -> None:
		result = self._run('"ai/issue-7" "55"', [
			_named_run(55, run_id=1, created=_iso_minutes_ago(300)),
			_named_run(55, run_id=2, status="completed", conclusion="success"),
		])
		self.assertEqual(result.stdout.strip(), "")
		self.assertIn("outcome=no_fresh_review_run", result.stderr)

	def test_spoofed_pr_named_run_does_not_block_the_push(self) -> None:
		result = self._run('"ai/issue-7" "55"', [
			_named_run(55, run_id=1, head_branch="attacker/branch"),
			_named_run(55, title="AI Review [pr:55]", run_id=2, path=_INTERNAL_REVIEW_PATH),
		])
		self.assertEqual(result.stdout.strip(), "")
		self.assertIn("outcome=no_fresh_review_run", result.stderr)

	def test_incomplete_listing_prints_the_sentinel(self) -> None:
		# Issue #4927: both push sites skip the empty-commit push on any
		# non-empty result, so the sentinel blocks the push.
		result = self._run('"ai/issue-7" "55"', [_named_run(55)], GH_API_FAIL="ai-review.yml:1")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(result.stdout.strip(), "listing-incomplete")
		self.assertIn("pr=55", result.stderr)
		self.assertIn("outcome=pr_named_listing_incomplete", result.stderr)
		# rc is the PR-named listing's return code, not the branch listing's
		# (which succeeded to reach this path).
		self.assertIn("STALL_INFLIGHT_DIRECT_CHECK branch=ai/issue-7 pr=55 rc=1 ", result.stderr)
		self.assertIn("PR_NAMED_REVIEW_RUNS pr=55 outcome=incomplete reason=page_failed", result.stderr)

	def test_without_a_pr_number_makes_no_extra_call(self) -> None:
		result = self._run('"ai/issue-7"', [_named_run(55)])
		self.assertEqual(result.stdout.strip(), "")
		self.assertEqual(_api_calls(self.gh_calls()), [])
		self.assertEqual(_default_branch_calls(self.gh_calls()), [])
		self.assertEqual(len(self.gh_calls()), 1, self.gh_calls())


class StallJudgeWorkflowOutcomesPrNamed(unittest.TestCase):
	"""The stall judge's run list reads the cached blob, so PR-named runs cost no call."""

	def setUp(self) -> None:
		text = POLLER_SCRIPT.read_text(encoding="utf-8")
		start = text.index('workflow_outcomes="$(printf \'%s\' "${workflows_json}" | jq -c')
		program_start = text.index("'", text.index("--arg pr", start)) + 1
		program_end = text.index("' 2>/dev/null || echo '[]')\"", program_start)
		self.program = text[program_start:program_end]

	def _outcomes(self, runs: list[dict], pr: str, default_branch: str = "main") -> list[dict]:
		result = subprocess.run(
			["jq", "-c", "--arg", "head_ref", "ai/issue-7", "--arg", "head_sha", "a" * 40, "--arg", "pr", pr,
			 "--arg", "default_branch", default_branch, self.program],
			input=json.dumps({"workflow_runs": runs}), capture_output=True, text=True,
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		return json.loads(result.stdout)

	def test_includes_pr_named_dispatch_runs_and_excludes_others(self) -> None:
		base = {"name": "Internal: AI Review & Autofix", "path": ".github/workflows/internal-review.yml",
			"head_branch": "main", "head_sha": "f" * 40, "status": "completed"}
		runs = [
			dict(base, id=1, event="workflow_dispatch", display_title="Internal: AI Review & Autofix [pr:77]",
				conclusion="failure", created_at="2026-09-28T01:00:00Z"),
			dict(base, id=2, event="workflow_dispatch", display_title="AI Review [pr:77]",
				name="AI Review", path=".github/workflows/ai-review.yml", conclusion="success", created_at="2026-09-28T02:00:00Z"),
			dict(base, id=3, event="workflow_dispatch", display_title="Internal: AI Review & Autofix [pr:78]",
				conclusion="failure", created_at="2026-09-28T03:00:00Z"),
			dict(base, id=4, event="pull_request", display_title="Internal: AI Review & Autofix [pr:77]",
				conclusion="failure", created_at="2026-09-28T04:00:00Z"),
			dict(base, id=5, event="pull_request", head_branch="ai/issue-7", display_title="some title",
				conclusion="success", created_at="2026-09-28T00:30:00Z"),
		]
		self.assertEqual([r["id"] for r in self._outcomes(runs, "77")], [2, 1, 5])

	def test_rejects_pr_named_runs_without_identity_or_provenance(self) -> None:
		base = {"name": "Internal: AI Review & Autofix", "path": _INTERNAL_REVIEW_PATH, "event": "workflow_dispatch",
			"display_title": "Internal: AI Review & Autofix [pr:77]", "head_branch": "main", "head_sha": "f" * 40,
			"status": "completed", "conclusion": "failure", "created_at": "2026-09-28T01:00:00Z"}
		runs = [
			dict(base, id=1, head_branch="attacker/branch"),
			dict(base, id=2, path=".github/workflows/evil-internal-review.yml"),
			dict(base, id=3, display_title="AI Review [pr:77]"),
			dict(base, id=4),
		]
		self.assertEqual([r["id"] for r in self._outcomes(runs, "77")], [4])
		self.assertEqual(self._outcomes([dict(base, id=4)], "77", default_branch=""), [])
		self.assertEqual([r["id"] for r in self._outcomes([dict(base, id=5, head_branch="trunk")], "77", "trunk")], [5])

	def test_resolves_the_default_branch_before_the_scan(self) -> None:
		text = POLLER_SCRIPT.read_text(encoding="utf-8")
		start = text.index('workflow_outcomes="$(printf \'%s\' "${workflows_json}" | jq -c')
		preamble = text[text.rindex("_load_actions_runs_cached", 0, start):start]
		self.assertIn('_wo_default_branch="$(_pr_named_review_default_branch 2>/dev/null)"', preamble)
		self.assertIn('--arg default_branch "${_wo_default_branch}"', text[start:start + 300])

	def test_no_linked_pr_keeps_head_branch_matching_only(self) -> None:
		runs = [
			{"id": 1, "name": "AI Review", "path": ".github/workflows/ai-review.yml", "event": "workflow_dispatch",
			 "display_title": "AI Review [pr:77]", "head_branch": "main", "head_sha": "f" * 40,
			 "status": "completed", "conclusion": "failure", "created_at": "2026-09-28T01:00:00Z"},
		]
		self.assertEqual(self._outcomes(runs, ""), [])


class MergeTrainDispatchDefaultBranch(_ShellHarness):
	functions = ((MERGE_TRAIN_SCRIPT, "_mt_dispatch_review"),)

	def _dispatch(self, pr: str) -> subprocess.CompletedProcess:
		return self.run_bash(
			f"""
			MT_REPO=owner/repo
			_mt_log() {{ printf '%s\\n' "$*"; }}
			_mt_warn() {{ printf '::warning::%s\\n' "$*"; }}
			rc=0
			_mt_dispatch_review "{pr}" "ai/issue-9" || rc=$?
			echo "rc=${{rc}}"
			""",
		)

	def test_dispatch_passes_no_ref(self) -> None:
		result = self._dispatch("4077")
		self.assertIn("rc=0", result.stdout, result.stderr)
		self.assertIn("MERGE_TRAIN_DISPATCHED pr=4077 workflow=ai-review.yml ref=default head=ai/issue-9", result.stdout)
		calls = self.gh_calls()
		self.assertEqual(len(calls), 1)
		self.assertNotIn("--ref", calls[0])
		self.assertNotIn("ai/issue-9", calls[0])

	def test_invalid_pr_number_is_refused_before_any_call(self) -> None:
		for bad in ("0", "", "4077x"):
			with self.subTest(pr=bad):
				self.gh_log.write_text("", encoding="utf-8")
				result = self._dispatch(bad)
				self.assertIn("rc=1", result.stdout)
				self.assertEqual(self.gh_calls(), [])


class ReviewWrapperRunNames(unittest.TestCase):
	def test_consumer_ai_review_names_dispatched_runs_for_their_pr(self) -> None:
		text = AI_REVIEW_TEMPLATE.read_text(encoding="utf-8")
		self.assertIn(
			"run-name: \"${{ github.event_name == 'workflow_dispatch' && "
			"format('AI Review [pr:{0}]', inputs.pr_number) || '' }}\"",
			text,
		)
		self.assertLess(text.index("run-name:"), text.index("\non:"))

	def test_lookups_use_the_names_the_wrappers_set(self) -> None:
		poller = POLLER_SCRIPT.read_text(encoding="utf-8")
		merge_train = MERGE_TRAIN_SCRIPT.read_text(encoding="utf-8")
		self.assertIn("format('Internal: AI Review & Autofix [pr:{0}]'", INTERNAL_REVIEW_WF.read_text(encoding="utf-8"))
		self.assertIn("format('AI Review [pr:{0}]'", AI_REVIEW_TEMPLATE.read_text(encoding="utf-8"))
		for name in ("Internal: AI Review & Autofix [pr:", "AI Review [pr:"):
			self.assertIn(f'("{name}" + $pr + "]")', poller)
		self.assertIn("(?<name>Internal: AI Review & Autofix|AI Review) \\\\[pr:", merge_train)
		# Issue #5840: each name counts only with the path of the wrapper that sets it.
		self.assertIn('.name == "Internal: AI Review & Autofix" and $path == ".github/workflows/internal-review.yml"', merge_train)
		self.assertIn('.name == "AI Review" and $path == ".github/workflows/ai-review.yml"', merge_train)

	def test_both_empty_commit_push_guards_match_pr_named_runs(self) -> None:
		# The managed-path scan is exercised end to end in
		# test_orchestrate_poll_process.py; the standalone scan carries the
		# identical clause, and both pass the PR to the direct fallback.
		poller = POLLER_SCRIPT.read_text(encoding="utf-8")
		for scan_start in ('_rtr_inflight_id="$(printf', '_std_rtr_inflight_id="$(printf'):
			with self.subTest(scan=scan_start):
				start = poller.index(scan_start)
				scan = poller[start:poller.index("(.[0].id // empty)", start)]
				self.assertIn('--arg pr "${pr_num}"', scan)
				self.assertIn('--arg default_branch "$(_pr_named_review_default_branch 2>/dev/null)"', scan)
				self.assertIn('or ($pr != "" and $default_branch != "" and (.event // "") == "workflow_dispatch"', scan)
				self.assertIn('and (.head_branch // "") == $default_branch', scan)
				self.assertIn('(.display_title // "") == ("Internal: AI Review & Autofix [pr:" + $pr + "]")', scan)
				self.assertIn('and (.path // "") == ".github/workflows/internal-review.yml")', scan)
				self.assertIn('(.display_title // "") == ("AI Review [pr:" + $pr + "]")', scan)
				self.assertIn('and (.path // "") == ".github/workflows/ai-review.yml")', scan)
		self.assertEqual(poller.count('_direct_inflight_review_run_on_branch "${head_ref}" "${pr_num}")"'), 2)

	def test_no_review_dispatch_passes_a_head_ref(self) -> None:
		poller = POLLER_SCRIPT.read_text(encoding="utf-8")
		body = _extract_fn(POLLER_SCRIPT, "_dispatch_review_for_conflicts")
		self.assertNotIn('--ref "${head_ref}"', body)
		self.assertNotIn('--ref "${head_ref}"', poller)
		self.assertNotIn('--ref "${head}"', MERGE_TRAIN_SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
	unittest.main()
