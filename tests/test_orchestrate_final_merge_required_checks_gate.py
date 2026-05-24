"""Regression tests for Layers 1 and 2 of the final-merge gate hardening in
``scripts/orchestrate_poll_process.sh``.

Layer 1 — required-checks scope (`_pr_checks_completed`,
``_pr_required_check_names_for_base``). The pre-Layer-1 gate treated every
check-run on the head SHA as blocking, regardless of whether GitHub
branch protection actually required it. A single failing third-party
advisory check (e.g. Copilot's "Running Copilot Code Review" on PR #2955)
silently stalled the orchestrator for hours with the merge counter
suppressed by ``FINAL_MERGE_BUDGET_ELIGIBLE=0``. Layer 1 narrows the gate
to (1) branch protection's ``required_status_checks.contexts``, falling
back to (2) the operator allowlist ``ORCH_FINAL_MERGE_REQUIRED_CHECKS``,
falling back to (3) the built-in default. Sentinels ``*`` and ``""``
restore legacy fail-closed and allow-all respectively.

Layer 2 — time-bounded ineligibility alert
(``_check_final_merge_ineligibility_alert``,
``_summarize_final_merge_blockers``,
``_clear_final_merge_ineligibility_state``). The budget-ineligible
deferral path in ``mark_validation_complete`` previously logged
``[final-merge] budget-ineligible deferral/failure`` once per poll cycle
and otherwise stayed silent. Layer 2 attaches a per-SHA clock; the first
poll cycle to observe the same head SHA stuck past
``ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS`` hours fires a CRITICAL
Telegram alert plus tracking comment. The alert is per-SHA-idempotent
(a fresh autofix push resets the clock).

The harness sources the production poller script with a fake ``gh`` on
PATH and exercises the functions directly. Patterns mirror
``tests/test_orchestrate_integration_ahead_by_gate.py``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
GH_HELPERS_SCRIPT = REPO_ROOT / "scripts" / "gh_helpers.sh"


# ---------------------------------------------------------------------------
# gh stub
# ---------------------------------------------------------------------------

def _make_gh_stub(tmp: Path, *, gh_state: dict) -> Path:
	"""Write a fake ``gh`` to ``tmp/bin/gh`` that mocks the API calls the
	gate functions issue: PR metadata, branch protection, check-runs.

	``gh_state`` shape:
		{
			"pr": {<pr_num>: {"head_sha": str, "base_ref": str}},
			"protection": {<branch>: {"required_status_checks_contexts": [str]}
			               | "404"},
			"check_runs": {<head_sha>: [
			    {"name": str, "status": str, "conclusion": str|null}
			]},
		}
	"""
	bin_dir = tmp / "bin"
	bin_dir.mkdir(parents=True, exist_ok=True)
	gh_path = bin_dir / "gh"
	calls_path = tmp / "gh_calls.jsonl"
	state_path = tmp / "gh_state.json"
	state_path.write_text(
		json.dumps({"gh": gh_state, "calls_path": str(calls_path)}),
		encoding="utf-8",
	)

	gh_script = f'''#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

state_blob = json.loads(Path({str(state_path)!r}).read_text(encoding="utf-8"))
state = state_blob["gh"]
calls_path = Path(state_blob["calls_path"])
args = sys.argv[1:]
with calls_path.open("a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": args}}) + "\\n")

if not args or args[0] != "api":
    sys.stderr.write("unhandled gh invocation: %r\\n" % (args,))
    sys.exit(99)

# Parse the api invocation.
path = None
jq = None
paginate = False
it = iter(args[1:])
for tok in it:
    if tok == "--jq":
        jq = next(it, None)
    elif tok == "--paginate":
        paginate = True
    elif tok == "--slurp":
        continue
    elif tok.startswith("-"):
        next(it, None)
    else:
        if path is None:
            path = tok

# repos/<owner>/<repo>/pulls/<n>
m = re.match(r"^repos/[^/]+/[^/]+/pulls/(\\d+)$", path or "")
if m:
    pr_num = int(m.group(1))
    pr = state.get("pr", {{}}).get(str(pr_num)) or state.get("pr", {{}}).get(pr_num)
    if not pr:
        sys.stderr.write("HTTP 404: pull %d not found\\n" % pr_num)
        sys.exit(1)
    body = {{"number": pr_num,
            "head": {{"sha": pr["head_sha"]}},
            "base": {{"ref": pr["base_ref"]}}}}
    if jq:
        j = jq.strip()
        if j == ".head.sha":
            sys.stdout.write(pr["head_sha"])
        elif j == ".base.ref":
            sys.stdout.write(pr["base_ref"])
        else:
            sys.stdout.write(json.dumps(body))
    else:
        sys.stdout.write(json.dumps(body))
    sys.exit(0)

# repos/<owner>/<repo>/branches/<branch>/protection
m = re.match(r"^repos/[^/]+/[^/]+/branches/(.+)/protection$", path or "")
if m:
    branch = m.group(1)
    prot = state.get("protection", {{}}).get(branch)
    if prot is None or prot == "404":
        sys.stderr.write("HTTP 404: Branch not protected\\n")
        sys.exit(1)
    body = {{"required_status_checks":
             {{"contexts": prot.get("required_status_checks_contexts", [])}}}}
    sys.stdout.write(json.dumps(body))
    sys.exit(0)

# repos/<owner>/<repo>/commits/<sha>/check-runs
m = re.match(r"^repos/[^/]+/[^/]+/commits/([^/]+)/check-runs.*$", path or "")
if m:
    sha = m.group(1)
    runs = state.get("check_runs", {{}}).get(sha, [])
    if paginate:
        body = [{{"total_count": len(runs), "check_runs": runs}}]
    else:
        body = {{"total_count": len(runs), "check_runs": runs}}
    sys.stdout.write(json.dumps(body))
    sys.exit(0)

# repos/<owner>/<repo>  (default_branch lookup from the poller's top-level
# standalone sweeps — return a minimal repo body so sourcing doesn't error.)
m = re.match(r"^repos/[^/]+/[^/]+$", path or "")
if m:
    body = {{"default_branch": "main"}}
    if jq:
        j = jq.strip()
        if j == ".default_branch":
            sys.stdout.write("main")
        else:
            sys.stdout.write(json.dumps(body))
    else:
        sys.stdout.write(json.dumps(body))
    sys.exit(0)

sys.stderr.write("unhandled gh api path: %r\\n" % (path,))
sys.exit(99)
'''
	gh_path.write_text(gh_script, encoding="utf-8")
	gh_path.chmod(0o755)
	return gh_path


def _read_gh_calls(tmp: Path) -> list[dict]:
	calls_path = tmp / "gh_calls.jsonl"
	if not calls_path.exists():
		return []
	return [json.loads(line) for line in calls_path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Shell harness
# ---------------------------------------------------------------------------

def _run_shell(
	tmp: Path,
	body: str,
	*,
	state_blob: dict | None = None,
	env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
	state_file = tmp / "state.json"
	if state_blob is not None:
		state_file.write_text(json.dumps(state_blob), encoding="utf-8")
	else:
		state_file.write_text("{}", encoding="utf-8")

	runtime_dir = tmp / "runtime"
	runtime_dir.mkdir(exist_ok=True)
	(runtime_dir / "tracking_issues.json").write_text("[]", encoding="utf-8")

	env = os.environ.copy()
	env["PATH"] = f"{tmp / 'bin'}:" + env.get("PATH", "")
	env["GITHUB_REPOSITORY"] = "test-owner/test-repo"
	env["STATE_FILE"] = str(state_file)
	env["TRACKING_NUM"] = "192"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["SUPPORT_SCRIPTS_DIR"] = str(REPO_ROOT / "scripts")
	env["GH_RETRY_MAX_ATTEMPTS"] = "1"
	# Capture tg_notify / post_tracking_comment side-effects via tmp files.
	env["TG_NOTIFY_OUT"] = str(tmp / "tg_notify.log")
	env["POST_TRACKING_OUT"] = str(tmp / "tracking_comments.log")
	if env_overrides:
		env.update(env_overrides)

	preamble = textwrap.dedent(
		f"""
		set -e
		set +u
		exec 3>&1
		exec 1>&2
		source {str(GH_HELPERS_SCRIPT)!r}
		# Stub out side-effecting helpers so the test harness can record calls
		# without needing real GitHub / Telegram. Stubs are redefined AFTER
		# sourcing the poller (which would otherwise install the real versions).
		_install_test_stubs() {{
		    tg_notify() {{
		        printf '%s\\n---\\n' "$1" >> "${{TG_NOTIFY_OUT}}"
		    }}
		    tg_send_msg() {{ :; }}
		    tg_send_tracked() {{ :; }}
		    tg_cleanup_msgs() {{ :; }}
		    post_tracking_comment() {{
		        printf '%s\\n---\\n' "$1" >> "${{POST_TRACKING_OUT}}"
		    }}
		    post_state_comment() {{ :; }}
		    mark_integration_branch_missing_failed() {{ :; }}
		    _gh_url() {{ echo "https://github.com/test-owner/test-repo/$1"; }}
		}}
		# Pre-source stubs so the poller's top-level code doesn't try to
		# reach Telegram.
		_install_test_stubs
		source {str(POLLER_SCRIPT)!r}
		# Re-install stubs to override the real implementations from the
		# sourced poller.
		_install_test_stubs
		exec 1>&3
		exec 3>&-
		set -u
		# Disable errexit for the test body so the body can inspect exit
		# codes of the function under test (e.g. `_pr_checks_completed N;
		# echo "EXIT=$?"`) without bash aborting at the failed call.
		set +e
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
# Layer 1 tests — _pr_required_check_names_for_base
# ---------------------------------------------------------------------------

def test_required_names_uses_branch_protection_contexts_when_present(tmp_path):
	_make_gh_stub(tmp_path, gh_state={
		"protection": {
			"main": {"required_status_checks_contexts": ["lint", "tests/integration"]},
		},
	})
	result = _run_shell(
		tmp_path,
		'_pr_required_check_names_for_base "main"',
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "lint,tests/integration"


def test_required_names_falls_back_to_env_var_when_branch_unprotected(tmp_path):
	_make_gh_stub(tmp_path, gh_state={
		"protection": {"main": "404"},
	})
	result = _run_shell(
		tmp_path,
		'_pr_required_check_names_for_base "main"',
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI,lint"},
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "CI,lint"


def test_required_names_uses_built_in_default_when_var_unset(tmp_path):
	_make_gh_stub(tmp_path, gh_state={
		"protection": {"main": "404"},
	})
	# Important: the env-var declaration at script top happens at source time,
	# so we need to unset ORCH_FINAL_MERGE_REQUIRED_CHECKS BEFORE sourcing.
	# The harness sources the script; pass an explicit override.
	result = _run_shell(
		tmp_path,
		# Re-trigger the env-var declaration block to simulate "var was never
		# set in the runtime environment". `unset` here unsets the assigned
		# value (which was set by sourcing the script with the default), then
		# re-evaluates the resolution: unset → DEFAULT.
		'unset ORCH_FINAL_MERGE_REQUIRED_CHECKS; '
		'ORCH_FINAL_MERGE_REQUIRED_CHECKS="${ORCH_FINAL_MERGE_REQUIRED_CHECKS-${ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT}}"; '
		'_pr_required_check_names_for_base "main"',
	)
	assert result.returncode == 0, result.stderr
	expected_default = ("CI,Integration PR readiness check,"
		"Lint plan-archival completeness,"
		"Lint PR body for auto-close keywords against orchestrator-tracking issues,"
		"review / gate")
	assert result.stdout.strip() == expected_default


def test_required_names_preserves_explicit_empty_for_allow_all(tmp_path):
	_make_gh_stub(tmp_path, gh_state={
		"protection": {"main": "404"},
	})
	result = _run_shell(
		tmp_path,
		'_pr_required_check_names_for_base "main"; echo "EXIT=$?"',
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""},
	)
	assert result.returncode == 0, result.stderr
	# Explicit empty must remain empty (allow-all sentinel).
	assert result.stdout.strip().endswith("EXIT=0")
	# Strip the "EXIT=0" trailer; verify the helper produced an empty line.
	helper_line, _, _ = result.stdout.partition("EXIT=")
	assert helper_line.strip() == ""


def test_required_names_branch_protection_with_empty_contexts_falls_back(tmp_path):
	# When required_status_checks.contexts is an empty array (branch protected
	# but with no required checks), Layer 1 must NOT treat that as "use the
	# server-side empty list" (which would silently allow-all); it must fall
	# back to the env var.
	_make_gh_stub(tmp_path, gh_state={
		"protection": {"main": {"required_status_checks_contexts": []}},
	})
	result = _run_shell(
		tmp_path,
		'_pr_required_check_names_for_base "main"',
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI"},
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "CI"


# ---------------------------------------------------------------------------
# Layer 1 tests — _pr_checks_completed
# ---------------------------------------------------------------------------

def test_gate_ignores_failing_advisory_check_not_in_required_set(tmp_path):
	"""The exact bitsafe/PR #2955 scenario: a non-required Copilot review
	conclusion=failure must NOT block the merge when an explicit allowlist
	is in effect."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "deadbeefcafe", "base_ref": "main"}},
		"protection": {"main": "404"},
		"check_runs": {"deadbeefcafe": [
			{"name": "CI", "status": "completed", "conclusion": "success"},
			{"name": "review / gate", "status": "completed", "conclusion": "success"},
			{"name": "Running Copilot Code Review", "status": "completed",
			 "conclusion": "failure"},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_pr_checks_completed 2955 "" "main"; echo "EXIT=$?"',
		env_overrides={
			"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI,review / gate",
		},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=0" in result.stdout


def test_gate_blocks_failing_required_check_in_set(tmp_path):
	"""Required check failing must still block."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "deadbeefcafe", "base_ref": "main"}},
		"protection": {"main": "404"},
		"check_runs": {"deadbeefcafe": [
			{"name": "CI", "status": "completed", "conclusion": "failure"},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_pr_checks_completed 2955 "" "main"; echo "EXIT=$?"',
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI"},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=1" in result.stdout


def test_gate_blocks_pending_required_check(tmp_path):
	"""Required check still in-progress must block (not just failures)."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "deadbeefcafe", "base_ref": "main"}},
		"protection": {"main": "404"},
		"check_runs": {"deadbeefcafe": [
			{"name": "CI", "status": "in_progress", "conclusion": None},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_pr_checks_completed 2955 "" "main"; echo "EXIT=$?"',
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI"},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=1" in result.stdout


def test_gate_branch_protection_contexts_win_over_env_var(tmp_path):
	"""When branch protection has explicit required contexts, the env var
	allowlist is ignored — server-side truth wins."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "deadbeefcafe", "base_ref": "main"}},
		"protection": {"main": {"required_status_checks_contexts": [
			"Running Copilot Code Review",
		]}},
		"check_runs": {"deadbeefcafe": [
			{"name": "CI", "status": "completed", "conclusion": "success"},
			{"name": "Running Copilot Code Review", "status": "completed",
			 "conclusion": "failure"},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_pr_checks_completed 2955 "" "main"; echo "EXIT=$?"',
		# Env var explicitly removes Copilot; protection re-includes it.
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI"},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=1" in result.stdout  # branch-protection Copilot failure blocks


def test_gate_star_sentinel_restores_legacy_fail_closed_behavior(tmp_path):
	"""ORCH_FINAL_MERGE_REQUIRED_CHECKS=* must block on ANY failing
	check-run — the pre-Layer-1 behaviour."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "deadbeefcafe", "base_ref": "main"}},
		"protection": {"main": "404"},
		"check_runs": {"deadbeefcafe": [
			{"name": "CI", "status": "completed", "conclusion": "success"},
			{"name": "Running Copilot Code Review", "status": "completed",
			 "conclusion": "failure"},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_pr_checks_completed 2955 "" "main"; echo "EXIT=$?"',
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "*"},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=1" in result.stdout


def test_gate_empty_sentinel_allows_all(tmp_path):
	"""Explicit empty env var = allow-all: no check-run blocks."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "deadbeefcafe", "base_ref": "main"}},
		"protection": {"main": "404"},
		"check_runs": {"deadbeefcafe": [
			{"name": "CI", "status": "completed", "conclusion": "failure"},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_pr_checks_completed 2955 "" "main"; echo "EXIT=$?"',
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=0" in result.stdout


def test_gate_acceptable_conclusions_pass(tmp_path):
	"""success / neutral / skipped / cancelled — all acceptable for required
	checks."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "deadbeefcafe", "base_ref": "main"}},
		"protection": {"main": "404"},
		"check_runs": {"deadbeefcafe": [
			{"name": "ck-success", "status": "completed", "conclusion": "success"},
			{"name": "ck-neutral", "status": "completed", "conclusion": "neutral"},
			{"name": "ck-skipped", "status": "completed", "conclusion": "skipped"},
			{"name": "ck-cancelled", "status": "completed", "conclusion": "cancelled"},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_pr_checks_completed 2955 "" "main"; echo "EXIT=$?"',
		env_overrides={
			"ORCH_FINAL_MERGE_REQUIRED_CHECKS":
				"ck-success,ck-neutral,ck-skipped,ck-cancelled",
		},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=0" in result.stdout


def test_gate_explicit_head_sha_and_base_ref_skip_pr_fetch(tmp_path):
	"""§15 hygiene: when caller passes both head_sha and base_ref, no PR
	metadata call is issued."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "deadbeefcafe", "base_ref": "main"}},
		"protection": {"main": "404"},
		"check_runs": {"deadbeefcafe": [
			{"name": "CI", "status": "completed", "conclusion": "success"},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_pr_checks_completed 2955 "deadbeefcafe" "main"; echo "EXIT=$?"',
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI"},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=0" in result.stdout
	# Verify no `pulls/2955` API call was issued (only protection + check-runs).
	calls = _read_gh_calls(tmp_path)
	paths = [
		next((a for a in c["argv"] if a.startswith("repos/")), "")
		for c in calls
	]
	assert not any("/pulls/2955" in p for p in paths), paths


def test_gate_whitespace_in_csv_is_trimmed(tmp_path):
	"""Operators may format the env var with spaces after commas; tokens
	must be trimmed before matching."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "deadbeefcafe", "base_ref": "main"}},
		"protection": {"main": "404"},
		"check_runs": {"deadbeefcafe": [
			{"name": "Running Copilot Code Review", "status": "completed",
			 "conclusion": "failure"},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_pr_checks_completed 2955 "" "main"; echo "EXIT=$?"',
		env_overrides={
			"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI ,  review / gate",
		},
	)
	assert result.returncode == 0, result.stderr
	# Copilot not in trimmed allowlist; non-blocking.
	assert "EXIT=0" in result.stdout


def test_gate_without_base_ref_keeps_legacy_all_checks_behavior(tmp_path):
	"""Legacy callers that omit base_ref keep the pre-Layer-1 behaviour:
	ANY failing check-run still blocks, and no base-ref lookup is needed."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "deadbeefcafe", "base_ref": "main"}},
		"protection": {"main": "404"},
		"check_runs": {"deadbeefcafe": [
			{"name": "CI", "status": "completed", "conclusion": "success"},
			{"name": "Running Copilot Code Review", "status": "completed",
			 "conclusion": "failure"},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_pr_checks_completed 2955 "deadbeefcafe"; echo "EXIT=$?"',
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI"},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=1" in result.stdout
	calls = _read_gh_calls(tmp_path)
	paths = [
		next((a for a in c["argv"] if a.startswith("repos/")), "")
		for c in calls
	]
	assert not any("/pulls/2955" in p for p in paths), paths
	assert not any("/branches/main/protection" in p for p in paths), paths


# ---------------------------------------------------------------------------
# Layer 2 tests — _check_final_merge_ineligibility_alert
# ---------------------------------------------------------------------------

def _read_log(path: Path) -> str:
	return path.read_text(encoding="utf-8") if path.exists() else ""


def test_alert_no_op_when_threshold_zero(tmp_path):
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "abc1234", "base_ref": "main"}},
		"check_runs": {"abc1234": []},
	})
	state_blob = {"final_merge_pr": 2955}
	result = _run_shell(
		tmp_path,
		'_check_final_merge_ineligibility_alert; echo "EXIT=$?"',
		state_blob=state_blob,
		env_overrides={"ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS": "0"},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=0" in result.stdout
	assert _read_log(tmp_path / "tg_notify.log") == ""
	assert _read_log(tmp_path / "tracking_comments.log") == ""


def test_alert_first_observation_starts_clock_without_alerting(tmp_path):
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "abc1234", "base_ref": "main"}},
		"check_runs": {"abc1234": [
			{"name": "Copilot", "status": "completed", "conclusion": "failure"},
		]},
	})
	state_blob = {"final_merge_pr": 2955}
	result = _run_shell(
		tmp_path,
		'_check_final_merge_ineligibility_alert; echo "EXIT=$?"',
		state_blob=state_blob,
		env_overrides={"ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS": "6"},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=0" in result.stdout
	assert _read_log(tmp_path / "tg_notify.log") == ""

	written = json.loads((tmp_path / "state.json").read_text())
	assert written["final_merge_ineligible_blocked_at_sha"] == "abc1234"
	assert written["final_merge_ineligible_first_blocked_at_utc"] > 0
	assert written["final_merge_ineligible_alert_sent_for_sha"] == ""


def test_alert_fires_once_after_threshold_elapses(tmp_path):
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "abc1234", "base_ref": "main"}},
		"check_runs": {"abc1234": [
			{"name": "review / gate", "status": "completed", "conclusion": "failure"},
			{"name": "Copilot", "status": "completed", "conclusion": "failure"},
		]},
	})
	# Epoch time — safely past the 6h threshold on any modern test run.
	long_ago = 0
	state_blob = {
		"final_merge_pr": 2955,
		"final_merge_ineligible_blocked_at_sha": "abc1234",
		"final_merge_ineligible_first_blocked_at_utc": long_ago,
		"final_merge_ineligible_alert_sent_for_sha": "",
	}
	result = _run_shell(
		tmp_path,
		'_check_final_merge_ineligibility_alert; echo "EXIT=$?"',
		state_blob=state_blob,
		env_overrides={
			"ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS": "6",
			"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI,review / gate",
		},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=0" in result.stdout

	# Alert fired with blocker summary including only the actual gate blocker.
	tg_log = _read_log(tmp_path / "tg_notify.log")
	assert "Final integration merge stuck" in tg_log
	assert "PR: https://github.com/test-owner/test-repo/pull/2955" in tg_log
	assert "review / gate (failure)" in tg_log
	assert "Copilot (failure)" not in tg_log

	tracking_log = _read_log(tmp_path / "tracking_comments.log")
	assert "Final merge blocked for" in tracking_log
	assert "review / gate (failure)" in tracking_log
	assert "Copilot (failure)" not in tracking_log
	match = re.search(r"for at least (\d+)h", tracking_log)
	assert match is not None
	assert int(match.group(1)) > 6

	calls = _read_gh_calls(tmp_path)
	paths = [
		next((a for a in c["argv"] if a.startswith("repos/")), "")
		for c in calls
	]
	assert sum("/pulls/2955" in p for p in paths) == 1, paths

	# State updated with alert_sent_for_sha so the second invocation is a no-op.
	written = json.loads((tmp_path / "state.json").read_text())
	assert written["final_merge_ineligible_alert_sent_for_sha"] == "abc1234"


def test_alert_idempotent_per_sha(tmp_path):
	"""Second invocation on the same SHA after the alert fired must not
	produce another Telegram message."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "abc1234", "base_ref": "main"}},
		"check_runs": {"abc1234": [
			{"name": "Copilot", "status": "completed", "conclusion": "failure"},
		]},
	})
	state_blob = {
		"final_merge_pr": 2955,
		"final_merge_ineligible_blocked_at_sha": "abc1234",
		"final_merge_ineligible_first_blocked_at_utc": 0,
		"final_merge_ineligible_alert_sent_for_sha": "abc1234",  # already alerted
	}
	result = _run_shell(
		tmp_path,
		'_check_final_merge_ineligibility_alert; echo "EXIT=$?"',
		state_blob=state_blob,
		env_overrides={"ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS": "6"},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=0" in result.stdout
	assert _read_log(tmp_path / "tg_notify.log") == ""
	assert _read_log(tmp_path / "tracking_comments.log") == ""


def test_alert_clock_resets_on_sha_change(tmp_path):
	"""New head SHA (autofix push) resets the clock and clears the
	alert-sent flag — a fresh ${ALERT_HOURS}h window starts."""
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "newsha9", "base_ref": "main"}},
		"check_runs": {"newsha9": [
			{"name": "Copilot", "status": "completed", "conclusion": "failure"},
		]},
	})
	state_blob = {
		"final_merge_pr": 2955,
		"final_merge_ineligible_blocked_at_sha": "oldsha1",  # different SHA
		"final_merge_ineligible_first_blocked_at_utc": 0,
		"final_merge_ineligible_alert_sent_for_sha": "oldsha1",
	}
	result = _run_shell(
		tmp_path,
		'_check_final_merge_ineligibility_alert; echo "EXIT=$?"',
		state_blob=state_blob,
		env_overrides={"ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS": "6"},
	)
	assert result.returncode == 0, result.stderr
	# Clock reset — no alert fired this cycle even though the previous SHA
	# had already received one and the previous timestamp was 0 (would have
	# elapsed past threshold if not reset).
	assert _read_log(tmp_path / "tg_notify.log") == ""

	written = json.loads((tmp_path / "state.json").read_text())
	assert written["final_merge_ineligible_blocked_at_sha"] == "newsha9"
	assert written["final_merge_ineligible_first_blocked_at_utc"] > 0
	assert written["final_merge_ineligible_alert_sent_for_sha"] == ""


def test_alert_no_op_when_no_final_pr_recorded(tmp_path):
	_make_gh_stub(tmp_path, gh_state={})
	state_blob = {}  # no final_merge_pr
	result = _run_shell(
		tmp_path,
		'_check_final_merge_ineligibility_alert; echo "EXIT=$?"',
		state_blob=state_blob,
		env_overrides={"ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS": "6"},
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=0" in result.stdout
	assert _read_log(tmp_path / "tg_notify.log") == ""


def test_clear_helper_resets_all_ineligibility_keys(tmp_path):
	state_blob = {
		"final_merge_ineligible_blocked_at_sha": "abc",
		"final_merge_ineligible_first_blocked_at_utc": 123456,
		"final_merge_ineligible_alert_sent_for_sha": "abc",
	}
	_make_gh_stub(tmp_path, gh_state={})
	result = _run_shell(
		tmp_path,
		'_clear_final_merge_ineligibility_state; echo "EXIT=$?"',
		state_blob=state_blob,
	)
	assert result.returncode == 0, result.stderr
	assert "EXIT=0" in result.stdout

	written = json.loads((tmp_path / "state.json").read_text())
	assert written["final_merge_ineligible_blocked_at_sha"] == ""
	assert written["final_merge_ineligible_first_blocked_at_utc"] == 0
	assert written["final_merge_ineligible_alert_sent_for_sha"] == ""


def test_summarize_blockers_matches_required_set_and_pending_semantics(tmp_path):
	_make_gh_stub(tmp_path, gh_state={
		"pr": {"2955": {"head_sha": "abc1234", "base_ref": "main"}},
		"protection": {"main": "404"},
		"check_runs": {"abc1234": [
			{"name": "CI", "status": "completed", "conclusion": "success"},
			{"name": "review / gate", "status": "completed", "conclusion": "failure"},
			{"name": "Copilot", "status": "completed", "conclusion": "failure"},
			{"name": "Snyk", "status": "in_progress", "conclusion": None},
		]},
	})
	result = _run_shell(
		tmp_path,
		'_summarize_final_merge_blockers 2955 abc1234',
		env_overrides={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI,review / gate"},
	)
	assert result.returncode == 0, result.stderr
	out = result.stdout.strip()
	# Successful CI omitted; required failures + pending checks surface,
	# but advisory failures outside the required set do not.
	assert "CI" not in out
	assert "review / gate (failure)" in out
	assert "Copilot (failure)" not in out
	assert "Snyk (in_progress)" in out
