"""Contract for the /implement-plan-claude command text: the rules the
end-to-end dummy run (plan #4307) showed must hold for an unattended chain."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMAND = ROOT / ".claude" / "commands" / "implement-plan-claude.md"
TEMPLATE_COMMAND = ROOT / "workflow-templates" / ".claude" / "commands" / "implement-plan-claude.md"
CLAUDE_MD = ROOT / "CLAUDE.md"
REVIEW_COMMANDS = [
	ROOT / prefix / ".claude" / "commands" / name
	for prefix in ("", "workflow-templates")
	for name in ("verify-activation.md", "deploy-activate.md")
]


def _flat(path: Path) -> str:
	return " ".join(path.read_text(encoding="utf-8").split())


@pytest.fixture(scope="module")
def text() -> str:
	return " ".join(COMMAND.read_text(encoding="utf-8").split())


def test_template_parity():
	assert TEMPLATE_COMMAND.read_text(encoding="utf-8") == COMMAND.read_text(encoding="utf-8")


def test_validation_verdict_reads_status_json_first(text):
	# A passing validate run emits no VALIDATION_FAILURE_SUMMARY line.
	status_json = text.index("(1) `validation_status.json`")
	env = text.index("(2) the `STATUS_VALUE` / `RAW_STATUS_VALUE`")
	summary_line = text.index("(3) the `VALIDATION_FAILURE_SUMMARY` line")
	assert status_json < env < summary_line
	assert "emits **only on a failing verdict**" in text


def test_failed_runs_block_instead_of_passing(text):
	assert "only a `success` run has audited anything" in text
	assert "never continue to step 9 on your own" in text


def test_completion_pr_body_always_references_an_issue_or_pr(text):
	assert "`lint-plan-archival.yml` fails an archival PR whose body references no `#N` at all" in text
	assert "`Refs #<phase PR>`" in text


def test_checker_does_not_archive_itself(text):
	assert "and finally archive_session on your own session id" not in text
	assert "Do **not** archive yourself" in text


def test_only_the_chain_archives_its_sessions(text):
	assert "**Only the chain archives its own sessions.**" in text
	assert "if it is blocked, archived, or idle with no pending check-in" in text


def test_source_revision_reads_plan_and_log_from_default_branch(text):
	assert "read both from `origin/<default>`" in text


def test_checker_is_sonnet_every_three_hours(text):
	# Haiku cannot run in Auto mode, and outside it the claude-code-remote
	# write tools prompt on every call, so a Haiku checker never runs unattended.
	assert "`model: claude-sonnet-5`" in text
	assert "claude-haiku-4-5-20251001" not in text
	assert '`model: "haiku"`' not in text
	assert "call send_later with delay_minutes 60" in text
	assert "call send_later with delay_minutes 180" not in text


def test_claude_md_section_28_scopes_auto_decisions():
	claude = _flat(CLAUDE_MD)
	assert "## §28. Unattended Auto-Decisions in `/implement-plan-claude` Projects (MANDATORY)" in claude
	# Start-up checks, failure escalations, and ask-first operations are never auto-decided.
	assert "### C) Never auto-decided — still stop and ask" in claude
	assert "§22.B (DigitalOcean mutations), §23.C" in claude
	assert "`codex_failure`, unknown payload" in claude
	# §0 and the final reminder point at the exception.
	assert "The one exception is §28" in claude
	assert "Inside §28's scope: record the RECOMMENDED pick and continue." in claude


def test_chain_auto_decides_after_startup_checks(text):
	assert "## Auto-Decisions" in text
	assert "From the end of step 3 onward" in text
	assert "Steps 0, 1, and 3 still ask" in text
	assert "`docs/plans/<slug>-plan.md — scope conformance — unattended`" in text
	assert "always ending with `— unattended` (§28)" in text
	assert "## Auto-decisions - AD-<n> [<stage>, <YYYY-MM-DD>]" in text
	assert "Uncommitted auto-decisions:" in text
	# The old hard stop on plan ambiguity is gone.
	assert "stop and ask (§0/§2) rather than shipping a guess" not in text


def test_review_commands_list_auto_decisions_without_asking():
	for path in REVIEW_COMMANDS:
		body = _flat(path)
		assert "`change AD-<n> → <letter>`" in body, path
		if path.name == "verify-activation.md":
			assert "## Unattended Runs" in body, path
			assert "Auto-decisions (for review)" in body, path
		else:
			assert "## Auto-Decisions Review" in body, path
			assert "`claude/implement-plan-<slug>-decision-changes`" in body, path
			assert "no Q/A question is asked about any entry" in body, path


def test_live_state_still_accepts_decision_review_replies(text):
	assert "the LIVE stop does not apply to a `change AD-<n> → <letter>` or `confirm` reply" in text
	for path in REVIEW_COMMANDS:
		if path.name == "deploy-activate.md":
			body = _flat(path)
			assert "`Status: LIVE` report LIVE and stop unless the current reply changes an auto-decision" in body, path
			assert "`Status: LIVE` just report LIVE and stop unless the current reply changes an auto-decision" in body, path
			rules = body.split("## Rules", 1)[1]
			assert "`Status: LIVE`, report LIVE and stop unless the current reply changes an auto-decision" in rules, path


def test_project_branch_and_draft_final_pr(text):
	assert "3a. **Open the project branch and the draft final PR**" in text
	assert "`claude/implement-plan-<slug>`" in text
	assert "**`draft: true`**" in text
	assert "mark it ready for review (`mcp__github__update_pull_request` with `draft: false`)" in text
	assert "11a. **Final merge into the default branch**" in text
	# Legacy projects (log on the default branch without a Project branch line)
	# finish the way they started.
	assert "**Legacy mode** when the log exists on the default branch without a `Project branch:` line" in text
	assert "`git merge --no-edit origin/<default>`" in text


def test_review_rounds_are_fixed_by_claude(text):
	assert "7a. **Review round — Claude fixes what the reviewer panel found.**" in text
	assert "<!-- ai:claude-fixer-handoff:v1 kind=<findings|conflict> head=<sha> round=<r> -->" in text
	assert "`[claude-autofix] review round <r>: <summary>`" in text
	assert "`<!-- ai:claude-fixer-verdict:v1 head=<sha> -->`" in text
	assert "<!-- ai:claude-fixer-verdict:v2 head=<sha> round=<r> ledger=<64hex> -->" in text
	assert "CLAUDE_FIXER_VERDICT_BOT_LOGIN" in text
	assert "one fresh reviewer panel on that head" in text
	assert "--input claude_fixer_converged_head=<sha>" in text
	assert "`[claude-merge-resolve] merge <base branch>`" in text
	assert "`[claude-intervention] <summary>`" in text
	assert "the workflow never runs the GPT review-blocked judge" in text
	assert "`review` → `<next stage on review round>`" in text


def test_security_dispatch_targets_project_branch(text):
	assert "`--input ref=claude/implement-plan-<slug>`" in text
	# The audit files every finding (no weekly cap), so no bypass input exists.
	assert "bypass_weekly_cap" not in text
	assert "deferred_by_weekly_cap" not in text


def test_convergence_dispatch_goes_straight_to_review_autofix_here(text):
	# internal-review.yml pins review_autofix.yml@main; forwarding a new input
	# through it made every run on the PR adding the input a startup_failure
	# (run 36095647423).
	assert "`--workflow review_autofix.yml --input pr_number=<N> --input claude_fixer_converged_head=<sha>`" in text
	assert "gh workflow run internal-review.yml" not in text
	assert "--workflow internal-review.yml" not in text


def test_validation_dispatch_inputs(text):
	assert "plus `--input pr_number=0` for `internal-validate.yml` only" in text
	assert "`--input target_ref=claude/implement-plan-<slug>`" in text
	assert "-f tracking_issue=0 -f pr_number=0" not in text
	assert "--input tracking_issue=0 --input pr_number=0" not in text


def test_checker_starts_with_effort_low_then_one_shot_instructions(text):
	assert "the prompt `/effort low` **and nothing else**" in text
	assert "`create_trigger` with `persistent_session_id` = the checker's session id" in text
	assert "**hourly check-in by a low-effort Sonnet checker session**" in text
	assert "3-hourly" not in text
	assert "every 3h" not in text


def test_one_checker_per_project_keeps_the_chain_shallow(text):
	# claude-code-remote refuses create_session / send_later / create_trigger
	# 8 parent links below a root; a checker per stage added two links per
	# hand-off and stalled a project at its fourth security cycle.
	assert "**One checker per project, so the session chain stays shallow.**" in text
	assert "`title` = `<numbers>implement-plan <slug> — checker`" in text
	assert "**Reuse it** when it is not archived" in text
	assert "`title` = `implement-plan <slug> — waiting:" not in text
	assert "archives the previous stage session and the checker" not in text
	assert "that session archives this one and the checker" not in text
	assert "do **not** archive it here, because every later wait reuses it" in text


def test_zombie_checkers_are_cleaned_up(text):
	assert "### Zombie-checker cleanup" in text
	assert "Then run the [Zombie-checker cleanup](#zombie-checker-cleanup)" in text
	assert "`implement-plan <slug> — waiting:` (the older one-checker-per-wait design)" in text
	assert "archive it right after the new one is created, so a project never has two" in text
	assert "**Clear its stale check-ins.**" in text
	assert "archive the project checker (the project has no more waits), and stop" in text
	assert "report, archive the project checker, and archive this session" in text


def test_checker_ignores_superseded_waits(text):
	assert "the wake is stale: reply `stale check-in` and end the turn without re-arming" in text
	assert 'message "Check-in for wait <stage session id>:' in text
	assert "end the turn without re-arming: the next stage hands you its own wait" in text


def test_stage_sessions_start_no_side_sessions(text):
	"""A stage's side session and its §26 checker add two links (PR #4601's checker hit depth 8)."""
	assert "- **No side sessions.** A stage session calls `create_session` only where this command says so: the project checker, the next stage, the `/deploy-activate` session, and a fixer." in text
	assert "When a separate fix is wanted, file it as a GitHub issue: the Claude issue route starts it from the pickup, at depth 2 or less." in text


def test_depth_limit_refusal_is_loud_not_a_session_local_cron(text):
	assert "**Refused at the depth limit.**" in text
	assert "do **not** fall back to `CronCreate` or any other session-local loop" in text
	assert "[Fallbacks](#fallbacks) apply only when the tools are missing, not when they refuse." in text


def test_every_started_session_runs_opus_at_high_effort(text):
	"""Q2/Q14: stages, /deploy-activate and fixers start as Opus 5.5 with `/effort high` alone."""
	assert "The **stage model** is always `claude-opus-5-5` at high effort" in text
	assert "the prompt `/effort high` **and nothing else**" in text
	assert "`name` = `implement-plan <slug>: stage start`" in text
	assert "model claude-opus-5-5, permission_mode <mode>" in text
	assert "and the prompt `/effort high` and nothing else; (b) create_trigger" in text
	assert "model <stage model>" not in text and "`model` = the stage model" not in text


def test_stage_sessions_claim_before_fixing(text):
	"""Q22: a stage session claims the PR head so the §26.H sweep never duplicates it."""
	assert "### Claims" in COMMAND.read_text(encoding="utf-8")
	assert ".claude/scripts/claude_fix_claim.py post" in text
	assert "[Claim the head](#claims) (`--kind review` for findings, `--kind conflict` for a conflict)" in text
	assert "[claim the head](#claims) (`--kind blocked`, or `--kind ci` for a stuck PR)" in text
	assert "the §26.H hand-back cap does not apply to its PRs" in text


def test_issue_mode_follows_a_base_branch_that_merged(text):
	"""Q27: a stranded issue-mode project moves onto the branch its base merged into."""
	assert "**A base branch that merges moves the project.**" in text
	assert '`gh api "repos/<owner>/<repo>/pulls?state=closed&head=<owner>:<issue base>"`' in text
	assert "retarget the final PR (`mcp__github__update_pull_request` with `base` = `<new base>`)" in text
	assert "a move onto the default branch switches the final PR's body to `Fixes #<N>`" in text


def test_checker_routes_on_action_not_state(text):
	"""PR #4596: a low-effort checker read `state: review-round` and handed the
	PR back; the checker prompt now follows the script's `action` field."""
	prompt = text[text.index("### Checker prompt"):text.index("### Hand-back")]
	assert "Route on the JSON's `action` field only, never on `state`" in prompt
	assert "A review round or a conflict never uses the hand-back: it is always `next_stage` + `review` (step 5)" in prompt
	# The explicit state → action table, one row per mapping.
	for row in (
		"| PR | merged | next_stage | success | 5 |",
		"| PR | review-round, conflict | next_stage | review | 5 |",
		"| PR | blocked, closed, stuck | hand_back | — | 4 |",
		"| run | completed | next_stage | success | 5 |",
		"| run | failed | next_stage | block | 5 |",
		"| issue list | resolved | next_stage | success | 5 |",
		"| issue list | blocked | next_stage | block | 5 |",
		"| anything | not done | wait | — | 2 |",
		"| anything | read failed (exit 2) | retry | — | 3 |",
	):
		assert row in prompt, row
	assert "2. `action` is `wait` →" in prompt
	# A checker reused across the change refreshes its older script first.
	assert "git checkout FETCH_HEAD -- .claude/scripts/check_in_status.py` once and run step 1 again" in prompt
	assert "3. `action` is `retry` (exit 2, the JSON carries `error`) →" in prompt
	assert "4. `action` is `hand_back` (only a blocked, closed, or stuck PR) →" in prompt
	assert "Never take this step for any other `action`." in prompt
	assert "5. `action` is `next_stage` (or a step 3 / 4 / 4b fallback, which uses `block`) → pick the next stage from the JSON's `next_stage` field alone" in prompt
	assert "`success` → `<next stage on success>`; `review` → `<next stage on review round>`; `block` → `<next stage on block>`" in prompt
	# The old state-reading branches are gone.
	assert "`state` is blocked / closed / stuck and a hand-back trigger is given" not in prompt
	assert "if `state` is merged / completed / resolved use" not in prompt


def test_done_waiting_documents_action(text):
	section = text[text.index("**What counts as \"done waiting\"**"):text.index("### Checker prompt")]
	assert "`action`, and `next_stage` when `action` is `next_stage`" in section
	assert "exit 2 means the read failed and carries `action: retry`" in section
	assert "A review round or a conflict is **never** `hand_back`" in section


def test_hand_back_section_routes_on_action(text):
	section = text[text.index("### Hand-back"):text.index("### Fallbacks")]
	assert "and routes on its `action`" in section
	assert "If `action` is `hand_back` (blocked, closed, or stuck), the stage session:" in section


def test_third_conformance_fix_gets_a_fix_check_not_a_fourth_run(text):
	# Issue #4545's chain stopped at "conformance 4/3": the third run opened a
	# fix PR, and re-auditing it would have been a fourth run.
	assert "3 conformance runs per project" in text
	assert "the next stage is `conformance 3/3 — fix check` instead" in text
	assert "`docs/plans/<slug>-plan.md — scope fix-check #<fix PR> — unattended`" in text
	assert "opens no fix PR, and does not count toward the cap" in text
	assert "**FIX-VERIFIED** → continue as **CONFORMANT with no fix PR** below" in text
	assert "**FIX-DEFECTIVE**" in text and "never auto-decided (§28.C)" in text
	assert "after the third run's fix PR merges, the fix check replaces it" in text
	assert "`Outside fix-check scope`" in text
	assert "conformance 3/3 — fix check | security-pass" in text


def test_resume_stage_makes_no_self_get_session_and_rides_out_classifier_outages(text):
	assert "Take the id from Bash (`echo \"session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}\"`)" in text
	assert "A `— resume.` stage session reads its mode from the log's `Stage model: … Permission mode: <mode>` line instead" in text
	assert "a resume stage makes no self lookup (issue #4750)" in text
	assert "reading that line in this step, before step 2 (`git fetch origin claude/implement-plan-<slug>`, then `git show origin/claude/implement-plan-<slug>:docs/implement-plan/<slug>.md`" in text
	assert "(that fallback call follows the **Classifier outage** rule below like any other)" in text
	assert "**Classifier outage** (CLAUDE.md §23.J)" in text
	assert "is retried once. Refused again, it is not a blocker: do not ask, do not stop at `Status: BLOCKED`" in text
	assert "`delay_minutes: 30`, `initiation: own_followup`, `name: implement-plan <slug>: classifier outage retry`" in text


# Issue #4948: interim automatic twin-first default for protected-path phases.
# These tests read the workflow-templates twin, which this change edits first
# (twin-first); `.claude/` catches up through the [claude-twin-sync] copy.
INTERIM_TWIN_FIRST_MARKER = "Interim automatic default: twin-first (until #4785)"
INTERIM_TWIN_FIRST_CLAUDE_MD_MARKER = "Interim automatic twin-first default for protected-path phases (until #4785)"
INTERIM_TWIN_FIRST_AGENTS_MD_MARKER = "Interim until #4785 (issue #4948):"
INTERIM_TWIN_FIRST_MASTER_SESSION_MARKER = "Since #4948, stages in this repo record"
# Closing sentences that sit apart from each opening marker: the sunset guard
# checks them too, so a partial removal cannot leave one behind.
INTERIM_TWIN_FIRST_SUNSET_MARKER = "**Sunset:** this default ends with #4785."
INTERIM_TWIN_FIRST_CLAUDE_MD_SUNSET_MARKER = "**Sunset:** the PR that makes #4785's Actions sync live (`scripts/claude_twin_sync.py`) removes this bullet"
INTERIM_TWIN_FIRST_AGENTS_MD_CLOSER = "The PR that makes #4785's sync live removes this default."
TWIN_SYNC_SCRIPT = ROOT / "scripts" / "claude_twin_sync.py"
AGENTS_MD = ROOT / "agents.md"
MASTER_SESSION_MD = ROOT / "docs" / "operations" / "master-session.md"


def _step_4_twin() -> str:
	twin = _flat(TEMPLATE_COMMAND)
	return twin.split("4. **Implement the current phase", 1)[1].split("5. **Verify the phase", 1)[0]


def test_protected_path_phase_records_the_automatic_twin_first_approval():
	step = _step_4_twin()
	assert INTERIM_TWIN_FIRST_MARKER in step
	assert "In a repo that has `workflow-templates/.claude/` (coding-workflows), the question above is not asked" in step
	assert "when the log has no `Protected-path approval: phase <n>` line and the plan does not say the phase needs a watched session" in step
	assert "record `Protected-path approval: phase <n> — twin-first (automatic, interim until #4785) (<date>)` under `## Notes`" in step
	# §6: the approval line keeps its format; the new value is one more answer.
	assert "Record the answer as `Protected-path approval: phase <n> — <letter> (<date>)` under `## Notes`" in step
	assert "the line format is unchanged; this is one more `<answer>` value" in step
	assert "started only under a recorded `Protected-path approval: phase <n> — <answer> (<date>)` line" in _flat(TEMPLATE_COMMAND)


def test_twin_first_edits_twins_only_and_reaches_the_twin_sync_blocker():
	step = _step_4_twin()
	assert "Edit only the `workflow-templates/.claude/**` twins, never `.claude/**`" in step
	assert "A `.claude/` path with no twin (for example `.claude/commands/claude-issue-pickup.md`) is not edited at all" in step
	assert "its exact diff and the sha256 of the file it produces go into the twin-sync blocker" in step
	assert "After step 6 opens the phase PR, do not arm the wait (step 7)" in step
	assert "`claude_fix_claim.py post … --kind hold --by <your session id>`" in step
	assert "set `Status: BLOCKED` with `Waiting on: PR #N: twin sync`" in step
	assert "the `<!-- ai:claude-blocked:v1 -->` comment on the source issue, with the `ai:claude-blocked` label and one `PushNotification`" in step
	assert "each changed twin with its `.claude/` target and the twin's `sha256sum`" in step
	assert "as a `[claude-twin-sync]` commit on the phase branch" in step
	assert "arms the wait on that PR (step 7) and never re-implements the phase" in step
	assert "A later stage of this project whose push must change a `.claude/**` file" in step


def test_protected_path_question_remains_for_what_twin_first_cannot_cover():
	step = _step_4_twin()
	# The A/B/C question itself is unchanged.
	assert "**Q: Phase `<n>` must edit `<paths>`, which Claude Code never auto-approves in an unattended session. How should it run?**" in step
	assert "**C** — Try it unattended anyway" in step
	cases = step.split("The automatic default does not cover three cases, and the rules above apply to them unchanged:", 1)[1]
	assert "an edit that is denied even in the `workflow-templates/.claude/**` twin tree: stop and ask the question;" in cases
	assert "a phase whose plan says it must run in a watched session (for example `watched session: required`): stop and ask the question;" in cases
	assert "a log that already records a different `Protected-path approval: phase <n>` answer: that answer stands" in cases
	assert "The automatic default never overwrites a recorded answer." in cases
	assert "A repo without `workflow-templates/.claude/` (a consumer) has no twins, so it keeps the question." in cases
	assert INTERIM_TWIN_FIRST_SUNSET_MARKER in cases


def test_claude_md_section_28c_names_the_interim_twin_first_default():
	claude = _flat(CLAUDE_MD)
	section = claude.split("### C) Never auto-decided — still stop and ask", 1)[1].split("### D) Recording", 1)[0]
	# The protected-path stop stays listed; the interim default is added beside it.
	assert "- **Protected-path edits.** A phase that must edit `.claude/**`" in section
	assert INTERIM_TWIN_FIRST_CLAUDE_MD_MARKER in section
	assert "records `Protected-path approval: phase <n> — twin-first (automatic, interim until #4785) (<date>)` itself (the line format is unchanged)" in section
	assert "edits only the `workflow-templates/.claude/**` twins" in section
	assert "posts a `hold` claim and the twin-sync blocker" in section
	assert "for an edit that is denied even in the twin tree and for a phase whose plan says it needs a watched session" in section
	assert "stands and is never overwritten" in section
	assert INTERIM_TWIN_FIRST_CLAUDE_MD_SUNSET_MARKER in section


def test_operator_docs_name_the_interim_twin_first_default():
	# The sunset guard below checks these markers are gone; this keeps them from
	# drifting while the interim default is live, so that guard never passes vacuously.
	if TWIN_SYNC_SCRIPT.exists():
		pytest.skip("#4785's Actions twin sync is on this branch; the interim default is removed")
	agents = _flat(AGENTS_MD)
	assert INTERIM_TWIN_FIRST_AGENTS_MD_MARKER in agents
	assert INTERIM_TWIN_FIRST_AGENTS_MD_CLOSER in agents
	assert INTERIM_TWIN_FIRST_MASTER_SESSION_MARKER in _flat(MASTER_SESSION_MD)


def test_interim_twin_first_default_is_removed_when_the_4785_sync_lands():
	"""Removal trigger (#4948): once #4785's sync script exists, the interim default must be gone."""
	if not TWIN_SYNC_SCRIPT.exists():
		pytest.skip("#4785's Actions twin sync is not on this branch yet; the interim default stays")
	assert INTERIM_TWIN_FIRST_MARKER not in _flat(TEMPLATE_COMMAND), "remove the #4948 interim default from implement-plan-claude.md step 4"
	assert INTERIM_TWIN_FIRST_MARKER not in _flat(COMMAND), "remove the #4948 interim default from implement-plan-claude.md step 4"
	assert INTERIM_TWIN_FIRST_SUNSET_MARKER not in _flat(TEMPLATE_COMMAND), "remove the #4948 interim default's Sunset paragraph from implement-plan-claude.md step 4"
	assert INTERIM_TWIN_FIRST_SUNSET_MARKER not in _flat(COMMAND), "remove the #4948 interim default's Sunset paragraph from implement-plan-claude.md step 4"
	assert INTERIM_TWIN_FIRST_CLAUDE_MD_MARKER not in _flat(CLAUDE_MD), "remove the #4948 interim bullet from CLAUDE.md §28.C"
	assert INTERIM_TWIN_FIRST_CLAUDE_MD_SUNSET_MARKER not in _flat(CLAUDE_MD), "remove the #4948 interim bullet's Sunset sentence from CLAUDE.md §28.C"
	assert INTERIM_TWIN_FIRST_AGENTS_MD_MARKER not in _flat(AGENTS_MD), "remove the #4948 interim sentence from agents.md"
	assert INTERIM_TWIN_FIRST_AGENTS_MD_CLOSER not in _flat(AGENTS_MD), "remove the #4948 interim closing sentence from agents.md"
	assert INTERIM_TWIN_FIRST_MASTER_SESSION_MARKER not in _flat(MASTER_SESSION_MD), "remove the #4948 interim clause from the Q40 row of docs/operations/master-session.md"
