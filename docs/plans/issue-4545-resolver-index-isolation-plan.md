Source issue: shubhodeep1/coding-workflows#4545 (https://github.com/shubhodeep1/coding-workflows/issues/4545)
Base branch: ai/issue-4512
Security pass: skip (ai:workflow-heal: automation-produced issue)

# Isolate the conflict resolver's model attempt from the real Git index

## Summary
`scripts/review_conflict_resolve.sh`'s source-repo scope check fails closed
whenever the model attempt changes the real Git index (e.g. by running
`git add`), even when the underlying file content stays entirely within the
conflicted set. Give each model attempt a disposable copy of the index via
`GIT_INDEX_FILE` so a model-issued stage/commit can no longer trip that
false positive, and make the prompt explicitly forbid staging/committing.

## Context
Issue #4545 is an automated `ai:workflow-heal` report (generation 1 of 3)
for PR #4516: the resolver's `Internal: Review-Blocked Judge Dispatch`
workflow failed 4 consecutive times with
`Resolver scope check failed closed (ValueError)` /
`Resolver attempt scope cannot be verified; refusing to retry or commit.`
The issue's root-cause analysis (citing
`analysis/workflow-optimization-2026-09-26-2.md`) attributes this to the
model issuing `git add` on its own resolution before the trusted staging
step (`stage_resolver_touched_path_or_fail`, which runs after the model
exits). `_resolver_scope_state`'s `merge_state()` hashes the real
`.git/index` file's bytes as part of its pre/post-attempt snapshot
(`scripts/review_conflict_resolve.sh:555-560`); a `check` action raises
`ValueError("merge index or MERGE_HEAD changed during resolver attempt")`
when that hash differs (`scripts/review_conflict_resolve.sh:583-587`), which
the outer loop treats as unrecoverable (`scope_rc` not `0` or `1`) and
aborts the run (`scripts/review_conflict_resolve.sh:2126-2132`, now
2126+16 after this plan's insertions). Trusted staging happens later, on
the *real* index, and is unaffected
(`scripts/review_conflict_resolve.sh:2613-2695`).

The issue's suggested fix — give the model attempt a disposable index copy
via an invocation-scoped `GIT_INDEX_FILE`, keep the real-index equality
check, keep trusted staging as the only path that writes the real index,
and explicitly forbid staging/committing in the prompt — is adopted
directly; independent reading of the cited line ranges confirms the
diagnosis (the model's working directory, `RESOLVER_OPENCODE_WORKSPACE`,
is the live checkout, not a copy: `scripts/review_conflict_resolve.sh:470`).

## Goals
- A model attempt that edits only in-scope (conflicted) files and stages
  its own resolution no longer trips the "merge index changed" fail-closed
  path.
- The real Git index and `MERGE_HEAD` are never mutated by anything except
  `_resolver_scope_state`'s own capture/restore machinery and the trusted
  post-attempt staging step.
- An edit to a file outside the conflicted set is still rejected exactly as
  before (isolation must not weaken the existing out-of-scope guard).
- The resolver prompt explicitly forbids staging or committing.

## Non-goals
- No change to the worktree-content scope check, the restore/verify retry
  path, or `check_resolver_diff.sh`'s final commit gate.
- No change to consumer-repo (non-`IS_WORKFLOW_SOURCE_REPO`) behavior.
- Not fixing or second-guessing the four prior failed runs directly — this
  is a forward-looking fix; PR #4516 recovers on its own next retry once
  this lands on `ai/issue-4512`.

## Constraints
- §6 naming immutability: no existing env var, function, or log-prefix is
  renamed; only new identifiers (`RESOLVER_SCRATCH_INDEX`,
  `_resolver_prepare_scratch_index`) are introduced, checked for
  collision against the script's existing `RESOLVER_*` / `_resolver_*`
  namespace (none found).
- §9 style: tabs in the shell script (matches existing file); the touched
  Python test file already uses tabs, preserved.
- §5 minimal change set: no unrelated refactor of `_resolver_scope_state`
  or the staging path.
- Fix constraints from the issue body: keep the change minimal and
  backward compatible; never rename/remove existing identifiers, env vars,
  labels, or log prefixes; do not disable, skip, or weaken the failing
  check — the real-index equality check is preserved unchanged, only the
  model's own writes are redirected away from what it compares against.

## Approach
Add `RESOLVER_SCRATCH_INDEX` (a path under `RUNTIME_DIR`) and a small helper
`_resolver_prepare_scratch_index` (seeds the scratch file from the real
index's current bytes, or leaves it absent if the real index does not yet
exist). Call it right after `_resolver_scope_state capture` (source-repo
only), then export `GIT_INDEX_FILE="${RESOLVER_SCRATCH_INDEX}"` only around
the model invocation block (all three `timeout` branches), unsetting it and
removing the scratch file immediately after the model exits and before the
post-attempt `_resolver_scope_state check` runs. This means:
- The model's own `git add`/`git commit` (if it runs one despite the
  prompt change) writes to the scratch copy only.
- `_resolver_scope_state check/restore/verify` always run with the real
  index in effect (env unset before they run), so their existing logic
  and the final `check_resolver_diff.sh` gate are untouched.
- The out-of-scope worktree-content guard (`entries()` diffing tracked +
  untracked files) is unaffected — it does not depend on which index file
  is live, only on `git ls-files` output, which is read after the isolation
  env is unset.

`prompts/conflict-resolver.txt` gets one added Rules bullet explicitly
forbidding `git add` / `git rm --cached` / `git commit` / `git stage` and
any other index-mutating command, framing it as "a separate trusted step
stages and commits your edits." This is defense in depth; the isolation is
what actually contains a model that ignores the rule.

Alternative considered: reset the real index to `HEAD` after the model
exits, before the check. Rejected — this would (a) require re-doing the
merge's auto-merge state from scratch, which the existing trusted-staging
comment block explicitly says NOT to do (`scripts/review_conflict_resolve.sh`
lines documenting "NOT resetting the index preserves git merge's
auto-merged content"), and (b) still requires distinguishing the model's
stray index writes from legitimate merge-state, which `GIT_INDEX_FILE`
isolation sidesteps entirely by never letting the model see the real index.

## Phases & Merge Strategy
Issue mode (CLAUDE.md §28.A) authorises a single-phase plan; the change is
one cohesive fix to one script, one prompt, and their tests.

1. **Phase 1 — Git index isolation for the resolver's model attempt.**
   Files: `scripts/review_conflict_resolve.sh`, `prompts/conflict-resolver.txt`,
   `tests/test_review_conflict_resolve_retry_prelude_render.py`, `agents.md`,
   `changelog.d/4545-resolver-index-isolation.md`.
   Done condition: the new regression test
   (`test_scope_state_git_index_isolation_prevents_false_positive_and_still_guards_worktree`)
   passes, reproducing the false positive without isolation, showing the
   real index stays byte-identical with isolation active, and confirming an
   out-of-scope worktree edit is still rejected; the pre-existing
   `test_scope_snapshot_restore_and_index_fail_closed` regression (which
   exercises `_resolver_scope_state` directly, unaffected by this change)
   still passes; `bash -n scripts/review_conflict_resolve.sh` is clean.
   Rollback: revert the phase PR; the resolver returns to its previous
   (safe, if false-positive-prone) fail-closed behavior.

## Implementation Steps
1. `scripts/review_conflict_resolve.sh`: add `RESOLVER_SCRATCH_INDEX`
   next to the existing `RESOLVER_SCOPE_*` var block.
2. `scripts/review_conflict_resolve.sh`: add `_resolver_prepare_scratch_index`
   immediately after `_resolver_scope_state`'s closing brace.
3. `scripts/review_conflict_resolve.sh`: call the new helper right after
   `_resolver_scope_state capture` in the per-attempt loop, failing closed
   (`exit 1`) if it fails.
4. `scripts/review_conflict_resolve.sh`: export `GIT_INDEX_FILE` around the
   `_run_codex` model-invocation block only, unset + remove the scratch
   file right after, before falling through to the post-attempt scope
   check.
5. `prompts/conflict-resolver.txt`: add the staging/committing prohibition
   to the Rules section.
6. `tests/test_review_conflict_resolve_retry_prelude_render.py`: add
   `_scope_and_scratch_source`, `_run_scope_script`, and the new regression
   test; register it in `main()`.
7. `agents.md`: extend the existing "Integration-sync verifier + bootstrap
   contract" bullet describing the source-repo resolver scope check to
   document the new isolation.
8. `changelog.d/4545-resolver-index-isolation.md`: new fragment per §20.

## Files & Modules
- `scripts/review_conflict_resolve.sh` — edited
- `prompts/conflict-resolver.txt` — edited
- `tests/test_review_conflict_resolve_retry_prelude_render.py` — edited
- `agents.md` — edited
- `changelog.d/4545-resolver-index-isolation.md` — [new]
- `docs/plans/issue-4545-resolver-index-isolation-plan.md` — [new] (this file)
- `docs/implement-plan/issue-4545-resolver-index-isolation.md` — [new] (progress log)

## Tests
- New: `test_scope_state_git_index_isolation_prevents_false_positive_and_still_guards_worktree`
  in `tests/test_review_conflict_resolve_retry_prelude_render.py` (three
  sub-scenarios: reproduce without isolation, pass with isolation + real
  index byte-identical, out-of-scope edit still rejected with isolation
  active).
- Existing, re-verified unaffected: `test_scope_snapshot_restore_and_index_fail_closed`,
  `test_scope_retry_restores_full_attempt_and_keeps_final_gate`,
  `test_scope_symlink_restore_preserves_preexisting_target`,
  `test_scope_feedback_is_available_for_generic_resolver` (all in the same
  file), plus a broader regression sweep across every test file
  referencing `review_conflict_resolve.sh` (thread-reuse, review pipeline
  contract, substate ledger, skip-git-repo-check, force-tick, Semble
  contract, stall guard scripts, reasoning schedule) — all pass.
- Manual: `bash -n scripts/review_conflict_resolve.sh`.

## Risks & Mitigations
- **Risk:** an OpenCode-invoked subprocess strips/overrides `GIT_INDEX_FILE`
  before spawning the model's own shell tool calls. Mitigation: verified
  `scripts/opencode_helpers.sh`'s `opencode_run_cmd` performs no env
  sanitization before exec, so the exported var propagates to every child
  process as with any other inherited shell env var.
- **Risk:** leftover scratch index file from an aborted attempt. Mitigation:
  `_resolver_prepare_scratch_index` removes any stale file before
  re-seeding on every attempt (`rm -f` then re-copy), and the model-block
  cleanup removes it again right after each attempt.
- **Risk:** the prompt change alone (without isolation) would have been
  insufficient, since models don't reliably follow negative instructions.
  ACCEPTED — this is why isolation is the primary mechanism and the prompt
  change is explicitly framed as defense-in-depth in the Approach section.

## Rollout
No feature flag; this is a bug fix to existing resolver behavior gated
entirely on the pre-existing `IS_WORKFLOW_SOURCE_REPO` flag (source repo
only, unchanged). No consumer-repo propagation is needed for this fix
itself (consumer repos never take the `IS_WORKFLOW_SOURCE_REPO=true` path),
but the fixed `scripts/review_conflict_resolve.sh` reaches consumer repos
through their normal per-run bootstrap staging from the reviewed workflow
commit (§14/agents.md "Integration-sync verifier + bootstrap contract"),
same as any other resolver change.

## References
- Issue: https://github.com/shubhodeep1/coding-workflows/issues/4545
- Failing PR: https://github.com/shubhodeep1/coding-workflows/pull/4516
- Failed run: https://github.com/shubhodeep1/coding-workflows/actions/runs/36227396863
- `analysis/workflow-optimization-2026-09-26-2.md` (cited by the issue)

## Auto-decisions
- AD-1 [plan, 2026-09-26] The issue's suggested fix names a specific
  mechanism (`GIT_INDEX_FILE`, keep the real-index check, forbid
  staging in the prompt). Should the plan adopt it as-is, or design an
  alternative isolation mechanism?
  - **A** — Adopt the issue's suggested `GIT_INDEX_FILE` isolation
    mechanism as specified, since it is minimal, keeps every existing
    check intact, and was proposed by the same analysis that diagnosed
    the root cause (RECOMMENDED)
  - **B** — Design an alternative (e.g. a full disposable worktree clone
    for the model attempt)
  - Picked: A. Why: smaller blast radius, no change to
    `RESOLVER_OPENCODE_WORKSPACE`'s existing "model works in the live
    checkout" design, and directly addresses the confirmed root cause
    (index-byte mutation, not worktree-file mutation). Applied in: this
    phase's PR. Status: pending review
