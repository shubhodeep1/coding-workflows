# Implement-Plan Log — dispatch_workflow.py returns the run its own dispatch started

- Plan: docs/plans/issue-5016-dispatch-exact-run-id-plan.md
- Source issue: shubhodeep1/coding-workflows#5016
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5016-dispatch-exact-run-id   Final PR: #5051 draft
- Status: BLOCKED
- Stage: conformance 1/3 — review round
- Activation: not started
- Waiting on: PR #5625: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01RnAvxLYqkEtpVTb2kSW6eL (kept for reuse)   safety net none   hand-back none (not armed while the twin sync holds the PR)
- Last updated: 2026-09-30
- Last note: conformance 1/3 — review round 2 on head 3bd212a (session_01B1uq5ppzcpYDRRkAoizNiS): the step 9 read-result text asked the stage to match a candidate still in progress, whose log is not served yet, so a run still going could be treated as a mismatch and re-dispatched; fixed twin-first (an in-progress candidate re-arms the wait, re-dispatch only once every recorded run completed without a match). The checker-side finding is rejected per AD-7. Held for the `[claude-twin-sync]` copy.

## Phases
1. [x] Phase 1 — exact run id from the dispatch response, no guessing on fallback, target-ref check in steps 9–10   — PR #5059 merged 2026-09-30 (`dcb72a9`, by the operator under Q46: A); review rounds: 2; interventions: 0
   - protected paths: `.claude/scripts/dispatch_workflow.py`, `.claude/commands/implement-plan-claude.md` (twins: `workflow-templates/.claude/scripts/dispatch_workflow.py`, `workflow-templates/.claude/commands/implement-plan-claude.md`)
   - [x] `_post_dispatch` sends `return_run_details: true` and returns the parsed response
   - [x] `dispatch` uses the response's `workflow_run_id` (`matched_by: dispatch_response`), one best-effort run read fills `status` / `created_at`
   - [x] fallback poll: one new run → `matched_by: new_run`; more than one → exit 2, `ambiguous: true`, `candidate_run_ids`
   - [x] `tests/test_dispatch_workflow.py`: race, fallback, body, parsing tests; twin parity
   - [x] `/implement-plan-claude` Dispatch helper section and steps 9–10: target-ref check, one re-dispatch on mismatch
   - [x] `README.md`, `agents.md`, `changelog.d/5016-dispatch-exact-run-id.md`
   - review rounds: 2; interventions: 0
   - Done: `tests/test_dispatch_workflow.py`, `tests/test_permission_prompts.py`, `tests/test_update_workflows_guardrails.py` pass; `ruff check` clean on changed Python.

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Implemented: COMPLETE; Correctness: CONCERNS, 2 EVIDENCE-BASED concerns, no blocker) — fix PR #5625 (pre-security); fix PR review rounds: 2

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should the helper tie a dispatch to its run? — Picked: A — send `return_run_details: true` and use the `workflow_run_id` GitHub returns. Alternatives: B — correlation input echoed into each workflow's `run-name` and matched on `display_title`; C — read each candidate run's inputs from its log. Why: exact, no workflow or consumer-wrapper change, no extra API calls (§5, §15). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What should the helper do when the response carries no run id and more than one new run appears? — Picked: A — exit 2 with `dispatched: true`, `ambiguous: true`, and `candidate_run_ids`; a single new run is returned with `matched_by: new_run`. Alternatives: B — keep returning the newest new run; C — exit 2 on any fallback match, even a single run. Why: the issue asks for no guessing; a single new run stays usable but flagged unverified. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] What should a read-result stage do when the recorded run's target ref is not this project's branch? — Picked: A — never read its verdict; re-dispatch once in the same cycle and wait on the new run; a second mismatch stops at `Status: BLOCKED`. Alternatives: B — stop at `Status: BLOCKED` on the first mismatch; C — search recent runs for one whose log shows this project's ref. Why: one bounded re-dispatch keeps the project unattended without looping. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should a successful response id be followed by a read of the run? — Picked: A — one best-effort read of `actions/runs/<id>` to fill `status` and `created_at`, `null` when it fails. Alternatives: B — no read; `status` and `created_at` always `null`. Why: keeps the documented output fields populated (§6) at one call. Applied in: phase 1. Status: pending review
- AD-5 [phase 1/1 — review round 1, 2026-09-30] What should the helper do when the pre-dispatch run-list read fails? — Picked: A — keep the read before the POST but make its failure non-fatal: dispatch anyway; a returned run id is exact (exit 0), no id → exit 2 with `dispatched: true` and no polling. Alternatives: B — keep failing before the POST with `dispatched: false` (the contract on `main`); C — move the read after the POST (the reviewers' literal suggestion). Why: after #5016 the list only feeds the fallback, so a transient read failure should not cancel a dispatch GitHub can name; C would let the new run count as "already existing". Applied in: PR #5059. Status: pending review
- AD-6 [conformance 1/3, 2026-09-30] How should a stage record a dispatch the helper could not match exactly, given a run's target ref sits in its log, which `gh run view --log` serves only once the run has completed? — Picked: A — record it unverified (an ambiguous result waits on the first candidate and records the rest) and confirm the ref at read-result, re-arming on a matching candidate still in progress. Alternatives: B — wait inside the dispatching stage until the run completes; C — add a `run-name` carrying the target ref to the audit and validate workflows. Why: keeps the read-result check the plan added as the single gate, with no workflow or consumer-wrapper change (plan non-goal) and no waiting inside a stage. Applied in: PR #5625. Status: pending review
- AD-7 [conformance 1/3 — review round 1, 2026-09-30] Should the checker wait on every candidate of an ambiguous dispatch instead of the first one? — Picked: A — keep the first-candidate wait; the read-result stage checks every recorded candidate. Alternatives: B — add a multi-run mode to `check_in_status.py` that is done when any candidate completes; C — record the candidates and wait on each in turn from the dispatching stage. Why: the candidates are runs of the same workflow started inside the helper's 90-second window, so the delay is bounded by the difference in their durations, on a path that needs both a missing run id and a parallel dispatch; B adds a new interface to a protected script (§5, §6) and C waits inside a stage. Applied in: no code change (PR #5625 finding rejected). Status: pending review

## Lessons
- [source:intervention] Once an API response names the object a call created, reads taken only to find that object afterwards become fallback-only: make their failure non-fatal instead of letting them block the call (files: .claude/scripts/dispatch_workflow.py)
- [source:conformance] A GitHub Actions run's log, and every env value echoed in it, is readable only after the run completes (`gh run view --log` refuses an in-progress run), so a check that reads a run's inputs from its log belongs in the stage that reads the result, never in the stage that dispatched it (files: .claude/commands/implement-plan-claude.md)
- [source:conformance] When a helper's cost or contract changes, update every table that describes it, including the CLAUDE.md §23.I helper table, not only README.md and agents.md (files: CLAUDE.md)
- [source:intervention] An instruction that identifies an object from data served only after it completes (a run's log) must say what to do with the objects still in progress, or the "none matched" path fires early (files: .claude/commands/implement-plan-claude.md)

## Notes
- Conformance fix PR #5625 review round 1 (2026-09-30, head c979e85, ledger e82a94ff…98bc): finding `CLAUDE.md:1361` (call count omits the default-branch read and the pre-dispatch run-list read) fixed; finding `.claude/commands/implement-plan-claude.md:71` (checker waits only on the first ambiguous candidate) rejected per AD-7. The fix touches no `.claude/` path, so no twin sync is needed.
- Conformance fix PR #5625 review round 2 (2026-09-30, head 3bd212a, ledger 0f4fa7e1…d64b): finding `implement-plan-claude.md:71` (read-result stage cannot match an in-progress candidate, may re-dispatch while this project's run is still going; 3 reviewers) fixed twin-first in `workflow-templates/.claude/commands/implement-plan-claude.md` (sha256 `2c1707a0db2dc5ec69a51411473eac622d6546555debf6030a379cad50b6a053`); finding `check_in_status.py:664-670, 772-778` (checker should pivot to other candidates) rejected: the checker never reads a target ref, the read-result stage does, and with the fix it re-arms on each remaining candidate (AD-7). Project branch synced with `main` first (clean merge `a698fd4`, 381 passed / 1 skipped). Scratch copy with the twin in `.claude/` → 138 passed / 1 skipped (command, helper, guardrail, permission suites) and 235 passed (the other suites that read the command).
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29): answered Q1: A by the master session on issue #5016 (comment 5884723402).
- Review round 1 (2026-09-30, head df70ca3, ledger c5622640…01e0): finding `dispatch_workflow.py:213` (pre-dispatch list read) fixed in part — the read stays before the POST (it must, for the fallback), its failure no longer cancels the dispatch (AD-5); finding `dispatch_workflow.py:184` (response not validated as a dict) rejected — line 185 and `_response_run_id` already return `None` for non-object JSON (covered by `test_post_dispatch_returns_the_response_object_or_none`). Twin-first: only `workflow-templates/.claude/scripts/dispatch_workflow.py` changed; scratch copy with the twin in `.claude/` → 154 passed, `ruff check` clean.
- Phase 1 verification (2026-09-29): in a scratch copy with the twin in `.claude/scripts/`, `tests/test_dispatch_workflow.py`, `tests/test_permission_prompts.py`, `tests/test_update_workflows_guardrails.py` → 101 passed; `ruff check` clean. On the phase branch itself `test_template_parity` fails until the twin sync, by design.
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#5016: deliver`) in session session_015qQ7gjLiTs5eDGwni2CFiA, permission mode `auto`.
- Security pass: `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`, so the pass runs.
- Session started without `gh` and without a repository checkout (known pattern, #4938): the repo was attached and cloned, and `.claude/hooks/session-start.sh` was run by hand to install `gh`. No GitHub MCP tools were available; issue and PR writes went through `gh api` routine writes (CLAUDE.md §23.H).
- Stale Routine sweep (2026-09-29): 17 listed; 14 already gone (not found); the Auto-mode classifier refused 3 deletes (`dispatch …#5018: deliver`, `dispatch …#5016: deliver`, `implement-plan issue-4886-numbered-session-titles: check-in`), left for a later sweep.
- Review round 2 (2026-09-30, head 791b572, session_01Q8WxfhVutLZWycR5omH9Jf): the panel reviewed the head three times (runs 36669862592, 36670081879, 36670930991); 2 findings and 1 task gap, all rejected with reasons on PR #5059 (issuecomment-5906589279): stale pre-sync evidence (`261711e`) or misread code. No `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is configured, so no verdict could converge the round; `Status: BLOCKED` with the question on issue #5016 (comment 5906597024).
- Q46: A (2026-09-30, master session, issue #5016 comment 5907962079): the operator merged PR #5059 into the project branch as `dcb72a9`, bound to head `791b572`, and resumed the chain at the stage after phase 1/1.
- Conformance 1/3 (2026-09-30, session_01MF3T3dSpJesMjsZ57DbhRz, started by the dispatcher on `/reclarify`): project branch synced with `main` (clean merge `b98f152`, 159 passed / 1 skipped on the helper and command suites before the push); zombie checkers archived: 0; stale Routine sweep: nothing to delete. `return_run_details` confirmed against GitHub's 2026-02-19 changelog. Fix PR #5625 is twin-first under the phase 1 protected-path approval: only `workflow-templates/.claude/commands/implement-plan-claude.md` changed (sha256 `a75a8a9608fb49ef711b3e1fad7b1740c2db290b16cff8675c5b63e265799db7`); scratch copy with the twin in `.claude/` → 162 passed / 1 skipped on the command suites, 41 passed on the other two parity-bearing files.
