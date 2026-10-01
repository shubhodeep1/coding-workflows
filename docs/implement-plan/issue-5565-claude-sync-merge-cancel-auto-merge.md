# Implement-Plan Log — Merge claude/* PRs synchronously after the hold gate and cancel stale auto-merge

- Plan: docs/plans/issue-5565-claude-sync-merge-cancel-auto-merge-plan.md
- Source issue: shubhodeep1/coding-workflows#5565
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5565-claude-sync-merge-cancel-auto-merge   Final PR: #5574 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5603
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01GcyAp4JGTne7wrHFfj4g66   safety net and hand-back: see the review-round stage report
- Last updated: 2026-10-01
- Last note: review round 3 (head e5636673945b, run 36794137432 after the OpenRouter credit outage): fixed finding 1 (on a merge-queue base `gh pr merge` without `--auto` still enrolls or queues the PR) by merging `claude/*` heads with the REST `PUT pulls/{n}/merge` + `sha` (AD-8; the suggested `--disable-auto` would never merge); rejected finding 2 (the failed `review / codex-agent` check is the outage run, not a defect)

## Phases
1. [ ] Phase 1 — synchronous claude/* merge and stale auto-merge cancellation (scripts/review_enable_auto_merge.sh, review_autofix.yml gate + deterministic-skip-merge, gate docstring, tests, docs) — PR #5603 open (waiting); review rounds: 3; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How does the review workflow merge a `claude/*` head the gate allowed? — Picked: A — a synchronous head-bound merge (`gh pr merge --squash --match-head-commit`, no `--auto`) right after the gate, in both paths. Alternatives: B — keep `--auto` and add hold-triggered cancellation; C — keep `--auto` and accept the window. Why: an enrollment cannot re-run the gate when checks settle (§1). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens when GitHub refuses the synchronous merge? — Picked: A — enroll nothing; log `AUTOFIX_AUTO_MERGE_SKIPPED reason=merge_not_ready`, warn, no ready labels. Alternatives: B — fall back to `--auto`. Why: B reopens the finding's window (§1). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How is an existing enrollment cancelled on a head change? — Picked: A — the gate job of every review run cancels any enrollment on an open `claude/*` PR, reusing its `pulls/{n}` read. Alternatives: B — cancel only in the merge helper; C — no cancellation. Why: only a clean run reaches the helper. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Add a separate cancellation when a hold is posted without a push? — Picked: A — no. Alternatives: B — an `issue_comment`-triggered workflow; C — a job in the hourly catch-all sweep. Why: after AD-1 the workflow never enrolls a `claude/*` PR (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] What if the cancellation call fails? — Picked: A — log `result=failed`, warn, continue. Alternatives: B — fail the gate job. Why: failing the gate does not stop GitHub's armed merge. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Which log keys carry the new outcomes? — Picked: A — `action=squash_sync`, `reason=merge_not_ready`, new key `AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED` registered in `agents.md`. Alternatives: B — a new key per outcome. Why: extends existing audit lines without renames (§6). Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Edit CLAUDE.md §26.H? — Picked: A — no; README and `agents.md` only. Alternatives: B — edit CLAUDE.md and its twin. Why: §5, #5316 AD-8. Applied in: no code change. Status: pending review
- AD-8 [phase 1/1 — review round 3, 2026-10-01] On a base with a merge queue, `gh pr merge` without `--auto` still enables auto-merge or queues the PR, so the "synchronous" `claude/*` merge can outlive the hold gate. How does round 3 close that? — Picked: A — merge `claude/*` heads with the REST `PUT pulls/{n}/merge` (`merge_method=squash`, `sha=<reviewed head>`), which only merges or refuses; after a refusal one `pulls/{n}` read counts a merge whose response was lost; a refusal (merge queue included) logs `merge_not_ready`. Alternatives: B — add `--disable-auto` as the reviewer suggested (gh then only disables auto-merge and never merges); C — keep `gh pr merge`, re-read after it and cancel any enrollment (gh cannot dequeue a queued PR); D — keep it and document "no merge queue on claude/* bases". Why: closes the window by construction with one call (§1, §5), no rename (§6). Applied in: PR #5603 (round 3). Status: pending review

## Lessons
- [source:intervention] A best-effort GitHub mutation in a workflow step (warn and continue on failure) should retry with backoff, re-read the state after each failure so a lost response is not reported as a failure, and put gh's first stderr line in the warning; `>/dev/null 2>&1` hides the cause. (files: .github/workflows/review_autofix.yml)
- [source:intervention] `gh pr merge` without `--auto` is not a synchronous merge: on a base with a merge queue it enables auto-merge or queues the PR. A merge that must not outlive a gate uses the REST `PUT repos/{repo}/pulls/{n}/merge` with `sha`, which only merges or refuses. (files: scripts/review_enable_auto_merge.sh, .github/workflows/review_autofix.yml)

## Notes
- Local verification env (2026-09-30): Python 3.11 container with a scratch venv (pytest, pyyaml, ruff, shellcheck-py) and actionlint 1.7.12. `ruff check` with the repo's pyproject config reports EXE001/BLE001 on scripts/claude_merge_hold_gate.py identically on the base; CI's selection (`--select E,F --ignore E501`) passes. shellcheck SC1007 on scripts/review_enable_auto_merge.sh:36 is pre-existing.
- Security pass: skip (`security_pass_skip.py`: `ai:security` created and labelled by the issue automation).
- Phase 1 has no protected paths (no `.claude/**` edit).
- 2026-09-30 19:43Z blocked: round 3 could not run, the OpenRouter reviewer account was out of credits (runs 36742933136, 36752342910, 36760241990; hold claim by session_01AVCQARvPjaMcEgLyF33mTb). 2026-10-01: the operator topped up credits and answered Q1: A with `/reclarify`; the review sweep re-ran review on the head (run 36794137432), and session_016pg5kEMqrS8hcdfATHAbEr lifted the hold with a `review` claim and handled round 3.
- Issue base is the #5316 project branch (final PR #5323, draft into main): Activation n/a; the final-merge stage closes #5565 with `ai:merged`.
