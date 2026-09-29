# Implement-Plan Log — dispatch_workflow.py returns the run its own dispatch started

- Plan: docs/plans/issue-5016-dispatch-exact-run-id-plan.md
- Source issue: shubhodeep1/coding-workflows#5016
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5016-dispatch-exact-run-id   Final PR: #5051 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented twin-first (Q40): only the `workflow-templates/.claude/` twins changed; the phase PR carries a `hold` claim and waits for the master's `[claude-twin-sync]` into `.claude/` (twin-sync request on issue #5016).

## Phases
1. [ ] Phase 1 — exact run id from the dispatch response, no guessing on fallback, target-ref check in steps 9–10
   - protected paths: `.claude/scripts/dispatch_workflow.py`, `.claude/commands/implement-plan-claude.md` (twins: `workflow-templates/.claude/scripts/dispatch_workflow.py`, `workflow-templates/.claude/commands/implement-plan-claude.md`)
   - [x] `_post_dispatch` sends `return_run_details: true` and returns the parsed response
   - [x] `dispatch` uses the response's `workflow_run_id` (`matched_by: dispatch_response`), one best-effort run read fills `status` / `created_at`
   - [x] fallback poll: one new run → `matched_by: new_run`; more than one → exit 2, `ambiguous: true`, `candidate_run_ids`
   - [x] `tests/test_dispatch_workflow.py`: race, fallback, body, parsing tests; twin parity
   - [x] `/implement-plan-claude` Dispatch helper section and steps 9–10: target-ref check, one re-dispatch on mismatch
   - [x] `README.md`, `agents.md`, `changelog.d/5016-dispatch-exact-run-id.md`
   - Done: `tests/test_dispatch_workflow.py`, `tests/test_permission_prompts.py`, `tests/test_update_workflows_guardrails.py` pass; `ruff check` clean on changed Python.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should the helper tie a dispatch to its run? — Picked: A — send `return_run_details: true` and use the `workflow_run_id` GitHub returns. Alternatives: B — correlation input echoed into each workflow's `run-name` and matched on `display_title`; C — read each candidate run's inputs from its log. Why: exact, no workflow or consumer-wrapper change, no extra API calls (§5, §15). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What should the helper do when the response carries no run id and more than one new run appears? — Picked: A — exit 2 with `dispatched: true`, `ambiguous: true`, and `candidate_run_ids`; a single new run is returned with `matched_by: new_run`. Alternatives: B — keep returning the newest new run; C — exit 2 on any fallback match, even a single run. Why: the issue asks for no guessing; a single new run stays usable but flagged unverified. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] What should a read-result stage do when the recorded run's target ref is not this project's branch? — Picked: A — never read its verdict; re-dispatch once in the same cycle and wait on the new run; a second mismatch stops at `Status: BLOCKED`. Alternatives: B — stop at `Status: BLOCKED` on the first mismatch; C — search recent runs for one whose log shows this project's ref. Why: one bounded re-dispatch keeps the project unattended without looping. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should a successful response id be followed by a read of the run? — Picked: A — one best-effort read of `actions/runs/<id>` to fill `status` and `created_at`, `null` when it fails. Alternatives: B — no read; `status` and `created_at` always `null`. Why: keeps the documented output fields populated (§6) at one call. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29): answered Q1: A by the master session on issue #5016 (comment 5884723402).
- Phase 1 verification (2026-09-29): in a scratch copy with the twin in `.claude/scripts/`, `tests/test_dispatch_workflow.py`, `tests/test_permission_prompts.py`, `tests/test_update_workflows_guardrails.py` → 101 passed; `ruff check` clean. On the phase branch itself `test_template_parity` fails until the twin sync, by design.
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#5016: deliver`) in session session_015qQ7gjLiTs5eDGwni2CFiA, permission mode `auto`.
- Security pass: `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`, so the pass runs.
- Session started without `gh` and without a repository checkout (known pattern, #4938): the repo was attached and cloned, and `.claude/hooks/session-start.sh` was run by hand to install `gh`. No GitHub MCP tools were available; issue and PR writes went through `gh api` routine writes (CLAUDE.md §23.H).
- Stale Routine sweep (2026-09-29): 17 listed; 14 already gone (not found); the Auto-mode classifier refused 3 deletes (`dispatch …#5018: deliver`, `dispatch …#5016: deliver`, `implement-plan issue-4886-numbered-session-titles: check-in`), left for a later sweep.
