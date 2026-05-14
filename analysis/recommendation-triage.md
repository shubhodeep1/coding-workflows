# Recommendation triage ledger

Grounding note: statuses below are based on current-repo verification on this ref, not on historical report wording. Source-doc lists under each recommendation name the canonical docs used for the deduped change contract; repeated restatements are intentionally collapsed.

## Status legend
- `already_satisfied` — current repo already carries the fix/behavior; do not reopen unless a newer regression report proves drift.
- `safe_to_apply` — small local cleanup that preserves current contracts and is worth a downstream issue.
- `risky_defer` — real finding, but rollout touches retry/race/pagination/label-contract semantics and should not be forced into a cleanup-only issue.
- `obsolete` — superseded by a current compatibility or architecture choice; do not treat it as a live cleanup target.

## Source-doc inventory
Reviewed 66 required docs on this ref, in sorted glob order:
- `analysis/workflow-optimization-2026-04-21.md`
- `analysis/workflow-optimization-2026-04-28-2.md`
- `analysis/workflow-optimization-2026-04-28-3.md`
- `analysis/workflow-optimization-2026-04-28.md`
- `analysis/workflow-optimization-2026-04-29-2.md`
- `analysis/workflow-optimization-2026-04-29-3.md`
- `analysis/workflow-optimization-2026-04-29-4.md`
- `analysis/workflow-optimization-2026-04-29-5.md`
- `analysis/workflow-optimization-2026-04-29.md`
- `analysis/workflow-optimization-2026-04-30-2.md`
- `analysis/workflow-optimization-2026-04-30.md`
- `analysis/workflow-optimization-2026-05-01-2.md`
- `analysis/workflow-optimization-2026-05-01-3.md`
- `analysis/workflow-optimization-2026-05-01-4.md`
- `analysis/workflow-optimization-2026-05-01-5.md`
- `analysis/workflow-optimization-2026-05-01-6.md`
- `analysis/workflow-optimization-2026-05-01-7.md`
- `analysis/workflow-optimization-2026-05-01-8.md`
- `analysis/workflow-optimization-2026-05-01.md`
- `analysis/workflow-optimization-2026-05-02-2.md`
- `analysis/workflow-optimization-2026-05-02-3.md`
- `analysis/workflow-optimization-2026-05-02-4.md`
- `analysis/workflow-optimization-2026-05-02-5.md`
- `analysis/workflow-optimization-2026-05-02-6.md`
- `analysis/workflow-optimization-2026-05-02-7.md`
- `analysis/workflow-optimization-2026-05-02-8.md`
- `analysis/workflow-optimization-2026-05-02-9.md`
- `analysis/workflow-optimization-2026-05-02.md`
- `analysis/workflow-optimization-2026-05-03-2.md`
- `analysis/workflow-optimization-2026-05-03-3.md`
- `analysis/workflow-optimization-2026-05-03-4.md`
- `analysis/workflow-optimization-2026-05-03.md`
- `analysis/workflow-optimization-2026-05-04-2.md`
- `analysis/workflow-optimization-2026-05-04-3.md`
- `analysis/workflow-optimization-2026-05-04-4.md`
- `analysis/workflow-optimization-2026-05-04-5.md`
- `analysis/workflow-optimization-2026-05-04-6.md`
- `analysis/workflow-optimization-2026-05-04.md`
- `analysis/workflow-optimization-2026-05-05-2.md`
- `analysis/workflow-optimization-2026-05-05-3.md`
- `analysis/workflow-optimization-2026-05-05-4.md`
- `analysis/workflow-optimization-2026-05-05.md`
- `analysis/workflow-optimization-2026-05-06-2.md`
- `analysis/workflow-optimization-2026-05-06-3.md`
- `analysis/workflow-optimization-2026-05-06-4.md`
- `analysis/workflow-optimization-2026-05-06-5.md`
- `analysis/workflow-optimization-2026-05-06.md`
- `analysis/workflow-optimization-2026-05-07-2.md`
- `analysis/workflow-optimization-2026-05-07-3.md`
- `analysis/workflow-optimization-2026-05-07-4.md`
- `analysis/workflow-optimization-2026-05-07-5.md`
- `analysis/workflow-optimization-2026-05-07-6.md`
- `analysis/workflow-optimization-2026-05-07.md`
- `analysis/workflow-optimization-2026-05-08-2.md`
- `analysis/workflow-optimization-2026-05-08-3.md`
- `analysis/workflow-optimization-2026-05-08.md`
- `analysis/workflow-optimization-2026-05-09-2.md`
- `analysis/workflow-optimization-2026-05-09.md`
- `analysis/workflow-optimization-2026-05-10-2.md`
- `analysis/workflow-optimization-2026-05-10-3.md`
- `analysis/workflow-optimization-2026-05-10.md`
- `analysis/workflow-optimization-2026-05-11-2.md`
- `analysis/workflow-optimization-2026-05-11.md`
- `analysis/workflow-optimization-2026-05-14.md`
- `analysis/plan-workflow-log-analysis.md`
- `analysis/e2e-smoke-failure-25126757724.md`

## Downstream local issue IDs
| Local ID | Intended use after this triage |
|---|---|
| `apply-safe-review-autofix-cleanups` | Reserved; no review_autofix-only cleanup survived conservative triage. |
| `apply-safe-e2e-smoke-cleanups` | `capture-issue-url-from-create-response` |
| `apply-safe-issue-pr-status-cleanups` | `collapse-duplicate-linked-issue-graphql-read` |
| `apply-safe-orchestrate-poll-cleanups` | `internal-wrapper-stop-passing-deprecated-caller-workflow` |
| `apply-safe-internal-review-cleanups` | `internal-review-lazy-default-branch-lookup` |
| `apply-safe-implement-diagnose-cleanups` | `implement-diagnose-single-issue-cache-miss-fetch` |
| `apply-safe-shared-helper-cleanups` | Reserved; remaining shared-helper findings change retry/pagination semantics. |
| `apply-safe-plan-clarify-cleanups` | Reserved; remaining plan/clarify findings are prompt-shaping or pagination-sensitive. |
| `apply-safe-misc-cleanups` | Reserved; no extra misc-only cleanup survived conservative triage. |

## Recommendations by subsystem

### Plan / clarify

#### `checkout-before-setup-uv`
- Summary: Keep checkout ahead of UV/tool-cache setup; the cache warnings from the old plan log analysis are not current-repo behavior.
- Source docs: `analysis/plan-workflow-log-analysis.md`
- Status: `already_satisfied`
- Current-repo evidence: `.github/workflows/plan.yml` now performs repository checkout before the later `astral-sh/setup-uv@v3` bootstrap.

#### `plan-no-progress-status-output`
- Summary: Keep plan output final-only; do not reopen status/progress-chatter fixes.
- Source docs: `analysis/plan-workflow-log-analysis.md`, `analysis/workflow-optimization-2026-04-28-3.md`
- Status: `already_satisfied`
- Current-repo evidence: `.github/workflows/plan.yml` explicitly tells the planner: `Do NOT emit progress/status messages ... Output ONLY the final plan or clarification result.`

#### `clarify-comment-history-fetch-collapse`
- Summary: Clarify still reads issue comments twice when semantic cache is enabled; any consolidation changes bounded-context and pagination behavior.
- Source docs: `analysis/workflow-optimization-2026-05-01-4.md`, `analysis/workflow-optimization-2026-05-02-5.md`
- Status: `risky_defer`
- Rationale: `.github/workflows/clarify.yml` intentionally keeps one bounded `per_page=50` JSON snapshot for prompt context and one paginated history render for semantic-cache/thread-history use. Collapsing them is real cleanup work, but it changes prompt shaping and fail-open semantics.

### Review / autofix

#### `review-autofix-sweep-paginated-jq-merge`
- Summary: Keep the paginated `jq -s '(add // [])'` merge fix; it is already in place and should not be reopened.
- Source docs: `analysis/workflow-optimization-2026-05-14.md`
- Status: `already_satisfied`
- Current-repo evidence: `.github/workflows/review_autofix_sweep.yml` already merges paginated PR pages with `jq -s '(add // [])'` before client-side filtering.

#### `no-pr-claude-review-light-profile`
- Summary: No-PR `claude/*` review runs already have the reduced reviewer/two-pass profile that older reports kept asking for.
- Source docs: `analysis/workflow-optimization-2026-05-04-2.md`, `analysis/workflow-optimization-2026-05-07-2.md`, `analysis/workflow-optimization-2026-05-09-2.md`
- Status: `already_satisfied`
- Current-repo evidence: `.github/workflows/review_autofix.yml` has a dedicated `Use lightweight reviewer profile for no-PR claude-branch-review` step that sets `ENABLE_REVIEWER_TWO_PASS=false` and `REVIEWER_REASONING_EFFORT=low`.

#### `editor-noop-blocks-conflict-resolver`
- Summary: Empty-editor/noop runs already stop before merge-conflict preparation/resolution.
- Source docs: `analysis/e2e-smoke-failure-25126757724.md`, `analysis/workflow-optimization-2026-05-04-2.md`, `analysis/workflow-optimization-2026-05-05-3.md`
- Status: `already_satisfied`
- Current-repo evidence: `.github/workflows/review_autofix.yml` gates merge-conflict detection/prep/resolver on `env.EDITOR_NOOP_SUSPICIOUS != 'true'`.

#### `shared-strict-linked-issue-extractor`
- Summary: Review/autofix, judge, and issue-status paths should eventually share one strict PR-text linked-issue extractor.
- Source docs: `analysis/workflow-optimization-2026-05-14.md`
- Status: `risky_defer`
- Rationale: the divergence is real, but this touches validate dispatch, phase-label mutation, and linked-issue semantics across multiple workflows/scripts. That needs one shared-helper rollout with fixture coverage, not a narrow cleanup issue.

#### `review-context-helper-and-fallback-label-batching`
- Summary: `review_autofix` should eventually consolidate PR context hydration and fallback issue-label lookups onto the shared helper path.
- Source docs: `analysis/workflow-optimization-2026-05-06-5.md`, `analysis/workflow-optimization-2026-05-10-2.md`
- Status: `risky_defer`
- Rationale: current `review_autofix.yml` staging and `gh_pr_with_all_comments`/GraphQL-helper behavior are close but not field-for-field identical. Pagination, fallback, and empty/partial-response parity should be proven before changing hot review paths.

#### `active-editor-model-rollout-away-from-legacy-split`
- Summary: Older “move active editor paths off legacy `gpt-5.3-codex`” recommendations are already closed out.
- Source docs: `analysis/workflow-optimization-2026-05-06-3.md`, `analysis/workflow-optimization-2026-05-07-5.md`
- Status: `already_satisfied`
- Current-repo evidence: `.github/workflows/implement.yml` and `.github/workflows/review_autofix.yml` both default `MODEL_EDITOR` to `openai/gpt-5.4`, so the historical editor-model split is no longer the active runtime path.

### Stable-release E2E / `test-and-mark-stable`

#### `empty-editor-shortcut-and-phase4b-gating`
- Summary: The empty-editor smoke-test shortcut and clean-review gate are already present; do not reopen them as actionable.
- Source docs: `analysis/e2e-smoke-failure-25126757724.md`, `analysis/workflow-optimization-2026-05-02-8.md`, `analysis/workflow-optimization-2026-05-03-2.md`
- Status: `already_satisfied`
- Current-repo evidence: `.github/workflows/test-and-mark-stable.yml` short-circuits on the live `EDITOR_NOOP_SUSPICIOUS` marker and only runs Phase 4b when `wait-review` completed cleanly.

#### `dead-commits-after-fetch-removed`
- Summary: The old dead `COMMITS_AFTER` API fetch is already gone.
- Source docs: `analysis/workflow-optimization-2026-05-03-2.md`, `analysis/workflow-optimization-2026-05-04-6.md`
- Status: `already_satisfied`
- Current-repo evidence: `.github/workflows/test-and-mark-stable.yml` now validates bait removal through `fetch_pr_head_sha`; there is no remaining `COMMITS_AFTER` read in the workflow.

#### `capture-issue-url-from-create-response`
- Summary: Reuse the issue-creation response in the smoke harness instead of immediately re-reading the same issue for `html_url`.
- Source docs: `analysis/workflow-optimization-2026-04-30.md`, `analysis/workflow-optimization-2026-05-01-8.md`, `analysis/workflow-optimization-2026-05-11-2.md`
- Status: `safe_to_apply`
- Downstream local ID(s): `apply-safe-e2e-smoke-cleanups`
- Why safe now: `.github/workflows/test-and-mark-stable.yml` still does `POST /issues` then `GET /issues/{issue_number}` only to recover `html_url` in the same step, with no intervening mutation.

### Issue / PR state sync and internal review

#### `collapse-duplicate-linked-issue-graphql-read`
- Summary: Enrich the first `closingIssuesReferences` query with `labels/body`, then keep any second lookup only for branch-added issue numbers or malformed GraphQL responses.
- Source docs: `analysis/workflow-optimization-2026-04-28-2.md`, `analysis/workflow-optimization-2026-05-07-5.md`, `analysis/workflow-optimization-2026-05-10-3.md`
- Status: `safe_to_apply`
- Downstream local ID(s): `apply-safe-issue-pr-status-cleanups`
- Why safe now: `.github/workflows/issue_pr_status.yml` already performs both GraphQL reads in one step against the same PR. A scoped consolidation can preserve the existing branch-derived delta handling and REST fallback for malformed/partial GraphQL responses.

#### `internal-review-lazy-default-branch-lookup`
- Summary: Do not fetch `repository.default_branch` on the early-exit “existing PR already open” path.
- Source docs: `analysis/workflow-optimization-2026-05-04.md`, `analysis/workflow-optimization-2026-05-07-4.md`, `analysis/workflow-optimization-2026-05-10-3.md`
- Status: `safe_to_apply`
- Downstream local ID(s): `apply-safe-internal-review-cleanups`
- Why safe now: `.github/workflows/internal-review.yml` still reads `base_ref` before it knows whether `proceed=false`; moving that lookup below the existing-PR early exit is local and behavior-preserving.

### Implement / diagnose

#### `implement-diagnose-single-issue-cache-miss-fetch`
- Summary: On cache miss, fetch the issue JSON once and derive both labels and body from that payload.
- Source docs: `analysis/workflow-optimization-2026-05-14.md`
- Status: `safe_to_apply`
- Downstream local ID(s): `apply-safe-implement-diagnose-cleanups`
- Why safe now: `scripts/implement_diagnose_post_codex_failure.sh` still reads `GET /issues/{n}` once for labels and again for body on the same straight-line cache-miss path, with no intervening mutation.

### Orchestrate / poll

#### `internal-wrapper-stop-passing-deprecated-caller-workflow`
- Summary: Stop the internal wrapper from passing the deprecated `caller_workflow` no-op input.
- Source docs: `analysis/workflow-optimization-2026-05-03-2.md`
- Status: `safe_to_apply`
- Downstream local ID(s): `apply-safe-orchestrate-poll-cleanups`
- Why safe now: `.github/workflows/orchestrate_poll.yml` explicitly documents `caller_workflow` as a deprecated ignored input, but `.github/workflows/internal-orchestrate-poll.yml` still passes it.

#### `remove-caller-workflow-input-entirely`
- Summary: Do not delete the reusable `caller_workflow` input itself just because the internal wrapper no longer needs it.
- Source docs: `analysis/workflow-optimization-2026-05-03-2.md`
- Status: `obsolete`
- Rationale: `.github/workflows/orchestrate_poll.yml` keeps that input as backward-compat surface for existing callers. The live cleanup target is the internal wrapper’s redundant pass-through, not the reusable interface.

#### `final-merge-and-reissue-read-caching`
- Summary: Final-merge/reissue code still has obvious duplicate PR/issue reads, but those paths are recovery logic rather than simple cleanup.
- Source docs: `analysis/workflow-optimization-2026-04-30-2.md`, `analysis/workflow-optimization-2026-05-04-5.md`, `analysis/workflow-optimization-2026-05-09-2.md`
- Status: `risky_defer`
- Rationale: the duplicated reads live inside final-merge, self-heal, and stall-recovery branches where retry timing, race handling, and greppable log strings are operational contract.

#### `reserved-label-repair-helper-rollout`
- Summary: Reserved contradiction-evidence helpers should only be wired into the active poller with an explicit rollout plan.
- Source docs: `analysis/workflow-optimization-2026-05-08-2.md`, `analysis/workflow-optimization-2026-05-11-2.md`
- Status: `risky_defer`
- Rationale: `agents.md` already documents these `scripts/orchestrate_lib.py` helpers as contract/reserved and not yet wired into poller reconciliation. That is a feature rollout, not a cleanup.

### Shared helpers / cross-cutting

#### `tg-helper-delete-loop-and-write-transport-normalization`
- Summary: `tg_helpers.sh` still mixes page-indexed deletion, raw GitHub write calls, and brittle CSV ID iteration.
- Source docs: `analysis/workflow-optimization-2026-05-01-7.md`, `analysis/workflow-optimization-2026-05-14.md`
- Status: `risky_defer`
- Rationale: the bug is real, but the fix changes Telegram/GitHub cleanup side effects, comment-pagination semantics, and write-path retry behavior. That belongs in a dedicated helper-hardening issue, not a generic cleanup bundle.
