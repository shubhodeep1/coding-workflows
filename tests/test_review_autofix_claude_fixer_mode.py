#!/usr/bin/env python3
"""Contract: Claude-fixer mode is the Claude engine of the review write roles.

The session hand-off is retired (docs/plans/replace-claude-sessions-with-cli-
engine-plan.md, Phase 2): PR-backed `claude/*` heads take the normal review
path like every other PR. The `claude_fixer_converged_head` input stays
declared and ignored because pinned consumer wrappers still pass it, and the
`claude-fixer-auto-merge` job id is kept but never runs.

Phase 5c (Q19/Q35) gives `CLAUDE_FIXER_ENABLED` (default `true`) its new
meaning: the review editor, consolidator, conflict resolver and RB judge run
through the Claude engine inside the review job, and `false` keeps all four
on their unchanged OpenCode commands, ahead of the labels and AI_ENGINE.
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



# ---- Phase 5c: Claude-fixer mode is the review write roles' Claude engine ----

FIXER_ROLES = ("REVIEW_EDITOR", "REVIEW_CONSOLIDATOR", "CONFLICT_RESOLVER", "RB_JUDGE")


def _run_resolve_step(tmp: Path, **env_overrides: str) -> dict[str, str]:
	step = AGENT_STEPS["Resolve AI engine"]
	github_env = tmp / "github_env"
	github_output = tmp / "github_output"
	env = {
		"PATH": os.environ.get("PATH", ""),
		"HOME": str(tmp),
		"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
		"GITHUB_ENV": str(github_env),
		"GITHUB_OUTPUT": str(github_output),
		"AI_ENGINE": "",
		"CLAUDE_FIXER_ENABLED": "true",
		**{f"AI_ENGINE_{role}": "" for role in FIXER_ROLES},
		**env_overrides,
	}
	proc = subprocess.run(["bash", "-c", step["run"]], cwd=tmp, env=env, capture_output=True, text=True)
	assert proc.returncode == 0, proc.stderr
	values = {}
	for path in (github_env, github_output):
		for line in path.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			values[key] = value
	return values


def test_resolve_step_wiring():
	step = AGENT_STEPS["Resolve AI engine"]
	assert step["id"] == "ai_engine"
	assert step["env"]["CLAUDE_FIXER_ENABLED"] == "${{ vars.CLAUDE_FIXER_ENABLED || 'true' }}"
	state = AGENT_STEPS["Check PR state (defense-in-depth)"]["run"]
	assert 'pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" || echo \'null\')"' in state
	assert 'echo "AI_ENGINE_LABELS=${AI_ENGINE_LABELS}" >> "$GITHUB_ENV"' in state
	assert '["ai:codex"]' in state
	for role in FIXER_ROLES:
		assert step["env"][f"AI_ENGINE_{role}"] == f"${{{{ vars.AI_ENGINE_{role} || '' }}}}"
	names = list(AGENT_STEPS)
	for name, uses in (
		("Install Claude Code CLI", "./.codex-workflow-src/.github/actions/install-claude"),
		("Resolve Claude credential", "./.codex-workflow-src/.github/actions/claude-pool-token"),
	):
		assert AGENT_STEPS[name]["uses"] == uses
		assert AGENT_STEPS[name]["continue-on-error"] is True
		assert AGENT_STEPS[name]["if"] == "env.PR_CLOSED != 'true' && steps.ai_engine.outputs.any_claude == 'true'"
		assert names.index("Resolve AI engine") < names.index(name) < names.index("Install project dependencies (best-effort)")


def test_fixer_mode_on_puts_the_four_roles_on_claude(tmp_path):
	values = _run_resolve_step(tmp_path)
	assert values["any_claude"] == "true"
	for role in FIXER_ROLES:
		assert values[f"AI_ENGINE_RESOLVED_{role}"] == "claude", role


def test_fixer_mode_off_beats_ai_engine_and_role_variables(tmp_path):
	values = _run_resolve_step(tmp_path, CLAUDE_FIXER_ENABLED="false", AI_ENGINE="claude", AI_ENGINE_RB_JUDGE="claude")
	assert values["any_claude"] == "false"
	for role in FIXER_ROLES:
		assert values[f"AI_ENGINE_RESOLVED_{role}"] == "codex", role


def test_a_role_variable_still_moves_one_role_to_codex(tmp_path):
	values = _run_resolve_step(tmp_path, AI_ENGINE_CONFLICT_RESOLVER="codex")
	assert values["AI_ENGINE_RESOLVED_CONFLICT_RESOLVER"] == "codex"
	assert values["AI_ENGINE_RESOLVED_REVIEW_EDITOR"] == "claude"


def test_pr_labels_take_precedence_over_engine_variables(tmp_path):
	values = _run_resolve_step(tmp_path, AI_ENGINE_LABELS='["ai:codex"]', AI_ENGINE="claude")
	assert {values[f"AI_ENGINE_RESOLVED_{role}"] for role in FIXER_ROLES} == {"codex"}
	values = _run_resolve_step(tmp_path, AI_ENGINE_LABELS='["ai:engine-claude"]', AI_ENGINE="codex")
	assert {values[f"AI_ENGINE_RESOLVED_{role}"] for role in FIXER_ROLES} == {"claude"}


def test_pr_state_label_snapshot_fails_closed_on_unavailable_metadata(tmp_path):
	state = AGENT_STEPS["Check PR state (defense-in-depth)"]["run"]
	label_line = next(line.strip() for line in state.splitlines() if line.strip().startswith('AI_ENGINE_LABELS="$('))
	for metadata, expected in (
		('{"labels":[{"name":"ai:engine-claude"},{"name":"other"}]}', '["ai:engine-claude","other"]'),
		('null', '["ai:codex"]'),
		('{"labels":"malformed"}', '["ai:codex"]'),
	):
		result = subprocess.run(
			["bash", "-c", 'set -euo pipefail\npr_meta="$PR_META"\n' + label_line + '\nprintf "%s\\n" "$AI_ENGINE_LABELS"'],
			env={**os.environ, "PR_META": metadata},
			cwd=tmp_path,
			capture_output=True,
			text=True,
			check=False,
		)
		assert result.returncode == 0, result.stderr
		assert result.stdout.strip() == expected


def test_missing_engine_keeps_every_role_on_codex(tmp_path):
	values = _run_resolve_step(tmp_path, SUPPORT_SCRIPTS_DIR=str(tmp_path))
	assert values["any_claude"] == "false"
	assert {values[f"AI_ENGINE_RESOLVED_{role}"] for role in FIXER_ROLES} == {"codex"}


def test_sandbox_prepare_follows_both_roles_with_an_opencode_fallback():
	run = AGENT_STEPS["Install project dependencies (best-effort)"]["run"]
	assert 'review_untrusted_sandbox.sh" prepare claude; then' in run
	assert '[ "${AI_ENGINE_RESOLVED_REVIEW_CONSOLIDATOR:-codex}" = "claude" ]' in run
	assert 'for role in REVIEW_EDITOR REVIEW_CONSOLIDATOR; do' in run
	assert 'echo "${resolved}=codex" >> "$GITHUB_ENV"' in run
	assert run.count('bash "${SUPPORT_SCRIPTS_DIR}/review_untrusted_sandbox.sh" prepare\n') == 2


def test_engine_files_ride_the_optional_bootstrap():
	text = (REPO_ROOT / "scripts" / "stage_workflow_support.sh").read_text(encoding="utf-8")
	line = next(l for l in text.splitlines() if l.startswith("OPTIONAL_BOOTSTRAP_SCRIPTS="))
	for name in ("ai_engine.sh", "claude_engine.py", "claude_anthropic_relay.py", "claude_settings.json.tmpl"):
		assert name in line.split("=", 1)[1].strip("\"").split(), name


# Each review script: the Claude branch, the 75 gate, and the OpenCode
# command after it (G4); consolidation uses a fresh sandbox.
REVIEW_SITES = {
	"review_apply_fixes.sh": (
		'if [ "${AI_ENGINE_RESOLVED_REVIEW_EDITOR:-codex}" = "claude" ]; then',
		'[ "${editor_claude_rc}" -eq 75 ] || return "${editor_claude_rc}"',
		'      -- "${editor_opencode_cmd[@]}" < "${prompt_file}" 2>"${stderr_target}"',
	),
	"review_consolidate.sh": (
		'claude REVIEW_CONSOLIDATOR read \\',
		'elif consolidator_opencode_sandbox_prepare; then',
		'				-- "${consolidator_cmd[@]}" < "${CONSOLIDATOR_PROMPT_FILE}"; then',
	),
	"review_conflict_resolve.sh": (
		'_resolver_sandbox_attempt claude || resolver_claude_rc=$?',
		'if [ "${resolver_claude_rc}" -eq 75 ]; then',
		'_resolver_sandbox_attempt codex || resolver_claude_rc=$?',
	),
	"review_rb_judge.sh": (
		'review_rb_claude_run read "${RB_JUDGE_PROMPT}" "${RB_JUDGE_OUTPUT}" "${JUDGE_STDERR_FILE}" "${level}" || rb_judge_claude_rc=$?',
		'if [ -x "${CODEX_STALL_GUARD_HELPER}" ]; then',
		'      -- "${judge_codex_cmd[@]}" < "${RB_JUDGE_PROMPT}" || rc=$?',
	),
}


def test_each_review_role_tries_claude_then_opencode():
	for script, (claude_call, gate, opencode_call) in REVIEW_SITES.items():
		text = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
		assert text.count(claude_call) == 1, script
		start = text.index(claude_call)
		gate_at = text.index(gate, start)
		assert text.index(opencode_call, gate_at) > gate_at, script
	rb = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	assert 'review_rb_claude_run write "${RB_FIX_PROMPT}" "${RB_FIX_OUTPUT}" "${RB_FIX_STDERR}" "${JUDGE_EFFECTIVE_REASONING_EFFORT}" || rb_fix_claude_rc=$?' in rb


FAKE_SANDBOX = r'''#!/usr/bin/env bash
case "$1" in
  prepare-ephemeral)
    [ "${MODE}" != prepare_failed ] || exit 1
    if [ "${MODE}" = prepare_partial_cleanup_failed ]; then echo "${FAKE_ROOT}"; exit 1; fi
    count=0
    [ ! -f "${CALLS}" ] || count=$(grep -c '^prepare-ephemeral' "${CALLS}" || true)
    [ "${MODE}" != prepare_second_failed ] || [ "${count}" -eq 0 ] || exit 1
    if [ "${RESOLVER_TEST:-false}" = true ] || [ "${MODE}" = claude_unavailable_then_success ]; then
      echo "${FAKE_ROOT}-${count}"
    else
      echo "${FAKE_ROOT}"
    fi
    echo prepare-ephemeral >> "${CALLS}"
    ;;
  run)
    [ "$#" -eq 9 ] || exit 2
    printf 'run|%s|%s|%s|%s\n' "$7" "$8" "$9" "${REVIEW_SANDBOX_ROOT}" >> "${CALLS}"
    case "${MODE}" in
      success) printf 'verdict\n' > "$3" ;;
      claude_unavailable_then_success|prepare_second_failed)
        [ "$7" != claude ] || exit 75
        printf 'verdict\n' > "$3" ;;
      transfer_failed) : > "${RUNTIME_DIR}/review_sandbox_transfer_failed"; exit 1 ;;
      unsafe_directory) printf '::error::Review isolation snapshot or transfer rejected (ValueError) reason=unsafe_directory category=other depth=2\n' > "${RUNTIME_DIR}/review_sandbox_transfer_reason_${3##*/}"; : > "${RUNTIME_DIR}/review_sandbox_transfer_failed"; exit 1 ;;
      marker_on_success) : > "${RUNTIME_DIR}/review_sandbox_transfer_failed" ;;
      unavailable) exit 75 ;;
      claude_unavailable) if [ "$7" = claude ]; then printf 'stale\n' > "$3"; exit 75; fi ;;
      outdated) exit 2 ;;
      *) exit 1 ;;
    esac
    ;;
  cleanup) echo cleanup >> "${CALLS}"; case "${MODE}" in cleanup_failed|prepare_partial_cleanup_failed) exit 1 ;; esac ;;
esac
'''


FAKE_CONSOLIDATOR_SANDBOX = r'''#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  prepare-ephemeral)
    [ "$#" -eq 2 ] && [ "$2" = codex ] || exit 2
    printf 'prepare-ephemeral\n' >> "${MOCK_CONSOLIDATOR_CALLS}"
    [ "${MOCK_SANDBOX_MODE:-}" != prepare_failed ] || exit 1
    printf '%s\n' "${MOCK_CONSOLIDATOR_ROOT}"
    ;;
  run)
    [ "$#" -eq 9 ] || exit 2
    printf 'run|%s|%s|%s|%s\n' "$7" "$8" "$9" "${REVIEW_SANDBOX_ROOT}" >> "${MOCK_CONSOLIDATOR_CALLS}"
    if [ "$7" = claude ]; then
      [ "${MOCK_CLAUDE_RC:-0}" -eq 0 ] || exit "${MOCK_CLAUDE_RC}"
    elif [ "${MOCK_SANDBOX_MODE:-}" = run_outdated ] || [ "${MOCK_SANDBOX_MODE:-}" = run_outdated_cleanup_failed ]; then
      exit 2
    fi
    [ -z "${MOCK_CONSOLIDATOR_PROMPT_CAPTURE:-}" ] || cp "$2" "${MOCK_CONSOLIDATOR_PROMPT_CAPTURE}"
    cp "${MOCK_OPENCODE_OUTPUT_FILE}" "$3"
    exit "${MOCK_CONSOLIDATOR_RUN_RC:-0}"
    ;;
  cleanup)
    printf 'cleanup\n' >> "${MOCK_CONSOLIDATOR_CALLS}"
    case "${MOCK_SANDBOX_MODE:-}" in cleanup_failed|run_outdated_cleanup_failed) exit 1 ;; esac
    ;;
  *) exit 2 ;;
esac
'''


def install_consolidator_mock_support(tmp: Path, *, sandbox: bool = True, support_dir: Path | None = None) -> Path:
	"""Use real trusted helpers, replacing only the sandbox with a recording stub."""
	support_dir = support_dir or tmp / "consolidator_support"
	support_dir.mkdir(exist_ok=True)
	for script in (REPO_ROOT / "scripts").iterdir():
		if script.is_file() and script.name != "review_untrusted_sandbox.sh":
			if not (support_dir / script.name).exists():
				(support_dir / script.name).symlink_to(script)
	if sandbox:
		(support_dir / "review_untrusted_sandbox.sh").write_text(FAKE_CONSOLIDATOR_SANDBOX, encoding="utf-8")
	return support_dir


def _run_consolidator_sandbox_case(tmp: Path, *, mode: str = "success", engine: str = "codex", claude_rc: int = 0, sandbox: bool = True):
	support = install_consolidator_mock_support(tmp, sandbox=sandbox)
	runtime = tmp / "runtime"
	runtime.mkdir()
	(runtime / "reviewer_bundle.txt").write_text("Finding in src/a.py\n", encoding="utf-8")
	fixture = tmp / "fixture.txt"
	fixture.write_text("=== ISSUE example ===\nFILE: src/a.py\n=== END ISSUE example ===\n", encoding="utf-8")
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "opencode").write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo 1.18.23; else touch "$HOST_WRITER_MARKER"; fi\n', encoding="utf-8")
	(bin_dir / "opencode").chmod(0o755)
	config_writer = tmp / "config_writer.sh"
	config_writer.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do if [ "$1" = --config-path ]; then printf "{}\\n" > "$2"; fi; shift; done\n', encoding="utf-8")
	env = dict(os.environ, SUPPORT_SCRIPTS_DIR=str(support), SUPPORT_PROMPTS_DIR=str(REPO_ROOT / "prompts"),
		RUNTIME_DIR=str(runtime), OPENCODE_CONFIG_WRITER_PATH=str(config_writer),
		MOCK_CONSOLIDATOR_ROOT=str(tmp / "isolated"), MOCK_CONSOLIDATOR_CALLS=str(tmp / "calls"),
		MOCK_OPENCODE_OUTPUT_FILE=str(fixture), MOCK_SANDBOX_MODE=mode, MOCK_CLAUDE_RC=str(claude_rc),
		HOST_WRITER_MARKER=str(tmp / "host_writer"), AI_ENGINE_RESOLVED_REVIEW_CONSOLIDATOR=engine,
		PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", PYTHONDONTWRITEBYTECODE="1",
		AI_MEMORY_ENABLED="false", CODEX_HEARTBEAT_ENABLED="0")
	if engine == "claude":
		env["REVIEW_SANDBOX_ROOT"] = str(tmp / "shared")
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "TG_BOT_SECRET", "TG_ADMIN_CHAT_ID"):
		env.pop(inherited, None)
	proc = subprocess.run(["bash", str(REPO_ROOT / "scripts" / "review_consolidate.sh")],
		cwd=tmp, env=env, capture_output=True, text=True)
	calls = (tmp / "calls").read_text(encoding="utf-8").splitlines() if (tmp / "calls").exists() else []
	return proc, calls, runtime / "consolidator_raw.txt"


def test_consolidator_default_and_claude_unavailable_use_fresh_read_only_sandbox(tmp_path):
	for engine, claude_rc in (("codex", 0), ("claude", 75)):
		work = tmp_path / engine
		work.mkdir()
		proc, calls, output = _run_consolidator_sandbox_case(work, engine=engine, claude_rc=claude_rc)
		assert proc.returncode == 0, proc.stderr
		assert calls[-3:] == ["prepare-ephemeral", f"run|codex|REVIEW_CONSOLIDATOR|read|{work / 'isolated'}", "cleanup"]
		assert output.read_text(encoding="utf-8").startswith("=== ISSUE example ===")
		assert not (work / "host_writer").exists()


def test_consolidator_isolation_failures_skip_without_host_writer(tmp_path):
	for index, (mode, sandbox, reason) in enumerate((
		("prepare_failed", True, "sandbox_prepare_failed"),
		("run_outdated", True, "sandbox_helper_outdated"),
		("run_outdated_cleanup_failed", True, "sandbox_helper_outdated"),
		("success", False, "sandbox_support_missing"),
		("cleanup_failed", True, "sandbox_cleanup_failed"),
	)):
		work = tmp_path / str(index)
		work.mkdir()
		proc, calls, output = _run_consolidator_sandbox_case(work, mode=mode, sandbox=sandbox)
		assert proc.returncode == 0, proc.stderr
		assert output.read_text(encoding="utf-8") == ""
		assert f"CONSOLIDATOR_ISOLATION outcome=skipped reason={reason}" in proc.stderr
		assert not (work / "host_writer").exists()
		if mode in ("run_outdated", "run_outdated_cleanup_failed", "cleanup_failed"):
			assert "cleanup" in calls
		if mode == "run_outdated_cleanup_failed":
			assert "::warning::Consolidator sandbox cleanup failed" in proc.stderr
			assert "CONSOLIDATOR_ISOLATION outcome=skipped reason=sandbox_cleanup_failed" not in proc.stderr


def _resolver_claude_sections() -> str:
	text = (REPO_ROOT / "scripts" / "review_conflict_resolve.sh").read_text(encoding="utf-8")
	functions = ""
	for name in ("_resolver_disable_opencode_snapshot", "_resolver_fail_closed", "_resolver_sandbox_attempt", "_resolver_sandbox_opencode_attempt"):
		match = re.search(rf"^{name}\(\)\n\{{\n.*?^\}}\n", text, re.M | re.S)
		assert match, name
		functions += match.group(0) + "\n"
	call = text[text.index('    resolver_claude_rc=75\n'):text.index('  resolver_clean_output="${tmp_output}.ansi-clean"')]
	return functions + "\n" + call


def _run_resolver_claude_section(tmp: Path, *, mode: str, engine: str = "claude", path: str = "scripts/a.py", config_fail: bool = False, helpers: bool = True):
	scripts = tmp / "scripts"
	scripts.mkdir()
	if helpers:
		(scripts / "review_untrusted_sandbox.sh").write_text(FAKE_SANDBOX, encoding="utf-8")
		(scripts / "review_untrusted_workspace.py").write_bytes((REPO_ROOT / "scripts" / "review_untrusted_workspace.py").read_bytes())
	config_writer = scripts / "config_writer.sh"
	config_writer.write_text('#!/usr/bin/env bash\n[ "${CONFIG_FAIL:-false}" != true ] || exit 1\n'
		'while [ "$#" -gt 0 ]; do\n'
		'  if [ "$1" = --config-path ]; then printf \'{}\\n\' > "$2"; fi\n'
		'  printf "%s\\n" "$1" >> "${CONFIG_ARGS}"\n  shift\ndone\n', encoding="utf-8")
	(tmp / "paths.txt").write_text(path + "\n", encoding="utf-8")
	(tmp / "prompt.txt").write_text("Resolve this conflict\n", encoding="utf-8")
	if mode == "output_unavailable":
		(tmp / "output.txt").symlink_to(scripts, target_is_directory=True)
	# The call site is run after setup, with the host branch intact as a sentinel.
	functions, call = _resolver_claude_sections().split('    resolver_claude_rc=75\n', 1)
	script = ("set -euo pipefail\n" + functions + '\nemit_conflict_resolver_substate() { :; }\n'
		'attempt=1\ntmp_output="${RUNTIME_DIR}/output.txt"\n_stall_status_file="${RUNTIME_DIR}/status.txt"\n'
		'_effective_prompt_file="${RUNTIME_DIR}/prompt.txt"\n_current_reasoning_effort=high\n'
		'CONFLICTED_PATHS_FILE="${RUNTIME_DIR}/paths.txt"\nCONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS=5\n'
		'CODEX_STALL_GUARD_HELPER=/not/staged\nCODEX_HEARTBEAT_HELPER=/not/staged\n'
		'RESOLVER_OPENCODE_CONFIG="${RUNTIME_DIR}/resolver_sandbox_opencode.json"\n'
		'resolver_opencode_cmd=(bash -c \'echo host >> "$RUNTIME_DIR/host"\')\n_codex_exit=0\n'
		'if true; then\n' + '    resolver_claude_rc=75\n' + call + '\n'
		'printf "codex_exit=%s\\n" "${_codex_exit}"\n')
	env = dict(os.environ, SUPPORT_SCRIPTS_DIR=str(scripts), OPENCODE_CONFIG_WRITER_PATH=str(config_writer),
		MODEL_EDITOR="openai/gpt-6-sol", AI_ENGINE_RESOLVED_CONFLICT_RESOLVER=engine,
		MODE=mode, RESOLVER_TEST="true", FAKE_ROOT=str(tmp / "root"), RUNTIME_DIR=str(tmp),
		CONFIG_FAIL="true" if config_fail else "false", CONFIG_ARGS=str(tmp / "config_args"), CALLS=str(tmp / "calls"))
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
		env.pop(inherited, None)
	proc = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True)
	return proc, (tmp / "calls").read_text(encoding="utf-8").splitlines() if (tmp / "calls").exists() else []


def test_resolver_claude_isolation_failures_never_call_host(tmp_path):
	for index, (mode, path, helpers, config_fail, reason) in enumerate((
		("prepare_failed", "scripts/a.py", True, False, "sandbox_prepare_failed"),
		("prepare_partial_cleanup_failed", "scripts/a.py", True, False, "sandbox_prepare_failed"),
		("success", ".github/ai/WORKFLOW.md", True, False, "sandbox_path_unsupported"),
		("success", "scripts/a.py", False, False, "sandbox_prepare_failed"),
		("outdated", "scripts/a.py", True, False, "sandbox_helper_outdated"),
		("cleanup_failed", "scripts/a.py", True, False, "sandbox_cleanup_failed"),
		("transfer_failed", "scripts/a.py", True, False, "sandbox_transfer_failed"),
		("output_unavailable", "scripts/a.py", True, False, "sandbox_output_unavailable"),
		("claude_unavailable", "scripts/a.py", True, True, "opencode_config_failed"),
		("unavailable", "scripts/a.py", True, False, "sandbox_opencode_unavailable"),
	)):
		work = tmp_path / str(index)
		work.mkdir()
		proc, calls = _run_resolver_claude_section(work, mode=mode, path=path, helpers=helpers, config_fail=config_fail)
		assert proc.returncode == 1, (reason, proc.stderr)
		assert f"reason={reason} action=fail_closed" in proc.stderr
		assert not (work / "host").exists()
		if reason == "sandbox_path_unsupported":
			assert calls == []
		if reason == "opencode_config_failed":
			assert calls[-1] == "cleanup"
		if reason == "sandbox_output_unavailable":
			assert calls == ["prepare-ephemeral", "cleanup"]
		if reason == "sandbox_transfer_failed":
			assert (work / "review_sandbox_transfer_failed").exists()


def test_resolver_claude_retry_uses_distinct_sandboxes_and_no_host(tmp_path):
	proc, calls = _run_resolver_claude_section(tmp_path, mode="claude_unavailable")
	assert proc.returncode == 0, proc.stderr
	assert "codex_exit=0" in proc.stdout
	assert "reason=claude_unavailable action=sandbox_opencode" in proc.stderr
	assert calls == ["prepare-ephemeral", f"run|claude|CONFLICT_RESOLVER|write|{tmp_path / 'root-0'}", "cleanup",
		"prepare-ephemeral", f"run|codex|CONFLICT_RESOLVER|write|{tmp_path / 'root-1'}", "cleanup"]
	assert not (tmp_path / "host").exists()
	assert (tmp_path / "output.txt").read_text(encoding="utf-8") == ""
	assert (tmp_path / "config_args").read_text(encoding="utf-8").splitlines()[-2:] == ["--serena", "off"]
	assert json.loads((tmp_path / "resolver_sandbox_opencode.json").read_text(encoding="utf-8"))["snapshot"] is False


def test_resolver_selected_engine_and_failed_transfer(tmp_path):
	for mode, engine in (("success", "claude"), ("success", "codex")):
		work = tmp_path / (mode + engine)
		work.mkdir()
		proc, calls = _run_resolver_claude_section(work, mode=mode, engine=engine)
		assert proc.returncode == 0, proc.stderr
		assert "codex_exit=0" in proc.stdout
		assert not (work / "host").exists()
		assert calls == ["prepare-ephemeral", f"run|{engine}|CONFLICT_RESOLVER|write|{work / 'root-0'}", "cleanup"]


def _rb_helper() -> str:
	text = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	return text[text.index("review_rb_claude_run()\n{"):text.index("# Fallback: if gh_helpers.sh")]


def _run_rb_helper(tmp: Path, *, engine: str, mode: str, access: str = "read", stage: bool = True):
	scripts = tmp / "scripts"
	scripts.mkdir()
	if stage:
		(scripts / "review_untrusted_sandbox.sh").write_text(FAKE_SANDBOX, encoding="utf-8")
	(tmp / "prompt.txt").write_text("judge\n", encoding="utf-8")
	calls = tmp / "calls"
	script = _rb_helper() + f'rc=0; review_rb_claude_run {access} prompt.txt out.txt err.txt high || rc=$?; echo "rc=$rc flag=${{RB_SANDBOX_TRANSFER_FAILED:-false}}"\n'
	env = dict(os.environ, SUPPORT_SCRIPTS_DIR=str(scripts), RB_OPENCODE_WORKSPACE=str(tmp), MODEL_EDITOR="openai/gpt-6-sol",
		AI_ENGINE_RESOLVED_RB_JUDGE=engine, MODE=mode, CALLS=str(calls), FAKE_ROOT=str(tmp / "fake-root"), RUNTIME_DIR=str(tmp))
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
		env.pop(inherited, None)
	proc = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True)
	return proc, (calls.read_text(encoding="utf-8") if calls.exists() else "")


def test_rb_verdict_pass_runs_claude_read_only(tmp_path):
	proc, calls = _run_rb_helper(tmp_path, engine="claude", mode="success")
	assert "rc=0" in proc.stdout, proc.stderr
	assert calls.splitlines() == ["prepare-ephemeral", f"run|claude|RB_JUDGE|read|{tmp_path / 'fake-root'}", "cleanup"]
	assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "verdict\n"


def test_rb_fix_pass_keeps_the_write_profile(tmp_path):
	_proc, calls = _run_rb_helper(tmp_path, engine="claude", mode="success", access="write")
	assert calls.splitlines()[1] == f"run|claude|RB_JUDGE|write|{tmp_path / 'fake-root'}"


def test_rb_helper_returns_75_off_claude_unavailable_or_unstaged(tmp_path):
	for index, (engine, mode, stage) in enumerate((("codex", "success", True), ("claude", "unavailable", True), ("claude", "success", False), ("claude", "outdated", True), ("claude", "prepare_failed", True))):
		work = tmp_path / str(index)
		work.mkdir()
		proc, recorded = _run_rb_helper(work, engine=engine, mode=mode, stage=stage)
		assert "rc=75" in proc.stdout, (engine, mode, stage, proc.stderr)
		if mode in ("unavailable", "outdated"):
			assert recorded.splitlines()[-1] == "cleanup"
	crash = tmp_path / "crash"
	crash.mkdir()
	proc, _calls = _run_rb_helper(crash, engine="claude", mode="crash")
	assert "rc=1" in proc.stdout


def test_rb_helper_keeps_transfer_failure_after_cleanup(tmp_path):
	for mode in ("transfer_failed", "marker_on_success"):
		work = tmp_path / mode
		work.mkdir()
		# A stale marker must not count as a failure of this attempt.
		(work / "review_sandbox_transfer_failed").touch()
		proc, calls = _run_rb_helper(work, engine="claude", mode=mode, access="write")
		assert "rc=1 flag=true" in proc.stdout, proc.stderr
		assert calls.splitlines()[-1] == "cleanup"
		assert not (work / "review_sandbox_transfer_failed").exists()
	clean = tmp_path / "clean"
	clean.mkdir()
	(clean / "review_sandbox_transfer_failed").touch()
	proc, _ = _run_rb_helper(clean, engine="claude", mode="success", access="write")
	assert "rc=0 flag=false" in proc.stdout, proc.stderr


def test_rb_helper_reports_unsafe_directory_without_repeating_path(tmp_path):
	proc, calls = _run_rb_helper(tmp_path, engine="claude", mode="unsafe_directory", access="write")
	assert "rc=1 flag=true" in proc.stdout, proc.stderr
	assert "sandbox transfer failed; refusing to commit the fix. reason=unsafe_directory category=other depth=2" in proc.stderr
	assert "dir=.claude/commands" not in proc.stderr
	assert calls.splitlines()[-1] == "cleanup"


def _run_rb_isolated_fallback(tmp: Path, *, engine: str, mode: str, access: str = "read", stage: bool = True):
	scripts = tmp / "scripts"
	scripts.mkdir()
	if stage:
		(scripts / "review_untrusted_sandbox.sh").write_text(FAKE_SANDBOX, encoding="utf-8")
	(tmp / "prompt.txt").write_text("judge\n", encoding="utf-8")
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "opencode").write_text('#!/bin/sh\ntouch "${HOST_WRITER_MARKER}"\n', encoding="utf-8")
	(bin_dir / "opencode").chmod(0o755)
	calls = tmp / "calls"
	script = (_rb_helper() + '''
review_rb_prepare_opencode_config() {
  [ "$MODE" != config_failed ] || return 1
  printf '%s' "$1" > "$3"
}
rb_claude_rc=0
review_rb_claude_run "$ACCESS" prompt.txt out.txt err.txt high || rb_claude_rc=$?
rc="$rb_claude_rc"
if [ "$rb_claude_rc" -eq 75 ]; then
  if review_rb_opencode_sandbox_prepare config.json "$RB_TEST_PHASE" err.txt off; then
    cmd=(env "REVIEW_SANDBOX_ROOT=${RB_OC_SANDBOX_ROOT}" bash "${SUPPORT_SCRIPTS_DIR}/review_untrusted_sandbox.sh" run prompt.txt out.txt "${MODEL_EDITOR}" high config.json codex RB_JUDGE "$ACCESS")
    rc=0
    "${cmd[@]}" || rc=$?
    review_rb_opencode_sandbox_finish "$rc" out.txt err.txt || rc=$?
  else
    rc=$?
  fi
fi
printf 'claude=%s rc=%s reason=%s flag=%s\\n' "$rb_claude_rc" "$rc" "${RB_OC_ISOLATION_REASON:-}" "${RB_SANDBOX_TRANSFER_FAILED:-false}"
''')
	env = dict(os.environ, SUPPORT_SCRIPTS_DIR=str(scripts), RB_OPENCODE_WORKSPACE=str(tmp), MODEL_EDITOR="openai/gpt-6-sol",
		AI_ENGINE_RESOLVED_RB_JUDGE=engine, MODE=mode, ACCESS=access, CALLS=str(calls), FAKE_ROOT=str(tmp / "fake-root"),
		RUNTIME_DIR=str(tmp), RB_TEST_PHASE="review_rb_judge" if access == "read" else "review_rb_fix",
		PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", HOST_WRITER_MARKER=str(tmp / "host-writer"))
	for inherited in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
		env.pop(inherited, None)
	proc = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True)
	return proc, (calls.read_text(encoding="utf-8") if calls.exists() else ""), tmp / "host-writer"


def test_rb_codex_off_uses_isolated_opencode_for_both_access_modes(tmp_path):
	for access in ("read", "write"):
		work = tmp_path / access
		work.mkdir()
		proc, calls, host_writer = _run_rb_isolated_fallback(work, engine="codex", mode="success", access=access)
		assert "claude=75 rc=0" in proc.stdout, proc.stderr
		assert calls.splitlines() == ["prepare-ephemeral", f"run|codex|RB_JUDGE|{access}|{work / 'fake-root'}", "cleanup"]
		assert (work / "config.json").read_text(encoding="utf-8") == ("reviewer" if access == "read" else "writer")
		assert not host_writer.exists()


def test_rb_claude_unavailable_tries_new_isolated_root(tmp_path):
	proc, calls, host_writer = _run_rb_isolated_fallback(tmp_path, engine="claude", mode="claude_unavailable_then_success")
	assert "claude=75 rc=0" in proc.stdout, proc.stderr
	assert calls.splitlines() == ["prepare-ephemeral", f"run|claude|RB_JUDGE|read|{tmp_path / 'fake-root-0'}", "cleanup",
		"prepare-ephemeral", f"run|codex|RB_JUDGE|read|{tmp_path / 'fake-root-1'}", "cleanup"]
	assert not host_writer.exists()


def test_rb_isolation_failure_defers_without_host_writer(tmp_path):
	for index, (engine, mode, stage, reason) in enumerate((
		("codex", "prepare_failed", True, "sandbox_prepare_failed"),
		("claude", "prepare_second_failed", True, "sandbox_prepare_failed"),
		("codex", "success", False, "support_missing"),
		("codex", "config_failed", True, "opencode_config_failed"),
		("codex", "outdated", True, "sandbox_helper_outdated"),
	)):
		work = tmp_path / str(index)
		work.mkdir()
		proc, calls, host_writer = _run_rb_isolated_fallback(work, engine=engine, mode=mode, stage=stage)
		assert f"rc=77 reason={reason}" in proc.stdout, proc.stderr
		assert not host_writer.exists()
		if mode == "outdated":
			assert calls.count("run|codex") == 1
		else:
			assert not any(line.startswith("run|codex") for line in calls.splitlines())


def test_rb_opencode_transfer_failure_blocks_a_fix(tmp_path):
	proc, calls, host_writer = _run_rb_isolated_fallback(tmp_path, engine="codex", mode="transfer_failed", access="write")
	assert "rc=1 reason= flag=true" in proc.stdout, proc.stderr
	assert "run|codex|RB_JUDGE|write" in calls
	assert calls.splitlines()[-1] == "cleanup"
	assert not host_writer.exists()


def test_rb_fix_refuses_failed_claude_transfer_before_commit_or_merge():
	text = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	block = text.split('  fix)\n', 1)[1].split('  merge_with_followup)', 1)[0]
	gate = block.index('if [ "${rb_fix_rc}" -ne 0 ] || [ "${RB_SANDBOX_TRANSFER_FAILED:-false}" = "true" ]; then')
	for needle in ('git status --porcelain', 'git commit -m "[judge-fix]', 'Treating as merge.'):
		assert gate < block.index(needle), needle
	assert 'judge_skip_reason=fix_transfer_failed' in block
	assert 'judge_skip_reason=fix_failed' in block
	assert 'judge_skip_reason=isolation_unavailable' in block
	assert 'failed isolated fix' in block
	helper = _rb_helper()
	assert helper.index('RB_SANDBOX_TRANSFER_FAILED=true') < helper.index('rm -f -- "${RUNTIME_DIR}/review_sandbox_transfer_failed"', helper.index('RB_SANDBOX_TRANSFER_FAILED=true'))


def test_rb_judge_never_runs_opencode_on_host_and_defers_before_retry():
	text = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	assert "opencode_run_cmd" not in text
	assert 'local rb_oc_role=writer' in text
	assert '[ "${rb_oc_phase}" != review_rb_judge ] || rb_oc_role=reviewer' in text
	assert 'RB_JUDGE_SANDBOX_OPENCODE_CONFIG="${RB_JUDGE_OPENCODE_CONFIG}"' in text
	assert 'review_rb_prepare_opencode_config reviewer review_rb_judge "${RB_JUDGE_OPENCODE_CONFIG}" off' not in text
	verdict = text.split('for attempt_idx in "${!JUDGE_ATTEMPT_LEVELS[@]}"; do', 1)[1].split('if [ "${JUDGE_SUCCESS}" != "true" ]; then', 1)[0]
	assert verdict.index('RB_JUDGE_ISOLATION_DEFERRED=true') < verdict.index('break', verdict.index('RB_JUDGE_ISOLATION_DEFERRED=true')) < verdict.index('sleep 10')
	assert 'judge_skip_reason=isolation_unavailable' in verdict
	assert 'rb_fix_claude_rc}" -ne 75 ] &&' not in text
	step = AGENT_STEPS["Post review-blocked comment on PR (autofix exhaustion)"]["run"]
	assert 'isolation_unavailable)' in step
	assert 'AI review/autofix — judge deferred: isolation unavailable' in step


def test_sandbox_reports_progress_while_claude_streams():
	text = (REPO_ROOT / "scripts" / "review_untrusted_sandbox.sh").read_text(encoding="utf-8")
	assert 'echo "CLAUDE_ENGINE progress role=${claude_role} transcript_bytes=${size}" >&2' in text
	assert 'wait "${progress_sleep_pid}" || break' in text
	assert text.count('kill "${progress_pid}"') == 2
