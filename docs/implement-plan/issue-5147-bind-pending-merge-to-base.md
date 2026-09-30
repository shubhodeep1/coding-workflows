# Implement-Plan Log — Claude-fixer pending-checks merge: bind the marker to the reviewed base

- Plan: docs/completed/issue-5147-bind-pending-merge-to-base-plan.md (moved from docs/plans/ by the completion PR)
- Source issue: shubhodeep1/coding-workflows#5147
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
- Project branch: claude/implement-plan-issue-5147-bind-pending-merge-to-base   Final PR: #5179 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge)
- Waiting on: the completion PR into the project branch, then final PR #5179 (into the #4900 branch)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Pv1NrebS3urzs5D9gF6Dy4
- Last updated: 2026-09-30
- Last note: completion stage (resumed by `/reclarify` after Q1: A on #5147): validation skipped as answered, plan moved to docs/completed/, 274 pytest passed on the project branch; next stage final-merge 1/1.

## Phases
1. [x] Phase 1 — bind the pending-checks marker to the reviewed base   — PR #5186 merged 2026-09-29 (c1a616e); review rounds: 1; interventions: 0
   - Hand-off step posts an `ai:claude-fixer-pending-checks:v2` line bound to `PR_PAYLOAD_FILE`'s `base.ref` (sha256) and `base.sha`; no pending comment without a valid base
   - `scripts/claude_fixer_pending_checks.py` returns `base_changed` / `base_unbound` and never merges on a missing or mismatched binding
   - Gate skips dispatched re-runs only for a marker bound to the current base (fresh review on retarget)
   - Tests reproduce the #5147 retarget exploit; README / agents.md; `changelog.d/` fragment
   - Done: the plan's phase 1 "done" condition. Protected paths: none.

## Conformance
- Run 1 — 2026-09-29: CONFORMANT (Step 3 COMPLETE, Step 4 CONCERNS) — fix PR from `claude/implement-plan-issue-5147-bind-pending-merge-to-base-conformance-fix-1` (pre-security), PR #5213 (review rounds: 1): `gate_claude_pending_checks_on_head` now reads only the latest trusted pending-checks comment for the head, as `find_pending_marker` does; checks: 259 pytest passed across the five plan suites, actionlint, shellcheck, `bash -n`. PR #5213 merged 2026-09-29 (45eeae0).
- Run 2 — 2026-09-29: CONFORMANT (Step 3 COMPLETE, Step 4 CONCERNS: two HYPOTHESIS concerns, nothing fixable on evidence; listed in the #5147 blocked comment and under ## Notes) — no fixes (pre-security); checks: 274 pytest passed across the five plan suites, shellcheck and `bash -n` on the hand-off step, `review_autofix.yml` 459,807 bytes.

## Security pass
- Skipped (ai:security: automation-produced issue).

## Validation
- Skipped — 2026-09-30: base is the #4900 project branch; validate.yml authorizes only a PR into the default branch (Q1: A on #5147, answered 2026-09-29 by the owner as standing decision Q17: A). #4900's chain runs its own security audit and runtime validation on a branch that contains this change before anything reaches `main`. Long-term fix: #4734.

## Completion
- Completion PR (branch `claude/implement-plan-issue-5147-bind-pending-merge-to-base-complete`) — doc moved to docs/completed/issue-5147-bind-pending-merge-to-base-plan.md; 274 pytest passed on the project branch at 45eeae0
- Final PR #5179 draft (into the #4900 branch)

## Activation
- n/a: the base is the #4900 project branch, so this change goes live with that project's own lifecycle (final PR #4922). The final-merge stage closes #5147 explicitly and labels it `ai:merged`.

## Auto-decisions
- AD-1 [plan, 2026-09-29] What does the marker bind as the "reviewed base commit"? — Picked: A — the PR's `base.ref` and `base.sha` as the review run read them (`PR_PAYLOAD_FILE`), compared with the same fields of the sweep's and gate's existing PR reads. Alternatives: B — the base branch's live tip; C — the merge base from the compare API. Why: catches the retarget exploit and any PR re-sync with zero new API calls, without invalidating pending merges on every unrelated base push. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How does the marker carry the binding? — Picked: A — a new `ai:claude-fixer-pending-checks:v2` line beside the unchanged v1 line; readers require the bound v2 line. Alternatives: B — extend the v1 line in place. Why: §6. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How is the base ref encoded in the marker? — Picked: A — `base_ref_sha256=<sha256 of the ref name>`, readable ref on an unparsed `Reviewed base:` line. Alternatives: B — the raw ref with a restricted charset. Why: git ref names may contain `>` and `-`. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] What triggers the "automated fresh review"? — Picked: A — the existing 30-minute `review_autofix_sweep.yml` dispatch, which the gate now skips only for a marker bound to the current base. Alternatives: B — the catch-all sweep dispatches `internal-review.yml` itself. Why: no new write path (§5, §15, §23.C). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] What does the hand-off step do without a valid payload base? — Picked: A — no pending-checks comment; fall through to the existing fail-closed path. Alternatives: B — post the v1 line only. Why: an unbound marker could never merge and would leave the head without an owner. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] How are v1-only pending-checks comments treated? — Picked: A — unbound: no merge, no gate skip. Alternatives: B — accept v1 when the base looks unchanged. Why: fail closed; no v1 comment exists outside the unreleased #4900 branch. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] Should the in-run zero-findings auto-merge also be base-bound? — Picked: A — no, out of scope. Alternatives: B — add a base re-read to the workflow's auto-merge step. Why: §5; the finding is about the sweep's delayed authorization. Applied in: no code change. Status: pending review
- AD-8 [conformance 1/3, 2026-09-29] The gate skips on any trusted pending-checks comment bound to the current base, but the sweep evaluates only the latest one; which should both follow? — Picked: A — the gate reads only the latest trusted pending-checks comment for the head (selected as `find_pending_marker` selects it) and requires its single v2 line, with the v1 line's round and ledger, to match the current base. Alternatives: B — the sweep merges on any trusted comment bound to the current base. Why: §1, fail closed; a retarget back to an earlier reviewed base costs one fresh review instead of merging on an older review, and keeps the plan's "latest trusted marker wins" rule. Applied in: conformance fix PR (branch …-conformance-fix-1). Status: pending review

## Lessons
- [source:intervention] A jq filter that must agree with a Python reader on comment text has to split lines the way `str.splitlines()` does (CRLF, lone CR, and the Unicode line boundaries), not with `split("\n")`, and a test should run both readers over the same comments. (files: .github/workflows/review_autofix.yml, scripts/claude_fixer_pending_checks.py)
- [source:security] A marker that authorizes a delayed merge must bind everything the review depended on (head and base); the REST PR `base.sha` changes on a retarget or PR sync but not on every base push, so binding it costs no call and no extra reviews. (files: scripts/claude_fixer_pending_checks.py, scripts/review_autofix_step_claude_fixer_handoff.sh, .github/workflows/review_autofix.yml)
- [source:conformance] When two readers act on the same marker (a gate that skips re-reviews and a sweep that merges), they must select the same comment; `any()` in one and latest-wins in the other lets them disagree, and the PR stalls with neither a merge nor a review. (files: .github/workflows/review_autofix.yml, scripts/claude_fixer_pending_checks.py)
- [source:plan-deviation] An issue-mode project based on another project's branch cannot pass validate.yml's explicit-target check, which requires the final PR to target the default branch; settle validation before starting such a project. (files: .github/workflows/validate.yml, .claude/commands/implement-plan-claude.md)

## Notes
- 2026-09-29 conformance 2/3 stopped before validation 1/3 (`validate.yml`'s `Authorize explicit validation target` accepts a `target_ref` only for exactly one open PR from that branch into the default branch; #5179 targets the #4900 branch). Q1 asked on #5147; answered `Q1: A` with `/reclarify` on 2026-09-29. Resumed 2026-09-30 by the Claude issue dispatcher in session_013DBNgxismwnXBcJpjPmwU5.
- Conformance run 2 HYPOTHESIS concerns, not fixed and carried to #4900's final review: (1) the sweep binds the base only up to enabling auto-merge; a write user retargeting while a merge still waits on required approvals is not caught (no approvals are required on `main` here; same property as AD-7). (2) the gate reads comment bodies clipped to the first and last 2000 characters when over 4000; a crafted check-run name with a Unicode line separator in the clipped middle could make the gate skip a re-review the sweep would not merge on (stalls only the PR's own author; inherited from #4900).
- Started 2026-09-29 by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#5147: deliver`) in session_01C1BNRjR2aajP3S6bUTQGn2 (Auto mode). The session had no GitHub MCP tools and no `gh` until the repo's SessionStart hook was run by hand after attaching the repo; GitHub writes use `gh api` routine calls.
- Security pass: `.claude/scripts/security_pass_skip.py` returned `skip: true` (`ai:security: created and labelled by the issue automation`).
