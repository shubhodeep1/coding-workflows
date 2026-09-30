# Decide a stalled review's re-dispatch from a verified workflow run, not from a claim

Source issue: shubhodeep1/coding-workflows#5376 (https://github.com/shubhodeep1/coding-workflows/issues/5376)
Base branch: claude/implement-plan-issue-4985-skip-marker-review-stall
Security pass: skip (ai:security: automation-produced issue)

## Summary

`check_in_status.py --hand-back` sets `stall_redispatched` on a `review-stalled`
verdict whenever a trusted `review` claim exists on the head. The PR's author
counts as trusted, so the author can post a claim, dispatch nothing, and make
the next fixer hold an unreviewed head instead of retrying. This plan decides
`stall_redispatched` from a completed review run that GitHub started from the
default branch for this PR after the head arrived. Claims go back to being
leases only.

## Context

- Security finding #5376 (`STRIDE: Spoofing`, medium, `Refs #3576`) points at
  `.claude/scripts/check_in_status.py:723`, the `redispatched = any(...)` claim
  scan in `_review_stall_verdict`.
- The rule came from issue #4985 (project branch
  `claude/implement-plan-issue-4985-skip-marker-review-stall`, AD-10). A traceless
  gate skip must not loop, so a second stall on a re-dispatched head holds and
  asks. The rule was sound, but the evidence it used can be forged: a claim only
  shows that someone typed a marker.
- The #4985 log's Notes already record a related false hold: an older sweep
  reservation whose fixer never dispatched makes a later fixer hold instead of
  dispatching. Deciding from the run removes that case too.
- `internal-review.yml` names dispatched runs `Internal: AI Review & Autofix
  [pr:<N>]` (issue #4618), and `_is_pr_dispatched_review_run` already binds such
  a run to a PR by that exact title plus `event`, `path` and `head_branch` = the
  default branch. The consumer wrapper `workflow-templates/ai-review.yml` has no
  `run-name`, so its dispatched runs cannot be bound to a PR today.

## Goals

- A trusted `review` claim, live or expired, no longer sets
  `stall_redispatched`. Claims only decide `claimed`, `held` and the cap, as
  before.
- `stall_redispatched` is true only when the review workflow's dispatched-run
  listing holds a run that:
  - is completed and was not cancelled;
  - is a `workflow_dispatch` run of that workflow file;
  - has `head_branch` equal to the PR base repository's default branch;
  - is titled for exactly this PR number;
  - was created at or after the head arrived.
- The head's arrival time cannot be moved earlier by the PR's author. It is the
  later of the head commit's committer date and the earliest `started_at` among
  the head's check runs, which GitHub sets.
- In consumer repos (no `internal-review.yml`), the same checks read
  `ai-review.yml`'s dispatched runs, which the wrapper now titles
  `AI Review [pr:<N>]`. The active-run count uses the same listing, so a
  running re-dispatched review in a consumer is no longer reported as a stall.
- No extra API call in coding-workflows: the stall check reuses the listing the
  active-run count already read (§15).

## Non-goals

- The stall clock (AD-8, committer date for `since` and the 2-hour window) and
  every other `check_in_status.py` verdict stay unchanged.
- Claim trust rules (#4622), the hand-back cap, and `claude_fix_claim.py` stay
  unchanged.
- `review_autofix_sweep.yml`'s dispatch dedupe key (it matches only the
  `internal-review.yml` title) stays unchanged.

## Constraints

- §6: no identifier is renamed or removed. `_review_stall_verdict` keeps its
  `ignore_claim_by` parameter (now unused for the re-dispatch decision). New
  names were checked for uniqueness across the repo.
- §15: coding-workflows pays no new call. A consumer repo pays one more listing
  read (`internal-review.yml` answers 404, then `ai-review.yml`), only on the
  paths that already read the listing.
- §28.C twin-first (interim until #4785): `.claude/**` files are edited only
  through their `workflow-templates/.claude/**` twins. The phase stops for a
  `[claude-twin-sync]` copy.
- §19: every PR uses `Refs #5376`. The final PR targets a non-default base, so
  the final-merge stage closes the issue.
- §20: a `changelog.d/` fragment (`security`).
- §9: tabs in Python, 2-space YAML.

## Approach

1. A shared listing helper `_dispatched_review_runs(repo)` tries each
   `(workflow path, title, runs path)` in `DISPATCHED_REVIEW_SOURCES`:
   `internal-review.yml` first, then the consumer `ai-review.yml`. A 404 means
   that workflow does not exist here, so it moves to the next one. Any other
   failed read raises `ReadError`.
2. `_active_run_count` uses the helper and gains an optional
   `dispatched_listing` dict it fills with what it read, so the stall check
   reuses the listing.
3. `_head_arrival_time(committed, check_runs)` returns the later of the
   committer date and the earliest check-run `started_at`.
4. `_verified_review_redispatch(listing, number, default_branch, arrived)`
   returns the first run that satisfies every goal condition, or None.
   `_review_stall_verdict` gets the head's check runs from
   `check_pr_hand_back`, which already fetched them, and sets
   `stall_redispatched` from the result. When one is found, the reason names
   the run id.
5. `workflow-templates/ai-review.yml` gains
   `run-name: ${{ github.event_name == 'workflow_dispatch' && format('AI Review [pr:{0}]', inputs.pr_number) || '' }}`
   (the `internal-review.yml` pattern: other events keep GitHub's default name).
6. The docs that describe the claim rule are updated to the run rule: the
   script docstrings, `fix-claude-pr.md` step 3 (twin), `agents.md`, and
   `README.md`.

Alternatives considered: keep claims and add the run check (rejected: a forged
claim would still decide); bind by the run's `head_sha` (impossible, since a
dispatched run's `head_sha` is the default branch's commit); use only the
committer date (rejected: the author controls it).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
script change, its tests, the consumer wrapper's run name and the docs form one
behaviour change that is safe to ship together.

1. **Phase 1: verified-run re-dispatch rule.**
   - Files: see [Files & Modules](#files--modules).
   - Protected paths: `.claude/scripts/check_in_status.py` and
     `.claude/commands/fix-claude-pr.md`, edited through their twins.
   - Done when:
     - the new and changed hand-back tests pass against the twin;
     - `tests/test_check_in_status.py`, `tests/test_claude_pr_sweep.py` and the
       ai-review wrapper tests pass;
     - ruff and yamllint are clean on the changed files;
     - only the twin-parity checks stay red until the `[claude-twin-sync]` copy.
   - Rollback: revert the phase PR. `stall_redispatched` returns to the claim
     rule, and the extra `run-name` is harmless.

## Implementation Steps

Phase 1:

1. `workflow-templates/.claude/scripts/check_in_status.py`:
   - add the consumer constants and `DISPATCHED_REVIEW_SOURCES` after
     `DISPATCHED_REVIEW_RUNS_PATH`;
   - add `_dispatched_review_runs`, and use it in `_active_run_count` together
     with the `dispatched_listing` parameter;
   - add `_head_arrival_time` and `_verified_review_redispatch`;
   - in `_review_stall_verdict`, add the `head_check_runs` parameter and replace
     the claim scan with the verified-run check;
   - in `check_pr_hand_back`, pass the head's check runs;
   - update the module docstring (hand-back rules and API budget) and the
     function docstrings.
2. `workflow-templates/ai-review.yml`: add the `run-name` line.
3. `workflow-templates/.claude/commands/fix-claude-pr.md` step 3: describe
   `stall_redispatched` as a completed review run dispatched for the PR after
   this head arrived. Point the fixer at that run's log for the
   `AUTOFIX_GATE_SKIP` line; the reason names the run id.
4. `tests/test_check_in_status_hand_back.py`:
   - load `check_in_status` from the twin;
   - replace the claim-based re-dispatch test with tests for each case:
     - a claim alone is not a re-dispatch (the #5376 exploit);
     - a verified completed run is a re-dispatch, and its id is in the reason;
     - a cancelled run, another PR's run, a run on another branch, a
       `pull_request` run, and a run before the head arrived are not;
     - a backdated committer date cannot pull an old run in;
     - the consumer fallback, with its call count;
     - active consumer dispatched runs count as active.
   Update `tests/test_check_in_status.py` only if a call list changes.
5. `agents.md` and `README.md`: state the run rule where they describe
   `stall_redispatched`.
6. `changelog.d/5376-verify-stall-redispatch-run.md` (`security`).

## Files & Modules

- `workflow-templates/.claude/scripts/check_in_status.py` (twin of `.claude/scripts/check_in_status.py`)
- `workflow-templates/.claude/commands/fix-claude-pr.md` (twin of `.claude/commands/fix-claude-pr.md`)
- `workflow-templates/ai-review.yml`
- `tests/test_check_in_status_hand_back.py`
- `agents.md`
- `README.md`
- `changelog.d/5376-verify-stall-redispatch-run.md` [new]
- `docs/implement-plan/issue-5376-verify-stall-redispatch-run.md` [new] (progress log)

## Rollout & Operational Notes

- The `.claude/` copies take effect after the `[claude-twin-sync]` commit.
  Consumers receive the script and the wrapper together on the next `@stable`
  sync. Until a consumer has both, its dispatched runs carry no PR title,
  `stall_redispatched` stays false there, and a traceless skip is re-dispatched
  once per claim lease. That can repeat but stays bounded by the lease, and it
  errs toward retrying, the direction the finding asks for.
- A head with no check runs falls back to the committer date alone. That case
  is recorded as a residual risk: only an author who also dispatched a real
  review run for an earlier head can use it, and the result is a hold, not a
  skipped review.

## Risks

- A review run created seconds before the first check run starts would not
  count. The result is one extra re-dispatch (a safe retry).
- The 100-run listing window: a qualifying run older than the last 100
  dispatches is missed. The result is one extra re-dispatch.

## Auto-decisions

- AD-1 [plan, 2026-09-30] What proves a stalled head's review was already re-dispatched? — Picked: A — a completed, not-cancelled `workflow_dispatch` run of the review workflow on the default branch, titled for this PR, created at or after the head arrived. Alternatives: B — keep the claim and also require the run; C — any claim by the workflow account only. Why: the issue's recommendation; runs dispatched from the default branch cannot be forged by a PR author, and claims stay leases. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] When did the head arrive? — Picked: A — the later of the committer date and the earliest check-run `started_at` on the head. Alternatives: B — the committer date only; C — the earliest check-run start only. Why: a backdated commit cannot pull an older run in, and a head without check runs still has a time. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Which run conclusions count as a re-dispatch? — Picked: A — every completed conclusion except `cancelled`. Alternatives: B — `success` only; C — every completed conclusion. Why: a cancelled run reviewed nothing, so retry; a failing or skipped run would fail again, so hold and ask as AD-10 intended. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] How do consumer repos, which have no `internal-review.yml`, bind a dispatched run to the PR? — Picked: A — give `workflow-templates/ai-review.yml` the same dispatch-only `run-name` (`AI Review [pr:<N>]`) and fall back to its listing on a 404. Alternatives: B — no binding in consumers, so they always retry; C — keep the claim rule in consumers. Why: B loops once per lease and C keeps the vulnerability. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Should the active-run count also see consumer dispatched runs? — Picked: A — yes, through the same listing helper. Alternatives: B — leave `_active_run_count` on `internal-review.yml` only. Why: under the new rule, a running re-dispatch in a consumer would otherwise be reported as a stall and dispatched again. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-30] Which copy of `check_in_status.py` do the hand-back tests load? — Picked: A — the `workflow-templates/.claude/` twin; `tests/test_check_in_status.py` already asserts the two copies are equal. Alternatives: B — keep loading `.claude/` and accept red new tests until the sync. Why: the twin-first rule says tests of new `.claude/` behaviour read the twin. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-30] Is `CLAUDE.md` §26.H's wording ("a head already re-dispatched is held and asked about") changed? — Picked: A — no; it stays true under the new evidence. Alternatives: B — spell out the run rule in `CLAUDE.md` and its template. Why: §5; `agents.md` and `README.md` carry the detail. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` reported `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
