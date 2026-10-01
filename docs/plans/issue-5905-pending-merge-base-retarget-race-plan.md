# Claude-fixer pending-checks merge: re-check the reviewed base at merge time

Source issue: shubhodeep1/coding-workflows#5905 (https://github.com/shubhodeep1/coding-workflows/issues/5905)
Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
Security pass: skip (ai:security: automation-produced issue)

## Summary

Issue #5147 bound the pending-checks marker to the base the review ran against, and `evaluate` in `scripts/claude_fixer_pending_checks.py` compares that binding with the PR read it makes at the start. The merge happens much later in the same call, after the check-run snapshot, the review-run reads, the comment re-read, and the variable read. Then `scripts/review_enable_auto_merge.sh` re-reads the PR and re-checks only the head before `gh pr merge --auto --match-head-commit`. A PR retargeted inside that window, with the head unchanged, still gets auto-merge into a base nobody reviewed (security finding #5905).

This plan makes the merge helper re-check the reviewed base, from the PR read it already makes right before the merge call. It also makes the sweep re-read the PR once after enabling auto-merge and revoke auto-merge when the reviewed head and base no longer hold. The existing #5147 gate then lets the 30-minute review sweep review the retargeted PR again.

## Context

- Security finding #5905 (`A01:2021-Broken Access Control`, severity high, confidence 9/10), filed by `.github/workflows/security-audit.yml` against the #4900 project branch (`Refs #3576`). Location: `scripts/claude_fixer_pending_checks.py:641`, the `enable_auto_merge` call in `evaluate`.
- `evaluate` (`scripts/claude_fixer_pending_checks.py:552-643`) compares the marker's v2 binding with `base.sha` and sha256 of `base.ref` from its first PR read (lines 595-602). Between that check and the merge call it issues up to about a dozen reads: the check-run snapshot, the review run, five run listings, the comments re-read, and the `ENABLE_AUTO_MERGE` variable.
- `enable_auto_merge` (lines 528-549) runs `scripts/review_enable_auto_merge.sh` with `INITIAL_HEAD_SHA` only. The helper's PR read (`_ORCH_PR_META_JSON`, helper lines 145-160) checks `.head.sha` against `INITIAL_HEAD_SHA` (lines 168-177) and never looks at `.base`.
- GitHub's merge APIs bind only the head: `gh pr merge --match-head-commit` maps to `expectedHeadOid`, and neither `enablePullRequestAutoMerge` nor `mergePullRequest` takes a base. So a base check is a read-compare-act check, and the window between the helper's PR read and the merge call can only be narrowed, not closed atomically.
- When someone without write access switches a PR's base, GitHub disables its auto-merge itself. Someone with write access can merge the PR directly, under the same branch protection that auto-merge waits on, so a retarget by them after auto-merge was enabled gains them nothing they did not already have. The exploit is the sweep authorizing a merge for a base nobody reviewed.
- `.github/workflows/review_autofix.yml` (gate `gate_claude_pending_checks_on_head`, issue #5147) already stops skipping dispatched re-runs when the live marker's binding no longer matches the PR's base. The 30-minute `sweep` job of `.github/workflows/review_autofix_sweep.yml` then reviews the PR again, and that review posts a marker bound to the new base.
- The helper's other caller, the workflow's in-run `Enable auto-merge on PR` step (`review_autofix.yml`, around line 5712), sets no base and stays as it is (AD-4).
- The pending-checks feature exists only on the base branch (project #4900, final PR #4922 still a draft), so nothing in production or in any consumer repo runs this path yet.

## Goals

- G1. `scripts/review_enable_auto_merge.sh` accepts two optional inputs, `REVIEWED_BASE_REF` and `REVIEWED_BASE_SHA`. When either is non-empty, the helper refuses to merge unless `REVIEWED_BASE_SHA` is 40-hex, `REVIEWED_BASE_REF` is non-empty, and both equal `.base.sha` and `.base.ref` of the PR read it already makes before every merge call. A refusal prints `AUTOFIX_AUTO_MERGE_HEAD_BOUND pr=<n> head_sha=<sha> action=refuse reason=base_changed` and a `::warning::`, records no merge-authorization labels, and exits 0 like every other refusal. With both inputs empty, behaviour is unchanged.
- G2. `enable_auto_merge` in `scripts/claude_fixer_pending_checks.py` passes the base it verified against the marker's binding (`base.ref`, `base.sha` from `evaluate`'s PR read). A helper refusal for the base is reported as `base_changed`, not `merge_failed`.
- G3. After the helper reports `Auto-merge enabled.`, `evaluate` re-reads the PR once:
  - head and base still the reviewed pair, or the PR already merged into the reviewed base → `merge_enabled`, as today;
  - head or base changed and the PR not merged → `gh pr merge <n> --repo <repo> --disable-auto` and the new state `merge_revoked` (`merge_revoke_failed` when that call fails);
  - already merged into a base other than the reviewed one → the new state `merged_unreviewed_base`;
  - the re-read fails → the same revoke, so the result fails closed.
- G4. `scripts/claude_pr_sweep.py` prints a `::warning::` for `merge_revoke_failed` and `merged_unreviewed_base`; the other new states are logged by the existing `pending_checks` line.
- G5. Tests reproduce the #5905 exploit (retarget between `evaluate`'s base check and the helper's PR read, same head: no merge call) and cover every G1-G4 rule. Docs and a `changelog.d/` fragment are updated (§7, §20).

## Non-goals

- Binding the workflow's in-run zero-findings auto-merge step to the base (AD-4).
- An event-driven handler for `pull_request: edited`, and an hourly revoke of auto-merge on PRs whose marker went stale after the sweep's own call (AD-2).
- Any change to the marker format, the gate, `.claude/scripts/check_in_status.py`, or any `.claude/**` file.
- Re-validating base-branch commits that land without a retarget (#5147's non-goal, unchanged).

## Constraints

- §1 / §3: security first; a missing or malformed reviewed base, a mismatch, and an unreadable post-merge re-read all fail closed.
- §5: extend the helper's existing PR read and `evaluate`; no new module, workflow, or job.
- §6: no renames. Existing log keys stay; `reason=base_changed` is a new value of the existing `AUTOFIX_AUTO_MERGE_HEAD_BOUND` line. New identifiers were checked for collisions across the repo (`git grep`, none found): the env inputs `REVIEWED_BASE_REF` / `REVIEWED_BASE_SHA`, the `evaluate` states `merge_revoked` / `merge_revoke_failed` / `merged_unreviewed_base`, and the helper functions added to `claude_fixer_pending_checks.py` (to be checked again when named).
- §9: tabs in the Python and shell bodies, as both files use them.
- §15: the base check in the helper adds no call (it reuses the helper's PR read). The post-merge check adds 1 PR read per `merge_enabled` result, plus 1 GraphQL write (the `--disable-auto` mutation) only on a mismatch. The module docstring's budget is updated.
- §18: no new script or schedule; the fresh review keeps coming from the existing `review_autofix_sweep.yml` `sweep` job.
- §19: every PR of this project uses `Refs #5905`; the final PR targets a non-default base, so the final-merge stage closes #5905 explicitly.
- §20: `changelog.d/5905-pending-merge-base-retarget-race.md` (`security` section).
- §27: `review_autofix.yml` is not edited.

## Approach

1. **Helper** (`scripts/review_enable_auto_merge.sh`). Document the two inputs in the header. Right after the existing head check (after line 177, before the forward-merge, orchestrator, and squash branches, so every merge path is covered), when either input is non-empty, read `.base.ref` and `.base.sha` from `_ORCH_PR_META_JSON` and refuse on any mismatch or malformed input (G1). `reviewed_head_is_current_for_labels` is not changed: it only runs on paths that never merge.
2. **Sweep module** (`scripts/claude_fixer_pending_checks.py`). `enable_auto_merge` takes the reviewed base and exports it to the helper. It maps a `reason=base_changed` refusal line to `base_changed`. A new helper re-reads the PR after a successful enable and compares head and base with the reviewed pair. A second new helper runs `gh pr merge --disable-auto` with a timeout. `evaluate` returns the G3 states, and the docstrings list the new states and API budget.
3. **Sweep driver** (`scripts/claude_pr_sweep.py`). Add the G4 warnings and update the docstring's budget sentence.
4. **Re-review.** No new code. After a revoke, the gate sees the live marker bound to a base that is no longer the PR's, stops skipping, and the next 30-minute review sweep reviews the PR again (AD-3).

Alternatives considered: the sweep re-reading the PR itself right before calling the helper (AD-1 B) costs a call and leaves a wider window than the helper's own read; an hourly revoke of stale authorizations (AD-2 B) can fight the in-run auto-merge of a fresh clean review; a new `pull_request: edited` workflow (AD-2 C) adds a workflow and consumer wrappers for a case GitHub already covers for users without write access.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — re-check the reviewed base at merge time and revoke a stale authorization.** Files: see [Files & Modules](#files--modules). Done when:
   - the helper refuses a base mismatch or malformed input, and behaves as before with no base inputs;
   - `evaluate` passes the reviewed base, reports `base_changed` for a helper refusal, and after enabling auto-merge revokes it on a moved head or base or an unreadable re-read;
   - the #5905 retarget-inside-the-window scenario is reproduced in tests with no merge call;
   - the repo's CI suites for these files pass.

   Rollback: revert the PR. The sweep returns to the #5147 behaviour (base checked once, at the start of `evaluate`).

## Implementation Steps

Phase 1:
1. `scripts/review_enable_auto_merge.sh`: header inputs list; the base check after the head check (G1).
2. `scripts/claude_fixer_pending_checks.py`:
   - module docstring (fail-closed list and API budget);
   - `enable_auto_merge` signature and env (backwards-compatible keyword defaults), plus the base-refusal mapping;
   - the post-enable re-read and revoke helpers;
   - the `evaluate` states and docstring.
3. `scripts/claude_pr_sweep.py`: the G4 warnings and the docstring budget sentence.
4. Tests (below), the `README.md` / `agents.md` pending-checks paragraphs, and `changelog.d/5905-pending-merge-base-retarget-race.md`.

## Files & Modules

- `scripts/review_enable_auto_merge.sh`
- `scripts/claude_fixer_pending_checks.py`
- `scripts/claude_pr_sweep.py`
- `tests/test_claude_fixer_pending_checks.py`
- `tests/test_review_autofix_review_pipeline_contract.py` (helper-level cases, if its fake-`gh` runner fits better there)
- `tests/test_claude_pr_sweep.py` (the G4 warnings)
- `README.md`, `agents.md`
- `changelog.d/5905-pending-merge-base-retarget-race.md` [new]

## Tests

Unit / integration (pytest; the existing `ci.yml` steps already run these files):
- Helper, with the fake `gh`:
  - matching `REVIEWED_BASE_*` → the merge call is made;
  - a different `.base.ref`, a different `.base.sha`, only one input set, or a malformed sha → no `gh pr merge` call, the `reason=base_changed` line, and no labels recorded;
  - both inputs empty → today's calls exactly (regression).
- `evaluate`, with the fake `gh` extended to answer successive PR reads differently:
  - the PR is retargeted after `evaluate`'s first read (the #5905 exploit) → `base_changed` and no merge call;
  - retargeted after the helper's enable → `merge_revoked` and one `--disable-auto` call;
  - a failing `--disable-auto` → `merge_revoke_failed`;
  - the head moved after the enable → `merge_revoked`;
  - an unreadable re-read → revoke attempted;
  - already merged into the reviewed base → `merge_enabled`, no revoke;
  - already merged into another base → `merged_unreviewed_base`;
  - the happy path still ends `merge_enabled`, with the one extra PR read counted.
- Sweep: `merge_revoke_failed` and `merged_unreviewed_base` print a `::warning::` line.
- Regression: the full suites of `tests/test_claude_fixer_pending_checks.py`, `tests/test_claude_pr_sweep.py`, `tests/test_review_autofix_review_pipeline_contract.py`, `tests/test_review_autofix_claude_fixer_mode.py`, and `tests/test_check_in_status_hand_back.py`.

## Risks & Mitigations

- A retarget between the helper's PR read and `gh pr merge` (seconds) is still possible. MITIGATED by the post-enable re-read and revoke when the merge is still pending; when GitHub merged at once, `merged_unreviewed_base` makes it loud. The window cannot be closed atomically because the merge APIs take no base. ACCEPTED (AD-2).
- A retarget by a user with write access after the post-enable re-read while auto-merge is still pending. ACCEPTED: that user can merge the PR directly under the same protection, and GitHub disables auto-merge for a retarget by anyone without write access (AD-2).
- A `--disable-auto` failure leaves auto-merge on. MITIGATED by the `::warning::` from the sweep; the next run's gate re-reviews the PR because its marker no longer matches.
- An extra PR read per merge. ACCEPTED: one call, only when the sweep actually enabled auto-merge.

## Rollout

Ships with the #4900 project: this project's final PR merges into its branch, and #4922 carries both into `main`. Consumers get it on the next `@stable` sync. No flag: the change only narrows when an existing, unreleased merge path fires. Rollback: revert the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-10-01] Where is the reviewed base re-checked at merge time? — Picked: A — in `review_enable_auto_merge.sh`, from the PR read it already makes right before the merge call, through optional `REVIEWED_BASE_REF` / `REVIEWED_BASE_SHA` inputs that only the sweep sets. Alternatives: B — a second PR read in `evaluate` just before calling the helper; C — bind the base inside the merge mutation (not possible: GitHub's merge APIs take only the head). Why: the narrowest window with zero new API calls (§15), as the finding recommends; the workflow's caller is unchanged (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-10-01] How is an authorization cancelled when the PR is retargeted after the base check? — Picked: A — re-read the PR once right after enabling auto-merge; on a moved head or base, or an unreadable re-read, run `gh pr merge --disable-auto` (`merge_revoked`), and report an already-made merge into another base as `merged_unreviewed_base`. Alternatives: B — also revoke auto-merge on every hourly sweep for PRs whose live marker no longer matches their base; C — a new workflow on `pull_request: edited` that disables auto-merge on a base change. Why: A closes the window the sweep itself opens. B can revoke the in-run auto-merge of a fresh clean review that posted no new marker, then trigger another review, in a loop. C adds a workflow and consumer wrappers (§5) for a retarget by someone without write access, which GitHub already handles by disabling auto-merge, or by someone with write access, who can merge directly. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-10-01] What triggers the re-review the finding asks for after a retarget? — Picked: A — the existing #5147 gate path: the 30-minute `review_autofix_sweep.yml` dispatch, which the gate no longer skips once the marker's base binding is stale. Alternatives: B — the catch-all sweep dispatches `internal-review.yml` itself after a revoke. Why: no new dispatch or write path (§5, §15, §23.C), and #5147 already wired it. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-01] Should the workflow's in-run zero-findings auto-merge step also pass the reviewed base to the helper? — Picked: A — no, out of scope, as in #5147 AD-7. Alternatives: B — pass `PR_PAYLOAD_FILE`'s base to the in-run step. Why: §5. The finding is the sweep's delayed authorization; the in-run step serves every PR in every repo and runs in the same job as the review, so changing it is a separate, wider change. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-10-01] What does the helper do when only one base input is set, or the sha is malformed? — Picked: A — refuse (`reason=base_changed`). Alternatives: B — ignore the base check unless both are valid. Why: fail closed (§1); the sweep always sets both. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-10-01] What does `evaluate` report for a helper base refusal and for the post-enable outcomes? — Picked: A — reuse `base_changed` for the refusal; add `merge_revoked`, `merge_revoke_failed`, and `merged_unreviewed_base`. Alternatives: B — fold them all into `merge_failed`. Why: operators can tell a security refusal and a revoke from an ordinary merge failure in the `pending_checks` log line; no existing state changes meaning (§6). Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py` verified the skip: `ai:security`, created and labelled by the issue automation (`Refs #3576` names the audit tracker).

## References

- #5905 (this finding), #5147 (base binding of the marker), #5148 (newer-review race), #4900 (pending-checks auto-merge), #4922 (its final PR), #3576 (security audit tracker).
- `docs/completed/issue-5147-bind-pending-merge-to-base-plan.md` on the base branch.
