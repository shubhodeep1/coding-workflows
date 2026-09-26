Source issue: shubhodeep1/coding-workflows#4538 (https://github.com/shubhodeep1/coding-workflows/issues/4538)
Base branch: claude/claude-issue-pickup-queue-4525
Security pass: skip (ai:workflow-heal: automation-produced issue)

# Plan — Resolver scope guard fails closed on the model's own allowed staging

## Goal

Stop `_resolver_scope_state` in `scripts/review_conflict_resolve.sh` from
failing closed when the conflict-resolver model stages one of its own
allowed conflicted paths (`git add`) before the script's own
validation/staging step runs. This is what happened on PR #4531 (run
36224679302): the model staged both allowed conflicted paths, the
attempt's `check` step raised `ValueError("merge index or MERGE_HEAD
changed during resolver attempt")` because the guard hashed the raw
`.git/index` file (which changes on any `git add`, allowed or not), and
the workflow aborted with "Resolver attempt scope cannot be verified;
refusing to retry or commit."

## Non-goals

- Do not remove or weaken the scope guard or the final `check_resolver_diff.sh`
  commit gate (explicit fix constraint from the issue).
- Do not change `_resolver_scope_state`'s CLI (`capture` / `check` / `restore`
  / `verify`) or its call sites in the retry loop.
- Do not attempt to make `restore`/`verify` tolerate a staged-but-then-reverted
  allowed path; that only matters when a real out-of-scope violation is also
  present in the same attempt, which is a materially different (and untested)
  scenario from the reported incident, and is not required by the fix
  constraints.

## Root cause (confirmed by reading the code)

`merge_state()` returned `[sha256(.git/index bytes), sha256(MERGE_HEAD bytes)]`.
The `check` action (and `restore`/`verify`) raised `ValueError` whenever this
pair differed from the value captured before the model ran. `.git/index`'s
raw bytes change on every `git add`, including a legitimate stage of a path
the resolver was explicitly allowed to touch (`scripts/review_conflict_resolve.sh:555-560,
584-586` before this fix). The model staged the two reported conflicted
paths (`scripts/review_conflict_resolve.sh:2642-2649, 2679-2695` in the
issue's line numbering) before the resolver's own staging step, so this
coarse check tripped on a resolution the scope guard should have accepted.

## Fix

Replace the raw-index-hash comparison with a structural one:

- `index_entries()` reads `git ls-files -z --stage` into `[path, stage, mode,
  blob]` tuples — the same shape `entries()` already uses for the worktree
  side, immune to the index file's own stat-cache noise.
- `merge_state()` now returns `[merge_head_hash, index_entries()]`; `MERGE_HEAD`
  is still compared by raw hash and must never change.
- `_assert_index_transition_allowed(saved_index, current_index, allowed)`
  is a no-op when the index did not change. Otherwise, for every path whose
  index entries differ, it requires: the path is in the allowlist read from
  `CONFLICTED_PATHS_FILE`; the path now has exactly one stage-0 index entry
  (fully resolved, not a lingering unmerged stage 1/2/3); and that entry's
  blob id matches `git hash-object` on the current worktree file (i.e., the
  model staged exactly what it left in the worktree). Any other index
  transition still raises `ValueError`, which fails the action closed exactly
  as before.
- `check` calls this after validating the allowlist and before computing the
  worktree-level `outside` scope violations, so an out-of-scope index-only
  change (invisible to the worktree `entries()` diff) is still caught.
- `restore` and `verify` keep a strict `current_index != saved_index` guard
  (unchanged behavior): they only ever run after `check` already found a real
  worktree scope violation, at which point any index drift — allowed staging
  included — is conservatively treated as unverifiable, matching the existing
  fail-closed contract for those two actions.

## Files

- `scripts/review_conflict_resolve.sh` — `_resolver_scope_state`'s embedded
  Python (`index_entries`, `merge_head`, `merge_state`, `_index_by_path`,
  `_assert_index_transition_allowed`, and the `check`/`restore`/`verify`
  dispatch).
- `tests/test_review_conflict_resolve_retry_prelude_render.py` — three new
  regression cases: an allowed staged resolution now passes `check`; an
  out-of-scope staged path still fails `check`/`restore` with exit 2; a
  staged/worktree content mismatch on an allowed path still fails `check`
  with exit 2.
- `changelog.d/4538-resolver-scope-index-fail-closed.md` — fixed-section entry.

## Tests

- `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_review_conflict_resolve_retry_prelude_render.py`
  (this is also the exact command CI runs in `ci.yml`, `mark-stable.yml`, and
  `test-and-mark-stable.yml`). All existing cases pass unchanged; three new
  cases cover the fix and its negative space.

## Risks

- The new `git hash-object` call runs once per changed allowed path per
  `check` invocation — negligible cost, bounded by the size of the allowlist.
- `index_entries()` reads the whole repo's index (like the pre-existing
  `entries()` reads the whole worktree), so the blast radius for detecting an
  out-of-scope index change is unchanged.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorizes a single-phase plan for a
standalone issue.

1. **Phase 1 — Fix the resolver scope guard's index comparison.** Implement
   the fix above, add the three regression tests, run the test file, add the
   changelog fragment. Done when: the test command above passes, the fix
   constraints (scope/commit gates untouched, no renamed identifiers) are
   verified by re-reading the diff.

## Auto-decisions

- **AD-1** [phase 1, 2026-09-26] This session has no `claude-code-remote` MCP
  tooling available (`create_session`, `get_session`, `send_later`,
  `create_trigger` are all absent), so the `/implement-plan-claude` project-branch
  + draft-final-PR + checker/stage-session chain (conformance, security,
  validation, activation stages) cannot be run. — Picked: **A** — Ship this
  as a single direct PR from `claude/implement-plan-issue-4538-resolver-scope-index-fail-closed`
  against the issue base branch, relying on the repo's own `review_autofix.yml`
  Claude-fixer-mode CI (which triggers on any `claude/`-prefixed head
  independent of session tooling) to review and auto-merge it, and documenting
  that no automated post-merge check-in was armed. (RECOMMENDED) Alternatives:
  **B** — Stop and ask a human to run this from a session with the required
  tooling; **C** — Attempt to hand-simulate the project-branch/draft-final-PR
  two-tier structure without a checker to advance it, leaving a permanently
  stuck draft PR. Why: A ships the fix through the same CI review path every
  other Claude-authored PR in this repo already goes through, and does not
  require an operator to run anything by hand (CLAUDE.md §18); B stalls a
  single-file, well-specified fix for no verifiable benefit; C produces a
  PR that can never complete its own protocol. Applied in: this PR.
