#!/usr/bin/env python3
"""Contract: the Claude-fixer hand-off in review_autofix.yml is retired.

PR-backed `claude/*` heads now take the normal review path like every other
PR: reviewer panel, GPT editor, conflict resolver, review-blocked judge and
auto-merge (docs/plans/replace-claude-sessions-with-cli-engine-plan.md,
Phase 2). The `claude_fixer_converged_head` input stays declared and ignored
because pinned consumer wrappers still pass it, `CLAUDE_FIXER_ENABLED` is read
but unused until Phase 5c gives it its Claude-engine meaning, and the
`claude-fixer-auto-merge` job id is kept but never runs. Phase 5c rewrites this
file for the new Claude-engine review write roles.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import (  # noqa: E402
	REPO_ROOT,
	REVIEW_AUTOFIX_WORKFLOW_PATH,
	expanded_review_autofix_text,
)

TOPOLOGY_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_merge_topology_gate.sh"
COUNT_ITERATIONS_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_count_iterations.sh"
WRAPPERS = (REPO_ROOT / "workflow-templates" / "ai-review.yml",)
HEAD = "c" * 40
AUTHOR = "workflow-bot"
RETIRED_HANDOFF = f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=1 -->"


def _load(path: Path) -> dict:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	if True in workflow:
		workflow["on"] = workflow.pop(True)
	return workflow


def _steps(workflow: dict, job: str) -> dict[str, dict]:
	return {step["name"]: step for step in workflow["jobs"][job]["steps"] if "name" in step}


WORKFLOW = _load(REVIEW_AUTOFIX_WORKFLOW_PATH)
AGENT_STEPS = _steps(WORKFLOW, "codex-agent")


def test_converged_head_input_is_kept_and_marked_deprecated():
	for trigger in ("workflow_call", "workflow_dispatch"):
		spec = WORKFLOW["on"][trigger]["inputs"]["claude_fixer_converged_head"]
		assert spec["default"] == "" and spec["type"] == "string" and spec["required"] is False
		assert spec["description"].startswith("Deprecated; ignored")
	gate_env = _steps(WORKFLOW, "gate")["Evaluate review gate"]["env"]
	assert "CLAUDE_FIXER_CONVERGED_HEAD" not in gate_env
	assert "CLAUDE_FIXER_VERDICT_BOT_LOGIN" not in gate_env
	assert gate_env["CLAUDE_FIXER_ENABLED"] == "${{ vars.CLAUDE_FIXER_ENABLED || 'true' }}"


def test_wrappers_accept_but_no_longer_pass_the_retired_input():
	for wrapper in WRAPPERS:
		workflow = _load(wrapper)
		spec = workflow["on"]["workflow_dispatch"]["inputs"]["claude_fixer_converged_head"]
		assert spec["description"].startswith("Deprecated; ignored"), wrapper
		assert "claude_fixer_converged_head" not in workflow["jobs"]["review"]["with"], wrapper


def test_handoff_machinery_is_gone():
	text = REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8")
	for needle in (
		"CLAUDE_FIXER_MODE",
		"CLAUDE_FIXER_VERIFICATION",
		"CLAUDE_FIXER_ZERO_FINDINGS",
		"claude_fixer_awaiting_session",
		"gate_claude_handoff_on_head",
		"review_autofix_step_claude_fixer_handoff.sh",
		"Hand review round to Claude session",
		"Label Claude-fixer PR review-blocked",
	):
		assert needle not in text, needle
	# The only ai:claude-fixer-* marker left is the review-skipped notice for
	# claude/* PRs (issue #4985), which is not part of the hand-off.
	assert set(re.findall(r"ai:claude-fixer-[a-z-]+", text)) <= {"ai:claude-fixer-review-skipped"}
	assert "ai:claude-fixer-handoff" not in expanded_review_autofix_text()
	assert not (REPO_ROOT / "scripts" / "review_autofix_step_claude_fixer_handoff.sh").exists()
	outputs = WORKFLOW["jobs"]["gate"]["outputs"]
	for name in ("claude_fixer", "claude_fixer_converged", "claude_fixer_verify"):
		assert name not in outputs, name


def test_legacy_auto_merge_job_is_kept_but_never_runs():
	job = WORKFLOW["jobs"]["claude-fixer-auto-merge"]
	assert job["if"] == "${{ needs.gate.outputs.skip_reason == 'claude_fixer_auto_merge_retired' }}"
	gate_run = _steps(WORKFLOW, "gate")["Evaluate review gate"]["run"]
	assert "claude_fixer_auto_merge_retired" not in gate_run


def test_editor_tail_and_auto_merge_no_longer_depend_on_fixer_mode():
	for name in ("Apply fixes with editor model", "Detect merge conflicts", "Enable auto-merge on PR", "Review-blocked judge decision"):
		assert "CLAUDE_FIXER" not in AGENT_STEPS[name]["if"], name


def test_topology_gate_has_no_claude_branch():
	text = TOPOLOGY_SCRIPT.read_text(encoding="utf-8")
	assert "CLAUDE_FIXER_MODE" not in text
	assert "claude_fixer_handoff" not in text


def test_iteration_counter_counts_claude_autofix_rounds():
	run = COUNT_ITERATIONS_SCRIPT.read_text(encoding="utf-8")
	pattern = re.search(r"grep -Eq '(\^\\\[\(ai\|claude\)-autofix\\\])'", run)
	assert pattern, run
	regex = re.compile(r"^\[(ai|claude)-autofix\]")
	assert regex.match("[claude-autofix] fix review round 2")
	assert regex.match("[ai-autofix] apply PR fixes")
	for subject in ("[claude-intervention] unblock", "[claude-merge-resolve] merge main", "[judge-fix] x"):
		assert not regex.match(subject)



# ---- the real "Evaluate review gate" script, end to end with a stubbed gh ----

GATE_MOCK_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path

state = json.loads(Path(os.environ["MOCK_GATE_STATE"]).read_text())
args = sys.argv[1:]
jq = args[args.index("--jq") + 1] if "--jq" in args else None
path = next((a for a in args[1:] if a.startswith("repos/") or a == "user"), "")

def emit(value):
	text = json.dumps(value)
	if jq is not None:
		text = subprocess.run(["jq", "-rc", jq], input=text, capture_output=True, text=True, check=True).stdout
	sys.stdout.write(text if text.endswith("\n") else text + "\n")

if args[:1] != ["api"]:
	sys.exit(1)
if path == "user":
	emit({"login": state["login"]})
elif path.endswith("/comments"):
	if state.get("comments_fail"):
		sys.exit(1)
	emit(state["comments"])
elif path.endswith("/files"):
	emit(state.get("files") or [{"filename": "scripts/big_change.sh"}])
elif "/pulls/" in path:
	emit(state["pr"])
else:
	sys.exit(1)
'''


def _run_gate(tmp: Path, *, head_ref: str, comments: list[dict], event_name: str = "workflow_dispatch", converged_head: str = "", extra_env: dict | None = None, marker_author: str = AUTHOR, files: list[dict] | None = None, pr_overrides: dict | None = None, comments_fail: bool = False):
	gate_run = _steps(WORKFLOW, "gate")["Evaluate review gate"]["run"]
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(GATE_MOCK_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	state_file = tmp / "state.json"
	state_file.write_text(json.dumps({
		"login": marker_author,
		"comments": None if comments is None else [
			{"id": i, "user": {"login": c["author_login"], "type": c["author_type"]}, "author_association": c["author_association"], "created_at": "2026-09-25T00:00:00Z", "body": c["body"]}
			for i, c in enumerate(comments, 1)
		],
		"pr": {"state": "open", "merged": False, "head": {"ref": head_ref, "sha": HEAD, "repo": {"full_name": "o/r"}}, "labels": [], "additions": 400, "deletions": 50, "mergeable": True, "mergeable_state": "clean", "title": "Demo — phase 1/2: x", "body": "Refs #1", **(pr_overrides or {})},
		"files": files,
		"comments_fail": comments_fail,
	}), encoding="utf-8")
	output_file = tmp / "out.txt"
	output_file.write_text("", encoding="utf-8")
	(tmp / "runner_temp").mkdir()
	env = {
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"HOME": str(tmp),
		"MOCK_GATE_STATE": str(state_file),
		"GITHUB_OUTPUT": str(output_file),
		"RUNNER_TEMP": str(tmp / "runner_temp"),
		"GH_TOKEN": "x",
		"REPOSITORY": "o/r",
		"EVENT_NAME": event_name,
		"EVENT_ACTION": "",
		"PR_NUMBER": "42",
		"PR_HEAD_SHA": "",
		"PR_IS_DRAFT": "false",
		"PR_SKIP_AI": "false",
		"PR_TITLE": "Demo — phase 1/2: x",
		"PR_BODY": "Refs #1",
		"FORCE_RB_JUDGE": "false",
		"REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED": "false",
		"CLAUDE_FIXER_ENABLED": "true",
		# Retired inputs a stale caller might still export; the gate must ignore them.
		"CLAUDE_FIXER_VERDICT_BOT_LOGIN": "dedicated-fixer[bot]",
		"CLAUDE_FIXER_CONVERGED_HEAD": converged_head,
	}
	env.update(extra_env or {})
	script = tmp / "gate.sh"
	script.write_text(gate_run, encoding="utf-8")
	proc = subprocess.run(["bash", str(script)], cwd=tmp, env=env, capture_output=True, text=True)
	outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	return proc, outputs


CLAUDE_REF = "claude/quirky-wozniak-e9t88m"


def _c(body: str, author: str = AUTHOR) -> dict:
	return {"body": body, "author_login": author, "author_type": "User", "author_association": "OWNER"}


def test_gate_runs_claude_prs_like_any_other_pr():
	for head_ref in (CLAUDE_REF, "claude/implement-plan-demo-phase-1", "ai/issue-7"):
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=head_ref, comments=[], event_name="pull_request")
		assert proc.returncode == 0, proc.stderr
		assert out["should_run"] == "true", head_ref
		assert "claude_fixer" not in out, head_ref
		assert "AUTOFIX_GATE_CLAUDE_FIXER" not in proc.stdout, head_ref


def test_gate_ignores_a_leftover_handoff_comment_on_dispatch():
	"""A hand-off comment left by the retired flow no longer parks the PR."""
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=CLAUDE_REF, comments=[_c(RETIRED_HANDOFF)])
	assert proc.returncode == 0, proc.stderr
	assert out["should_run"] == "true"
	assert out.get("skip_reason", "") != "claude_fixer_awaiting_session"


def test_gate_ignores_the_deprecated_converged_head_input():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=CLAUDE_REF, comments=[], converged_head=HEAD)
	assert proc.returncode == 0, proc.stderr
	assert out["should_run"] == "true"
	assert "AUTOFIX_GATE_CLAUDE_FIXER_CONVERGED" not in proc.stdout


DOCS_FILES = [{"filename": "docs/deploy-activation/pr-1.md", "status": "modified"}, {"filename": "notes.md", "status": "added"}]


def test_docs_only_claude_pr_takes_the_deterministic_skip_even_with_a_leftover_handoff():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=CLAUDE_REF, comments=[_c(RETIRED_HANDOFF)], event_name="pull_request",
			files=DOCS_FILES, pr_overrides={"changed_files": 2})
	assert proc.returncode == 0, proc.stderr
	assert out["deterministic_skip"] == "true" and out["det_skip_reason"] == "docs_only"
	assert "claude_handoff_suppressed" not in proc.stdout


def test_small_diff_claude_pr_takes_the_deterministic_skip():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=CLAUDE_REF, comments=[], event_name="pull_request",
			files=[{"filename": "src/app.py", "status": "modified"}],
			pr_overrides={"changed_files": 1, "additions": 3, "deletions": 2})
	assert proc.returncode == 0, proc.stderr
	assert out["deterministic_skip"] == "true" and out["det_skip_reason"] == "small_diff"


def test_unverified_pr_head_never_enters_deterministic_merge():
	assert WORKFLOW["jobs"]["codex-agent"]["if"] == "${{ needs.gate.outputs.should_run == 'true' }}"
	assert WORKFLOW["jobs"]["deterministic-skip-merge"]["if"] == "${{ needs.gate.outputs.deterministic_skip == 'true' }}"
	for head_repo, head_sha in (("other/repo", HEAD), ("", HEAD), ("o/r", "bad-sha")):
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=CLAUDE_REF, comments=[],
				files=DOCS_FILES, pr_overrides={"changed_files": 2,
					"head": {"ref": CLAUDE_REF, "sha": head_sha, "repo": {"full_name": head_repo}}})
		assert proc.returncode == 0, proc.stderr
		assert out["should_run"] == "false" and out["deterministic_skip"] == "false"
		assert out["skip_reason"] == "review_checkout_unverified"
		assert out["review_checkout_sha"] == ""


def test_claude_pr_skip_keeps_the_protected_path_and_size_guards():
	for files, overrides in (
		([{"filename": "CLAUDE.md", "status": "modified"}], {"changed_files": 1, "additions": 2, "deletions": 1}),
		([{"filename": "src/app.py", "status": "modified"}], {"changed_files": 1, "additions": 40, "deletions": 2}),
	):
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=CLAUDE_REF, comments=[], event_name="pull_request",
				files=files, pr_overrides=overrides)
		assert proc.returncode == 0, proc.stderr
		assert out["deterministic_skip"] == "false" and out["should_run"] == "true", files


# ---- reviewer budget slots (unchanged by the retirement) ----

REVIEWERS_SCRIPT = REPO_ROOT / "scripts" / "review_run_reviewers.sh"


def _reviewers_block(start_marker: str, end_marker: str) -> str:
	text = REVIEWERS_SCRIPT.read_text(encoding="utf-8")
	start = text.index(start_marker)
	return text[start:text.index(end_marker, start)]


def _run_reviewer_pass_with_statuses(td: Path, statuses: list[str]) -> tuple[subprocess.CompletedProcess, Path]:
	"""Run the real `run_reviewer_pass` with each slot ending in the given status.

	`run_reviewer` is stubbed to write the slot's status file and output, so
	the pass's own tally and partial-finalize decision run unchanged.
	"""
	partial_block = _reviewers_block(
		'REVIEWER_PARTIAL_FINALIZE_REQUEST_FILE="${RUNTIME_DIR:-.}/reviewers_partial_finalize_request.txt"',
		"resolve_ledger_substate_helper() {",
	)
	pass_block = _reviewers_block("run_reviewer_pass() {", "# Wrap a consolidated pass-1 ledger")
	reviews = td / "reviews"
	runtime = td / "runtime"
	reviews.mkdir()
	runtime.mkdir()
	models = "".join(f"vendor/model{index}\\n" for index in range(len(statuses)))
	status_cases = "".join(f'\t\tvendor/model{index}) echo "{status}" ;;\n' for index, status in enumerate(statuses))
	script = (
		"set -euo pipefail\n"
		f"{partial_block}\n"
		"emit_run_budget_gate_note() { :; }\n"
		"codex_run_budget_phase_may_start() { return 0; }\n"
		"reviewer_resume_should_reuse_success_slot() { return 1; }\n"
		"reviewer_circuit_breaker_enabled() { return 1; }\n"
		f"get_active_reviewer_models_text() {{ printf '{models}'; }}\n"
		"slot_status_for() {\n"
		'\tcase "$1" in\n'
		f"{status_cases}"
		"\tesac\n"
		"}\n"
		"run_reviewer() {\n"
		'\tlocal model="$1" safe_name="$2" prefix="$3"\n'
		'\tslot_status_for "${model}" > "${PREVIOUS_REVIEWS_DIR}/status_${prefix}_${safe_name}.txt"\n'
		'\tprintf "(No findings reported.)\\n" > "${PREVIOUS_REVIEWS_DIR}/${prefix}_${safe_name}.txt"\n'
		"}\n"
		f"{pass_block}\n"
		'run_reviewer_pass review "prompt" ""\n'
	)
	env = {
		**os.environ,
		"PREVIOUS_REVIEWS_DIR": str(reviews),
		"RUNTIME_DIR": str(runtime),
		"GITHUB_ENV": str(td / "github_env"),
	}
	proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
	return proc, runtime / "reviewers_partial_finalize_request.txt"


def test_budget_skip_only_pass_still_requests_partial_finalize(tmp_path):
	"""Q47: A. A pass whose only non-success slot is `skipped_budget` still
	requests a partial finalize before the summariser, so no ledger is written.
	"""
	proc, request = _run_reviewer_pass_with_statuses(tmp_path, ["success"] * 3 + ["skipped_budget"])
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.strip().splitlines()[-1] == "3"
	assert request.exists()
	assert "AUTOFIX_PARTIAL_FINALIZE_REASON=soft_deadline" in request.read_text(encoding="utf-8")


def test_budget_skip_beside_a_hard_failure_reaches_the_summariser(tmp_path):
	proc, request = _run_reviewer_pass_with_statuses(tmp_path, ["success"] * 3 + ["failed", "skipped_budget", "skipped_unmapped"])
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.strip().splitlines()[-1] == "3"
	assert not request.exists()
