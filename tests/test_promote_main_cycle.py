#!/usr/bin/env python3
"""Tests for scripts/promote_main_cycle.sh (the daily promote cycle)."""

from __future__ import annotations

import json
import os
import stat
import re
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "promote_main_cycle.sh"

TIP = "1" * 40
TAG_COMMIT = "2" * 40
OLD_BASELINE = "3" * 40

MOCK_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path(os.environ["MOCK_GH_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def save():
    state_path.write_text(json.dumps(state))
def respond(payload):
    save()
    sys.stdout.write(json.dumps(payload))
    sys.exit(0)
if args[:1] == ["api"]:
    method = "GET"
    path = None
    rest = args[1:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-X", "--method"):
            method = rest[i + 1]; i += 2; continue
        if a in ("-f", "-F", "--jq", "--input", "-H"):
            i += 2; continue
        if not a.startswith("-") and path is None:
            path = a
        i += 1
    # Pinned mode (phase 3): promote-candidate branch and promote-cycle-frozen tag.
    if path.endswith("/branches/promote-candidate"):
        pin = state.get("pin_branch", "missing")
        if pin == "missing":
            save(); sys.stderr.write("HTTP 404: Not Found\n"); sys.exit(1)
        if pin == "error":
            save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
        respond({"name": "promote-candidate", "protected": pin == "protected"})
    if path.endswith("/git/refs/tags/promote-cycle-frozen"):
        if method == "DELETE":
            state["tag_deletes"] = state.get("tag_deletes", 0) + 1
            respond({})
        state.setdefault("tag_ref_patches", []).append(args)
        respond({})
    if path.endswith("/git/ref/tags/promote-cycle-frozen"):
        if state.get("pin_tag_exists"):
            respond({"object": {"type": "tag", "sha": "8" * 40}})
        save(); sys.stderr.write("HTTP 404: Not Found\n"); sys.exit(1)
    if path.endswith("/git/tags") and method == "POST":
        created = state.setdefault("tag_objects_created", [])
        created.append(args)
        respond({"sha": str(6 + len(created) - 1)[-1] * 40})
    if path.endswith("/git/refs") and method == "POST":
        state.setdefault("ref_creates", []).append(args)
        if state.get("tag_ref_race"):
            save(); sys.stderr.write("HTTP 422: Reference already exists\n"); sys.exit(1)
        if state.get("tag_ref_fail"):
            save(); sys.stderr.write("HTTP 502: Bad Gateway\n"); sys.exit(1)
        respond({})
    if path.endswith("/git/refs/heads/promote-candidate") and method == "PATCH":
        state.setdefault("branch_patches", []).append(args)
        mode = state.get("pin_branch_patch", "ok")
        if mode == "not_ff":
            save(); sys.stderr.write("HTTP 422: Update is not a fast forward\n"); sys.exit(1)
        if mode == "error":
            save(); sys.stderr.write("HTTP 403: Forbidden\n"); sys.exit(1)
        respond({})
    if path.endswith("/git/ref/heads/promote-candidate"):
        respond({"object": {"type": "commit", "sha": state.get("pin_branch_tip", state["main_tip"])}})
    if path.endswith("/git/ref/heads/stable"):
        # The stable BRANCH (#7888); defaults to the stable tag's commit.
        if state.get("stable_branch_error"):
            save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
        seq = state.get("stable_branch_sequence")
        if seq:
            n = state.get("stable_branch_reads", 0)
            state["stable_branch_reads"] = n + 1
            respond({"object": {"type": "commit", "sha": seq[min(n, len(seq) - 1)]}})
        respond({"object": {"type": "commit", "sha": state.get("stable_branch_sha", state["tag_commit"])}})
    if path.endswith("/git/ref/heads/main"):
        respond({"object": {"type": "commit", "sha": state["main_tip"]}})
    if path.endswith("/git/ref/tags/stable"):
        if state.get("tag_missing"):
            save(); sys.stderr.write("404\n"); sys.exit(1)
        if state.get("tag_lookup_error"):
            save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
        respond({"object": {"type": "tag", "sha": "9" * 40}})
    if "/git/tags/" in path:
        respond({"object": {"type": "commit", "sha": state["tag_commit"]}, "message": state.get("tag_message", "")})
    if "/compare/" in path:
        base = path.split("/compare/")[1].split("...")[0]
        entry = state.get("compares", {}).get(base)
        if entry == "api_error":
            save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
        if entry is None:
            respond({"status": "ahead", "total_commits": 1, "files": [{"filename": "scripts/x.sh"}]})
        respond(entry)
    if path.startswith("search/issues"):
        q = next(a for a in args if a.startswith("q="))
        if "apply-analysis-cycle-baseline-sha:" in q:
            respond({"total_count": 1 if state.get("last_cycle_issue") else 0, "items": ([{"number": state["last_cycle_issue"]}] if state.get("last_cycle_issue") else [])})
        respond({"total_count": 0, "items": []})
    if "/issues/" in path and path.endswith("/comments?per_page=100"):
        respond(state.get("cycle_comments", [{"body": "apply-analysis-source-doc: x\napply-analysis-cycle-baseline-sha: " + state.get("last_cycle_baseline", ""), "user": {"login": "github-actions[bot]"}, "author_association": "NONE"}]))
    if "/issues?" in path:
        respond([{"number": n, "labels": []} for n in state.get("in_flight", [])])
    if "/actions/workflows/promote-main-to-stable.yml/runs" in path:
        respond({"workflow_runs": state.get("self_runs", [])})
    if "/actions/workflows/test-and-mark-stable.yml/runs" in path and "event=workflow_dispatch" not in path:
        # The idle check before dispatch (any branch, any event).
        itick = state.get("gate_idle_tick", 0)
        state["gate_idle_tick"] = itick + 1
        iseq = state.get("gate_idle_sequence") or []
        respond({"workflow_runs": iseq[min(itick, len(iseq) - 1)] if iseq else []})
    if "/actions/workflows/test-and-mark-stable.yml/runs" in path:
        tick = state.get("gate_tick", 0)
        state["gate_tick"] = tick + 1
        seq = state.get("gate_runs_sequence") or []
        runs = seq[min(tick, len(seq) - 1)] if seq else []
        respond({"workflow_runs": runs})
    if "/actions/runs/" in path and "/jobs" in path:
        state.setdefault("jobs_reads", []).append(path)
        if state.get("gate_jobs_error"):
            save(); sys.stderr.write("HTTP 404: Not Found\n"); sys.exit(1)
        respond(state.get("gate_jobs", {"jobs": []}))
    save(); sys.stderr.write("unexpected api path " + path + "\n"); sys.exit(1)
if args[:2] == ["workflow", "run"]:
    state.setdefault("dispatches", []).append(args[2:])
    save(); sys.exit(0)
save(); sys.stderr.write("unexpected gh call " + " ".join(args) + "\n"); sys.exit(1)
'''

STUB_DISPATCHER = r'''#!/usr/bin/env bash
set -euo pipefail
if [ "${APPLY_ANALYSIS_LIST_ONLY:-false}" = "true" ]; then
  echo "APPLY_ANALYSIS_CANDIDATES count=${STUB_CANDIDATES:-2} docs=a,b"
  exit 0
fi
env | grep -E '^(APPLY_ANALYSIS_|GITHUB_SHA)' | sort > "${STUB_ENV_OUT}"
if [ "${STUB_DISPATCH_OUTCOME:-dispatched}" = "dispatched" ]; then
  echo "APPLY_ANALYSIS_DISPATCHED doc=analysis/workflow-optimization-2026-08-30.md role=${APPLY_ANALYSIS_ROLE}"
else
  echo "APPLY_ANALYSIS_SKIPPED reason=all_docs_processed"
fi
'''


MOCK_GIT = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path(os.environ["MOCK_GH_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("git_calls", []).append(args)
if args[:1] == ["fetch"] and state.get("git_fetch_fail"):
    state_path.write_text(json.dumps(state))
    sys.stderr.write("fatal: simulated fetch failure\\n")
    sys.exit(1)
if args[:1] == ["checkout"]:
    state["git_head"] = args[-1]
if args[:2] == ["rev-parse", "HEAD"]:
    sys.stdout.write(state.get("git_head", "1" * 40) + "\\n")
if args[:2] == ["rev-parse", "--is-shallow-repository"]:
    sys.stdout.write(state.get("git_shallow", "true") + "\\n")
state_path.write_text(json.dumps(state))
sys.exit(0)
"""

# Stands in for scripts/operator_step_issue.py (PROMOTE_CYCLE_OPERATOR_STEP_HELPER).
STUB_TICK_HELPER = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["STUB_TICK_ARGS_OUT"], "a", encoding="utf-8") as out:
    out.write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("STUB_TICK_FAIL") == "1":
    print(json.dumps({"error": "simulated tick failure"}))
    sys.exit(2)
print(json.dumps({"issue": 5, "ticked": ["pr-1"], "skipped": {}}))
'''


def _run(tmp: Path, state: dict, env: dict[str, str] | None = None, script: Path = SCRIPT) -> tuple[subprocess.CompletedProcess[str], dict, Path]:
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(MOCK_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	# The script only touches git when the smoke gate tested a newer commit
	# than the tip it started from; never let a test move the real checkout.
	(bin_dir / "git").write_text(MOCK_GIT, encoding="utf-8")
	(bin_dir / "git").chmod(0o755)
	stub = tmp / "dispatcher_stub.sh"
	stub.write_text(STUB_DISPATCHER, encoding="utf-8")
	stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
	env_out = tmp / "dispatcher_env.txt"
	tick_helper = tmp / "operator_step_stub.py"
	tick_helper.write_text(STUB_TICK_HELPER, encoding="utf-8")
	tick_args_out = tmp / "tick_args.jsonl"
	state_file = tmp / "state.json"
	base_state = {"main_tip": TIP, "tag_commit": TAG_COMMIT, "self_runs": [], "compares": {}}
	base_state.update(state)
	state_file.write_text(json.dumps(base_state), encoding="utf-8")
	output_file = tmp / "out.txt"
	output_file.write_text("", encoding="utf-8")
	run_env = os.environ.copy()
	run_env.update(
		{
			"PATH": f"{bin_dir}:{run_env['PATH']}",
			"MOCK_GH_STATE": str(state_file),
			"GITHUB_REPOSITORY": "owner/repo",
			"GH_TOKEN": "x",
			"GITHUB_RUN_ID": "777",
			"GITHUB_OUTPUT": str(output_file),
			"APPLY_ANALYSIS_DISPATCHER": str(stub),
			"STUB_ENV_OUT": str(env_out),
			"PROMOTE_CYCLE_OPERATOR_STEP_HELPER": str(tick_helper),
			"STUB_TICK_ARGS_OUT": str(tick_args_out),
			"PROMOTE_CYCLE_GATE_POLL_SECS": "1",
			"PROMOTE_CYCLE_GATE_WAIT_SECS": "6",
			"PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS": "6",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	for key in ("BASH_ENV", "ENV"):
		run_env.pop(key, None)
	if env:
		run_env.update(env)
	proc = subprocess.run(["bash", str(script)], cwd=str(tmp), env=run_env, text=True, capture_output=True, timeout=120)
	final = json.loads(state_file.read_text(encoding="utf-8"))
	final["github_output"] = output_file.read_text(encoding="utf-8")
	final["tick_calls"] = [json.loads(line) for line in tick_args_out.read_text(encoding="utf-8").splitlines()] if tick_args_out.exists() else []
	return proc, final, env_out


def _compare(files: list[str], status: str = "ahead") -> dict:
	return {"status": status, "total_commits": 1, "files": [{"filename": f} for f in files]}


GATE_SUCCESS = [
	[],
	[{"id": 500, "status": "in_progress", "conclusion": None, "head_branch": "main", "head_sha": TIP, "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}],
	[{"id": 500, "status": "completed", "conclusion": "success", "head_branch": "main", "head_sha": TIP, "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}],
]
ADVANCED_TIP = "5" * 40
GATE_SUCCESS_ON_ADVANCED_MAIN = [
	[],
	[{"id": 500, "status": "completed", "conclusion": "success", "head_branch": "main", "head_sha": ADVANCED_TIP, "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}],
]


def test_kill_switch() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {}, env={"PROMOTE_CYCLE_ENABLED": "false"})
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=disabled" in proc.stdout
	assert final.get("calls", []) == []


def test_skips_when_another_cycle_run_is_active() -> None:
	runs = [{"id": 1, "status": "in_progress", "event": "schedule", "conclusion": None, "head_sha": TIP, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"self_runs": runs})
	assert "PROMOTE_CYCLE_SKIPPED reason=another_run_active active_runs=1" in proc.stdout
	assert not final.get("dispatches")


def test_own_run_id_is_not_counted_as_active() -> None:
	runs = [{"id": 777, "status": "in_progress", "event": "schedule", "conclusion": None, "head_sha": TIP, "created_at": "2026-09-19T00:00:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"self_runs": runs, "compares": {TAG_COMMIT: _compare(["docs/a.md"])}})
	assert "reason=another_run_active" not in proc.stdout


def test_skips_when_cycle_in_flight() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"in_flight": [42]})
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout
	assert not final.get("dispatches")


def test_non_code_changes_do_not_start_a_cycle() -> None:
	files = ["analysis/workflow-optimization-2026-09-19.md", "ai-memory/x.json", "docs/plans/p.md", "docs/README.md", "changelog.d/x.md", "CHANGELOG.md", "tests/e2e_smoke_canary.txt"]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(files)}})
	assert proc.returncode == 0, proc.stderr
	assert f"PROMOTE_CYCLE_SKIPPED reason=no_code_changes base={TAG_COMMIT} head={TIP}" in proc.stdout
	assert not final.get("dispatches")


def test_claude_md_and_dot_claude_count_as_code() -> None:
	# workflow-templates/.claude/ is the tree consumers receive on the @stable sync.
	for path in ("CLAUDE.md", "workflow-templates/CLAUDE.md", ".claude/commands/investigate-issue.md", "workflow-templates/.claude/commands/investigate-issue.md"):
		with tempfile.TemporaryDirectory() as tmp:
			proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare([path, "docs/x.md"])}, "gate_runs_sequence": GATE_SUCCESS})
		assert "reason=no_code_changes" not in proc.stdout, path
		assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout, (path, proc.stdout, proc.stderr)


def test_pipeline_instruction_files_count_as_code() -> None:
	# Markdown the pipeline reads into model context steers it, so it is code (#7662).
	for path in ("README.md", "unattended_system_instructions.md", "ai_pipeline.md", "agents.md", "AGENTS.md", "codex.md", "probably_unnecessary_but_read_if_stuck.md", "agents.d/123-x.md"):
		with tempfile.TemporaryDirectory() as tmp:
			proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare([path, "docs/x.md"])}, "gate_runs_sequence": GATE_SUCCESS})
		assert "reason=no_code_changes" not in proc.stdout, path
		assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout, (path, proc.stdout, proc.stderr)


def _case_pattern_lines(path: Path, function: str) -> list[str]:
	lines = path.read_text(encoding="utf-8").splitlines()
	start = next(i for i, line in enumerate(lines) if line.strip().startswith(f"{function}()"))
	patterns: list[str] = []
	for line in lines[start + 1:]:
		stripped = line.strip()
		if stripped == "}":
			break
		if stripped.endswith(";;"):
			patterns.append(stripped)
	return patterns


def test_code_path_case_lists_match_poller() -> None:
	cycle = _case_pattern_lines(SCRIPT, "is_code_path")
	poller = _case_pattern_lines(REPO_ROOT / "scripts" / "orchestrate_poll_process.sh", "comprehensive_cycle_is_code_path")
	assert len(cycle) == 2
	assert cycle == poller


def test_identical_tip_and_tag_skips() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: {"status": "identical", "total_commits": 0, "files": []}}})
	assert "PROMOTE_CYCLE_SKIPPED reason=no_code_changes" in proc.stdout


def test_no_code_change_since_last_cycle_baseline_skips() -> None:
	state = {
		"compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), OLD_BASELINE: _compare(["analysis/a.md"])},
		"last_cycle_issue": 900,
		"last_cycle_baseline": OLD_BASELINE,
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert f"PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_last_cycle base={OLD_BASELINE} head={TIP}" in proc.stdout
	assert not final.get("dispatches")


def test_non_marker_comments_do_not_break_last_cycle_baseline_lookup() -> None:
	state = {
		"compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), OLD_BASELINE: _compare(["analysis/a.md"])},
		"last_cycle_issue": 900,
		"last_cycle_baseline": OLD_BASELINE,
		"cycle_comments": [
			{"body": "unrelated orchestrator state", "user": {"login": "github-actions[bot]"}},
			{"body": f"apply-analysis-cycle-baseline-sha: {OLD_BASELINE}", "user": {"login": "shubhodeep1"}, "author_association": "OWNER"},
			{"body": "another progress comment", "user": {"login": "github-actions[bot]"}},
		],
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stderr
	assert "reason=guard_unavailable" not in proc.stdout
	assert "reason=no_code_changes_since_last_cycle" in proc.stdout
	assert not final.get("dispatches")


def test_untrusted_baseline_marker_comment_is_ignored() -> None:
	# An outside account posting the current tip as a baseline must not make
	# the tick believe a cycle already covered it.
	state = {
		"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])},
		"last_cycle_issue": 900,
		"last_cycle_baseline": TIP,
		"cycle_comments": [{"body": f"apply-analysis-cycle-baseline-sha: {TIP}", "user": {"login": "stranger"}, "author_association": "NONE"}],
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, _final, _ = _run(Path(tmp), state)
	assert "only in comments from untrusted authors" in proc.stderr
	assert "reason=no_code_changes_since_last_cycle" not in proc.stdout
	# The tick got past the baseline guard and on to the smoke gate.
	assert "Dispatching test-and-mark-stable.yml on main with gate_only=true" in proc.stdout


def test_diverged_main_skips_before_the_smoke_gate() -> None:
	# main no longer descends from the stable tag: the promotion's
	# fast-forward would refuse it, so the tick must not spend a cycle.
	state = {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"], status="diverged")}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stderr
	assert f"PROMOTE_CYCLE_SKIPPED reason=base_not_ancestor base={TAG_COMMIT} head={TIP} status=diverged" in proc.stdout
	assert not final.get("dispatches")


def test_tag_lookup_failure_fails_closed() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"tag_lookup_error": True})
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=guard_unavailable lookup=tag:stable" in proc.stdout
	assert not final.get("dispatches")


def test_no_code_change_since_failed_run_skips() -> None:
	# Budget of one: a single failed cycle holds the tip, as before the retry budget.
	failed_head = "4" * 40
	runs = [{"id": 2, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": failed_head, "created_at": "2026-09-18T00:00:00Z"}]
	state = {"self_runs": runs, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), failed_head: _compare(["docs/README.md"])}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env={"PROMOTE_CYCLE_MAX_ATTEMPTS": "1"})
	assert f"PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_failed_run base={failed_head} head={TIP} attempts=1 max=1" in proc.stdout
	assert not final.get("dispatches")


def test_failed_run_on_same_tip_is_retried_within_the_default_budget() -> None:
	# Two failed ticks (one on the tip itself, one a docs-only commit behind
	# it) spend two of the default three attempts; the tick runs the gate.
	docs_only_head = "4" * 40
	runs = [
		{"id": 2, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": docs_only_head, "created_at": "2026-09-17T00:00:00Z"},
		{"id": 3, "status": "completed", "event": "schedule", "conclusion": "timed_out", "head_sha": TIP, "created_at": "2026-09-18T00:00:00Z"},
	]
	state = {"self_runs": runs, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), docs_only_head: _compare(["docs/README.md"])}, "gate_runs_sequence": GATE_SUCCESS}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stderr + proc.stdout
	assert "no_code_changes_since_failed_run" not in proc.stdout
	assert f"Retrying the cycle on {TIP}: 2 failed attempt(s) since the last code change, budget 3." in proc.stdout
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout


def test_failed_runs_on_same_tip_hold_once_the_budget_is_spent() -> None:
	older_head = "4" * 40
	runs = [
		{"id": 2, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": TIP, "created_at": "2026-09-16T00:00:00Z"},
		{"id": 3, "status": "completed", "event": "schedule", "conclusion": "cancelled", "head_sha": older_head, "created_at": "2026-09-17T00:00:00Z"},
		{"id": 4, "status": "completed", "event": "schedule", "conclusion": "success", "head_sha": TIP, "created_at": "2026-09-17T12:00:00Z"},
		{"id": 5, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": TIP, "created_at": "2026-09-18T00:00:00Z"},
	]
	state = {"self_runs": runs, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), older_head: _compare(["docs/notes.md"])}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	# Skipped ticks (conclusion success) are not attempts; the three failed
	# or cancelled ones are, so the newest failed head is reported as base.
	assert f"PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_failed_run base={TIP} head={TIP} attempts=3 max=3" in proc.stdout
	assert not final.get("dispatches")


def test_failed_runs_before_a_code_change_do_not_spend_the_budget() -> None:
	# Newest failed run has no code change to the tip (one attempt); the one
	# before it is separated from the tip by a code change, so the walk stops
	# there and the older failure is never compared or counted.
	recent_head = "4" * 40
	old_head = "6" * 40
	oldest_head = "7" * 40
	runs = [
		{"id": 2, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": oldest_head, "created_at": "2026-09-15T00:00:00Z"},
		{"id": 3, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": old_head, "created_at": "2026-09-16T00:00:00Z"},
		{"id": 4, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": recent_head, "created_at": "2026-09-18T00:00:00Z"},
	]
	state = {"self_runs": runs, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), recent_head: _compare(["docs/README.md"]), old_head: _compare(["scripts/y.sh"]), oldest_head: _compare(["docs/README.md"])}, "gate_runs_sequence": GATE_SUCCESS}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env={"PROMOTE_CYCLE_MAX_ATTEMPTS": "2"})
	assert proc.returncode == 0, proc.stderr + proc.stdout
	assert f"Retrying the cycle on {TIP}: 1 failed attempt(s) since the last code change, budget 2." in proc.stdout
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout
	compared = [c[1].split("/compare/")[1].split("...")[0] for c in final["calls"] if c[:1] == ["api"] and "/compare/" in c[1]]
	assert oldest_head not in compared


def test_diverged_newest_failed_run_still_skips_base_not_ancestor() -> None:
	failed_head = "4" * 40
	runs = [{"id": 2, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": failed_head, "created_at": "2026-09-18T00:00:00Z"}]
	state = {"self_runs": runs, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), failed_head: _compare([], status="diverged")}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert f"PROMOTE_CYCLE_SKIPPED reason=base_not_ancestor base={failed_head} head={TIP} status=diverged" in proc.stdout
	assert not final.get("dispatches")


def test_newest_failed_run_compare_error_still_skips_guard_unavailable() -> None:
	failed_head = "4" * 40
	runs = [{"id": 2, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": failed_head, "created_at": "2026-09-18T00:00:00Z"}]
	state = {"self_runs": runs, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), failed_head: "api_error"}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert f"PROMOTE_CYCLE_SKIPPED reason=guard_unavailable compare={failed_head}...{TIP}" in proc.stdout
	assert not final.get("dispatches")


def test_older_failed_run_compare_error_stops_walk_without_skipping() -> None:
	older_head = "4" * 40
	runs = [
		{"id": 2, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": older_head, "created_at": "2026-09-17T00:00:00Z"},
		{"id": 3, "status": "completed", "event": "schedule", "conclusion": "failure", "head_sha": TIP, "created_at": "2026-09-18T00:00:00Z"},
	]
	state = {"self_runs": runs, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), older_head: "api_error"}, "gate_runs_sequence": GATE_SUCCESS}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stderr + proc.stdout
	assert "reason=guard_unavailable" not in proc.stdout
	assert f"Retrying the cycle on {TIP}: 1 failed attempt(s) since the last code change, budget 3." in proc.stdout
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout


def test_invalid_max_attempts_fails_fast() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {}, env={"PROMOTE_CYCLE_MAX_ATTEMPTS": "0"})
	assert proc.returncode == 1
	assert "PROMOTE_CYCLE_MAX_ATTEMPTS must be a positive integer" in proc.stdout + proc.stderr
	assert not final.get("calls")


def test_insufficient_docs_skips_before_the_smoke_gate() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}}, env={"STUB_CANDIDATES": "1"})
	assert "PROMOTE_CYCLE_SKIPPED reason=insufficient_docs available=1 required=2" in proc.stdout
	assert not final.get("dispatches")


def test_full_cycle_runs_smoke_gate_then_dispatches_proving_run() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh", "README.md"])}, "gate_runs_sequence": GATE_SUCCESS})
		env_lines = env_out.read_text(encoding="utf-8").splitlines()
	assert proc.returncode == 0, proc.stderr + proc.stdout
	assert final["dispatches"] == [["test-and-mark-stable.yml", "--repo", "owner/repo", "--ref", "main", "-f", "gate_only=true", "-f", "gate_cycle_id=777"]]
	assert "Smoke gate passed on " + TIP + " (run 500)" in proc.stdout
	assert f"PROMOTE_CYCLE_DISPATCHED doc=analysis/workflow-optimization-2026-08-30.md baseline={TIP} smoke={TIP}" in proc.stdout
	assert "APPLY_ANALYSIS_ROLE=proving" in env_lines
	assert f"APPLY_ANALYSIS_CYCLE_BASELINE_SHA={TIP}" in env_lines
	assert f"APPLY_ANALYSIS_SMOKE_SHA={TIP}" in env_lines
	assert "outcome=dispatched" in final["github_output"]
	assert "smoke_run_id=500" in final["github_output"]


def test_smoke_gate_failure_fails_the_cycle_without_dispatching_a_proving_run() -> None:
	seq = [[], [{"id": 501, "status": "completed", "conclusion": "failure", "head_branch": "main", "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}]]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": seq})
		assert not env_out.exists()
	assert proc.returncode == 1
	assert "PROMOTE_CYCLE_FAILED reason=smoke_gate_failed run=501 conclusion=failure" in proc.stdout
	assert len(final["dispatches"]) == 1


def test_cycle_waits_for_an_active_release_gate_before_dispatching() -> None:
	# A stable release gate is running: dispatching ours now would cancel its
	# e2e job. Wait until the gate workflow is idle, then dispatch as usual.
	stable_gate = [{"id": 400, "status": "in_progress", "conclusion": None, "head_branch": "stable", "head_sha": "9" * 40, "display_title": "Test & Mark Stable Release", "created_at": "2026-09-18T23:50:00Z"}]
	idle = [stable_gate, stable_gate, []]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_SUCCESS, "gate_idle_sequence": idle}, env={"PROMOTE_CYCLE_GATE_WAIT_SECS": "3"})
	assert proc.returncode == 0, proc.stderr + proc.stdout
	assert proc.stdout.count("run(s) active (a stable release gate must not be cancelled by ours)") == 2
	assert "reason=smoke_gate_timeout" not in proc.stdout
	assert "PROMOTE_CYCLE_DISPATCHED doc=analysis/workflow-optimization-2026-08-30.md" in proc.stdout
	# Only the smoke gate goes through gh; the proving run goes through the stub.
	assert [d[0] for d in final["dispatches"]] == ["test-and-mark-stable.yml"]


def test_cycle_skips_when_the_gate_stays_busy_for_the_whole_budget() -> None:
	stable_gate = [{"id": 400, "status": "in_progress", "conclusion": None, "head_branch": "stable", "head_sha": "9" * 40, "display_title": "Test & Mark Stable Release", "created_at": "2026-09-18T23:50:00Z"}]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_idle_sequence": [stable_gate]}, env={"PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS": "3"})
		assert not env_out.exists()
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=gate_busy active_runs=1 waited=3s" in proc.stdout
	assert not final.get("dispatches")
	assert "outcome=skipped:gate_busy" in final["github_output"]


def test_cancelled_smoke_gate_is_a_skip_not_a_failure() -> None:
	seq = [
		[],
		[{"id": 500, "status": "in_progress", "conclusion": None, "head_branch": "main", "head_sha": TIP, "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}],
		[{"id": 500, "status": "completed", "conclusion": "cancelled", "head_branch": "main", "head_sha": TIP, "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}],
	]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": seq})
		assert not env_out.exists()
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=smoke_gate_cancelled run=500" in proc.stdout
	assert "PROMOTE_CYCLE_FAILED" not in proc.stdout
	assert "outcome=skipped:smoke_gate_cancelled" in final["github_output"]


def test_smoke_gate_timeout_fails_the_cycle() -> None:
	seq = [[], [{"id": 502, "status": "in_progress", "conclusion": None, "head_branch": "main", "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}]]
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": seq}, env={"PROMOTE_CYCLE_GATE_WAIT_SECS": "3"})
	assert proc.returncode == 1
	assert "PROMOTE_CYCLE_FAILED reason=smoke_gate_timeout" in proc.stdout


def test_smoke_gate_ignores_unrelated_concurrent_dispatch() -> None:
	seq = [
		[],
		[
			{"id": 503, "status": "completed", "conclusion": "failure", "head_branch": "main", "display_title": "Test & Mark Stable Release", "created_at": "2026-09-19T00:00:01Z"},
			{"id": 504, "status": "in_progress", "conclusion": None, "head_branch": "main", "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:02Z"},
		],
		[{"id": 504, "status": "completed", "conclusion": "success", "head_branch": "main", "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:02Z"}],
	]
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": seq})
	assert proc.returncode == 0, proc.stderr
	assert "Smoke gate run: 504" in proc.stdout
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout


def test_dispatcher_skip_after_gate_is_reported_as_skip() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_SUCCESS}, env={"STUB_DISPATCH_OUTCOME": "skipped"})
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=dispatcher:all_docs_processed smoke_run=500" in proc.stdout


def test_smoke_sha_is_the_gate_runs_head_when_main_advanced_before_the_gate() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_SUCCESS_ON_ADVANCED_MAIN})
		env_lines = env_out.read_text(encoding="utf-8").splitlines()
	assert proc.returncode == 0, proc.stderr + proc.stdout
	assert "main advanced from " + TIP + " to " + ADVANCED_TIP in proc.stdout + proc.stderr
	assert f"Smoke gate passed on {ADVANCED_TIP} (run 500)" in proc.stdout
	assert f"APPLY_ANALYSIS_SMOKE_SHA={ADVANCED_TIP}" in env_lines
	assert f"APPLY_ANALYSIS_CYCLE_BASELINE_SHA={TIP}" in env_lines
	# The dispatcher runs against the smoke-tested commit, not the older tip.
	assert f"GITHUB_SHA={ADVANCED_TIP}" in env_lines
	assert ["checkout", "--quiet", "--detach", ADVANCED_TIP] in final["git_calls"]
	assert f"PROMOTE_CYCLE_DISPATCHED doc=analysis/workflow-optimization-2026-08-30.md baseline={TIP} smoke={ADVANCED_TIP}" in proc.stdout


def test_smoke_head_checkout_failure_fails_the_cycle() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_SUCCESS_ON_ADVANCED_MAIN, "git_fetch_fail": True})
		assert not env_out.exists()
	assert proc.returncode != 0
	assert f"PROMOTE_CYCLE_FAILED reason=smoke_head_checkout_failed sha={ADVANCED_TIP}" in proc.stdout
	# The smoke gate was dispatched (that is what moved main), but nothing else.
	assert [d for d in final.get("dispatches", []) if d and d[0] != "test-and-mark-stable.yml"] == []
	assert "outcome=failed:smoke_head_checkout_failed" in final["github_output"]


# --- operator-step drain (plan item 4d) ---------------------------------------

TICK_ARGS = ["tick", "--repo", "owner/repo", "--stable-sha", TAG_COMMIT, "--repo-dir", str(REPO_ROOT)]


def test_operator_steps_are_ticked_before_the_no_code_changes_skip() -> None:
	# Right after a promotion main equals the tag: the cycle skips, but only after ticking.
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["docs/a.md"])}})
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=no_code_changes" in proc.stdout
	assert final["tick_calls"] == [TICK_ARGS]
	assert '"ticked": ["pr-1"]' in proc.stdout
	fetch = next(call for call in final["git_calls"] if call[:1] == ["fetch"])
	assert "--filter=tree:0" in fetch and "--unshallow" in fetch
	assert "+refs/tags/stable:refs/tags/stable" in fetch and fetch[-1] == "main"


def test_operator_steps_are_ticked_on_a_cycle_that_continues() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"gate_runs_sequence": GATE_SUCCESS, "git_shallow": "false"})
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout, (proc.stdout, proc.stderr)
	assert final["tick_calls"] == [TICK_ARGS]
	fetch = next(call for call in final["git_calls"] if call[:1] == ["fetch"])
	assert "--unshallow" not in fetch  # A full clone rejects --unshallow.


def test_operator_step_tick_failure_is_not_fatal() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["docs/a.md"])}}, env={"STUB_TICK_FAIL": "1"})
	assert proc.returncode == 0, proc.stderr
	assert "::warning::Operator-step tick failed (rc=2)" in proc.stdout
	assert "PROMOTE_CYCLE_SKIPPED reason=no_code_changes" in proc.stdout
	assert final["tick_calls"] == [TICK_ARGS]


def test_operator_step_fetch_failure_still_ticks() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["docs/a.md"])}, "git_fetch_fail": True})
	assert proc.returncode == 0, proc.stderr
	assert "::warning::Operator-step tick: commit history fetch failed" in proc.stdout
	assert final["tick_calls"] == [TICK_ARGS]


def test_operator_step_missing_helper_is_not_fatal() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["docs/a.md"])}},
			env={"PROMOTE_CYCLE_OPERATOR_STEP_HELPER": str(Path(tmp) / "absent.py")})
	assert proc.returncode == 0, proc.stderr
	assert "::warning::Operator-step tick skipped" in proc.stdout
	assert "PROMOTE_CYCLE_SKIPPED reason=no_code_changes" in proc.stdout
	assert final["tick_calls"] == []


def test_operator_steps_are_not_ticked_without_a_tag_or_when_disabled() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		_, final, _ = _run(Path(tmp), {"tag_missing": True, "gate_runs_sequence": GATE_SUCCESS})
	assert final["tick_calls"] == []
	with tempfile.TemporaryDirectory() as tmp:
		_, final, _ = _run(Path(tmp), {"tag_lookup_error": True})
	assert final["tick_calls"] == []
	with tempfile.TemporaryDirectory() as tmp:
		_, final, _ = _run(Path(tmp), {}, env={"PROMOTE_CYCLE_ENABLED": "false"})
	assert final["tick_calls"] == []


# --- failed job / step on the smoke-gate failure line (#7614) -----------------

GATE_FAILURE = [[], [{"id": 501, "status": "completed", "conclusion": "failure", "head_branch": "main", "head_sha": TIP, "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}]]


def _job(name: str, conclusion: str, steps: list[tuple[str, str]]) -> dict:
	return {"name": name, "conclusion": conclusion, "steps": [{"name": n, "conclusion": c} for n, c in steps]}


def test_smoke_gate_failure_logs_failed_job_and_step() -> None:
	jobs = {"jobs": [
		_job("validate", "success", [("Checkout", "success")]),
		_job("e2e-smoke-test", "failure", [("Checkout", "success"), ("Phase 7: Close PR", "failure"), ("Later step", "failure")]),
		_job("release", "failure", [("Tag", "failure")]),
	]}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_FAILURE, "gate_jobs": jobs})
		assert not env_out.exists()
	assert proc.returncode == 1
	assert "PROMOTE_CYCLE_FAILED reason=smoke_gate_failed run=501 conclusion=failure failed_job=e2e-smoke-test failed_step=Phase_7:_Close_PR\n" in proc.stdout
	assert final["jobs_reads"] == ["repos/owner/repo/actions/runs/501/jobs?per_page=100"]
	assert "outcome=failed:smoke_gate_failed" in final["github_output"]


def test_smoke_gate_failure_names_are_sanitised_and_timed_out_job_is_picked() -> None:
	long_step = "bad=step`name " + "x" * 100
	jobs = {"jobs": [
		_job("lint", "success", []),
		_job("smoke job\nnext", "timed_out", [("ok", "success"), (long_step, "failure")]),
	]}
	expected_step = ("badstepname_" + "x" * 100)[:80]
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_FAILURE, "gate_jobs": jobs})
	assert proc.returncode == 1
	assert f"PROMOTE_CYCLE_FAILED reason=smoke_gate_failed run=501 conclusion=failure failed_job=smoke_job_next failed_step={expected_step}\n" in proc.stdout


def test_smoke_gate_failure_job_without_failed_step_logs_unknown_step() -> None:
	jobs = {"jobs": [_job("e2e-smoke-test", "timed_out", [("Wait", "cancelled")])]}
	with tempfile.TemporaryDirectory() as tmp:
		proc, _, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_FAILURE, "gate_jobs": jobs})
	assert proc.returncode == 1
	assert "conclusion=failure failed_job=e2e-smoke-test failed_step=unknown\n" in proc.stdout


def test_smoke_gate_failure_jobs_read_failure_logs_unknown() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_FAILURE, "gate_jobs_error": True}, env={"GH_RETRY_MAX_ATTEMPTS": "1"})
		assert not env_out.exists()
	assert proc.returncode == 1
	assert "PROMOTE_CYCLE_FAILED reason=smoke_gate_failed run=501 conclusion=failure failed_job=unknown failed_step=unknown\n" in proc.stdout
	assert final["jobs_reads"]
	assert len(final["dispatches"]) == 1


def test_successful_or_cancelled_gate_makes_no_jobs_read() -> None:
	cancelled = [[], [{"id": 500, "status": "completed", "conclusion": "cancelled", "head_branch": "main", "head_sha": TIP, "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}]]
	for seq in (GATE_SUCCESS, cancelled):
		with tempfile.TemporaryDirectory() as tmp:
			proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": seq})
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert not final.get("jobs_reads")
		assert not any("/jobs" in a for call in final["calls"] for a in call)


# --- pinned promote cycle (phase 3) -------------------------------------------

PIN_REUSABLE = (
	"cancel_on_pr_close", "check_failure_triage", "clarify", "implement", "issue_pr_status",
	"memory_maintenance", "orchestrate_clarify_respond", "orchestrate_poll", "orchestrate",
	"plan", "review_autofix", "validate", "workflow_failure_heal",
)
PIN_ADVANCED = "4" * 40


def _pinned_repo(tmp: Path, *, wrappers_pinned: bool = True, support_pinned: bool = True) -> Path:
	"""A copy of the script in a fixture repo whose workflow files the pre-flight reads."""
	repo = tmp / "repo"
	(repo / "scripts").mkdir(parents=True)
	workflows = repo / ".github" / "workflows"
	workflows.mkdir(parents=True)
	script = repo / "scripts" / "promote_main_cycle.sh"
	script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
	(workflows / "internal-review.yml").write_text("jobs:\n  r:\n    uses: o/r/.github/workflows/review_autofix.yml@promote-candidate\n", encoding="utf-8")
	ref = "main" if not wrappers_pinned else "promote-candidate"
	(workflows / "internal-plan.yml").write_text(f"jobs:\n  p:\n    uses: o/r/.github/workflows/plan.yml@{ref}\n", encoding="utf-8")
	for index, name in enumerate(PIN_REUSABLE):
		marker = "SUPPORT_REF source=promote_candidate" if support_pinned or index else "SUPPORT_REF ref=main"
		(workflows / f"{name}.yml").write_text(f"jobs:\n  j:\n    steps:\n      - run: echo {marker}\n", encoding="utf-8")
	return script


def _gate_on(branch: str, head: str) -> list:
	run = {"id": 500, "head_branch": branch, "head_sha": head, "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}
	return [[], [dict(run, status="completed", conclusion="success")]]


def _pinned_run(tmp: Path, state: dict, env: dict[str, str] | None = None, **repo_kwargs):
	base = {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "pin_branch": "protected", "gate_runs_sequence": _gate_on("promote-candidate", TIP)}
	base.update(state)
	return _run(tmp, base, env=env, script=_pinned_repo(tmp, **repo_kwargs))


def _assert_unpinned_dispatch(proc, final, reason: str) -> None:
	assert f"PROMOTE_CYCLE_PIN outcome=skipped reason={reason}" in proc.stdout, proc.stdout + proc.stderr
	assert final["dispatches"][0][:5] == ["test-and-mark-stable.yml", "--repo", "owner/repo", "--ref", "main"]
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout and "pinned=false" in proc.stdout
	assert not final.get("ref_creates")


def test_pin_skips_when_disabled() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), {"gate_runs_sequence": GATE_SUCCESS}, env={"PROMOTE_CYCLE_PIN_ENABLED": "FALSE"})
	_assert_unpinned_dispatch(proc, final, "disabled")


def test_pin_skips_when_a_wrapper_is_not_pinned() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), {"gate_runs_sequence": GATE_SUCCESS}, wrappers_pinned=False)
	_assert_unpinned_dispatch(proc, final, "wrappers_not_pinned")


def test_pin_skips_when_support_is_not_pinned() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), {"gate_runs_sequence": GATE_SUCCESS}, support_pinned=False)
	_assert_unpinned_dispatch(proc, final, "support_not_pinned")


def test_pin_skips_when_branch_missing_or_unprotected() -> None:
	for pin_state, reason in (("missing", "branch_missing"), ("unprotected", "branch_unprotected"), ("error", "branch_lookup_failed")):
		with tempfile.TemporaryDirectory() as tmp:
			proc, final, _ = _pinned_run(Path(tmp), {"pin_branch": pin_state, "gate_runs_sequence": GATE_SUCCESS})
		_assert_unpinned_dispatch(proc, final, reason)


def test_real_repo_without_branch_keeps_todays_path() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_SUCCESS})
	assert "PROMOTE_CYCLE_PIN outcome=skipped reason=" in proc.stdout
	assert final["dispatches"][0][4] == "main"


def test_pinned_cycle_freezes_and_gates_the_candidate() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _pinned_run(Path(tmp), {})
		env_lines = env_out.read_text(encoding="utf-8").splitlines()
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert f"PROMOTE_CYCLE_PIN outcome=frozen candidate={TIP}" in proc.stdout
	tag_args = final["tag_objects_created"][0]
	assert "tag=promote-cycle-frozen" in tag_args and f"object={TIP}" in tag_args
	assert any(a.startswith("message=frozen_at=") and a.endswith(" cycle_run=777") for a in tag_args)
	assert "ref=refs/tags/promote-cycle-frozen" in final["ref_creates"][0]
	patch = final["branch_patches"][0]
	assert f"sha={TIP}" in patch and "force=false" in patch
	assert final["dispatches"][0] == ["test-and-mark-stable.yml", "--repo", "owner/repo", "--ref", "promote-candidate", "-f", "gate_only=true", "-f", "gate_cycle_id=777"]
	assert "APPLY_ANALYSIS_PINNED=true" in env_lines
	assert f"APPLY_ANALYSIS_CYCLE_BASELINE_SHA={TIP}" in env_lines
	assert f"APPLY_ANALYSIS_SMOKE_SHA={TIP}" in env_lines
	assert f"PROMOTE_CYCLE_DISPATCHED doc=analysis/workflow-optimization-2026-08-30.md baseline={TIP} smoke={TIP} pinned=true" in proc.stdout
	assert final.get("tag_deletes", 0) == 0
	assert "pinned=true" in final["github_output"]


def test_pinned_cycle_tag_race_skips_as_cycle_in_flight() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _pinned_run(Path(tmp), {"tag_ref_race": True})
		assert not env_out.exists()
	assert proc.returncode == 0
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tag=promote-cycle-frozen" in proc.stdout
	assert final.get("tag_deletes", 0) == 0
	assert not final.get("dispatches")


def test_pinned_cycle_deletes_a_stale_tag_before_freezing() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), {"pin_tag_exists": True})
	assert "PROMOTE_CYCLE_PIN outcome=stale_tag_deleted" in proc.stdout
	assert final["tag_deletes"] == 1
	assert "PROMOTE_CYCLE_PIN outcome=frozen" in proc.stdout


def test_pinned_cycle_reports_the_stale_tag_age() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), {"pin_tag_exists": True, "tag_message": "frozen_at=1 cycle_run=5"})
	assert re.search(r"PROMOTE_CYCLE_PIN outcome=stale_tag_deleted age=[0-9]+s", proc.stdout), proc.stdout
	assert final["tag_deletes"] == 1


def test_pinned_cycle_reconciles_an_advanced_branch() -> None:
	state = {
		"pin_branch_patch": "not_ff",
		"pin_branch_tip": PIN_ADVANCED,
		"compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), TIP: {"status": "ahead", "total_commits": 1, "files": []}},
		"gate_runs_sequence": _gate_on("promote-candidate", PIN_ADVANCED),
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _pinned_run(Path(tmp), state)
		env_lines = env_out.read_text(encoding="utf-8").splitlines()
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert f"PROMOTE_CYCLE_PIN outcome=candidate_advanced main_tip={TIP} candidate={PIN_ADVANCED}" in proc.stdout
	assert f"object={PIN_ADVANCED}" in final["tag_objects_created"][1]
	assert any("force=true" in patch for patch in final["tag_ref_patches"])
	assert f"APPLY_ANALYSIS_SMOKE_SHA={PIN_ADVANCED}" in env_lines
	# The dispatcher reads docs from the checkout, so it must be the candidate.
	assert ["checkout", "--quiet", "--detach", PIN_ADVANCED] in final["git_calls"]


def test_pinned_cycle_trap_deletes_tag_on_gate_failure() -> None:
	seq = [[], [{"id": 501, "status": "completed", "conclusion": "failure", "head_branch": "promote-candidate", "head_sha": TIP, "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}]]
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), {"gate_runs_sequence": seq})
	assert proc.returncode == 1
	assert "PROMOTE_CYCLE_FAILED reason=smoke_gate_failed run=501" in proc.stdout
	assert final["tag_deletes"] == 1


def test_pinned_cycle_gate_on_main_is_not_matched() -> None:
	# The run is matched on head_branch == promote-candidate; a main run is ignored.
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), {"gate_runs_sequence": GATE_SUCCESS})
	assert "PROMOTE_CYCLE_FAILED reason=smoke_gate_timeout" in proc.stdout
	assert final["tag_deletes"] == 1


def test_pinned_cycle_head_mismatch_fails_and_releases() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _pinned_run(Path(tmp), {"gate_runs_sequence": _gate_on("promote-candidate", ADVANCED_TIP)})
		assert not env_out.exists()
	assert proc.returncode == 1
	assert f"PROMOTE_CYCLE_FAILED reason=pin_head_mismatch gate_head={ADVANCED_TIP} candidate={TIP}" in proc.stdout
	assert final["tag_deletes"] == 1


def test_pinned_cycle_freeze_failure_runs_unpinned() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), {"pin_branch_patch": "error", "pin_branch_tip": "0" * 40, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), TIP: {"status": "diverged"}}, "gate_runs_sequence": GATE_SUCCESS})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "PROMOTE_CYCLE_PIN outcome=skipped reason=freeze_failed detail=branch_not_fast_forward" in proc.stdout
	assert final["tag_deletes"] == 1
	assert final["dispatches"][0][4] == "main"
	assert "pinned=false" in proc.stdout


def test_pinned_cycle_tag_ref_create_failure_deletes_tag_and_runs_unpinned() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), {"tag_ref_fail": True, "gate_runs_sequence": GATE_SUCCESS})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "PROMOTE_CYCLE_PIN outcome=skipped reason=freeze_failed detail=tag_ref_create" in proc.stdout
	assert final["tag_deletes"] == 1
	assert final["dispatches"][0][4] == "main"


def test_pinned_cycle_compare_failure_is_reported_as_unavailable() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), {"pin_branch_patch": "error", "pin_branch_tip": "0" * 40, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), TIP: {}}, "gate_runs_sequence": GATE_SUCCESS})
	assert "PROMOTE_CYCLE_PIN outcome=skipped reason=freeze_failed detail=compare_unavailable" in proc.stdout
	assert final["tag_deletes"] == 1


# --- stable branch must be an ancestor of the candidate (#7888) ---------------

STABLE_BRANCH_OTHER = "7" * 40


def _compare_bases(final: dict) -> list[str]:
	return [c[1].split("/compare/")[1].split("...")[0] for c in final["calls"] if c[:1] == ["api"] and "/compare/" in c[1]]


def test_stable_branch_at_the_tag_commit_needs_no_extra_compare() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_SUCCESS})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout
	assert _compare_bases(final) == [TAG_COMMIT]
	assert any(c[:1] == ["api"] and c[1].endswith("/git/ref/heads/stable") for c in final["calls"])


def test_stable_branch_identical_to_the_tip_proceeds() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"stable_branch_sha": TIP, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_SUCCESS})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout
	assert STABLE_BRANCH_OTHER not in _compare_bases(final)


def test_stable_branch_behind_the_tip_with_another_sha_proceeds() -> None:
	state = {
		"stable_branch_sha": STABLE_BRANCH_OTHER,
		"compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), STABLE_BRANCH_OTHER: _compare(["scripts/y.sh"])},
		"gate_runs_sequence": GATE_SUCCESS,
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout
	assert STABLE_BRANCH_OTHER in _compare_bases(final)


def test_stable_branch_not_an_ancestor_skips_before_the_gate() -> None:
	for status in ("behind", "diverged"):
		state = {
			"stable_branch_sha": STABLE_BRANCH_OTHER,
			"compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), STABLE_BRANCH_OTHER: {"status": status, "total_commits": 0, "files": []}},
			"gate_runs_sequence": GATE_SUCCESS,
		}
		with tempfile.TemporaryDirectory() as tmp:
			proc, final, env_out = _run(Path(tmp), state)
			assert not env_out.exists()
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert f"PROMOTE_CYCLE_SKIPPED reason=base_not_ancestor base={STABLE_BRANCH_OTHER} head={TIP} status={status} ref=heads/stable" in proc.stdout
		assert not final.get("dispatches")


def test_stable_branch_lookup_failure_fails_closed() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"stable_branch_error": True, "compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_runs_sequence": GATE_SUCCESS})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=guard_unavailable lookup=branch:stable" in proc.stdout
	assert not final.get("dispatches")


def test_stable_branch_checked_when_the_tag_is_missing() -> None:
	state = {
		"tag_missing": True,
		"stable_branch_sha": STABLE_BRANCH_OTHER,
		"compares": {STABLE_BRANCH_OTHER: {"status": "diverged", "total_commits": 0, "files": []}},
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert "PROMOTE_CYCLE_SKIPPED reason=base_not_ancestor" in proc.stdout and "ref=heads/stable" in proc.stdout
	assert not final.get("dispatches")


def test_pinned_cycle_checks_the_stable_branch_after_the_freeze() -> None:
	# Step 3 sees the branch at the tag commit; by the freeze it moved sideways.
	state = {
		"stable_branch_sequence": [TAG_COMMIT, STABLE_BRANCH_OTHER],
		"compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), STABLE_BRANCH_OTHER: {"status": "diverged", "total_commits": 0, "files": []}},
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, env_out = _pinned_run(Path(tmp), state)
		assert not env_out.exists()
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert f"PROMOTE_CYCLE_PIN outcome=frozen candidate={TIP}" in proc.stdout
	assert f"PROMOTE_CYCLE_SKIPPED reason=base_not_ancestor base={STABLE_BRANCH_OTHER} head={TIP} status=diverged ref=heads/stable" in proc.stdout
	assert not final.get("dispatches")
	assert final["tag_deletes"] == 1
