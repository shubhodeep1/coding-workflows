# Recommendation processing report

Grounding note: this report folds the prior recommendation triage into one final artifact. "Actioned" is based on current repository state on this ref, not on historical intent or external GitHub issue state.

## Processed source docs (66)
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

## Downstream local issue ID recheck (repo-state only)
| Local ID | Recommendation | Files checked | Result | Current repo evidence |
|---|---|---|---|---|
| `apply-safe-e2e-smoke-cleanups` | `capture-issue-url-from-create-response` | `.github/workflows/test-and-mark-stable.yml` | not verified landed on this ref | The create-issue step still posts to `repos/${TEST_REPO}/issues` for `.number` and then immediately calls `gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" --jq '.html_url'` only to recover the URL in the same step. |
| `apply-safe-issue-pr-status-cleanups` | `collapse-duplicate-linked-issue-graphql-read` | `.github/workflows/issue_pr_status.yml` | not verified landed on this ref | The workflow still does one GraphQL `closingIssuesReferences` read that returns only issue numbers, then a second batched GraphQL issue lookup to fetch labels/body for those same issue numbers. |
| `apply-safe-internal-review-cleanups` | `internal-review-lazy-default-branch-lookup` | `.github/workflows/internal-review.yml` | not verified landed on this ref | The `Resolve PR for head branch` step still fetches `base_ref="$(gh api "repos/${REPOSITORY}" --jq '.default_branch')"` before it checks whether an open PR already exists and exits with `proceed=false`. |
| `apply-safe-implement-diagnose-cleanups` | `implement-diagnose-single-issue-cache-miss-fetch` | `scripts/implement_diagnose_post_codex_failure.sh` | not verified landed on this ref | On the straight-line cache-miss path, the script still calls `GET /issues/{n}` once to rebuild `ISSUE_LABELS_JSON` and later calls `GET /issues/{n}` again to refill `ISSUE_BODY_FILE`. |
| `apply-safe-orchestrate-poll-cleanups` | `internal-wrapper-stop-passing-deprecated-caller-workflow` | `.github/workflows/internal-orchestrate-poll.yml` | not verified landed on this ref | The reusable-wrapper job still passes `caller_workflow: internal-orchestrate-poll.yml` into `.github/workflows/orchestrate_poll.yml`, even though that input is documented as deprecated/ignored. |

## Actioned recommendations
None of the five downstream local issue IDs from the approved plan could be verified as landed on this ref, so there are no repo-verified actioned entries to record here.

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

## Preserved machine-maintained artifacts
- `analysis/validation-selftest-status.json` (kept unchanged)
- `analysis/last_collection_timestamp.txt` (kept unchanged)

## Conflicts / assumptions recorded for follow-up
- The approved plan warned that the five `safe_to_apply` items still looked unlanded on the planning ref; the current repo still shows those pre-change behaviors, so they remain recorded as pending rather than actioned.
- `.github/workflows/comprehensive-test-and-release.yml` currently errors with `No workflow analysis report found under analysis/.` when no `analysis/workflow-optimization-*.md` files exist. This cleanup removes all 64 triaged dated backlog reports, so that workflow dependency needs a separate follow-up before merge/deploy automation relies on the old glob.
