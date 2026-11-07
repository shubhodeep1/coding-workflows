#!/usr/bin/env python3
"""Tests for scripts/promote_main_cycle.sh (the daily promote cycle)."""

from __future__ import annotations

import json
import os
import stat
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
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
# Release-blocker reporters (Phase 2b): the heal report dispatch.
if args[:3] == ["api", "-X", "POST"] and args[3].endswith("/dispatches"):
    if state.get("heal_dispatch_error"):
        save(); sys.stderr.write("HTTP 403: Resource not accessible by integration\n"); sys.exit(1)
    body = json.loads(Path(args[args.index("--input") + 1]).read_text())
    state.setdefault("heal_dispatches", []).append(body)
    respond({})
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
    # Bounded hold (stuck-states P4): aliased LabeledEvent history batch.
    if path == "graphql":
        import re as _re
        query = next((a[len("query="):] for a in args if a.startswith("query=")), "")
        state.setdefault("graphql_queries", []).append(query)
        if state.get("graphql_error"):
            save(); sys.stderr.write("HTTP 404: Not Found\n"); sys.exit(1)
        repo = {}
        for num in _re.findall(r"i(\d+):issue", query):
            events = state.get("latch_events", {}).get(num, [])
            nodes = [{"createdAt": e["createdAt"], "label": {"name": e["label"]}} for e in events]
            number = int(num) + 1000 if state.get("graphql_wrong_number") else int(num)
            repo["i" + num] = {"number": number, "timelineItems": {"pageInfo": {"hasPreviousPage": bool(state.get("graphql_has_previous"))}, "nodes": nodes}}
        # Release-blocker untrusted-hold check: aliased comments batch.
        for num in _re.findall(r"u(\d+):issue", query):
            repo["u" + num] = {"number": int(num), "comments": {"nodes": state.get("issue_comments_gql", {}).get(num, [])}}
        respond({"data": {"repository": repo}})
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
        if state.get("pin_tag_lookup_error"):
            save(); sys.stderr.write("HTTP 503: Service Unavailable\n"); sys.exit(1)
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
            if state.get("baseline_search_error"):
                save(); sys.stderr.write("HTTP 503\n"); sys.exit(1)
            respond({"total_count": 1 if state.get("last_cycle_issue") else 0, "items": ([{"number": state["last_cycle_issue"]}] if state.get("last_cycle_issue") else [])})
        respond({"total_count": 0, "items": []})
    if "/issues/" in path and path.endswith("/comments?per_page=100"):
        respond(state.get("cycle_comments", [{"body": "apply-analysis-source-doc: x\napply-analysis-cycle-baseline-sha: " + state.get("last_cycle_baseline", ""), "user": {"login": "github-actions[bot]"}, "author_association": "NONE"}]))
    if "/issues?" in path:
        labels = state.get("in_flight_labels", {})
        respond([{"number": n, "labels": [{"name": l} for l in labels.get(str(n), [])]} for n in state.get("in_flight", [])])
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
    if "/actions/runs/" in path and "/jobs" in path and path.split("/actions/runs/")[1].split("/")[0] in state.get("self_run_jobs", {}):
        # Release-blocker streak check: an earlier cycle run's jobs.
        state.setdefault("self_jobs_reads", []).append(path)
        respond(state["self_run_jobs"][path.split("/actions/runs/")[1].split("/")[0]])
    if "/actions/jobs/" in path and path.endswith("/logs"):
        state.setdefault("log_reads", []).append(path)
        if state.get("job_logs_error"):
            save(); sys.stderr.write("HTTP 410: Gone\n"); sys.exit(1)
        save()
        sys.stdout.write(state.get("job_logs", {}).get(path.split("/actions/jobs/")[1].split("/")[0], ""))
        sys.exit(0)
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


MOCK_CURL = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["MOCK_CURL_LOG"], "a", encoding="utf-8") as out:
    out.write(json.dumps(sys.argv[1:]) + "\n")
sys.stdout.write(json.dumps({"ok": True, "result": {"message_id": 1}}))
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
			"RELEASE_BLOCKER_HEAL_PY": str(REPO_ROOT / "scripts" / "workflow_failure_heal.py"),
		}
	)
	for key in (
		"BASH_ENV", "ENV", "TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "TG_CHAT_ID", "ALERT_MSG_LEVEL", "SUPPORT_SCRIPTS_DIR",
		"ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "GITHUB_WORKFLOW", "GITHUB_RUN_ATTEMPT",
		"RELEASE_BLOCKER_HEAL_ENABLED", "RELEASE_BLOCKER_CYCLE_SKIP_STREAK", "RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS", "RELEASE_BLOCKER_CYCLE_STALE_SECS",
	):
		run_env.pop(key, None)
	if env:
		run_env.update(env)
	curl_log = tmp / "curl_log.jsonl"
	if run_env.get("TG_BOT_SECRET"):
		# Opt-in Telegram stub: records each post instead of sending it.
		(bin_dir / "curl").write_text(MOCK_CURL, encoding="utf-8")
		(bin_dir / "curl").chmod(0o755)
		run_env["MOCK_CURL_LOG"] = str(curl_log)
	proc = subprocess.run(["bash", str(script)], cwd=str(tmp), env=run_env, text=True, capture_output=True, timeout=120)
	final = json.loads(state_file.read_text(encoding="utf-8"))
	final["tg_posts"] = [json.loads(line) for line in curl_log.read_text(encoding="utf-8").splitlines()] if curl_log.exists() else []
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
	# The real Telegram helper, so alert assertions in pinned tests can fail.
	(repo / "scripts" / "tg_helpers.sh").write_text((REPO_ROOT / "scripts" / "tg_helpers.sh").read_text(encoding="utf-8"), encoding="utf-8")
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


def test_stable_branch_compare_keeps_the_tag_change_log() -> None:
	state = {
		"stable_branch_sha": STABLE_BRANCH_OTHER,
		"compares": {TAG_COMMIT: _compare(["scripts/x.sh"]), STABLE_BRANCH_OTHER: _compare(["scripts/y.sh"])},
		"gate_runs_sequence": GATE_SUCCESS,
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, _final, _ = _run(Path(tmp), state)
	assert "  scripts/x.sh" in proc.stdout
	assert "  scripts/y.sh" not in proc.stdout


# --- Bounded hold for human-latched tracking issues (stuck-states P4) --------

TG_ENV = {"TG_BOT_SECRET": "test-secret", "TG_ADMIN_CHAT_ID": "123"}


def _hours_ago(hours: float) -> str:
	return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _graphql_calls(final: dict) -> int:
	# The hold guard's LabeledEvent batch only; the release-blocker
	# untrusted-hold check makes its own comments batch on cycle_in_flight.
	return sum(1 for query in final.get("graphql_queries", []) if "LABELED_EVENT" in query)


def test_hold_release_disabled_keeps_todays_skip_and_makes_no_graphql_call() -> None:
	state = {"in_flight": [42], "in_flight_labels": {"42": ["ai:blocked"]}, "latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(30)}]}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env={"PROMOTE_CYCLE_HOLD_RELEASE_ENABLED": "false"})
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout
	assert "PROMOTE_CYCLE_HOLD_RELEASED" not in proc.stdout
	assert _graphql_calls(final) == 0
	assert not final.get("dispatches")


def test_hold_without_latch_label_makes_no_graphql_call() -> None:
	state = {"in_flight": [42], "in_flight_labels": {"42": ["ai:orchestrator-tracking", "ai:comprehensive-test-pending"]}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout
	assert _graphql_calls(final) == 0


def test_old_latch_releases_the_hold_and_alerts() -> None:
	state = {
		"in_flight": [42],
		"in_flight_labels": {"42": ["ai:orchestrator-tracking", "ai:blocked"]},
		"latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(30)}]},
		"gate_runs_sequence": GATE_SUCCESS,
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env=TG_ENV)
	assert proc.returncode == 0, proc.stderr
	assert re.search(r"^PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=42 reason=ai:blocked latch_age_secs=\d+ max_hours=24$", proc.stdout, re.MULTILINE), proc.stdout
	assert "reason=cycle_in_flight" not in proc.stdout
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout
	assert _graphql_calls(final) == 1
	assert len(final["tg_posts"]) == 1
	text = next(a for a in final["tg_posts"][0] if a.startswith("text="))
	assert "WARNING" in text and "#42" in text
	assert "test-secret" not in proc.stdout + proc.stderr


def test_recent_latch_keeps_holding() -> None:
	state = {"in_flight": [42], "in_flight_labels": {"42": ["ai:needs-human"]}, "latch_events": {"42": [{"label": "ai:needs-human", "createdAt": _hours_ago(2)}]}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env=TG_ENV)
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout
	assert "PROMOTE_CYCLE_HOLD_RELEASED" not in proc.stdout
	assert _graphql_calls(final) == 1
	assert final["tg_posts"] == []


def test_custom_cap_releases_a_younger_latch() -> None:
	state = {"in_flight": [42], "in_flight_labels": {"42": ["ai:validation-failed"]}, "latch_events": {"42": [{"label": "ai:validation-failed", "createdAt": _hours_ago(2)}]}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, _final, _ = _run(Path(tmp), state, env={"PROMOTE_CYCLE_HOLD_MAX_HOURS": "1"})
	assert "PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=42 reason=ai:validation-failed" in proc.stdout
	assert "max_hours=1" in proc.stdout
	assert "reason=cycle_in_flight" not in proc.stdout


def test_newest_of_several_latch_labels_sets_the_age() -> None:
	state = {
		"in_flight": [42],
		"in_flight_labels": {"42": ["ai:blocked", "ai:needs-human"]},
		"latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(48)}, {"label": "ai:needs-human", "createdAt": _hours_ago(1)}]},
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, _final, _ = _run(Path(tmp), state)
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout
	assert "PROMOTE_CYCLE_HOLD_RELEASED" not in proc.stdout


def test_missing_label_history_keeps_holding() -> None:
	for has_previous in (False, True):
		state = {
			"in_flight": [42],
			"in_flight_labels": {"42": ["ai:blocked", "ai:harness-broken"]},
			"latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(48)}]},
			"graphql_has_previous": has_previous,
		}
		with tempfile.TemporaryDirectory() as tmp:
			proc, _final, _ = _run(Path(tmp), state)
		assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout, has_previous
		assert "::warning::Promote cycle hold: tracking issue #42 keeps holding (history_incomplete)" in proc.stdout
		assert "PROMOTE_CYCLE_HOLD_RELEASED" not in proc.stdout


def test_unparseable_latch_timestamp_keeps_holding() -> None:
	state = {"in_flight": [42], "in_flight_labels": {"42": ["ai:blocked"]}, "latch_events": {"42": [{"label": "ai:blocked", "createdAt": "not-a-date"}]}}
	with tempfile.TemporaryDirectory() as tmp:
		proc, _final, _ = _run(Path(tmp), state)
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout
	assert "(history_unparseable)" in proc.stdout


def test_graphql_failure_or_wrong_alias_number_keeps_holding() -> None:
	events = {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(48)}]}
	for extra, marker in (({"graphql_error": True}, "label history unavailable"), ({"graphql_wrong_number": True}, "(history_unverified)")):
		state = {"in_flight": [42], "in_flight_labels": {"42": ["ai:blocked"]}, "latch_events": events, **extra}
		with tempfile.TemporaryDirectory() as tmp:
			proc, final, _ = _run(Path(tmp), state)
		assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout, extra
		assert marker in proc.stdout, (extra, proc.stdout)
		assert "PROMOTE_CYCLE_HOLD_RELEASED" not in proc.stdout
		assert not final.get("dispatches")


def test_mixed_holders_skip_lists_only_the_remaining_issue() -> None:
	state = {
		"in_flight": [42, 43],
		"in_flight_labels": {"42": ["ai:blocked"], "43": ["ai:security-pass-failed"]},
		"latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(30)}], "43": [{"label": "ai:security-pass-failed", "createdAt": _hours_ago(3)}]},
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert "PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=42 reason=ai:blocked" in proc.stdout
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=43\n" in proc.stdout
	assert _graphql_calls(final) == 1
	query = final["graphql_queries"][0]
	assert "i42:issue(number:42)" in query and "i43:issue(number:43)" in query


def test_unlatched_holder_still_holds_beside_a_released_one() -> None:
	state = {
		"in_flight": [42, 44],
		"in_flight_labels": {"42": ["ai:blocked"], "44": []},
		"latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(30)}]},
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert "PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=42" in proc.stdout
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=44\n" in proc.stdout
	# Only latch candidates are queried.
	assert "i44:" not in final["graphql_queries"][0]


def test_invalid_hold_max_hours_falls_back_to_24() -> None:
	for bad in ("abc", "0", "08", "1234567"):
		state = {"in_flight": [42], "in_flight_labels": {"42": ["ai:blocked"]}, "latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(30)}]}, "gate_runs_sequence": GATE_SUCCESS}
		with tempfile.TemporaryDirectory() as tmp:
			proc, _final, _ = _run(Path(tmp), state, env={"PROMOTE_CYCLE_HOLD_MAX_HOURS": bad})
		assert proc.returncode == 0, (bad, proc.stderr)
		assert "::warning::PROMOTE_CYCLE_HOLD_MAX_HOURS must be a positive integer; using 24." in proc.stdout, bad
		assert "PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=42 reason=ai:blocked" in proc.stdout, bad
		assert "max_hours=24" in proc.stdout, bad


def test_cycle_step_env_wires_the_hold_and_telegram() -> None:
	text = (REPO_ROOT / ".github" / "workflows" / "promote-main-to-stable.yml").read_text(encoding="utf-8")
	cycle_job = text.split("\n  cycle:\n", 1)[1].split("\n  promote:\n", 1)[0]
	for row in (
		"PROMOTE_CYCLE_HOLD_RELEASE_ENABLED: ${{ vars.PROMOTE_CYCLE_HOLD_RELEASE_ENABLED || 'true' }}",
		"PROMOTE_CYCLE_HOLD_MAX_HOURS: ${{ vars.PROMOTE_CYCLE_HOLD_MAX_HOURS || '24' }}",
		"TG_BOT_SECRET: ${{ secrets.TG_BOT_SECRET }}",
		"TG_ADMIN_CHAT_ID: ${{ vars.TG_ADMIN_CHAT_ID || '' }}",
		"TG_CHAT_ID: ${{ vars.TG_ADMIN_CHAT_ID || '' }}",
		"ALERT_MSG_LEVEL: ${{ vars.ALERT_MSG_LEVEL || 'WARNING' }}",
	):
		assert f"          {row}\n" in cycle_job, row


def test_release_reason_names_the_label_with_the_newest_event() -> None:
	state = {
		"in_flight": [42],
		"in_flight_labels": {"42": ["ai:harness-broken", "ai:blocked"]},
		"latch_events": {"42": [{"label": "ai:harness-broken", "createdAt": _hours_ago(48)}, {"label": "ai:blocked", "createdAt": _hours_ago(30)}]},
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, _final, _ = _run(Path(tmp), state)
	match = re.search(r"^PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=42 reason=(\S+) latch_age_secs=(\d+) ", proc.stdout, re.MULTILINE)
	assert match, proc.stdout
	assert match.group(1) == "ai:blocked"
	assert 29 * 3600 < int(match.group(2)) < 31 * 3600


def test_graphql_failure_reason_is_logged() -> None:
	state = {"in_flight": [42], "in_flight_labels": {"42": ["ai:blocked"]}, "latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(48)}]}, "graphql_error": True}
	with tempfile.TemporaryDirectory() as tmp:
		proc, _final, _ = _run(Path(tmp), state)
	match = re.search(r"^::warning::Promote cycle hold: GraphQL label history read failed: (.+)$", proc.stdout + proc.stderr, re.MULTILINE)
	assert match, proc.stdout + proc.stderr
	assert match.group(1).strip() and match.group(1) != "no error output"
	assert "::" not in match.group(1)


def test_released_tick_runs_the_cycle_unpinned() -> None:
	state = {
		"in_flight": [42],
		"in_flight_labels": {"42": ["ai:blocked"]},
		"latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(30)}]},
		"gate_runs_sequence": GATE_SUCCESS,
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), state, env=TG_ENV)
	assert "PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=42" in proc.stdout
	# The pinned fixture carries tg_helpers.sh, so the release alert is sent.
	assert len(final["tg_posts"]) == 1
	_assert_unpinned_dispatch(proc, final, "released_cycle_holds_pin")
	assert "PROMOTE_CYCLE_PIN outcome=stale_tag_deleted" not in proc.stdout


def test_released_issue_keeps_holding_while_the_freeze_tag_exists() -> None:
	state = {
		"in_flight": [42],
		"in_flight_labels": {"42": ["ai:blocked"]},
		"latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(30)}]},
		"pin_tag_exists": True,
		"gate_runs_sequence": GATE_SUCCESS,
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _pinned_run(Path(tmp), state, env=TG_ENV)
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42 tag=promote-cycle-frozen" in proc.stdout
	assert "PROMOTE_CYCLE_HOLD_RELEASED" not in proc.stdout
	assert not final.get("dispatches")
	assert not final.get("tag_deletes")
	assert final["tg_posts"] == []


def test_released_issue_freeze_tag_lookup_failure_fails_closed() -> None:
	state = {
		"in_flight": [42],
		"in_flight_labels": {"42": ["ai:blocked"]},
		"latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(30)}]},
		"pin_tag_lookup_error": True,
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env={"GH_RETRY_MAX_ATTEMPTS": "1"})
	assert "PROMOTE_CYCLE_SKIPPED reason=guard_unavailable lookup=tag:promote-cycle-frozen" in proc.stdout
	assert not final.get("dispatches")


def test_freeze_tag_is_ignored_with_pinning_disabled() -> None:
	state = {
		"in_flight": [42],
		"in_flight_labels": {"42": ["ai:blocked"]},
		"latch_events": {"42": [{"label": "ai:blocked", "createdAt": _hours_ago(30)}]},
		"pin_tag_exists": True,
		"gate_runs_sequence": GATE_SUCCESS,
	}
	with tempfile.TemporaryDirectory() as tmp:
		proc, _final, _ = _run(Path(tmp), state, env={"PROMOTE_CYCLE_PIN_ENABLED": "false"})
	assert "PROMOTE_CYCLE_HOLD_RELEASED tracking_issue=42" in proc.stdout
	assert "reason=cycle_in_flight" not in proc.stdout
	assert "PROMOTE_CYCLE_DISPATCHED" in proc.stdout


# --- Release-blocker reporters (stuck-states P4 / Phase 2b, Refs #8291) ------

STABLE_GATE_BUSY = [{"id": 400, "status": "in_progress", "conclusion": None, "head_branch": "stable", "head_sha": "9" * 40, "display_title": "Test & Mark Stable Release", "created_at": "2026-09-18T23:50:00Z"}]
GATE_BUSY_ENV = {"PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS": "1"}
UNTRUSTED_ALERT = "## ⚠️ Untrusted apply-analysis marker\n\nThis tracking issue carries `ai:comprehensive-test-pending` but ..."


def _reports(final: dict) -> list[dict]:
	return [body["client_payload"]["report"] for body in final.get("heal_dispatches", [])]


def _prior_tick(run_id: int, run_number: int, hours: float, skip: str = "gate_busy", conclusion: str = "success") -> tuple[dict, dict, dict]:
	"""An earlier scheduled cycle run, its jobs listing and its cycle job log."""
	run = {"id": run_id, "run_number": run_number, "status": "completed", "event": "schedule", "conclusion": conclusion, "head_sha": TIP, "created_at": _hours_ago(hours)}
	jobs = {str(run_id): {"jobs": [{"id": run_id + 9000, "name": "cycle"}]}}
	log = {str(run_id + 9000): f"2026-11-02T00:00:01.0000000Z Waiting\n2026-11-02T00:30:01.1234567Z PROMOTE_CYCLE_SKIPPED reason={skip} active_runs=1 waited=1800s\r\n"}
	return run, jobs, log


def _streak_state(*ticks: tuple[dict, dict, dict], **extra) -> dict:
	state = {"compares": {TAG_COMMIT: _compare(["scripts/x.sh"])}, "gate_idle_sequence": [STABLE_GATE_BUSY], "self_runs": [], "self_run_jobs": {}, "job_logs": {}}
	for run, jobs, log in ticks:
		state["self_runs"].append(run)
		state["self_run_jobs"].update(jobs)
		state["job_logs"].update(log)
	state.update(extra)
	return state


def test_gate_busy_streak_reports_cycle_gate_unavailable_once() -> None:
	state = _streak_state(_prior_tick(701, 11, 24), _prior_tick(700, 10, 48, skip="smoke_gate_cancelled"))
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env=GATE_BUSY_ENV)
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=gate_busy" in proc.stdout
	assert "outcome=skipped:gate_busy" in final["github_output"]
	assert proc.stdout.index("RELEASE_BLOCKER_REPORTED reason=cycle_gate_unavailable") < proc.stdout.index("PROMOTE_CYCLE_SKIPPED")
	assert re.search(rf"^RELEASE_BLOCKER_REPORTED reason=cycle_gate_unavailable age=\d+ branch=main sha={TIP} run_ref=none$", proc.stdout, re.MULTILINE), proc.stdout
	reports = _reports(final)
	assert len(reports) == 1
	assert final["heal_dispatches"][0]["event_type"] == "workflow-failure-heal"
	report = reports[0]
	assert report["source_kind"] == "release_blocker"
	assert report["blocker_reason"] == "cycle_gate_unavailable"
	assert report["head_branch"] == "main"
	assert report["head_sha"] == TIP
	assert report["run_refs"] == []
	assert report["workflow_name"] == "Promote main to stable"
	assert report["reporter_run"]["run_id"] == "777"
	assert report["blocker_age_secs"] >= 47 * 3600
	lines = report["blocker_evidence"].splitlines()
	assert lines[0] == "reason=cycle_gate_unavailable"
	assert any("actions/runs/777 reason=gate_busy" in line for line in lines)
	assert any("actions/runs/700 reason=smoke_gate_cancelled" in line for line in lines)
	assert len(final["self_jobs_reads"]) == 2 and len(final["log_reads"]) == 2


def test_gate_busy_without_enough_prior_ticks_sends_nothing() -> None:
	state = _streak_state(_prior_tick(701, 11, 24))
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env=GATE_BUSY_ENV)
	assert "PROMOTE_CYCLE_SKIPPED reason=gate_busy" in proc.stdout
	assert not final.get("heal_dispatches")
	assert not final.get("self_jobs_reads") and not final.get("log_reads")
	assert "RELEASE_BLOCKER_" not in proc.stdout


def test_prior_tick_with_another_skip_breaks_the_streak() -> None:
	state = _streak_state(_prior_tick(701, 11, 24), _prior_tick(700, 10, 48, skip="no_code_changes"))
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env=GATE_BUSY_ENV)
	assert "PROMOTE_CYCLE_SKIPPED reason=gate_busy" in proc.stdout
	assert not final.get("heal_dispatches")
	assert "RELEASE_BLOCKER_REPORTED" not in proc.stdout


def test_non_success_prior_tick_makes_no_job_or_log_read() -> None:
	state = _streak_state(_prior_tick(701, 11, 24), _prior_tick(700, 10, 48, conclusion="failure"))
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env=GATE_BUSY_ENV)
	assert "PROMOTE_CYCLE_SKIPPED reason=gate_busy" in proc.stdout
	assert not final.get("heal_dispatches")
	assert not final.get("self_jobs_reads") and not final.get("log_reads")


def test_smoke_gate_cancelled_streak_reports() -> None:
	seq = [
		[],
		[{"id": 500, "status": "completed", "conclusion": "cancelled", "head_branch": "main", "head_sha": TIP, "display_title": "Test & Mark Stable Release [cycle:777]", "created_at": "2026-09-19T00:00:01Z"}],
	]
	state = _streak_state(_prior_tick(701, 11, 24, skip="smoke_gate_cancelled"), _prior_tick(700, 10, 48), gate_runs_sequence=seq, gate_idle_sequence=[])
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=smoke_gate_cancelled run=500" in proc.stdout
	reports = _reports(final)
	assert [r["blocker_reason"] for r in reports] == ["cycle_gate_unavailable"]
	assert "reason=smoke_gate_cancelled detail=run=500" in reports[0]["blocker_evidence"]


def test_log_read_error_is_guard_unavailable_and_keeps_the_tick() -> None:
	state = _streak_state(_prior_tick(701, 11, 24), _prior_tick(700, 10, 48), job_logs_error=True)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env=GATE_BUSY_ENV)
	assert proc.returncode == 0, proc.stderr
	assert "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=cycle_gate_unavailable lookup=logs run=701" in proc.stdout
	assert "PROMOTE_CYCLE_SKIPPED reason=gate_busy" in proc.stdout
	assert "outcome=skipped:gate_busy" in final["github_output"]
	assert not final.get("heal_dispatches")


def _untrusted_state(comments: list[dict]) -> dict:
	return {"in_flight": [42], "in_flight_labels": {"42": ["ai:orchestrator-tracking", "ai:comprehensive-test-pending"]}, "issue_comments_gql": {"42": comments}}


def _gql_comment(body: str, hours: float, login: str = "owner", association: str = "OWNER") -> dict:
	return {"createdAt": _hours_ago(hours), "body": body, "authorAssociation": association, "author": {"login": login}}


def test_old_untrusted_marker_alert_reports_cycle_held_untrusted_marker() -> None:
	state = _untrusted_state([_gql_comment(UNTRUSTED_ALERT, 30), _gql_comment("state", 1, login="github-actions", association="NONE")])
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout
	reports = _reports(final)
	assert [r["blocker_reason"] for r in reports] == ["cycle_held_untrusted_marker"]
	assert reports[0]["head_branch"] == "main"
	assert reports[0]["head_sha"] == TIP
	assert reports[0]["blocker_evidence"].splitlines()[0] == "reason=cycle_held_untrusted_marker"
	assert "/issues/42 alerted_at=" in reports[0]["blocker_evidence"]
	query = next(q for q in final["graphql_queries"] if "u42:issue" in q)
	assert "comments(last:30)" in query
	assert _graphql_calls(final) == 0


def test_untrusted_hold_not_reported_when_young_superseded_or_untrusted() -> None:
	baseline = "apply-analysis-source-doc: x\napply-analysis-cycle-baseline-sha: " + "a" * 40
	cases = {
		"young": [_gql_comment(UNTRUSTED_ALERT, 2)],
		"superseded": [_gql_comment(UNTRUSTED_ALERT, 30), _gql_comment(baseline, 20, login="github-actions", association="NONE")],
		"untrusted_author": [_gql_comment(UNTRUSTED_ALERT, 30, login="drive-by", association="NONE")],
	}
	for name, comments in cases.items():
		with tempfile.TemporaryDirectory() as tmp:
			proc, final, _ = _run(Path(tmp), _untrusted_state(comments))
		assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout, name
		assert not final.get("heal_dispatches"), name
		assert "RELEASE_BLOCKER_REPORTED" not in proc.stdout, name


def test_untrusted_comments_read_failure_is_guard_unavailable() -> None:
	state = {**_untrusted_state([_gql_comment(UNTRUSTED_ALERT, 30)]), "graphql_error": True}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state)
	assert "RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable blocker=cycle_held_untrusted_marker lookup=untrusted_comments" in proc.stdout
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout
	assert not final.get("heal_dispatches")


def _stale_compare(commit_hours: float) -> dict:
	entry = _compare(["scripts/x.sh"])
	entry["commits"] = [{"commit": {"committer": {"date": _hours_ago(commit_hours)}}}]
	return entry


def _stale_state(commit_hours: float, marker_hours: float | None) -> dict:
	comments = [{"body": "apply-analysis-source-doc: x\napply-analysis-cycle-baseline-sha: " + OLD_BASELINE, "user": {"login": "github-actions[bot]"}, "author_association": "NONE"}]
	if marker_hours is not None:
		comments[0]["created_at"] = _hours_ago(marker_hours)
	return {
		"compares": {TAG_COMMIT: _stale_compare(commit_hours), OLD_BASELINE: _compare(["docs/a.md"])},
		"last_cycle_issue": 9,
		"last_cycle_baseline": OLD_BASELINE,
		"cycle_comments": comments,
	}


def test_cycle_stale_reports_when_the_later_clock_is_past_the_threshold() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), _stale_state(commit_hours=200, marker_hours=80))
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_last_cycle" in proc.stdout
	reports = _reports(final)
	assert [r["blocker_reason"] for r in reports] == ["cycle_stale"]
	# max(marker 80h ago, oldest commit 200h ago) = 80h.
	assert 79 * 3600 <= reports[0]["blocker_age_secs"] <= 81 * 3600
	evidence = reports[0]["blocker_evidence"]
	assert evidence.splitlines()[0] == "reason=cycle_stale"
	assert "skip_reason=no_code_changes_since_last_cycle" in evidence


def test_cycle_stale_not_reported_under_the_threshold_or_for_excluded_skips() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), _stale_state(commit_hours=200, marker_hours=10))
	assert "PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_last_cycle" in proc.stdout
	assert not final.get("heal_dispatches")
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"compares": {TAG_COMMIT: _stale_compare(200)}}, env={"STUB_CANDIDATES": "1"})
	assert "PROMOTE_CYCLE_SKIPPED reason=insufficient_docs" in proc.stdout
	assert not final.get("heal_dispatches")
	assert "RELEASE_BLOCKER_" not in proc.stdout
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), {"in_flight": [42], "compares": {TAG_COMMIT: _stale_compare(200)}})
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight" in proc.stdout
	assert not final.get("heal_dispatches")


def test_only_one_report_per_tick() -> None:
	state = _streak_state(_prior_tick(701, 11, 24), _prior_tick(700, 10, 48))
	state["compares"] = {TAG_COMMIT: _stale_compare(200)}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env=GATE_BUSY_ENV)
	assert [r["blocker_reason"] for r in _reports(final)] == ["cycle_gate_unavailable"]
	assert proc.stdout.count("RELEASE_BLOCKER_REPORTED") == 1


def test_kill_switch_sends_nothing_and_reads_nothing() -> None:
	state = _streak_state(_prior_tick(701, 11, 24), _prior_tick(700, 10, 48))
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env={**GATE_BUSY_ENV, "RELEASE_BLOCKER_HEAL_ENABLED": "false"})
	assert "RELEASE_BLOCKER_REPORT_SKIPPED reason=disabled" in proc.stdout
	assert "PROMOTE_CYCLE_SKIPPED reason=gate_busy" in proc.stdout
	assert not final.get("heal_dispatches")
	assert not final.get("self_jobs_reads") and not final.get("log_reads")
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), _untrusted_state([_gql_comment(UNTRUSTED_ALERT, 30)]), env={"RELEASE_BLOCKER_HEAL_ENABLED": "false"})
	assert not final.get("graphql_queries")
	assert not final.get("heal_dispatches")


def test_denied_dispatch_alerts_and_keeps_the_tick_outcome() -> None:
	state = _streak_state(_prior_tick(701, 11, 24), _prior_tick(700, 10, 48), heal_dispatch_error=True)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env={**GATE_BUSY_ENV, **TG_ENV, "GH_RETRY_MAX_ATTEMPTS": "1"})
	assert proc.returncode == 0, proc.stderr
	assert "RELEASE_BLOCKER_REPORT_SKIPPED reason=dispatch_denied blocker=cycle_gate_unavailable detail=" in proc.stdout
	assert "PROMOTE_CYCLE_SKIPPED reason=gate_busy" in proc.stdout
	assert "outcome=skipped:gate_busy" in final["github_output"]
	assert len(final["tg_posts"]) == 1
	text = next(a for a in final["tg_posts"][0] if a.startswith("text="))
	assert "WARNING" in text and "cycle_gate_unavailable" in text
	assert "test-secret" not in proc.stdout + proc.stderr


def test_invalid_release_blocker_settings_warn_and_fall_back() -> None:
	env = {"RELEASE_BLOCKER_CYCLE_SKIP_STREAK": "11", "RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS": "1d", "RELEASE_BLOCKER_CYCLE_STALE_SECS": "-5"}
	with tempfile.TemporaryDirectory() as tmp:
		proc, _final, _ = _run(Path(tmp), {"in_flight": [42]}, env=env)
	assert proc.returncode == 0, proc.stderr
	assert "::warning::RELEASE_BLOCKER_CYCLE_SKIP_STREAK must be an integer from 1 to 10 (got '11'); using 3." in proc.stdout
	assert "::warning::RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS must be a non-negative integer (got '1d'); using 86400." in proc.stdout
	assert "::warning::RELEASE_BLOCKER_CYCLE_STALE_SECS must be a non-negative integer (got '-5'); using 259200." in proc.stdout
	assert "PROMOTE_CYCLE_SKIPPED reason=cycle_in_flight tracking_issues=42" in proc.stdout


def test_cycle_job_grants_oidc_and_wires_the_reporter_settings() -> None:
	text = (REPO_ROOT / ".github" / "workflows" / "promote-main-to-stable.yml").read_text(encoding="utf-8")
	cycle_job = text.split("\n  cycle:\n", 1)[1].split("\n  promote:\n", 1)[0]
	permissions = cycle_job.split("    permissions:\n", 1)[1].split("\n    steps:\n", 1)[0]
	assert "      id-token: write" in permissions
	for row in (
		"RELEASE_BLOCKER_HEAL_ENABLED: ${{ vars.RELEASE_BLOCKER_HEAL_ENABLED || 'true' }}",
		"RELEASE_BLOCKER_CYCLE_SKIP_STREAK: ${{ vars.RELEASE_BLOCKER_CYCLE_SKIP_STREAK || '3' }}",
		"RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS: ${{ vars.RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS || '86400' }}",
		"RELEASE_BLOCKER_CYCLE_STALE_SECS: ${{ vars.RELEASE_BLOCKER_CYCLE_STALE_SECS || '259200' }}",
	):
		assert f"          {row}\n" in cycle_job, row


def test_untrusted_alert_heading_matches_the_poller() -> None:
	# The detector regex in promote_main_cycle.sh must match the heading the
	# poller posts (orchestrate_poll_process.sh), or the reporter goes silent.
	script = SCRIPT.read_text(encoding="utf-8")
	assert 'test("^## .{0,8}Untrusted apply-analysis marker")' in script
	poller = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
	headings = [line.split('"', 1)[1] for line in poller.splitlines() if "Untrusted apply-analysis marker" in line and 'post_tracking_comment "' in line]
	assert headings, "poller no longer posts the untrusted-marker alert"
	assert all(re.match(r"^## .{0,8}Untrusted apply-analysis marker", heading) for heading in headings)


def test_leading_zero_untrusted_hold_threshold_still_detects() -> None:
	# 0086400 passes validation; it must reach jq --argjson as 86400.
	state = _untrusted_state([_gql_comment(UNTRUSTED_ALERT, 30)])
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env={"RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS": "0086400"})
	assert proc.returncode == 0, proc.stderr
	assert "lookup=untrusted_parse" not in proc.stdout
	assert [r["blocker_reason"] for r in _reports(final)] == ["cycle_held_untrusted_marker"]


def test_ansi_coloured_prior_log_still_counts_toward_the_streak() -> None:
	first = _prior_tick(701, 11, 24)
	second = _prior_tick(700, 10, 48)
	log_id = str(700 + 9000)
	second[2][log_id] = "2026-11-02T00:30:01.1234567Z \x1b[36;1mPROMOTE_CYCLE_SKIPPED reason=gate_busy active_runs=1\x1b[0m\n"
	state = _streak_state(first, second)
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env=GATE_BUSY_ENV)
	assert proc.returncode == 0, proc.stderr
	assert [r["blocker_reason"] for r in _reports(final)] == ["cycle_gate_unavailable"]


def test_failed_baseline_search_never_reports_cycle_stale() -> None:
	# guard_unavailable before guard 4's marker read must not fall back to the
	# oldest-commit clock alone (it would report a stale cycle falsely).
	state = {**_stale_state(commit_hours=200, marker_hours=1), "baseline_search_error": True}
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), state, env={"GH_RETRY_MAX_ATTEMPTS": "1"})
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=guard_unavailable" in proc.stdout
	assert not final.get("heal_dispatches")
	assert "RELEASE_BLOCKER_REPORTED" not in proc.stdout


def _diverged_stable_state(marker_hours: float) -> dict:
	state = {**_stale_state(commit_hours=200, marker_hours=marker_hours), "stable_branch_sha": STABLE_BRANCH_OTHER}
	state["compares"][STABLE_BRANCH_OTHER] = {"status": "diverged", "total_commits": 0, "files": []}
	return state


def test_diverged_stable_branch_still_reports_cycle_stale() -> None:
	# base_not_ancestor on heads/stable skips before guard 4 on every tick; the
	# reporter reads the baseline marker itself, so the clock is still the
	# later of the marker (80h) and the oldest unreleased commit (200h).
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), _diverged_stable_state(marker_hours=80))
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=base_not_ancestor" in proc.stdout and "ref=heads/stable" in proc.stdout
	reports = _reports(final)
	assert [r["blocker_reason"] for r in reports] == ["cycle_stale"]
	assert "skip_reason=base_not_ancestor" in reports[0]["blocker_evidence"]
	assert "baseline_marker_at=none" not in reports[0]["blocker_evidence"]
	assert 79 * 3600 <= reports[0]["blocker_age_secs"] <= 81 * 3600


def test_diverged_stable_branch_with_recent_marker_does_not_report() -> None:
	# A recent cycle marker keeps a stable-diverged tick (hotfix before
	# sync-to-main) from reporting on the oldest-commit clock alone.
	with tempfile.TemporaryDirectory() as tmp:
		proc, final, _ = _run(Path(tmp), _diverged_stable_state(marker_hours=1))
	assert proc.returncode == 0, proc.stderr
	assert "PROMOTE_CYCLE_SKIPPED reason=base_not_ancestor" in proc.stdout
	assert not final.get("heal_dispatches")
	assert "RELEASE_BLOCKER_REPORTED" not in proc.stdout
