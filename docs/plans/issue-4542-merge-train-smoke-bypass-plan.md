Source issue: shubhodeep1/coding-workflows#4542 (https://github.com/shubhodeep1/coding-workflows/issues/4542)
Base branch: stable
Security pass: skip (ai:workflow-heal: automation-produced issue)

# Merge-train gate must not queue release smoke-test PRs

## Summary
`scripts/review_merge_train.sh::_mt_gate` queues an `ai/issue-*` PR whenever its
changed files overlap an older open `ai/issue-*` PR, regardless of whether the
PR is a release smoke test. A queued smoke PR soft-exits without running the
reviewer/editor path, so the smoke fixture's bait marker is never removed, and
`test-and-mark-stable.yml` Phase 4b fails asserting the canary was restored.

## Context
Workflow-heal issue #4542 (generation 1, cap 3) was filed automatically from
`Test & Mark Stable Release` run
[36224773465](https://github.com/shubhodeep1/coding-workflows/actions/runs/36224773465),
step "Phase 4b: Verify editor restored canary (pytest + retry)", failing on
`stable` at `46a3170f54c17a5a11107cebbeebeb223616fc1e`.

Evidence from the failing run:
```text
... idle 0s (timeout in ~60m) — step: Merge-train gate (queue behind older overlapping PRs), head: b1dc74a, reviews: 0, log: 0b
retry run #36225891933: status=completed conclusion=success
E       AssertionError: canary still contains the bait marker — editor failed to remove it
```
and the PR-close payload carried the `ai:merge-queued` label.

At the failing SHA:
- `scripts/review_merge_train.sh::_mt_gate` (lines ~304-397) queues any
  `ai/issue-*` PR whose changed paths overlap an older open `ai/issue-*` PR on
  the same base, and sets `AUTOFIX_STALE_BASE_SKIP=true` in `$GITHUB_ENV`.
- `.github/workflows/review_autofix.yml`'s "Merge-train gate" step
  (~line 4267) runs `review_merge_train.sh gate` before the reviewer panel,
  and consumes `AUTOFIX_STALE_BASE_SKIP` to soft-exit the run without
  reviewers/editor.
- The same workflow's earlier "Detect smoke test and tune LLM settings" step
  (~line 3298-3425) already exports `IS_SMOKE_TEST=true` into `$GITHUB_ENV`
  for a smoke PR (via the `e2e-smoke-test` label, primary signal, or the
  linked issue's `^\[E2E ` title, fallback) — and that step runs **before**
  the merge-train gate step in the same job, so `IS_SMOKE_TEST` is already a
  process-environment variable by the time `_mt_gate` runs.
- `.github/workflows/test-and-mark-stable.yml` Phase 4b (~lines 1616-1621)
  accepts a successful run conclusion as proof the editor ran, without
  checking whether a review actually happened — so a queued-and-soft-exited
  run (`conclusion: success`, no review) satisfies Phase 4b's precondition
  while leaving the canary bait marker in place.

The smoke test exists specifically to exercise the reviewer/editor path; a
queued soft-exit defeats its purpose regardless of which older PR happens to
overlap its canary file at release time. Ordinary (non-smoke) PRs must keep
queuing exactly as before — the merge-train's whole purpose (avoiding O(n²)
resolver churn on overlapping siblings, see the script's header) still
applies to them.

## Goals
- A PR with `IS_SMOKE_TEST=true` in the review-run environment always
  proceeds through `_mt_gate` unqueued (`result=smoke_test_bypass`), with no
  label added, no queue comment posted, and no `AUTOFIX_MERGE_QUEUED` /
  `AUTOFIX_STALE_BASE_SKIP` set — regardless of whether an older open
  `ai/issue-*` PR overlaps its changed files.
- Every existing merge-train behavior for non-smoke PRs (queuing, release,
  the one-shot bypass marker, stale-label retirement) is unchanged.
- A regression test proves the split: an older PR touching the same path
  still queues an ordinary PR but lets an otherwise-identical smoke PR
  proceed.

## Non-goals
- Not touching `test-and-mark-stable.yml` Phase 4b's success-conclusion
  check itself — the fix constraints explicitly call for the gate change,
  and weakening the release check would mask real editor failures on future
  smoke runs.
- Not changing how `IS_SMOKE_TEST` / the `e2e-smoke-test` label is detected
  (`.github/workflows/review_autofix.yml`'s "Detect smoke test" step) —
  only how the merge-train gate consumes the existing signal.
- Not touching the `release` subcommand, the one-shot bypass-marker path, or
  `MERGE_TRAIN_MAX_OLDER_PRS` — none of those are implicated by this defect.

## Constraints
- §5 Minimal change set — add the smoke-test check only; no reformatting or
  unrelated refactors to `_mt_gate` or its helpers.
- §6 Naming immutability — no existing identifier (env var, label, log
  prefix, function name) is renamed or removed; `IS_SMOKE_TEST` is an
  existing identifier this change reads, not introduces.
- §9 Code style — tabs for indentation in the shell script (matches the
  file's existing convention).
- §7 Output requirements — README.md's merge-train documentation (the
  `MERGE_TRAIN_ENABLED` row and the `gate` subcommand table row) is updated
  to describe the new bypass, since this is an observable behavior change.
- §20 CHANGELOG — a `changelog.d/4542-*.md` fragment is required (observable
  failure-mode / behavior change to an existing workflow gate).
- Fix constraints from the issue: keep the change minimal and backward
  compatible; do not rename/remove existing identifiers, env vars, labels,
  or log prefixes; do not disable, skip, or weaken the failing check itself
  (Phase 4b's canary assertion stays exactly as strict).

## Approach
Add an early bypass check inside `_mt_gate()`, immediately after the existing
`ai/issue-*` head-prefix check and before any API calls are spent computing
file overlaps: if `IS_SMOKE_TEST` (case-insensitively `true`/`1`/`yes`/`on`,
matching the same convention `MERGE_TRAIN_ENABLED` already uses) is set,
log `result=smoke_test_bypass action=continue` and return immediately. This
is the smallest change that satisfies the issue's suggested fix: it reads a
signal already exported earlier in the same job, spends zero extra API
calls for the common (non-smoke) case, and leaves every other branch of
`_mt_gate` — the older-PR scan, the queue label/comment, the one-shot bypass
marker, the stale-label release path — completely untouched.

An alternative considered: pass `IS_SMOKE_TEST` through as an explicit
`env:` key on the "Merge-train gate" workflow step instead of relying on
`$GITHUB_ENV` propagation. Rejected: `$GITHUB_ENV` writes are already
visible to every later step in the same job without a step-level `env:`
entry (this is how the same workflow's own later steps consume
`IS_SMOKE_TEST`, e.g. the LLM settings themselves), so adding one would be
a no-op change to the workflow file for no behavioral difference — pure
churn against §5.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for this
standalone issue.

1. **Phase 1 — smoke-test bypass in the merge-train gate.**
   - Files: `scripts/review_merge_train.sh`, `tests/test_review_merge_train.py`, `README.md`, `changelog.d/4542-merge-train-smoke-test-bypass.md`.
   - Done condition: `_mt_gate` returns `result=smoke_test_bypass action=continue` and skips all queuing when `IS_SMOKE_TEST` is truthy, verified by a new regression test (`test_gate_bypasses_queue_for_smoke_test_pr`) that pins an older PR overlapping the same canary path; the full `tests/test_review_merge_train.py` suite (20 → 21 tests) passes.
   - Rollback: revert the single commit; the gate reverts to queuing every overlapping PR including smoke PRs (the pre-fix, buggy-but-previously-shipped behavior).
   - Independently mergeable / complete / production-safe at merge: yes — this is a single self-contained bash change with its own test coverage and doc update; nothing else depends on it landing first, and the repo is fully functional (if still exposed to the original bug) both before and after.

## Implementation Steps
1. `scripts/review_merge_train.sh` — in `_mt_gate()`, add the `IS_SMOKE_TEST` case-insensitive bypass check right after the `MT_PREFIX` head check and before the `own_files` fetch; add the same signal to the file's `Env (gate)` header comment.
2. `tests/test_review_merge_train.py` — add `test_gate_bypasses_queue_for_smoke_test_pr`, modeled on `test_gate_queues_younger_overlapping_pr`, with an older PR overlapping `tests/e2e_smoke_canary.txt` and `IS_SMOKE_TEST="true"` passed to the gate invocation; assert no queue label/comment/env vars are set and no `pulls` API call is logged.
3. `README.md` — extend the `MERGE_TRAIN_ENABLED` row and the merge-train `gate` subcommand table row to document the smoke-test bypass.
4. `changelog.d/4542-merge-train-smoke-test-bypass.md` — new fragment (`<!-- changelog: fixed -->`) describing the fix per §20's structure.

## Files & Modules
- `scripts/review_merge_train.sh` — edit (`_mt_gate` early-return bypass + header comment).
- `tests/test_review_merge_train.py` — edit (new regression test).
- `README.md` — edit (merge-train documentation, two locations).
- `changelog.d/4542-merge-train-smoke-test-bypass.md` — new.

## Data Model / Index Changes
None — no MongoDB collection is touched.

## Tests
- Unit/behavioral: `python3 -m pytest tests/test_review_merge_train.py` — all 21 tests (20 existing + 1 new) pass. The new test drives `scripts/review_merge_train.sh gate` through the existing fake-`gh` harness with `IS_SMOKE_TEST=true` and an older overlapping PR, and asserts `result=smoke_test_bypass`, no `ai:merge-queued` label/comment, no `AUTOFIX_MERGE_QUEUED`/`AUTOFIX_STALE_BASE_SKIP`, and zero `pulls` API calls logged (the bypass returns before any lookup).
- No e2e test is added here: the repo's own release pipeline (`test-and-mark-stable.yml`'s smoke-test phases) is the end-to-end proof that a real smoke PR now gets reviewed instead of queued, and re-running that pipeline is outside this plan's scope (it runs on its own release schedule).

## Risks & Mitigations
- **Risk:** a future PR could spoof `IS_SMOKE_TEST` to dodge the merge train. **Mitigation:** `IS_SMOKE_TEST` is set by `review_autofix.yml` itself from the `e2e-smoke-test` label (applied atomically at PR creation by `implement.yml`, per the existing "Detect smoke test" step's own comment) or the linked issue's anchored `^\[E2E ` title — neither is attacker-controlled PR body/title text, so this plan does not change the trust boundary of that signal, only how the gate consumes it. ACCEPTED as out of scope: hardening `IS_SMOKE_TEST` detection itself is unrelated to this defect.
- **Risk:** the bypass could accidentally also fire for non-smoke PRs if `IS_SMOKE_TEST` leaks from a prior job step in some edge case. **Mitigation:** `IS_SMOKE_TEST` is only ever written by the one "Detect smoke test and tune LLM settings" step, once per job, from the label/title signals above; this plan does not change that step.

## Rollout
No feature flag needed — this is a bug fix to existing, always-on behavior
(`MERGE_TRAIN_ENABLED` defaults `true` already). No migration, no consumer-repo
propagation beyond the normal `@stable` sync (this fix lands directly on
`stable`, per the issue's `Target branch: stable`).

## References
- Issue: https://github.com/shubhodeep1/coding-workflows/issues/4542
- Failing run: https://github.com/shubhodeep1/coding-workflows/actions/runs/36224773465
- Heal intake run: https://github.com/shubhodeep1/coding-workflows/actions/runs/36226863814

## Auto-decisions
None recorded. The issue's suggested fix, evidence, and fix constraints fully
determined the design (early bypass on the existing `IS_SMOKE_TEST` signal);
no ambiguous or under-specified step required a §28 pick.
