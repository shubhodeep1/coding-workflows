# Page through the review wrappers' dispatch runs before the check-in hands a PR to a fixer

Source issue: shubhodeep1/coding-workflows#5521 (https://github.com/shubhodeep1/coding-workflows/issues/5521)
Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

`_active_run_count` in `.claude/scripts/check_in_status.py` counts the active review runs dispatched from the default branch for a PR by reading one page of the newest 100 `workflow_dispatch` runs of the whole repository. More than 100 newer, unrelated dispatches push a live review run off that page, so the checker reports "no active run" and can hand the PR to a fixer while its review is still running (security finding `check-in-misses-active-review-beyond-global-page`, medium). This plan makes the checker page through the dispatch runs of the two review wrappers only, within the review-run window, and treat an incomplete listing as a failed read (`action: retry`), which defers the hand-off.

## Context

- Finding location: `.claude/scripts/check_in_status.py:313` on the base branch, `listing = gh_api(PR_NAMED_REVIEW_RUNS_PATH.format(repo=repo))`, where `PR_NAMED_REVIEW_RUNS_PATH = "repos/{repo}/actions/runs?event=workflow_dispatch&per_page=100"` (line 148). The same code is in the `workflow-templates/.claude/scripts/check_in_status.py` twin.
- `_active_run_count` is called by `check_pr` (stuck path), `_check_claude_fixer_pr` (conflict and review hand-off), and `check_pr_hand_back` (the CLAUDE.md §26 checker and `scripts/claude_pr_sweep.py`, §26.H). Each caller hands the PR on when the count is 0.
- Issue #4927 fixed the same exposure in the poller's `_pr_named_review_dispatch_runs` (`scripts/orchestrate_poll_process.sh`): per-wrapper listings of `internal-review.yml` and `ai-review.yml`, `created=>=<now - window>`, paged until the distinct runs read reach `total_count`, at most 10 pages, a first-page 404 meaning the wrapper is absent, and anything else incomplete. Its AD-16 (`docs/implement-plan/issue-4927-paginate-review-dispatch-runs.md`) recorded that the checker kept the single page; this issue is that follow-up.
- A run's `path` and `display_title` bind it to its PR (`PR_NAMED_REVIEW_DISPATCHES`, `_is_pr_named_review_pair`, issues #4618, #4701, #4926). That match is unchanged.

## Goals

- `_active_run_count` reads, for each wrapper in `PR_NAMED_REVIEW_DISPATCHES`, `repos/{repo}/actions/workflows/<wrapper>/runs?event=workflow_dispatch&created=>=<cutoff>&per_page=100&page=<p>`, where `<cutoff>` is `now` minus 250 minutes, and counts the active runs whose `(path, display_title)` pair names the PR.
- The listing is complete only when, for each wrapper, the distinct run ids read reach the latest page's `total_count`, or the wrapper's first page answers HTTP 404 (wrapper absent). A failed read, a malformed page, a short page before `total_count` is reached, or more than 10 pages raises `ReadError`, so `main` prints `action: retry` (exit 2) and no hand-off happens on that check.
- The head-branch reads, the pair match, and every verdict other than the one above are unchanged. `DISPATCHED_REVIEW_RUNS_PATH` and `PR_NAMED_REVIEW_RUNS_PATH` stay defined (§6).
- Tests cover a live run beyond the first 100, a second page, a shifted listing, truncation, a 404 wrapper, a non-404 failure, the cutoff in the query, and the call count.

## Non-goals

- `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh` (the review workflow's retrigger peer check) reads the same single page. It is a different file and caller and fails open by design; it stays out of this issue (one issue, one phase) and is listed in the report.
- No change to the pair match, the head-branch reads, the claim logic, or routing.

## Constraints

- §6: no identifier is renamed or removed; `_active_run_count` and `_check_claude_fixer_pr` gain an optional `now` keyword argument only. New names (`PR_NAMED_REVIEW_WRAPPER_RUNS_PATH`, `PR_NAMED_REVIEW_WINDOW_MINUTES`, `_pr_named_review_wrapper_runs`) were checked for collisions.
- §15: REST only. The listing is issued only after the head-branch reads found nothing (as today). In coding-workflows it is about 3 calls (two `internal-review.yml` pages, one `ai-review.yml` 404), measured by the poller in #4927. The module docstring states the budget.
- §28.C / protected paths: `.claude/scripts/check_in_status.py` is under `.claude/**`, so the phase runs twin-first (edits only `workflow-templates/.claude/scripts/check_in_status.py`) and stops on the twin-sync blocker.
- §9 tabs; §20 changelog fragment; §7 `agents.md` and CLAUDE.md §26.C read-count wording.

## Approach

Mirror the poller's #4927 listing in Python. A new helper `_pr_named_review_wrapper_runs(repo, workflow_file, cutoff)` pages one wrapper's window, de-duplicates by run id, checks completeness against the latest `total_count`, and raises `ReadError` for every incomplete case. `_active_run_count` computes the cutoff from `now` (default: the current UTC time), calls the helper for each distinct wrapper file in `PR_NAMED_REVIEW_DISPATCHES`, and counts active pair matches as before. Callers pass their `now` through so tests are deterministic.

Alternatives: see AD-1 and AD-3.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — paginated, completeness-aware PR-named dispatch listing in the checker.** Files: `workflow-templates/.claude/scripts/check_in_status.py` (twin-first; `.claude/scripts/check_in_status.py` by `[claude-twin-sync]`), `tests/test_check_in_status.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_check_in_status_dispatch_listing.py` [new], `.github/workflows/ci.yml`, `agents.md`, `CLAUDE.md`, `changelog.d/5521-check-in-paginated-review-dispatch-listing.md` [new]. Done: the new test file passes against the twin; with the twin copied into `.claude/`, the check-in suites and template parity pass. Rollback: revert the PR.
   protected paths: `.claude/scripts/check_in_status.py`

## Implementation Steps

1. Twin `check_in_status.py`: add `PR_NAMED_REVIEW_WINDOW_MINUTES = 250` and `PR_NAMED_REVIEW_WRAPPER_RUNS_PATH`; mark `PR_NAMED_REVIEW_RUNS_PATH` as kept but no longer read.
2. Add `_pr_named_review_wrapper_runs`; rewrite the PR-named branch of `_active_run_count` to use it; add the `now` keyword to `_active_run_count` and `_check_claude_fixer_pr` and pass it from `check_pr`, `_check_claude_fixer_pr`, and `check_pr_hand_back`.
3. Update the module docstring's API budget and `_active_run_count`'s docstring.
4. Tests: switch the existing stubs to the per-wrapper listing; add the new file with the goal cases, loading the twin; add it to the ci.yml check-in step.
5. Docs: `agents.md` (listing description, read count), CLAUDE.md §26.C step 1 read count, changelog fragment.

## Files & Modules

- `workflow-templates/.claude/scripts/check_in_status.py`
- `.claude/scripts/check_in_status.py` (by the supervising session's `[claude-twin-sync]` copy)
- `tests/test_check_in_status.py`, `tests/test_check_in_status_hand_back.py`
- `tests/test_check_in_status_dispatch_listing.py` [new]
- `.github/workflows/ci.yml`
- `agents.md`, `CLAUDE.md`
- `changelog.d/5521-check-in-paginated-review-dispatch-listing.md` [new]

## Tests

- New (loads the twin, green before the sync): live run on page 2 of 2 counted; 100 unrelated runs plus the live run; `ai-review.yml` 404 is complete and empty; non-404 failure, malformed page, short page before `total_count`, and more than 10 pages all raise `ReadError`; `main` prints `action: retry` and exit 2 for an incomplete listing on a hand-back; the cutoff in the query is `now - 250 min`; head-branch activity skips the listing.
- Updated: the existing stubs in `tests/test_check_in_status.py` and `tests/test_check_in_status_hand_back.py` serve the per-wrapper listing. They load `.claude/`, so they and `test_template_matches_live_script` stay red until the twin sync.
- Verification: run the check-in suites against a scratch tree with the twin copied into `.claude/`, plus `ruff` if available.

## Risks & Mitigations

- A burst so large that a wrapper's 250-minute window exceeds 1,000 runs keeps the listing incomplete → `retry` every check. ACCEPTED — that fails toward waiting; the §26 dead-man's switch and the implement-plan checker's third-retry `block` stage surface it.
- A review run still active more than 250 minutes after it was created is not counted. ACCEPTED — the review job's budget is 240 minutes, the same window the poller uses.
- `retry` on the implement-plan checker routes to a block stage after three consecutive failures. ACCEPTED — same as any other failed read today.

## Rollout

Ships to the base project branch, then with that project to `main`, and to consumers through the `.claude/` sync. No flag; rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which listing replaces the repo-wide page? — Picked: A — per-wrapper `actions/workflows/<wrapper>/runs?event=workflow_dispatch&created=>=<cutoff>`, paginated, as the poller does since #4927. Alternatives: B — per-wrapper with a `status=` filter and no window (4 to 6 calls per check); C — the repo-wide listing paginated. Why: only a per-wrapper listing cannot be crowded by unrelated dispatches, and it matches the poller and the finding's recommendation. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] How long is the window? — Picked: A — a module constant of 250 minutes (the poller's `REVIEW_RUN_MAX_RUNTIME_MINUTES` default; the review job budget is 240). Alternatives: B — read `REVIEW_RUN_MAX_RUNTIME_MINUTES` from the environment; C — 24 hours. Why: the checker sessions and the sweep job do not carry that variable, so B adds a knob nobody sets; C costs up to 10 pages per wrapper. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What does an incomplete listing do? — Picked: A — raise `ReadError`, so the verdict is `action: retry` (exit 2) and no hand-off happens. Alternatives: B — count it as one active run (`wait`). Why: unknown is not active; `retry` is how every failed read is already reported, and it is visible. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Where do the new tests live? — Picked: A — a new `tests/test_check_in_status_dispatch_listing.py` that loads the twin, added to ci.yml's check-in step, with the existing stubs updated. Alternatives: B — add the cases to the existing `.claude`-loading files. Why: twin-first; the new cases pass before the sync (#4886 AD-8 precedent). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Correct the "at most six further reads" count in CLAUDE.md §26.C and agents.md? — Picked: A — yes, name the bounded dispatch listing instead of a fixed count. Alternatives: B — leave both. Why: the count was already off by one since #4926 and would now be wrong by pages; stale docs mislead (§7). Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Fix `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh` (same single page) here? — Picked: A — no; list it in the report. Alternatives: B — fix it in this PR. Why: one issue, one phase; a different file and caller that fails open by design. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #5521; finding tracker #3576.
- #4927 (poller fix) and its log's AD-16; #4926, #4701, #4618 (PR-named review dispatches).
