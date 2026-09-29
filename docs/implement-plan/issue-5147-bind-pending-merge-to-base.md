# Implement-Plan Log — Claude-fixer pending-checks merge: bind the marker to the reviewed base

- Plan: docs/plans/issue-5147-bind-pending-merge-to-base-plan.md
- Source issue: shubhodeep1/coding-workflows#5147
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
- Project branch: claude/implement-plan-issue-5147-bind-pending-merge-to-base   Final PR: #5179 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5186
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Pv1NrebS3urzs5D9gF6Dy4
- Last updated: 2026-09-29
- Last note: review round 1 on PR #5186: 1 consensus finding (lone-surrogate base ref crashed `base_ref_digest`) accepted and fixed with an injective `surrogatepass` encoding plus tests.

## Phases
1. [ ] Phase 1 — bind the pending-checks marker to the reviewed base   — PR #5186 open (waiting); review rounds: 1; interventions: 0
   - Hand-off step posts an `ai:claude-fixer-pending-checks:v2` line bound to `PR_PAYLOAD_FILE`'s `base.ref` (sha256) and `base.sha`; no pending comment without a valid base
   - `scripts/claude_fixer_pending_checks.py` returns `base_changed` / `base_unbound` and never merges on a missing or mismatched binding
   - Gate skips dispatched re-runs only for a marker bound to the current base (fresh review on retarget)
   - Tests reproduce the #5147 retarget exploit; README / agents.md; `changelog.d/` fragment
   - Done: the plan's phase 1 "done" condition. Protected paths: none.

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] What does the marker bind as the "reviewed base commit"? — Picked: A — the PR's `base.ref` and `base.sha` as the review run read them (`PR_PAYLOAD_FILE`), compared with the same fields of the sweep's and gate's existing PR reads. Alternatives: B — the base branch's live tip; C — the merge base from the compare API. Why: catches the retarget exploit and any PR re-sync with zero new API calls, without invalidating pending merges on every unrelated base push. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How does the marker carry the binding? — Picked: A — a new `ai:claude-fixer-pending-checks:v2` line beside the unchanged v1 line; readers require the bound v2 line. Alternatives: B — extend the v1 line in place. Why: §6. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How is the base ref encoded in the marker? — Picked: A — `base_ref_sha256=<sha256 of the ref name>`, readable ref on an unparsed `Reviewed base:` line. Alternatives: B — the raw ref with a restricted charset. Why: git ref names may contain `>` and `-`. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] What triggers the "automated fresh review"? — Picked: A — the existing 30-minute `review_autofix_sweep.yml` dispatch, which the gate now skips only for a marker bound to the current base. Alternatives: B — the catch-all sweep dispatches `internal-review.yml` itself. Why: no new write path (§5, §15, §23.C). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] What does the hand-off step do without a valid payload base? — Picked: A — no pending-checks comment; fall through to the existing fail-closed path. Alternatives: B — post the v1 line only. Why: an unbound marker could never merge and would leave the head without an owner. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] How are v1-only pending-checks comments treated? — Picked: A — unbound: no merge, no gate skip. Alternatives: B — accept v1 when the base looks unchanged. Why: fail closed; no v1 comment exists outside the unreleased #4900 branch. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] Should the in-run zero-findings auto-merge also be base-bound? — Picked: A — no, out of scope. Alternatives: B — add a base re-read to the workflow's auto-merge step. Why: §5; the finding is about the sweep's delayed authorization. Applied in: no code change. Status: pending review

## Lessons
- [source:security] A marker that authorizes a delayed merge must bind everything the review depended on (head and base); the REST PR `base.sha` changes on a retarget or PR sync but not on every base push, so binding it costs no call and no extra reviews. (files: scripts/claude_fixer_pending_checks.py, scripts/review_autofix_step_claude_fixer_handoff.sh, .github/workflows/review_autofix.yml)

## Notes
- Started 2026-09-29 by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#5147: deliver`) in session_01C1BNRjR2aajP3S6bUTQGn2 (Auto mode). The session had no GitHub MCP tools and no `gh` until the repo's SessionStart hook was run by hand after attaching the repo; GitHub writes use `gh api` routine calls.
- Security pass: `.claude/scripts/security_pass_skip.py` returned `skip: true` (`ai:security: created and labelled by the issue automation`).
