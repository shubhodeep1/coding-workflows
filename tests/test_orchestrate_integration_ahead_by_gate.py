"""Regression tests for the integration-ahead-by gate added to
``cmd_check_wave_status`` and ``finalize_integration_merge_if_needed`` to
close the silent-drift bug surfaced by ``shubhodeep1/binance-blessings#135``.

The bug, in two interacting layers:

* Layer A (Python): ``cmd_check_wave_status`` declared ``project_complete``
  purely from wave-issue merge state into the integration branch, never
  comparing the integration branch against the default branch. So the
  orchestrator could mark a project complete + validated while ``main``
  was behind by N PRs.

* Layer B (shell): ``finalize_integration_merge_if_needed`` early-returned
  whenever the state's ``final_merge_status`` was already ``"merged"`` —
  even if a subsequent wave PR had since landed on the integration branch
  and never reached default. The pin became permanent silent drift.

The tests below cover the Python gate (``--integration-ahead-by``) and the
shell-side recheck (``_integration_branch_ahead_of_default`` +
``finalize_integration_merge_if_needed``) via a sourced-function harness
with a fake ``gh`` on the PATH.

The end-to-end regression test that drives the full poll loop through
``judge=complete + wave_complete=true + ahead_by>0`` (the bitsafe.io#325
deadlock — where PR #2778's ``project_complete`` gate collided with the
pre-existing ``Hard guard: judge cannot declare "complete" while waves
remain`` override in ``scripts/orchestrate_poll_process.sh``) lives next
to the other complete-verdict poller tests in
``tests/test_orchestrate_poll_process.py::test_complete_verdict_falls_through_to_finalize_on_integration_drift``,
which is where the full poller harness (``_run_poller``) is wired up.

Each test isolates one regression and reads naturally as a description of
the contract the code now upholds.
"""

from __future__ import annotations

import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import orchestrate_lib  # noqa: E402  (path must be set above first)
# ---------------------------------------------------------------------------
# Helpers — Python (cmd_check_wave_status)
# ---------------------------------------------------------------------------

def _wave_state_two_issues_all_merged() -> dict:
	"""Single-wave state where both wave issues are merged. ``all_merged``
	resolves to True and ``current_wave_idx + 1 >= len(waves)`` holds, so
	the only knob left controlling ``project_complete`` is the new
	integration-ahead-by gate."""
	return {
		"current_wave": 1,
		"waves": [
			{
				"issues": [
					{"id": "issue-a", "github_issue": 10, "status": "merged"},
					{"id": "issue-b", "github_issue": 11, "status": "merged"},
				]
			}
		],
	}


def _run_check_wave_status(state: dict, integration_ahead_by: object | None) -> dict:
	with tempfile.NamedTemporaryFile(
		mode="w", suffix=".json", delete=False
	) as f:
		json.dump(state, f)
		state_path = f.name
	try:
		labels_json = json.dumps({"10": ["ai:merged"], "11": ["ai:merged"]})
		args_dict = {
			"state_file": state_path,
			"labels_json": labels_json,
		}
		if integration_ahead_by is not None:
			args_dict["integration_ahead_by"] = integration_ahead_by
		args_obj = type("Args", (), args_dict)()
		buf = io.StringIO()
		with redirect_stdout(buf):
			orchestrate_lib.cmd_check_wave_status(args_obj)
		return json.loads(buf.getvalue())
	finally:
		os.unlink(state_path)


# ---------------------------------------------------------------------------
# Tests — Python gate
# ---------------------------------------------------------------------------

def test_project_complete_true_when_ahead_by_zero():
	"""``--integration-ahead-by 0`` means default contains integration tip;
	project_complete must be True."""
	result = _run_check_wave_status(
		_wave_state_two_issues_all_merged(), integration_ahead_by="0"
	)
	assert result["project_complete"] is True, result
	assert result["integration_ahead_by"] == "0"
	assert result["integration_contained_in_default"] is True


def test_project_complete_true_when_ahead_by_zero_is_integer():
	"""Direct Python callers may pass a scalar 0 instead of argparse's
	string "0"; this must still mean integration is contained in default."""
	result = _run_check_wave_status(
		_wave_state_two_issues_all_merged(), integration_ahead_by=0
	)
	assert result["project_complete"] is True, result
	assert result["integration_ahead_by"] == "0"


def test_project_complete_false_when_ahead_by_nonzero():
	"""Any non-zero ahead_by forces project_complete=False so the
	orchestrator does not declare completion while wave PRs are stranded
	on the integration branch."""
	result = _run_check_wave_status(
		_wave_state_two_issues_all_merged(), integration_ahead_by="3"
	)
	assert result["project_complete"] is False, result
	assert result["integration_ahead_by"] == "3"
	assert result["integration_contained_in_default"] is False


def test_project_complete_false_when_ahead_by_empty_fail_closed():
	"""Empty string is the fail-closed sentinel callers pass when the
	GitHub compare API errored. Must be treated as "unknown → not
	complete"."""
	result = _run_check_wave_status(
		_wave_state_two_issues_all_merged(), integration_ahead_by=""
	)
	assert result["project_complete"] is False, result
	assert result["integration_contained_in_default"] is False


def test_project_complete_false_when_ahead_by_non_integer_fail_closed():
	"""Garbled value (non-numeric) must also fail closed."""
	result = _run_check_wave_status(
		_wave_state_two_issues_all_merged(),
		integration_ahead_by="api-error",
	)
	assert result["project_complete"] is False, result


def test_project_complete_backward_compat_when_arg_absent():
	"""Callers from before the gate landed do not pass --integration-
	ahead-by. argparse's default ("0") preserves prior semantics so
	existing call sites keep working until they are migrated."""
	result = _run_check_wave_status(
		_wave_state_two_issues_all_merged(), integration_ahead_by=None
	)
	assert result["project_complete"] is True, result


def test_project_complete_false_if_waves_not_all_merged_regardless_of_ahead_by():
	"""ahead_by=0 only unblocks the new gate; the pre-existing
	``all_merged`` precondition is still required. Otherwise an
	ahead_by=0 + half-done wave could declare a project complete, which
	would be a regression in the opposite direction."""
	state = _wave_state_two_issues_all_merged()
	state["waves"][0]["issues"][1]["status"] = "open"
	with tempfile.NamedTemporaryFile(
		mode="w", suffix=".json", delete=False
	) as f:
		json.dump(state, f)
		state_path = f.name
	try:
		labels_json = json.dumps({"10": ["ai:merged"], "11": []})
		args_obj = type(
			"Args",
			(),
			{
				"state_file": state_path,
				"labels_json": labels_json,
				"integration_ahead_by": "0",
			},
		)()
		buf = io.StringIO()
		with redirect_stdout(buf):
			orchestrate_lib.cmd_check_wave_status(args_obj)
		result = json.loads(buf.getvalue())
		assert result["project_complete"] is False, result
		assert result["wave_complete"] is False
	finally:
		os.unlink(state_path)


# ---------------------------------------------------------------------------
# Helpers — shell (_integration_branch_ahead_of_default,
#                   finalize_integration_merge_if_needed re-check)
# ---------------------------------------------------------------------------

POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
GH_HELPERS_SCRIPT = REPO_ROOT / "scripts" / "gh_helpers.sh"


def _make_gh_stub(
	tmp: Path,
	*,
	ahead_by_response: str | None,
	commit_parent_counts: list[int] | None = None,
) -> Path:
	"""Write a fake ``gh`` to ``tmp/bin/gh`` that responds to the calls our
	tests exercise.

	``ahead_by_response`` controls the compare-API mock:
	* ``"<integer>"``: respond with ``{"ahead_by": <integer>}`` (success)
	* ``"error"``: exit non-zero, simulating a compare API failure
	* ``None``: should never be called; emit a diagnostic and fail

	``commit_parent_counts`` optionally adds a ``commits`` list to the
	compare payload, one entry per integer giving that commit's parent
	count (1 = squash/regular commit, 2 = merge commit). ``None`` omits
	the key entirely, which the helper must treat as "work count unknown".
	Any ``--jq`` filter other than the legacy ``.ahead_by`` is evaluated
	with the real ``jq`` against the full payload.
	"""
	bin_dir = tmp / "bin"
	bin_dir.mkdir(parents=True, exist_ok=True)
	gh_path = bin_dir / "gh"
	calls_path = tmp / "gh_calls.jsonl"
	state_path = tmp / "gh_state.json"
	state_path.write_text(
		json.dumps(
			{
				"ahead_by_response": ahead_by_response,
				"commit_parent_counts": commit_parent_counts,
				"calls_path": str(calls_path),
			}
		),
		encoding="utf-8",
	)

	gh_script = f'''#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

state = json.loads(Path({str(state_path)!r}).read_text(encoding="utf-8"))
calls_path = Path(state["calls_path"])
args = sys.argv[1:]
with calls_path.open("a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": args}}) + "\\n")


if args and args[0] == "api":
    path = None
    jq = None
    it = iter(args[1:])
    for tok in it:
        if tok == "--jq":
            jq = next(it, None)
        elif tok == "--paginate":
            continue
        elif tok.startswith("-"):
            next(it, None)
        else:
            if path is None:
                path = tok
    if path and re.match(r"^repos/[^/]+/[^/]+/compare/.+\\.\\.\\..+$", path):
        resp = state.get("ahead_by_response")
        if resp == "error":
            sys.stderr.write("simulated compare API error\\n")
            sys.exit(22)
        try:
            ahead = int(resp)
        except (TypeError, ValueError):
            sys.stderr.write("test bug: ahead_by_response not int/error: %r\\n" % (resp,))
            sys.exit(99)
        payload = {{"ahead_by": ahead, "behind_by": 0}}
        parent_counts = state.get("commit_parent_counts")
        if parent_counts is not None:
            payload["commits"] = [
                {{"sha": "c%04d" % idx, "parents": [{{"sha": "p%d" % j}} for j in range(int(n))]}}
                for idx, n in enumerate(parent_counts)
            ]
        if jq and jq.strip() == ".ahead_by":
            sys.stdout.write(str(ahead))
        elif jq:
            import subprocess
            p = subprocess.run(["jq", "-r", jq], input=json.dumps(payload), capture_output=True, text=True)
            sys.stdout.write(p.stdout)
            sys.exit(p.returncode)
        else:
            sys.stdout.write(json.dumps(payload))
        sys.exit(0)

    if path and re.match(r"^repos/[^/]+/[^/]+$", path):
        if jq and ".default_branch" in jq:
            sys.stdout.write("main")
        else:
            sys.stdout.write(json.dumps({{"default_branch": "main"}}))
        sys.exit(0)

    m = re.match(r"^repos/[^/]+/[^/]+/pulls/(\\d+)$", path or "")
    if m:
        pr_num = int(m.group(1))
        pr_state = state.get("pr_states", {{}}).get(str(pr_num), {{}})
        if not pr_state:
            sys.stderr.write("test bug: no pr_state for %d\\n" % pr_num)
            sys.exit(99)
        if jq:
            if jq.strip() == ".state":
                sys.stdout.write(pr_state.get("state", ""))
            elif jq.strip() == ".merged_at != null":
                sys.stdout.write("true" if pr_state.get("merged") else "false")
            else:
                sys.stderr.write("unhandled jq for pulls: %r\\n" % jq)
                sys.exit(99)
        else:
            sys.stdout.write(json.dumps(pr_state))
        sys.exit(0)

    m = re.match(r"^repos/[^/]+/[^/]+/git/ref/heads/(.+)$", path or "")
    if m:
        branch_ref = m.group(1)
        if branch_ref in (state.get("missing_branch", ""), state.get("missing_branch_uri", "")):
            sys.stderr.write("gh: Not Found\\n")
            sys.exit(1)
        sys.stdout.write(json.dumps({{"ref": "refs/heads/" + branch_ref}}))
        sys.exit(0)

    sys.stderr.write("unhandled gh api path: %r\\n" % (path,))
    sys.exit(99)

sys.stderr.write("unhandled gh invocation: %r\\n" % (args,))
sys.exit(99)
'''
	gh_path.write_text(gh_script, encoding="utf-8")
	gh_path.chmod(0o755)
	return gh_path


def _update_gh_state(tmp: Path, **patch) -> None:
	state_path = tmp / "gh_state.json"
	state = json.loads(state_path.read_text(encoding="utf-8"))
	state.update(patch)
	state_path.write_text(json.dumps(state), encoding="utf-8")


def _read_gh_calls(tmp: Path) -> list[dict]:
	calls_path = tmp / "gh_calls.jsonl"
	if not calls_path.exists():
		return []
	return [json.loads(line) for line in calls_path.read_text().splitlines() if line]


def _run_shell(
	tmp: Path,
	body: str,
	*,
	state_blob: dict | None = None,
) -> subprocess.CompletedProcess:
	state_file = tmp / "state.json"
	if state_blob is not None:
		state_file.write_text(json.dumps(state_blob), encoding="utf-8")
	runtime_dir = tmp / "runtime"
	runtime_dir.mkdir(exist_ok=True)
	# Empty tracking_issues.json so the script's top-level "for" loop
	# iterates 0 times and we can source it without triggering live work.
	(runtime_dir / "tracking_issues.json").write_text("[]", encoding="utf-8")

	env = os.environ.copy()
	env["PATH"] = f"{tmp / 'bin'}:" + env.get("PATH", "")
	env["GITHUB_REPOSITORY"] = "test-owner/test-repo"
	env["STATE_FILE"] = str(state_file)
	env["TRACKING_NUM"] = "192"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["SUPPORT_SCRIPTS_DIR"] = str(REPO_ROOT / "scripts")
	# Speed up: gh_retry's exponential backoff would slow tests on simulated
	# errors. One attempt is enough — the helper still reports failure.
	env["GH_RETRY_MAX_ATTEMPTS"] = "1"

	preamble = textwrap.dedent(
		f"""
		# Relax `set -u` while sourcing the poller — it has top-level code
		# that touches optional env vars; we only want function definitions
		# from it for these tests.
		set -e
		set +u
		# Redirect the poller's top-level side effects (standalone-recovery
		# sweeps, etc.) to stderr so the test body's stdout stays clean for
		# assertions against the function under test. fd 3 keeps a handle on
		# the real stdout to restore after the source completes.
		exec 3>&1
		exec 1>&2
		source {str(GH_HELPERS_SCRIPT)!r}
		post_state_comment() {{ :; }}
		post_tracking_comment() {{ :; }}
		mark_integration_branch_missing_failed() {{ :; }}
		source {str(POLLER_SCRIPT)!r}
		exec 1>&3
		exec 3>&-
		set -u
		"""
	)
	return subprocess.run(
		["bash", "-c", preamble + body],
		env=env,
		capture_output=True,
		text=True,
		timeout=60,
	)


# ---------------------------------------------------------------------------
# Tests — shell helper _integration_branch_ahead_of_default
# ---------------------------------------------------------------------------

def test_helper_work_commit_side_output_excludes_merge_commits(tmp_path):
	"""The tele-funtoken-msg-scoring#3928 shape: ahead_by=26 of which 15 are
	the poller's own sync-merge commits. The OUTVAR form must assign the raw
	ahead_by to the named variable AND set INTEGRATION_AHEAD_BY_WORK_COMMITS
	to the non-merge count from the same compare call."""
	_make_gh_stub(tmp_path, ahead_by_response="26", commit_parent_counts=[1] * 11 + [2] * 15)
	result = _run_shell(
		tmp_path,
		'_integration_branch_ahead_of_default "orchestrator/project-192" "main" probe_ahead; '
		'echo "ahead=${probe_ahead} work=${INTEGRATION_AHEAD_BY_WORK_COMMITS}"',
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "ahead=26 work=11"
	compare_calls = [c for c in _read_gh_calls(tmp_path) if "/compare/" in " ".join(c["argv"])]
	assert len(compare_calls) == 1, "work count must come from the single existing compare call"


def test_helper_work_commit_side_output_unknown_when_commits_absent(tmp_path):
	"""A compare payload without a `commits` list (older fixtures, partial
	API responses) leaves the work count unknown — callers fall back to the
	raw ahead_by — while the echoed ahead_by is unchanged."""
	_make_gh_stub(tmp_path, ahead_by_response="7")
	result = _run_shell(
		tmp_path,
		'_integration_branch_ahead_of_default "orchestrator/project-192" "main" probe_ahead; '
		'echo "ahead=${probe_ahead} work=[${INTEGRATION_AHEAD_BY_WORK_COMMITS}]"',
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "ahead=7 work=[]"


def test_helper_work_commit_side_output_unknown_when_commits_truncated(tmp_path):
	"""GitHub caps compare `commits` at 250; a list shorter than ahead_by
	must not be trusted for the non-merge count."""
	_make_gh_stub(tmp_path, ahead_by_response="30", commit_parent_counts=[1] * 12)
	result = _run_shell(
		tmp_path,
		'_integration_branch_ahead_of_default "orchestrator/project-192" "main" probe_ahead; '
		'echo "ahead=${probe_ahead} work=[${INTEGRATION_AHEAD_BY_WORK_COMMITS}]"',
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "ahead=30 work=[]"


def test_helper_legacy_echo_form_still_prints_ahead_by(tmp_path):
	"""Command-substitution callers (finalize / check-wave paths) keep the
	stdout contract: exactly the ahead_by integer, nothing else."""
	_make_gh_stub(tmp_path, ahead_by_response="4", commit_parent_counts=[1, 2, 1, 2])
	result = _run_shell(
		tmp_path,
		'value="$(_integration_branch_ahead_of_default "orchestrator/project-192" "main")"; echo "[${value}]"',
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "[4]"


def test_helper_returns_ahead_by_count_on_api_success(tmp_path):
	_make_gh_stub(tmp_path, ahead_by_response="5")
	result = _run_shell(
		tmp_path,
		'_integration_branch_ahead_of_default "orchestrator/project-192" "main"',
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "5"


def test_helper_returns_zero_when_branches_match(tmp_path):
	# Same branch → trivially ahead_by=0, no API call.
	_make_gh_stub(tmp_path, ahead_by_response=None)
	result = _run_shell(
		tmp_path,
		'_integration_branch_ahead_of_default "main" "main"',
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "0"
	# Crucially, no compare API call was issued for the same-branch case.
	calls = _read_gh_calls(tmp_path)
	assert not any(
		any("/compare/" in tok for tok in c["argv"]) for c in calls
	), calls


def test_helper_fails_closed_on_api_error(tmp_path):
	_make_gh_stub(tmp_path, ahead_by_response="error")
	result = _run_shell(
		tmp_path,
		'_integration_branch_ahead_of_default "orchestrator/project-192" "main" '
		'|| echo "FAIL-CLOSED rc=$?"',
	)
	assert result.returncode == 0, result.stderr
	assert "FAIL-CLOSED" in result.stdout, result.stdout


def test_helper_returns_zero_when_branch_was_deleted(tmp_path):
	"""When the integration branch has been deleted (legitimate post-
	merge state after `gh pr merge --delete-branch`), the helper must
	return 0 without trying compare-API drift detection. Otherwise the
	caller's pinned ``final_merge_status=merged`` state would be
	cleared in steady-state after every successful finalize."""
	# ahead_by_response="0" — but we set the branch existence probe to
	# 404 first. The helper should short-circuit on the missing branch
	# without ever hitting the compare endpoint.
	_make_gh_stub(tmp_path, ahead_by_response="0")
	# Override the branch existence probe to return 404 for the
	# integration branch.
	state_path = tmp_path / "gh_state.json"
	state = json.loads(state_path.read_text())
	# The branch name appears URL-percent-encoded in the git/ref/heads
	# request, so we store the encoded form too — the integration_branch_
	# exists helper uses jq's @uri filter.
	state["missing_branch"] = "orchestrator/project-192"
	state["missing_branch_uri"] = "orchestrator%2Fproject-192"
	state_path.write_text(json.dumps(state))
	result = _run_shell(
		tmp_path,
		'_integration_branch_ahead_of_default "orchestrator/project-192" "main"',
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "0"
	# Verify no compare API call was issued — the short-circuit must
	# precede compare.
	calls = _read_gh_calls(tmp_path)
	compare_calls = [c for c in calls if any("/compare/" in tok for tok in c["argv"])]
	assert compare_calls == [], compare_calls


# ---------------------------------------------------------------------------
# Tests — finalize_integration_merge_if_needed re-check (Fix 3)
# ---------------------------------------------------------------------------

def _finalize_state(*, final_merge_status: str, final_merge_pr: int | None) -> dict:
	state: dict = {
		"integration_branch": "orchestrator/project-192",
		"final_merge_status": final_merge_status,
		"sync": {"status": "active"},
	}
	if final_merge_pr is not None:
		state["final_merge_pr"] = final_merge_pr
	return state


def test_finalize_returns_clean_when_pinned_merged_and_branch_contained(tmp_path):
	"""Baseline: state pinned to ``merged`` AND integration is fully
	contained in default (ahead_by=0). Function returns 0 without
	clearing the pin — the legitimate steady state once a project really
	is done."""
	_make_gh_stub(tmp_path, ahead_by_response="0")
	state = _finalize_state(final_merge_status="merged", final_merge_pr=146)
	result = _run_shell(
		tmp_path,
		'finalize_integration_merge_if_needed "orchestrator/project-192" "main" "Test project"; '
		'echo "RC=$?"; jq -c . "${STATE_FILE}"',
		state_blob=state,
	)
	assert result.returncode == 0, result.stderr
	assert "RC=0" in result.stdout
	stdout_lines = result.stdout.splitlines()
	json_line = next(
		line for line in reversed(stdout_lines) if line.startswith("{")
	)
	final_state = json.loads(json_line)
	assert final_state["final_merge_status"] == "merged"
	assert final_state["final_merge_pr"] == 146


def test_finalize_clears_pin_when_integration_ahead_of_default(tmp_path):
	"""Regression case from binance-blessings#135: state pinned to
	``merged`` (because review_autofix's auto-merge fired early), but a
	subsequent wave PR has since landed on the integration branch and
	never reached default. The function must clear the pin so the
	follow-on PR-discovery / PR-creation path can run for the new diff
	on the same tick (or the next one if the recreate API call fails).

	The assertions intentionally check ONLY the pin-clearing contract —
	final_merge_status is no longer ``merged`` and final_merge_pr is no
	longer the stale 146 — because the function then falls through to
	the existing PR-discovery / recreate path, which writes its own
	status / error messages depending on whether `gh pr list` and
	`gh pr create` succeed. Verifying the recreate-path output is the
	job of the existing test_final_merge_* coverage; this test owns the
	Fix 3 contract: a pinned "merged" state with integration ahead of
	default must NOT be treated as terminal.
	"""
	_make_gh_stub(tmp_path, ahead_by_response="2")
	state = _finalize_state(final_merge_status="merged", final_merge_pr=146)
	result = _run_shell(
		tmp_path,
		'finalize_integration_merge_if_needed "orchestrator/project-192" "main" "Test project" '
		'|| echo "  (function returned non-zero; expected when the recreate path needs follow-up)"; '
		'jq -c . "${STATE_FILE}"',
		state_blob=state,
	)
	assert result.returncode == 0, result.stderr
	stdout_lines = result.stdout.splitlines()
	json_line = next(
		line for line in reversed(stdout_lines) if line.startswith("{")
	)
	final_state = json.loads(json_line)
	# Pin must NOT be terminal anymore — that is what makes the next
	# tick reopen the final PR for the unmerged diff.
	assert final_state["final_merge_status"] != "merged", final_state
	# The stale recorded PR (146) must be gone so the recreate path can
	# discover or open a fresh one.
	assert final_state.get("final_merge_pr") != 146, final_state


def test_finalize_clears_pin_fail_closed_on_compare_api_error(tmp_path):
	"""Fail-closed posture: when the compare API errors, the function
	cannot prove that default contains the integration tip. Clear the
	pin so the next tick can recreate the PR — better to do one extra
	merge cycle than to leave silent drift in place. Like the
	ahead-of-default test above, we only assert the pin-clearing
	contract since the function then falls through to the existing
	recreate path which writes its own status / error messages."""
	_make_gh_stub(tmp_path, ahead_by_response="error")
	state = _finalize_state(final_merge_status="merged", final_merge_pr=146)
	result = _run_shell(
		tmp_path,
		'finalize_integration_merge_if_needed "orchestrator/project-192" "main" "Test project" '
		'|| echo "  (function returned non-zero when compare API errored — fail-closed clears the pin and forces a recreate path)"; '
		'jq -c . "${STATE_FILE}"',
		state_blob=state,
	)
	assert result.returncode == 0, result.stderr
	stdout_lines = result.stdout.splitlines()
	json_line = next(
		line for line in reversed(stdout_lines) if line.startswith("{")
	)
	final_state = json.loads(json_line)
	assert final_state["final_merge_status"] != "merged", final_state
	assert final_state.get("final_merge_pr") != 146, final_state


def test_finalize_skips_recheck_when_superseded_by_main(tmp_path):
	"""``superseded-by-main`` is a deliberate terminal state — the
	integration branch was abandoned because default moved ahead during
	a sync. The re-check must NOT clear that pin."""
	# Set ahead_by to a value that would normally clear a "merged" pin
	# (5). If the function incorrectly re-checks for superseded-by-main
	# too, this would mutate the state.
	_make_gh_stub(tmp_path, ahead_by_response="5")
	state = _finalize_state(
		final_merge_status="superseded-by-main", final_merge_pr=None
	)
	state["sync"] = {"status": "superseded-by-main"}
	result = _run_shell(
		tmp_path,
		'finalize_integration_merge_if_needed "orchestrator/project-192" "main" "Test project"; '
		'echo "RC=$?"; jq -c . "${STATE_FILE}"',
		state_blob=state,
	)
	assert result.returncode == 0, result.stderr
	assert "RC=0" in result.stdout
	stdout_lines = result.stdout.splitlines()
	json_line = next(
		line for line in reversed(stdout_lines) if line.startswith("{")
	)
	final_state = json.loads(json_line)
	assert final_state["final_merge_status"] == "superseded-by-main"

def _invoke_test(func) -> None:
	params = list(inspect.signature(func).parameters)
	if not params:
		func()
		return
	if params == ["tmp_path"]:
		with tempfile.TemporaryDirectory(prefix=f"{func.__name__}_") as td:
			func(Path(td))
		return
	raise TypeError(f"unsupported direct-run parameters: {params}")


def main(argv: list[str] | None = None) -> int:
	"""Direct ``python3 tests/<file>.py`` entrypoint.

	The repo's CI/release allowlists execute tests via explicit
	``python3 tests/test_*.py`` calls rather than pytest discovery, so this
	module needs a small fixture shim for the ``tmp_path``-style tests.
	"""
	selected_names = list(sys.argv[1:] if argv is None else argv)
	try:
		sys.stdout.reconfigure(line_buffering=True)
	except Exception:
		pass

	tests_by_name = {
		name: func
		for name, func in sorted(globals().items())
		if name.startswith("test_") and callable(func)
	}
	if selected_names:
		missing = [name for name in selected_names if name not in tests_by_name]
		for name in missing:
			print(f"  FAIL  {name}: unknown test name", flush=True)
		if missing:
			return 1
		named_tests = [(name, tests_by_name[name]) for name in selected_names]
	else:
		named_tests = list(tests_by_name.items())
	passed = 0
	failed = 0
	for name, func in named_tests:
		try:
			_invoke_test(func)
			print(f"  PASS  {name}", flush=True)
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}", flush=True)
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
