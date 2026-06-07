# Recommendation processing report

Grounding note: this report folds the prior recommendation triage into one final artifact. "Actioned" is based on current repository state on this ref, not on historical intent or external GitHub issue state.

## Processed source docs (85)
The filenames below are retained for provenance. The four 2026-05-22/23 source docs processed in this pass are no longer present under `analysis/` on this ref because they were deleted on this ref.

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
- `analysis/workflow-optimization-2026-05-22.md`
- `analysis/workflow-optimization-2026-05-22-2.md`
- `analysis/workflow-optimization-2026-05-23.md`
- `analysis/workflow-optimization-2026-05-23-2.md`
- `analysis/plan-workflow-log-analysis.md`
- `analysis/e2e-smoke-failure-25126757724.md`

## Downstream local issue ID recheck (repo-state only)
| Local ID | Recommendation | Files checked | Result | Current repo evidence |
|---|---|---|---|---|
| `apply-safe-e2e-smoke-cleanups` | `capture-issue-url-from-create-response` | `.github/workflows/test-and-mark-stable.yml` | verified landed on this ref | `Create E2E test issue` now stores `ISSUE_CREATE_RESP` from the `POST /issues` call and parses both `.number` and `.html_url` locally; there is no immediate `GET /issues/{ISSUE_NUMBER}` follow-up. |
| `apply-safe-issue-pr-status-cleanups` | `collapse-duplicate-linked-issue-graphql-read` | `.github/workflows/issue_pr_status.yml` | verified landed on this ref | The workflow now fetches `closingIssuesReferences { number body labels(first: 50) { nodes { name } } }` in one GraphQL call and classifies orchestrator issues directly from that payload. |
| `apply-safe-internal-review-cleanups` | `internal-review-lazy-default-branch-lookup` | `.github/workflows/internal-review.yml` | verified landed on this ref | `Resolve PR for head branch` now exits on an existing open PR before the later `base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' ...)"` lookup, so the repo read happens only on the no-PR path. |
| `apply-safe-implement-diagnose-cleanups` | `implement-diagnose-single-issue-cache-miss-fetch` | `scripts/implement_diagnose_post_codex_failure.sh` | verified landed on this ref | The diagnoser now reads labels/body from `ISSUE_META_FILE` first and only falls back to `GET /issues/{n}` when the cached JSON is missing, mismatched, or unparsable. |
| `apply-safe-orchestrate-poll-cleanups` | `internal-wrapper-stop-passing-deprecated-caller-workflow` | `.github/workflows/internal-orchestrate-poll.yml` | verified landed on this ref | The internal wrapper now invokes `.github/workflows/orchestrate_poll.yml@main` with no `with: caller_workflow:` pass-through at all. |

## Actioned recommendations
- `capture-issue-url-from-create-response` (`apply-safe-e2e-smoke-cleanups`) — `.github/workflows/test-and-mark-stable.yml` now reuses the issue-create response for both `ISSUE_NUMBER` and `ISSUE_URL` instead of immediately re-reading the new issue.
- `collapse-duplicate-linked-issue-graphql-read` (`apply-safe-issue-pr-status-cleanups`) — `.github/workflows/issue_pr_status.yml` now asks GraphQL for linked issue numbers, bodies, and labels in one response and reuses that payload for orchestrator classification.
- `internal-review-lazy-default-branch-lookup` (`apply-safe-internal-review-cleanups`) — `.github/workflows/internal-review.yml` now checks for an existing open PR before it reads `default_branch`.
- `implement-diagnose-single-issue-cache-miss-fetch` (`apply-safe-implement-diagnose-cleanups`) — `scripts/implement_diagnose_post_codex_failure.sh` now reuses `ISSUE_META_FILE` for both label and body recovery before falling back to live issue reads.
- `internal-wrapper-stop-passing-deprecated-caller-workflow` (`apply-safe-orchestrate-poll-cleanups`) — `.github/workflows/internal-orchestrate-poll.yml` no longer forwards the deprecated `caller_workflow` input into the reusable poller.

The dated 2026-05-15 through 2026-05-23 source-doc outcomes are tracked in the ledger below.

## Skipped / deferred / closed recommendations
The remaining 15 deduped recommendations on this ref break down as: 8 already satisfied items, 6 risky deferrals, and 1 obsolete recommendation.

### Already satisfied on this ref
- `checkout-before-setup-uv` - `.github/workflows/plan.yml` now performs repository checkout before the later `astral-sh/setup-uv@v7` bootstrap.
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

## Outcome ledger for the 2026-05-15 through 2026-05-23 backlog docs
Status keys below are scoped as `<source doc> :: <recommendation id>` because IDs such as `MERGE-001` and `REUSE-001` repeat across the dated reports. `analysis/workflow-optimization-2026-05-23.md` did not use MERGE/REUSE IDs, so its recommendations are grouped under stable heading-text labels. On this ref, the 19 in-scope docs resolve to 33 implemented items, 11 already-satisfied items, and 63 intentionally deferred items.

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
  - `REUSE-002` — `.github/workflows/internal-review.yml` now checks for an existing PR before the later `default_branch` lookup, so the no-PR short-circuit no longer burns that repo read.
- Already satisfied on this ref:
  - `DEAD-API-001` — `scripts/orchestrate_poll_process.sh` still defines the standalone-state helper fetches, but `rg` only finds their definitions, so the paginated comments path is unreachable in current runtime flows.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/clarify.yml` still keeps one bounded prompt-context comment fetch plus a separate paginated semantic-cache history fetch.
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
  - `DEAD-API-001` — `.github/workflows/internal-review.yml` now defers the `default_branch` lookup until after the existing-PR early exit, so the no-PR fast-exit path no longer burns that repo read.
- Intentionally deferred:
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
  - `MERGE-001` — `.github/workflows/test-and-mark-stable.yml` now captures the full `POST /issues` response and derives both `ISSUE_NUMBER` and `ISSUE_URL` locally instead of re-reading the new issue.

### `analysis/workflow-optimization-2026-05-19.md`
- Implemented by sibling work:
  - `MERGE-001` — `scripts/review_rb_judge.sh` now reuses the initial `_pr_meta` snapshot instead of always issuing a second `/pulls/{PR_NUMBER}` read when `closingIssuesReferences` is empty.
  - `REUSE-002` — `.github/workflows/internal-review.yml` now checks for an existing PR before the later `default_branch` lookup, so the no-PR path avoids that repo read.
- Intentionally deferred:
  - `REUSE-001` — `scripts/review_rb_judge.sh` still does its own GraphQL/REST linked-issue discovery even though `review_autofix.yml` already materializes `LINKED_ISSUES_JSON` and `PR_META_FILE` earlier in the run.
  - `MERGE-002` — the final-merge PR snapshot consolidation in `scripts/orchestrate_poll_process.sh` remains deferred.

### `analysis/workflow-optimization-2026-05-20.md`
- Implemented by sibling work:
  - `REUSE-001` — `scripts/review_rb_judge.sh` already reuses `_pr_meta` on the GraphQL-empty PR fallback path.
  - `REUSE-002` — `.github/workflows/test-and-mark-stable.yml` now reuses the `POST /issues` response for `ISSUE_URL` instead of immediately issuing a second issue GET.
- Already satisfied on this ref:
  - `DEAD-API-001` — `.github/workflows/review_autofix.yml` only calls `repos/${{ github.repository }}` for `default_branch` when `BASE_REF_OVERRIDE` is empty, and the in-repo no-PR caller `.github/workflows/internal-review.yml` still passes `base_ref_override` on every live path.
- Intentionally deferred:
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
  - `MERGE-001` — `.github/workflows/test-and-mark-stable.yml` now parses `ISSUE_URL` from the original issue-create response instead of re-reading the issue.
- Intentionally deferred:
  - `MERGE-002` — the cancel-on-close wait loop still does two `actions/runs/{run_id}` reads per iteration.

### `analysis/workflow-optimization-2026-05-21.md`
- Implemented by sibling work:
  - `REUSE-001` — `.github/workflows/internal-review.yml` now fetches `default_branch` only after the existing-PR early exit, so the no-PR path no longer burns that repo read.
- Already satisfied on this ref:
  - `REUSE-004` — `scripts/review_rb_judge.sh` now keeps the initial `_pr_meta` payload and only re-fetches PR JSON when the cached snapshot is unusable.
- Intentionally deferred:
  - `REUSE-002` — `.github/workflows/implement.yml` still performs both the precheck issue GET and the later metadata GET.
  - `REUSE-003` — `.github/workflows/orchestrate_clarify_respond.yml` still refetches child/tracking issues in its later context step.
  - `MERGE-001` — the pre-existing cancel-on-close run wait loop in `.github/workflows/test-and-mark-stable.yml` still re-reads the same run resource twice per iteration.

### `analysis/workflow-optimization-2026-05-22.md`
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/implement.yml` still does the early approval-label issue GET and the later checkout-context issue metadata GET on the common path.
  - `MERGE-002` — `.github/workflows/test-and-mark-stable.yml` still reads `.status` and `.conclusion` via separate `GET /actions/runs/{id}` calls inside the cancel-on-close polling loop.
  - `REUSE-001` — `.github/workflows/orchestrate_clarify_respond.yml` still refetches child/tracking issues in the later prompt-context step.
  - `REUSE-002` — `.github/workflows/review_autofix.yml` still live-fetches `/pulls/${PR_NUMBER}` in `Enable auto-merge on PR` instead of reading cached `PR_META_FILE` first.

### `analysis/workflow-optimization-2026-05-22-2.md`
- Implemented by sibling work:
  - `REUSE-001` — `scripts/review_rb_judge.sh` now derives the PR title/body fallback from cached `_pr_meta` before the live pull GET fallback.
  - `REUSE-002` — `.github/workflows/issue_pr_status.yml` now reuses the earlier orchestrator classification in the merged-alert step and falls back to per-issue body lookups only when that classification is incomplete.
- Intentionally deferred:
  - `MERGE-001` — `scripts/orchestrate_poll_process.sh` still re-reads `repos/.../pulls/${final_pr}` at separate final-merge decision clusters.
  - `MERGE-002` — `scripts/orchestrate_poll_process.sh` still does separate issue title/body GETs in the close/reissue flows.
  - `REUSE-003` — `.github/workflows/orchestrate_clarify_respond.yml` still refetches the child issue in the later context step.

### `analysis/workflow-optimization-2026-05-23.md`
- Implemented by sibling work:
  - `shorten-review-autofix-check-run-waits` (`Speed-2`; `GH API-1`) — `.github/workflows/review_autofix.yml` now defaults `CHECK_RUNS_WAIT_TIMEOUT_SECS` to `300`, caches the last self-excluded in-flight snapshot signature, and backs off the sleep to `2x` / `4x` the base poll interval when that snapshot is unchanged.
  - `small-diff-pass2-reasoning-split` (`Cost-2`) — `.github/workflows/review_autofix.yml` now exports `REVIEWER_PASS2_REASONING_SMALL=high` and `REVIEWER_PASS2_REASONING_LARGE=xhigh`, so small diffs no longer inherit the old `xhigh/xhigh` second-pass default.
  - `reviewer-memory-title-body-handoff` (`Cost-3`) — the reviewer-memory step now skips empty title/body PRs and otherwise passes `--issue-title` plus `--issue-body-file` into `memory_retrieve`.
  - `orchestrate-poll-regression-fixes` (`Reliability-1`) — `scripts/orchestrate_poll_process.sh` now skips early-phase stall recovery when an open linked PR already exists and promotes prior-wave `ai:ready-to-merge` issues with merged linked PRs to `ai:merged`; verify with `python -m pytest -q tests/test_orchestrate_poll_process.py::test_no_labels_with_open_linked_pr_skips_retrigger_pipeline tests/test_orchestrate_poll_process.py::test_backward_scan_promotes_ready_to_merge_with_merged_pr_to_merged`.
  - `node24-setup-uv-rollout` (`Reliability-4`) — the workflows now pin `astral-sh/setup-uv@v7`, not the older Node20-backed action version called out by the source doc.
- Already satisfied on this ref:
  - `keep-semble-focus-on-bigger-cost-drivers` (`Cost-4`) — `.github/workflows/review_autofix.yml` still defaults `SEMBLE_ENABLED=true` and `SERENA_ENABLED=false`, matching the doc's recommendation to leave Semble in place and not spend effort on Serena before it is actually enabled.
- Intentionally deferred:
  - `self-triggered-skip-by-default` (`Speed-1`; `Cost-1`) — `AUTOFIX_SKIP_SELF_TRIGGERED` still defaults `false` because productive `[ai-autofix]` commits now use the explicit continuation path rather than a default skip-first policy.
  - `orchestrate-poll-setup-tax-reduction` (`Speed-3`) — the `setup-uv` deprecation sub-problem landed, but the broader Semble/bootstrap reuse work has not.
  - `ci-fast-fail-shard-for-hot-tests` (`Speed-4`) — the cited tests are fixed, but `.github/workflows/ci.yml` still does not run them in a dedicated earlier shard.
  - `prompt-cache-observability` (`Cost-5`) — cache-normalization/probe helpers exist in `scripts/review_run_reviewers.sh`, but the workflow still does not surface those counters as a stable normal-path runtime contract.
  - `integration-fingerprint-heal` (`Reliability-2`) — this reporting pass does not alter orchestrator integration-branch recovery behavior.
  - `cross-trigger-dedupe-tightening` (`Reliability-3`) — review_autofix still relies on the current peer-dedup plus continuation-bypass contract, not the proposed PR+SHA active-run classifier.
  - `pr-context-artifact-handoff` (`GH API-2`) — review_autofix still re-hydrates some PR data in later steps instead of handing one `pr_context.json`-style artifact through the whole run.
  - `review-autofix-sweep-zero-candidate-early-exit` (`GH API-3`) — this pass does not change `.github/workflows/review_autofix_sweep.yml`.
  - `drift-audit-missing-log-fast-fail` (`GH API-4`) — the drift-audit log-fetch behavior is unchanged on this ref.

### `analysis/workflow-optimization-2026-05-23-2.md`
- Implemented by sibling work:
  - `MERGE-002` — `.github/workflows/test-and-mark-stable.yml` now captures the full `POST /issues` response and derives `ISSUE_NUMBER` plus `ISSUE_URL` locally.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/review_autofix.yml` still fans out PR issue comments, reviews, and review comments through separate REST calls instead of one raw-compatible discussion bundle helper.
  - `MERGE-003` — `scripts/orchestrate_poll_process.sh` still re-reads final-merge PR state in separate decision clusters.
  - `MERGE-004` — `.github/workflows/test-and-mark-stable.yml` still reads `.status` and `.conclusion` separately inside the cancel-on-close poll loop.
  - `REUSE-001` — `.github/workflows/orchestrate_clarify_respond.yml` still refetches child/tracking issue payloads across steps.

## Preserved machine-maintained artifacts
- `analysis/validation-selftest-status.json` (kept unchanged)
- `analysis/last_collection_timestamp.txt` (kept unchanged)

## Conflicts / assumptions recorded for follow-up
- The approved plan's stale-repo warning for the five `safe_to_apply` items was no longer true by implementation time: current repo state shows those cleanups already merged, so this report records them as actioned.
- `analysis/workflow-optimization-2026-05-23.md` did not use MERGE/REUSE-style IDs, so the ledger groups its repeated source recommendations under stable heading-text labels when multiple sections point at the same landed change.
- With the four 2026-05-22/23 source docs now deleted, `.github/workflows/comprehensive-test-and-release.yml` will hit its existing fallback path to `analysis/recommendation-processing-report.md` on future runs.

---

# Pass 2026-06-07 — workflow-optimization 2026-05-29 → 2026-06-06-3 (8 docs)

Grounding note: every classification below reflects the **current repository
state on this ref** (branch `claude/ecstatic-goodall-R7j2W`), validated by
re-reading the targeted code, not historical intent or external GitHub state.
"Applied" means the edit is present in this PR; "obsolete/already-satisfied"
means the current code already does it (verified, several via passing CI
contract tests); "rejected" means the recommendation is wrong on re-read of
the actual code; "not applied" means correct-but-risky and left for human
review per the apply-analysis safety bar (correct **and** safe to land
without breaking existing flows).

## Processed source docs (8 — deleted in this pass, retained for provenance)
- `analysis/workflow-optimization-2026-05-29.md`
- `analysis/workflow-optimization-2026-06-04.md`
- `analysis/workflow-optimization-2026-06-05.md`
- `analysis/workflow-optimization-2026-06-05-2.md`
- `analysis/workflow-optimization-2026-06-05-3.md`
- `analysis/workflow-optimization-2026-06-06.md`
- `analysis/workflow-optimization-2026-06-06-2.md`
- `analysis/workflow-optimization-2026-06-06-3.md`

The four excluded non-recommendation files were left untouched:
`analysis/last_collection_timestamp.txt`,
`analysis/validation-selftest-status.json`, this report, and the prior
report sections above.

## Applied (3)

- **Quote the credential-bearing remote URL (SC2086 / SEC)** —
  `scripts/review_commit_changes.sh:489`. Was
  `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`
  (unquoted); now quoted. Zero behavior change for normal inputs, strictly
  safer (word-split/glob hardening on a credential string). Source IDs:
  `2026-05-29 SHELL-001`, `2026-06-06 SEC-001`. Verified `bash -n` +
  `test_workspace_safety_check`, `test_codex_thread_reuse_review`,
  `test_review_conflict_resolve_*` all pass.
- **Quote the credential-bearing remote URL (SC2086 / SEC)** —
  `scripts/review_conflict_resolve.sh:2510`. Same minimal quoting fix.
  Source IDs: `2026-05-29 SHELL-001`, `2026-06-04 SHELL-001`,
  `2026-06-06 SEC-001`. The docs' larger alternative (switch to
  `git -c http.extraHeader` Basic-auth) was **not** taken — it is a bigger
  behavioral refactor; all three docs name quoting as the minimum safe fix
  (§5 minimal change set).
- **Reuse `github.event.repository.default_branch` instead of a redundant
  `repos/{repo}` read** — `.github/workflows/internal-review.yml:91,118`
  (`resolve-claude-branch-pr`). Replaced
  `base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' ... || echo 'main')"`
  with `base_ref="${EVENT_DEFAULT_BRANCH:-main}"`, sourced from the event
  payload. The job is gated on `github.event_name == 'push'`, so the field
  is always present in the event context; the `:-main` fallback preserves
  the previous default exactly (CLAUDE.md §15). Source ID:
  `2026-06-06-2 REUSE-001` (tagged `SAFE_TO_MERGE`). Verified `yamllint -s`
  passes and the YAML parses.

## Rejected (invalid on re-read)

- **Change `VALIDATE_WORKFLOW_NAME` default from `ai-validate.yml` to
  `internal-validate.yml`** (`2026-06-04 Speed-4` / `GH API-1`;
  `2026-06-04 Reliability` post-merge dispatch) — REJECTED. The doc only
  inspected `.github/workflows/` in this repo and concluded `ai-validate.yml`
  does not exist. It does exist as the **consumer template**
  `workflow-templates/ai-validate.yml`, which consumer repos copy into their
  own `.github/workflows/ai-validate.yml`. So `ai-validate.yml` is the
  correct default for the primary consumer use case, and the self-repo
  (`internal-validate.yml`) path is already covered by the explicit fallback
  at `review_autofix.yml:819`
  (`... || { [ "${validate_workflow}" != "internal-validate.yml" ] && gh workflow run "internal-validate.yml" ...; }`).
  Changing the default would break consumer repos that have no
  `internal-validate.yml`. The only real cost is one failed self-repo
  dispatch that the fallback already absorbs.

## Obsolete / already-satisfied on this ref

The following recommendations target code that the current ref already does;
several are verified by contract tests that pass on this branch (so the
"CI contract regression" failures the docs cite were on stale tested refs,
not current main).

- **Per-attempt prompt-file contract** (`2026-06-04 Reliability-1`,
  `2026-06-05 Speed-2/Reliability-1`, `2026-06-05-2 Reliability-2`,
  `2026-06-06 Reliability-1`) — current main feeds Codex stdin from the
  per-attempt prompt file. `tests/test_review_autofix_editor_noop_cascade_contract.py`
  PASSES on this ref.
- **`REVIEW_RUN_MAX_RUNTIME_MINUTES` unbound + `timeout --signal=TERM
  --kill-after=5s` in `implement.yml`** (`2026-06-05-3 Reliability-1`,
  `2026-06-06 Reliability-1`, `2026-06-06-2 Reliability-1`) —
  `tests/test_codex_stall_guard_poller.py` PASSES on this ref.
- **Thread-reuse wiring contract `codex_thread_reuse.sh; do`**
  (`2026-06-06-3 Reliability-1`) — `tests/test_codex_thread_reuse_core.py`
  and `tests/test_codex_thread_reuse_review.py` PASS on this ref.
- **Brace-expansion rename parsing in the reviewer diffstat filter**
  (`2026-05-29 Reliability-1`) — `scripts/review_run_reviewers.sh:434-436`
  already handles `{ ... => ... }` rename diffstat rows;
  `tests/test_review_autofix_review_pipeline_contract.py` PASSES.
- **Keep Semble enabled / do not invest in Serena / treat Semble fixture
  fallbacks as healthy fail-open** (every doc's Cost + Reliability "Semble"
  and "Serena" items) — these are affirmations to *not change* current
  defaults (`SEMBLE_ENABLED=true`, `SERENA_ENABLED=false`, fail-open
  fixtures). No action required; current state already matches.

## Not applied — correct-but-risky / needs review

These are real findings, but each is high-blast-radius, hot-path,
public-contract, a multi-file refactor, a behavior/observability tradeoff,
a §6 identifier change, or tagged `NEEDS_VERIFICATION` / `RISKY_SKIP` by the
source doc's own consolidation audit. They are left for human review per the
apply bar; none were split into a follow-up PR.

### Large refactors (expression-size + duplication)
- **Extract the workflow-support staging/bootstrap blocks to a shared
  `scripts/stage_workflow_support.sh`** (`EXPR-001/002/003/004` and
  `DUP-001` across 05-29, 06-04, 06-05-3, 06-06, 06-06-2, 06-06-3) — the
  largest blocks (`validate.yml`, `review_autofix.yml`) are near the 21k
  expression cap, but extraction spans 6-8 workflows and changes
  staging/fallback semantics. Multi-file, high blast radius.
- **Centralize the inline `gh_retry` / `gh_api_safe` / `_rl_wait` wrappers
  on `scripts/gh_helpers.sh`** (`DUP-001/002/003` retry-wrapper variants in
  every deep-audit doc; `API-005` 06-04; `API-001` 06-06-3) — touches many
  workflows and changes permanent-vs-transient failure handling.
- **Extract `Commit changes` / `Collect PR metadata` to dedicated scripts**
  (`EXPR-003/004`) — large hot-path `run:` blocks.
- **Shared Python log-analysis util module** (`2026-05-29 DUP-003`) and
  **canonical Python integration-ref resolver** (`2026-06-06-2 DUP-003`) —
  module reorganization (§12.D).

### GitHub API batching / consolidation (NEEDS_VERIFICATION / RISKY_SKIP)
- **`gh_pr_with_all_comments` consolidation of the 4 PR-context fetches**
  (`API-001` 06-04/06-06/06-06-2, `BATCH-001` 06-05-3) — hot review path;
  GraphQL/REST pagination + `PR_REVIEWS_FILE` parity must be proven first.
- **Batch post-merge / fallback linked-issue label hydration via GraphQL**
  (`BATCH-001/002/003` 06-04/06-05-3/06-06/06-06-3, `API-002/003`) —
  validate-dispatch fail-open + label-removal semantics need parity proof.
- **Batch `_subissue_closing_pr_number` per-PR reads / `review_rb_judge`
  linked-issue GraphQL** (`API-001` 06-06, `BATCH-002` 06-06-3,
  `API-002` 06-04) — orchestrator/poller decision logic.
- **Consolidation/re-fetch candidates** `MERGE-001..003`, `REUSE-001..004`,
  `DEAD-API-001/002` across all consolidation sections — every one is
  tagged `NEEDS_VERIFICATION` or `RISKY_SKIP` (clarify.yml comment merge,
  implement.yml issue reads, orchestrate_clarify_respond issue reads,
  test-and-mark-stable PR-stability probe, issue_pr_status PR-body
  fallback, review_autofix base_ref_override, force-tick tracking issue,
  `read_standalone_state_json` / `list_run_log_excerpts` dead helpers in
  externally-sourceable scripts). The single `SAFE_TO_MERGE` PR-metadata
  re-fetch (`2026-06-06-2 REUSE-001`) is the one applied above;
  `2026-06-05-3 MERGE-002` (implement-diagnose issue-read consolidation) was
  tagged `SAFE_TO_MERGE` but is left for review — see below.
- **`2026-06-05-3 MERGE-002` (implement_diagnose label+body single fetch)**
  — `scripts/implement_diagnose_post_codex_failure.sh:166-172,261-277`. The
  two reads are separated by early-`exit 0` branches (label-already-set,
  no-capture), use different mechanisms (`_safe_gh_jq` vs `gh api`) and
  different fail-open defaults (`[]` vs empty body). Consolidating to one
  memoized fetch is doable but changes fail-open semantics on a
  failure-diagnosis path for a 1-call saving on a rare miss path. §1
  (correctness > speed) → defer.

### Correctness defects with tradeoffs / shared-helper rollout
- **`gh api --jq ... || echo` stdout-corruption PR/issue-state guards**
  (`2026-05-29 BUG-001` plan.yml auto-approve, `BUG-002` review_autofix
  two PR-state guards) — real, but the fix has a fail-open-vs-fail-closed
  tradeoff on hot dispatch/alert paths (§12.D "material tradeoff").
- **Strict linked-issue extractor** (`2026-06-04 BUG-001/002/005`,
  `CONSIST-001`; `2026-06-05-3 BUG-001`) — bare `issue #N` / `issues/N`
  accepted in several review_autofix/`review_rb_judge` fallbacks while
  `issue_pr_status.yml` rejects them. Correct concern, but the fix is one
  shared extractor replacing ad-hoc regex across multiple workflows/scripts
  (matches the prior report's deferred `shared-strict-linked-issue-extractor`).
- **`resolve_integration_ref.sh` fail-open-to-default-branch on transient
  API error in write flows** (`2026-06-06-2 BUG-001`) — genuine
  correctness/safety concern (write flows could reroute to the default
  branch on a transient error), but the fix is a tri-state resolver contract
  change across `implement`/`validate`/`clarify`/`plan`/`respond`. Flagged
  as the highest-priority deferral for human review.
- **`tg_helpers` read-modify-write race** (`2026-06-04 BUG-004`) — changes
  Telegram/GitHub comment side effects (prior report's
  `tg-helper-...-normalization`).
- **`internal-review.yml` percent-encode `HEAD_REF`** (`2026-06-04 BUG-003`)
  — safe in principle, but `claude/**` branch names do not contain
  URL-reserved characters in practice, so it is low-value added code; left
  out to keep the touched step minimal.
- **`review_run_reviewers.sh` pass-1 hardcoded `xhigh` / pass-2 small-diff
  default** (`2026-06-06 BUG-001`, `CONSIST-001`) — reasoning-effort
  behavior change with a quality tradeoff (§12.D).
- **`implement.yml` suffix-match `*/coding-workflows` self-repo guard**
  (`2026-06-05-3 CONSIST-001`) and **`orchestrate_poll_process.sh`
  `ensure_label_exists` return-0-on-failure** (`2026-06-06-3 CONSIST-001`)
  — behavior changes in self-repo / poller paths needing review.

### Speed / cost / reliability program items (architecture & tuning)
- Inline review gate into heavy job; reasoning tiering for
  plan/implement/reviewer; conditionalize `free-disk-space`;
  phase-completion poller dispatch; reviewer fan-out reduction;
  small-diff / no-PR review fast path + lightweight profile; check-run
  poll backoff / lower `CHECK_RUNS_WAIT_TIMEOUT_SECS`; terminal-state
  short-circuit for stuck editor / PR-closed runs; Phase-4 non-cancelled
  run selection at `PIN_SHA`; one comment-router/dispatcher workflow; CI
  `lint` sharding + brittle-contract-first ordering; trim
  `workflow_log_analysis` scope / cap `summarize_unselected_runs`;
  prompt-cache + reviewer-token telemetry instrumentation; AI-memory
  retrieval tuning / worktree-collision fix; prompt-size reduction; Semble
  byte budgets + fallback-parser de-noising; validate observability /
  log-retention; trim `orchestrate_poll` fixed overhead. All are
  architectural changes, model/behavior tradeoffs, or observability work
  on hot paths — out of scope for an auto-safe apply.

### Dead code / shellcheck (low value, sensitive or non-trivial)
- `RESOLVER_ESCALATION_COMMENT_MARKER` unused (`2026-05-29 DEAD-001`),
  `orchestrate_lib.py` contradiction-evidence dormant (`2026-06-04
  DEAD-001`, already documented reserved in `agents.md`),
  `ai_context_utils.py` unattached (`2026-06-05-3 DEAD-001`),
  `read_standalone_state_json` (`DEAD-001`/`DEAD-API-001` 06-06/06-06-2/
  06-06-3, externally-sourceable poller script), `review_issue_ledger.sh`
  unused locals (`2026-06-06-3 DEAD-001`), `list_run_log_excerpts` wrapper
  (`2026-06-06 DEAD-API-002`, still referenced by tests),
  `review_run_reviewers.sh` SC2086/unused-vars/smart-quotes (`2026-06-04
  SHELL-002`), `validate_changed_files_syntax.sh` overlapping case
  (`2026-06-06-2 SHELL-001`), `review_conflict_resolve.sh`
  `_dispatch_integration_judge_now` inline-env `--repo` (`2026-06-06-3
  SHELL-001`, race-dispatch path), deprecated `caller_workflow` /
  `codex_mode` / `--commit-message` / `--pr-title` surfaces (`DEBT-001`
  06-06/06-06-3 — §6 back-compat, ask-first). Deferred: each is either in a
  sensitive/externally-consumable path, still referenced, or a §6 surface.

## Verification (this pass)
- `bash -n` clean on both edited scripts.
- `yamllint -s` clean on `internal-review.yml`; YAML parses.
- Contract tests covering the edited files pass:
  `test_codex_thread_reuse_review`, `test_workspace_safety_check`,
  `test_review_conflict_resolve_retry_state`,
  `test_review_conflict_resolve_reasoning_step_down`,
  `test_review_conflict_resolve_smoke_deterministic`,
  `test_review_conflict_resolve_retry_prelude_render`,
  `test_codex_stall_guard_scripts`, `test_run_substate_ledger`,
  `test_review_autofix_review_pipeline_contract`.
- "Already-satisfied" CI contract tests independently pass on this ref
  (see Obsolete section). Remaining local test failures are environment
  artifacts only (container commit-signing server, missing `gawk`), not
  code or recommendation issues.
