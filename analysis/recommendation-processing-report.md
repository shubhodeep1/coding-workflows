# Recommendation processing report

Grounding note: this report folds the prior recommendation triage into one final artifact. "Actioned" is based on current repository state on this ref, not on historical intent or external GitHub issue state.

## Processed source docs (81)
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
- `analysis/workflow-optimization-2026-05-15.md`
- `analysis/workflow-optimization-2026-05-16.md`
- `analysis/workflow-optimization-2026-05-16-2.md`
- `analysis/workflow-optimization-2026-05-17.md`
- `analysis/workflow-optimization-2026-05-17-2.md`
- `analysis/workflow-optimization-2026-05-17-3.md`
- `analysis/workflow-optimization-2026-05-18.md`
- `analysis/workflow-optimization-2026-05-18-2.md`
- `analysis/workflow-optimization-2026-05-18-3.md`
- `analysis/workflow-optimization-2026-05-18-4.md`
- `analysis/workflow-optimization-2026-05-19.md`
- `analysis/workflow-optimization-2026-05-20.md`
- `analysis/workflow-optimization-2026-05-20-2.md`
- `analysis/workflow-optimization-2026-05-20-3.md`
- `analysis/workflow-optimization-2026-05-21.md`
- `analysis/plan-workflow-log-analysis.md`
- `analysis/e2e-smoke-failure-25126757724.md`

## Downstream local issue ID recheck (repo-state only)
| Local ID | Recommendation | Files checked | Result | Current repo evidence |
|---|---|---|---|---|
| `apply-safe-e2e-smoke-cleanups` | `capture-issue-url-from-create-response` | `.github/workflows/test-and-mark-stable.yml` | not verified landed on this ref | The create-issue step still posts to `repos/${TEST_REPO}/issues` for `.number` and then immediately calls `gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url'` only to recover the URL in the same step. |
| `apply-safe-issue-pr-status-cleanups` | `collapse-duplicate-linked-issue-graphql-read` | `.github/workflows/issue_pr_status.yml` | not verified landed on this ref | The workflow still does one GraphQL `closingIssuesReferences` read that returns only issue numbers, then a second batched GraphQL issue lookup to fetch labels/body for those same issue numbers. |
| `apply-safe-internal-review-cleanups` | `internal-review-lazy-default-branch-lookup` | `.github/workflows/internal-review.yml` | not verified landed on this ref | The `Resolve PR for head branch` step still fetches `base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch')"` before it checks whether an open PR already exists and exits with `proceed=false`. |
| `apply-safe-implement-diagnose-cleanups` | `implement-diagnose-single-issue-cache-miss-fetch` | `scripts/implement_diagnose_post_codex_failure.sh` | not verified landed on this ref | On the straight-line cache-miss path, the script still calls `GET /issues/{n}` once to rebuild `ISSUE_LABELS_JSON` and later calls `GET /issues/{n}` again to refill `ISSUE_BODY_FILE`. |
| `apply-safe-orchestrate-poll-cleanups` | `internal-wrapper-stop-passing-deprecated-caller-workflow` | `.github/workflows/internal-orchestrate-poll.yml` | not verified landed on this ref | The reusable-wrapper job still passes `caller_workflow: internal-orchestrate-poll.yml` into `.github/workflows/orchestrate_poll.yml`, even though that input is documented as deprecated/ignored. |

## Actioned recommendations
None of the five downstream local issue IDs from the approved plan could be verified as landed on this ref, so there are no repo-verified actioned entries to record here for that earlier cleanup set. The dated 2026-05-15 through 2026-05-21 source-doc outcomes are tracked in the ledger below.

## Skipped / deferred / closed recommendations
The 20 deduped recommendations on this ref break down as: 5 safe cleanups still pending, 8 already satisfied items, 6 risky deferrals, and 1 obsolete recommendation.

### Not actioned on this ref (safe cleanup still pending)
- `capture-issue-url-from-create-response` (`apply-safe-e2e-smoke-cleanups`) - The create-issue step still posts to `repos/${TEST_REPO}/issues` for `.number` and then immediately calls `gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url'` only to recover the URL in the same step.
- `collapse-duplicate-linked-issue-graphql-read` (`apply-safe-issue-pr-status-cleanups`) - The workflow still does one GraphQL `closingIssuesReferences` read that returns only issue numbers, then a second batched GraphQL issue lookup to fetch labels/body for those same issue numbers.
- `internal-review-lazy-default-branch-lookup` (`apply-safe-internal-review-cleanups`) - The `Resolve PR for head branch` step still fetches `base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch')"` before it checks whether an open PR already exists and exits with `proceed=false`.
- `implement-diagnose-single-issue-cache-miss-fetch` (`apply-safe-implement-diagnose-cleanups`) - On the straight-line cache-miss path, the script still calls `GET /issues/{n}` once to rebuild `ISSUE_LABELS_JSON` and later calls `GET /issues/{n}` again to refill `ISSUE_BODY_FILE`.
- `internal-wrapper-stop-passing-deprecated-caller-workflow` (`apply-safe-orchestrate-poll-cleanups`) - The reusable-wrapper job still passes `caller_workflow: internal-orchestrate-poll.yml` into `.github/workflows/orchestrate_poll.yml`, even though that input is documented as deprecated/ignored.

### Already satisfied on this ref
- `checkout-before-setup-uv` - `.github/workflows/plan.yml` now performs repository checkout before the later `astral-sh/setup-uv@v3` bootstrap.
- `plan-no-progress-status-output` - `.github/workflows/plan.yml` explicitly tells the planner: `Do NOT emit progress/status messages ... Output ONLY the final plan or clarification result.`
- `review-autofix-sweep-paginated-jq-merge` - `.github/workflows/review_autofix_sweep.yml` already merges paginated PR pages with `jq -s '(add // [])'` before client-side filtering.
- `no-pr-claude-review-light-profile` - `.github/workflows/review_autofix.yml` has a dedicated `Use lightweight reviewer profile for no-PR claude-branch-review` step that sets `ENABLE_REVIEWER_TWO_PASS=false` and `REVIEWER_REASONING_EFFORT=low`.
- `editor-noop-blocks-conflict-resolver` - `.github/workflows/review_autofix.yml` gates merge-conflict detection/prep/resolver on `env.EDITOR_NOOP_SUSPICIOUS != 'true'`.
- `active-editor-model-rollout-away-from-legacy-split` - `.github/workflows/implement.yml` and `.github/workflows/review_autofix.yml` both default `MODEL_EDITOR` to `openai/gpt-5.4`, so the historical editor-model split is no longer the active runtime path.
- `empty-editor-shortcut-and-phase4b-gating` - `.github/workflows/test-and-mark-stable.yml` short-circuits on the live `EDITOR_NOOP_SUSPICIOUS` marker and only runs Phase 4b when `wait-review` completed cleanly.
- `dead-commits-after-fetch-removed` - `.github/workflows/test-and-mark-stable.yml` now validates bait removal through `fetch_pr_head_sha`; there is no remaining `COMMITS_AFTER` read in the workflow.

### Deferred as risky / broader-scope work
- `clarify-comment-history-fetch-collapse` - `.github/workflows/clarify.yml` intentionally keeps one bounded `per_page=50` JSON snapshot for prompt context and one paginated history render for semantic-cache/thread-history use. Collapsing them is real cleanup work, but it changes prompt shaping and fail-open semantics.
- `shared-strict-linked-issue-extractor` - the divergence is real, but this touches validate dispatch, phase-label mutation, and linked-issue semantics across multiple workflows/scripts. That needs one shared-helper rollout with fixture coverage, not a narrow cleanup issue.
- `review-context-helper-and-fallback-label-batching` - current `review_autofix.yml` staging and `gh_pr_with_all_comments`/GraphQL-helper behavior are close but not field-for-field identical. Pagination, fallback, and empty/partial-response parity should be proven before changing hot review paths.
- `final-merge-and-reissue-read-caching` - the duplicated reads live inside final-merge, self-heal, and stall-recovery branches where retry timing, race handling, and greppable log strings are operational contract.
- `reserved-label-repair-helper-rollout` - `agents.md` already documents these `scripts/orchestrate_lib.py` helpers as contract/reserved and not yet wired into poller reconciliation. That is a feature rollout, not a cleanup.
- `tg-helper-delete-loop-and-write-transport-normalization` - the bug is real, but the fix changes Telegram/GitHub cleanup side effects, comment-pagination semantics, and write-path retry behavior. That belongs in a dedicated helper-hardening issue, not a generic cleanup bundle.

### Obsolete / intentionally not reopened
- `remove-caller-workflow-input-entirely` - `.github/workflows/orchestrate_poll.yml` keeps that input as backward-compat surface for existing callers. The live cleanup target is the internal wrapper's redundant pass-through, not the reusable interface.

## Outcome ledger for the 2026-05-15 through 2026-05-21 backlog docs
Status keys below are scoped as `<source doc> :: <recommendation id>` because IDs such as `MERGE-001` and `REUSE-001` repeat across the dated reports. On this ref, the 15 in-scope docs resolve to 17 implemented items, 10 already-satisfied items, and 49 intentionally deferred items.

### `analysis/workflow-optimization-2026-05-15.md`
- Implemented by sibling work:
  - `REUSE-001` — `scripts/review_rb_judge.sh` now reuses the early `_pr_meta` snapshot for the PR-body fallback; the second `/pulls/{PR_NUMBER}` read is retained only as invalid-cache fallback.
- Intentionally deferred:
  - `MERGE-001` — `scripts/review_rb_judge.sh` still asks GraphQL for linked issue numbers only, then loops over REST issue reads to recover body/labels.
  - `REUSE-002` — `.github/workflows/orchestrate_clarify_respond.yml` still refetches the child issue and tracking issue in the later context step.
  - `REUSE-003` — `.github/workflows/review_autofix.yml` still keeps live `/pulls/{PR_NUMBER}` fallbacks in the late linked-issue labeler paths.
  - `DEAD-API-001` — `scripts/orchestrate_poll_process.sh` still performs the standalone conflict-sweep `DEFAULT_BRANCH` repo read and does not consume it afterward.

### `analysis/workflow-optimization-2026-05-16.md`
- Implemented by sibling work:
  - `REUSE-001` — the same `scripts/review_rb_judge.sh` PR-metadata reuse is now present on this ref.
- Already satisfied on this ref:
  - `DEAD-API-001` — `scripts/orchestrate_poll_process.sh` still defines the standalone-state helper fetches, but `rg` only finds their definitions, so the paginated comments path is unreachable in current runtime flows.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/clarify.yml` still keeps one bounded prompt-context comment fetch plus a separate paginated semantic-cache history fetch.
  - `REUSE-002` — `.github/workflows/internal-review.yml` still resolves `default_branch` before it knows whether an open PR already makes the no-PR path unnecessary.
  - `REUSE-003` — `.github/workflows/implement.yml` still performs both the issue-state precheck read and the later `ISSUE_META_FILE` read on the common path.

### `analysis/workflow-optimization-2026-05-16-2.md`
- Already satisfied on this ref:
  - `REUSE-002` — `scripts/review_rb_judge.sh` now reuses the first PR JSON snapshot instead of unconditionally refetching `/pulls/{PR_NUMBER}` on the GraphQL-empty path.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/review_autofix.yml` still hydrates PR payload, issue comments, reviews, and review comments with separate REST calls instead of one `gh_pr_with_all_comments` result.
  - `REUSE-001` — `.github/workflows/implement.yml` still splits the early issue-state precheck from the later metadata fetch.
  - `REUSE-003`, `REUSE-004` — `.github/workflows/orchestrate_clarify_respond.yml` still refetches both child and tracking issues in the later context-building step.

### `analysis/workflow-optimization-2026-05-17.md`
- Implemented by sibling work:
  - `API-002` — `.github/workflows/issue_pr_status.yml` now exports the earlier batched orchestrator classification and reuses it in the merged-alert step instead of re-fetching every linked issue body on the common path.
  - `MERGE-001` — `scripts/implement_diagnose_post_codex_failure.sh` now reuses `ISSUE_META_FILE` for both labels and body when the cached issue JSON is valid.
  - `REUSE-001` — `scripts/review_rb_judge.sh` now reuses `_pr_meta` for the PR-body fallback.
- Intentionally deferred:
  - `API-001` — `scripts/review_rb_judge.sh` still widens linked issue context with per-issue REST reads instead of a single GraphQL body/label fetch.
  - `BATCH-001` — `.github/workflows/review_autofix.yml` still uses separate PR/comments/reviews/review-comments calls even though `gh_pr_with_all_comments` already exists in `scripts/gh_helpers.sh`.
  - `REUSE-002` — `.github/workflows/orchestrate_clarify_respond.yml` still duplicates child/tracking issue reads across its two issue-context steps.
  - `DEAD-API-001`, `MERGE-002` — both recommendations still point at `scripts/orchestrate_poll_process.sh` hot-path cleanup, and that file was intentionally left untouched in this project bundle.

### `analysis/workflow-optimization-2026-05-17-2.md`
- Implemented by sibling work:
  - `REUSE-001` — `.github/workflows/issue_pr_status.yml` now reuses the earlier linked-issue classification for merged-alert suppression.
  - `REUSE-002` — `scripts/review_rb_judge.sh` no longer refetches PR JSON on the common GraphQL-empty path.
- Already satisfied on this ref:
  - `DEAD-API-001` — the standalone-state helper fetch is still present, but it remains definition-only / unreferenced on this ref.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/review_autofix.yml` still falls back to per-issue `gh issue view ... --json labels` calls when linked-issue labels are missing.
  - `REUSE-003` — `.github/workflows/orchestrate_clarify_respond.yml` still refetches child/tracking issues in its later issue-context step.

### `analysis/workflow-optimization-2026-05-17-3.md`
- Already satisfied on this ref:
  - `REUSE-001`, `DEAD-API-001` — `.github/workflows/issue_pr_status.yml` already gets `PR_TITLE` and `PR_BODY` from the pull_request event, so the extra `/pulls/{PR_NUMBER}` read survives only as blank-payload fallback.
- Intentionally deferred:
  - `MERGE-001` — `scripts/review_rb_judge.sh` still does its own GraphQL linked-issue lookup plus REST issue hydration instead of collapsing the path to one response.
  - `MERGE-002` — `.github/workflows/review_autofix.yml` still does `closingIssuesReferences` plus a PR-body fallback read on the GraphQL-empty path.
  - `MERGE-003` — `.github/workflows/test-and-mark-stable.yml` still polls `actions/runs/{run_id}` twice per cancel-on-close loop iteration.
  - `MERGE-004` — `.github/workflows/cancel_on_pr_close.yml` remains out of this cleanup bundle and still needs a dedicated batching change.

### `analysis/workflow-optimization-2026-05-18.md`
- Already satisfied on this ref:
  - `REUSE-001` — `.github/workflows/issue_pr_status.yml` already stays on the event-supplied PR title/body on the normal PR path and only uses the PR GET as blank-payload fallback.
  - `REUSE-003` — `scripts/implement_diagnose_post_codex_failure.sh` now reuses `ISSUE_META_FILE` for both labels and body when the cached JSON is valid.
  - `DEAD-API-001` — the standalone-state helper fetches in `scripts/orchestrate_poll_process.sh` are still definition-only / unreferenced.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/review_autofix.yml` still does GraphQL `closingIssuesReferences` and then a PR-body/title fallback read when the GraphQL list is empty.
  - `REUSE-002` — `.github/workflows/orchestrate_clarify_respond.yml` still refetches child/tracking issues across steps.
  - `MERGE-002` — the close-and-reissue issue-fetch collapse remains deferred in `scripts/orchestrate_poll_process.sh`.

### `analysis/workflow-optimization-2026-05-18-2.md`
- Implemented by sibling work:
  - `REUSE-001` — `scripts/review_rb_judge.sh` now reuses `_pr_meta` on the PR fallback path.
  - `REUSE-002` — `scripts/implement_diagnose_post_codex_failure.sh` now reuses the cached issue JSON for the later body read.
- Intentionally deferred:
  - `DEAD-API-001` — `.github/workflows/internal-review.yml` still resolves `base_ref` up front even when an existing PR makes the no-PR route exit immediately.
  - `MERGE-001` — `scripts/review_rb_judge.sh` still widens linked issue context with REST issue reads instead of a single GraphQL payload.
  - `MERGE-002` — the `scripts/orchestrate_poll_process.sh` close-and-reissue branch remains intentionally untouched in this cleanup.

### `analysis/workflow-optimization-2026-05-18-3.md`
- Implemented by sibling work:
  - `REUSE-002` — `.github/workflows/issue_pr_status.yml` now reuses the earlier batched issue-body/label classifier in the merged-alert step.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/orchestrate_clarify_respond.yml` still fetches child/tracking issue payloads in both `Check orchestrator metadata` and `Fetch issue and tracking context`.
  - `REUSE-001` — `scripts/review_rb_judge.sh` still performs its own early PR/linked-issue discovery before later consuming `PR_META_FILE`, so the workflow-level cache is not yet enough to remove that script-side read.
  - `MERGE-002` — `scripts/review_rb_judge.sh` still loops over REST issue GETs for linked issue bodies/labels after the GraphQL lookup.
  - `MERGE-003` — the final-merge PR snapshot collapse in `scripts/orchestrate_poll_process.sh` is still deferred.

### `analysis/workflow-optimization-2026-05-18-4.md`
- Implemented by sibling work:
  - `REUSE-001` — `scripts/review_rb_judge.sh` now keeps and reuses `_pr_meta` on the fallback path.
  - `REUSE-002` — `scripts/implement_diagnose_post_codex_failure.sh` now pulls the issue body from `ISSUE_META_FILE` before resorting to a second API read.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/test-and-mark-stable.yml` still creates the issue for `.number` and immediately re-reads it for `.html_url`.

### `analysis/workflow-optimization-2026-05-19.md`
- Implemented by sibling work:
  - `MERGE-001` — `scripts/review_rb_judge.sh` now reuses the initial `_pr_meta` snapshot instead of always issuing a second `/pulls/{PR_NUMBER}` read when `closingIssuesReferences` is empty.
- Intentionally deferred:
  - `REUSE-001` — `scripts/review_rb_judge.sh` still does its own GraphQL/REST linked-issue discovery even though `review_autofix.yml` already materializes `LINKED_ISSUES_JSON` and `PR_META_FILE` earlier in the run.
  - `REUSE-002` — `.github/workflows/internal-review.yml` still resolves `base_ref` before the existing-PR early exit.
  - `MERGE-002` — the final-merge PR snapshot consolidation in `scripts/orchestrate_poll_process.sh` remains deferred.

### `analysis/workflow-optimization-2026-05-20.md`
- Implemented by sibling work:
  - `REUSE-001` — `scripts/review_rb_judge.sh` already reuses `_pr_meta` on the GraphQL-empty PR fallback path.
- Already satisfied on this ref:
  - `DEAD-API-001` — `.github/workflows/review_autofix.yml` only calls `repos/${{ github.repository }}` for `default_branch` when `BASE_REF_OVERRIDE` is empty, and the in-repo no-PR caller `.github/workflows/internal-review.yml` still passes `base_ref_override` on every live path.
- Intentionally deferred:
  - `REUSE-002` — `.github/workflows/test-and-mark-stable.yml` still does a second issue GET for `ISSUE_URL` right after creation.
  - `MERGE-001` — `scripts/review_rb_judge.sh` still fetches linked issue bodies/labels through the REST loop instead of collapsing the path to one GraphQL response.
  - `MERGE-002` — the cancel-on-close poll loop in `.github/workflows/test-and-mark-stable.yml` still re-reads the same run for both status and conclusion each pass.

### `analysis/workflow-optimization-2026-05-20-2.md`
- Implemented by sibling work:
  - `MERGE-001` — `scripts/implement_diagnose_post_codex_failure.sh` now reuses `ISSUE_META_FILE` before a second issue GET.
  - `REUSE-001` — `scripts/review_rb_judge.sh` now reuses `_pr_meta` on the GraphQL-miss path.
- Intentionally deferred:
  - `MERGE-002` — `scripts/review_conflict_resolve.sh` still does separate `gh run list` calls for `in_progress` and `queued`.
  - `MERGE-003`, `MERGE-004` — `.github/workflows/test-and-mark-stable.yml` still keeps duplicated workflow-run polling in the plan/cancel wait loops.
  - `MERGE-005` — `scripts/orchestrate_poll_process.sh` still reads `repos/.../pulls/${final_pr}` multiple times across the final-merge checkpoints.

### `analysis/workflow-optimization-2026-05-20-3.md`
- Implemented by sibling work:
  - `REUSE-001` — `scripts/review_rb_judge.sh` reuses `_pr_meta` on the `ISSUE_NUMBERS=` path.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/test-and-mark-stable.yml` still posts the issue and then GETs it again for `html_url`.
  - `MERGE-002` — the cancel-on-close wait loop still does two `actions/runs/{run_id}` reads per iteration.

### `analysis/workflow-optimization-2026-05-21.md`
- Already satisfied on this ref:
  - `REUSE-004` — `scripts/review_rb_judge.sh` now keeps the initial `_pr_meta` payload and only re-fetches PR JSON when the cached snapshot is unusable.
- Intentionally deferred:
  - `REUSE-001` — `.github/workflows/internal-review.yml` still fetches `default_branch` before it knows whether an existing PR already makes the no-PR path unnecessary.
  - `REUSE-002` — `.github/workflows/implement.yml` still performs both the precheck issue GET and the later metadata GET.
  - `REUSE-003` — `.github/workflows/orchestrate_clarify_respond.yml` still refetches child/tracking issues in its later context step.
  - `MERGE-001` — the pre-existing cancel-on-close run wait loop in `.github/workflows/test-and-mark-stable.yml` still re-reads the same run resource twice per iteration.

## Preserved machine-maintained artifacts
- `analysis/validation-selftest-status.json` (kept unchanged)
- `analysis/last_collection_timestamp.txt` (kept unchanged)

## Conflicts / assumptions recorded for follow-up
- The approved plan warned that the five `safe_to_apply` items still looked unlanded on the planning ref; the current repo still shows those pre-change behaviors, so they remain recorded as pending rather than actioned.
- `.github/workflows/comprehensive-test-and-release.yml` now falls back to `analysis/recommendation-processing-report.md` when no dated `analysis/workflow-optimization-*.md` file exists. The current branch still keeps the 2026-05-22/23 dated reports, so the dated-report path remains the primary selection until those newer files are cleaned up in a later pass.
