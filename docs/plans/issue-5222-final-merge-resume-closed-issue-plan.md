# Resume an issue-mode project with /reclarify after the final merge closed its issue

Source issue: shubhodeep1/coding-workflows#5222 (https://github.com/shubhodeep1/coding-workflows/issues/5222)
Base branch: main
Security pass: run

## Summary

An issue-mode `/implement-plan-claude` project that stops at a `final-merge` stage asks a human to merge the final PR and then comment `/reclarify` on the issue. The final PR carries `Fixes #<N>`, so the merge closes the issue, and clarify skips every comment on a closed issue. `/reclarify` is dropped and verify-activation never starts. This plan adds a narrow `final_merge_resume` route: a trusted `/reclarify` on a closed issue passes clarify, the intake, and `/implement-issue-claude` only when that issue is a Claude project that is blocked at a `final-merge` stage. Every other closed-issue comment keeps skipping.

## Context

- Incident (#5119): final PR #5151 merged at 2026-09-29T19:01:37Z as `e663dac` and closed #5119 in the same second. The `/reclarify` at 19:01:42Z reached clarify run 36616307042, which logged `AI_PHASE_GATE_V1 phase=clarify gate=route reason=issue_closed outcome=skip issue=5119`. No routing or dispatch comment was posted. The master started verify-activation by hand at 20:11Z.
- The blocked comment on #5119 (comment 5896457919, posted as the `OWNER` account) begins `<!-- ai:claude-blocked:v1 -->` and carries the stage as `**Stage:** \`final-merge — review round\``. Its option A reads "merge it … then comments `/reclarify` here".
- The resume path has three closed-issue gates today:
  1. `.github/workflows/clarify.yml` step `Decide clarify route` (lines 408–518) sets `SKIP_CODEX=true` when `IS_CLOSED` and logs `reason=issue_closed outcome=skip`. The router is called only when `SKIP_CODEX` is false. Step `Claude issue handoff` (line 534) also requires `is_closed != 'true'`.
  2. `scripts/claude_issue_route.py` `authorize_target` (lines 347–391) refuses with `issue_closed`. The intake (`scripts/claude_issue_intake.sh` step 1b) reads comments only when `needs_comments` is set.
  3. `.claude/commands/implement-issue-claude.md` step 2: **Closed** → `issue closed — nothing to do`, stop.
- Downstream of those gates the resume already works. At the final merge the project log on the default branch reads `Status: COMPLETE`, `Activation: pending verify-activation` (see `docs/implement-plan/issue-5119-deflake-stall-guard-observe-test.md` on `main`). `/implement-issue-claude` step 4 finds the project through `docs/completed/issue-<N>-*-plan.md`. `/implement-plan-claude` step 2 resumes a `Status: COMPLETE` log at step 12 (`verify-activation`). Step 2's Claim of `/implement-issue-claude` already removes `ai:claude-blocked`.
- The queue, the pickup (`.claude/commands/claude-issue-pickup.md`), and `queue_pending` never read the target issue's state, so they need no change.

## Goals

- A trusted `/reclarify` on a **closed** issue is routed to Claude with route reason `final_merge_resume` when, and only when, all of these hold:
  - the issue carries `ai:claude` and `ai:claude-blocked`, and `route_issue` would route it to Claude (no `ai:codex`, not orchestrator-managed, not a Codex-only type or an `[E2E ` fixture);
  - its latest `<!-- ai:claude-blocked:v1 -->` comment from a trusted `User` (`OWNER` / `MEMBER` / `COLLABORATOR`) has a `Stage:` line whose value starts with `final-merge`;
  - a trusted `User` commented `/reclarify` after that blocked comment.
- The intake authorizes the same case (reason `final_merge_resume`) from its own live reads and queues it. The issue is never reopened.
- `/implement-issue-claude` continues a closed issue under the same rule instead of stopping, and its Claim removes `ai:claude-blocked` from the closed issue.
- Every other closed-issue comment keeps the `reason=issue_closed outcome=skip` gate, and the intake keeps refusing other closed issues with `issue_closed`.
- Tests: the closed-issue route (pure function, CLI, clarify step run end to end, intake), plus a regression test that an unrelated closed issue still skips.

## Non-goals

- Reopening the issue or changing the `Fixes #<N>` contract of the final PR.
- Auto-resuming without a `/reclarify` (for example by the project checker watching the merge). That needs a `check_in_status.py` change for held PRs.
- Other closed-issue stages (a block at `security-pass`, `validation`, …). The issue is still open at those stages, so `/reclarify` already works.
- The verdict-bot (#4648) and check-wait timeout (#4900) root causes.

## Constraints

- §1: the new path widens a security gate, so every condition is checked from live GitHub data by trusted authors only. Untrusted blocked or `/reclarify` comments are ignored. A read failure keeps the skip.
- §5: minimal change. No new trigger value, payload key, or label. The dispatch keeps `trigger: reclarify`, and only the route reason is new.
- §6: new identifiers `final_merge_resume`, `FINAL_MERGE_RESUME_REASON`, `BLOCKED_COMMENT_MARKER`, `BLOCKED_STAGE_LINE_RE`, `FINAL_MERGE_STAGE_RE`, the CLI subcommand `final-merge-resume`, the clarify output `final_merge_resume`, and the shell variable `FINAL_MERGE_RESUME` do not collide with existing names (checked with `grep -rn` over `scripts/`, `.github/`, `tests/`). No existing identifier is renamed or removed. The existing `issue_closed` log line and refusal reason stay.
- §9: tabs in Python and shell, 2-space YAML.
- §15: clarify reads the issue's comments (one paginated REST read) only on a closed issue whose `/reclarify` event carries both labels. Other closed issues cost no new call. The intake already reads comments when `needs_comments` is set, and now sets it for a closed issue that carries both labels.
- §20: observable behaviour change, so one `changelog.d/5222-final-merge-resume.md` fragment.
- §27: `clarify.yml` is 78,249 bytes, far under the 480,000-byte guard.
- §28.C / protected paths: `.claude/commands/implement-issue-claude.md` and `.claude/commands/implement-plan-claude.md` are protected. The interim twin-first rule applies: only `workflow-templates/.claude/commands/*` is edited, and the phase PR stops for the `[claude-twin-sync]` copy.

## Approach

AD-1 chose the intake route (the issue's first direction). A pure function `final_merge_resume(issue, comments)` in `scripts/claude_issue_route.py` holds the rule. Three callers use it:

1. **clarify** (`Decide clarify route`): on a closed issue whose event is a `/reclarify` and whose labels include `ai:claude` and `ai:claude-blocked`, fetch the comments (`gh api --paginate …/comments?per_page=100`, `jq -s 'add // []'`) and run `claude_issue_route.py final-merge-resume`. When the result is eligible, the step sets `FINAL_MERGE_RESUME=true`, `ISSUE_IMPLEMENTER=claude`, `ISSUE_IMPLEMENTER_REASON=final_merge_resume`, keeps `SKIP_CODEX=true` and `IS_CLOSED=true`, and logs `AI_PHASE_GATE_V1 phase=clarify gate=route reason=final_merge_resume outcome=handoff issue=<N>`. Otherwise it logs the existing `reason=issue_closed outcome=skip` line. The step gains `GH_TOKEN: ${{ secrets.GH_PAT }}` for that read and a new output `final_merge_resume`. The handoff step's `if:` becomes `issue_implementer == 'claude' && (is_closed != 'true' || final_merge_resume == 'true')`. The Codex steps stay skipped because `SKIP_CODEX` is true.
2. **intake** (`authorize_target`): a closed issue with both labels returns `needs_comments=True` when no comments were supplied. With comments supplied, it authorizes with reason `final_merge_resume` when the rule holds and refuses with `issue_closed` otherwise. A closed issue without both labels is refused with `issue_closed` and no comments read, as today. The shell script needs no change beyond its header comment.
3. **`/implement-issue-claude` step 2** (twin): a closed issue that meets the same rule (checked by the session from the issue and comments it already fetched in step 1) is a final-merge resume. The session does not reopen it, runs the Claim (which removes `ai:claude-blocked`), and continues at step 4, which resumes the project at verify-activation.

`claude_issue_handoff.sh` posts a resume-specific routing comment for reason `final_merge_resume` (AD-6), so the thread shows the issue stays closed and the project is resuming. `/implement-plan-claude`'s Issue Mode stop bullet (twin) now requires the blocked comment to carry the stage on a `**Stage:** \`<stage>\`` line, and tells a `final-merge` stop to name `/reclarify` as the resume after the merge.

Alternatives: AD-1 B (swap the final PR's `Fixes` for `Refs` at a final-merge block) changes the documented issue-closing contract and does not help an issue that already closed. AD-1 C (the project checker watches the merge) needs `check_in_status.py` to honour holds on the final PR, which is a larger change in a protected path.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the three gates are one resume path, and opening only some of them would leave a route that queues a session that then stops.

1. **Phase 1 — final-merge resume on a closed issue**. Files: `scripts/claude_issue_route.py`, `.github/workflows/clarify.yml`, `scripts/claude_issue_handoff.sh`, `scripts/claude_issue_intake.sh` (comment only), `workflow-templates/.claude/commands/implement-issue-claude.md`, `workflow-templates/.claude/commands/implement-plan-claude.md`, `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`, `README.md`, `agents.md`, `changelog.d/5222-final-merge-resume.md`. Done when: the new and existing tests in `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py` (twin-reading tests), and `tests/test_phase_skip_gate_telemetry_contract.py` pass, the clarify YAML parses, and `bash -n` passes on both scripts. The template-parity tests stay red until the twin sync, as the interim rule expects. Rollback: revert the phase PR. Every closed issue then skips again.

## Implementation Steps

1. `scripts/claude_issue_route.py`: add `FINAL_MERGE_RESUME_REASON = "final_merge_resume"`, `BLOCKED_COMMENT_MARKER = "<!-- ai:claude-blocked:v1 -->"`, `BLOCKED_STAGE_LINE_RE` (first line of the form `Stage:` / `**Stage:**`, optionally bulleted, capturing the value without backticks), and `FINAL_MERGE_STAGE_RE` (`^final-merge(?![\w-])`). Add `final_merge_resume(issue, comments) -> {"eligible", "reason", "blocked_comment_id"}` with reasons `not_issue`, `issue_open`, `not_claude_routed`, `not_blocked`, `no_blocked_comment`, `stage_not_final_merge`, `no_reclarify_after_block`, `final_merge_resume`.
2. Same file: in `authorize_target`, replace the closed refusal with the rule above. Update its docstring. Add a CLI subcommand `final-merge-resume --issue-json F --comments-json F` that prints the JSON result: exit 0 on a decision either way, exit 2 on unreadable input. Update the module docstring.
3. `.github/workflows/clarify.yml` `Decide clarify route`: add `GH_TOKEN`, the comment read, the CLI call, the `FINAL_MERGE_RESUME` branch and log line, and the new output. Add it to the `::notice::` line. Change the `Claude issue handoff` `if:`.
4. `scripts/claude_issue_handoff.sh`: a resume-specific comment body when `ROUTE_REASON` is `final_merge_resume`. `scripts/claude_issue_intake.sh`: update the header comment (step 1b).
5. `workflow-templates/.claude/commands/implement-issue-claude.md` step 2 **Closed** bullet: the final-merge resume exception. `workflow-templates/.claude/commands/implement-plan-claude.md` Issue Mode "Stops are reported on the issue" bullet: the `**Stage:**` line and the final-merge resume sentence.
6. Tests, README (Claude issue implementer failure modes and switching table), agents.md (item 15), and the changelog fragment.

## Files & Modules

- `scripts/claude_issue_route.py`
- `.github/workflows/clarify.yml`
- `scripts/claude_issue_handoff.sh`
- `scripts/claude_issue_intake.sh`
- `workflow-templates/.claude/commands/implement-issue-claude.md`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `tests/test_claude_issue_route.py`
- `tests/test_implement_issue_claude_command.py`
- `README.md`, `agents.md`
- `changelog.d/5222-final-merge-resume.md` [new]

## Tests

- `final_merge_resume`: eligible on the #5119 shape; refused for an open issue, a missing label, `ai:codex`, a non-final-merge stage, an untrusted blocked comment, no `/reclarify` after the block, a `/reclarify` only before the block, an untrusted `/reclarify`, and a newer blocked comment at another stage.
- `authorize_target`: a closed issue without the labels is still `issue_closed` with no comments requested (regression). With the labels it asks for comments, then authorizes `final_merge_resume` or refuses `issue_closed`.
- Intake end to end with the existing `gh` stub: a closed final-merge issue is queued with `reason=final_merge_resume`, and a closed unrelated issue is still rejected with nothing written.
- The clarify `Decide clarify route` step run under bash with a stub `gh`: a closed, blocked final-merge issue routes `final_merge_resume` (outputs `issue_implementer=claude`, `final_merge_resume=true`); an unrelated closed issue logs `reason=issue_closed outcome=skip` and makes no comment read (regression). The handoff `if:` is asserted.
- Handoff: the routing comment for `final_merge_resume`.
- Command twins: the step 2 exception text and the `**Stage:**` line rule.

## Risks

- A blocked comment that omits the `Stage:` line cannot be resumed through `/reclarify`. That is fail-closed, and the command now requires the line.
- A second `/reclarify` before the resumed session removes `ai:claude-blocked` can queue a second item. The intake reuses an open queue item for the same issue, and the session's Claim removes the label within minutes of its start.

## Rollout

Ships to consumers with the next `@stable` (clarify is reusable; its scripts are staged from coding-workflows). The command changes reach `.claude/` at the `[claude-twin-sync]` copy. No new env var or repo variable is added.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which fix direction? — Picked: A — intake route `final_merge_resume` in clarify, intake, and `/implement-issue-claude`. Alternatives: B — swap the final PR's `Fixes` for `Refs` at a final-merge block; C — the project checker resumes on the merge. Why: the issue lists A first and asks for closed-issue route tests; it keeps the `Fixes` contract and also covers an issue that already closed. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] New trigger value or new route reason? — Picked: A — keep `trigger: reclarify` and add route reason `final_merge_resume`. Alternatives: B — a new `final_merge_resume` trigger in `VALID_TRIGGERS` and the payload. Why: §5, no payload or fire-text schema change, and the intake re-derives eligibility from live data anyway. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Who may author the blocked comment that unlocks the route? — Picked: A — a trusted `User` only (`OWNER` / `MEMBER` / `COLLABORATOR`). Alternatives: B — also `github-actions[bot]`. Why: §1; the sessions post blocked comments through MCP as a user, never as the Actions bot. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] How is the blocked stage read? — Picked: A — the first `Stage:` / `**Stage:**` line of the latest trusted blocked comment, whose value starts with `final-merge`. Alternatives: B — a new marker attribute `<!-- ai:claude-blocked:v1 stage=… -->`. Why: A matches the comments already on blocked issues (#5119), and B would strand them. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Where is `ai:claude-blocked` removed? — Picked: A — the resumed `/implement-issue-claude` session's step 2 Claim (existing behaviour, now reached for the closed issue). Alternatives: B — the clarify handoff script. Why: the label stays the visible "blocked" signal until a session really resumes, and the label gate keeps a repeated `/reclarify` from re-routing after that. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Should the handoff's routing comment differ for the resume? — Picked: A — yes, a resume-specific body under the same `ai:claude-issue-routed:v1` marker. Alternatives: B — reuse the "a Claude session will implement this issue" text. Why: the generic text says the issue closes when the completion PR merges, which is wrong for a closed issue. Applied in: phase 1. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`.
- Protected paths: phase 1 edits `.claude/commands/implement-issue-claude.md` and `.claude/commands/implement-plan-claude.md` through their `workflow-templates/.claude/commands/` twins only (interim twin-first rule, until #4785).
