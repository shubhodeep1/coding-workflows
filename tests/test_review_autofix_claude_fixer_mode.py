#!/usr/bin/env python3
"""Contract for review_autofix.yml Claude-fixer mode.

Every PR-backed `claude/*` head (/implement-plan-claude stages and any
session's PR under CLAUDE.md §26) keeps the reviewer panel, but the GPT editor, conflict resolver, push / re-trigger tail
and review-blocked judge do not run: the findings (or the pre-review
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
import shutil
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
	# Q20: the review-blocked judge would have GPT edit the PR.
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
	assert "CAN_PUSH" not in block
	assert "action=claude_fixer_handoff" in block


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


def _run_handoff(tmp: Path, *, ledger: str | None, check_context: str = "", pre_review_resolve: bool = False, unmerged: str = "", fresh_status: str = "ready", verification: bool = False, statuses: dict[str, str] | None = None, outputs: dict[str, str] | None = None, min_clean: str | None = None, extra_env: dict[str, str] | None = None):
	support = tmp / "support"
	support.mkdir()
	calls = tmp / "calls.jsonl"
	(support / "post_review_comment.sh").write_text(
		f"#!/usr/bin/env bash\necho post_review_comment \"$REVIEWER_CONSENSUS_FILE\" \"$PR_NUMBER\" >> {tmp / 'posts.log'}\n",
		encoding="utf-8",
	)
	(support / "collect_pr_check_runs_context.py").write_text(
		"import os\nfrom pathlib import Path\n"
		f"Path(os.environ['PR_CHECK_RUNS_CONTEXT_FILE']).write_text('PR_CHECK_RUNS_CONTEXT\\nhead_sha: {HEAD}\\ncollection_status: {fresh_status}\\ntotal_check_runs: 1\\nfailed_count: 0\\nincomplete_count: 0\\n')\n",
		encoding="utf-8",
	)
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(
		"#!/usr/bin/env python3\n"
		"import json, sys\n"
		"args = sys.argv[1:]\n"
		"payload = None\n"
		"if '--input' in args:\n"
		"\tpayload = json.load(open(args[args.index('--input') + 1]))\n"
		f"open({str(calls)!r}, 'a').write(json.dumps({{'args': args, 'payload': payload}}) + '\\n')\n",
		encoding="utf-8",
	)
	gh.chmod(0o755)
	ledger_path = tmp / "reviewer_consensus.txt"
	if ledger is not None:
		ledger_path.write_text(ledger, encoding="utf-8")
	checks_path = tmp / "checks.txt"
	checks_path.write_text(check_context, encoding="utf-8")
	github_env = tmp / "github_env"
	github_env.write_text("", encoding="utf-8")
	env = {
		**os.environ,
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
		"REVIEWERS_SUCCESSFUL": "2",
	}
	env.pop("PREVIOUS_REVIEWS_DIR", None)
	env.pop("CLAUDE_FIXER_MIN_CLEAN_REVIEWERS", None)
	if statuses is not None or outputs is not None:
		reviews = tmp / "previous_reviews"
		reviews.mkdir()
		for slug, status in (statuses or {}).items():
			(reviews / f"status_review_{slug}.txt").write_text(status + "\n", encoding="utf-8")
		# The runner's review_<slug>.txt; a terminally failed slot holds its failure line.
		for slug, output in (outputs or {}).items():
			(reviews / f"review_{slug}.txt").write_text(output + "\n", encoding="utf-8")
		env["PREVIOUS_REVIEWS_DIR"] = str(reviews)
	if statuses is not None:
		env["REVIEWERS_SUCCESSFUL"] = str(sum(1 for status in statuses.values() if status == "success"))
	if min_clean is not None:
		env["CLAUDE_FIXER_MIN_CLEAN_REVIEWERS"] = min_clean
	env.update(extra_env or {})
	proc = subprocess.run(["bash", "-c", f'source "{HANDOFF_SCRIPT}"'], env=env, capture_output=True, text=True)
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


def test_handoff_does_not_merge_without_fresh_ready_checks():
	for status in ("timeout", "disabled", "api_error"):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, fresh_status=status)
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
		assert len(calls) == 1


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


# ---- a failed reviewer slot is a missing vote, not a finding (issue #4835) ----

CLEAN_BODY = "(No findings reported.)"
FINDING_BODY = "- scripts/a.sh:10 | severity=high\n  PROBLEM: unquoted expansion"
SLOT_LIMIT_FAILURE = "Reviewer {model} failed after reaching the slot retryable-failure limit (3)."
PANEL = (
	"deepseek/deepseek-v4-pro",
	"google/gemini-3.1-flash-lite",
	"minimax/minimax-m3",
	"qwen/qwen3.7-plus",
	"x-ai/grok-4.20",
	"z-ai/glm-5.2",
)


def _slug(model: str) -> str:
	# review_run_reviewers.sh: safe_name="$(echo "${model}" | tr '/.:' '___')"
	return model.replace("/", "_").replace(".", "_").replace(":", "_")


def _ledger(blocks: list[tuple[str, str]]) -> str:
	parts = [
		"=== CONSENSUS FINDINGS ===\n(No findings reported.)\n=== END CONSENSUS FINDINGS ===\n",
		"=== CONSENSUS TASK GAPS ===\n(No task gaps reported.)\n=== END CONSENSUS TASK GAPS ===\n",
	]
	for slug, body in blocks:
		parts.append(f"=== FINDINGS FROM {slug} ===\n{body}\n=== END FINDINGS FROM {slug} ===\n")
	return "\n".join(parts)


def _panel(failed: tuple[str, ...] = (), findings: tuple[str, ...] = ()):
	"""Ledger blocks and runner status files for PANEL with the named slots failed or finding."""
	blocks = []
	statuses = {}
	for model in PANEL:
		slug = _slug(model)
		if model in failed:
			blocks.append((slug, SLOT_LIMIT_FAILURE.format(model=model)))
			statuses[slug] = "failed"
		elif model in findings:
			blocks.append((slug, FINDING_BODY))
			statuses[slug] = "success"
		else:
			blocks.append((slug, CLEAN_BODY))
			statuses[slug] = "success"
	return blocks, statuses


# The nine lens headings of prompts/review-reviewer-checklist.txt, in order.
CHECKLIST_LENSES = (
	"SECURITY & INPUT VALIDATION",
	"CORRECTNESS & LOGIC",
	"CONCURRENCY / RACES / IDEMPOTENCY",
	"ERROR PATHS & EDGE CASES",
	"PERFORMANCE & RESOURCE USE",
	"INDEX-CONTRACT / DB RULES",
	"NAMING / BACKWARD COMPATIBILITY",
	"IMPLICIT-EXECUTION & TRUST-BOUNDARY RISKS",
	"TASK COMPLETENESS / INTENT GAPS",
)


def _checklist_output(verdicts: dict[str, str] | None = None) -> str:
	"""A checklist-shaped reviewer output: NONE under every lens unless overridden."""
	verdicts = verdicts or {}
	return "\n\n".join(f"{lens}\n{verdicts.get(lens, 'NONE')}" for lens in CHECKLIST_LENSES)


# What a clean reviewer writes to review_<slug>.txt with the checklist on.
CLEAN_RUNNER_OUTPUT = _checklist_output()


def _runner_outputs(failed: tuple[str, ...]) -> dict[str, str]:
	"""The review_<slug>.txt files the runner writes for _panel: the failure line
	for each failed slot and a clean checklist verdict for every other slot
	(a clean vote needs its own runner output, issue #5114)."""
	return {
		_slug(model): SLOT_LIMIT_FAILURE.format(model=model) if model in failed else CLEAN_RUNNER_OUTPUT
		for model in PANEL
	}


def test_five_clean_one_failed_slot_is_clean():
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	with tempfile.TemporaryDirectory() as td:
		proc, calls, posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3",)))
	assert proc.returncode == 0, proc.stderr
	assert calls == [] and posts == ""
	assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env
	assert (
		f"CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS pr=42 head={HEAD} round=2 failed_slots=minimax_minimax-m3 clean_reviewers=5 min=5"
		in proc.stdout
	)
	assert "action=auto_merge" in proc.stdout


def test_every_retry_exhaustion_line_is_a_failed_slot():
	for line in (
		"Reviewer minimax/minimax-m3 failed after reaching the slot retryable-failure limit (3).",
		"Reviewer minimax/minimax-m3 failed after retryable failure recovery was exhausted.",
		"Reviewer minimax/minimax-m3 failed after 4 attempts.",
	):
		blocks, statuses = _panel(failed=("minimax/minimax-m3",))
		blocks = [(slug, line if slug == "minimax_minimax-m3" else body) for slug, body in blocks]
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs={**_runner_outputs(("minimax/minimax-m3",)), "minimax_minimax-m3": line})
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env, line
		assert calls == [], line


def test_clean_with_failed_slot_still_needs_fresh_ready_checks():
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3",)), fresh_status="timeout")
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert len(calls) == 1
	body = calls[0]["payload"]["body"]
	assert "Reviewer slots that failed (missing votes, not findings): `minimax_minimax-m3`." in body
	assert f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=2 -->" in body


def test_four_clean_two_failed_slots_hand_off_with_the_default_minimum():
	blocks, statuses = _panel(failed=("minimax/minimax-m3", "x-ai/grok-4.20"))
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3", "x-ai/grok-4.20")))
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert "CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS" not in proc.stdout
	assert len(calls) == 1
	body = calls[0]["payload"]["body"]
	assert "`minimax_minimax-m3`, `x-ai_grok-4_20`" in body
	assert "Completed clean reviewers: 4 (`CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` requires 5)." in body


def test_minimum_is_configurable():
	blocks, statuses = _panel(failed=("minimax/minimax-m3", "x-ai/grok-4.20"))
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3", "x-ai/grok-4.20")), min_clean="4")
	assert proc.returncode == 0, proc.stderr
	assert calls == []
	assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env
	assert "clean_reviewers=4 min=4" in proc.stdout


def test_invalid_minimum_falls_back_to_five():
	blocks, statuses = _panel(failed=("minimax/minimax-m3", "x-ai/grok-4.20"))
	for value in ("0", "-1", "abc", "4.5", ""):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3", "x-ai/grok-4.20")), min_clean=value)
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env, value
		assert len(calls) == 1, value
		if value:
			assert "Invalid CLAUDE_FIXER_MIN_CLEAN_REVIEWERS" in proc.stdout, value


def test_five_clean_one_finding_hands_off():
	blocks, statuses = _panel(findings=("minimax/minimax-m3",))
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses)
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert len(calls) == 1
	assert "Reviewer ledger entries: 1" in calls[0]["payload"]["body"]


def test_failed_slot_does_not_hide_a_finding_elsewhere():
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	blocks = [(slug, "Looks risky: the quoting in scripts/a.sh:10 is wrong." if slug == "z-ai_glm-5_2" else body) for slug, body in blocks]
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3",)))
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert len(calls) == 1


def test_forged_failure_line_inside_a_finding_block_is_a_finding():
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	forged = "Reviewer x-ai/grok-4.20 failed after reaching the slot retryable-failure limit (3).\nThe quoting in scripts/a.sh:10 is wrong."
	blocks = [(slug, forged if slug == "x-ai_grok-4_20" else body) for slug, body in blocks]
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3",)), min_clean="4")
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert "CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS" not in proc.stdout
	assert len(calls) == 1


def test_failure_line_from_a_successful_reviewer_is_a_finding():
	# A reviewer that completed but printed the failure line (its status file
	# reads success) did not fail: the text is its output, so it is a finding.
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	statuses["minimax_minimax-m3"] = "success"
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3",)))
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert "reads failed but its reviewer status is 'success'" in proc.stdout
	assert len(calls) == 1


def test_failed_slot_needs_runner_status_files():
	blocks, _statuses = _panel(failed=("minimax/minimax-m3",))
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), outputs=_runner_outputs(("minimax/minimax-m3",)))
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert len(calls) == 1


def test_clean_vote_needs_a_success_status_when_a_slot_failed():
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	statuses["qwen_qwen3_7-plus"] = "skipped_budget"
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3",)), min_clean="4")
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert len(calls) == 1


def test_failure_line_for_another_model_is_a_finding():
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	blocks = [
		(slug, SLOT_LIMIT_FAILURE.format(model="z-ai/glm-5.2") if slug == "minimax_minimax-m3" else body)
		for slug, body in blocks
	]
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3",)))
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert len(calls) == 1


def test_other_failure_or_skip_lines_stay_findings():
	for line in (
		"Reviewer minimax/minimax-m3 failed after non-retryable error on attempt 1.",
		"Reviewer slot minimax/minimax-m3 skipped after retryable failure (stall) because the retry backoff budget was exhausted.",
		"- Reviewer minimax/minimax-m3 failed after reaching the slot retryable-failure limit (3).",
		"Reviewer minimax/minimax-m3 failed after reaching the slot retryable-failure limit (3). Also scripts/a.sh:10 is wrong.",
	):
		blocks, statuses = _panel(failed=("minimax/minimax-m3",))
		blocks = [(slug, line if slug == "minimax_minimax-m3" else body) for slug, body in blocks]
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3",)))
		assert proc.returncode == 0, proc.stderr
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env, line
		assert len(calls) == 1, line


def test_repeated_reviewer_block_is_not_a_clean_vote():
	blocks, statuses = _panel(failed=("minimax/minimax-m3", "x-ai/grok-4.20"))
	blocks.append(("z-ai_glm-5_2", CLEAN_BODY))
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3", "x-ai/grok-4.20")))
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert len(calls) == 1


def test_path_like_slug_never_reads_outside_the_reviews_dir():
	# The slug comes from model output. A clean block named
	# "x/../../outside" would resolve status_review_x/../../outside.txt to a
	# file outside the reviews dir that reads "success"; it must not count.
	blocks, statuses = _panel(failed=("minimax/minimax-m3", "x-ai/grok-4.20"))
	blocks.append(("x/../../outside", CLEAN_BODY))
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		(tmp / "previous_reviews" / "status_review_x").mkdir(parents=True)
		(tmp / "outside.txt").write_text("success\n", encoding="utf-8")
		reviews = tmp / "previous_reviews"
		for slug, status in statuses.items():
			(reviews / f"status_review_{slug}.txt").write_text(status + "\n", encoding="utf-8")
		for slug, output in _runner_outputs(("minimax/minimax-m3", "x-ai/grok-4.20")).items():
			(reviews / f"review_{slug}.txt").write_text(output + "\n", encoding="utf-8")
		proc, calls, _posts, github_env = _run_handoff(tmp, ledger=_ledger(blocks), min_clean="5", extra_env={"PREVIOUS_REVIEWS_DIR": str(reviews), "REVIEWERS_SUCCESSFUL": "4"})
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert "'x/../../outside' reads clean but its reviewer status is 'missing'" in proc.stdout
	assert len(calls) == 1


# ---- a failed slot's line must be the runner's own output line (issue #4885) ----

NON_RETRYABLE_FAILURE = "Reviewer minimax/minimax-m3 failed after non-retryable error on attempt 1."


def _assert_runner_line_mismatch_hands_off(proc, calls, github_env, state: str):
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert "CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS" not in proc.stdout
	assert f"'minimax_minimax-m3' reads failed but the reviewer runner's review_minimax_minimax-m3.txt is {state}, not that failure line" in proc.stdout
	assert len(calls) == 1
	body = calls[0]["payload"]["body"]
	assert f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=2 -->" in body
	# An unverified slot is never reported as a missing vote.
	assert "Reviewer slots that failed" not in body


def test_non_retryable_failure_relabelled_as_retry_exhaustion_is_not_a_missing_vote():
	# The runner writes `failed` to the status file for a non-retryable error
	# too; its output line says so. A ledger line claiming retry exhaustion for
	# that slot must not turn five clean votes into an auto-merge.
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs={**_runner_outputs(("minimax/minimax-m3",)), "minimax_minimax-m3": NON_RETRYABLE_FAILURE})
	_assert_runner_line_mismatch_hands_off(proc, calls, github_env, "different")


def test_failed_slot_without_runner_output_is_not_a_missing_vote():
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	with tempfile.TemporaryDirectory() as td:
		outputs = _runner_outputs(("minimax/minimax-m3",))
		del outputs["minimax_minimax-m3"]
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=outputs)
	_assert_runner_line_mismatch_hands_off(proc, calls, github_env, "missing")


def test_runner_output_with_more_than_the_failure_line_is_not_a_missing_vote():
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	line = SLOT_LIMIT_FAILURE.format(model="minimax/minimax-m3")
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs={**_runner_outputs(("minimax/minimax-m3",)), "minimax_minimax-m3": line + "\n- scripts/a.sh:10 | severity=high"})
	_assert_runner_line_mismatch_hands_off(proc, calls, github_env, "different")


def test_another_retry_exhaustion_line_than_the_runner_wrote_is_not_a_missing_vote():
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs={**_runner_outputs(("minimax/minimax-m3",)), "minimax_minimax-m3": "Reviewer minimax/minimax-m3 failed after 4 attempts."})
	_assert_runner_line_mismatch_hands_off(proc, calls, github_env, "different")


def test_empty_runner_output_is_reported_as_empty():
	# PR #4983 review round 1: a zero-byte or newline-only runner file is
	# rejected like any mismatch, but the warning names it empty.
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	for content in ("", "\n"):
		with tempfile.TemporaryDirectory() as td:
			tmp = Path(td)
			reviews = tmp / "previous_reviews"
			reviews.mkdir()
			for slug, status in statuses.items():
				(reviews / f"status_review_{slug}.txt").write_text(status + "\n", encoding="utf-8")
			for slug, output in _runner_outputs(("minimax/minimax-m3",)).items():
				(reviews / f"review_{slug}.txt").write_text(output + "\n", encoding="utf-8")
			(reviews / "review_minimax_minimax-m3.txt").write_text(content, encoding="utf-8")
			proc, calls, _posts, github_env = _run_handoff(tmp, ledger=_ledger(blocks), extra_env={"PREVIOUS_REVIEWS_DIR": str(reviews), "REVIEWERS_SUCCESSFUL": "5"})
		_assert_runner_line_mismatch_hands_off(proc, calls, github_env, "empty")


def test_unreadable_runner_output_is_reported_as_unreadable():
	# A runner file that exists but cannot be read is rejected and named
	# unreadable, not empty. A cat wrapper fails for that one file, because a
	# root test run can read a mode-000 file.
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	real_cat = shutil.which("cat")
	assert real_cat
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		fake_bin = tmp / "fake_bin"
		fake_bin.mkdir()
		fake_cat = fake_bin / "cat"
		fake_cat.write_text(
			"#!/usr/bin/env bash\n"
			'[ "${1##*/}" = "review_minimax_minimax-m3.txt" ] && exit 1\n'
			f'exec {real_cat} "$@"\n',
			encoding="utf-8",
		)
		fake_cat.chmod(0o755)
		path = os.pathsep.join((str(fake_bin), str(tmp / "bin"), os.environ.get("PATH", "")))
		proc, calls, _posts, github_env = _run_handoff(tmp, ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3",)), extra_env={"PATH": path})
	_assert_runner_line_mismatch_hands_off(proc, calls, github_env, "unreadable")


def test_one_unverified_failed_slot_keeps_the_ledger_from_being_clean():
	failed = ("minimax/minimax-m3", "x-ai/grok-4.20")
	blocks, statuses = _panel(failed=failed)
	outputs = _runner_outputs(failed)
	outputs["minimax_minimax-m3"] = NON_RETRYABLE_FAILURE
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=outputs, min_clean="4")
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert "CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS" not in proc.stdout
	assert "'minimax_minimax-m3' reads failed but the reviewer runner's review_minimax_minimax-m3.txt is different" in proc.stdout
	assert len(calls) == 1
	# Only the verified slot is reported as a missing vote.
	assert "Reviewer slots that failed (missing votes, not findings): `x-ai_grok-4_20`." in calls[0]["payload"]["body"]


def test_repeated_failed_block_is_not_a_missing_vote():
	# Failed records carry the line after the slug; the repeat check still
	# compares slugs only.
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	blocks.append(("minimax_minimax-m3", SLOT_LIMIT_FAILURE.format(model="minimax/minimax-m3")))
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=_runner_outputs(("minimax/minimax-m3",)))
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert "ledger repeats a reviewer block" in proc.stdout
	assert len(calls) == 1


def test_all_clean_ledger_without_failed_slots_keeps_todays_rule():
	# No failed slot: no minimum and no status files needed (AD-1).
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, min_clean="5")
	assert proc.returncode == 0, proc.stderr
	assert calls == []
	assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env
	assert "CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS" not in proc.stdout


# ---- a clean vote must come from the reviewer's own runner output (issue #5114) ----

GLM = "z-ai_glm-5_2"
LABELLED_FINDING = (
	"File: scripts/a.sh\n"
	"Line or code reference: 10\n"
	"Problem: unquoted expansion\n"
	"Why it fails at runtime: word splitting on paths with spaces\n"
	"SEVERITY: MAJOR\n"
	"ISSUE_CONFIDENCE: 4"
)
TASK_GAP = (
	"Requirement: the issue asks for a timeout on the fetch\n"
	"Expected change site: scripts/fetch.sh\n"
	"Evidence of absence: no timeout flag in the diff\n"
	"SEVERITY: MAJOR\n"
	"ISSUE_CONFIDENCE: 4"
)


def _run_one_failed_slot(td: str, glm_output: str | None, *, extra_env: dict[str, str] | None = None):
	"""Five clean ledger blocks and one verified failed slot (minimax); GLM's runner output replaced (None deletes it)."""
	blocks, statuses = _panel(failed=("minimax/minimax-m3",))
	outputs = _runner_outputs(("minimax/minimax-m3",))
	if glm_output is None:
		del outputs[GLM]
	else:
		outputs[GLM] = glm_output
	return _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=outputs, extra_env=extra_env)


def _assert_clean_vote_rejected(proc, calls, github_env, reason: str):
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
	assert "CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS" not in proc.stdout
	assert (
		f"Claude-fixer ledger block '{GLM}' reads clean but the reviewer runner's review_{GLM}.txt is not an unambiguous no-findings result ({reason}); the ledger is not clean."
		in proc.stdout
	)
	assert len(calls) == 1
	assert f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=2 -->" in calls[0]["payload"]["body"]


def test_clean_ledger_block_over_a_reviewer_finding_is_not_a_clean_vote():
	# The issue's exploit: one genuine failed slot, and the consensus ledger
	# labels a successful reviewer's block clean although that reviewer's own
	# output reports a finding. Five success statuses must not auto-merge.
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_one_failed_slot(td, _checklist_output({"CORRECTNESS & LOGIC": LABELLED_FINDING}))
	_assert_clean_vote_rejected(proc, calls, github_env, "reports a finding or task gap")
	# The failed slot itself is still verified and reported as a missing vote.
	assert "Reviewer slots that failed (missing votes, not findings): `minimax_minimax-m3`." in calls[0]["payload"]["body"]


def test_clean_ledger_block_over_a_reviewer_task_gap_is_not_a_clean_vote():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_one_failed_slot(td, _checklist_output({"TASK COMPLETENESS / INTENT GAPS": TASK_GAP}))
	_assert_clean_vote_rejected(proc, calls, github_env, "reports a finding or task gap")


def test_markdown_decorated_finding_labels_are_findings():
	for finding in (
		"- **File:** scripts/a.sh",
		"**Problem**: unquoted expansion",
		"1. File: scripts/a.sh",
		"> SEVERITY: BLOCKER",
		"  `Requirement:` a timeout",
		"### Evidence of absence: no hunk in scripts/fetch.sh",
		"issue_confidence: 3",
	):
		# A NONE verdict elsewhere does not outweigh a finding field.
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_one_failed_slot(td, "NONE\n" + finding)
		_assert_clean_vote_rejected(proc, calls, github_env, "reports a finding or task gap")


def test_clean_ledger_block_needs_a_runner_output():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_one_failed_slot(td, None)
	_assert_clean_vote_rejected(proc, calls, github_env, "missing")


def test_empty_clean_runner_output_is_not_a_clean_vote():
	for content in ("", "\n\n", "   "):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_one_failed_slot(td, content)
		_assert_clean_vote_rejected(proc, calls, github_env, "empty")


def test_unreadable_clean_runner_output_is_not_a_clean_vote():
	real_cat = shutil.which("cat")
	assert real_cat
	with tempfile.TemporaryDirectory() as td:
		fake_bin = Path(td) / "fake_bin"
		fake_bin.mkdir()
		fake_cat = fake_bin / "cat"
		fake_cat.write_text(
			"#!/usr/bin/env bash\n"
			f'[ "${{1##*/}}" = "review_{GLM}.txt" ] && exit 1\n'
			f'exec {real_cat} "$@"\n',
			encoding="utf-8",
		)
		fake_cat.chmod(0o755)
		path = os.pathsep.join((str(fake_bin), str(Path(td) / "bin"), os.environ.get("PATH", "")))
		proc, calls, _posts, github_env = _run_one_failed_slot(td, CLEAN_RUNNER_OUTPUT, extra_env={"PATH": path})
	_assert_clean_vote_rejected(proc, calls, github_env, "unreadable")


def test_runner_output_without_a_none_verdict_is_not_a_clean_vote():
	# Real outputs seen on 2026-09-29: a bare remark and a narration line.
	for content in ("No changes are needed.", "Reading the current state of both files to verify parity.", "None."):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_one_failed_slot(td, content)
		_assert_clean_vote_rejected(proc, calls, github_env, "has no NONE verdict")


def test_checklist_lens_without_a_none_verdict_is_not_a_clean_vote():
	prose_lens = _checklist_output({"TASK COMPLETENESS / INTENT GAPS": "The three pass-1 claims are false positives."})
	missing_lens = "\n\n".join(f"{lens}\nNONE" for lens in CHECKLIST_LENSES[:-1])
	trailing_heading = "\n\n".join(f"{lens}\nNONE" for lens in CHECKLIST_LENSES[:-1]) + f"\n\n{CHECKLIST_LENSES[-1]}\n"
	two_headings = CLEAN_RUNNER_OUTPUT.replace("SECURITY & INPUT VALIDATION\nNONE", "SECURITY & INPUT VALIDATION\nCORRECTNESS & LOGIC\nNONE", 1)
	for content in (prose_lens, missing_lens, trailing_heading, two_headings):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_one_failed_slot(td, content)
		_assert_clean_vote_rejected(proc, calls, github_env, "leaves a checklist lens without a NONE verdict")


def _assert_clean_with_one_failed_slot(proc, calls, github_env):
	assert proc.returncode == 0, proc.stderr
	assert calls == []
	assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env
	assert "failed_slots=minimax_minimax-m3 clean_reviewers=5 min=5" in proc.stdout


def test_clean_votes_accept_the_reviewer_contract_shapes():
	lenses = "\n\n".join(f"{lens}\nNONE" for lens in CHECKLIST_LENSES)
	for content in (
		# Checklist verdicts with narration before and a summary after (seen on real runs).
		"Reading the actual files to verify the pass-1 claims.\n" + lenses + "\n\nThe diff implements all four findings correctly.",
		# Markdown headings, CRLF line ends, and indentation.
		"\r\n".join(f"## {lens}\r\n  NONE  " for lens in CHECKLIST_LENSES),
		# Checklist off: the reviewer contract's bare NONE (prompts/_nag_reminders.txt).
		"NONE",
	):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_one_failed_slot(td, content)
		_assert_clean_with_one_failed_slot(proc, calls, github_env)


def test_clean_runner_output_check_behaves_the_same_under_mawk_and_gawk():
	cases = {
		CLEAN_RUNNER_OUTPUT: None,
		_checklist_output({"CORRECTNESS & LOGIC": "- **File:** scripts/a.sh"}): "reports a finding or task gap",
		_checklist_output({"TASK COMPLETENESS / INTENT GAPS": "prose"}): "leaves a checklist lens without a NONE verdict",
		"No changes are needed.": "has no NONE verdict",
	}
	for awk_name in ("mawk", "gawk"):
		awk_path = shutil.which(awk_name)
		if awk_path is None:
			pytest.skip(f"{awk_name} not installed")
		for content, reason in cases.items():
			with tempfile.TemporaryDirectory() as td:
				shim = Path(td) / "awk_shim"
				shim.mkdir()
				(shim / "awk").symlink_to(awk_path)
				path = os.pathsep.join((str(shim), str(Path(td) / "bin"), os.environ.get("PATH", "")))
				proc, calls, _posts, github_env = _run_one_failed_slot(td, content, extra_env={"PATH": path})
			if reason is None:
				_assert_clean_with_one_failed_slot(proc, calls, github_env)
			else:
				_assert_clean_vote_rejected(proc, calls, github_env, reason)


def test_lens_headings_match_the_reviewer_checklist_prompt():
	# The script's heading list must be the prompt's nine lens headings, in
	# order: a renamed lens would otherwise silently loosen the lens rule.
	script = HANDOFF_SCRIPT.read_text(encoding="utf-8")
	match = re.search(r'^claude_fixer_checklist_lens_headings="([^"]*)"$', script, re.M)
	assert match, "claude_fixer_checklist_lens_headings not found"
	assert tuple(match.group(1).split("|")) == CHECKLIST_LENSES
	prompt = (REPO_ROOT / "prompts" / "review-reviewer-checklist.txt").read_text(encoding="utf-8")
	prompt_headings = tuple(
		line
		for line in prompt.splitlines()
		if re.fullmatch(r"[A-Z][A-Z &/-]+", line) and line not in ("REVIEWER CHECKLIST", "WHAT TO FLAG", "WHAT NOT TO FLAG")
	)
	assert prompt_headings == CHECKLIST_LENSES
	assert "nine lens headings" in prompt
	assert "or the literal word NONE" in prompt


def test_ledger_without_failed_slots_does_not_read_runner_outputs():
	# AD-1: only the failed-slot path verifies clean votes against the runner
	# output; a ledger with no failed slot keeps the every-block-clean rule.
	blocks, statuses = _panel()
	outputs = {_slug(model): "No changes are needed." for model in PANEL}
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=_ledger(blocks), statuses=statuses, outputs=outputs)
	assert proc.returncode == 0, proc.stderr
	assert calls == []
	assert "CLAUDE_FIXER_ZERO_FINDINGS=true" in github_env
	assert "not an unambiguous no-findings result" not in proc.stdout


def test_workflow_wires_the_minimum_clean_reviewers_variable():
	steps = _steps(_load(REVIEW_AUTOFIX_WORKFLOW_PATH), "codex-agent")
	env = steps["Hand review round to Claude session (Claude-fixer mode)"]["env"]
	assert env["CLAUDE_FIXER_MIN_CLEAN_REVIEWERS"] == "${{ vars.CLAUDE_FIXER_MIN_CLEAN_REVIEWERS || '5' }}"


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
