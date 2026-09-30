# Implement-Plan Log — Page through the review wrappers' dispatch runs before the check-in hands a PR to a fixer

- Plan: docs/plans/issue-5521-paginate-check-in-review-dispatch-listing-plan.md
- Source issue: shubhodeep1/coding-workflows#5521 (https://github.com/shubhodeep1/coding-workflows/issues/5521)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5521-paginate-check-in-review-dispatch-listing   Final PR: (opening)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: n/a (base claude/implement-plan-issue-4898-retrigger-dispatch-default-branch)
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project started from issue #5521 (security finding check-in-misses-active-review-beyond-global-page); issue progress comment 5906433067.

## Phases
1. [ ] Phase 1 — paginated, completeness-aware PR-named dispatch listing in the checker — protected paths: `.claude/scripts/check_in_status.py`
   - [ ] twin `check_in_status.py`: per-wrapper windowed listing (`_pr_named_review_wrapper_runs`), `ReadError` on an incomplete listing, `now` threaded through, docstrings
   - [ ] `PR_NAMED_REVIEW_RUNS_PATH` / `DISPATCHED_REVIEW_RUNS_PATH` kept (§6)
   - [ ] tests: existing stubs switched; new `tests/test_check_in_status_dispatch_listing.py` (loads the twin) wired into ci.yml
   - [ ] docs: agents.md, CLAUDE.md §26.C read count, changelog fragment
   - Done: new tests pass against the twin; with the twin copied into `.claude/`, the check-in suites and template parity pass

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which listing replaces the repo-wide page? — Picked: A — per-wrapper `actions/workflows/<wrapper>/runs?event=workflow_dispatch&created=>=<cutoff>`, paginated, as the poller does since #4927. Alternatives: B — per-wrapper with a `status=` filter and no window; C — the repo-wide listing paginated. Why: only a per-wrapper listing cannot be crowded by unrelated dispatches, and it matches the poller and the finding's recommendation. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] How long is the window? — Picked: A — a module constant of 250 minutes. Alternatives: B — read `REVIEW_RUN_MAX_RUNTIME_MINUTES` from the environment; C — 24 hours. Why: the checker sessions and the sweep job do not carry that variable; C costs up to 10 pages per wrapper. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What does an incomplete listing do? — Picked: A — raise `ReadError` (`action: retry`, exit 2), no hand-off. Alternatives: B — count it as one active run (`wait`). Why: unknown is not active; `retry` is how every failed read is already reported. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Where do the new tests live? — Picked: A — new `tests/test_check_in_status_dispatch_listing.py` loading the twin, wired into ci.yml, existing stubs updated. Alternatives: B — add the cases to the existing `.claude`-loading files. Why: twin-first; the new cases pass before the sync. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Correct the "at most six further reads" count in CLAUDE.md §26.C and agents.md? — Picked: A — yes. Alternatives: B — leave both. Why: already off by one since #4926 and now wrong by pages (§7). Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Fix `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh` here? — Picked: A — no; list it in the report. Alternatives: B — fix it in this PR. Why: one issue, one phase; a different caller that fails open by design. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- Security pass skip verified: `security_pass_skip.py` → `{"skip": true, "label": "ai:security", ...}`.
- Stale Routine sweep at start: 6 fired one-shots deleted.
