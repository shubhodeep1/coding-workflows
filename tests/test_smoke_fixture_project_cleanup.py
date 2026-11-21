#!/usr/bin/env python3
"""Smoke-fixture projects never outlive their release gate.

Gates 43445515854, 43590943288 and 43708007145 left orchestrator projects
#9490, #9523 and #9550 open: the orchestrate bootstrap the
orchestrate-decompose-test job dispatched was still queued when
scripts/dispatch_and_watch_workflow_run.sh stopped watching it, so the job's
cleanup ran before the project existed, and the run created it hours later.

Covered here:
- the watcher writes `dispatched_run_id` as soon as the run registers, so the
  cleanup can cancel a run it stopped watching;
- scripts/stable_orchestrate_cancel_dispatched_run.sh cancels a run that is not
  completed and finds a tracking issue it already created;
- scripts/orchestrate_poll_smoke_fixture_reap.sh (sourced by the poller) closes
  a fixture project once its gate run has completed, and leaves it alone while
  the gate runs or when any lookup fails;
- the wiring in the gate, the poller and the poll workflow's staging list.

Every test runs the shipped shell against a fake `gh` on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REAP_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_smoke_fixture_reap.sh"
CANCEL_SCRIPT = REPO_ROOT / "scripts" / "stable_orchestrate_cancel_dispatched_run.sh"
WATCH_SCRIPT = REPO_ROOT / "scripts" / "dispatch_and_watch_workflow_run.sh"
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
GATE_WF = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
REPO = "acme/widgets"
GATE = "43708007145"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

# Fake gh: answers `gh api` from a JSON routes file keyed by "<METHOD> <endpoint>".
# A route value is a JSON response, {"__fail__": true}, or {"__seq__": [...]}:
# responses consumed one per call (the last one repeats).
# Every call is appended to calls.log as one JSON line.
FAKE_GH = textwrap.dedent(
	"""\
	#!/usr/bin/env python3
	import json, os, subprocess, sys
	state_dir = os.environ["FAKE_GH_DIR"]
	args = sys.argv[1:]
	if args[:2] == ["workflow", "run"]:
		with open(os.path.join(state_dir, "calls.log"), "a") as fh:
			fh.write(json.dumps({"key": "DISPATCH " + args[2], "fields": {}}) + "\\n")
		sys.exit(0)
	if not args or args[0] != "api":
		sys.exit(1)
	args = args[1:]
	method, endpoint, jq, fields = "GET", None, None, {}
	i = 0
	while i < len(args):
		a = args[i]
		if a == "-X":
			method = args[i + 1]; i += 2; continue
		if a == "--jq":
			jq = args[i + 1]; i += 2; continue
		if a in ("-f", "-F", "--field", "--raw-field"):
			k, _, v = args[i + 1].partition("="); fields[k] = v; i += 2; continue
		if a.startswith("-"):
			i += 1; continue
		if endpoint is None:
			endpoint = a
		i += 1
	key = f"{method} {endpoint}"
	with open(os.path.join(state_dir, "calls.log"), "a") as fh:
		fh.write(json.dumps({"key": key, "fields": fields}) + "\\n")
	routes_path = os.path.join(state_dir, "routes.json")
	routes = json.load(open(routes_path))
	if key not in routes:
		if method != "GET":
			routes[key] = {}
		else:
			sys.stderr.write(f"gh: Not Found (HTTP 404) {key}\\n")
			sys.exit(1)
	resp = routes[key]
	if isinstance(resp, dict) and "__seq__" in resp:
		seq = resp["__seq__"]
		resp = seq[0] if len(seq) == 1 else seq.pop(0)
		json.dump(routes, open(routes_path, "w"))
	if isinstance(resp, dict) and resp.get("__fail__"):
		sys.stderr.write(f"gh: failure (HTTP 500) {key}\\n")
		sys.exit(1)
	body = json.dumps(resp)
	if jq is not None:
		out = subprocess.run(["jq", "-r", jq], input=body, capture_output=True, text=True)
		if out.returncode != 0:
			sys.stderr.write(out.stderr)
			sys.exit(1)
		sys.stdout.write(out.stdout)
	else:
		sys.stdout.write(body)
	"""
)


@pytest.fixture()
def fake_gh(tmp_path: Path):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
	state = tmp_path / "state"
	state.mkdir()
	(state / "calls.log").write_text("", encoding="utf-8")

	class Fake:
		def routes(self, routes: dict) -> None:
			(state / "routes.json").write_text(json.dumps(routes), encoding="utf-8")

		def calls(self) -> list[dict]:
			return [json.loads(line) for line in (state / "calls.log").read_text(encoding="utf-8").splitlines() if line]

		def writes(self) -> list[dict]:
			return [c for c in self.calls() if not c["key"].startswith("GET ")]

		def env(self, **extra: str) -> dict:
			env = dict(os.environ)
			env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
			env["FAKE_GH_DIR"] = str(state)
			env.update(extra)
			return env

	return Fake()


# ---------------------------------------------------------------- poller reap


def _run_reap(fake, tracking: int, title: str, labels: str = "[]") -> subprocess.CompletedProcess:
	script = (
		"set -uo pipefail\n"
		"gh_retry() { \"$@\"; }\n"
		f"source {REAP_SCRIPT}\n"
		'smoke_fixture_reap_if_gate_done "$1" "$2" "$3"\n'
	)
	return subprocess.run(
		["bash", "-c", script, "reap", str(tracking), title, labels],
		check=False, capture_output=True, text=True, timeout=60, env=fake.env(GITHUB_REPOSITORY=REPO),
	)


def _gate_run(status: str, path: str = ".github/workflows/test-and-mark-stable.yml") -> dict:
	return {"id": int(GATE), "status": status, "path": path}


def _project_routes(tracking: int, gate_status: str = "completed") -> dict:
	branch = f"orchestrator/project-{tracking}"
	return {
		f"GET repos/{REPO}/actions/runs/{GATE}": _gate_run(gate_status),
		f"GET repos/{REPO}/pulls?state=open&base={branch}&per_page=100": [{"number": 9598}, {"number": 9615}],
		f"GET repos/{REPO}/pulls?state=open&head=acme:{branch}&per_page=100": [{"number": 9565}],
		f"GET repos/{REPO}/issues?state=open&labels=ai:orchestrator-managed&per_page=100": [
			{"number": 9612, "body": f"Tracking issue: #{tracking}\nwork"},
			{"number": 9613, "body": f"x\nTracking issue: #{tracking}"},
			# Another project whose number starts with this one's digits.
			{"number": 9700, "body": f"Tracking issue: #{tracking}1"},
			{"number": 9701, "body": "Tracking issue: #42", "pull_request": {}},
		],
	}


TITLE = f"[Orchestrator] [E2E Orchestrate Smoke {GATE}] Update two smoke canary files in parallel"


def test_reap_closes_project_once_gate_completed(fake_gh) -> None:
	fake_gh.routes(_project_routes(9550))
	result = _run_reap(fake_gh, 9550, TITLE)
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"SMOKE_FIXTURE_REAP tracking_issue=9550 gate_run={GATE} outcome=closed reason=gate_completed closed_prs=3 closed_issues=2" in result.stdout
	writes = [(w["key"], w["fields"]) for w in fake_gh.writes()]
	keys = [k for k, _ in writes]
	assert keys == [
		f"PATCH repos/{REPO}/pulls/9565",
		f"PATCH repos/{REPO}/pulls/9598",
		f"PATCH repos/{REPO}/pulls/9615",
		f"PATCH repos/{REPO}/issues/9612",
		f"PATCH repos/{REPO}/issues/9613",
		f"PATCH repos/{REPO}/issues/9550",
		f"POST repos/{REPO}/issues/9550/comments",
	]
	assert all(f.get("state") == "closed" for k, f in writes if k.startswith("PATCH"))
	assert writes[-2][1].get("state_reason") == "not_planned"
	comment = writes[-1][1]["body"]
	assert GATE in comment and "#9612" in comment and "No branch or run was deleted" in comment
	# Never deletes anything.
	assert not any(k.startswith("DELETE") for k in keys)


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_reap_leaves_project_while_gate_runs(fake_gh, status: str) -> None:
	fake_gh.routes(_project_routes(9550, gate_status=status))
	result = _run_reap(fake_gh, 9550, TITLE)
	assert result.returncode == 1
	assert f"reason=gate_running status={status}" in result.stdout
	assert fake_gh.writes() == []


def test_reap_skips_run_that_is_not_the_gate(fake_gh) -> None:
	routes = _project_routes(9550)
	routes[f"GET repos/{REPO}/actions/runs/{GATE}"] = _gate_run("completed", ".github/workflows/ci.yml")
	fake_gh.routes(routes)
	result = _run_reap(fake_gh, 9550, TITLE)
	assert result.returncode == 1
	assert "reason=not_gate_run" in result.stdout
	assert fake_gh.writes() == []


def test_reap_fails_open_when_gate_lookup_fails(fake_gh) -> None:
	routes = _project_routes(9550)
	del routes[f"GET repos/{REPO}/actions/runs/{GATE}"]
	fake_gh.routes(routes)
	result = _run_reap(fake_gh, 9550, TITLE)
	assert result.returncode == 1
	assert "reason=gate_run_unavailable" in result.stdout
	assert fake_gh.writes() == []


def test_reap_fails_open_when_listing_fails(fake_gh) -> None:
	routes = _project_routes(9550)
	routes[f"GET repos/{REPO}/pulls?state=open&head=acme:orchestrator/project-9550&per_page=100"] = {"__fail__": True}
	fake_gh.routes(routes)
	result = _run_reap(fake_gh, 9550, TITLE)
	assert result.returncode == 1
	assert "reason=listing_failed" in result.stdout
	assert fake_gh.writes() == []


def test_reap_reads_body_when_title_lacks_gate_id(fake_gh) -> None:
	routes = _project_routes(9550)
	routes[f"GET repos/{REPO}/issues/9550"] = {"number": 9550, "body": f"Project: [E2E Orchestrate Smoke {GATE}] two canaries"}
	fake_gh.routes(routes)
	result = _run_reap(fake_gh, 9550, "[Orchestrator] Update two canary files", '[{"name":"e2e-smoke-test"}]')
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=closed" in result.stdout


def test_reap_skips_when_no_gate_id_anywhere(fake_gh) -> None:
	fake_gh.routes({f"GET repos/{REPO}/issues/9550": {"number": 9550, "body": "no marker here"}})
	result = _run_reap(fake_gh, 9550, "[Orchestrator] Update two canary files", '[{"name":"e2e-smoke-test"}]')
	assert result.returncode == 1
	assert "reason=no_gate_run_id" in result.stdout
	assert fake_gh.writes() == []


def test_reap_reads_run_suffix_of_gate_fixture_titles(fake_gh) -> None:
	routes = _project_routes(9700)
	fake_gh.routes(routes)
	result = _run_reap(fake_gh, 9700, f"[E2E Smoke Test] Review-Blocked Simulation (run {GATE})")
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"gate_run={GATE} outcome=closed" in result.stdout


def test_reap_ignores_run_suffix_on_real_titles(fake_gh) -> None:
	fake_gh.routes({f"GET repos/{REPO}/issues/9700": {"number": 9700, "body": ""}})
	result = _run_reap(fake_gh, 9700, f"[Orchestrator] Fix flaky job (run {GATE})", '[{"name":"e2e-smoke-test"}]')
	assert result.returncode == 1
	assert "reason=no_gate_run_id" in result.stdout


def test_reap_keeps_tracking_open_when_an_item_close_fails(fake_gh) -> None:
	routes = _project_routes(9550)
	routes[f"PATCH repos/{REPO}/issues/9613"] = {"__fail__": True}
	fake_gh.routes(routes)
	result = _run_reap(fake_gh, 9550, TITLE)
	# Handled this tick (not driven), retried next tick.
	assert result.returncode == 0
	assert "outcome=failed reason=item_close_failed failed=1" in result.stdout
	keys = [w["key"] for w in fake_gh.writes()]
	assert f"PATCH repos/{REPO}/issues/9550" not in keys
	assert f"POST repos/{REPO}/issues/9550/comments" not in keys


# ---------------------------------------------------------------- gate cancel


def _run_cancel(fake, **env: str) -> subprocess.CompletedProcess:
	base = {"TEST_REPO": REPO, "RUN_ID": GATE, "SMOKE_CANCEL_POLL_SECS": "0", "SMOKE_CANCEL_WAIT_SECS": "1", "SMOKE_CANCEL_FORCE_WAIT_SECS": "1"}
	base.update(env)
	return subprocess.run(["bash", str(CANCEL_SCRIPT)], check=False, capture_output=True, text=True, timeout=60, env=fake.env(**base))


def _tracking_listing(*items: dict) -> dict:
	return {f"GET repos/{REPO}/issues?state=open&labels=ai:orchestrator-tracking&per_page=100": list(items)}


def test_cancel_cancels_queued_bootstrap(fake_gh) -> None:
	routes = {f"GET repos/{REPO}/actions/runs/111": {"__seq__": [{"status": "queued"}, {"status": "completed"}]}}
	routes.update(_tracking_listing())
	fake_gh.routes(routes)
	result = _run_cancel(fake_gh, DISPATCHED_RUN_ID="111")
	assert result.returncode == 0
	assert "ORCHESTRATE_SMOKE_DISPATCH_CANCEL run=111 outcome=cancelled" in result.stderr
	assert [w["key"] for w in fake_gh.writes()] == [f"POST repos/{REPO}/actions/runs/111/cancel"]
	assert "tracking_issue=" not in result.stdout


def test_cancel_force_cancels_when_plain_cancel_does_not_land(fake_gh) -> None:
	fake_gh.routes({f"GET repos/{REPO}/actions/runs/111": {"status": "in_progress"}, **_tracking_listing()})
	result = _run_cancel(fake_gh, DISPATCHED_RUN_ID="111")
	assert result.returncode == 0
	assert [w["key"] for w in fake_gh.writes()] == [
		f"POST repos/{REPO}/actions/runs/111/cancel",
		f"POST repos/{REPO}/actions/runs/111/force-cancel",
	]
	assert "outcome=cancel_requested_not_completed" in result.stderr


def test_cancel_leaves_completed_run_and_finds_tracking(fake_gh) -> None:
	routes = {f"GET repos/{REPO}/actions/runs/111": {"status": "completed"}}
	routes.update(_tracking_listing(
		{"number": 9001, "created_at": "2026-11-01T00:00:00Z", "body": "other project 1234"},
		{"number": 9550, "created_at": "2026-11-15T23:33:56Z", "body": f"[E2E Orchestrate Smoke {GATE}] ..."},
	))
	routes[f"GET repos/{REPO}/issues?state=open&labels=ai:orchestrator-managed&per_page=100"] = [
		{"number": 9613, "body": "Tracking issue: #9550"},
		{"number": 9612, "body": "Tracking issue: #9550\nmore"},
		{"number": 9700, "body": "Tracking issue: #95501"},
	]
	fake_gh.routes(routes)
	result = _run_cancel(fake_gh, DISPATCHED_RUN_ID="111")
	assert result.returncode == 0
	assert "outcome=already_completed" in result.stderr
	assert fake_gh.writes() == []
	assert result.stdout.strip().splitlines() == ["tracking_issue=9550", "child_issues=9612,9613"]


def test_cancel_retries_a_failed_status_read(fake_gh) -> None:
	fake_gh.routes({
		f"GET repos/{REPO}/actions/runs/111": {"__seq__": [{"__fail__": True}, {"status": "completed"}]},
		**_tracking_listing(),
	})
	result = _run_cancel(fake_gh, DISPATCHED_RUN_ID="111")
	assert result.returncode == 0
	assert "outcome=already_completed" in result.stderr


def test_cancel_keeps_known_tracking_number(fake_gh) -> None:
	fake_gh.routes({f"GET repos/{REPO}/actions/runs/111": {"status": "completed"}})
	result = _run_cancel(fake_gh, DISPATCHED_RUN_ID="111", TRACKING_NUMBER="9550")
	assert result.returncode == 0
	assert result.stdout == ""
	assert [c["key"] for c in fake_gh.calls()] == [f"GET repos/{REPO}/actions/runs/111"]


def test_cancel_without_dispatched_run_still_discovers(fake_gh) -> None:
	fake_gh.routes(_tracking_listing({"number": 9490, "created_at": "2026-11-14T03:51:51Z", "body": f"run {GATE}"}))
	result = _run_cancel(fake_gh, DISPATCHED_RUN_ID="")
	assert result.returncode == 0
	assert "reason=no_dispatched_run" in result.stderr
	assert result.stdout.strip() == "tracking_issue=9490"


def test_cancel_never_fails_the_step(fake_gh) -> None:
	fake_gh.routes({})
	result = _run_cancel(fake_gh, DISPATCHED_RUN_ID="111")
	assert result.returncode == 0
	assert "outcome=lookup_failed" in result.stderr


# ---------------------------------------------------------------- watcher


def test_watcher_writes_dispatched_run_id_before_timing_out(fake_gh, tmp_path: Path) -> None:
	wf = "internal-orchestrate.yml"
	fake_gh.routes({
		f"GET repos/{REPO}/actions/workflows/{wf}/runs?event=workflow_dispatch&per_page=1": {"workflow_runs": [{"id": 100}]},
		f"GET repos/{REPO}/actions/workflows/{wf}/runs?event=workflow_dispatch&per_page=10": {"workflow_runs": [{"id": 100}, {"id": 4353, "created_at": "2026-11-14T00:08:30Z"}]},
		f"POST repos/{REPO}/actions/workflows/{wf}/dispatches": {},
		f"GET repos/{REPO}/actions/runs/4353": {"status": "queued", "conclusion": None},
	})
	out = tmp_path / "gh_output"
	out.write_text("", encoding="utf-8")
	result = subprocess.run(
		["bash", str(WATCH_SCRIPT), "--repo", REPO, "--workflow", wf, "--display-name", "orchestrate",
			"--registration-timeout-secs", "5", "--registration-poll-interval-secs", "0",
			"--completion-timeout-secs", "2", "--completion-poll-interval-secs", "1"],
		check=False, capture_output=True, text=True, timeout=120, env=fake_gh.env(GITHUB_OUTPUT=str(out)),
	)
	assert result.returncode == 1, result.stdout + result.stderr
	assert "timed out" in result.stdout + result.stderr
	lines = out.read_text(encoding="utf-8").splitlines()
	assert "dispatched_run_id=4353" in lines, lines
	# run_id keeps its meaning: never written for a run that did not complete.
	assert not any(line.startswith("run_id=") for line in lines), lines


# ---------------------------------------------------------------- wiring


def test_gate_cleanup_cancels_dispatched_run() -> None:
	text = GATE_WF.read_text(encoding="utf-8")
	start = text.index("      - name: Cleanup orchestrate test issues")
	end = text.find("\n      - name: ", start + 10)
	step = text[start:end if end != -1 else len(text)]
	assert "if: always()" in step
	assert "DISPATCHED_RUN_ID: ${{ steps.dispatch.outputs.dispatched_run_id }}" in step
	cancel_at = step.index("stable_orchestrate_cancel_dispatched_run.sh")
	assert cancel_at < step.index("# Phase 1: close known issues")


def test_poller_sources_and_runs_reap_for_smoke_fixtures() -> None:
	text = POLLER.read_text(encoding="utf-8")
	assert 'source "${_OPP_SMOKE_REAP_DIR}/orchestrate_poll_smoke_fixture_reap.sh"' in text
	loop_at = text.index('SMOKE_FIXTURE_REAPED_NUMS_JSON=\'[]\'')
	main_loop_at = text.index('for ((tidx=0; tidx<COUNT; tidx++)); do')
	reap_at = text.index('smoke_fixture_reap_if_gate_done "${smoke_candidate_num}"')
	assert loop_at < reap_at < main_loop_at
	assert text.index("TRACKING_ISSUES=\"${smoke_fixture_remaining}\"") < main_loop_at


def test_poll_workflow_stages_reap_script() -> None:
	assert "for f in orchestrate_poll_smoke_fixture_reap.sh; do" in POLL_WF.read_text(encoding="utf-8")


def test_poller_drops_reaped_project_from_the_tick(tmp_path: Path) -> None:
	"""Run the poller's smoke-detection block with a stub reaper."""
	text = POLLER.read_text(encoding="utf-8")
	start = text.index('SMOKE_FIXTURE_TRACKING_NUMS=""\n')
	end = text.index('if [ "${STANDALONE_PR_SWEEP_ONLY_ACTIVE}" != "true" ]; then\n  prime_phase_concurrency_snapshot', start)
	fn_start = text.index("tracking_issue_is_smoke_fixture() {")
	fn_end = text.index("\n}\n", fn_start) + 3
	script = (
		"set -euo pipefail\n"
		+ text[fn_start:fn_end]
		+ "smoke_fixture_reap_if_gate_done() { [ \"$1\" = 9550 ]; }\n"
		+ "TRACKING_ISSUES='" + json.dumps([
			{"number": 9550, "title": TITLE, "labels": []},
			{"number": 9551, "title": "[Orchestrator] Real project", "labels": []},
			{"number": 9552, "title": "[E2E Smoke Test] Review-Blocked Simulation (run 1)", "labels": []},
		]) + "'\n"
		+ "COUNT=3\n"
		+ text[start:end]
		+ 'echo "COUNT=${COUNT}"; echo "${TRACKING_ISSUES}" | jq -c "[.[].number]"; echo "SMOKE=${SMOKE_FIXTURE_TRACKING_NUMS}"\n'
	)
	result = subprocess.run(["bash", "-c", script], check=False, capture_output=True, text=True, timeout=30)
	assert result.returncode == 0, result.stderr
	assert "COUNT=2" in result.stdout
	assert "[9551,9552]" in result.stdout
	# Alert silencing still covers the reaped project.
	assert "SMOKE= 9550 9552" in result.stdout
