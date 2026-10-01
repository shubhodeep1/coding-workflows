# Implement-Plan Log — Consumer sync takes hook and settings files from the owner-reviewed `.claude/` tree, not the twin

- Plan: docs/plans/issue-5607-guard-assets-from-claude-tree-plan.md
- Source issue: shubhodeep1/coding-workflows#5607
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5607-guard-assets-from-claude-tree   Final PR: #5651 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5654
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01A5rLUxqZWzzL1obW7dTcMW   safety net trig_01FgYaw1wg6vqty65HXDenAy   hand-back trig_01LdeyTCo8d7G8hD5x9sKPpt
- Last updated: 2026-10-01
- Last note: review round 3 on head 4d85fe2: no valid finding (one reviewer flagged the stale `review / codex-agent` failure from the OpenRouter credit outage, in `review_autofix.yml`, which this PR does not touch); no verdict bot, so the round closes with a new head: the issue-base sync (merged into the project branch as ce49684) plus this log update (AD-10).

## Phases
1. [ ] Phase 1 — guard assets from the `.claude/` tree at consumer sync and seed (edits the twin `workflow-templates/.claude/commands/seed-repo.md` only; protected paths: none)   — PR #5654 open (waiting); review rounds: 3; interventions: 0

## Conformance

## Security pass
- Security: skipped (ai:security: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which sync surfaces does the fix cover? — Picked: A — the consumer updater and the `/seed-repo` twin. Alternatives: B — the updater only; C — also add a release gate. Why: both copy the stable twin tree into consumers; seed is edited twin-first at no extra cost (§1). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What changes at release? — Picked: A — nothing; release sources no `.claude/` asset, and the consumer side reads the tagged commit's `.claude/` tree. Alternatives: B — fail the release while any guard twin differs from `.claude/`; C — rewrite the twin at release. Why: B stalls unrelated releases behind an owner review, C writes to protected history (§5). Applied in: no code change. Status: pending review
- AD-3 [plan, 2026-09-30] A guard twin differs from `.claude/` and the consumer lacks the file: what is installed? — Picked: A — the `.claude/` copy from the same commit (owner-reviewed). Alternatives: B — nothing. Why: follows the recommendation and never leaves a new consumer without a reviewed guard. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] The `.claude/` copy is missing at the stable commit: what happens? — Picked: A — skip with a warning; never install the twin. Alternatives: B — install the twin. Why: B is the finding's exploit path (§1). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Where does the rule live? — Picked: A — in the existing shell step, with a parity test against `claude_twin_sync.py`'s constants. Alternatives: B — a new `claude_twin_sync.py` subcommand. Why: smallest change, no new CLI surface (§5). Applied in: phase 1. Status: pending review
- AD-6 [phase 1/1 — review round 2, 2026-09-30] The guard compare fails (`cmp` exit 2+, a read error): what happens to that file? — Picked: A — warn with the cmp exit status and change nothing, whether or not the consumer has the file. Alternatives: B — treat it as equal and install the `.claude/` copy; C — fail the step. Why: fail safe like the missing-source case, and the next sync retries; B installs on an unverified compare, C blocks every other file's sync (§1, §5). Applied in: PR #5654. Status: pending review
- AD-7 [phase 1/1 — blocked PR (resume), 2026-10-01] The issue's log says IN_PROGRESS and the project checker exists, but it is idle with no pending check-in and the issue carried ai:claude-blocked when /reclarify arrived: resume, or report "already in progress"? — Picked: A — resume (remove ai:claude-blocked, hand the wait back to the checker). Alternatives: B — report already in progress and stop. Why: /reclarify is the documented resume path, and the log lagged because the blocked stage had no PR to commit to; B would strand the project. Applied in: no code change. Status: pending review
- AD-8 [phase 1/1 — blocked PR (resume), 2026-10-01] Q1: A said to push a progress-log commit for a fresh head, but the operator had already removed ai:review-blocked and the review sweep re-dispatched review on head 4d85fe2, which is running: push anyway? — Picked: A — no push; wait on the running review. Alternatives: B — push the log commit (a new head starts another review over the same code). Why: the running review already does what the push was for, and the operator asked to wait on it (§5). Applied in: no code change. Status: pending review
- AD-9 [phase 1/1 — review round 3, 2026-10-01] Head 4d85fe2 still carried the `hold` claim of catch-all fixer session_01NMcUjX2WRgsQt2WyVxMeaJ (§26.H hand-back cap, posted during the credit outage) when the round 3 hand-off arrived: handle the round, or arm the wait and leave the head held? — Picked: A — handle the round; this stage's newer `review` claim lifts the hold. Alternatives: B — arm the wait (the checker would route the same review round straight back). Why: the owner already answered the hold (credits topped up, label removed, /reclarify), and this command's PRs are outside the §26.H hand-back cap. Applied in: no code change. Status: pending review
- AD-10 [phase 1/1 — review round 3, 2026-10-01] Round 3 has no valid finding and `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is unset, so no verdict can close it: how does the round close? — Picked: A — post the finding-by-finding reply and push a real change: the issue-base sync (25 commits of claude/implement-plan-issue-4785-twin-first-claude-sync, merged into the project branch as ce49684) plus this log update, which starts a fresh review round. Alternatives: B — stop BLOCKED until a verdict bot exists; C — hold the PR for an owner merge. Why: the base merge is due at every stage (step 2), as in issue-4886's AD-12/AD-15/AD-18, and the new head also replaces the stale failed `review / codex-agent` check from the credit outage. Applied in: PR #5654 (merge commit). Status: pending review

## Lessons
- [source:plan-deviation] GNU diff has no `--ignore-space-at-eol` (that is a git diff flag), so a `diff -q --strip-trailing-cr --ignore-space-at-eol` guard exits 2 and every file counts as changed; use `--ignore-trailing-space` (`-Z`) or `cmp` and test the shell step end to end (files: .github/workflows/update_workflows.yml)
- [source:intervention] In a POSIX shell `case` pattern `*` also matches `/`, so `hooks/*` covers nested paths; pin shell-vs-Python path rules with a test that runs the pattern through `bash` and `sh` against the Python classifier, not just a string match (files: .github/workflows/update_workflows.yml, tests/test_update_workflows_guardrails.py)
- [source:intervention] `! cmp -s a b` treats a read error (exit 2) like a difference; capture the status (`cmp -s a b || st=$?`) and branch on 1 vs 2+, and test the error path with a stub `cmp` on PATH, since root in CI can read chmod-000 files (files: .github/workflows/update_workflows.yml, tests/test_update_workflows_guardrails.py)

## Notes
- Security pass: skip (`security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Base PR check (2026-09-30): the base branch's PR #4804 is open (draft) into main.
- Pre-existing, not fixed here (§5): the three `diff -q --strip-trailing-cr --ignore-space-at-eol` calls in `update_workflows.yml` (the `.claude/` sync, the CLAUDE.md sync, and the wrapper compare) fail with `unrecognized option` on GNU diffutils 3.10, so they always count a file as changed and copy it. Content stays correct (the commit step skips an empty index), but `claude_changed` is inflated. Candidate for its own issue.
- Phase 1 evidence: `tests/test_update_workflows_guardrails.py` passes (the new behavioural test fails on the pre-fix step); `tests/test_changelog_fragment_contract.py` and `tests/test_workflow_file_size_limit.py` pass; `claude_twin_sync.py check` is ok; ruff shows only the file's existing E402.
- Final PR #5651 (draft) opened 2026-09-30 into claude/implement-plan-issue-4785-twin-first-claude-sync.
- Review round 1 (2026-09-30, head 45d44f6): the `scripts/claude_twin_sync.py:436-449` finding (a stable backport of a `.claude/` guard file without its twin fails the twin-at-head compare) is outside this PR's diff and a plan non-goal; sibling finding #5609 covers that file. Not fixed here.
- Review round 2 (2026-09-30, head e962ec0): one consensus NIT (3/6 reviewers), valid and fixed.
- 2026-09-30 18:06–20:35Z: three review runs on head 4d85fe2 (36750314659, 36760218666, 36766420075) failed in `Run reviewer models` with OpenRouter `Insufficient credits`; the fingerprint cap labelled the PR `ai:review-blocked`, and catch-all fixer session_01NMcUjX2WRgsQt2WyVxMeaJ posted a `hold` claim (comment 5921646426). The owner topped up credits, removed the label, and commented /reclarify; the review sweep re-dispatched review (run 36794117965).
- Review round 3 (2026-10-01, head 4d85fe2, run 36794117965): 5 of 6 reviewers reported nothing; the one finding (gpt-6-luna, `review_autofix.yml:4375`) restates the stale failed `review / codex-agent` check of outage run 36750314659 and names no defect in this PR's diff. Rejected (reply on the PR). Closed by a new head per AD-10.
