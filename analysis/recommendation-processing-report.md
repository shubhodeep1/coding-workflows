# Recommendation processing report

Grounding note: this report folds the prior recommendation triage into one final artifact. "Actioned" is based on current repository state on this ref, not on historical intent or external GitHub issue state.

## Processed source docs (95)
The filenames below are retained for provenance. The twelve source docs deleted across the cleanup passes reflected in this report are no longer present under `analysis/` on this ref because their triage now lives here.

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
- `analysis/workflow-optimization-2026-05-29.md`
- `analysis/workflow-optimization-2026-06-04.md`
- `analysis/workflow-optimization-2026-06-05.md`
- `analysis/workflow-optimization-2026-06-05-2.md`
- `analysis/workflow-optimization-2026-06-05-3.md`
- `analysis/workflow-optimization-2026-06-06.md`
- `analysis/workflow-optimization-2026-06-06-2.md`
- `analysis/workflow-optimization-2026-06-06-3.md`
- `analysis/workflow-optimization-2026-06-08-2.md`
- `analysis/workflow-optimization-2026-06-08.md`
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
Status keys below are scoped as `<source doc> :: <recommendation id>` because IDs such as `MERGE-001` and `REUSE-001` repeat across the dated reports. `analysis/workflow-optimization-2026-05-23.md` did not use MERGE/REUSE IDs, so its recommendations are grouped under stable heading-text labels. On this ref, the 20 in-scope docs resolve to 41 implemented items, 15 already-satisfied items, 80 intentionally deferred items, and 1 obsolete item.

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

### `analysis/workflow-optimization-2026-05-29.md`
- Implemented by sibling work:
  - `brace-expansion diffstat filter hardening` — `scripts/review_run_reviewers.sh` now normalizes brace-expansion rename targets before applying reviewer skip filters, so the CI regression this doc called out is fixed on this ref.
  - `event-driven orchestrator force-ticks` — `.github/workflows/implement.yml`, `.github/workflows/validate.yml`, and `.github/workflows/review_autofix.yml` now invoke `scripts/orchestrate_force_tick.sh` at major phase transitions instead of relying only on the cron poller.
- Already satisfied on this ref:
  - `keep-semble-defer-serena` — current workflow defaults still keep `SEMBLE_ENABLED=true` and `SERENA_ENABLED=false`, matching the doc's recommendation to leave Semble in place and not spend effort on Serena yet.
  - `DEAD-API-001` — `scripts/orchestrate_poll_process.sh` still defines `read_standalone_state_json()`, but repo-local search still only finds the definition, so the dormant paginated comments path remains unreachable in current runtime flows.
- Intentionally deferred:
  - `REUSE-001` — `.github/workflows/implement.yml` still performs the early approval/precheck issue read and the later metadata-file population on the common path.
  - `REUSE-002` — `review_rb_judge` still rebuilds PR discussion from live helpers instead of reusing the earlier comment snapshots.
  - `REUSE-003` — `.github/workflows/review_autofix.yml` still re-reads `/pulls/{PR_NUMBER}` across separate gate and metadata stages.
  - `gate-inline / free-disk / reasoning-tiering / reviewer-memory / wrapper-noise reductions` — the broader queue-shape and default-reasoning changes proposed in this doc remain unlanded on this ref.

### `analysis/workflow-optimization-2026-06-04.md`
- Implemented by sibling work:
  - `per-attempt prompt-file contract restore` — `scripts/review_apply_fixes.sh` now copies `EDITOR_PROMPT_FILE` into a per-attempt prompt file, appends a retry nonce, and feeds that file into each editor attempt, which is the contract this doc flagged in CI.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/clarify.yml` still keeps one bounded issue-comment snapshot for prompt context and one paginated fetch for semantic-cache history.
  - `REUSE-001` — `.github/workflows/implement.yml` still re-reads issue metadata across precheck, checkout-ref resolution, and later `ISSUE_META_FILE` population.
  - `REUSE-002` — `.github/workflows/orchestrate_clarify_respond.yml` still re-fetches child/tracking issues in its later context step.
  - `comment-router / CI-frontload / validate-default / reasoning-tiering / prompt-cache observability` — the broader wrapper-collapse, CI-sharding, validate-dispatch-default, and telemetry changes from this doc remain unlanded on this ref.

### `analysis/workflow-optimization-2026-06-05.md`
- Implemented by sibling work:
  - `optional render_prompt.py backend handling` — `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`, and `.github/workflows/test-and-mark-stable.yml` now treat `render_prompt.py` as an optional staged backend, which resolves the deterministic release-gate failure this doc described.
  - `terminal editor abort handling` — `scripts/review_apply_fixes.sh` now short-circuits the editor path when the PR is already closed or the editor watchdog kills an idle run, instead of leaving those states to linger until outer cancellation.
- Already satisfied on this ref:
  - `validate/implement diagnostics surfaces` — `.github/workflows/validate.yml` and `.github/workflows/implement.yml` already write run summaries and upload runtime artifacts for downstream diagnosis.
  - `keep-semble-defer-serena` — current defaults still keep Semble enabled and Serena disabled by default.
- Intentionally deferred:
  - `workflow_log_analysis scope reduction` — the analysis workflow still keeps the current deep-audit plus broad run-summarization shape.
  - `prompt-cache telemetry / reviewer-memory tuning` — prompt-cache counters are still not surfaced as a stable runtime contract, and reviewer retrieval remains low-yield on this ref.
  - `earlier wrapper no-op suppression` — the comment-trigger wrappers still start child workflows before the later skip gates run.

### `analysis/workflow-optimization-2026-06-05-2.md`
- Implemented by sibling work:
  - `latest non-cancelled review-run selection at PIN_SHA` — `.github/workflows/test-and-mark-stable.yml` now prefers the newest completed non-cancelled review run before falling back to cancelled siblings, fixing the Phase-4 timeout class this doc highlighted.
  - `per-attempt editor prompt-file contract` — `scripts/review_apply_fixes.sh` now feeds the editor from per-attempt prompt files rather than a single unchanging prompt path.
  - `terminal review_codex-agent exits` — the editor path now exits on PR-closed and idle-watchdog terminal states instead of waiting for hours of outer cancellation.
  - `behavioural-smoke runtime restore` — `.github/workflows/validate.yml` now resolves the linked review PR and restores cached behavioural-smoke runtime artifacts before validation.
- Already satisfied on this ref:
  - `missing-log negative cache` — `scripts/collect_workflow_logs.py` now caches missing-log-archive results within a collection pass.
  - `keep-semble-defer-serena` — current defaults still match the doc's recommendation to keep Semble and treat Serena as disabled.
- Intentionally deferred:
  - `small-diff review fast path` — `review_autofix` still keeps the current broader review structure for tiny diffs.
  - `REUSE-001` — `.github/workflows/internal-review.yml` still calls `GET /repos/{repo}` for `default_branch` on the no-PR path.
  - `REUSE-002` — the no-PR path in `.github/workflows/review_autofix.yml` still falls back to a repo `default_branch` lookup when `base_ref_override` is empty.
  - `REUSE-003` — `.github/workflows/issue_pr_status.yml` still keeps the PR title/body REST fallback when event payload text is blank.
  - `REUSE-004` — `.github/workflows/orchestrate_clarify_respond.yml` plus `scripts/resolve_integration_ref.sh` still re-fetch the child issue across step/helper boundaries.
  - `CI sharding / workflow_log_analysis fail-fast / prompt-cache instrumentation / tiny workflow fan-out / earlier validate-failure capture` — the broader structural changes from this doc remain unlanded.

### `analysis/workflow-optimization-2026-06-05-3.md`
- Implemented by sibling work:
  - `review stall-guard contract restore` — `scripts/orchestrate_poll_process.sh` now restores and validates the `REVIEW_RUN_MAX_RUNTIME_MINUTES` default that the CI failures in this doc called out.
  - `thread-reuse contract update` — `scripts/codex_thread_reuse.sh` now carries the shared timeout wrapper and helper-based wiring that `tests/test_codex_thread_reuse_core.py` asserts on this ref, so the old literal-shell contract failure no longer matches current code.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/clarify.yml` still does separate prompt-context and semantic-cache comment fetches.
  - `MERGE-002` — `scripts/implement_diagnose_post_codex_failure.sh` now reuses `ISSUE_META_FILE` first, but the cache-miss path still splits live label and body recovery into separate `GET /issues/{n}` calls instead of one shared fallback fetch.
  - `MERGE-003` — `.github/workflows/test-and-mark-stable.yml` still keeps the two-read SHA stability probe plus a later PR metadata read.
  - `REUSE-001` — `.github/workflows/orchestrate_clarify_respond.yml` and `scripts/resolve_integration_ref.sh` still re-fetch the child issue body across gate, helper, and prompt-assembly paths.
  - `implement reasoning tiering / support-checkout reduction / dispatcher collapse / conflict-tail cleanup` — the broader implement-cost and review-failure-tail changes from this doc remain deferred.

### `analysis/workflow-optimization-2026-06-06.md`
- Already satisfied on this ref:
  - `missing-log negative cache` — `scripts/collect_workflow_logs.py` now caches missing-log-archive failures within a pass instead of repeatedly re-fetching the same 404.
  - `keep-semble-defer-serena` — current workflow defaults still keep Semble enabled and Serena disabled.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/test-and-mark-stable.yml` still does separate `GET /actions/runs/{id}` reads for `.status` and `.conclusion` inside the cancel-on-close wait loop.
  - `REUSE-001` — `.github/workflows/issue_pr_status.yml` still keeps the fallback live PR title/body fetch when event payload text is blank.
  - `REUSE-002` — `.github/workflows/review_autofix.yml` still projects `PR_META_FILE` without `head_sha`, so `scripts/review_rb_judge.sh` still falls back to `gh pr view --json headRefOid`.
  - `REUSE-003` — `.github/workflows/orchestrate_clarify_respond.yml` still re-fetches child/tracking issue data during later context assembly.
  - `REUSE-004` — `.github/workflows/test-and-mark-stable.yml` still keeps the deliberate two-sample PR stability probe rather than collapsing those reads.
  - `DEAD-API-001` — `read_standalone_state_json()` remains defined but not removed.
  - `DEAD-API-002` — `list_run_log_excerpts()` is still present as an unused wrapper around `_fetch_run_log_archive()`.
  - `adaptive review defaults / validate-workflow default cleanup` — `.github/workflows/review_autofix.yml` still keeps the heavier reviewer/editor defaults and still defaults `VALIDATE_WORKFLOW_NAME` to `ai-validate.yml`.

### `analysis/workflow-optimization-2026-06-06-2.md`
- Implemented by sibling work:
  - `stall-guard/env-drift fix` — `scripts/orchestrate_poll_process.sh` now restores the shared review stall-guard default expected by the CI contract failures called out here.
  - `per-attempt editor prompt-file guard` — `scripts/review_apply_fixes.sh` now feeds the editor from per-attempt prompt files.
  - `behavioural-smoke restore` — `.github/workflows/validate.yml` now restores cached behavioural-smoke runtime artifacts before validation.
- Already satisfied on this ref:
  - `keep-semble-defer-serena` — the repo still keeps Semble active and Serena effectively disabled by default.
- Intentionally deferred:
  - `MERGE-001` — `.github/workflows/test-and-mark-stable.yml` still keeps the three `/pulls/{PR_NUMBER}` reads around SHA stabilization and the later PR-state guard.
  - `REUSE-001` — `.github/workflows/internal-review.yml` still reads repo `default_branch` on the no-PR path.
  - `REUSE-002` — `.github/workflows/review_autofix.yml` still falls back to repo `default_branch` when `base_ref_override` is missing in the no-PR path.
  - `REUSE-003` — `.github/workflows/issue_pr_status.yml` still keeps the fallback PR title/body fetch.
  - `REUSE-004` — `.github/workflows/orchestrate_clarify_respond.yml` and `scripts/resolve_integration_ref.sh` still re-fetch the child issue across steps/helpers.
  - `check-run wait budget / prompt-size reduction / CI contract sharding / helper-resolution-once-per-job / prompt-cache telemetry` — the broader structural changes from this doc remain unlanded.

### `analysis/workflow-optimization-2026-06-06-3.md`
- Implemented by sibling work:
  - `thread-reuse regression cleanup` — `tests/test_codex_thread_reuse_core.py` now asserts the helper-based thread-reuse wiring on this ref rather than the stale literal shell snippet this doc's CI failures cited.
- Already satisfied on this ref:
  - `no-pr metadata synthesis` — `.github/workflows/review_autofix.yml` already synthesizes `PR_PAYLOAD_FILE` and `PR_META_FILE` without hitting PR endpoints when `force_claude_branch_review=true` and no PR exists.
  - `DEAD-API-001` — `read_standalone_state_json()` is still definition-only, so the latent paginated comments path remains unreachable in current repo flows.
- Intentionally deferred:
  - `no-pr fast-path completion` — the no-PR review path still falls back to repo `default_branch` and still runs the later check-run collection step, so the fast path is not fully trimmed yet.
  - `REUSE-001` — `.github/workflows/issue_pr_status.yml` still keeps the fallback PR title/body fetch.
  - `REUSE-002` — `.github/workflows/implement.yml` still parses `TRACKING_ISSUE_NUMBER` for the PR body but does not pass it through to `scripts/orchestrate_force_tick.sh`.
  - `dispatcher / prompt-cache / context-pressure / memory-worktree cleanup` — the broader wrapper-collapse, prompt-observability, and review-worktree recommendations from this doc remain deferred.

### `analysis/workflow-optimization-2026-06-08.md`
- Implemented by sibling work:
  - `CI timeout-prelude staging contract restore` — `.github/workflows/review_autofix.yml` now stages both retry prelude templates with the expected `install -m 0644 ... integration-sync-conflict-resolver-retry-timeout-prelude.txt` contract, which is the safe-subset fix tracked as `#3223` from this source doc's CI regression cluster.
  - `review prompt-cache + Semble fallback telemetry` — the review telemetry path now emits prompt-cache usage counters and distinguishes test-only `SEMBLE_FALLBACK` contract noise additively, matching the safe-subset observability fix tracked as `#3224`.
  - `opaque-phase skip/gate telemetry` — the phase workflows now emit additive skip/gate status telemetry for the previously opaque paths, which is the safe-subset visibility fix tracked as `#3225`.
  - `validate cache action/path cleanup` — `.github/workflows/validate.yml` now uses the corrected cache path/action setup described by the safe-subset fix tracked as `#3226`.
- Already satisfied on this ref:
  - `implementation-failed reissue / blocker-gate regression` — `test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs` was already passing on current main when this source doc was triaged, so the doc's top CI blocker was no longer an open fix on this ref.
  - `keep Semble active; do not tune Serena yet` — the repo still keeps `SEMBLE_ENABLED=true` and `SERENA_ENABLED=false` by default, matching the source doc's recommendation to leave Serena untouched until it emits live traffic.
- Intentionally deferred:
  - `review_autofix` oversized-context fallback — the proposed reviewer fan-out / reasoning reductions after `CONTEXT_BUDGET_WARN` change hot review-path behavior and remain deferred for a dedicated rollout.
  - shorter `CHECK_RUNS_WAIT_TIMEOUT_SECS` defaults — reducing the existing wait budget changes review snapshot timing and fail-open semantics, so it remains deferred.
  - first-pass `implement` reasoning downshift — lowering default implement reasoning from `xhigh` to `high` is still deferred pending a broader runtime-quality evaluation.
  - AI-memory push / fail-open semantics hardening — the suggested rebase+jitter retry contract and the downgrade of some memory writes to soft-fail warnings are broader persistence-behavior changes and remain deferred.
  - reviewer-slot retry tightening — reducing per-slot retry budgets for rate-limited reviewer models remains deferred because it changes the current fail-open / quorum behavior under provider instability.
  - `orchestrate_poll` coalescing / early no-op exit — poll coalescing and changed-work short-circuiting remain deferred because they alter orchestrator pickup latency and scheduling behavior.
  - broader GH API shape cleanup — the doc's plan/review/sweep API bundle collapses are still deferred because they touch hot-path batching, cache reuse, and failure-surface contracts across multiple workflows.
  - broader Semble / overflow-query policy changes — keeping targeted reviewer-context Semble usage while trimming broader implement overflow-query behavior remains deferred until a dedicated retrieval-policy pass.

### `analysis/workflow-optimization-2026-06-08-2.md`
- Implemented by sibling work:
  - `ci fast-fail / early stable guard` — sibling issue `#3246 / PR #3252` added the early `test_implementation_failed_*` fast-fail split in `.github/workflows/ci.yml`, and sibling issue `#3247 / PR #3253` added the matching `Phase 0a: Hot orchestrate-poll regression guard` in `.github/workflows/test-and-mark-stable.yml`.
  - `non-blocking workflow-log-analysis wait` — sibling issue `#3247 / PR #3253` changed `.github/workflows/test-and-mark-stable.yml` to stop after dispatch registration plus one child-run snapshot instead of polling the non-blocking `workflow-log-analysis` child to terminal state.
  - `BUG-001` — sibling issue `#3245 / PR #3255` removed the older broad review-path inline fallback regex copies from `.github/workflows/review_autofix.yml` / `scripts/review_rb_judge.sh` in favor of the shared strict-helper path, so the unsafe bare `issue #N` / `issues/N` matches called out by the source doc are no longer the live common-path behavior on this ref.
  - `DUP-001` — sibling issue `#3249 / PR #3251` now sources `scripts/comprehensive_test_and_release_gh_api.sh` from both `gh_api_safe()` callsites in `.github/workflows/comprehensive-test-and-release.yml`.
  - `MERGE-001`, `MERGE-002`, `DEAD-API-001` — sibling issue `#3257 / PR #3258` taught `.github/workflows/review_autofix.yml` to reuse cached `post_merge_pr_text_json` / `post_merge_linked_issues_json`, and `scripts/review_collect_pr_metadata.sh` now keeps `PR_REVIEWS_FILE=[]` unless break-glass review fetches are explicitly enabled.
  - `REUSE-001` — sibling issue `#3248 / PR #3250` removed the late `/pulls/{PR_NUMBER}` fallback from `.github/workflows/issue_pr_status.yml`, which now only parses cached PR title/body text on the fallback path.
- Already satisfied on this ref:
  - `implementation_failed` reissue regression — the source doc's recommendation to reopen `scripts/orchestrate_poll_process.sh` is stale on this ref. The orchestrator tracking issue already recorded that current HEAD carries the earlier dependency-gate / `pending_issue_defs` repair, and current HEAD now front-loads the matching regression checks in `.github/workflows/ci.yml` and `.github/workflows/test-and-mark-stable.yml`.
  - `keep Semble enabled / defer Serena rollout / Semble-fallback overlap` — `.github/workflows/review_autofix.yml` still defaults `SEMBLE_ENABLED=true` and `SERENA_ENABLED=false`, and the earlier `analysis/workflow-optimization-2026-06-08.md` safe subset already landed the additive Semble-fallback telemetry this follow-up doc repeats.
  - `REUSE-002` — `.github/workflows/internal-review.yml` only reads `default_branch` after the no-open-PR branch, so the existing-PR short-circuit this recommendation wanted is already present on this ref.
  - `REUSE-004` — `.github/workflows/implement.yml` now writes `ISSUE_META_FILE` during checkout-ref resolution and reuses it on later common paths, with live `gh api` reads only as cache-miss / invalid-cache fallback.
- Invalid / obsolete on this ref:
  - `implement git-submodule warning cleanup` — repo-local search only finds the simulated smoke-fixture `git submodule foreach` lines in `.github/workflows/test-and-mark-stable.yml`; there is no live `implement.yml` callsite left to clean up on current HEAD.
- Intentionally deferred:
  - `SEC-001` — `scripts/run_validation_repo_checks.sh` still replaces the default checks with raw CLI arguments and executes each override through `timeout ... /bin/sh -c`, so the shell-reparse hardening proposed in sibling issue `#3244` is not present on this ref.
  - `BUG-002` — `scripts/review_rb_judge.sh` still selects `FIRST_ISSUE` / `FIRST_ISSUE_BODY` / `FIRST_ISSUE_LABELS_JSON` from the first linked issue only, so the canonical-parent selection fix remains a behavior change for a dedicated follow-up.
  - `API-001` — `scripts/gh_helpers.sh` still iterates `gh api "${pr_url}"` once per cross-referenced PR URL on the REST fallback path; keep deferred until a batched helper preserves the same fail-open contract.
  - `API-002` — `scripts/review_collect_pr_metadata.sh` still loops `GET /issues/{n}` across fallback issue numbers when GraphQL linked-issue context is missing; batching remains directionally correct but not yet landed.
  - `API-003` — `scripts/check_external_branch_advance.sh` still documents the per-commit lookup as acceptable because the identity-verification set is usually tiny, so this remains intentionally skipped.
  - `DUP-002` — multiple workflow families still keep their own inline `gh_api_safe` / retry wrappers, including `test-and-mark-stable.yml`, `cancel_on_pr_close.yml`, `mark-stable.yml`, and `orchestrate_poll.yml`.
  - `DUP-003` — six workflows (`clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate.yml`, `orchestrate_poll.yml`, `orchestrate_clarify_respond.yml`) still inline large “Stage workflow support files” blocks instead of reusing `scripts/stage_workflow_support.sh`.
  - `EXPR-001`, `EXPR-002`, `EXPR-003`, `EXPR-004` — the source doc's extraction candidates remain broader YAML/shell churn with no live expression-size breach on this ref, so they stay deferred.
  - `DEAD-001` — `scripts/review_run_reviewers.sh` still carries definition-only leftovers such as `probe_prompt`, `RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE`, `RAW_REVIEWER_SYMBOL_DIFF_SUMMARY_FILE`, and `REVIEWER_HEALTH_LAST_OPEN_UNTIL_EPOCH`; cleanup remains low-risk but not yet wired.
  - `REUSE-003` — `.github/workflows/review_autofix.yml` still resolves the linked issue title during smoke detection when the PR title/body path is not enough, so the last fallback lookup remains.
  - `REUSE-005` — `.github/workflows/orchestrate_clarify_respond.yml` still refetches both the child issue and tracking issue across its early gate and later context step.
  - `review_autofix` right-sizing / `CONTEXT_BUDGET_WARN` circuit breaker / prompt-prefix stability / AI-memory retrieval — the cost-control ideas remain valid, but current HEAD still keeps `REVIEWER_RISK_TIER_ENABLED=0`, dynamic pass-2 cross-pollination, and low-yield AI-memory retrieval, so the broader hot-path policy change stays deferred.
  - `poller lazy tool bootstrap` — `.github/workflows/orchestrate_poll.yml` still installs Codex (and Semble when enabled) whenever `has_work == true`, not only on paths that actually need those tools.
  - `workflow_log_analysis` partial-output chaining — `.github/workflows/workflow-log-analysis.yml` still keeps `api-redundancy` gated on a completed `deep-audit` job, so the earlier partial-artifact handoff remains deferred.

## Preserved machine-maintained artifacts
- `analysis/validation-selftest-status.json` (kept unchanged)
- `analysis/last_collection_timestamp.txt` (kept unchanged)

## Conflicts / assumptions recorded for follow-up
- The approved plan's stale-repo warning for the five `safe_to_apply` items was no longer true by implementation time: current repo state shows those cleanups already merged, so this report records them as actioned.
- `analysis/workflow-optimization-2026-05-23.md` did not use MERGE/REUSE-style IDs, so the ledger groups its repeated source recommendations under stable heading-text labels when multiple sections point at the same landed change.
- The 2026-05-29 through 2026-06-06 source docs also repeated some recommendations across speed/cost/reliability/API sections, so the ledger groups those repeats under stable short labels within each per-doc section.
- `analysis/workflow-optimization-2026-06-05-3.md`'s `MERGE-002` is only partially landed on this ref: `scripts/implement_diagnose_post_codex_failure.sh` prefers `ISSUE_META_FILE`, but the cache-miss path still splits live label/body recovery, so the source-doc item is recorded as deferred.
- `analysis/workflow-optimization-2026-06-08.md` mixed one now-already-satisfied CI regression item with four landed safe-subset fixes and several risky hot-path follow-ups, so this closeout records those buckets separately instead of treating the whole doc as one implementation state.
- `analysis/workflow-optimization-2026-06-08-2.md`'s API summary row says `NEEDS_VERIFICATION | 3` but then enumerates four IDs (`REUSE-003`, `REUSE-005`, `API-001`, `API-002`); this closeout follows the enumerated IDs rather than the stale count cell.
- Tracking issue `#3243` treated `#3244` (`SEC-001`) as part of the safe subset, but current HEAD still shells override checks through `/bin/sh -c`; this closeout therefore records `SEC-001` from code state as deferred rather than crediting the closed sibling issue alone.
- PR `#3255` added `extract_repo_scoped_issue_refs_from_text` to `scripts/gh_helpers.sh`, and current HEAD retains that helper body while `.github/workflows/review_autofix.yml`, `scripts/review_collect_pr_metadata.sh`, and `scripts/review_rb_judge.sh` still reuse it for the strict repo-scoped fallback path.
- With the earlier 2026-05-22/23 source docs, this pass's eight 2026-05-29/06-06 source docs, and `analysis/workflow-optimization-2026-06-08-2.md` now deleted, `.github/workflows/comprehensive-test-and-release.yml` will hit its existing fallback path to `analysis/recommendation-processing-report.md` on future runs.
