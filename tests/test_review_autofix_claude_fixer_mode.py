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
		"pr": {"state": "open", "merged": False, "head": {"ref": head_ref, "sha": HEAD}, "labels": [], "additions": 400, "deletions": 50, "mergeable": True, "mergeable_state": "clean", "title": "Demo — phase 1/2: x", "body": "Refs #1", **(pr_overrides or {})},
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
		**{f"AI_ENGINE_{role}": "" for role in FIXER_ROLES + ("SUMMARISER", "BEHAVIOURAL_SMOKE")},
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
	state_check = AGENT_STEPS["Check PR state (defense-in-depth)"]["run"]
	assert 'echo "AI_ENGINE_LABELS=$(printf' in state_check
	assert '"${pr_meta}" | jq -ce' in state_check
	assert '|| printf \'["ai:codex"]\')" >> "$GITHUB_ENV"' in state_check
	assert step["env"]["CLAUDE_FIXER_ENABLED"] == "${{ vars.CLAUDE_FIXER_ENABLED || 'true' }}"
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
	for role in FIXER_ROLES:
		assert values[f"AI_ENGINE_RESOLVED_{role}"] == "codex", role
	# The utility roles (Phase 5d) are not Claude-fixer roles.
	assert values["AI_ENGINE_RESOLVED_SUMMARISER"] == "claude"
	values = _run_resolve_step(tmp_path, CLAUDE_FIXER_ENABLED="false", AI_ENGINE="codex")
	assert values["any_claude"] == "false"


def test_a_role_variable_still_moves_one_role_to_codex(tmp_path):
	values = _run_resolve_step(tmp_path, AI_ENGINE_CONFLICT_RESOLVER="codex")
	assert values["AI_ENGINE_RESOLVED_CONFLICT_RESOLVER"] == "codex"
	assert values["AI_ENGINE_RESOLVED_REVIEW_EDITOR"] == "claude"


def test_pr_codex_label_keeps_every_review_role_on_codex(tmp_path):
	values = _run_resolve_step(tmp_path, AI_ENGINE="claude", AI_ENGINE_LABELS='["ai:codex"]')
	assert values["any_claude"] == "false"
	for role in FIXER_ROLES + ("SUMMARISER", "BEHAVIOURAL_SMOKE"):
		assert values[f"AI_ENGINE_RESOLVED_{role}"] == "codex", role


def test_missing_engine_keeps_every_role_on_codex(tmp_path):
	values = _run_resolve_step(tmp_path, SUPPORT_SCRIPTS_DIR=str(tmp_path))
	assert values["any_claude"] == "false"
	assert {values[f"AI_ENGINE_RESOLVED_{role}"] for role in FIXER_ROLES} == {"codex"}


def test_sandbox_prepare_follows_the_editor_engine_with_an_opencode_fallback():
	run = AGENT_STEPS["Install project dependencies (best-effort)"]["run"]
	assert 'review_untrusted_sandbox.sh" prepare claude; then' in run
	assert 'echo "AI_ENGINE_RESOLVED_REVIEW_EDITOR=codex" >> "$GITHUB_ENV"' in run
	assert run.count('bash "${SUPPORT_SCRIPTS_DIR}/review_untrusted_sandbox.sh" prepare\n') == 2


def test_engine_files_ride_the_optional_bootstrap():
	text = (REPO_ROOT / "scripts" / "stage_workflow_support.sh").read_text(encoding="utf-8")
	line = next(l for l in text.splitlines() if l.startswith("OPTIONAL_BOOTSTRAP_SCRIPTS="))
	for name in ("ai_engine.sh", "claude_engine.py", "claude_anthropic_relay.py", "claude_settings.json.tmpl"):
		assert name in line.split("=", 1)[1].strip("\"").split(), name


# Each review script: the Claude branch, the 75 gate, and the unchanged
# OpenCode command after it (G4).
REVIEW_SITES = {
	"review_apply_fixes.sh": (
		'if [ "${AI_ENGINE_RESOLVED_REVIEW_EDITOR:-codex}" = "claude" ]; then',
		'[ "${editor_claude_rc}" -eq 75 ] || return "${editor_claude_rc}"',
		'      -- "${editor_opencode_cmd[@]}" < "${prompt_file}" 2>"${stderr_target}"',
	),
	"review_consolidate.sh": (
		'bash -c \'source "$1" && claude_run REVIEW_CONSOLIDATOR "$2" "$3" "$4"\'',
		'elif [ -x "${CODEX_HEARTBEAT_HELPER}" ]; then',
		'			-- "${consolidator_cmd[@]}" < "${CONSOLIDATOR_PROMPT_FILE}"; then',
	),
	"review_conflict_resolve.sh": (
		'bash -c \'source "$1" && claude_run CONFLICT_RESOLVER "$2" "$3" "$4"\'',
		'elif [ -x "${CODEX_STALL_GUARD_HELPER}" ]; then',
		'        -- "${resolver_opencode_cmd[@]}" < "${_effective_prompt_file}" \\',
	),
	"review_rb_judge.sh": (
		'review_rb_claude_run read "${RB_JUDGE_PROMPT}" "${RB_JUDGE_OUTPUT}" "${JUDGE_STDERR_FILE}" "${level}" || rb_judge_claude_rc=$?',
		'elif [ -x "${CODEX_STALL_GUARD_HELPER}" ]; then',
		'      -- "${judge_codex_cmd[@]}" < "${RB_JUDGE_PROMPT}" || rc=$?',
	),
}


def test_each_review_role_tries_claude_then_the_unchanged_opencode_command():
	for script, (claude_call, gate, opencode_call) in REVIEW_SITES.items():
		text = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
		assert text.count(claude_call) == 1, script
		start = text.index(claude_call)
		gate_at = text.index(gate, start)
		assert text.index(opencode_call, gate_at) > gate_at, script
	rb = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	assert 'review_rb_claude_run write "${RB_FIX_PROMPT}" "${RB_FIX_OUTPUT}" "${RB_FIX_STDERR}" "${JUDGE_EFFECTIVE_REASONING_EFFORT}" || rb_fix_claude_rc=$?' in rb


FAKE_ENGINE = r"""
claude_run() {
  printf '%s|%s|%s|%s|%s|%s\n' "$1" "$(basename "$2")" "$(basename "$3")" "$4" "${AI_ENGINE_READ_ONLY:-}" "${AI_ENGINE_EFFORT_HINT:-}" >> "${CALLS}"
  case "${MODE}" in
    success) printf 'verdict\n' > "$3"; return 0 ;;
    unavailable) echo "AI_ENGINE_FALLBACK role=$1 reason=no_credential" >&2; return 75 ;;
    *) return 1 ;;
  esac
}
"""


def _rb_helper() -> str:
	text = (REPO_ROOT / "scripts" / "review_rb_judge.sh").read_text(encoding="utf-8")
	match = re.search(r"^review_rb_claude_run\(\)\n\{\n.*?^\}\n", text, re.M | re.S)
	assert match
	return match.group(0)


def _run_rb_helper(tmp: Path, *, engine: str, mode: str, access: str = "read", stage: bool = True):
	scripts = tmp / "scripts"
	scripts.mkdir()
	if stage:
		(scripts / "ai_engine.sh").write_text(FAKE_ENGINE, encoding="utf-8")
	(tmp / "prompt.txt").write_text("judge\n", encoding="utf-8")
	calls = tmp / "calls"
	script = _rb_helper() + f'rc=0; review_rb_claude_run {access} prompt.txt out.txt err.txt high || rc=$?; echo "rc=$rc"\n'
	env = dict(os.environ, SUPPORT_SCRIPTS_DIR=str(scripts), RB_OPENCODE_WORKSPACE=str(tmp), MODEL_EDITOR="openai/gpt-6-sol",
		AI_ENGINE_RESOLVED_RB_JUDGE=engine, MODE=mode, CALLS=str(calls))
	proc = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True)
	return proc, (calls.read_text(encoding="utf-8") if calls.exists() else "")


def test_rb_verdict_pass_runs_claude_read_only(tmp_path):
	proc, calls = _run_rb_helper(tmp_path, engine="claude", mode="success")
	assert "rc=0" in proc.stdout, proc.stderr
	assert calls.splitlines() == [f"RB_JUDGE|prompt.txt|out.txt|{tmp_path}|true|high"]
	assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "verdict\n"


def test_rb_fix_pass_keeps_the_write_profile(tmp_path):
	_proc, calls = _run_rb_helper(tmp_path, engine="claude", mode="success", access="write")
	assert calls.split("|")[4] == "false"


def test_rb_helper_returns_75_off_claude_unavailable_or_unstaged(tmp_path):
	for index, (engine, mode, stage) in enumerate((("codex", "success", True), ("claude", "unavailable", True), ("claude", "success", False))):
		work = tmp_path / str(index)
		work.mkdir()
		proc, _calls = _run_rb_helper(work, engine=engine, mode=mode, stage=stage)
		assert "rc=75" in proc.stdout, (engine, mode, stage, proc.stderr)
	crash = tmp_path / "crash"
	crash.mkdir()
	proc, _calls = _run_rb_helper(crash, engine="claude", mode="crash")
	assert "rc=1" in proc.stdout


def test_sandbox_reports_progress_while_claude_streams():
	text = (REPO_ROOT / "scripts" / "review_untrusted_sandbox.sh").read_text(encoding="utf-8")
	assert 'echo "CLAUDE_ENGINE progress role=REVIEW_EDITOR transcript_bytes=${size}" >&2' in text
	assert 'while sleep "${REVIEW_SANDBOX_PROGRESS_SECS:-60}" </dev/null >/dev/null 2>&1; do' in text
	assert text.count('kill "${progress_pid}"') == 2
