#!/usr/bin/env python3
"""Contract for review_autofix.yml Claude-fixer mode.

Every PR-backed `claude/*` head (/implement-plan-claude stages and any
session's PR under CLAUDE.md §26) keeps the reviewer panel, but the GPT editor
never runs, the conflict resolver and push / re-trigger tail run only for a
pre-review conflict that touches `.claude/**`, and the review-blocked judge
runs only in its Claude mode: the findings (or any other pre-review
conflict) are handed to the Claude session that owns the PR, and a
`claude_fixer_converged_head` dispatch re-reviews a bot-authenticated ledger
verdict. Only the fresh clean review can auto-merge, and the
MAX_AUTOFIX_ITERATIONS cap still applies, counting `[claude-autofix]` rounds.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import (  # noqa: E402
	REPO_ROOT,
	REVIEW_AUTOFIX_WORKFLOW_PATH,
	expanded_review_autofix_text,
)

HANDOFF_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_claude_fixer_handoff.sh"
TOPOLOGY_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_merge_topology_gate.sh"
WRAPPERS = (REPO_ROOT / "workflow-templates" / "ai-review.yml",)
INTERNAL_REVIEW = REPO_ROOT / ".github" / "workflows" / "internal-review.yml"
HEAD = "c" * 40
AUTHOR = "workflow-bot"
FIXER_BOT = "dedicated-fixer[bot]"
DIGEST = "a" * 64

EDITOR_TAIL_STEPS = (
	"Pre-editor stale-base gate",
	"Install project dependencies (best-effort)",
	"Switch reasoning effort for editor",
	"Setup Serena for editor",
	"Apply fixes with editor model",
	"Decide partial-finalize validation/push safety",
	"Commit changes",
	"Run interim judge",
	"Synthesize behavioural smoke",
	"Post editor summary comment",
	"Clear Serena after editor",
	"Detect editor-claimed-but-uncommitted changes",
	"Validate editor no-op disposition",
	"Detect merge conflicts",
	"Prepare merge-conflict resolver prompt and pre-snapshot",
	"Run Codex resolver, validate, stage, commit",
	"Telegram conflict resolution message",
	"Push all pending commits",
	"Resolve addressed PR review threads",
	"Re-trigger review via workflow_dispatch",
	"Post partial finalize comment and persist runtime marker",
	"Stage review-issue ledger for partial finalize cache save",
	"Save review-issue ledger after partial finalize",
	"Telegram success",
	"Re-dispatch review on editor-changes-lost",
	"Telegram editor-changes-lost warning",
	"Telegram editor-noop-suspicious warning",
	# Q20: the review-blocked judge would have GPT edit the PR. Its Claude
	# mode (claude_fixer_judge_head) is the one exception, pinned in
	# test_judge_step_runs_only_on_an_accepted_judge_dispatch.
	"Detect review-blocked break-glass override",
	"Review-blocked judge decision",
	"Telegram review-blocked judge decision",
)


def _load(path: Path) -> dict:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	if True in workflow:
		workflow["on"] = workflow.pop(True)
	return workflow


def _steps(workflow: dict, job: str) -> dict[str, dict]:
	return {step["name"]: step for step in workflow["jobs"][job]["steps"] if "name" in step}


WORKFLOW = _load(REVIEW_AUTOFIX_WORKFLOW_PATH)
AGENT_STEPS = _steps(WORKFLOW, "codex-agent")


def test_converged_head_input_on_every_entry_point():
	for trigger in ("workflow_call", "workflow_dispatch"):
		spec = WORKFLOW["on"][trigger]["inputs"]["claude_fixer_converged_head"]
		assert spec["default"] == "" and spec["type"] == "string" and spec["required"] is False
	for wrapper in WRAPPERS:
		workflow = _load(wrapper)
		assert workflow["on"]["workflow_dispatch"]["inputs"]["claude_fixer_converged_head"]["default"] == ""
		assert workflow["jobs"]["review"]["with"]["claude_fixer_converged_head"] == (
			"${{ github.event_name == 'workflow_dispatch' && github.event.inputs.claude_fixer_converged_head || '' }}"
		)


def test_internal_review_does_not_forward_the_converged_input():
	"""internal-review.yml calls review_autofix.yml@main, so on the PR that adds
	an input to review_autofix.yml, forwarding it from internal-review.yml makes
	every run a zero-job startup_failure (run 36095647423: `input
	"claude_fixer_converged_head" is not defined`). This library dispatches
	review_autofix.yml directly for the convergence run instead."""
	workflow = _load(INTERNAL_REVIEW)
	assert "claude_fixer_converged_head" not in workflow["on"]["workflow_dispatch"]["inputs"]
	for job in workflow["jobs"].values():
		assert "claude_fixer_converged_head" not in (job.get("with") or {})


def test_gate_exports_fixer_outputs_and_mode():
	outputs = WORKFLOW["jobs"]["gate"]["outputs"]
	assert outputs["claude_fixer"] == "${{ steps.evaluate.outputs.claude_fixer }}"
	assert outputs["claude_fixer_converged"] == "${{ steps.evaluate.outputs.claude_fixer_converged }}"
	assert outputs["claude_fixer_verify"] == "${{ steps.evaluate.outputs.claude_fixer_verify }}"
	gate_run = _steps(WORKFLOW, "gate")["Evaluate review gate"]["run"]
	assert "claude/*) CLAUDE_FIXER=\"true\" ;;" in gate_run
	assert "claude/implement-plan-*) CLAUDE_FIXER" not in gate_run
	assert 'SKIP_REASON="claude_fixer_awaiting_session"' in gate_run
	assert 'CLAUDE_FIXER_VERIFY="true"' in gate_run
	assert "${{" not in gate_run.split("# ----- Claude-fixer mode", 1)[1].split("# ----- Terminal same-head skip", 1)[0]
	gate_env = _steps(WORKFLOW, "gate")["Evaluate review gate"]["env"]
	assert gate_env["CLAUDE_FIXER_ENABLED"] == "${{ vars.CLAUDE_FIXER_ENABLED || 'true' }}"
	assert gate_env["CLAUDE_FIXER_VERDICT_BOT_LOGIN"] == "${{ vars.CLAUDE_FIXER_VERDICT_BOT_LOGIN || '' }}"
	assert WORKFLOW["jobs"]["codex-agent"]["env"]["CLAUDE_FIXER_MODE"] == "${{ needs.gate.outputs.claude_fixer }}"
	assert WORKFLOW["jobs"]["codex-agent"]["env"]["CLAUDE_FIXER_VERIFICATION"] == "${{ needs.gate.outputs.claude_fixer_verify }}"


def test_editor_tail_and_judge_are_skipped_in_fixer_mode():
	for name in EDITOR_TAIL_STEPS:
		assert "env.CLAUDE_FIXER_MODE != 'true'" in AGENT_STEPS[name]["if"], name


def test_zero_findings_rounds_still_auto_merge():
	for name in ("Enable auto-merge on PR", "Mark linked issues ready to merge"):
		assert "(env.CLAUDE_FIXER_MODE != 'true' || env.CLAUDE_FIXER_ZERO_FINDINGS == 'true')" in AGENT_STEPS[name]["if"], name


def test_handoff_step_runs_after_reviewers_and_before_the_editor():
	names = list(AGENT_STEPS)
	handoff = "Hand review round to Claude session (Claude-fixer mode)"
	assert names.index("Run reviewer models") < names.index(handoff) < names.index("Apply fixes with editor model")
	step = AGENT_STEPS[handoff]
	assert "env.CLAUDE_FIXER_MODE == 'true'" in step["if"]
	assert "max_iterations_reached != 'true'" in step["if"]
	# Runs for a pre-review conflict too (reviewers are skipped then).
	assert "AUTOFIX_PRE_REVIEW_RESOLVE" not in step["if"]
	assert step["env"]["CLAUDE_FIXER_ROUND_INDEX"] == "${{ steps.retrigger_guard.outputs.autofix_iteration }}"


def test_cap_labels_the_pr_itself_in_fixer_mode():
	step = AGENT_STEPS["Label Claude-fixer PR review-blocked (autofix exhaustion)"]
	assert "max_iterations_reached == 'true'" in step["if"] and "env.CLAUDE_FIXER_MODE == 'true'" in step["if"]
	assert 'issues/${PR_NUMBER}/labels" -f "labels[]=ai:review-blocked"' in step["run"]


def test_iteration_counter_counts_claude_autofix_rounds():
	run = AGENT_STEPS["Count autofix iterations"]["run"]
	pattern = re.search(r"grep -Eq '(\^\\\[\(ai\|claude\)-autofix\\\])'", run)
	assert pattern, run
	regex = re.compile(r"^\[(ai|claude)-autofix\]")
	assert regex.match("[claude-autofix] fix review round 2")
	assert regex.match("[ai-autofix] apply PR fixes")
	for subject in ("[claude-intervention] unblock", "[claude-merge-resolve] merge main", "[judge-fix] x"):
		assert not regex.match(subject)


def test_converged_dispatch_cannot_merge_without_fresh_review():
	assert WORKFLOW["jobs"]["claude-fixer-auto-merge"]["if"] == "${{ needs.gate.outputs.claude_fixer_verify == 'legacy_disabled' }}"
	assert "CLAUDE_FIXER_VERIFICATION_FAILED == 'true'" in AGENT_STEPS["Label Claude-fixer PR review-blocked (autofix exhaustion)"]["if"]
	assert "CLAUDE_FIXER_ZERO_FINDINGS == 'true'" in AGENT_STEPS["Enable auto-merge on PR"]["if"]


def test_topology_gate_hands_conflicts_to_claude_regardless_of_resolver_toggle():
	text = TOPOLOGY_SCRIPT.read_text(encoding="utf-8")
	block = text.split('if [ "${CLAUDE_FIXER_MODE:-false}" = "true" ]; then', 1)[1].split("fi\n", 1)[0]
	assert 'echo "AUTOFIX_PRE_REVIEW_RESOLVE=true" >> "$GITHUB_ENV"' in block
	# The hand-off itself never depends on this run's push permission; only
	# the D14 protected-conflict route asks whether the resolver could push.
	handoff_part = block.split("# Plan D14", 1)[0]
	assert "CAN_PUSH" not in handoff_part
	assert "action=claude_fixer_handoff" in block


PROTECTED_CONFLICT_STEPS = (
	"Detect merge conflicts",
	"Prepare merge-conflict resolver prompt and pre-snapshot",
	"Run Codex resolver, validate, stage, commit",
	"Telegram conflict resolution message",
	"Push all pending commits",
	"Re-trigger review via workflow_dispatch",
)
PROTECTED_CONFLICT_CLAUSE = "(env.CLAUDE_FIXER_MODE != 'true' || env.CLAUDE_FIXER_PROTECTED_CONFLICT == 'true')"


def _protected_route(unmerged: str, enabled: str | None = None, can_push: str = "true") -> tuple[str, str]:
	"""Run the D14 route of the topology gate's Claude-fixer branch."""
	text = TOPOLOGY_SCRIPT.read_text(encoding="utf-8")
	snippet = text.split("    # Plan D14", 1)[1].split("    unset _protected_conflict_paths\n", 1)[0]
	snippet = "    # Plan D14" + snippet + "    unset _protected_conflict_paths\n"
	with tempfile.TemporaryDirectory() as td:
		github_env = Path(td) / "github_env"
		github_env.write_text("", encoding="utf-8")
		env = {**os.environ, "GITHUB_ENV": str(github_env), "PR_NUMBER": "42", "LOCAL_HEAD_SHA": HEAD, "CAN_PUSH": can_push,
			"_pre_review_unmerged": unmerged}
		env.pop("CLAUDE_FIXER_PROTECTED_CONFLICT_RESOLVER_ENABLED", None)
		if enabled is not None:
			env["CLAUDE_FIXER_PROTECTED_CONFLICT_RESOLVER_ENABLED"] = enabled
		proc = subprocess.run(["bash", "-c", "set -euo pipefail\n_pre_review_unmerged=\"${_pre_review_unmerged}\"\n" + snippet],
			env=env, capture_output=True, text=True)
		assert proc.returncode == 0, proc.stderr
		return proc.stdout, github_env.read_text(encoding="utf-8")


def test_protected_conflict_routes_to_the_resolver():
	out, github_env = _protected_route("src/a.py,.claude/settings.json,.claude/commands/x.md")
	assert "CLAUDE_FIXER_PROTECTED_CONFLICT=true" in github_env
	assert "CLAUDE_FIXER_PROTECTED_CONFLICT_PATHS=.claude/settings.json,.claude/commands/x.md" in github_env
	assert f"AUTOFIX_GATE_CLAUDE_FIXER_PROTECTED_CONFLICT pr=42 head={HEAD} paths=2 resolver=ran" in out


@pytest.mark.parametrize("unmerged", ["src/a.py,README.md", "", "workflow-templates/.claude/settings.json"])
def test_unprotected_conflict_keeps_the_claude_handoff(unmerged):
	out, github_env = _protected_route(unmerged)
	assert github_env == "" and "PROTECTED_CONFLICT" not in out


@pytest.mark.parametrize(("enabled", "can_push", "resolver"), [("false", "true", "skipped_switch_off"), ("FALSE", "true", "skipped_switch_off"), ("true", "false", "skipped_cannot_push")])
def test_protected_conflict_switch_off_or_no_push_keeps_the_handoff(enabled, can_push, resolver):
	out, github_env = _protected_route(".claude/settings.json", enabled=enabled, can_push=can_push)
	assert github_env == ""
	assert f"paths=1 resolver={resolver}" in out


def test_resolver_chain_runs_for_a_protected_conflict_only():
	for name in PROTECTED_CONFLICT_STEPS:
		assert AGENT_STEPS[name]["if"].endswith(PROTECTED_CONFLICT_CLAUSE), name
	for name in set(EDITOR_TAIL_STEPS) - set(PROTECTED_CONFLICT_STEPS):
		assert "CLAUDE_FIXER_PROTECTED_CONFLICT" not in AGENT_STEPS[name]["if"], name
	for name in ("Enable auto-merge on PR", "Mark linked issues ready to merge", "Hand review round to Claude session (Claude-fixer mode)"):
		assert "CLAUDE_FIXER_PROTECTED_CONFLICT" not in AGENT_STEPS[name]["if"], name
	gate_env = AGENT_STEPS["Pre-review deterministic merge-topology gate"]["env"]
	assert gate_env["CLAUDE_FIXER_PROTECTED_CONFLICT_RESOLVER_ENABLED"] == "${{ vars.CLAUDE_FIXER_PROTECTED_CONFLICT_RESOLVER_ENABLED || 'true' }}"


def test_unresolved_protected_conflict_fallback_step_follows_the_resolver():
	names = list(AGENT_STEPS)
	fallback = "Hand unresolved protected conflict to Claude session"
	assert names.index("Run Codex resolver, validate, stage, commit") < names.index(fallback) < names.index("Enable auto-merge on PR")
	assert AGENT_STEPS[fallback]["if"] == "always() && env.CLAUDE_FIXER_PROTECTED_CONFLICT == 'true' && env.CONFLICT_RESOLVED != 'true' && env.PR_CLOSED != 'true'"
	# Fails open like the other always() Claude-fixer steps (the evidence uploads).
	assert AGENT_STEPS[fallback]["continue-on-error"] is True
	# PR_NUMBER comes from the codex-agent job env, as for the hand-off step.
	assert "PR_NUMBER" in WORKFLOW["jobs"]["codex-agent"]["env"]


def test_handoff_skips_a_protected_conflict_until_the_resolver_fails():
	extra = {"CLAUDE_FIXER_PROTECTED_CONFLICT": "true", "CLAUDE_FIXER_PROTECTED_CONFLICT_PATHS": ".claude/settings.json"}
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, _env = _run_handoff(Path(td), ledger=None, pre_review_resolve=True, unmerged=".claude/settings.json", extra_env=extra)
	assert proc.returncode == 0, proc.stderr
	assert calls == [] and "reason=protected_conflict_to_resolver" in proc.stdout
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, _env = _run_handoff(Path(td), ledger=None, pre_review_resolve=True, unmerged="src/a.py,.claude/settings.json",
			extra_env={**extra, "CLAUDE_FIXER_PROTECTED_CONFLICT_FALLBACK": "true"})
	assert proc.returncode == 0, proc.stderr
	body = [call for call in calls if call["payload"]][0]["payload"]["body"]
	assert f"<!-- ai:claude-fixer-handoff:v1 kind=conflict head={HEAD} round=2 -->" in body
	assert "Protected paths: `.claude/settings.json`." in body and "watched session" in body


def test_plain_conflict_handoff_names_no_protected_paths():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, _env = _run_handoff(Path(td), ledger=None, pre_review_resolve=True, unmerged="src/a.py")
	assert proc.returncode == 0, proc.stderr
	body = [call for call in calls if call["payload"]][0]["payload"]["body"]
	assert "kind=conflict" in body and "Protected paths" not in body


def test_fallback_step_script_posts_the_protected_handoff():
	script = REPO_ROOT / "scripts" / "review_autofix_step_claude_fixer_protected_conflict.sh"
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		support = tmp / "support"
		support.mkdir()
		(support / "review_autofix_step_claude_fixer_handoff.sh").write_text(
			'echo "fallback=${CLAUDE_FIXER_PROTECTED_CONFLICT_FALLBACK}"\n', encoding="utf-8")
		proc = subprocess.run(["bash", "-c", f'source "{script}"'], env={**os.environ, "SUPPORT_SCRIPTS_DIR": str(support),
			"PR_NUMBER": "42", "HEAD_SHA": HEAD, "CLAUDE_FIXER_PROTECTED_CONFLICT_PATHS": ".claude/x"}, capture_output=True, text=True)
	assert proc.returncode == 0, proc.stderr
	assert "fallback=true" in proc.stdout and "resolver=unresolved" in proc.stdout


# ---- gate jq predicates, executed against sample comment payloads ----

def _gate_jq(label: str) -> str:
	gate_run = _steps(WORKFLOW, "gate")["Evaluate review gate"]["run"]
	block = gate_run.split("# ----- Claude-fixer mode", 1)[1].split("# ----- Terminal same-head skip", 1)[0]
	pattern = r"jq -e --arg head \"\$\{pr_head_sha_gate\}\" --arg author \"\$\{gate_marker_author_login\}\"(?: --arg bot \"\$\{CLAUDE_FIXER_VERDICT_BOT_LOGIN\}\")? '(.*?)'"
	programs = re.findall(pattern, block, re.S)
	assert len(programs) == 2, programs
	if label == "converged":
		return programs[1]
	# The hand-off predicate lives in gate_claude_handoff_on_head(), shared by
	# the awaiting-session skip and the deterministic-skip hand-off guard.
	helper = gate_run.split("gate_claude_handoff_on_head()", 1)[1].split("\n}\n", 1)[0]
	helper_programs = re.findall(pattern, helper, re.S)
	assert len(helper_programs) == 1, helper_programs
	assert block.count("gate_claude_handoff_on_head") == 1
	return helper_programs[0]


def _jq_true(program: str, comments: list[dict]) -> bool:
	with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
		json.dump([{"id": index, **comment} for index, comment in enumerate(comments, 1)], handle)
	try:
		proc = subprocess.run(["jq", "-e", "--arg", "head", HEAD, "--arg", "author", AUTHOR, "--arg", "bot", FIXER_BOT, program, handle.name], capture_output=True, text=True)
	finally:
		os.unlink(handle.name)
	return proc.returncode == 0


def _c(body: str, author: str = AUTHOR, association: str = "OWNER") -> dict:
	# The workflow's hand-off step owns the comment header as well as its
	# marker; a marker echoed inside another GH_PAT-authored comment is not it.
	match = re.match(r"<!-- ai:claude-fixer-handoff:v1 kind=(findings|conflict) head=[0-9a-f]{40} round=([0-9]+) -->", body)
	if match:
		kind = "findings handed" if match.group(1) == "findings" else "merge conflict, handed"
		body = f"## Review round {match.group(2)}: {kind} to the Claude session\n" + body
	return {"body": body, "author_login": author, "author_type": "Bot" if author.endswith("[bot]") else "User", "author_association": association}


HANDOFF = f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=1 -->"
LEDGER_HANDOFF = f"<!-- ai:claude-fixer-handoff:v2 head={HEAD} round=1 ledger={DIGEST} -->"
CONFLICT = f"<!-- ai:claude-fixer-handoff:v1 kind=conflict head={HEAD} round=1 -->"
VERDICT = f"<!-- ai:claude-fixer-verdict:v1 head={HEAD} -->"
LEDGER_VERDICT = f"<!-- ai:claude-fixer-verdict:v2 head={HEAD} round=1 ledger={DIGEST} -->"


def test_converged_requires_workflow_handoff_and_trusted_verdict():
	program = _gate_jq("converged")
	assert _jq_true(program, [_c(HANDOFF + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT)])
	# Verdict from an untrusted author does not count.
	assert not _jq_true(program, [_c(HANDOFF + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT, author="someone", association="COLLABORATOR")])
	# A hand-off posted by anyone but the workflow identity does not count.
	assert not _jq_true(program, [_c(HANDOFF + "\n" + LEDGER_HANDOFF, author="someone"), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT)])
	# A conflict hand-off is not a reviewed head.
	assert not _jq_true(program, [_c(CONFLICT + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT)])
	# Markers for another head do not count.
	assert not _jq_true(program, [_c(HANDOFF.replace(HEAD, "d" * 40) + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT)])
	# The workflow's handoff instruction used to embed a literal verdict marker.
	assert not _jq_true(program, [_c(HANDOFF + "\n" + LEDGER_HANDOFF + "\nReply with `" + VERDICT + "`.")])
	# A force-review of the same head creates a new handoff requiring a new reply.
	assert not _jq_true(program, [_c(HANDOFF + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT), _c(HANDOFF.replace("round=1", "round=2"))])
	assert not _jq_true(program, [_c(HANDOFF + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT), _c(CONFLICT)])
	assert not _jq_true(program, [_c(HANDOFF + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT.replace(DIGEST, "b" * 64), author=FIXER_BOT)])
	assert not _jq_true(program, [_c(HANDOFF + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT.replace("round=1", "round=2"), author=FIXER_BOT)])
	assert not _jq_true(program, [_c("Other GH_PAT comment\n" + HANDOFF + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT)])


def test_dispatch_rerun_is_skipped_once_a_handoff_names_the_head():
	program = _gate_jq("awaiting")
	assert _jq_true(program, [_c(HANDOFF)])
	assert _jq_true(program, [_c(CONFLICT)])
	assert not _jq_true(program, [_c(HANDOFF.replace(HEAD, "d" * 40))])
	assert not _jq_true(program, [_c(HANDOFF, author="someone")])


def test_marker_comment_fetch_keeps_fixer_markers_and_association():
	gate_run = _steps(WORKFLOW, "gate")["Evaluate review gate"]["run"]
	assert '($b | contains("<!-- ai:claude-fixer-"))' in gate_run
	assert 'author_association: (.author_association // "")' in gate_run
	assert 'author_type: (.user.type // "")' in gate_run


# ---- hand-off script, executed with a stubbed gh ----

LEDGER_WITH_FINDINGS = """=== CONSENSUS FINDINGS ===
- scripts/a.sh:10-12 | severity=high | confidence=4
  flagged_by: [minimax, glm]
  PROBLEM: unquoted expansion
  WHY: word splitting
=== END CONSENSUS FINDINGS ===

=== CONSENSUS TASK GAPS ===
(No task gaps reported.)
=== END CONSENSUS TASK GAPS ===

=== FINDINGS FROM minimax ===
- scripts/a.sh:10 | severity=high
  PROBLEM: unquoted expansion
=== END FINDINGS FROM minimax ===
"""

LEDGER_EMPTY = """=== CONSENSUS FINDINGS ===
(No findings reported.)
=== END CONSENSUS FINDINGS ===

=== CONSENSUS TASK GAPS ===
(No task gaps reported.)
=== END CONSENSUS TASK GAPS ===

=== FINDINGS FROM minimax ===
(No findings reported.)
=== END FINDINGS FROM minimax ===
"""


EVIDENCE_HELPER = REPO_ROOT / "scripts" / "review_claude_fixer_evidence.py"

# Records every call; answers `api user` with MOCK_GH_LOGIN (empty by
# default, so the checks-pending comment is always a fresh POST) and a
# comments GET with MOCK_GH_COMMENTS run through the caller's --jq.
RECORDING_GH = """#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
payload = None
if '--input' in args:
	payload = json.load(open(args[args.index('--input') + 1]))
open(os.environ['MOCK_GH_CALLS'], 'a').write(json.dumps({'args': args, 'payload': payload}) + '\\n')
jq = args[args.index('--jq') + 1] if '--jq' in args else None
def emit(value):
	text = json.dumps(value)
	if jq is not None:
		text = subprocess.run(['jq', '-rc', jq], input=text, capture_output=True, text=True, check=True).stdout
	sys.stdout.write(text)
if 'user' in args and os.environ.get('MOCK_GH_LOGIN'):
	emit({'login': os.environ['MOCK_GH_LOGIN']})
elif any(a.endswith('/comments') for a in args) and 'GET' in args:
	emit(json.loads(os.environ.get('MOCK_GH_COMMENTS') or '[]'))
"""


def _run_handoff(tmp: Path, *, ledger: str | None, check_context: str = "", pre_review_resolve: bool = False, unmerged: str = "", fresh_status: str = "ready", verification: bool = False, fresh_failed: str = "", fresh_head: str = HEAD, extra_env: dict | None = None, evidence_helper: bool = True, panel_statuses: list[str] | None = None, reviewers_successful: str = "2", active_models: int | None = None, cwd: str | None = None):
	support = tmp / "support"
	support.mkdir()
	calls = tmp / "calls.jsonl"
	(support / "post_review_comment.sh").write_text(
		f"#!/usr/bin/env bash\necho post_review_comment \"$REVIEWER_CONSENSUS_FILE\" \"$PR_NUMBER\" >> {tmp / 'posts.log'}\n",
		encoding="utf-8",
	)
	failed_names = [name for name in fresh_failed.split(",") if name]
	fresh_context = (
		f"PR_CHECK_RUNS_CONTEXT\nhead_sha: {fresh_head}\ncollection_status: {fresh_status}\ntotal_check_runs: 1\n"
		f"failed_count: {len(failed_names)}\nincomplete_count: 0\n"
		+ "".join(f"failed[{i}].name: {name}\n" for i, name in enumerate(failed_names))
	)
	(support / "collect_pr_check_runs_context.py").write_text(
		"import os\nfrom pathlib import Path\n"
		f"Path(os.environ['PR_CHECK_RUNS_CONTEXT_FILE']).write_text({fresh_context!r})\n",
		encoding="utf-8",
	)
	if evidence_helper:
		(support / "review_claude_fixer_evidence.py").write_text(EVIDENCE_HELPER.read_text(encoding="utf-8"), encoding="utf-8")
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(RECORDING_GH, encoding="utf-8")
	gh.chmod(0o755)
	ledger_path = tmp / "reviewer_consensus.txt"
	if ledger is not None:
		ledger_path.write_text(ledger, encoding="utf-8")
	checks_path = tmp / "checks.txt"
	checks_path.write_text(check_context, encoding="utf-8")
	github_env = tmp / "github_env"
	github_env.write_text("", encoding="utf-8")
	panel_env: dict[str, str] = {}
	if panel_statuses is not None:
		reviews_dir = tmp / "previous_reviews"
		reviews_dir.mkdir()
		for index, status in enumerate(panel_statuses):
			(reviews_dir / f"status_review_slot{index}.txt").write_text(status + "\n", encoding="utf-8")
		panel_env["PREVIOUS_REVIEWS_DIR"] = str(reviews_dir)
	if active_models is not None:
		(tmp / "reviewer_active_models.txt").write_text("".join(f"vendor/model{index}\n" for index in range(active_models)), encoding="utf-8")
	env = {
		**os.environ,
		**panel_env,
		"PATH": os.pathsep.join((str(bin_dir), os.environ.get("PATH", ""))),
		"PR_NUMBER": "42",
		"GH_TOKEN": "t",
		"GITHUB_REPOSITORY": "o/r",
		"HEAD_SHA": HEAD,
		"HEAD_REF": "claude/implement-plan-demo-phase-1",
		"CLAUDE_FIXER_ROUND_INDEX": "1",
		"AUTOFIX_PRE_REVIEW_RESOLVE": "true" if pre_review_resolve else "false",
		"AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED": unmerged,
		"REVIEWER_CONSENSUS_FILE": str(ledger_path),
		"PR_CHECK_RUNS_CONTEXT_FILE": str(checks_path),
		"SUPPORT_SCRIPTS_DIR": str(support),
		"GITHUB_RUN_ID": "99",
		"RUNTIME_DIR": str(tmp),
		"GITHUB_ENV": str(github_env),
		"CLAUDE_FIXER_VERIFICATION": "true" if verification else "false",
		"REVIEWERS_SUCCESSFUL": reviewers_successful,
		"MOCK_GH_CALLS": str(calls),
		"MOCK_GH_LOGIN": "",
	}
	env.update(extra_env or {})
	proc = subprocess.run(["bash", "-c", f'source "{HANDOFF_SCRIPT}"'], env=env, capture_output=True, text=True, cwd=cwd)
	gh_calls = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
	posts = (tmp / "posts.log").read_text() if (tmp / "posts.log").exists() else ""
	return proc, gh_calls, posts, github_env.read_text()


def test_handoff_posts_findings_ledger_then_marker():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, posts, github_env = _run_handoff(Path(td), ledger=LEDGER_WITH_FINDINGS)
	assert proc.returncode == 0, proc.stderr
	assert "post_review_comment" in posts
	assert len(calls) == 1
	body = calls[0]["payload"]["body"]
	assert calls[0]["args"][:4] == ["api", "-X", "POST", "repos/o/r/issues/42/comments"]
	assert f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=2 -->" in body
	assert f"<!-- ai:claude-fixer-handoff:v2 head={HEAD} round=2 ledger={hashlib.sha256(LEDGER_WITH_FINDINGS.encode()).hexdigest()} -->" in body
	assert "Reviewer ledger entries: 2" in body
	assert "ai:claude-fixer-verdict:v1" in body
	assert f"<!-- ai:claude-fixer-verdict:v1 head={HEAD} -->" not in body
	assert f"claude_fixer_converged_head={HEAD}" in body
	assert "[claude-autofix]" in body
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert "kind=findings findings=2" in proc.stdout


def test_handoff_zero_findings_exports_auto_merge_flag_without_comments():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY)
	assert proc.returncode == 0, proc.stderr
	assert calls == [] and posts == ""
	assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env


PANEL_FLOOR_CASES = (
	# (statuses, successful, expected floor_met)
	(["success"] * 6, "6", "true"),
	(["success"] * 5 + ["failed"], "5", "true"),
	(["success"] * 3 + ["failed", "skipped_budget", "skipped_unmapped"], "3", "true"),
	(["success"] * 2 + ["failed"] * 4, "2", "false"),
	(["success"] * 3 + ["failed"] * 2, "3", "true"),
	(["success"] * 2 + ["failed"] * 3, "2", "false"),
)


def test_handoff_panel_floor_clean_rounds_still_merge():
	for statuses, successful, floor_met in PANEL_FLOOR_CASES[:3] + PANEL_FLOOR_CASES[4:5]:
		with tempfile.TemporaryDirectory() as td:
			proc, calls, posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, panel_statuses=statuses, reviewers_successful=successful)
		assert proc.returncode == 0, proc.stderr
		assert f"CLAUDE_FIXER_PANEL_FLOOR pr=42 head={HEAD} round=2 successful={successful} active={len(statuses)} floor_met={floor_met}" in proc.stdout
		assert calls == [] and posts == "", statuses
		assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env, statuses
		assert "action=auto_merge" in proc.stdout


def test_handoff_below_panel_floor_is_not_clean():
	for statuses, successful, floor_met in (PANEL_FLOOR_CASES[3], PANEL_FLOOR_CASES[5]):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, panel_statuses=statuses, reviewers_successful=successful)
		assert proc.returncode == 0, proc.stderr
		assert f"successful={successful} active={len(statuses)} floor_met={floor_met}" in proc.stdout
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env, statuses
		assert len(calls) == 1 and "kind=findings" in calls[0]["payload"]["body"]
		body = calls[0]["payload"]["body"]
		assert f"Only {successful} of {len(statuses)} reviewers returned a result, below the panel floor" in body
		assert "When the ledger has no finding there is nothing for the GPT judge to rule on" in body


def test_handoff_above_panel_floor_has_no_floor_note():
	with tempfile.TemporaryDirectory() as td:
		_proc, calls, _posts, _env = _run_handoff(Path(td), ledger=LEDGER_WITH_FINDINGS, panel_statuses=["success"] * 5 + ["failed"], reviewers_successful="5")
	assert "below the panel floor" not in calls[0]["payload"]["body"]


def test_handoff_real_finding_blocks_even_when_panel_floor_is_met():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, posts, github_env = _run_handoff(
			Path(td), ledger=LEDGER_WITH_FINDINGS, panel_statuses=["success"] * 5 + ["failed"], reviewers_successful="5"
		)
	assert proc.returncode == 0, proc.stderr
	assert "floor_met=true" in proc.stdout
	assert "post_review_comment" in posts
	assert len(calls) == 1 and "kind=findings findings=2" in proc.stdout
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env


def test_handoff_task_gap_blocks_even_when_panel_floor_is_met():
	gap_ledger = LEDGER_EMPTY.replace(
		"(No task gaps reported.)",
		"- requirement: add the flag\n  expected_change_site: scripts/a.sh\n  confidence=[4]\n  flagged_by: [minimax]\n  EVIDENCE: missing",
		1,
	)
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(
			Path(td), ledger=gap_ledger, panel_statuses=["success"] * 4 + ["failed"] * 2, reviewers_successful="4"
		)
	assert proc.returncode == 0, proc.stderr
	assert "floor_met=true" in proc.stdout
	assert len(calls) == 1
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env


def test_handoff_panel_floor_unknown_keeps_todays_behaviour():
	# No PREVIOUS_REVIEWS_DIR and no active-models file: the floor is not applied.
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, reviewers_successful="1")
	assert proc.returncode == 0, proc.stderr
	assert "active=0 floor_met=unknown" in proc.stdout
	assert calls == []
	assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env


def test_handoff_panel_floor_uses_larger_active_models_count():
	# A slot that never wrote a status file (silent drop) still counts as active.
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(
			Path(td), ledger=LEDGER_EMPTY, panel_statuses=["success"] * 2, reviewers_successful="2", active_models=6
		)
	assert proc.returncode == 0, proc.stderr
	assert "successful=2 active=6 floor_met=false" in proc.stdout
	assert len(calls) == 1
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env


def test_handoff_does_not_merge_without_fresh_ready_checks():
	# Pending (or a transient API error) waits for the merge check; a
	# collector that can never become ready keeps the hand-off.
	for status, pending in (("timeout", True), ("api_error", True), ("disabled", False), ("unavailable", False)):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, fresh_status=status)
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
		posts = [call for call in calls if call["payload"]]
		assert len(posts) == 1, (status, calls)
		body = posts[0]["payload"]["body"]
		assert ("ai:claude-fixer-checks-pending:v1" in body) is pending, status
		assert ("ai:claude-fixer-handoff:v1" in body) is not pending, status


def _evidence(tmp: Path) -> dict:
	return json.loads((tmp / "claude_fixer_evidence" / "evidence.json").read_text(encoding="utf-8"))


def test_clean_review_with_running_checks_posts_checks_pending_not_a_handoff():
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		proc, calls, posts, github_env = _run_handoff(tmp, ledger=LEDGER_EMPTY, fresh_status="timeout")
		evidence = _evidence(tmp)
		ledger_copy = (tmp / "claude_fixer_evidence" / "reviewer_consensus.txt").read_text(encoding="utf-8")
	assert proc.returncode == 0, proc.stderr
	assert posts == ""  # no ledger chunks for a clean round
	post = [call for call in calls if call["payload"]][0]
	assert post["args"][:4] == ["api", "-X", "POST", "repos/o/r/issues/42/comments"]
	body = post["payload"]["body"]
	assert body.startswith("## Review round 2: clean, waiting for checks\n")
	assert body.rstrip().endswith(f"<!-- ai:claude-fixer-checks-pending:v1 head={HEAD} round=2 run=99 -->")
	assert "ai:claude-fixer-handoff" not in body
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert f"CLAUDE_FIXER_CHECKS_PENDING pr=42 head={HEAD} round=2 run=99 action=wait" in proc.stdout
	assert evidence["outcome"] == "checks-pending" and evidence["pr"] == 42 and evidence["head_sha"] == HEAD and evidence["round"] == 2
	assert evidence["ledger_sha256"] == hashlib.sha256(LEDGER_EMPTY.encode()).hexdigest()
	assert ledger_copy == LEDGER_EMPTY


def test_checks_pending_kill_switch_restores_the_handoff():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, fresh_status="timeout",
			extra_env={"CLAUDE_FIXER_CHECKS_PENDING_ENABLED": "false"})
	assert proc.returncode == 0, proc.stderr
	assert len(calls) == 1
	assert f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=2 -->" in calls[0]["payload"]["body"]
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env


def test_checks_pending_needs_evidence_and_the_current_head():
	for kwargs in ({"evidence_helper": False}, {"fresh_head": "d" * 40}, {"extra_env": {"GITHUB_RUN_ID": ""}}):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, _env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, fresh_status="timeout", **kwargs)
		assert proc.returncode == 0, proc.stderr
		assert len(calls) == 1, kwargs
		assert "ai:claude-fixer-handoff:v1 kind=findings" in calls[0]["payload"]["body"], kwargs


def test_a_check_that_failed_during_review_is_handed_off_by_name():
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		proc, calls, _posts, github_env = _run_handoff(tmp, ledger=LEDGER_EMPTY, fresh_status="ready", fresh_failed="ci / lint")
		evidence = _evidence(tmp)
	assert proc.returncode == 0, proc.stderr
	assert len(calls) == 1
	body = calls[0]["payload"]["body"]
	assert "ai:claude-fixer-handoff:v1 kind=findings" in body and "Failing check runs on this head: `ci / lint`" in body
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert evidence["outcome"] == "findings" and evidence["failed_checks"] == ["ci / lint"]


def test_checks_pending_comment_is_updated_in_place_for_the_same_head():
	pending = f"## Review round 2: clean, waiting for checks\n\n<!-- ai:claude-fixer-checks-pending:v1 head={HEAD} round=2 run=50 -->"
	handoff = f"## Review round 1: findings handed to the Claude session\n\n<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=1 -->"
	cases = (
		([{"id": 7, "user": {"login": AUTHOR}, "body": handoff}, {"id": 9, "user": {"login": AUTHOR}, "body": pending}], "PATCH"),
		# A newer hand-off on the same head: post a fresh comment.
		([{"id": 7, "user": {"login": AUTHOR}, "body": pending}, {"id": 9, "user": {"login": AUTHOR}, "body": handoff}], "POST"),
		# Someone else's copy of the marker is not ours to edit.
		([{"id": 9, "user": {"login": "someone"}, "body": pending}], "POST"),
		# Another head's comment is left alone.
		([{"id": 9, "user": {"login": AUTHOR}, "body": pending.replace(HEAD, "d" * 40)}], "POST"),
	)
	for comments, method in cases:
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, _env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, fresh_status="timeout",
				extra_env={"MOCK_GH_LOGIN": AUTHOR, "MOCK_GH_COMMENTS": json.dumps(comments)})
		assert proc.returncode == 0, proc.stderr
		write = [call for call in calls if call["payload"]][0]
		assert write["args"][2] == method, (comments, calls)
		if method == "PATCH":
			assert write["args"][3] == "repos/o/r/issues/comments/9"
		assert "run=99 -->" in write["payload"]["body"]


def test_every_handoff_outcome_writes_evidence():
	for kwargs, outcome in (
		({"ledger": LEDGER_WITH_FINDINGS}, "findings"),
		({"ledger": LEDGER_EMPTY}, "clean"),
		({"ledger": None, "pre_review_resolve": True}, "conflict"),
	):
		with tempfile.TemporaryDirectory() as td:
			tmp = Path(td)
			proc, _calls, _posts, _env = _run_handoff(tmp, **kwargs)
			assert proc.returncode == 0, proc.stderr
			assert _evidence(tmp)["outcome"] == outcome, kwargs


def test_handoff_does_not_treat_unparseable_zero_ledger_as_clean():
	broken = LEDGER_EMPTY.replace("(No findings reported.)", "(No findings reported.)\nUnexpected content", 1)
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=broken)
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert len(calls) == 1


def test_verification_with_remaining_findings_blocks_instead_of_merging():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_WITH_FINDINGS, verification=True)
	assert proc.returncode == 0, proc.stderr
	assert len(calls) == 1
	assert "CLAUDE_FIXER_VERIFICATION_FAILED=true" in github_env
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert f"<!-- ai:claude-fixer-verification:v1 head={HEAD} result=unresolved -->" in calls[0]["payload"]["body"]


def test_handoff_failed_checks_alone_are_handed_off():
	context = "failed[0].name: ci / lint\nfailed[0].status: completed\n"
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, check_context=context)
	assert proc.returncode == 0, proc.stderr
	assert "Failing check runs on this head: `ci / lint`" in calls[0]["payload"]["body"]
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env


def test_handoff_missing_ledger_fails_closed():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, posts, github_env = _run_handoff(Path(td), ledger=None)
	assert proc.returncode == 0, proc.stderr
	assert posts == ""
	assert "consensus ledger was not produced" in calls[0]["payload"]["body"]
	assert "kind=findings" in calls[0]["payload"]["body"]
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env


def test_handoff_conflict_posts_conflict_marker_only():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, posts, github_env = _run_handoff(Path(td), ledger=None, pre_review_resolve=True, unmerged="a.py,b.py")
	assert proc.returncode == 0, proc.stderr
	assert posts == "" and len(calls) == 1
	body = calls[0]["payload"]["body"]
	assert f"<!-- ai:claude-fixer-handoff:v1 kind=conflict head={HEAD} round=2 -->" in body
	assert "`a.py`, `b.py`" in body
	assert "[claude-merge-resolve]" in body
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env


def test_handoff_rejects_malformed_head():
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		proc = subprocess.run(
			["bash", "-c", f'source "{HANDOFF_SCRIPT}"'],
			env={**os.environ, "PR_NUMBER": "42", "HEAD_SHA": "nothex", "SUPPORT_SCRIPTS_DIR": str(tmp), "GITHUB_ENV": str(tmp / "e")},
			capture_output=True,
			text=True,
		)
	assert proc.returncode == 1 and "40-hex HEAD_SHA" in proc.stdout


def test_expanded_workflow_carries_the_handoff_body():
	assert "ai:claude-fixer-handoff:v1 kind=findings" in expanded_review_autofix_text()


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
		"pr": {"state": "open", "merged": False, "head": {"ref": head_ref, "sha": HEAD}, "labels": [], "additions": 400, "deletions": 50, "mergeable": (extra_env or {}).get("MOCK_MERGEABLE") != "false", "mergeable_state": "dirty" if (extra_env or {}).get("MOCK_MERGEABLE") == "false" else "clean", "title": "Demo — phase 1/2: x", "body": "Refs #1", **(pr_overrides or {})},
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
		"CLAUDE_FIXER_VERDICT_BOT_LOGIN": FIXER_BOT,
		"CLAUDE_FIXER_CONVERGED_HEAD": converged_head,
	}
	env.update(extra_env or {})
	script = tmp / "gate.sh"
	script.write_text(gate_run, encoding="utf-8")
	proc = subprocess.run(["bash", str(script)], cwd=tmp, env=env, capture_output=True, text=True)
	outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	return proc, outputs


FIXER_REF = "claude/implement-plan-demo-phase-1"


def test_gate_marks_fixer_prs_and_runs_the_first_round():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[], event_name="pull_request")
	assert proc.returncode == 0, proc.stderr
	assert out["claude_fixer"] == "true" and out["should_run"] == "true" and out["claude_fixer_converged"] == "false"


def test_gate_marks_every_claude_head_as_a_fixer_pr():
	"""CLAUDE.md §26: a session's ad-hoc claude/* PR is fixed by Claude too."""
	for head_ref in ("claude/quirky-wozniak-e9t88m", "claude/verify-activation-demo-fix-1"):
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=head_ref, comments=[], event_name="pull_request")
		assert proc.returncode == 0, proc.stderr
		assert out["claude_fixer"] == "true" and out["should_run"] == "true", head_ref


def test_gate_kill_switch_returns_claude_heads_to_the_gpt_path():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref="claude/quirky-wozniak-e9t88m", comments=[], event_name="pull_request",
			extra_env={"CLAUDE_FIXER_ENABLED": "false"})
	assert proc.returncode == 0, proc.stderr
	assert out["claude_fixer"] == "false"


def test_gate_leaves_other_prs_alone():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref="ai/issue-7", comments=[_c(HANDOFF)])
	assert proc.returncode == 0, proc.stderr
	assert out["claude_fixer"] == "false" and out["should_run"] == "true"


def test_gate_skips_dispatch_rerun_while_the_session_owns_the_round():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_c(HANDOFF)])
	assert proc.returncode == 0, proc.stderr
	assert out["should_run"] == "false" and out["skip_reason"] == "claude_fixer_awaiting_session"


DOCS_FILES = [{"filename": "docs/deploy-activation/pr-1.md", "status": "modified"}, {"filename": "notes.md", "status": "added"}]


def test_gate_docs_only_claude_pr_takes_the_deterministic_skip():
	"""Claude PRs get the same doc-only skip + auto-merge as every other PR."""
	for head_ref in ("claude/sharp-franklin-1hrznc-11", FIXER_REF):
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=head_ref, comments=[], event_name="pull_request",
				files=DOCS_FILES, pr_overrides={"changed_files": 2})
		assert proc.returncode == 0, proc.stderr
		assert "AUTOFIX_GATE_DET_SKIP_EVAL pr=42 files=2" in proc.stdout, head_ref
		assert out["claude_fixer"] == "true", head_ref
		assert out["deterministic_skip"] == "true" and out["det_skip_reason"] == "docs_only", head_ref
		assert out["should_run"] == "false" and out["skip_reason"] == "deterministic_skip_docs_only", head_ref
		assert out["head_sha"] == HEAD


def test_gate_small_diff_claude_pr_takes_the_deterministic_skip():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref="claude/quirky-wozniak-e9t88m", comments=[], event_name="pull_request",
			files=[{"filename": "src/app.py", "status": "modified"}],
			pr_overrides={"changed_files": 1, "additions": 3, "deletions": 2})
	assert proc.returncode == 0, proc.stderr
	assert out["deterministic_skip"] == "true" and out["det_skip_reason"] == "small_diff"
	assert out["should_run"] == "false"


def test_gate_claude_pr_skip_keeps_the_protected_path_and_size_guards():
	for files, overrides in (
		([{"filename": "CLAUDE.md", "status": "modified"}], {"changed_files": 1, "additions": 2, "deletions": 1}),
		([{"filename": "src/app.py", "status": "modified"}], {"changed_files": 1, "additions": 40, "deletions": 2}),
	):
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref="claude/quirky-wozniak-e9t88m", comments=[], event_name="pull_request",
				files=files, pr_overrides=overrides)
		assert proc.returncode == 0, proc.stderr
		assert out["deterministic_skip"] == "false" and out["should_run"] == "true", files


def test_gate_docs_only_claude_pr_with_a_pending_handoff_still_waits_on_the_session():
	"""A dispatch on a head the reviewers already handed off never skips past the findings."""
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref="claude/sharp-franklin-1hrznc-11", comments=[_c(HANDOFF)],
			files=DOCS_FILES, pr_overrides={"changed_files": 2})
	assert proc.returncode == 0, proc.stderr
	assert out["should_run"] == "false" and out["skip_reason"] == "claude_fixer_awaiting_session"
	assert out["deterministic_skip"] == "false"


def test_gate_same_head_pull_request_event_with_a_pending_handoff_reviews_instead_of_skipping():
	"""reopened / ready_for_review arrive on an unchanged head: they must not skip past posted findings."""
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref="claude/sharp-franklin-1hrznc-11", comments=[_c(HANDOFF)], event_name="pull_request",
			files=DOCS_FILES, pr_overrides={"changed_files": 2})
	assert proc.returncode == 0, proc.stderr
	assert "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=claude_fixer_pending_handoff pr=42" in proc.stdout
	assert "claude_handoff_suppressed=true" in proc.stdout
	assert out["deterministic_skip"] == "false" and out["should_run"] == "true"


def test_gate_claude_pr_skip_fails_closed_when_the_handoff_lookup_fails():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref="claude/sharp-franklin-1hrznc-11", comments=[], event_name="pull_request",
			files=DOCS_FILES, pr_overrides={"changed_files": 2}, comments_fail=True)
	assert proc.returncode == 0, proc.stderr
	assert "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=claude_fixer_handoff_unverified pr=42 fetch_state=api_error" in proc.stdout
	assert "claude_handoff_suppressed=true" in proc.stdout
	assert out["deterministic_skip"] == "false" and out["should_run"] == "true"


def test_gate_claude_pr_skip_fails_closed_when_the_head_sha_is_invalid():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref="claude/sharp-franklin-1hrznc-11", comments=[], event_name="pull_request",
			files=DOCS_FILES, pr_overrides={"changed_files": 2, "head": {"ref": "claude/sharp-franklin-1hrznc-11", "sha": "not-a-sha"}})
	assert proc.returncode == 0, proc.stderr
	assert "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=claude_fixer_handoff_unverified pr=42" in proc.stdout
	assert out["deterministic_skip"] == "false" and out["should_run"] == "true"


def test_gate_handoff_for_an_older_head_does_not_block_the_skip():
	old_head = "d" * 40
	stale = HANDOFF.replace(HEAD, old_head)
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref="claude/sharp-franklin-1hrznc-11", comments=[_c(stale)], event_name="pull_request",
			files=DOCS_FILES, pr_overrides={"changed_files": 2})
	assert proc.returncode == 0, proc.stderr
	assert out["deterministic_skip"] == "true" and out["det_skip_reason"] == "docs_only"
	assert "claude_handoff_suppressed=false" in proc.stdout


def test_gate_verified_convergence_on_a_docs_only_head_still_reviews():
	"""#4453: an accepted verdict re-runs the reviewer panel; it never takes the skip."""
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(
			Path(td),
			head_ref=FIXER_REF,
			comments=[_c(HANDOFF + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT)],
			converged_head=HEAD,
			files=DOCS_FILES,
			pr_overrides={"changed_files": 2, "additions": 3, "deletions": 1},
		)
	assert proc.returncode == 0, proc.stderr
	assert out["claude_fixer_verify"] == "true"
	assert out["should_run"] == "true" and out["deterministic_skip"] == "false"


def test_gate_accepts_a_verified_convergence_dispatch():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(
			Path(td),
			head_ref=FIXER_REF,
			comments=[_c(HANDOFF + "\n" + LEDGER_HANDOFF), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT)],
			converged_head=HEAD,
		)
	assert proc.returncode == 0, proc.stderr
	assert out["claude_fixer_converged"] == "true"
	assert out["claude_fixer_verify"] == "true"
	assert out["should_run"] == "true" and out["skip_reason"] == ""
	assert out["deterministic_skip"] == "false"
	assert out["head_sha"] == HEAD


def test_gate_rejects_unconfigured_bot_and_human_verdict():
	for env, verdict in (
		({"CLAUDE_FIXER_VERDICT_BOT_LOGIN": ""}, _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT)),
		({"CLAUDE_FIXER_VERDICT_BOT_LOGIN": AUTHOR}, _c(VERDICT + "\n" + LEDGER_VERDICT, author=AUTHOR)),
		({}, _c(VERDICT + "\n" + LEDGER_VERDICT, author="dev", association="COLLABORATOR")),
	):
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_c(HANDOFF + "\n" + LEDGER_HANDOFF), verdict], converged_head=HEAD, extra_env=env)
		assert proc.returncode == 0, proc.stderr
		assert out["should_run"] == "false" and out["claude_fixer_verify"] == "false"
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF,
			comments=[_c(HANDOFF + "\n" + LEDGER_HANDOFF, author=FIXER_BOT), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT)],
			converged_head=HEAD, marker_author=FIXER_BOT)
	assert proc.returncode == 0, proc.stderr
	assert out["claude_fixer_verify"] == "false"  # GH_PAT and fixer must differ.


def test_gate_rejects_failed_comment_fetch():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=None, converged_head=HEAD)
	assert proc.returncode == 0, proc.stderr
	assert out["should_run"] == "false" and out["claude_fixer_verify"] == "false"


def test_gate_does_not_repeat_a_failed_same_head_verification():
	marker = f"<!-- ai:claude-fixer-verification:v1 head={HEAD} result=unresolved -->"
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF,
			comments=[_c(HANDOFF + "\n" + LEDGER_HANDOFF), _c(marker), _c(VERDICT + "\n" + LEDGER_VERDICT, author=FIXER_BOT)],
			converged_head=HEAD)
	assert proc.returncode == 0, proc.stderr
	assert out["claude_fixer_verify"] == "false" and out["should_run"] == "false"


def test_gate_rejects_an_embedded_or_stale_verdict_for_current_head():
	for comments in (
		[_c(HANDOFF + "\nReply with `" + VERDICT + "`.")],
		[_c(HANDOFF), _c(VERDICT), _c(HANDOFF.replace("round=1", "round=2"))],
	):
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=comments, converged_head=HEAD)
		assert proc.returncode == 0, proc.stderr
		assert out["claude_fixer_converged"] == "false"
		assert out["skip_reason"] == "claude_fixer_converged_unverified"


def test_gate_rejects_convergence_without_verdict_stale_head_or_non_fixer_pr():
	cases = (
		(FIXER_REF, [_c(HANDOFF)], HEAD),
		(FIXER_REF, [_c(HANDOFF), _c(VERDICT)], "d" * 40),
		("ai/issue-7", [_c(HANDOFF), _c(VERDICT)], HEAD),
		(FIXER_REF, [_c(HANDOFF), _c(VERDICT, association="NONE")], HEAD),
	)
	for head_ref, comments, converged in cases:
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=head_ref, comments=comments, converged_head=converged)
		assert proc.returncode == 0, proc.stderr
		assert out["claude_fixer_converged"] == "false", (head_ref, converged)
		assert out["should_run"] == "false" and out["skip_reason"] == "claude_fixer_converged_unverified"


def test_gate_disabled_by_repo_var():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_c(HANDOFF)], extra_env={"CLAUDE_FIXER_ENABLED": "false"})
	assert proc.returncode == 0, proc.stderr
	assert out["claude_fixer"] == "false" and out["should_run"] == "true"


# ---- checks pending: merge check step, gate routing, workflow wiring ----

MERGE_CHECK_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_claude_fixer_merge_check.sh"
PENDING = f"## Review round 2: clean, waiting for checks\n\n<!-- ai:claude-fixer-checks-pending:v1 head={HEAD} round=2 run=555 -->"


def _run_merge_check(tmp: Path, *, verdict: dict, context: str | None, evidence_helper: bool = True):
	support = tmp / "support"
	support.mkdir()
	calls = tmp / "calls.jsonl"
	if evidence_helper:
		# verify answers from MOCK_VERIFY; write runs the real helper.
		(support / "review_claude_fixer_evidence.py").write_text(
			"import json, os, runpy, sys\n"
			"if sys.argv[1] == 'verify':\n"
			"\topen(os.environ['MOCK_VERIFY_ARGS'], 'w').write(json.dumps(sys.argv[1:]))\n"
			"\tprint(os.environ['MOCK_VERIFY'])\n"
			"\tsys.exit(0)\n"
			f"sys.argv = [{str(EVIDENCE_HELPER)!r}] + sys.argv[1:]\n"
			f"runpy.run_path({str(EVIDENCE_HELPER)!r}, run_name='__main__')\n",
			encoding="utf-8",
		)
	if context is not None:
		(support / "collect_pr_check_runs_context.py").write_text(
			"import json, os\nfrom pathlib import Path\n"
			f"open({str(tmp / 'collector_env.json')!r}, 'w').write(json.dumps({{k: os.environ.get(k, '') for k in ('CHECK_RUNS_WAIT_TIMEOUT_SECS', 'SELF_RUN_ID', 'CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT')}}))\n"
			f"Path(os.environ['PR_CHECK_RUNS_CONTEXT_FILE']).write_text({context!r})\n",
			encoding="utf-8",
		)
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(RECORDING_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	github_env = tmp / "github_env"
	github_env.write_text("", encoding="utf-8")
	env = {
		**os.environ,
		"PATH": os.pathsep.join((str(bin_dir), os.environ.get("PATH", ""))),
		"PR_NUMBER": "42",
		"GH_TOKEN": "t",
		"GITHUB_REPOSITORY": "o/r",
		"HEAD_SHA": HEAD,
		"HEAD_REF": FIXER_REF,
		"DEFAULT_BRANCH": "main",
		"CLAUDE_FIXER_MERGE_CHECK_RUN_ID": "555",
		"CLAUDE_FIXER_ROUND_INDEX": "4",
		"PR_CHECK_RUNS_CONTEXT_FILE": str(tmp / "checks.txt"),
		"SUPPORT_SCRIPTS_DIR": str(support),
		"GITHUB_RUN_ID": "600",
		"RUNTIME_DIR": str(tmp),
		"GITHUB_ENV": str(github_env),
		"MOCK_GH_CALLS": str(calls),
		"MOCK_VERIFY": json.dumps(verdict),
		"MOCK_VERIFY_ARGS": str(tmp / "verify_args.json"),
	}
	proc = subprocess.run(["bash", "-c", f'source "{MERGE_CHECK_SCRIPT}"'], env=env, capture_output=True, text=True)
	gh_calls = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
	evidence_file = tmp / "claude_fixer_evidence" / "evidence.json"
	evidence = json.loads(evidence_file.read_text()) if evidence_file.exists() else None
	collector_env = json.loads((tmp / "collector_env.json").read_text()) if (tmp / "collector_env.json").exists() else None
	verify_args = json.loads((tmp / "verify_args.json").read_text()) if (tmp / "verify_args.json").exists() else None
	return proc, gh_calls, github_env.read_text(), evidence, collector_env, verify_args


VERIFIED = {"verified": True, "reason": "ok", "evidence": {"schema": 1, "pr": 42, "head_sha": HEAD, "round": 2, "outcome": "checks-pending"}}
GREEN = f"PR_CHECK_RUNS_CONTEXT\nhead_sha: {HEAD}\ncollection_status: ready\ntotal_check_runs: 3\nfailed_count: 0\nincomplete_count: 0\n"
RUNNING = f"PR_CHECK_RUNS_CONTEXT\nhead_sha: {HEAD}\ncollection_status: timeout\ntotal_check_runs: 3\nfailed_count: 0\nincomplete_count: 1\nincomplete[0].name: ci / test\n"
FAILED = f"PR_CHECK_RUNS_CONTEXT\nhead_sha: {HEAD}\ncollection_status: ready\ntotal_check_runs: 3\nfailed_count: 2\nincomplete_count: 0\nfailed[0].name: ci / lint\nfailed[1].name: ci / test\n"


def test_merge_check_green_enables_auto_merge_without_comments():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, github_env, evidence, collector_env, verify_args = _run_merge_check(Path(td), verdict=VERIFIED, context=GREEN)
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env
	assert calls == []
	assert f"CLAUDE_FIXER_CHECKS_PENDING pr=42 head={HEAD} run=555 action=auto_merge" in proc.stdout
	assert evidence["outcome"] == "clean" and evidence["round"] == 2
	assert collector_env == {"CHECK_RUNS_WAIT_TIMEOUT_SECS": "0", "SELF_RUN_ID": "600", "CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT": "true"}
	for pair in (["--run-id", "555"], ["--expect-outcome", "checks-pending"], ["--pr-head-ref", FIXER_REF], ["--default-branch", "main"], ["--head", HEAD]):
		index = verify_args.index(pair[0])
		assert verify_args[index + 1] == pair[1], pair


def test_merge_check_failed_checks_are_handed_to_the_session():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, github_env, evidence, _collector, _args = _run_merge_check(Path(td), verdict=VERIFIED, context=FAILED)
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert len(calls) == 1 and calls[0]["args"][:4] == ["api", "-X", "POST", "repos/o/r/issues/42/comments"]
	body = calls[0]["payload"]["body"]
	# Same header and marker as the hand-off step (gate + check_in_status.py).
	assert body.startswith("## Review round 2: findings handed to the Claude session\n")
	assert f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=2 -->" in body
	assert "`ci / lint`, `ci / test`" in body
	assert "action=handoff_ci" in proc.stdout
	assert evidence["outcome"] == "findings" and evidence["failed_checks"] == ["ci / lint", "ci / test"]


def test_merge_check_running_checks_wait_for_the_next_sweep():
	for context in (RUNNING, GREEN.replace("total_check_runs: 3", "total_check_runs: 0"), RUNNING.replace("timeout", "api_error")):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, github_env, evidence, _collector, _args = _run_merge_check(Path(td), verdict=VERIFIED, context=context)
		assert proc.returncode == 0, proc.stderr
		assert calls == [] and "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
		assert "action=still_waiting" in proc.stdout
		assert evidence is None


def test_merge_check_unverified_evidence_falls_back_to_the_handoff():
	for verdict, helper in (({"verified": False, "reason": "untrusted_caller_ref", "evidence": None}, True), (VERIFIED, False)):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, github_env, _evidence, collector_env, _args = _run_merge_check(Path(td), verdict=verdict, context=GREEN, evidence_helper=helper)
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
		assert collector_env is None  # checks are never read on unverified evidence
		assert len(calls) == 1
		body = calls[0]["payload"]["body"]
		assert f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=5 -->" in body
		assert "could not be verified" in body
		assert "action=evidence_unverified" in proc.stdout


def test_merge_check_unusable_snapshot_falls_back_to_the_handoff():
	for context in (GREEN.replace("ready", "disabled"), GREEN.replace(HEAD, "d" * 40), None):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, github_env, _evidence, _collector, _args = _run_merge_check(Path(td), verdict=VERIFIED, context=context)
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
		assert len(calls) == 1 and "ai:claude-fixer-handoff:v1 kind=findings" in calls[0]["payload"]["body"]
		assert "action=snapshot_unavailable" in proc.stdout


def test_merge_check_rejects_malformed_head():
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		proc = subprocess.run(
			["bash", "-c", f'source "{MERGE_CHECK_SCRIPT}"'],
			env={**os.environ, "PR_NUMBER": "42", "HEAD_SHA": "nothex", "SUPPORT_SCRIPTS_DIR": str(tmp), "GITHUB_ENV": str(tmp / "e")},
			capture_output=True,
			text=True,
		)
	assert proc.returncode == 1 and "40-hex HEAD_SHA" in proc.stdout


def _pending_c(body: str = PENDING, author: str = AUTHOR) -> dict:
	return {"body": body, "author_login": author, "author_type": "User", "author_association": "OWNER"}


def test_gate_routes_a_checks_pending_head_to_the_merge_check():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_c(HANDOFF.replace(HEAD, "d" * 40)), _pending_c()])
	assert proc.returncode == 0, proc.stderr
	assert out["should_run"] == "true" and out["skip_reason"] == ""
	assert out["claude_fixer_merge_check"] == "true" and out["claude_fixer_merge_check_run"] == "555"
	assert f"AUTOFIX_GATE_CLAUDE_FIXER_MERGE_CHECK pr=42 head={HEAD} evidence_run=555" in proc.stdout


def test_gate_newest_marker_decides_between_merge_check_and_awaiting_session():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_c(HANDOFF), _pending_c()])
	assert out["claude_fixer_merge_check"] == "true", proc.stdout
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_pending_c(), _c(HANDOFF)])
	assert out["claude_fixer_merge_check"] == "false"
	assert out["should_run"] == "false" and out["skip_reason"] == "claude_fixer_awaiting_session"


def test_gate_ignores_checks_pending_markers_it_must_not_trust():
	cases = (
		({"comments": [_pending_c(author="someone")]}, "untrusted author"),
		({"comments": [_pending_c(body="Quote:\n" + PENDING)]}, "no workflow header"),
		({"comments": [_pending_c(body=PENDING.replace(HEAD, "d" * 40))]}, "another head"),
		({"comments": [_pending_c()], "extra_env": {"CLAUDE_FIXER_CHECKS_PENDING_ENABLED": "false"}}, "kill switch"),
		({"comments": [_pending_c()], "event_name": "pull_request"}, "pull_request event"),
		({"comments": [_pending_c()], "extra_env": {"FORCE_RB_JUDGE": "true"}}, "force_rb_judge"),
	)
	for kwargs, label in cases:
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=FIXER_REF, **kwargs)
		assert proc.returncode == 0, proc.stderr
		assert out["claude_fixer_merge_check"] == "false", label
		assert out["should_run"] == "true", label  # a normal full review runs


def test_gate_does_not_merge_check_a_conflicted_pr():
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		proc, out = _run_gate(tmp, head_ref=FIXER_REF, comments=[_pending_c()], extra_env={"MOCK_MERGEABLE": "false"})
	assert proc.returncode == 0, proc.stderr
	assert out["claude_fixer_merge_check"] == "false" and out["should_run"] == "true"


def test_gate_merge_check_bypasses_the_terminal_same_head_skip():
	gate_run = _steps(WORKFLOW, "gate")["Evaluate review gate"]["run"]
	block = gate_run.split("# ----- Terminal same-head skip", 1)[1].split("terminal_force_review=\"false\"", 1)[0]
	assert '&& [ "${CLAUDE_FIXER_MERGE_CHECK}" != "true" ] \\' in block
	assert 'CLAUDE_FIXER_MERGE_CHECK="false"' in gate_run and 'CLAUDE_FIXER_MERGE_CHECK_RUN=""' in gate_run


def test_merge_check_wiring_in_the_workflow():
	outputs = WORKFLOW["jobs"]["gate"]["outputs"]
	assert outputs["claude_fixer_merge_check"] == "${{ steps.evaluate.outputs.claude_fixer_merge_check }}"
	assert outputs["claude_fixer_merge_check_run"] == "${{ steps.evaluate.outputs.claude_fixer_merge_check_run }}"
	gate_env = _steps(WORKFLOW, "gate")["Evaluate review gate"]["env"]
	assert gate_env["CLAUDE_FIXER_CHECKS_PENDING_ENABLED"] == "${{ vars.CLAUDE_FIXER_CHECKS_PENDING_ENABLED || 'true' }}"
	agent_env = WORKFLOW["jobs"]["codex-agent"]["env"]
	assert agent_env["CLAUDE_FIXER_MERGE_CHECK"] == "${{ needs.gate.outputs.claude_fixer_merge_check }}"
	assert agent_env["CLAUDE_FIXER_MERGE_CHECK_RUN_ID"] == "${{ needs.gate.outputs.claude_fixer_merge_check_run }}"
	handoff = AGENT_STEPS["Hand review round to Claude session (Claude-fixer mode)"]
	assert handoff["env"]["CLAUDE_FIXER_CHECKS_PENDING_ENABLED"] == "${{ vars.CLAUDE_FIXER_CHECKS_PENDING_ENABLED || 'true' }}"
	names = list(AGENT_STEPS)
	merge_check = "Claude-fixer merge check"
	upload = "Upload Claude-fixer evidence"
	assert names.index("Hand review round to Claude session (Claude-fixer mode)") < names.index(merge_check) < names.index(upload) < names.index("Enable auto-merge on PR")
	step = AGENT_STEPS[merge_check]
	assert step["if"] == "env.CLAUDE_FIXER_MERGE_CHECK == 'true' && env.CLAUDE_FIXER_MODE == 'true' && env.PR_CLOSED != 'true'"
	assert step["env"]["HEAD_SHA"] == "${{ env.INITIAL_HEAD_SHA }}"
	assert step["env"]["DEFAULT_BRANCH"] == "${{ github.event.repository.default_branch }}"
	step = AGENT_STEPS[upload]
	# A judge run uploads the same artifact name after the judge instead.
	assert step["if"] == "always() && env.CLAUDE_FIXER_MODE == 'true' && env.CLAUDE_FIXER_JUDGE != 'true'"
	assert step["continue-on-error"] is True
	assert step["uses"].startswith("actions/upload-artifact@")
	assert step["with"]["name"] == "claude-fixer-evidence-${{ github.run_id }}-${{ github.run_attempt }}"
	assert step["with"]["path"] == "${{ env.RUNTIME_DIR }}/claude_fixer_evidence/"
	assert step["with"]["retention-days"] == 30
	# The merge check never labels the PR review-blocked (its reviewers are skipped on purpose).
	assert "env.CLAUDE_FIXER_MERGE_CHECK != 'true'" in AGENT_STEPS["Label Claude-fixer PR review-blocked (autofix exhaustion)"]["if"]


def _run_retrigger_guard(tmp: Path, *, merge_check: bool, force_rb_judge: bool = False, autofix_commits: int = 0, judge: bool = False) -> dict:
	repo = tmp / "repo"
	repo.mkdir()
	git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "base"], check=True)
	for index in range(autofix_commits):
		subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", f"[claude-autofix] review round {index + 1}: x"], check=True)
	output = tmp / "out.txt"
	output.write_text("", encoding="utf-8")
	env = {
		**os.environ,
		"GITHUB_OUTPUT": str(output),
		"MAX_AUTOFIX_ITERATIONS": "5",
		"FORCE_RB_JUDGE": "true" if force_rb_judge else "false",
		"ORCH_PR_AUTOFIX_FLOW_ENABLED": "false",
		"ORCH_INTEGRATION_BRANCH_PATTERN": "^orchestrator/project-",
		"PR_META_FILE": str(tmp / "missing.json"),
		"CLAUDE_FIXER_MERGE_CHECK": "true" if merge_check else "false",
		"CLAUDE_FIXER_JUDGE": "true" if judge else "false",
	}
	proc = subprocess.run(["bash", "-c", AGENT_STEPS["Count autofix iterations"]["run"]], cwd=repo, env=env, capture_output=True, text=True)
	assert proc.returncode == 0, proc.stderr
	lines = output.read_text(encoding="utf-8").splitlines()
	keys = [line.split("=", 1)[0] for line in lines]
	assert len(keys) == len(set(keys)), lines  # every output written once
	return dict(line.split("=", 1) for line in lines)


def test_merge_check_early_check_read_does_not_wait():
	# The merge check re-reads checks itself with no wait; the early CI-context read must not poll
	# for CHECK_RUNS_WAIT_TIMEOUT_SECS on every 30-minute sweep dispatch (conformance fix).
	step = AGENT_STEPS["Collect PR check-run failures (CI/lint autofix context)"]
	assert step["env"]["CHECK_RUNS_WAIT_TIMEOUT_SECS"] == (
		"${{ env.CLAUDE_FIXER_MERGE_CHECK == 'true' && '0' || env.CHECK_RUNS_WAIT_TIMEOUT_SECS }}"
	)
	assert "CLAUDE_FIXER_MERGE_CHECK" in WORKFLOW["jobs"]["codex-agent"]["env"]


def test_merge_check_short_circuits_reviewers_and_judge():
	with tempfile.TemporaryDirectory() as td:
		out = _run_retrigger_guard(Path(td), merge_check=True, autofix_commits=2)
	assert out["max_iterations_reached"] == "true" and out["skip_judge"] == "true" and out["autofix_iteration"] == "2"
	for kwargs, expected in (({"merge_check": False}, "false"), ({"merge_check": False, "force_rb_judge": True}, "true"), ({"merge_check": False, "autofix_commits": 5}, "true")):
		with tempfile.TemporaryDirectory() as td:
			out = _run_retrigger_guard(Path(td), **kwargs)
		assert out["skip_judge"] == "false" and out["max_iterations_reached"] == expected, kwargs
	# skip_judge=true is what keeps the auto-merge steps eligible and the
	# review-blocked judge / exhaustion steps off.
	for name in ("Enable auto-merge on PR", "Mark linked issues ready to merge"):
		assert "(steps.retrigger_guard.outputs.max_iterations_reached != 'true' || steps.retrigger_guard.outputs.skip_judge == 'true')" in AGENT_STEPS[name]["if"]
	for name in ("Review-blocked judge decision", "Mark linked issues review-blocked (autofix exhaustion)", "Post review-blocked comment on PR (autofix exhaustion)"):
		assert "steps.retrigger_guard.outputs.skip_judge != 'true'" in AGENT_STEPS[name]["if"], name


def test_review_autofix_stays_under_the_phase_size_budget():
	assert REVIEW_AUTOFIX_WORKFLOW_PATH.stat().st_size < 470_000


# ---- Claude-fixer GPT judge (claude_fixer_judge_head) ----

# The hand-off step's real layout: header, run link, markers.
RUN_LINK_HANDOFF = f"## Review round 1: findings handed to the Claude session\n\nReviewed head: `{HEAD}` ([workflow run](https://github.com/o/r/actions/runs/4321)).\n\n" + HANDOFF + "\n" + LEDGER_HANDOFF
JUDGE_VERDICT = f"## Review round 1: GPT judge verdict — merge\n\n<!-- ai:claude-fixer-judge:v1 head={HEAD} round=1 run=900 decision=merge -->"


def test_judge_head_input_on_every_entry_point():
	for trigger in ("workflow_call", "workflow_dispatch"):
		spec = WORKFLOW["on"][trigger]["inputs"]["claude_fixer_judge_head"]
		assert spec["default"] == "" and spec["type"] == "string" and spec["required"] is False
	for wrapper in WRAPPERS:
		workflow = _load(wrapper)
		assert workflow["on"]["workflow_dispatch"]["inputs"]["claude_fixer_judge_head"]["default"] == ""
		assert workflow["jobs"]["review"]["with"]["claude_fixer_judge_head"] == (
			"${{ github.event_name == 'workflow_dispatch' && github.event.inputs.claude_fixer_judge_head || '' }}"
		)
	# internal-review.yml calls review_autofix.yml@main: never forward a new input.
	internal = _load(INTERNAL_REVIEW)
	assert "claude_fixer_judge_head" not in internal["on"]["workflow_dispatch"]["inputs"]
	for job in internal["jobs"].values():
		assert "claude_fixer_judge_head" not in (job.get("with") or {})
	gate_env = _steps(WORKFLOW, "gate")["Evaluate review gate"]["env"]
	assert gate_env["CLAUDE_FIXER_JUDGE_HEAD"] == "${{ inputs.claude_fixer_judge_head || github.event.inputs.claude_fixer_judge_head || '' }}"
	assert gate_env["CLAUDE_FIXER_JUDGE_ENABLED"] == "${{ vars.CLAUDE_FIXER_JUDGE_ENABLED || 'true' }}"


def test_gate_accepts_a_judge_dispatch_for_the_latest_findings_handoff():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_c(RUN_LINK_HANDOFF)], extra_env={"CLAUDE_FIXER_JUDGE_HEAD": HEAD})
	assert proc.returncode == 0, proc.stderr
	assert out["should_run"] == "true" and out["skip_reason"] == ""
	assert out["claude_fixer_judge"] == "true"
	assert out["claude_fixer_judge_run"] == "4321" and out["claude_fixer_judge_round"] == "1" and out["claude_fixer_judge_ledger"] == DIGEST
	assert out["claude_fixer_merge_check"] == "false" and out["claude_fixer_converged"] == "false"
	assert f"AUTOFIX_GATE_CLAUDE_FIXER_JUDGE pr=42 head={HEAD} requested={HEAD} accepted=true detail=handoff_run=4321 round=1" in proc.stdout


def test_gate_rejects_judge_dispatches_it_cannot_bind():
	cases = (
		({"comments": [_c(RUN_LINK_HANDOFF)], "judge_head": "d" * 40}, "stale_or_invalid_head"),
		({"comments": [_c(RUN_LINK_HANDOFF)], "judge_head": "not-a-sha"}, "stale_or_invalid_head"),
		({"comments": [_c(RUN_LINK_HANDOFF)], "extra_env": {"CLAUDE_FIXER_JUDGE_ENABLED": "false"}}, "disabled"),
		({"comments": [_c(RUN_LINK_HANDOFF)], "head_ref": "ai/issue-7"}, "not_claude_fixer_pr"),
		({"comments": []}, "no_handoff"),
		({"comments": [_c(RUN_LINK_HANDOFF, author="someone")]}, "no_handoff"),
		({"comments": [_c(RUN_LINK_HANDOFF), _c(CONFLICT)]}, "latest_handoff_not_findings"),
		({"comments": [_c(RUN_LINK_HANDOFF.replace(LEDGER_HANDOFF, ""))]}, "no_ledger_digest"),
		({"comments": [_c(RUN_LINK_HANDOFF.replace(LEDGER_HANDOFF, LEDGER_HANDOFF.replace("round=1", "round=2")))]}, "no_ledger_digest"),
		({"comments": [_c(HANDOFF + "\n" + LEDGER_HANDOFF)]}, "no_handoff_run"),
		({"comments": [_c(RUN_LINK_HANDOFF), _c(JUDGE_VERDICT)]}, "already_judged"),
		({"comments": None}, "comments_"),
	)
	for kwargs, detail in cases:
		judge_head = kwargs.pop("judge_head", HEAD)
		extra = {"CLAUDE_FIXER_JUDGE_HEAD": judge_head, **kwargs.pop("extra_env", {})}
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=kwargs.pop("head_ref", FIXER_REF), extra_env=extra, **kwargs)
		assert proc.returncode == 0, proc.stderr
		assert out["claude_fixer_judge"] == "false", detail
		assert out["should_run"] == "false" and out["skip_reason"] == "claude_fixer_judge_unverified", detail
		assert "accepted=false detail=" + detail in proc.stdout, (detail, proc.stdout)


def test_gate_judges_a_newer_handoff_after_an_old_verdict_for_the_same_round():
	"""A force-review of the same head re-issues round 1; the old verdict answers the old hand-off only."""
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_c(RUN_LINK_HANDOFF), _c(JUDGE_VERDICT), _c(RUN_LINK_HANDOFF.replace("4321", "4400"))], extra_env={"CLAUDE_FIXER_JUDGE_HEAD": HEAD})
	assert out["claude_fixer_judge"] == "true" and out["claude_fixer_judge_run"] == "4400", proc.stdout


def test_gate_judge_bypasses_the_terminal_same_head_skip_and_exports_outputs():
	gate_run = _steps(WORKFLOW, "gate")["Evaluate review gate"]["run"]
	block = gate_run.split("# ----- Terminal same-head skip", 1)[1].split("terminal_force_review=\"false\"", 1)[0]
	assert '&& [ "${CLAUDE_FIXER_JUDGE}" != "true" ] \\' in block
	outputs = WORKFLOW["jobs"]["gate"]["outputs"]
	agent_env = WORKFLOW["jobs"]["codex-agent"]["env"]
	for name, env_name in (("claude_fixer_judge", "CLAUDE_FIXER_JUDGE"), ("claude_fixer_judge_run", "CLAUDE_FIXER_JUDGE_RUN_ID"), ("claude_fixer_judge_round", "CLAUDE_FIXER_JUDGE_ROUND"), ("claude_fixer_judge_ledger", "CLAUDE_FIXER_JUDGE_LEDGER_SHA256")):
		assert outputs[name] == "${{ steps.evaluate.outputs." + name + " }}"
		assert agent_env[env_name] == "${{ needs.gate.outputs." + name + " }}"


def test_judge_dispatch_short_circuits_reviewers_and_keeps_the_judge():
	with tempfile.TemporaryDirectory() as td:
		out = _run_retrigger_guard(Path(td), merge_check=False, judge=True, autofix_commits=1)
	assert out["max_iterations_reached"] == "true" and out["skip_judge"] == "false"


def test_judge_step_runs_only_on_an_accepted_judge_dispatch():
	judge_if = AGENT_STEPS["Review-blocked judge decision"]["if"]
	assert judge_if.endswith("&& (env.CLAUDE_FIXER_MODE != 'true' || env.CLAUDE_FIXER_JUDGE == 'true')")
	env = AGENT_STEPS["Review-blocked judge decision"]["env"]
	assert env["CLAUDE_FIXER_JUDGE_FIX_CAP"] == "${{ vars.CLAUDE_FIXER_JUDGE_FIX_CAP || '2' }}"
	assert env["CLAUDE_FIXER_CHECKS_PENDING_ENABLED"] == "${{ vars.CLAUDE_FIXER_CHECKS_PENDING_ENABLED || 'true' }}"
	prepare = AGENT_STEPS["Prepare Claude-fixer judge"]
	assert prepare["if"] == "success() && env.CLAUDE_FIXER_JUDGE == 'true' && env.CLAUDE_FIXER_MODE == 'true' && env.PR_CLOSED != 'true'"
	assert prepare["env"]["HEAD_SHA"] == "${{ env.INITIAL_HEAD_SHA }}"
	names = list(AGENT_STEPS)
	assert names.index("Prepare Claude-fixer judge") + 1 == names.index("Review-blocked judge decision")
	upload = AGENT_STEPS["Upload Claude-fixer judge evidence"]
	assert upload["if"] == "always() && env.CLAUDE_FIXER_JUDGE == 'true'"
	assert upload["with"]["name"] == AGENT_STEPS["Upload Claude-fixer evidence"]["with"]["name"]
	assert names.index("Review-blocked judge decision") < names.index("Upload Claude-fixer judge evidence")
	# A judge that decided labels nothing; a judge that could not decide blocks the PR.
	label_if = AGENT_STEPS["Label Claude-fixer PR review-blocked (autofix exhaustion)"]["if"]
	assert "(env.CLAUDE_FIXER_JUDGE != 'true' || steps.rb_judge.outputs.judge_handled != 'true')" in label_if
	assert "claude_fixer_*)" in AGENT_STEPS["Post review-blocked comment on PR (autofix exhaustion)"]["run"]


# ---- "Prepare Claude-fixer judge" step, executed with stubbed helpers ----

PREPARE_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_claude_fixer_judge.sh"
JUDGE_HELPER = REPO_ROOT / "scripts" / "review_claude_fixer_judge.py"

FAKE_EVIDENCE = """import json, os, sys
from pathlib import Path
def verify_evidence(**kwargs):
	return {"verified": False, "reason": "not_used", "evidence": None}
if __name__ == "__main__":
	args = sys.argv[1:]
	Path(os.environ["MOCK_VERIFY_ARGS"]).write_text(json.dumps(args))
	verdict = json.loads(os.environ["MOCK_VERIFY"])
	if verdict.get("verified") and "--ledger-out" in args:
		Path(args[args.index("--ledger-out") + 1]).write_text(os.environ["MOCK_LEDGER"])
	print(json.dumps(verdict))
"""


def _handoff_comment(comment_id: int = 2, digest: str = DIGEST, round_number: int = 1) -> dict:
	return {
		"id": comment_id,
		"author_association": "OWNER",
		"user": {"login": "workflow-bot"},
		"body": f"## Review round {round_number}: findings handed to the Claude session\n\n<!-- ai:claude-fixer-handoff:v2 head={HEAD} round={round_number} ledger={digest} -->",
	}


def _run_prepare(tmp: Path, *, verdict: dict, comments: list[dict], ledger: str = LEDGER_WITH_FINDINGS, handoff: dict | None = None, gate_login: str | None = None):
	"""`handoff` is the hand-off comment the rejection must follow; the default is the current
	ledger's hand-off at id 2, older than every rejection the tests post."""
	comments = [handoff if handoff is not None else _handoff_comment(), *comments]
	support = tmp / "support"
	support.mkdir()
	(support / "review_claude_fixer_evidence.py").write_text(FAKE_EVIDENCE, encoding="utf-8")
	(support / "review_claude_fixer_judge.py").write_text(JUDGE_HELPER.read_text(encoding="utf-8"), encoding="utf-8")
	comments_file = tmp / "comments.json"
	comments_file.write_text(json.dumps(comments), encoding="utf-8")
	payload_file = tmp / "pr_payload.json"
	payload_file.write_text(json.dumps({"user": {"login": "PR-Author"}}), encoding="utf-8")
	# `gh api user` answers with the workflow account; every other call fails.
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text('#!/usr/bin/env bash\nif [ "$1 $2" = "api user" ]; then echo workflow-bot; exit 0; fi\nexit 1\n', encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	github_env = tmp / "github_env"
	github_env.write_text("", encoding="utf-8")
	env = {
		**os.environ,
		"PATH": os.pathsep.join((str(bin_dir), os.environ.get("PATH", ""))),
		"PR_PAYLOAD_FILE": str(payload_file),
		"PR_NUMBER": "42",
		"GITHUB_REPOSITORY": "o/r",
		"HEAD_SHA": HEAD,
		"HEAD_REF": FIXER_REF,
		"DEFAULT_BRANCH": "main",
		"CLAUDE_FIXER_JUDGE_RUN_ID": "4321",
		"CLAUDE_FIXER_JUDGE_ROUND": "1",
		"CLAUDE_FIXER_JUDGE_LEDGER_SHA256": DIGEST,
		"PR_ISSUE_COMMENTS_FILE": str(comments_file),
		"SUPPORT_SCRIPTS_DIR": str(support),
		"RUNTIME_DIR": str(tmp),
		"GITHUB_ENV": str(github_env),
		"MOCK_VERIFY": json.dumps(verdict),
		"MOCK_VERIFY_ARGS": str(tmp / "verify_args.json"),
		"MOCK_LEDGER": ledger,
	}
	env.pop("FINGERPRINT_CAP_MARKER_AUTHOR_LOGIN", None)
	if gate_login is not None:
		env["FINGERPRINT_CAP_MARKER_AUTHOR_LOGIN"] = gate_login
	proc = subprocess.run(["bash", "-c", f'source "{PREPARE_SCRIPT}"'], env=env, capture_output=True, text=True)
	verify_args = json.loads((tmp / "verify_args.json").read_text()) if (tmp / "verify_args.json").exists() else None
	inputs = tmp / "claude_fixer_judge"
	return proc, github_env.read_text(), verify_args, inputs


def _rejection(body: str, association: str = "OWNER", comment_id: int = 5, login: str = "pr-author") -> dict:
	return {"id": comment_id, "author_association": association, "user": {"login": login}, "body": body}


def test_prepare_step_collects_verified_inputs():
	marker = f"<!-- ai:claude-fixer-rejection:v1 head={HEAD} round=1 -->"
	comments = [
		_rejection(f"F1 rejected: quoted at a.sh:10\n{marker}", comment_id=5),
		_rejection(f"forged\n{marker}", association="NONE", comment_id=9),
		_rejection(f"quoting the marker `{marker}` inline", comment_id=10),
	]
	with tempfile.TemporaryDirectory() as td:
		proc, github_env, verify_args, inputs = _run_prepare(Path(td), verdict={"verified": True, "reason": "ok", "evidence": {}}, comments=comments)
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_JUDGE_READY=true" in github_env
		assert f"CLAUDE_FIXER_JUDGE_INPUTS_DIR={inputs}" in github_env
		findings = json.loads((inputs / "findings.json").read_text())
		assert [f["id"] for f in findings] == ["F1", "F2"]
		assert (inputs / "rejection.txt").read_text().startswith("F1 rejected: quoted at a.sh:10")
		assert json.loads((inputs / "prior_rulings.json").read_text())["rulings"] == []
	for pair in (["--run-id", "4321"], ["--expect-outcome", "findings"], ["--round", "1"], ["--expect-ledger", DIGEST], ["--head", HEAD], ["--pr-head-ref", FIXER_REF]):
		index = verify_args.index(pair[0])
		assert verify_args[index + 1] == pair[1], pair
	assert "action=prepared findings=2 prior_rulings=0 rejection=found handoff_run=4321" in proc.stdout


def test_prepare_step_refuses_a_ledger_with_no_findings():
	"""Conformance 3/3: a below-floor round is handed off with an empty ledger; the
	judge must not rule "nothing upheld" and merge a head too few reviewers saw."""
	with tempfile.TemporaryDirectory() as td:
		proc, github_env, _args, inputs = _run_prepare(Path(td), verdict={"verified": True, "reason": "ok", "evidence": {}}, comments=[], ledger=LEDGER_EMPTY)
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_JUDGE_READY=false" in github_env
		assert "CLAUDE_FIXER_JUDGE_SKIP_REASON=no_findings" in github_env
		assert "CLAUDE_FIXER_JUDGE_READY=true" not in github_env
		assert json.loads((inputs / "findings.json").read_text()) == []
	assert "action=not_ready reason=no_findings" in proc.stdout


def test_prepare_step_fails_closed_on_unverified_evidence_or_bad_inputs():
	with tempfile.TemporaryDirectory() as td:
		proc, github_env, _args, inputs = _run_prepare(Path(td), verdict={"verified": False, "reason": "evidence_ledger_mismatch", "evidence": None}, comments=[])
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_JUDGE_READY=false" in github_env
		assert "CLAUDE_FIXER_JUDGE_SKIP_REASON=evidence_evidence_ledger_mismatch" in github_env
		assert not (inputs / "findings.json").exists()
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		github_env = tmp / "e"
		github_env.write_text("")
		proc = subprocess.run(["bash", "-c", f'source "{PREPARE_SCRIPT}"'], env={**os.environ, "PR_NUMBER": "42", "HEAD_SHA": HEAD, "CLAUDE_FIXER_JUDGE_ROUND": "1", "CLAUDE_FIXER_JUDGE_RUN_ID": "5", "CLAUDE_FIXER_JUDGE_LEDGER_SHA256": "short", "RUNTIME_DIR": str(tmp), "GITHUB_ENV": str(github_env), "SUPPORT_SCRIPTS_DIR": str(tmp)}, capture_output=True, text=True)
		assert proc.returncode == 0 and "CLAUDE_FIXER_JUDGE_SKIP_REASON=invalid_inputs" in github_env.read_text()


def test_prepare_accepts_the_workflow_accounts_rejection():
	marker = f"<!-- ai:claude-fixer-rejection:v1 head={HEAD} round=1 -->"
	with tempfile.TemporaryDirectory() as td:
		proc, github_env, _args, inputs = _run_prepare(Path(td), verdict={"verified": True, "reason": "ok", "evidence": {}}, comments=[_rejection(f"sweep fixer\n{marker}", login="Workflow-Bot")])
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_JUDGE_READY=true" in github_env
		assert (inputs / "rejection.txt").read_text().startswith("sweep fixer")


def test_prepare_refuses_a_dispatch_without_the_fixers_rejection():
	"""Security follow-up #6061: a dispatch alone never authorizes the judge; without a
	collaborator's rejection comment for this head and round it decides nothing."""
	marker = f"<!-- ai:claude-fixer-rejection:v1 head={HEAD} round=1 -->"
	for comments in (
		[],
		[_rejection(f"forged\n{marker}", association="NONE")],
		[_rejection(f"other round\n<!-- ai:claude-fixer-rejection:v1 head={HEAD} round=2 -->")],
		# PR #6069 review: a collaborator who is neither the PR author nor the workflow account.
		[_rejection(f"another collaborator\n{marker}", association="COLLABORATOR", login="someone-else")],
	):
		with tempfile.TemporaryDirectory() as td:
			proc, github_env, _args, _inputs = _run_prepare(Path(td), verdict={"verified": True, "reason": "ok", "evidence": {}}, comments=comments)
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_JUDGE_READY=false" in github_env and "CLAUDE_FIXER_JUDGE_SKIP_REASON=rejection_missing" in github_env
		assert "CLAUDE_FIXER_JUDGE_READY=true" not in github_env
		assert "action=not_ready reason=rejection_missing" in proc.stdout


def test_prepare_reuses_the_gates_workflow_login():
	"""PR #6069 review round 2: the gate's resolved workflow login replaces the second GET /user."""
	marker = f"<!-- ai:claude-fixer-rejection:v1 head={HEAD} round=1 -->"
	with tempfile.TemporaryDirectory() as td:
		proc, github_env, _args, inputs = _run_prepare(Path(td), verdict={"verified": True, "reason": "ok", "evidence": {}}, comments=[_rejection(f"gate account\n{marker}", login="gate-bot")], gate_login="Gate-Bot")
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_JUDGE_READY=true" in github_env
		assert (inputs / "rejection.txt").read_text().startswith("gate account")
	# With the gate's login set, `gh api user` (workflow-bot here) is not consulted.
	with tempfile.TemporaryDirectory() as td:
		proc, github_env, _args, _inputs = _run_prepare(Path(td), verdict={"verified": True, "reason": "ok", "evidence": {}}, comments=[_rejection(f"stub account\n{marker}", login="workflow-bot")], gate_login="gate-bot")
		assert "CLAUDE_FIXER_JUDGE_SKIP_REASON=rejection_missing" in github_env


def test_prepare_binds_the_rejection_to_the_current_ledgers_handoff():
	"""PR #6069 review round 2: a rejection posted before the hand-off of the ledger being judged
	(an earlier ledger on the same head and round) does not authorize the judge."""
	marker = f"<!-- ai:claude-fixer-rejection:v1 head={HEAD} round=1 -->"
	other_digest = "f" * 64
	for handoff in (
		_handoff_comment(comment_id=7),  # the current hand-off is newer than the rejection (id 5)
		_handoff_comment(comment_id=2, digest=other_digest),  # only an earlier ledger's hand-off exists
		{"id": 2, "author_association": "OWNER", "body": "no hand-off marker"},
	):
		with tempfile.TemporaryDirectory() as td:
			proc, github_env, _args, _inputs = _run_prepare(Path(td), verdict={"verified": True, "reason": "ok", "evidence": {}}, comments=[_rejection(f"F1 rejected\n{marker}")], handoff=handoff)
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_JUDGE_SKIP_REASON=rejection_missing" in github_env, handoff
		assert "CLAUDE_FIXER_JUDGE_READY=true" not in github_env
	# A rejection after the current hand-off is accepted even when an earlier ledger's hand-off is newer still.
	with tempfile.TemporaryDirectory() as td:
		proc, github_env, _args, _inputs = _run_prepare(Path(td), verdict={"verified": True, "reason": "ok", "evidence": {}}, comments=[_rejection(f"F1 rejected\n{marker}", comment_id=5), _handoff_comment(comment_id=3, digest=other_digest)], handoff=_handoff_comment(comment_id=4))
		assert "CLAUDE_FIXER_JUDGE_READY=true" in github_env, proc.stdout


# ---- sticky rulings in the hand-off step ----

STICKY_EVIDENCE = """import json, os
def verify_evidence(**kwargs):
	return {"verified": True, "reason": "ok", "evidence": {"judge": {"rulings": json.loads(os.environ["MOCK_RULINGS"])}}}
def main():
	return 0
if __name__ == "__main__":
	import runpy, sys
	sys.argv[0] = os.environ["REAL_EVIDENCE"]
	runpy.run_path(os.environ["REAL_EVIDENCE"], run_name="__main__")
"""


def _sticky_repo(tmp: Path, *, change_after: bool = False, change_path: str = "scripts/a.sh") -> tuple[str, str]:
	"""A git checkout holding scripts/a.sh; returns (path, the ruled head)."""
	repo = tmp / "checkout"
	(repo / "scripts").mkdir(parents=True)
	(repo / "scripts" / "a.sh").write_text("echo $x\n", encoding="utf-8")
	git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo)]
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	subprocess.run([*git, "add", "-A"], check=True)
	subprocess.run([*git, "commit", "-qm", "ruled"], check=True)
	ruled = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
	if change_after:
		(repo / change_path).parent.mkdir(parents=True, exist_ok=True)
		(repo / change_path).write_text("eval $x\n", encoding="utf-8")
		subprocess.run([*git, "add", "-A"], check=True)
		subprocess.run([*git, "commit", "-qm", "changed"], check=True)
	return str(repo), ruled


def _run_handoff_with_rulings(tmp: Path, rulings: list[dict], *, enabled: str = "true", change_after: bool = False, change_path: str = "scripts/a.sh"):
	cwd, ruled_head = _sticky_repo(tmp, change_after=change_after, change_path=change_path)
	comments = tmp / "pr_comments.json"
	comments.write_text(json.dumps([{"id": 3, "author_association": "OWNER", "body": f"<!-- ai:claude-fixer-judge:v1 head={ruled_head} round=1 run=900 decision=merge -->"}]), encoding="utf-8")
	extra = {
		"PR_ISSUE_COMMENTS_FILE": str(comments),
		"MOCK_RULINGS": json.dumps(rulings),
		"REAL_EVIDENCE": str(EVIDENCE_HELPER),
		"CLAUDE_FIXER_JUDGE_ENABLED": enabled,
		"DEFAULT_BRANCH": "main",
	}
	orig_write_text = Path.write_text

	# _run_handoff copies the real evidence helper; swap in the stub (which
	# still delegates the CLI `write` to the real helper) after it does.
	def patched(self, data, *args, **kwargs):
		if self.name == "review_claude_fixer_evidence.py" and "support" in self.parts:
			orig_write_text(self.parent / "review_claude_fixer_judge.py", JUDGE_HELPER.read_text(encoding="utf-8"), encoding="utf-8")
			data = STICKY_EVIDENCE
		return orig_write_text(self, data, *args, **kwargs)

	Path.write_text = patched
	try:
		return _run_handoff(tmp, ledger=LEDGER_WITH_FINDINGS, extra_env=extra, cwd=cwd)
	finally:
		Path.write_text = orig_write_text


def test_handoff_demotes_findings_the_judge_ruled_invalid():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff_with_rulings(Path(td), [{"file": "scripts/a.sh", "line": 12, "ruling": "invalid", "claim": "unquoted expansion"}])
	assert proc.returncode == 0, proc.stderr
	# Both ledger entries sat within 3 lines of the ruling: the round is clean.
	assert "action=sticky_demoted findings=2" in proc.stdout
	assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env
	assert calls == []


def test_handoff_keeps_findings_when_the_file_changed_since_the_ruled_head():
	"""Security follow-up #6062: an invalid ruling only demotes while the code it judged is unchanged."""
	# PR #6069 review: a change in another file (a caller) counts too.
	for change_path in ("scripts/a.sh", "scripts/caller.sh"):
		with tempfile.TemporaryDirectory() as td:
			proc, _calls, _posts, github_env = _run_handoff_with_rulings(
				Path(td), [{"file": "scripts/a.sh", "line": 12, "ruling": "invalid", "claim": "unquoted expansion"}], change_after=True, change_path=change_path
			)
		assert proc.returncode == 0, proc.stderr
		assert "sticky_demoted" not in proc.stdout, change_path
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert "kind=findings findings=2" in proc.stdout


def test_handoff_keeps_findings_outside_the_sticky_window_or_with_the_switch_off():
	for rulings, enabled in (([{"file": "scripts/a.sh", "line": 14, "ruling": "invalid", "claim": "unquoted expansion"}], "true"), ([{"file": "scripts/a.sh", "line": 10, "ruling": "invalid", "claim": "unquoted expansion"}], "false"), ([{"file": "scripts/a.sh", "line": 10, "ruling": "invalid", "claim": "a different defect"}], "true")):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_handoff_with_rulings(Path(td), rulings, enabled=enabled)
		assert proc.returncode == 0, proc.stderr
		assert "sticky_demoted" not in proc.stdout
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
		assert "kind=findings findings=2" in proc.stdout


def test_findings_handoff_offers_the_judge_dispatch():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, _env = _run_handoff(Path(td), ledger=LEDGER_WITH_FINDINGS)
	body = calls[0]["payload"]["body"]
	assert f"claude_fixer_judge_head={HEAD}" in body
	assert f"<!-- ai:claude-fixer-rejection:v1 head={HEAD} round=2 -->" in body
	# The marker is only quoted inline; no line of the hand-off is a bare rejection marker.
	assert f"<!-- ai:claude-fixer-rejection:v1 head={HEAD} round=2 -->" not in body.splitlines()


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
	requests a partial finalize before the summariser, so no ledger is written
	and the panel floor does not apply to it (the hand-off fails closed). The
	README, agents.md and the changelog say so.
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


def test_budget_skip_only_pass_without_ledger_hands_off_fail_closed():
	"""Q47: A. The budget-only pass writes no ledger, so the hand-off step sees
	ledger=missing and hands the round to Claude (kind=findings) even though
	the panel floor is met. No auto-merge flag is exported.
	"""
	with tempfile.TemporaryDirectory() as td:
		proc, calls, posts, github_env = _run_handoff(Path(td), ledger=None, panel_statuses=["success"] * 3 + ["skipped_budget"], reviewers_successful="3")
	assert proc.returncode == 0, proc.stderr
	assert "kind=findings" in calls[0]["payload"]["body"]
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
