# Give the conflict-resolver model a private Git index

Source issue: shubhodeep1/coding-workflows#5627 (https://github.com/shubhodeep1/coding-workflows/issues/5627)
Base branch: main
Security pass: skip (ai:workflow-heal: automation-produced issue)

## Summary

The source-repository conflict resolver in `scripts/review_conflict_resolve.sh` fails closed whenever the resolver model runs `git add` on the file it just resolved, because the model shares the live merge index that both attempt-scope guards require to stay byte-identical. Give the model invocation a fresh, model-only copy of the merge index through `GIT_INDEX_FILE` (and stop OpenCode's own snapshot tracking from sharing that copy), so staging inside the model no longer touches the real index while every guard stays fail-closed.

## Context

- PR #5596 (`chore: forward-merge stable into main`, head `auto/forward-merge-stable-36694528358-1`) failed `Internal: AI Review & Autofix` six times at step `Run Codex resolver, validate, stage, commit` with `Resolver scope check failed closed (ValueError).` then `Resolver attempt scope cannot be verified; refusing to retry or commit.` (run 36705168693). The filtered log shows the model staged the permitted conflicted file (`M  .github/workflows/test-and-mark-stable.yml`) just before the check.
- `_resolver_scope_state` (`scripts/review_conflict_resolve.sh:505-637`) hashes the raw `index` and `MERGE_HEAD` bytes at capture and raises `ValueError("merge index or MERGE_HEAD changed during resolver attempt")` at `:585-587` on any change, before it looks at which paths changed; the caller exits 1 at `:2288-2294`. `_resolver_attempt_state` (`:727-867`) rejects any change to `git ls-files --stage` at `:786-811`. Both run only when `IS_WORKFLOW_SOURCE_REPO=true`.
- The model runs through `resolver_opencode_cmd` (`:2226-2237`), an OpenCode `writer` agent with `bash` allowed (`scripts/write_opencode_config.sh`), inside the real checkout, so its `git add` writes the real `.git/index`.
- OpenCode 1.18.23 (the pinned `OPENCODE_VERSION`) runs its snapshot tracking as `git --git-dir <snapshot> --work-tree <worktree> add … / write-tree` with `extendEnv: true` (`packages/opencode/src/snapshot/index.ts:84`), so it inherits `GIT_INDEX_FILE`. With a private index exported to the model process, the snapshot would write its own index into that file and collapse the unmerged entries the model inspects. The config key `snapshot: false` (`packages/core/src/v1/config/config.ts:52`) turns snapshot tracking off.
- `review_autofix.yml` always loads the resolver from protected `main` (or the `stable` tag / a consumer pin): see the `Resolve trusted review support commit` step and the `SCRIPT_REF` identity check before `Checkout workflow support source`. So the fix must reach `main` to unblock PR #5596. See AD-1.

Binding rules: §5 minimal change set, §6 naming immutability (no existing identifier, env var, or log line is renamed or removed), §9 style, §12.C (guards stay fail-closed), §20 changelog fragment, §7 docs update.

## Goals

- A resolver attempt in the source repository in which the model stages only the permitted conflicted file passes both attempt-scope checks. Today it fails closed with `ValueError`.
- The real merge index and `MERGE_HEAD` stay byte-identical across the model invocation unless the model deliberately bypasses the private index. That bypass still fails closed exactly as today (`_resolver_scope_state check` exit 2 → `exit 1`).
- A model that edits or stages an out-of-scope file can never get it into the `[ai-merge-resolve]` commit: the private index is discarded, and the worktree edit is still caught by the existing scope check, restored, and gated by `check_resolver_diff.sh`.
- The model sees the same merge index it sees today (unmerged stages intact) on every attempt, including retries.

## Non-goals

- No change to the consumer-repo resolver path (`IS_WORKFLOW_SOURCE_REPO != true`), where neither attempt-scope guard runs (AD-2).
- No relaxing of either scope guard, `check_resolver_diff.sh`, `verify_resolver_index_complete_or_fail`, or the post-loop touched-set gate.
- No change to `scripts/write_opencode_config.sh` or other OpenCode callers. The snapshot opt-out applies only to the resolver's own config file.
- Not fixing the stale comment in `scripts/workflow_failure_heal_intake.sh` that sends source-repo review/autofix heal issues to the PR head branch (see Notes).

## Constraints

- §6: new identifiers (`RESOLVER_MODEL_INDEX_FILE`, `_resolver_model_index_prepare`, `_resolver_disable_opencode_snapshot`) were checked unique across `scripts/`, `tests/`, and `.github/`. Existing log strings (`Resolver scope … failed closed`, `Resolver attempt scope cannot be verified`) are unchanged.
- §9: the script uses 2-space bash indentation inside the loop and tab-indented Python heredocs; new code matches the surrounding code.
- §12.C: the cheaper fix (an isolated copy of the index for the model) wins over reworking the guards.
- §27: `review_autofix.yml` is not touched.

## Approach

1. **Private model index, per attempt.** After `_resolver_scope_state capture` (so the baseline is already recorded), `_resolver_model_index_prepare` copies the real index (`git rev-parse --git-path index`, made absolute) to `${RUNTIME_DIR}/resolver_model_index` (outside the checkout, so no scope snapshot ever sees it). It deletes any previous copy first, so each attempt starts from the captured merge index, and verifies the copy's sha256 against the source. Then `resolver_opencode_cmd` is prefixed with `env GIT_INDEX_FILE=<copy>`, so only the model process tree gets the variable; the stall-guard, heartbeat, and workspace-safety wrappers and all post-model script steps keep using the real index. If the prepare step fails, the script refuses to invoke the model (`exit 1`), mirroring the existing capture-failure path.
2. **No shared index with OpenCode's snapshot.** Right after the resolver's OpenCode config is written and before `opencode_require_bootstrap` validates it, `_resolver_disable_opencode_snapshot` sets `"snapshot": false` in `RESOLVER_OPENCODE_CONFIG` (atomic rewrite). A failure raises the existing `config_generation` alert and exits 1. The resolver never used snapshot data: no resolver code reads OpenCode session diffs or reverts.
3. **Guards unchanged.** Both checks still read the real index. Staging inside the model now only changes the private copy. A deliberate bypass of the copy still changes the real index and fails closed.

Alternatives considered: tolerating index changes limited to conflicted paths and restoring the index. That weakens the fail-closed guard, which the issue forbids. Exporting `GIT_INDEX_FILE` without the snapshot opt-out lets OpenCode's snapshot overwrite the model's index view. Applying the change in consumer repos too widens the blast radius with no failing guard to fix there (AD-2).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: one issue, one phase, one PR.

1. **Phase 1 — private resolver model index.** Files: `scripts/review_conflict_resolve.sh`, `tests/test_review_conflict_resolve_retry_prelude_render.py`, `agents.md`, `changelog.d/5627-resolver-private-model-index.md`. Done when: the new regression tests pass with the fix and fail on the pre-fix script (`d1e530e`), the whole test file passes (`python3` entry point and pytest), `bash -n` passes on the script, and the related resolver test suites still pass. Rollback: revert the PR. The previous behaviour (shared index, fail-closed on staging) returns, with no state to migrate.

## Implementation Steps

1. `scripts/review_conflict_resolve.sh`, next to the other `RESOLVER_*` runtime paths (~`:489-499`): add `RESOLVER_MODEL_INDEX_FILE="${RUNTIME_DIR}/resolver_model_index"`.
2. Same file, after the OpenCode config writer (~`:475-483`): add `_resolver_disable_opencode_snapshot` and call it only when `IS_WORKFLOW_SOURCE_REPO=true`. On failure call `opencode_emit_failure_alert review_conflict_resolve writer "${MODEL_EDITOR}" 1 config_generation` and `exit 1`, like the writer failure above it.
3. Same file, near `_resolver_scope_state`: add `_resolver_model_index_prepare`, which resolves the absolute real index path, removes the stale copy and its `.lock`, copies it, verifies the sha256, and exports nothing.
4. Same file, in the retry loop after `resolver_opencode_cmd=(…)` (~`:2237`): when `IS_WORKFLOW_SOURCE_REPO=true`, run `_resolver_model_index_prepare`, or `exit 1` with `::error::Cannot prepare the resolver model's private Git index; refusing to invoke model.`, then prefix the command with `env "GIT_INDEX_FILE=${RESOLVER_MODEL_INDEX_FILE}"`.
5. `tests/test_review_conflict_resolve_retry_prelude_render.py`: add the regression tests (see Tests) and register them in `main()`, which is how `ci.yml`, `mark-stable.yml`, and `test-and-mark-stable.yml` run this file.
6. `agents.md` (Integration-sync verifier + bootstrap contract, first bullet): document the private model index and the snapshot opt-out (§7).
7. `changelog.d/5627-resolver-private-model-index.md` `[new]`: a `fixed` fragment (§20).

## Files & Modules

- `scripts/review_conflict_resolve.sh`
- `tests/test_review_conflict_resolve_retry_prelude_render.py`
- `agents.md`
- `changelog.d/5627-resolver-private-model-index.md` [new]
- `docs/plans/issue-5627-resolver-private-model-index-plan.md` [new] (this plan)
- `docs/implement-plan/issue-5627-resolver-private-model-index.md` [new] (progress log)

## Tests

All unit/integration tests run against real temporary Git repositories with a real merge conflict, using the script's own functions extracted from source (the file's existing pattern):

- **Permitted staging passes (regression).** Capture scope state, prepare the private index, and run a stub "model" through the same `env GIT_INDEX_FILE=…` prefix. The stub resolves the conflicted file and runs `git add` on it. `_resolver_scope_state check` returns 0, the attempt-state check returns 0, and the real index still has the unmerged entries. A control run without the private index returns exit 2 from the check (the #5627 failure). On the pre-fix script the test fails because the helper does not exist.
- **Out-of-scope staging cannot reach a commit.** The stub edits and stages a file outside the conflicted set. The real index is unchanged, the scope check returns 1 and lists the path, restore and verify succeed, and after the script's own staging of the permitted path the commit tree carries no out-of-scope change.
- **Bypass still fails closed.** A stub that writes the real index directly (unsetting `GIT_INDEX_FILE`) makes the check return 2.
- **Fresh copy per attempt.** A second prepare after the copy was modified restores it byte-for-byte to the real index.
- **Snapshot opt-out.** `_resolver_disable_opencode_snapshot` sets `snapshot: false` and keeps every other key; malformed JSON fails non-zero and leaves the file untouched.
- **Wiring (source level).** Inside the loop, the prepare call and the `GIT_INDEX_FILE` prefix sit after `_resolver_scope_state capture` and before the model invocation, and both are gated on `IS_WORKFLOW_SOURCE_REPO`. The snapshot opt-out is gated the same way and sits before `opencode_require_bootstrap`.
- **Existing suites** that must stay green: this file (both entry points), `tests/test_review_conflict_resolve_*.py`, `tests/test_check_resolver_diff*.py`, plus `bash -n` and `shellcheck` (if installed) on the script.

## Risks & Mitigations

- The model or OpenCode unsets `GIT_INDEX_FILE` and stages into the real index → the existing guard fails closed, as today. ACCEPTED: this is the intended behaviour.
- A different OpenCode version ignores or rejects `snapshot` → mitigated: the key exists in the pinned 1.18.23 schema, and `opencode_require_bootstrap` pins that version.
- Disabling OpenCode snapshots loses its undo/revert data for the resolver session → ACCEPTED: `opencode run` is non-interactive, and the resolver never used snapshot data.
- Git writes `<copy>.lock` next to the copy → it lives in `RUNTIME_DIR`, outside the worktree, so the scope snapshots never see it. Prepare removes a stale lock.

## Rollout

Ships to `main` through the final PR, and from there to every source-repo review run (the resolver is loaded from protected `main`). Consumer repos receive the script on the next `@stable` sync, but the new code is gated on `IS_WORKFLOW_SOURCE_REPO=true`, so their behaviour is unchanged. Once this is on `main`, PR #5596's next review run can resolve its conflict. Rollback: revert the PR.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which branch should this project build on, given that the issue's `Target branch:` is PR #5596's head `auto/forward-merge-stable-36694528358-1` but `review_autofix.yml` loads `review_conflict_resolve.sh` from protected `main`? — Picked: A — the default branch `main`, so the fix reaches the code the failing workflow runs and can unblock PR #5596. Alternatives: B — the named target branch, as the command's default rule says; the fix would reach `main` only when #5596 merges, which this very failure blocks. Why: the heal intake picked the PR head from a stale assumption (see Notes), and the resolver code is identical on both branches. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-30] Should the private model index apply to every repo or only the workflow source repo? — Picked: A — only when `IS_WORKFLOW_SOURCE_REPO=true`, where the two index-sensitive guards run. Alternatives: B — every repo. Why: smallest blast radius (§5, §12.C), because consumer repos stage from the worktree and have no failing guard. Applied in: Phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How to keep OpenCode's own snapshot git calls, which inherit the environment, from sharing the model's private index? — Picked: A — set `snapshot: false` in the resolver's own OpenCode config (source repo only). Alternatives: B — leave snapshots on and accept a corrupted model view of the index; C — add a `--snapshot` option to the shared `write_opencode_config.sh` (wider change). Why: smallest change that keeps the model's index view correct. Applied in: Phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Where should the regression tests live? — Picked: A — `tests/test_review_conflict_resolve_retry_prelude_render.py`, as the issue asks, registered in its `main()`. Alternatives: B — a new test file wired into `ci.yml`. Why: the file already runs in all three CI workflows and holds the scope-guard tests. Applied in: Phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": true, "label": "ai:workflow-heal", "reason": "ai:workflow-heal: created and labelled by the issue automation"}`.
- `scripts/workflow_failure_heal_intake.sh` (the `source_pr_head` branch of `workflow-defect|inconclusive`) says a source-repo review/autofix run "executes the pull request's own workflow code (review_autofix.yml resolves SCRIPT_REF to github.sha here)". Today `review_autofix.yml` pins `SCRIPT_REF` to protected `main` / `stable` / a consumer pin, so heal issues for this failure class target a branch that cannot carry the fix. Out of scope here (§5); recorded as a lesson and in the report.

## References

- Issue #5627; source PR #5596; failed run https://github.com/shubhodeep1/coding-workflows/actions/runs/36705168693; heal intake run https://github.com/shubhodeep1/coding-workflows/actions/runs/36706071565
- OpenCode v1.18.23: `packages/opencode/src/snapshot/index.ts`, `packages/core/src/v1/config/config.ts`
