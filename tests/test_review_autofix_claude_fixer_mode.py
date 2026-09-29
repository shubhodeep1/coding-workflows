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
import importlib.util
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
# The base the review ran against (issue #5147): the phase PR into its project branch.
BASE_REF = "claude/implement-plan-demo"
BASE_SHA = "e" * 40
BASE_REF_DIGEST = hashlib.sha256(BASE_REF.encode("utf-8")).hexdigest()

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


def _run_handoff(tmp: Path, *, ledger: str | None, check_context: str = "", pre_review_resolve: bool = False, unmerged: str = "", fresh_status: str = "ready", verification: bool = False, fresh_context: str | None = None, payload: dict | None = None):
	support = tmp / "support"
	support.mkdir()
	# PR_PAYLOAD_FILE: the review run's REST PR snapshot (its base binds a pending-checks marker).
	payload_path = tmp / "pr_payload.json"
	payload_path.write_text(json.dumps({"head": {"sha": HEAD, "ref": "claude/implement-plan-demo-phase-1"},
		"base": {"ref": BASE_REF, "sha": BASE_SHA}} if payload is None else payload), encoding="utf-8")
	calls = tmp / "calls.jsonl"
	(support / "post_review_comment.sh").write_text(
		f"#!/usr/bin/env bash\necho post_review_comment \"$REVIEWER_CONSENSUS_FILE\" \"$PR_NUMBER\" >> {tmp / 'posts.log'}\n",
		encoding="utf-8",
	)
	if fresh_context is None:
		fresh_context = f"PR_CHECK_RUNS_CONTEXT\nhead_sha: {HEAD}\ncollection_status: {fresh_status}\ntotal_check_runs: 1\nfailed_count: 0\nincomplete_count: 0\n"
	(support / "collect_pr_check_runs_context.py").write_text(
		"import os\nfrom pathlib import Path\n"
		f"Path(os.environ['PR_CHECK_RUNS_CONTEXT_FILE']).write_text({fresh_context!r})\n",
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
		"PR_PAYLOAD_FILE": str(payload_path),
	}
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
		"pr": {"state": "open", "merged": False, "head": {"ref": head_ref, "sha": HEAD}, "base": {"ref": BASE_REF, "sha": BASE_SHA}, "labels": [], "additions": 400, "deletions": 50, "mergeable": True, "mergeable_state": "clean", "title": "Demo — phase 1/2: x", "body": "Refs #1", **(pr_overrides or {})},
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


# ---- issue #4900: a clean review that finishes before CI ----

def _pending_context(status: str = "timeout", *, head: str = HEAD, total: int = 3, failed: int = 0, incomplete: int = 1, extra: str = "") -> str:
	body = f"PR_CHECK_RUNS_CONTEXT\nhead_sha: {head}\ncollection_status: {status}\ntotal_check_runs: {total}\nfailed_count: {failed}\nincomplete_count: {incomplete}\n\n"
	for index in range(incomplete):
		body += f"incomplete[{index}].name: ci / lint{'' if index == 0 else f'-{index}'}\nincomplete[{index}].status: in_progress\n\n"
	return body + extra


PENDING_V1 = f"<!-- ai:claude-fixer-pending-checks:v1 head={HEAD} round=1 ledger={DIGEST} -->"
PENDING_V2 = f"<!-- ai:claude-fixer-pending-checks:v2 head={HEAD} round=1 ledger={DIGEST} base_sha={BASE_SHA} base_ref_sha256={BASE_REF_DIGEST} -->"
PENDING = PENDING_V1 + "\n" + PENDING_V2


# The hand-off step's run line; the gate (like find_pending_marker) only
# counts a pending-checks comment that links this repository's run.
PENDING_RUN_LINE = f"Reviewed head: `{HEAD}` ([workflow run](https://github.com/o/r/actions/runs/7))."


def _pending_comment(body: str = PENDING, author: str = AUTHOR) -> dict:
	return {"body": "## Review round 1: clean review, waiting for check runs\n\n" + PENDING_RUN_LINE + "\n\n" + body, "author_login": author,
		"author_type": "User", "author_association": "OWNER"}


def test_handoff_clean_review_with_running_checks_posts_pending_checks_not_findings():
	"""PR #4869: reviewers clean, CI `lint` still running when the snapshot times out."""
	for status in ("timeout", "ready"):
		with tempfile.TemporaryDirectory() as td:
			proc, calls, posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, fresh_context=_pending_context(status))
		assert proc.returncode == 0, proc.stderr
		assert posts == "", "no ledger chunks for a clean review"
		assert len(calls) == 1
		body = calls[0]["payload"]["body"]
		assert body.startswith("## Review round 2: clean review, waiting for check runs\n")
		digest = hashlib.sha256(LEDGER_EMPTY.encode()).hexdigest()
		assert f"<!-- ai:claude-fixer-pending-checks:v1 head={HEAD} round=2 ledger={digest} -->" in body.splitlines()
		# Issue #5147: bound to the base the review ran against.
		assert (f"<!-- ai:claude-fixer-pending-checks:v2 head={HEAD} round=2 ledger={digest} "
			f"base_sha={BASE_SHA} base_ref_sha256={BASE_REF_DIGEST} -->") in body.splitlines()
		assert f"Reviewed base: `{BASE_REF}` at `{BASE_SHA}`." in body.splitlines()
		assert f"base_sha={BASE_SHA}" in proc.stdout
		assert f"Reviewed head: `{HEAD}` ([workflow run](https://github.com/o/r/actions/runs/99))." in body.splitlines()
		assert "`ci / lint`" in body
		assert "ai:claude-fixer-handoff" not in body
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env
		assert "CLAUDE_FIXER_VERIFICATION_FAILED" not in github_env
		assert "kind=pending-checks" in proc.stdout


def test_handoff_verification_run_with_running_checks_pends_instead_of_blocking():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, fresh_context=_pending_context(), verification=True)
	assert proc.returncode == 0, proc.stderr
	assert "ai:claude-fixer-pending-checks:v1" in calls[0]["payload"]["body"]
	assert "CLAUDE_FIXER_VERIFICATION_FAILED" not in github_env


def test_handoff_pending_checks_keeps_every_fail_closed_rule():
	cases = {
		"failed check": _pending_context(failed=1, extra="failed[0].name: ci / test\n"),
		"failed entry without count": _pending_context(extra="failed[0].name: ci / test\n"),
		"other head": _pending_context(head="d" * 40),
		"api error": _pending_context("api_error"),
		"disabled": _pending_context("disabled"),
		"no check runs": _pending_context(total=0),
		"no incomplete runs": _pending_context("timeout", incomplete=0),
		"missing header": _pending_context().replace("PR_CHECK_RUNS_CONTEXT\n", "", 1),
	}
	for label, context in cases.items():
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, fresh_context=context)
		assert proc.returncode == 0, (label, proc.stderr)
		body = calls[0]["payload"]["body"]
		assert "ai:claude-fixer-pending-checks" not in body, label
		assert "kind=findings" in body, label
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env, label


def test_handoff_findings_with_running_checks_still_hand_off():
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, _env = _run_handoff(Path(td), ledger=LEDGER_WITH_FINDINGS, fresh_context=_pending_context())
	assert proc.returncode == 0, proc.stderr
	assert "kind=findings" in calls[0]["payload"]["body"]
	assert "ai:claude-fixer-pending-checks" not in calls[0]["payload"]["body"]


def test_handoff_review_time_failed_check_is_not_pending():
	context = "failed[0].name: ci / lint\nfailed[0].status: completed\n"
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, _env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, check_context=context, fresh_context=_pending_context())
	assert proc.returncode == 0, proc.stderr
	assert "kind=findings" in calls[0]["payload"]["body"]


def test_gate_skips_dispatch_rerun_on_a_pending_checks_head():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_pending_comment()])
	assert proc.returncode == 0, proc.stderr
	assert out["should_run"] == "false" and out["skip_reason"] == "claude_fixer_pending_checks"
	assert "AUTOFIX_GATE_SKIP reason=claude_fixer_pending_checks pr=42" in proc.stdout


def test_gate_pending_checks_skip_needs_the_workflow_marker_for_this_head():
	cases = {
		"untrusted author": [_pending_comment(author="someone")],
		"other head": [_pending_comment(PENDING.replace(HEAD, "d" * 40))],
		"marker without the issued header": [{**_pending_comment(), "body": PENDING}],
		"quoted marker": [_pending_comment("\n".join("> " + line for line in PENDING.splitlines()))],
		# Same round contract as scripts/claude_fixer_pending_checks.py: rounds start at 1.
		"round zero": [{**_pending_comment(), "body": "## Review round 0: clean review, waiting for check runs\n"
			+ PENDING.replace(" round=1 ", " round=0 ")}],
	}
	for label, comments in cases.items():
		with tempfile.TemporaryDirectory() as td:
			proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=comments)
		assert proc.returncode == 0, (label, proc.stderr)
		assert out["should_run"] == "true", label


def test_gate_pull_request_event_on_a_pending_checks_head_is_not_skipped():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_pending_comment()], event_name="pull_request")
	assert proc.returncode == 0, proc.stderr
	assert out["skip_reason"] != "claude_fixer_pending_checks"


# ---- issue #5147: the pending-checks marker is bound to the reviewed base ----

def test_handoff_without_a_payload_base_hands_off_instead_of_pending():
	cases = {
		"no payload file content": {},
		"base missing": {"head": {"sha": HEAD}},
		"base sha missing": {"base": {"ref": BASE_REF}},
		"base sha not hex": {"base": {"ref": BASE_REF, "sha": "E" * 40}},
		"base ref empty": {"base": {"ref": "", "sha": BASE_SHA}},
		"base not an object": {"base": "main"},
	}
	for label, payload in cases.items():
		with tempfile.TemporaryDirectory() as td:
			proc, calls, _posts, github_env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, fresh_context=_pending_context(), payload=payload)
		assert proc.returncode == 0, (label, proc.stderr)
		body = calls[0]["payload"]["body"]
		assert "ai:claude-fixer-pending-checks" not in body, label
		assert "kind=findings" in body, label
		assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env, label
		assert "pending-checks marker disabled: the review run's PR snapshot has no valid base" in proc.stdout, label


def test_handoff_binds_any_valid_ref_name_by_digest():
	"""A ref may contain characters that would end the HTML comment; only its sha256 enters the marker."""
	ref = "claude/odd-->name"
	digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()
	with tempfile.TemporaryDirectory() as td:
		proc, calls, _posts, _env = _run_handoff(Path(td), ledger=LEDGER_EMPTY, fresh_context=_pending_context(),
			payload={"base": {"ref": ref, "sha": BASE_SHA}})
	assert proc.returncode == 0, proc.stderr
	lines = calls[0]["payload"]["body"].splitlines()
	marker = next(line for line in lines if line.startswith("<!-- ai:claude-fixer-pending-checks:v2 "))
	assert marker.endswith(f" base_sha={BASE_SHA} base_ref_sha256={digest} -->") and ref not in marker


def test_gate_pr_read_carries_the_base_binding_fields():
	gate_run = _steps(WORKFLOW, "gate")["Evaluate review gate"]["run"]
	assert 'base_ref: (.base.ref // ""), base_sha: (.base.sha // "")' in gate_run
	# The run script is dedented by YAML, so the function closes at column 0.
	helper = gate_run.split("gate_claude_pending_checks_on_head()", 1)[1].split("\n}\n", 1)[0]
	assert "gate_marker_comments_file" in helper and "Terminal same-head skip" not in helper
	# The v1 line only selects the latest comment (as find_pending_marker
	# does); the skip itself needs that comment's bound v2 line.
	assert "pending-checks:v2 head=" in helper and "max_by(.id)" in helper


@pytest.mark.parametrize("label, pr_base, comments", [
	# The finding's exploit: clean review into the project branch, then the
	# author retargets the PR to main without pushing (same head).
	("retargeted to main", {"ref": "main", "sha": "f" * 40}, [_pending_comment()]),
	("retargeted, base sha unchanged", {"ref": "main", "sha": BASE_SHA}, [_pending_comment()]),
	("same ref, base sha changed", {"ref": BASE_REF, "sha": "f" * 40}, [_pending_comment()]),
	("PR base without a sha", {"ref": BASE_REF}, [_pending_comment()]),
	("PR base without a ref", {"sha": BASE_SHA}, [_pending_comment()]),
	("marker without a base binding", {"ref": BASE_REF, "sha": BASE_SHA}, [_pending_comment(PENDING_V1)]),
	("binding for another head", {"ref": BASE_REF, "sha": BASE_SHA},
		[_pending_comment(PENDING_V1 + "\n" + PENDING_V2.replace(f"head={HEAD}", "head=" + "d" * 40))]),
])
def test_gate_reviews_again_when_the_base_is_not_the_reviewed_one(label, pr_base, comments):
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=comments, pr_overrides={"base": pr_base})
	assert proc.returncode == 0, (label, proc.stderr)
	assert out["should_run"] == "true", (label, out)
	assert "reason=claude_fixer_pending_checks" not in proc.stdout, label


def test_gate_skips_again_once_a_fresh_review_binds_the_new_base():
	new_base = {"ref": "main", "sha": "f" * 40}
	fresh = PENDING_V1 + "\n" + (f"<!-- ai:claude-fixer-pending-checks:v2 head={HEAD} round=1 ledger={DIGEST} base_sha={'f' * 40} "
		f"base_ref_sha256={hashlib.sha256(b'main').hexdigest()} -->")
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_pending_comment(), _pending_comment(fresh)], pr_overrides={"base": new_base})
	assert proc.returncode == 0, proc.stderr
	assert out["should_run"] == "false" and out["skip_reason"] == "claude_fixer_pending_checks"


MAIN_BINDING = (f"<!-- ai:claude-fixer-pending-checks:v2 head={HEAD} round=1 ledger={DIGEST} base_sha={'f' * 40} "
	f"base_ref_sha256={hashlib.sha256(b'main').hexdigest()} -->")


@pytest.mark.parametrize("label, comments", [
	# Clean review of the project branch, retarget to main, clean review of
	# main, retarget back (base sha unchanged). The sweep evaluates the
	# latest comment (bound to main) and refuses; the gate must not skip on
	# the older one, or the PR is neither merged nor reviewed until a push.
	("retargeted back after a fresh review", [_pending_comment(), _pending_comment(PENDING_V1 + "\n" + MAIN_BINDING)]),
	# A later comment without a binding is `base_unbound` for the sweep.
	("newer comment without a binding", [_pending_comment(), _pending_comment(PENDING_V1)]),
	# The sweep only trusts a v2 line with the v1 line's round and ledger.
	("binding for another round", [_pending_comment(PENDING_V1 + "\n" + PENDING_V2.replace(" round=1 ", " round=2 "))]),
	("binding for another ledger", [_pending_comment(PENDING_V1 + "\n" + PENDING_V2.replace(f"ledger={DIGEST}", "ledger=" + "b" * 64))]),
	("two binding lines", [_pending_comment(PENDING + "\n" + MAIN_BINDING)]),
])
def test_gate_follows_the_latest_pending_checks_comment_like_the_sweep(label, comments):
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=comments)
	assert proc.returncode == 0, (label, proc.stderr)
	assert out["should_run"] == "true", (label, out)
	assert "reason=claude_fixer_pending_checks" not in proc.stdout, label


def test_gate_skips_on_the_latest_comment_when_an_older_one_is_bound_elsewhere():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF,
			comments=[_pending_comment(PENDING_V1 + "\n" + MAIN_BINDING), _pending_comment()])
	assert proc.returncode == 0, proc.stderr
	assert out["should_run"] == "false" and out["skip_reason"] == "claude_fixer_pending_checks"


def _load_pending_checks_module():
	spec = importlib.util.spec_from_file_location("claude_fixer_pending_checks_for_gate_parity", REPO_ROOT / "scripts" / "claude_fixer_pending_checks.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _pending_body(sep: str = "\n", binding: str = PENDING_V2, run_line: str = PENDING_RUN_LINE, v1_lines: tuple[str, ...] = (PENDING_V1,), lead: str = "") -> str:
	lines = [lead + "## Review round 1: clean review, waiting for check runs", "", run_line, "", *v1_lines]
	if binding:
		lines.append(binding)
	return sep.join(lines) + sep


OTHER_HEAD_V1 = PENDING_V1.replace(f"head={HEAD}", "head=" + "d" * 40)


@pytest.mark.parametrize("label, bodies, skip", [
	("LF comment bound to the current base", [_pending_body()], True),
	# PR #5213 review round 1: a CRLF body (a web-UI edit) is one the sweep's
	# splitlines() accepts, so the gate must read it too.
	("CRLF comment bound to the current base", [_pending_body("\r\n")], True),
	("newer CRLF comment bound to main", [_pending_body(), _pending_body("\r\n", MAIN_BINDING)], False),
	("newer U+2028 comment bound to main", [_pending_body(), _pending_body(" ", MAIN_BINDING)], False),
	("newer lone-CR comment bound to main", [_pending_body(), _pending_body("\r", MAIN_BINDING)], False),
	# A comment the sweep does not count never shadows the one it does.
	("newer comment without a run line", [_pending_body(binding=MAIN_BINDING), _pending_body(run_line="")], False),
	("run line for another repository", [_pending_body(run_line=PENDING_RUN_LINE.replace("/o/r/", "/o/other/"))], False),
	("two run lines", [_pending_body(run_line=PENDING_RUN_LINE + "\n" + PENDING_RUN_LINE)], False),
	("header not on the first line", [_pending_body(lead="\n")], False),
	("a second v1 line for another head", [_pending_body(v1_lines=(OTHER_HEAD_V1, PENDING_V1))], False),
	("v1 line for another head only", [_pending_body(v1_lines=(OTHER_HEAD_V1,))], False),
	("newer comment without a binding", [_pending_body(), _pending_body(binding="")], False),
])
def test_gate_selects_the_same_pending_checks_comment_as_the_sweep(label, bodies, skip):
	"""The gate's jq and find_pending_marker must agree on the latest comment and its binding."""
	pending_checks = _load_pending_checks_module()
	marker = pending_checks.find_pending_marker(
		[{"id": i, "user": {"login": AUTHOR, "type": "User"}, "body": body} for i, body in enumerate(bodies, 1)], "o/r", HEAD, AUTHOR)
	sweep_bound_here = bool(marker) and marker["base_sha"] == BASE_SHA and marker["base_ref_sha256"] == BASE_REF_DIGEST
	assert sweep_bound_here is skip, (label, marker)
	comments = [{"body": body, "author_login": AUTHOR, "author_type": "User", "author_association": "OWNER"} for body in bodies]
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=comments)
	assert proc.returncode == 0, (label, proc.stderr)
	if skip:
		assert out["should_run"] == "false" and out["skip_reason"] == "claude_fixer_pending_checks", (label, out)
	else:
		assert out["should_run"] == "true", (label, out)
		assert "reason=claude_fixer_pending_checks" not in proc.stdout, label


def test_gate_never_skips_on_a_pending_marker_for_an_unexpected_repository_slug():
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[_pending_comment()], extra_env={"REPOSITORY": "o/r)|.*"})
	assert proc.returncode == 0, proc.stderr
	assert "reason=claude_fixer_pending_checks" not in proc.stdout


@pytest.mark.parametrize("run_repo, skip", [("o.x/r", True), ("oax/r", False)])
def test_gate_matches_a_dotted_repository_slug_literally(run_repo, skip):
	"""A `.` in the repository name is not a regex wildcard in the run-line check."""
	comment = _pending_comment()
	comment["body"] = comment["body"].replace("/o/r/", f"/{run_repo}/")
	with tempfile.TemporaryDirectory() as td:
		proc, out = _run_gate(Path(td), head_ref=FIXER_REF, comments=[comment], extra_env={"REPOSITORY": "o.x/r"})
	assert proc.returncode == 0, proc.stderr
	assert (out["should_run"] == "false" and out["skip_reason"] == "claude_fixer_pending_checks") is skip, out
