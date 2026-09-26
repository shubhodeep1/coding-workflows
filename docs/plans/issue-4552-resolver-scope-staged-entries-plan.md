# Resolver scope check: compare staged entries, forbid model staging

Source issue: shubhodeep1/coding-workflows#4552 (https://github.com/shubhodeep1/coding-workflows/issues/4552)
Base branch: claude/quirky-wozniak-e9t88m
Security pass: skip (ai:workflow-heal: automation-produced issue)

## Summary

The review conflict resolver's per-attempt scope check (`_resolver_scope_state`
in `scripts/review_conflict_resolve.sh`) fails the whole review run closed with
a bare `ValueError`. It happens both when the model stages files, which is a real
violation, and when a read-only Git command rewrites index metadata, which is a
false positive. This plan keeps the fail-closed response to real staging. It
makes the check compare staged entries instead of raw index bytes, logs which
check failed, and tells the resolver model not to stage.

## Context

- Issue #4552 (workflow heal, generation 1) was filed after review runs
  `36240577301` and `36241767821` on PR #4549 ended with
  `Resolver scope check failed closed (ValueError)` and
  `Resolver attempt scope cannot be verified; refusing to retry or commit.`
- The issue's diagnosis names two candidates: a Git inspection that refreshes
  index stat metadata without changing entries, or model-initiated staging.
- **Transcript evidence (read while planning):** the resolver model ran
  `git add -- <conflicted files>` in **both** failed runs (`36240577301` and
  `36241767821`), followed by `git diff --cached --check`. It also ran
  `git status --short` several times. `analysis/workflow-optimization-2026-09-26-2.md`
  records the same `git add` pattern in runs `36221714214`, `36221717540`,
  `36222494360`, and `36224174449`. Staging turns the stage 1/2/3 unmerged
  entries into stage 0 entries, and that is the change the check rejected.
- **Local reproduction of the second candidate:** in a repo with an in-progress
  conflicted merge, running `touch <tracked file>; git status` changes the
  SHA-256 of `.git/index` while the SHA-256 of `git ls-files --stage -z`
  (and of `git ls-files -s -v -z`) stays the same. So the raw-byte check also
  fails runs where the model only inspected the tree, even with no staging.
- `_resolver_attempt_state` (same file) already compares
  `git ls-files --stage -z` and rejects changed staged entries. The raw-byte
  comparison in `_resolver_scope_state.merge_state()` is the one inconsistent
  check.
- `_resolver_scope_state` reports only the exception type. Its `ValueError`
  messages are fixed literals written in the function, but the reason is
  dropped, so the logs could not say which check failed (CLAUDE.md §8).
- `prompts/conflict-resolver.txt` never says not to stage. It says "You may
  modify repository files directly", and the model treats `git add` as part of
  resolving.

## Goals

- `_resolver_scope_state` `capture` / `check` / `restore` / `verify` accept an
  attempt where Git only refreshed index metadata (staged entries unchanged).
- The same actions still fail closed (exit 2) when staged entries change
  (`git add`, `git rm --cached`, `git update-index --assume-unchanged`, …) or
  `MERGE_HEAD` changes.
- A failed-closed scope action names the reason when it is one of the
  function's own fixed `ValueError` messages, and never prints paths or
  untrusted exception text.
- The conflict-resolver prompt tells the model not to stage, unstage, commit,
  or otherwise change the Git index or `MERGE_HEAD`, and says the workflow
  stages the result itself.

## Non-goals

- Making staging retryable or auto-resetting the merge index. An agent that
  stages is still unsafe to retry (the existing contract).
- A tool-level `git add` deny in the OpenCode permission config (see AD-3).
- The integration-sync resolver prompt `prompts/integration-sync-conflict-resolver.txt`
  (see AD-4).
- Any change to `_resolver_attempt_state`, `check_resolver_diff.sh`, or the
  final touched-set gate.

## Constraints

- §5 minimal change set. The fix stays inside `_resolver_scope_state`, the
  prompt, and the test file.
- §6 naming immutability. The existing `::error::Resolver scope <action> failed
  closed (<Type>).` line is kept byte-for-byte (monitoring and
  `AUTOFIX_FAILURE_FIRST_ERROR` read it). The reason goes on a new line.
- Issue fix constraint: do not disable or weaken the failing check.
- §9 style: the Python heredoc keeps its existing 4-space indentation.
  Test files use tabs, as the existing file does.
- §20: a `changelog.d/4552-…` fragment (fixed).
- `tests/prompt_size_budget.py`: `conflict-resolver.txt` ≤ 150 lines.

## Approach

1. In `merge_state()`, replace the raw `index` file hash with the SHA-256 of
   `git ls-files -s -v -z`. That covers mode, object id, stage number, path,
   and the `-v` tag (so assume-unchanged / skip-worktree / unmerged flag
   changes still count). `MERGE_HEAD` keeps its raw-byte hash. `ls-files` never
   refreshes or writes the index, so the snapshot does not disturb what it
   measures.
2. In the `except` handler, after the unchanged `::error::` line, print
   `Resolver scope <action> failure reason: <message>` to stderr **only** for
   `ValueError`. Every `ValueError` raised in this heredoc is a fixed string.
   OSError, KeyError, and CalledProcessError text can carry paths, so it stays
   unprinted.
3. Add a rule to `prompts/conflict-resolver.txt`: do not run commands that
   change the Git index or merge state (`git add`, `git rm`, `git restore
   --staged`, `git reset`, `git checkout`, `git stash`, `git commit`,
   `git merge`, `git update-index`). The workflow verifies the edits and stages
   them itself; staging makes the run fail with no commit. Read-only commands
   (`git status`, `git diff`, `git grep`) stay allowed.

Alternative considered: keep the raw-byte check and only add the prompt rule.
Rejected because the reproduction shows harmless `git status` calls, which the
model makes in every run, can still fail it.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue fixes
the scope, and `/implement-issue-claude` always writes one phase.

1. **Phase 1: staged-entry scope check, safe reason, no-staging prompt rule.**
   - Files: `scripts/review_conflict_resolve.sh`, `prompts/conflict-resolver.txt`,
     `tests/test_review_conflict_resolve_retry_prelude_render.py`,
     `changelog.d/4552-resolver-scope-staged-entries.md` [new].
   - Done when: the new tests pass. The existing staging-rejection assertions in
     `test_scope_snapshot_restore_and_index_fail_closed` still pass. The prompt
     budget and prompt hardening tests pass. `bash -n` passes on the script.
   - Rollback: revert the phase PR. No state, config, or data migration.

## Implementation Steps

Phase 1:
1. `scripts/review_conflict_resolve.sh` `_resolver_scope_state`: rewrite
   `merge_state()` so the first element is the SHA-256 of `git ls-files -s -v -z`,
   and update the comment above the function.
2. Same function, `except` block: add the `ValueError`-only reason line.
3. `prompts/conflict-resolver.txt`: add the no-index-changes rule to `Rules:`.
4. `tests/test_review_conflict_resolve_retry_prelude_render.py`:
   - new test: capture, then a tracked file's mtime changes and `git status`
     rewrites the index (assert the raw index hash changed), then `check`
     returns 0;
   - new test: in a real conflicted merge fixture, capture, then `git add` of
     the conflicted path, then `check` returns 2 and stderr names the reason
     `merge index or MERGE_HEAD changed during resolver attempt`;
   - new test: `git update-index --assume-unchanged` after capture, then
     `check` returns 2;
   - new test: the prompt carries the no-staging rule;
   - register all of them in `main()`.
5. `changelog.d/4552-resolver-scope-staged-entries.md` (`fixed`).

## Files & Modules

- `scripts/review_conflict_resolve.sh`
- `prompts/conflict-resolver.txt`
- `tests/test_review_conflict_resolve_retry_prelude_render.py`
- `changelog.d/4552-resolver-scope-staged-entries.md` [new]
- `docs/implement-plan/issue-4552-resolver-scope-staged-entries.md` [new, progress log]

## Tests

- Unit (pytest + direct run): the four new tests above, plus the existing
  scope tests in the same file.
- Existing gates: `tests/prompt_size_budget.py`,
  `tests/test_review_surface_prompt_hardening.py`,
  `tests/test_review_autofix_smoke_conflict_resolver_override.py`,
  `tests/test_render_prompt_foundation.py`, and `bash -n` on the script.

## Risks & Mitigations

- The model ignores the prompt rule and stages anyway → the run still fails
  closed with a named reason (no unsafe commit). ACCEPTED: stronger enforcement
  is AD-3's follow-up path, and the reason line makes recurrences attributable.
- `git ls-files -s -v` misses an index change that matters → it lists every
  entry's mode, oid, stage, path, and flag tag. Index extensions (cache-tree,
  resolve-undo, untracked cache) do not change what gets committed, and the
  touched-set gate and `_resolver_attempt_state` still run afterwards.
- The reason line leaks untrusted text → printed only for `ValueError`, whose
  messages in this heredoc are all literals (a test asserts the format).

## Rollout

Ships on the PR #4549 branch (`claude/quirky-wozniak-e9t88m`, the issue's
target branch) and reaches `main` and consumers with that PR and the next
`@stable` release. Nothing needs to be enabled.

## Auto-decisions

- AD-1 [plan, 2026-09-26] Which fix does the transcript evidence call for? — Picked: A — both: compare staged entries instead of raw index bytes, and add the no-staging prompt rule. Alternatives: B — prompt rule only (the issue's "if staging is confirmed" branch); C — staged-entry comparison only. Why: both failed runs show `git add` (so the prompt rule applies), and a local repro shows `git status` alone changes the raw index bytes (so the comparison fix applies too); neither weakens the fail-closed response to real staging. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-26] How should the failed-closed reason be surfaced? — Picked: A — keep the existing `::error::` line unchanged and add a `failure reason:` line only for `ValueError` (fixed literal messages). Alternatives: B — no new diagnostics; C — append the reason to the existing `::error::` line. Why: §8 diagnostics without changing a line monitored by §6, and without printing paths or untrusted text. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-26] Should staging also be blocked at the tool boundary (OpenCode bash permission deny for `git add` and similar)? — Picked: A — no, prompt rule only in this issue. Alternatives: B — add deny rules to the resolver OpenCode config. Why: §5 minimal change. The issue names the prompt, deny-rule semantics would need their own verification, and the check still fails closed if the model ignores the prompt. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-26] Should the integration-sync resolver prompt get the same rule? — Picked: A — no, only `prompts/conflict-resolver.txt`. Alternatives: B — add it to `prompts/integration-sync-conflict-resolver.txt` too. Why: the issue and the evidence cover only the review resolver prompt (§5). Applied in: no code change. Status: pending review

## References

- Issue #4552; source PR #4549; failed runs 36240577301, 36241767821.
- `analysis/workflow-optimization-2026-09-26-2.md` (resolver scope findings).
