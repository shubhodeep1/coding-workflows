# Implement-Plan Log — Claude-fixer: auto-merge a clean review once its pending checks finish green

- Plan: docs/plans/issue-4900-claude-fixer-pending-checks-auto-merge-plan.md
- Source issue: shubhodeep1/coding-workflows#4900
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge   Final PR: #4922 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4942
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_011Tb3U5tZETVFkPYfdpNYmZ   safety net pending   hand-back pending (re-armed by the round 4 stage, session_01Vqfx592KSqqnuJxU98CZ25)
- Last updated: 2026-09-29
- Last note: review round 4 on PR #4942 (head 1516ddf, workflow round 2): 1 finding fixed (the sweep's pending-checks pass now catches every exception, so it can never end the catch-all sweep), 4 findings / task gaps rejected (fail-closed by design, or already present).

## Phases
1. [ ] Phase 1 — pending-checks marker, gate skip, and sweep auto-merge   — PR #4942 open (waiting); review rounds: 4; interventions: 0
   - Hand-off step posts `ai:claude-fixer-pending-checks:v1` for a clean ledger with only incomplete checks
   - Gate skips dispatched re-runs on such a head (`claude_fixer_pending_checks`)
   - `scripts/claude_fixer_pending_checks.py` + `scripts/claude_pr_sweep.py` enable head-bound auto-merge once the head's checks are ready
   - `check_in_status.py` unchanged; tests lock in `wait` / `ci-failed`
   - Tests reproduce PR #4869; docs; `changelog.d/` fragment
   - Done: the plan's phase 1 "done" condition. Protected paths: none.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the readiness re-check run? — Picked: A — in the hourly `claude-pr-catch-all` sweep (`scripts/claude_pr_sweep.py`), which already covers this repo and every consumer with `GH_PAT`. Alternatives: B — a new `review_autofix.yml` job fed by the 30-minute review sweep dispatch; C — a `workflow_run` completion trigger. Why: one place, all repos, no reviewer re-run, no workflow growth. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What does the hand-off step leave for a clean review with pending checks? — Picked: A — a workflow-owned comment with its own header and `<!-- ai:claude-fixer-pending-checks:v1 head=<sha> round=<n> ledger=<sha256> -->`. Alternatives: B — no comment, only a job output. Why: the sweep and the gate need a trusted, head-bound record. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Does `check_in_status.py` (a protected `.claude/**` path) change? — Picked: A — no; the new marker is not a hand-off, so it already routes to `wait`, and tests lock that in. Alternatives: B — add an explicit `pending-checks` state (protected-path edit, §28.C stop). Why: §5. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-29] What happens when a pending check later fails? — Picked: A — nothing new is posted; `check_in_status.py --hand-back` reports `ci-failed`, and `/implement-plan-claude` heads keep their 6-hour stuck window. Alternatives: B — the sweep posts a `kind=findings` hand-off. Why: matches the issue's acceptance. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] Should dispatched re-runs skip a pending-checks head? — Picked: A — yes, skip reason `claude_fixer_pending_checks`. Alternatives: B — let the 30-minute review sweep re-run the reviewer panel. Why: cost and intent. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] How does the sweep honour a repo's auto-merge policy? — Picked: A — read that repo's `ENABLE_AUTO_MERGE` variable (404 = unset = `true`, other failure = no merge) and run `review_enable_auto_merge.sh`. Alternatives: B — call `gh pr merge --auto` directly. Why: one auto-merge policy. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Does the sweep also run "Mark linked issues ready to merge"? — Picked: A — no. Alternatives: B — port the label step. Why: §5; its inputs exist only inside the review run. Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-29] Does `collection_status: timeout` count as "pending"? — Picked: A — yes, with `failed_count: 0` and `incomplete_count > 0`; `ready` with incomplete runs too. Alternatives: B — `ready` only. Why: the #4869 case is a timeout. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-29] A `cancelled` / `stale` check? — Picked: A — never merge; log it. Alternatives: B — treat it as green. Why: fail closed (§1). Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:intervention] A new pass added inside an existing per-item sweep loop must catch every exception per item (`except Exception`, logged), not a list of expected classes: widening the list one class per review round left the older, more important work of the loop exposed to any failure the list missed. (files: scripts/claude_pr_sweep.py)
- [source:intervention] When a workflow jq gate and a Python parser both match the same marker comment, give them the same round and header regexes (rounds start at 1) and cover the edge value in both test files, or a marker one side accepts can stall the other. (files: .github/workflows/review_autofix.yml, scripts/claude_fixer_pending_checks.py)
- [source:plan-deviation] When a clean Claude-fixer outcome needs a later action, record it as its own workflow-owned marker rather than a `kind=findings` hand-off: `check_in_status.py` treats every findings hand-off as a review round, so a 0-entry hand-off wakes a fixer with nothing to fix. (files: scripts/review_autofix_step_claude_fixer_handoff.sh, scripts/claude_fixer_pending_checks.py)
- [source:intervention] A per-item fail-open loop must catch every exception class the item's code can raise, including `OSError` from temp files and subprocess setup, not only the API read errors; one uncaught item ends the run for every later item. (files: scripts/claude_pr_sweep.py, scripts/claude_fixer_pending_checks.py)
- [source:intervention] When the project branch syncs with the default branch, re-run the phase's tests against the merged code: a shared reader (`check_in_status.py`) can gain a new API read or a new trusted run shape (a review dispatched from the default branch) that the phase's own verifier and test stubs must mirror. (files: scripts/claude_fixer_pending_checks.py, .claude/scripts/check_in_status.py)

## Notes
- Issue-mode project started by the Claude issue dispatcher routine (trigger trig_01REPcAnL9kSG6T8m3G2hkpn) in session session_01VGQb3cJVEoX1uoX989DUaJ (Auto mode). Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4900#issuecomment-5882051076
- Security pass: run (`security_pass_skip.py`: no skip label).
