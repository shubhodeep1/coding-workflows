# Dispatched review runs check out the PR head

Source issue: shubhodeep1/coding-workflows#5824 (https://github.com/shubhodeep1/coding-workflows/issues/5824)
Base branch: main
Security pass: run

## Summary

A `review_autofix.yml` run started by `workflow_dispatch` (the hourly sweep's
re-runs, the Claude-fixer `claude_fixer_converged_head` convergence dispatch)
checks out `github.sha`, the default branch, into `GITHUB_WORKSPACE`, and the
reviewer panel reads its files from there. The reviewers see the PR's diff
through `GIT_DIR` but the default branch's files on disk, so they report every
change in the PR as missing. This plan makes dispatched runs check out the
gate-verified PR head instead.

## Context

- Observed on PR #5097, run 36794195824: 30 false "missing at HEAD" ledger
  entries citing main's line numbers.
- `.github/workflows/review_autofix.yml:2448` (`codex-agent` → "Checkout repo")
  uses `ref: ${{ github.event.pull_request.head.sha || github.sha }}`. A
  dispatch (direct, or `workflow_call` from `internal-review.yml` /
  `ai-review.yml` on a dispatch) has no `pull_request` payload.
- "Activate workspace shell context" (`review_autofix.yml:2856-2870`) sets
  `GIT_DIR=${GITHUB_WORKSPACE}/.git` and `GIT_WORK_TREE=${WORKSPACE_PATH}`;
  "Checkout PR head branch" (`:3498`) then moves only `WORKSPACE_PATH` to the
  head. `GITHUB_WORKSPACE` keeps the default-branch files.
- Reviewer-side reads that use `GITHUB_WORKSPACE`:
  `scripts/review_run_reviewers.sh:666,984,1036,1046,1874,4300` (cache probe,
  symbol summary, uninteresting-file filter, targeted file context, OpenCode
  reviewer workspace), `scripts/summarize_reviewer_consensus.sh:267`,
  `scripts/review_agents_md_materiality.sh:300`, the slop scan
  (`review_autofix.yml:4006`).
- The gate's existing `/pulls/<n>` fetch (`review_autofix.yml:514-516`)
  already returns `head.sha` and exports it as `needs.gate.outputs.head_sha`.
- Moving review dispatches to the default branch (#4701, #4898) made this path
  common.

## Goals

- A `workflow_dispatch` (or `workflow_call` from a dispatch) review of a
  same-repository PR checks out the PR head SHA the gate read into
  `GITHUB_WORKSPACE`, so every reviewer file read sees the head's files.
- No reviewer-file-read path depends on `github.sha` for a same-repository PR
  review.
- `pull_request`-triggered runs and the no-PR `claude/**` push path keep their
  current checkout.
- A regression test fails if a dispatched run would check out `github.sha`
  for a PR review.

## Non-goals

- Re-pointing the individual reviewer scripts to `WORKSPACE_PATH` (AD-1).
- Reviewing fork-head PRs on dispatch (AD-2): fork PRs cannot run this
  workflow on `pull_request` either (no `GH_PAT`), so they stay unsupported.
- The `HEAD_SHA` env of the interim judge, behavioural smoke synthesis, and
  claude-branch consensus comment steps (AD-3).
- The race where the branch tip moves between the gate and "Checkout PR head
  branch"; it already exists for `pull_request` runs.

## Constraints

- §1: no new exposure of untrusted code to secrets. `GITHUB_WORKSPACE` only
  receives a head that lives in this repository, exactly what a
  `pull_request` run already checks out.
- §5 minimal change: one gate output, one checkout expression.
- §6: new identifier `review_checkout_sha` (gate output) and shell variables
  `pr_head_repo_gate` / `review_checkout_sha_gate`; none exist in the repo
  today. `head_sha` and every other output keep their meaning.
- §15: the head repository rides the existing `/pulls/<n>` fetch (one more jq
  field); no new API call.
- §20: changelog fragment. §27: the workflow stays under 480,000 bytes.
- §14: consumers receive the change through `@stable` like any
  `review_autofix.yml` change; no wrapper change is needed because the logic
  is inside the reusable workflow.

## Approach

1. Gate (`Evaluate review gate`): add `head_repo: (.head.repo.full_name // "")`
   to the existing `/pulls/<n>` jq projection, parse it into
   `pr_head_repo_gate`, and compute `review_checkout_sha_gate`:
   the gate's `pr_head_sha_gate` when it is a 40-hex SHA and
   `pr_head_repo_gate` equals `REPOSITORY`; otherwise empty. When a PR exists,
   the event is not `pull_request`, and the head is not in this repository,
   log `AUTOFIX_GATE_REVIEW_CHECKOUT pr=<n> head_sha=<sha> head_repo=<repo>
   checkout=event_sha reason=cross_repo_head` plus a `::warning::`; a
   resolved checkout logs `checkout=pr_head`. Export
   `review_checkout_sha=<value>` as a gate output.
2. `codex-agent` → "Checkout repo":
   `ref: ${{ github.event.pull_request.head.sha || needs.gate.outputs.review_checkout_sha || github.sha }}`.
   `pull_request` runs still take the event SHA; the no-PR push path has no
   PR number, so `review_checkout_sha` is empty and `github.sha` (the pushed
   SHA) is used.

The alternative (repoint ~10 reviewer reads in 4 scripts to `WORKSPACE_PATH`)
was rejected (AD-1): it touches many files, misses any reader added later, and
leaves `GITHUB_WORKSPACE` and `WORKSPACE_PATH` disagreeing.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — check out the gate-verified PR head on dispatched review runs.**
   - Files: `.github/workflows/review_autofix.yml`,
     `tests/test_review_autofix_merge_precheck.py`,
     `tests/test_review_autofix_dispatch_pr_head_checkout.py` [new],
     `agents.md`, `changelog.d/5824-dispatched-review-pr-head-checkout.md` [new].
   - Done when: the gate exports `review_checkout_sha` from the existing
     `/pulls` fetch, the checkout uses it before `github.sha`, the new test
     covers pull_request / same-repo dispatch / cross-repo dispatch / no-PR
     push, the updated assertion passes, the review workflow test suites pass,
     and the workflow is under 480,000 bytes.
   - Rollback: revert the PR; the checkout expression returns to the old one.

## Implementation Steps

1. `review_autofix.yml` gate: add the `head_repo` jq field, parse
   `pr_head_repo_gate` next to `pr_head_sha_gate`, initialise both new shell
   variables with the others (`:480-490`).
2. Compute `review_checkout_sha_gate` and log it just before the `head_sha=`
   output (`:1548-1551`); write `review_checkout_sha=` to `GITHUB_OUTPUT`;
   declare `review_checkout_sha` under `gate.outputs`.
3. "Checkout repo" (`:2448`): new `ref` expression and a comment citing
   #5824.
4. Update `tests/test_review_autofix_merge_precheck.py:61` to the new
   expression.
5. New `tests/test_review_autofix_dispatch_pr_head_checkout.py`: parse the
   workflow YAML; assert the checkout `ref` expression, the gate output
   wiring, the jq field, and the same-repo rule; run the gate's checkout-SHA
   shell fragment (extracted from the workflow) under bash for the four
   event shapes and assert the SHA the expression resolves to.
6. `agents.md`: one paragraph on which commit `GITHUB_WORKSPACE` holds for
   dispatched runs. Changelog fragment.

## Files & Modules

- `.github/workflows/review_autofix.yml`
- `tests/test_review_autofix_merge_precheck.py`
- `tests/test_review_autofix_dispatch_pr_head_checkout.py` [new]
- `agents.md`
- `changelog.d/5824-dispatched-review-pr-head-checkout.md` [new]

## Tests

- New regression test (unit, runs the extracted gate fragment under bash).
- Existing: `tests/test_review_autofix_merge_precheck.py`,
  `tests/test_review_autofix_claude_fixer_mode.py`,
  `tests/test_review_autofix_terminal_same_head_gate.py`,
  `tests/test_review_autofix_review_pipeline_contract.py`,
  `tests/test_workflow_file_size_limit.py`, and the repo's workflow lint
  (`actionlint` when installed).
- End to end: the next sweep or convergence dispatch on a `claude/*` PR; its
  "Checkout repo" log shows the PR head SHA and the gate logs
  `AUTOFIX_GATE_REVIEW_CHECKOUT ... checkout=pr_head`.

## Risks & Mitigations

- The head moves between the gate and the checkout → `GITHUB_WORKSPACE` holds
  the gate's head, which is still a PR commit; the same race exists for
  `pull_request` runs. ACCEPTED, out of scope.
- A fork head on dispatch still reviews the default branch's files → logged
  with a warning; fork PRs are unsupported by this workflow. ACCEPTED (AD-2).
- A consumer pinned to an older `review_autofix.yml` keeps the bug until its
  next `@stable` sync. ACCEPTED, normal propagation.

## Rollout

No flag. Ships to this repo on merge (internal-review.yml calls
`review_autofix.yml@main`) and to consumers on the next `@stable` sync.
Rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-10-01] How should dispatched runs give reviewers the PR head's files? — Picked: A — check out the gate-verified PR head SHA into `GITHUB_WORKSPACE` for non-`pull_request` runs through a new gate output. Alternatives: B — repoint every reviewer-side read to `WORKSPACE_PATH` (~10 sites, 4 scripts); C — both. Why: one change covers every `GITHUB_WORKSPACE` reader and matches what `pull_request` runs already do (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What should a dispatched run do for a PR whose head is in another repository? — Picked: A — keep the `github.sha` checkout and log a warning. Alternatives: B — skip the run; C — check out the fork head. Why: C would put untrusted fork code next to secrets that a fork `pull_request` run never gets (§1); B changes gate routing for a case the workflow does not support anyway (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] Should the `HEAD_SHA` env of the interim judge, behavioural smoke, and claude-branch consensus steps also switch to the gate head? — Picked: A — no, out of scope. Alternatives: B — switch them too. Why: they are not reviewer file reads, and the issue's acceptance criteria do not cover them (§5). Applied in: no code change. Status: pending review

## References

- Issue #5824; PR #5097 / run 36794195824; #5068, #4701, #4898.
