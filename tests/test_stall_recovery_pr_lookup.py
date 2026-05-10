#!/usr/bin/env python3
"""Runtime tests for the stall-recovery PR-lookup fixes.

Covers:
- Gap 1: ``close_linked_pr`` discovers linked PRs via timeline cross-ref,
  branch-name (``ai/issue-<n>``) and body-parse (``Closes #<n>``), iterates
  every open match, skips already-closed PRs, and logs a diagnostic when
  no PR is found.
- Gap 2: ``surface_reissue_closed_without_pr`` emits the
  ``REISSUE_CLOSED_WITHOUT_PR`` log prefix, the GHA ``::warning::``
  annotation, and posts an issue comment — only when the issue body
  carries the ``Re-issued from #<n>`` marker and no PR is found.

Uses the same function-extraction-plus-stub pattern as
``test_merge_probe.py``: pull the helper definitions out of the
production script with awk, source them, and exercise with a controlled
``gh`` / ``gh_retry`` / ``_safe_gh_jq`` environment.
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
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	full_env["GITHUB_REPOSITORY"] = "owner/repo"
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=cwd,
		env=full_env,
		capture_output=True,
		text=True,
	)


# Extracts a single shell function body from POLLER_SCRIPT by matching
# the declaration line (``fn()`` at column 0) through the first
# closing ``}`` at column 0. Works for both ``fn() {`` and ``fn()\n{``
# styles since we just scan forward from the declaration until we hit
# a bare ``}`` line.
_EXTRACT_FN = r"""
extract_fn() {
	local fn="$1"
	awk -v fn="${fn}" '
		BEGIN { in_fn=0 }
		$0 ~ "^"fn"\\(\\)" { in_fn=1 }
		in_fn { print }
		in_fn && /^\}$/ { exit }
	' "__POLLER__"
}
"""


_STUBS = r"""
# Minimal stubs for the helper dependencies we do not want to exercise
# from the production code path. Each test injects richer behavior by
# overriding individual stubs *after* sourcing the helpers.

gh_retry() { "$@"; }

_safe_gh_jq() {
	# Dispatched by ENDPOINT_STATES / ENDPOINT_BODIES associative arrays
	# exported from the test harness. Signature mirrors production:
	#   _safe_gh_jq "<path>" --jq '<filter>'
	local path="$1"
	shift
	local jq_filter=""
	while [ "$#" -gt 0 ]; do
		if [ "$1" = "--jq" ]; then
			jq_filter="$2"
			shift 2
		else
			shift
		fi
	done
	case "${jq_filter}" in
		".state")
			printf '%s' "${PR_STATE_MAP[${path}]:-open}"
			;;
		".body // \"\""|".body // \"\"")
			printf '%s' "${ISSUE_BODY_MAP[${path}]:-}"
			;;
		*)
			printf ''
			;;
	esac
}

# Default gh shim — tests override subcommands as needed. Honors --jq
# the same way the real gh does (pipes JSON through jq -r), so callers
# that rely on ``gh pr list --jq '.[].number'`` get numbers, not JSON.
gh() {
	local sub="${1:-}"
	local sub2="${2:-}"
	case "${sub}-${sub2}" in
		pr-list)
			local use_search=0
			local use_head=0
			local jq_filter=""
			local prev=""
			for arg in "$@"; do
				[ "${arg}" = "--search" ] && use_search=1
				[ "${arg}" = "--head" ] && use_head=1
				[ "${prev}" = "--jq" ] && jq_filter="${arg}"
				prev="${arg}"
			done
			local payload=""
			if [ "${use_search}" = 1 ]; then
				payload="${GH_PR_LIST_SEARCH_JSON:-[]}"
			elif [ "${use_head}" = 1 ]; then
				payload="${GH_PR_LIST_HEAD_JSON:-[]}"
			else
				payload="[]"
			fi
			if [ -n "${jq_filter}" ]; then
				printf '%s' "${payload}" | jq -r "${jq_filter}" 2>/dev/null || true
			else
				printf '%s' "${payload}"
			fi
			;;
		pr-close)
			printf '%s\n' "$3" >> "${GH_PR_CLOSE_LOG:-/dev/null}"
			;;
		api-*)
			local path="$2"
			if [[ "${path}" == *"/comments" ]]; then
				printf '%s\n' "${path}" >> "${GH_ISSUE_COMMENT_LOG:-/dev/null}"
			fi
			printf ''
			;;
		*)
			printf ''
			;;
	esac
	return 0
}

# Stub the timeline → cross-ref helper that upstream helpers call
# through when branch-name / body-parse return empty. Tests override
# ``_issue_cross_ref_pr_numbers_unique`` directly below. Emits newline-
# delimited numbers (one per line) to match the real helper's output
# shape — critical so the ``grep -E '^[0-9]+$'`` filter in
# ``_find_all_linked_prs`` sees each entry on its own line.
_issue_timeline_with_cross_refs_json() { printf '[]'; }
_issue_cross_ref_pr_numbers_unique() {
	local raw="${CROSS_REF_PR_NUMBERS:-}"
	[ -z "${raw}" ] && return 0
	printf '%s\n' "${raw}"
}

memory_record_run_event() { return 0; }

export -f gh gh_retry _safe_gh_jq _issue_timeline_with_cross_refs_json _issue_cross_ref_pr_numbers_unique memory_record_run_event 2>/dev/null || true
"""


def _bootstrap(tmp_dir: Path) -> Path:
	"""Write ``helpers.sh`` containing the extracted production helpers
	plus the common stubs, and return the path."""
	extractor = _EXTRACT_FN.replace("__POLLER__", str(POLLER_SCRIPT))
	script = textwrap.dedent(f"""
	set -euo pipefail
	{extractor}
	: > helpers.sh
	extract_fn '_linked_prs_by_branch_name'    >> helpers.sh
	echo                                       >> helpers.sh
	extract_fn '_linked_prs_by_body_reference' >> helpers.sh
	echo                                       >> helpers.sh
	extract_fn '_find_all_linked_prs'          >> helpers.sh
	echo                                       >> helpers.sh
	extract_fn 'close_linked_pr'               >> helpers.sh
	echo                                       >> helpers.sh
	extract_fn 'surface_reissue_closed_without_pr' >> helpers.sh
	""")
	r = _run_bash(script, cwd=tmp_dir)
	assert r.returncode == 0, f"extraction failed: {r.stderr}\n{r.stdout}"
	helpers = tmp_dir / "helpers.sh"
	assert helpers.exists() and helpers.stat().st_size > 0, "helpers.sh not produced"
	return helpers


def _invoke(body: str, cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	script = textwrap.dedent(f"""
	set -uo pipefail
	declare -gA PR_STATE_MAP=()
	declare -gA ISSUE_BODY_MAP=()
	{_STUBS}
	source helpers.sh
	{body}
	""")
	return _run_bash(script, cwd=cwd, env=extra_env)


# ---------------------------------------------------------------------------
# Gap 1: close_linked_pr broadened lookup
# ---------------------------------------------------------------------------

def test_close_linked_pr_finds_pr_via_branch_name_when_timeline_empty():
	"""Reproduces the #2552 / #2568 prod miss: timeline cross-ref event is
	missing, but the PR exists at branch ``ai/issue-2552``. The broadened
	lookup must discover and close it."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		log = tmp / "closed.log"
		log.write_text("", encoding="utf-8")
		comment_log = tmp / "comments.log"
		comment_log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_PR_CLOSE_LOG="{log}"
			export GH_ISSUE_COMMENT_LOG="{comment_log}"
			CROSS_REF_PR_NUMBERS=""
			export GH_PR_LIST_HEAD_JSON='[{{"number":2568}}]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			PR_STATE_MAP["repos/owner/repo/pulls/2568"]="open"
			close_linked_pr 2552 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "2568" in log.read_text(encoding="utf-8"), (
			f"expected PR 2568 to be closed; log was {log.read_text()}\nstdout:\n{r.stdout}"
		)
		assert "close_linked_pr: closing linked PR #2568" in r.stdout
		assert "scanned=1 closed=1" in r.stdout


def test_close_linked_pr_finds_pr_via_body_parse_when_branch_empty():
	"""Body-parse fallback: timeline and branch-name return empty, but a
	PR body says ``Closes #2552``. Must be discovered and closed."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		log = tmp / "closed.log"
		log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_PR_CLOSE_LOG="{log}"
			CROSS_REF_PR_NUMBERS=""
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[{{"number":2580,"body":"Closes #2552"}},{{"number":2581,"body":"mentions #25528 only"}}]'
			PR_STATE_MAP["repos/owner/repo/pulls/2580"]="open"
			PR_STATE_MAP["repos/owner/repo/pulls/2581"]="open"
			close_linked_pr 2552 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		text = log.read_text(encoding="utf-8")
		assert "2580" in text, f"expected #2580 closed; got: {text}"
		assert "2581" not in text, (
			f"#25528 substring must not match issue #2552 (word-boundary regression); got: {text}"
		)


def test_close_linked_pr_iterates_multiple_prs():
	"""Two distinct PRs linked to the issue — both must be closed."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		log = tmp / "closed.log"
		log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_PR_CLOSE_LOG="{log}"
			CROSS_REF_PR_NUMBERS=$'3001\n3002'
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			PR_STATE_MAP["repos/owner/repo/pulls/3001"]="open"
			PR_STATE_MAP["repos/owner/repo/pulls/3002"]="open"
			close_linked_pr 2552 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		closed = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
		assert sorted(closed) == ["3001", "3002"], closed
		assert "scanned=2 closed=2" in r.stdout


def test_close_linked_pr_dedupes_across_lookup_sources():
	"""A PR surfaced by BOTH timeline and branch-name must only be
	closed once."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		log = tmp / "closed.log"
		log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_PR_CLOSE_LOG="{log}"
			CROSS_REF_PR_NUMBERS="2568"
			export GH_PR_LIST_HEAD_JSON='[{{"number":2568}}]'
			export GH_PR_LIST_SEARCH_JSON='[{{"number":2568,"body":"Closes #2552"}}]'
			PR_STATE_MAP["repos/owner/repo/pulls/2568"]="open"
			close_linked_pr 2552 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		closed = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
		assert closed == ["2568"], closed


def test_close_linked_pr_skips_already_closed_pr():
	"""If the discovered PR is already closed/merged, no ``pr close`` is
	issued."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		log = tmp / "closed.log"
		log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_PR_CLOSE_LOG="{log}"
			CROSS_REF_PR_NUMBERS="4001"
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			PR_STATE_MAP["repos/owner/repo/pulls/4001"]="merged"
			close_linked_pr 2552 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert log.read_text(encoding="utf-8").strip() == ""
		assert "skipping PR #4001" in r.stdout
		assert "state=merged" in r.stdout


def test_close_linked_pr_logs_miss_when_no_sources_return_a_pr():
	"""When all three lookups are empty, log the diagnostic and return 0."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		r = _invoke(
			body="""
			CROSS_REF_PR_NUMBERS=""
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			close_linked_pr 9999 "stall recovery test"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "no linked PRs found for issue #9999" in r.stderr
		assert "timeline/branch/body lookups all empty" in r.stderr


# ---------------------------------------------------------------------------
# Gap 2: surface_reissue_closed_without_pr
# ---------------------------------------------------------------------------


def test_surface_fires_on_reissue_body_with_no_pr():
	"""Reproduces #2591: body marks this as a re-issue of #2552 and no PR
	exists. Must emit the stable log prefix, GHA warning, and post an
	issue comment."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		comment_log = tmp / "comments.log"
		comment_log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_ISSUE_COMMENT_LOG="{comment_log}"
			ISSUE_BODY_MAP["repos/owner/repo/issues/2591"]=$'Original body\\n\\n---\\n\\n**Re-issued from #2552** — stalled in ai:done for 127m.'
			CROSS_REF_PR_NUMBERS=""
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			surface_reissue_closed_without_pr 2591 "ai:done" 130 3 "standalone"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "REISSUE_CLOSED_WITHOUT_PR issue=2591 parent=2552 phase=ai:done stall_minutes=130 recovery_count=3 source=standalone" in r.stdout
		assert "::warning title=Re-issue closed without PR::" in r.stdout
		comments = comment_log.read_text(encoding="utf-8").strip().splitlines()
		assert any("/issues/2591/comments" in c for c in comments), comments


def test_surface_noops_when_issue_is_not_a_reissue():
	"""An ordinary (non-re-issue) task closing with no PR is out-of-scope
	for Gap 2. The helper must stay silent."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		comment_log = tmp / "comments.log"
		comment_log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_ISSUE_COMMENT_LOG="{comment_log}"
			ISSUE_BODY_MAP["repos/owner/repo/issues/7"]="plain task body with no re-issue marker"
			CROSS_REF_PR_NUMBERS=""
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			surface_reissue_closed_without_pr 7 "ai:done" 130 3 "main"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "REISSUE_CLOSED_WITHOUT_PR" not in r.stdout
		assert comment_log.read_text(encoding="utf-8").strip() == ""


def test_surface_noops_when_reissue_has_at_least_one_pr():
	"""Re-issue that actually produced a PR is healthy — no Gap-2
	signal."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		_bootstrap(tmp)
		comment_log = tmp / "comments.log"
		comment_log.write_text("", encoding="utf-8")
		r = _invoke(
			body=f"""
			export GH_ISSUE_COMMENT_LOG="{comment_log}"
			ISSUE_BODY_MAP["repos/owner/repo/issues/2591"]="**Re-issued from #2552** stalled"
			CROSS_REF_PR_NUMBERS="2700"
			export GH_PR_LIST_HEAD_JSON='[]'
			export GH_PR_LIST_SEARCH_JSON='[]'
			surface_reissue_closed_without_pr 2591 "ai:done" 130 3 "standalone"
			""",
			cwd=tmp,
		)
		assert r.returncode == 0, r.stderr
		assert "REISSUE_CLOSED_WITHOUT_PR" not in r.stdout
		assert comment_log.read_text(encoding="utf-8").strip() == ""


# ---------------------------------------------------------------------------
# Gap 3: implement.yml "Safety check for existing PR" — false-positive guard
#
# Regression for a consumer-side reproduction (bitsafe.io issue #141 stuck in
# /approved ↔ stall-recovery loop because the safety check used
# `gh pr list --search "issue:${ISSUE_NUMBER}"`. GitHub has no `issue:`
# PR-search qualifier, so the query degraded to fuzzy text match and returned
# any open PR whose body or comments contained the digit string — including
# unrelated PRs whose Copilot review comments referenced a migration line
# number that happened to match the issue number). The fix mirrors plan.yml's
# "Skip when issue already has a PR" step: walk the issue timeline for
# `cross-referenced` events that point at OPEN PullRequests. The tests below
# extract the workflow step's run-block as text and exercise it under a
# minimal `gh` stub so a future revert to the buggy `--search` form is
# caught at CI.
# ---------------------------------------------------------------------------


def _workflow_step_block(workflow_path: Path, step_name: str) -> list[str]:
	lines = workflow_path.read_text(encoding="utf-8").splitlines()
	needle = f"- name: {step_name}"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		step_indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:"):
				ind = len(candidate) - len(candidate.lstrip(" "))
				if ind == step_indent:
					end = j
					break
		return lines[idx:end]
	raise AssertionError(f"step not found in {workflow_path}: {step_name}")


def _extract_run_script(workflow_path: Path, step_name: str) -> str:
	"""Pull a `run: |` block out of a workflow step. Mirrors
	`tests/test_implement_post_codex_recovery.py:_extract_run_script`."""
	block = _workflow_step_block(workflow_path, step_name)
	run_idx = -1
	run_indent = 0
	for i, line in enumerate(block):
		if line.strip() == "run: |":
			run_idx = i
			run_indent = len(line) - len(line.lstrip(" "))
			break
	if run_idx == -1:
		raise AssertionError(f"step has no `run: |` block: {step_name}")
	out: list[str] = []
	for line in block[run_idx + 1 :]:
		if line.strip() == "":
			out.append("")
			continue
		ind = len(line) - len(line.lstrip(" "))
		if ind <= run_indent:
			break
		prefix = " " * (run_indent + 2)
		if line.startswith(prefix):
			out.append(line[len(prefix) :])
		else:
			out.append(line.lstrip())
	return "\n".join(out).rstrip() + "\n"


def _render_repository_expression(script: str, repository: str = "owner/repo") -> str:
	rendered = re.sub(
		r"\$\{\{\s*github\.repository\s*\}\}",
		repository,
		script,
	)
	assert "${{" not in rendered, f"unrendered GitHub expression(s) in script:\n{rendered}"
	return rendered


_GH_STUB_TIMELINE = """#!/usr/bin/env python3
# Minimal `gh` stub for `gh api ... timeline ... --jq <FILTER>` only.
# Reads canned timeline JSON from MOCK_GH_TIMELINE_JSON, applies the
# caller's --jq filter via the host `jq`, and writes to stdout.
import os, sys, subprocess
args = sys.argv[1:]
if not args or args[0] != "api":
	sys.stderr.write(f"gh-stub: unsupported invocation: {args}\\n")
	sys.exit(2)
jq_filter = None
for i, a in enumerate(args):
	if a == "--jq" and i + 1 < len(args):
		jq_filter = args[i + 1]
		break
canned = os.environ.get("MOCK_GH_TIMELINE_JSON", "[]")
if jq_filter is None:
	sys.stdout.write(canned)
	sys.exit(0)
proc = subprocess.run(
	["jq", "-r", jq_filter],
	input=canned,
	capture_output=True,
	text=True,
)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
sys.exit(proc.returncode)
"""


def _install_timeline_gh_stub(bin_dir: Path) -> None:
	bin_dir.mkdir(parents=True, exist_ok=True)
	gh_path = bin_dir / "gh"
	gh_path.write_text(_GH_STUB_TIMELINE, encoding="utf-8")
	gh_path.chmod(0o755)


def _run_safety_check(
	tmp: Path,
	issue_number: str,
	timeline: list[dict],
) -> tuple[subprocess.CompletedProcess, str, str]:
	"""Extract and execute the "Safety check for existing PR" step under a
	stubbed `gh`. Returns (proc, pr_exists_file_contents, github_output_contents)."""
	bin_dir = tmp / "bin"
	_install_timeline_gh_stub(bin_dir)

	script = _render_repository_expression(
		_extract_run_script(IMPLEMENT_WORKFLOW, "Safety check for existing PR")
	)
	script_path = tmp / "safety_check.sh"
	script_path.write_text(script, encoding="utf-8")

	pr_exists_file = tmp / "existing_prs.txt"
	github_output = tmp / "github_output"
	github_output.write_text("", encoding="utf-8")

	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
	env["ISSUE_NUMBER"] = issue_number
	env["PR_EXISTS_FILE"] = str(pr_exists_file)
	env["GITHUB_OUTPUT"] = str(github_output)
	env["MOCK_GH_TIMELINE_JSON"] = json.dumps(timeline)

	proc = subprocess.run(
		["bash", str(script_path)],
		cwd=str(tmp),
		env=env,
		capture_output=True,
		text=True,
		timeout=30,
	)
	pr_exists = pr_exists_file.read_text(encoding="utf-8") if pr_exists_file.exists() else ""
	gh_out = github_output.read_text(encoding="utf-8")
	return proc, pr_exists, gh_out


def test_safety_check_skips_text_only_mention():
	"""The original failure mode: a PR whose body or comments contain the
	issue's digit string (e.g. a Copilot review comment referencing line N
	of a migration file) but does NOT cross-reference the issue must NOT
	trigger the existing-PR skip. Reproduces the bitsafe.io#141 ↔ PR#135
	stall-recovery loop. The buggy `gh pr list --search "issue:${N}"` form
	would surface PR#135 here; the timeline-based form must not."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		# Timeline contains only events that should NOT count as a linked PR:
		# - a `labeled` event (irrelevant kind)
		# - a cross-reference to a different ISSUE (`pull_request` is null)
		# Critically, no cross-reference event for PR#135 — because PR#135
		# closes a different issue and only mentions "141" inside a review
		# comment, which does not generate a timeline cross-ref event on
		# issue #141.
		timeline = [
			{"event": "labeled", "label": {"name": "ai:awaiting-approval"}},
			{
				"event": "cross-referenced",
				"source": {
					"issue": {
						"number": 999,
						"html_url": "https://github.com/owner/repo/issues/999",
						"state": "open",
						"pull_request": None,
					}
				},
			},
		]
		proc, pr_exists, gh_out = _run_safety_check(tmp, "141", timeline)
		assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
		assert pr_exists == "", f"expected empty PR_EXISTS_FILE; got: {pr_exists!r}"
		assert "existing_pr=false" in gh_out, gh_out
		assert "existing_pr=true" not in gh_out, gh_out


def test_safety_check_finds_real_cross_referenced_open_pr():
	"""A PR that genuinely cross-references the issue (e.g. via "Closes #N"
	in its body, or via a commit message — both produce a `cross-referenced`
	event whose `source.issue.pull_request` is non-null) MUST be detected
	and surfaced in PR_EXISTS_FILE."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		timeline = [
			{
				"event": "cross-referenced",
				"source": {
					"issue": {
						"number": 200,
						"html_url": "https://github.com/owner/repo/pull/200",
						"state": "open",
						"pull_request": {"url": "https://api.github.com/.../pulls/200"},
					}
				},
			},
		]
		proc, pr_exists, gh_out = _run_safety_check(tmp, "141", timeline)
		assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
		assert "#200 https://github.com/owner/repo/pull/200" in pr_exists, pr_exists
		assert "existing_pr=true" in gh_out, gh_out


def test_safety_check_dedupes_multiple_cross_refs_to_same_pr():
	"""GitHub may emit more than one `cross-referenced` event for the same
	PR (PR creation, subsequent commits referencing the issue, manual
	'Linked issues' edits). The output must list each PR once."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		shared_pr = {
			"number": 200,
			"html_url": "https://github.com/owner/repo/pull/200",
			"state": "open",
			"pull_request": {"url": "x"},
		}
		timeline = [
			{"event": "cross-referenced", "source": {"issue": shared_pr}},
			{"event": "cross-referenced", "source": {"issue": shared_pr}},
			{"event": "cross-referenced", "source": {"issue": shared_pr}},
		]
		proc, pr_exists, gh_out = _run_safety_check(tmp, "141", timeline)
		assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
		lines = [ln for ln in pr_exists.splitlines() if ln.strip()]
		assert lines == ["#200 https://github.com/owner/repo/pull/200"], lines
		assert "existing_pr=true" in gh_out, gh_out


def test_safety_check_ignores_closed_prs():
	"""A cross-referenced PR that is already CLOSED (merged or rejected) is
	not a blocking duplicate for a fresh implement run — the workflow
	must proceed. Without this filter, the existing-PR skip would loop
	forever on an issue whose previous attempt was merged but the issue
	never advanced."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		timeline = [
			{
				"event": "cross-referenced",
				"source": {
					"issue": {
						"number": 199,
						"html_url": "https://github.com/owner/repo/pull/199",
						"state": "closed",
						"pull_request": {"url": "x", "merged_at": "2026-01-01T00:00:00Z"},
					}
				},
			},
		]
		proc, pr_exists, gh_out = _run_safety_check(tmp, "141", timeline)
		assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
		assert pr_exists == "", f"closed PR must not block; got: {pr_exists!r}"
		assert "existing_pr=false" in gh_out, gh_out


def test_safety_check_ignores_cross_referenced_issues_not_prs():
	"""Cross-references to plain issues (not PRs) must be ignored — only
	`source.issue.pull_request != null` counts. Without this guard, an
	issue that got linked from another tracking issue's comment would
	trigger a skip."""
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		timeline = [
			{
				"event": "cross-referenced",
				"source": {
					"issue": {
						"number": 42,
						"html_url": "https://github.com/owner/repo/issues/42",
						"state": "open",
						"pull_request": None,
					}
				},
			},
		]
		proc, pr_exists, gh_out = _run_safety_check(tmp, "141", timeline)
		assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
		assert pr_exists == "", f"issue cross-ref must not block; got: {pr_exists!r}"
		assert "existing_pr=false" in gh_out, gh_out


def test_implement_workflow_does_not_use_buggy_search_form():
	"""Belt-and-braces: scan implement.yml for the historic buggy pattern.
	If a future change reverts to `gh pr list --search "issue:${N}"` (or any
	`--search "issue:..."` variant) on an executable line, this test fails
	with a clear pointer to the regression. The literal `issue:` substring
	is allowed inside YAML comments (those document why the form is buggy)."""
	text = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	for lineno, line in enumerate(text.splitlines(), start=1):
		stripped = line.lstrip()
		if stripped.startswith("#"):
			continue
		if "gh pr list" in stripped and '--search "issue:' in stripped:
			raise AssertionError(
				f"{IMPLEMENT_WORKFLOW.name}:{lineno} reverted to buggy "
				f'`gh pr list --search "issue:..."` form. GitHub has no '
				f"`issue:` PR-search qualifier; use the issue timeline "
				f"cross-reference API instead. See plan.yml's "
				f'"Skip when issue already has a PR" step for precedent.'
			)
