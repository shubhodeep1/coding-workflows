# Bind a closed-issue final-merge resume to its triggering /reclarify

Source issue: shubhodeep1/coding-workflows#5514 (https://github.com/shubhodeep1/coding-workflows/issues/5514)
Base branch: claude/implement-plan-issue-5222-final-merge-resume-closed-issue
Security pass: skip (ai:security: automation-produced issue)

## Summary

The `final_merge_resume` route (#5222) lets a trusted `/reclarify` resume a closed issue whose Claude project is blocked at a `final-merge` stage. The route accepts any trusted `/reclarify` that sits after the blocked comment, including one posted before the issue closed. The intake also accepts it for an `opened` or `manual` payload. This plan binds the closed-issue path to one specific `/reclarify` comment, by ID, that was created after the issue closed. Payloads without that proof are refused.

## Context

- Security finding #5514 (`A01:2021-Broken Access Control`, severity medium, `scripts/claude_issue_route.py:487`): a write-authorized dispatcher sends an `opened` or `manual` intake payload for a closed issue. A trusted `/reclarify` posted before the closure satisfies `final_merge_resume`, so the intake queues a new Claude session with no request made after the closure. The finding recommends binding authorization to the triggering trusted comment ID and requiring that comment to be created after the closure and after the blocked comment.
- The code under audit is on the base branch (`claude/implement-plan-issue-5222-final-merge-resume-closed-issue`, final PR #5225, still a draft). It has not reached `main` or `@stable`, so no consumer runs it yet.
- Today's rule (`final_merge_resume`, lines 382–436): a closed, Claude-routed issue with `ai:claude` + `ai:claude-blocked`, whose latest trusted `<!-- ai:claude-blocked:v1 -->` comment names a `final-merge` stage, and **any** trusted `/reclarify` after that comment. `authorize_target` (lines 482–489) applies it whatever the payload's `trigger`.
- Replay paths this leaves open:
  1. The intake's `workflow_dispatch` (`trigger: manual`) or a hand-built `repository_dispatch` (`trigger: opened` / `reclarify`) from anyone with write on the target repo.
  2. A re-run of an old clarify run whose `/reclarify` was posted before the merge. clarify re-reads the issue live, sees it closed, and the old `/reclarify` passes.
- clarify already has the triggering comment: `github.event.comment` (the `Decide clarify route` step reads `.body`). The handoff builds the `claude_issue.v1` payload with `build-dispatch`. GitHub allows 10 top-level `client_payload` properties, and the payload uses 7.
- The REST issue object carries `closed_at`, and every comment carries `id` and `created_at`. The intake and clarify both already read the full comment list for a closed candidate.

## Goals

- A closed issue is authorized (intake) or routed (clarify) as `final_merge_resume` only when **all** of the #5222 conditions hold **and**:
  - a specific `/reclarify` comment is named: the payload's new `reclarify_comment_id` (intake), or `github.event.comment.id` (clarify);
  - that comment is in the issue's live comment list, is from a trusted `User` (`OWNER` / `MEMBER` / `COLLABORATOR`), and its body starts with `/reclarify`;
  - it comes after the latest trusted blocked comment;
  - its `created_at` is strictly later than the issue's `closed_at`.
- The intake refuses a closed issue whose payload is not `trigger: reclarify` with a valid `reclarify_comment_id`, before it reads any comments. The refusal reason stays `issue_closed`.
- Open-issue routing and authorization do not change. `opened` / `manual` payloads for open issues, and `/reclarify` payloads from a handoff that predates the key, still pass.
- Tests cover each new refusal (historical `/reclarify`, same-second `/reclarify`, missing or unknown ID, untrusted comment, `opened`/`manual` payload, clarify re-run replay) and the accepted #5119 shape.

## Non-goals

- Changing the session-side check in `/implement-issue-claude` step 2 (a protected `.claude/` path, AD-5).
- A `workflow_dispatch` input for a comment ID. The manual intake cannot resume a closed issue, and `/reclarify` is the resume path (AD-4).
- Reopening issues, changing the queue or fire-text format, or changing open-issue authorization.

## Constraints

- §1: this narrows a security gate. Every new condition is checked against live GitHub data, and a missing or unparseable value fails closed.
- §5: minimal change. One optional payload key and one new function parameter, plus the wiring through clarify and the handoff. No new API call: the comments are already read for a closed candidate (§15).
- §6: new identifiers `reclarify_comment_id` (payload/validated key, Python parameter), `--reclarify-comment-id` (CLI flag on `build-dispatch` and `final-merge-resume`), `CLAUDE_ISSUE_RECLARIFY_COMMENT_ID` (handoff env, default empty), `RECLARIFY_COMMENT_ID` (clarify step env), `_parse_positive_comment_id`, and the `final_merge_resume` reasons `no_reclarify_comment`, `reclarify_not_trusted`, `reclarify_before_close`. None are in use (`grep -rn` over `scripts/`, `.github/`, `tests/`, `.claude/`, `workflow-templates/`). No identifier is renamed or removed. The existing reasons `issue_closed` and `no_reclarify_after_block` keep their meaning.
- §4: the new handoff env var defaults to empty (no ID, as before).
- §9: tabs in Python and shell, 2-space YAML.
- §15: no new GitHub API call. The intake now reads comments for a closed candidate only when the payload names a `/reclarify` comment, so it makes fewer reads than before.
- §20: a security fix, so one `changelog.d/5514-final-merge-resume-reclarify-binding.md` fragment (`security`).
- §27: `clarify.yml` grows by a few lines, far under 480,000 bytes.
- Backward compatibility: `validate_payload` accepts a payload without the key (`reclarify_comment_id: None`). The key is added to the payload only when the handoff has an ID, so `test_build_dispatch_shape` keeps its exact shape for `opened`.

## Approach

AD-1 picks the finding's recommendation. `final_merge_resume(issue, comments, reclarify_comment_id=None)` gains the binding checks after the existing stage check:

1. no valid ID, or no comment with that ID → `no_reclarify_comment`;
2. that comment is not a trusted `User` `/reclarify` → `reclarify_not_trusted`;
3. it is at or before the latest trusted blocked comment → `no_reclarify_after_block` (existing reason);
4. `closed_at` or its `created_at` is missing or unparseable, or `created_at <= closed_at` → `reclarify_before_close`;
5. otherwise eligible (`final_merge_resume`).

Callers:

- **clarify** (`Decide clarify route`): new env `RECLARIFY_COMMENT_ID: ${{ github.event.comment.id || '' }}`, passed as `--reclarify-comment-id`. A re-run of a pre-closure clarify run carries its original comment, which fails check 4.
- **handoff** (`claude_issue_handoff.sh`): new env `CLAUDE_ISSUE_RECLARIFY_COMMENT_ID` (set by clarify's `Claude issue handoff` step to `${{ github.event.comment.id || '' }}`), passed to `build-dispatch --reclarify-comment-id`. `build_dispatch` adds `reclarify_comment_id` to `client_payload` when it is given (a positive int; anything else raises `ValueError`).
- **intake** (`authorize_target`): for a closed candidate, refuse `issue_closed` at once (no `needs_comments`) unless `validated["trigger"] == "reclarify"` and `validated["reclarify_comment_id"]` is a positive int. Then read comments and call `final_merge_resume(issue, comments, validated["reclarify_comment_id"])`. `validate_payload` parses the optional key (absent or `null` → `None`; a positive non-bool int → kept; anything else → `ValueError`, rejected as `invalid_payload`).

Alternatives: AD-1 B, requiring only some trusted `/reclarify` after `closed_at` with no ID, still lets an `opened`/`manual` payload or a later replay ride a genuine post-closure `/reclarify`, and it ignores the finding's provenance requirement. AD-1 C, dropping the intake's closed-issue path, breaks the #5222 resume.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the function, its two callers, and the payload key are one gate, and shipping only part of it would either leave the replay open or refuse every legitimate resume.

1. **Phase 1 — bind the closed-issue resume to its /reclarify comment**. Files: `scripts/claude_issue_route.py`, `scripts/claude_issue_handoff.sh`, `scripts/claude_issue_intake.sh` (header comment), `.github/workflows/clarify.yml`, `tests/test_claude_issue_route.py`, `README.md`, `agents.md`, `changelog.d/5514-final-merge-resume-reclarify-binding.md`. Done when: `tests/test_claude_issue_route.py`, `tests/test_phase_skip_gate_telemetry_contract.py`, and `tests/test_implement_issue_claude_command.py` pass; `clarify.yml` parses; `bash -n` passes on both scripts. Rollback: revert the phase PR, which restores the #5222 rule.

## Implementation Steps

1. `scripts/claude_issue_route.py`:
   - add `_parse_positive_comment_id(value)` (positive non-bool int → int, else `None`);
   - `build_dispatch(..., reclarify_comment_id=None)`: validate the value and add it to `client_payload` when it is not `None`;
   - `validate_payload`: parse and return `reclarify_comment_id`;
   - `final_merge_resume(..., reclarify_comment_id=None)`: add checks 1–4 above and update the docstring's reason list;
   - `authorize_target`: add the trigger and ID gate before `needs_comments`, and update the docstring;
   - CLI: `--reclarify-comment-id` (default empty) on `build-dispatch` and `final-merge-resume`; a non-empty value that is not a positive integer exits 2;
   - update the module docstring.
2. `.github/workflows/clarify.yml`: `RECLARIFY_COMMENT_ID` env on `Decide clarify route` and its use in the `final-merge-resume` call; `CLAUDE_ISSUE_RECLARIFY_COMMENT_ID` env on `Claude issue handoff`; update the step comment.
3. `scripts/claude_issue_handoff.sh`: read `CLAUDE_ISSUE_RECLARIFY_COMMENT_ID` (default empty), pass it to `build-dispatch`, and document it in the header. `scripts/claude_issue_intake.sh`: update the step 1b header text.
4. Tests (below), README "Claude issue implementer" resume paragraph, `agents.md` item 15, and the changelog fragment.

## Files & Modules

- `scripts/claude_issue_route.py`
- `scripts/claude_issue_handoff.sh`
- `scripts/claude_issue_intake.sh`
- `.github/workflows/clarify.yml`
- `tests/test_claude_issue_route.py`
- `README.md`, `agents.md`
- `changelog.d/5514-final-merge-resume-reclarify-binding.md` [new]

## Tests

- `final_merge_resume`: eligible for the #5119 shape (block, merge closes the issue at `T`, `/reclarify` at `T+5s`, bound by ID). Refused with `reclarify_before_close` for a `/reclarify` before `closed_at`, in the same second, or when `closed_at` or `created_at` is missing. Refused with `no_reclarify_comment` for no ID, an unknown ID, or a bool/str/negative ID. Refused with `reclarify_not_trusted` when the bound comment is untrusted, a Bot, or not a `/reclarify`. Refused with `no_reclarify_after_block` when it precedes the block. A later untrusted `/reclarify` does not help a historical bound one.
- `authorize_target`: `opened` and `manual` payloads for a closed candidate, and `reclarify` without an ID, are refused `issue_closed` with `needs_comments: False`. A historical pre-closure `/reclarify` bound by ID is refused. A post-closure one is authorized `final_merge_resume`. Open-issue cases are unchanged.
- `validate_payload` / `build_dispatch`: the key is accepted, kept, and omitted when `None`; invalid values are rejected; the payload has ≤ 10 keys.
- Intake end to end: a closed candidate with an `opened` or `manual` payload is rejected with no comments read and nothing written. A bound post-closure `/reclarify` is queued. A bound pre-closure one is rejected.
- clarify step run under bash: routes only when `RECLARIFY_COMMENT_ID` names the post-closure comment. A re-run carrying a pre-closure comment ID keeps `reason=issue_closed outcome=skip`. The handoff step's env carries the ID.
- Handoff: the dispatch body carries `reclarify_comment_id` when the env var is set, and omits it when the var is empty.

## Risks

- A `/reclarify` posted in the same second as the merge is refused. The human comments again. That is fail-closed and rare (the #5119 gap was 5 seconds).
- A consumer whose clarify predates this change sends no ID, so its closed-issue resume is refused. The route is not on `@stable` yet, and clarify, the handoff, and the intake ship together.

## Rollout

Lands on the #5222 project branch and reaches `main` with final PR #5225, then consumers with the next `@stable`. There are no new repository variables. The one new env var defaults to empty.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How should the closed-issue resume be bound? — Picked: A — bind it to the triggering `/reclarify` comment ID (payload `reclarify_comment_id` / clarify's event comment), which must be a trusted `/reclarify` after the latest blocked comment and created strictly after `closed_at`; refuse payloads without it. Alternatives: B — require any trusted `/reclarify` after `closed_at`, with no ID; C — remove the intake's closed-issue path. Why: A is the finding's recommendation and closes both replay paths; B leaves `opened`/`manual` replays that ride a real post-closure comment; C breaks #5222. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What should the new payload key be called? — Picked: A — `reclarify_comment_id`, optional, added only when set. Alternatives: B — `comment_id`; C — `trigger_comment_id`. Why: §6 uniqueness; `comment_id` is already a nested parameter in `final_merge_resume`, and `TRIGGER_COMMENT_ID` is an existing env name in `plan.yml`. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Is a `/reclarify` in the same second as `closed_at` after the closure? — Picked: A — no, require `created_at` strictly after `closed_at`. Alternatives: B — accept equal timestamps. Why: §1 fail-closed; timestamps have one-second resolution, so an equal one may predate the close. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] May the intake's `workflow_dispatch` resume a closed issue? — Picked: A — no, a `manual` payload for a closed issue is refused `issue_closed`. Alternatives: B — add a comment-ID input to `workflow_dispatch`. Why: the finding asks to reject payloads without provenance; `/reclarify` is the resume path; §5. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Should the session-side check in `/implement-issue-claude` step 2 also require a post-closure `/reclarify`? — Picked: A — no, leave the protected command unchanged. Alternatives: B — edit the `workflow-templates/.claude/` twin and stop for the twin sync. Why: a session for a closed issue starts only from a queue item the intake authorized, so the intake is the boundary the finding names; §5, and no protected-path stop. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Which reason does the intake report for the new refusals? — Picked: A — keep `issue_closed` for every closed-issue refusal; the detailed reason shows in clarify's `final_merge_resume … reason=` notice. Alternatives: B — new intake refusal reasons. Why: §5; README, agents.md, and tests document `issue_closed` as the closed-issue refusal. Applied in: phase 1. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- No protected paths: the phase edits nothing under `.claude/**` or `workflow-templates/.claude/**` (AD-5).
