# Stop treating issue-comment URLs as linked issues, and pin the non-target merge rule for Claude project PRs

Source issue: shubhodeep1/coding-workflows#5776 (https://github.com/shubhodeep1/coding-workflows/issues/5776)
Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
Security pass: run

## Summary

`issue_pr_status.yml` labelled #4867 `ai:merged` and finalized its AI-memory lineage when conformance-fix PR #5649 merged into #4867's own project branch. Two things combined: the body/title fallback counted a link to a *comment* on #4867 as a linked issue, and on `main` the label is applied before the base-branch gate. This plan narrows the shared fallback helper so a `/issues/N#…` URL is never a linked issue. It also pins, with tests, the target-branch gate that the #4813 project branch already ships, so a `/implement-plan-claude` PR merged into a `claude/implement-plan-*` branch leaves its issue alone.

## Context

- Incident (2026-09-30, run 36731103232): PR #5649 (base `claude/implement-plan-issue-4867-close-permission-prompt-duplicates`, body `Refs #4867` plus two `https://github.com/shubhodeep1/coding-workflows/issues/4867#issuecomment-…` links). The step `Update linked issue labels when PR closes` logged `Found linked issues via PR body/title fallback: 4867`, labelled #4867 `ai:merged` (label call before the `PR_BASE_REF == main` close gate), and `Finalize linked issue lineage state` wrote `final_state: merged`. The poller's close-merged sweep then sent a Telegram WARNING on every poll until the label was removed by hand.
- `extract_repo_scoped_issue_refs_from_text` (`scripts/gh_helpers.sh`, ~line 1662) accepts `github.com/<repo>/issues/<N>` and `<repo>/issues/<N>` followed by any non-word character or end of text. `#` is a non-word character, so `…/issues/4867#issuecomment-5908534539` matches. The helper is shared: `issue_pr_status.yml` (body/title fallback), `review_autofix.yml` (five call sites, including the identical-failure cap that labels linked issues `ai:review-blocked`), `scripts/review_collect_pr_metadata.sh` (`LINKED_ISSUE_FALLBACK_NUMBERS_JSON`), and `scripts/review_rb_judge.sh`.
- Requested change 1 (label and lineage only on a real completion) is already implemented on the #4813 project branch (draft final PR #4826), in the same block of `issue_pr_status.yml`: the target-branch gate (#4813), the managed-label rule (#4957), the automation-head rule (#5226), and the lineage allow-list `LINEAGE_FINALIZE_ISSUE_NUMBERS` (#5227). The default branch comes from the event payload (`github.event.pull_request.base.repo.default_branch`, the same value as `github.event.repository.default_branch` for the base repository), with `main` only when it is absent. #5619 / PR #5643 (tracking-issue lineage) is stacked on the same branch and edits the same loop and `tests/test_issue_pr_status_target_branch_gate.py`.
- On that branch the incident shape is already rejected: #4867 names no `Integration branch:` / `Target branch:` and the head `claude/implement-plan-…` is not an automation head, so the step logs `… leaving its labels and state unchanged.` and does not add #4867 to `LINEAGE_FINALIZE_ISSUE_NUMBERS`. No test pins that for a `claude/implement-plan-*` base with a comment URL, and the comment-URL link itself is still produced.
- `/implement-plan-claude` step 9 relies on a Codex security follow-up (head `ai/issue-<n>`) being labelled `ai:merged` when its PR merges into the project branch its `Integration branch:` line names; the parent chain's issue-list checker waits for that label. The #4813 gate keeps that path (AD-2).

## Goals

- `extract_repo_scoped_issue_refs_from_text` returns no issue for a repo-scoped issue URL or path that carries a `#` fragment (any fragment, `#issuecomment-…` included), whether the `#` follows the issue number directly or a `/…` or `?…` tail such as a notification link's query. Bare `/issues/N` URLs and paths, and `close|fix|resolve` keyword references, are unchanged.
- A PR merged into `claude/implement-plan-x` whose body has only `Refs #N` plus a comment URL on #N: no label call, no close, and #N absent from `LINKED_ISSUE_NUMBERS` and `LINEAGE_FINALIZE_ISSUE_NUMBERS`.
- The same PR with a closing keyword (`Fixes #N`) into `claude/implement-plan-x`, from a `claude/*` head: no label call, no close, #N not in `LINEAGE_FINALIZE_ISSUE_NUMBERS` (the #4813 / #5226 gate, pinned for the Claude-project shape).
- An `ai:orchestrator-managed` child merged into its integration branch from `ai/issue-<n>` still gets `ai:merged` and is closed; a default-branch merge with `Fixes #N` still labels and closes.

## Non-goals

- Changing which merges the target-branch gate accepts (#4813 / #4957 / #5226 / #5227 rules stay; AD-2).
- Tracking-issue lineage (#5619, PR #5643).
- PRs closed without merging: they keep today's `ai:closed` label, close, and `closed` lineage (AD-6).
- `orchestrate_poll_process.sh`'s close sweep (#4813 changes it on the same branch).

## Constraints

- §5: a change to the shared helper's matching only (an inline `python3` scan since review round 1 after intervention 1, AD-8; this plan first proposed one regex boundary), plus tests and docs. No change to the label/close loop.
- §6: no identifier is renamed. The helper keeps its name and signature. New test names are unique.
- §9: shell keeps the helper's tab indentation, YAML stays 2-space, and tests use tabs.
- §15: no new GitHub API call; the helper is pure text processing.
- §19: phase and fix PRs say `Refs #5776`. The final PR targets the #4813 project branch, so it also says `Refs #5776`, and the final-merge stage closes #5776 explicitly with `ai:merged`.
- §20: observable behaviour change (fewer linked issues), so one `changelog.d/5776-ignore-comment-url-issue-links.md` fragment (`fixed`).
- §27: `issue_pr_status.yml` is untouched; `ci.yml` grows by one line.

## Approach

In `extract_repo_scoped_issue_refs_from_text`, change the trailing boundary of the two URL/path alternatives from `([^[:alnum:]_]|$)` to `([^[:alnum:]_#]|$)`. A number followed by `#` then matches no alternative: shorter prefixes are followed by a digit, and the keyword alternative needs `<keyword> #N`. Review round 2 on PR #5825 widened this: each URL/path alternative also takes an optional `/…` or `?…` tail (up to whitespace, `#`, `<`, `>`; review round 3 let it run through `(` and `)`; review round 4 puts a line break before every repo-scoped `<repo>/issues/<n>` that follows a non-word character, so a tail never runs into the next issue link), a match ending in `#` is dropped, and the number is read from just after `issues/`, so `…/issues/N?query#issuecomment-…` is rejected too and a digit in the query is never read as the issue. Review round 1 after intervention 1 found that the round-4 split also fired inside a URL's query or fragment (`…/issues/1?next=<repo>/issues/2#c` linked #1, `…/issues/1#see-<repo>/issues/2` linked #2), so the matching moved to an inline `python3` scan, as `issue_body_integration_branch` did for #4956 (AD-8): each URL is taken whole, the way GitHub renders it (up to whitespace, `<`, or `>`, and a Markdown link destination up to its first unmatched `)`), a `#` anywhere in it after the issue number drops it, and an issue path inside it is never a link of its own. Review round 2 after intervention 1 extended that to URLs on other hosts (an issue path whose run, after any Markdown `](`, already holds `://`, `?`, or `#`, or ends with `/`, belongs to that URL) and made a backslash-escaped character part of a Markdown link destination, as GitHub reads it. A closing keyword inside a URL does not count, and one right after another match is no longer hidden by it. The doc comment gains the rejected examples. Narrowing the shared helper (AD-3) gives every caller one meaning of "linked issue". The `review_autofix.yml` identical-failure cap would otherwise label a comment-linked issue `ai:review-blocked` the same way.

The label/lineage gate needs no code change on this base (AD-2). New runtime tests drive the real step script through the existing stub harness and pin the four cases the issue lists, plus the keyword case into a Claude project branch.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan. The change is a one-line helper fix plus its tests and docs, and a split would ship the tests apart from the behaviour they pin.

1. **Phase 1: comment-URL narrowing and gate tests.** Files: `scripts/gh_helpers.sh`, `tests/test_issue_pr_status_comment_url_links.py` [new], `.github/workflows/ci.yml` (register the module), `README.md` (`issue_pr_status.yml` row), `changelog.d/5776-ignore-comment-url-issue-links.md` [new]. Done when the new module passes as a script and under pytest. It must fail against the unmodified helper for the comment-URL cases. The existing `test_issue_pr_status_*`, `test_gh_helpers_*`, and helper tests in `test_review_autofix_review_pipeline_contract.py` must still pass. Rollback: revert the phase PR, and the helper returns to counting comment URLs.

## Implementation Steps

1. `scripts/gh_helpers.sh`: narrow the two URL/path boundaries in `extract_repo_scoped_issue_refs_from_text` and extend its doc comment (Rejected: `owner/repo/issues/56#issuecomment-1`).
2. `tests/test_issue_pr_status_comment_url_links.py`: load `_run_step` from `tests/test_issue_pr_status_target_branch_gate.py` by path, so the stub harness is reused and the file #5643 edits is not touched. Add:
   - direct helper tests: a comment URL alone yields nothing; `/issues/N#anything` and `owner/repo/issues/N#x` yield nothing; bare URLs, paths, and keyword refs still match, including mixed text;
   - step tests: the incident shape into `claude/implement-plan-x` (no label, no close, empty linked and lineage lists); `Fixes #N` into `claude/implement-plan-x` from a `claude/*` head (no label, no close, not in the lineage list); a labelled managed child merged into its integration branch from `ai/issue-N` (label and close, in the lineage list); a default-branch `Fixes #N` merge (label and close, in the lineage list).
3. `.github/workflows/ci.yml`: run the new module next to `test_issue_pr_status_target_branch_gate.py`.
4. `README.md`: one sentence at the end of the `issue_pr_status.yml` row saying the body/title fallback ignores issue URLs with a `#` fragment (issue #5776).
5. `changelog.d/5776-ignore-comment-url-issue-links.md` (`fixed`).

## Files & Modules

- `scripts/gh_helpers.sh`
- `tests/test_issue_pr_status_comment_url_links.py` [new]
- `.github/workflows/ci.yml`
- `README.md`
- `changelog.d/5776-ignore-comment-url-issue-links.md` [new]

## Testing

- `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_issue_pr_status_comment_url_links.py` (the way `ci.yml` runs it) and under pytest.
- Regression: `tests/test_issue_pr_status_target_branch_gate.py`, `tests/test_issue_pr_status_payload_fallback_contract.py`, `tests/test_gh_helpers_issue_body_integration_branch.py`, `tests/test_gh_helpers_issue_automation_branch.py`, the `extract_repo_scoped_issue_refs` and linked-issue fallback tests in `tests/test_review_autofix_review_pipeline_contract.py`, and every other test that greps `extract_repo_scoped_issue_refs_from_text`.
- A red/green check: the new comment-URL tests fail against the base branch's helper.

## Risks

- A PR that links its own subject issue *only* through a `#fragment` URL loses the fallback link. That case is the defect itself, and closing keywords and bare URLs still link.
- Review-pipeline callers see fewer fallback links, so the identical-failure cap labels the PR itself instead of the comment-linked issue. That is intended.
- Consumers fetch `scripts/gh_helpers.sh` from `stable`, so the narrowing reaches them only after the #4813 project merges into `main` and is marked stable.

## Rollout

Lands on the #4813 project branch and ships to `main` with that project's final PR #4826. It reaches consumers on the next `@stable` sync, with no variable and no migration.

## Auto-decisions

- AD-1 [plan, 2026-10-01] The issue names no `Integration branch:`, but its requested change 1 is already built on the #4813 project branch, in the same `issue_pr_status.yml` block, which #4826 has not merged yet. Where should this project be built? — Picked: A — on `claude/implement-plan-issue-4813-close-sweep-target-branch-merges`, extending that gate. Alternatives: B — on `main`, re-implementing a label/lineage gate there; C — on `main`, helper narrowing only, leaving change 1 to #4826. Why: §5 says extend existing mechanisms and never compete with them. B duplicates #4813's gate and conflicts with #4826 and #5643 in the same block. C closes #5776 on `main` with change 1 still missing there. #5227 and #5619 used the same base. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Change 1 says to label only default-branch merges and managed children. The #4813 gate also labels (without closing) a standalone issue merged into its own declared `Integration branch:` / `Target branch:` from a same-repository automation head. Which rule? — Picked: A — keep the #4813 gate and pin it with tests. Alternatives: B — the literal rule, dropping the declared-integration-branch path. Why: B removes `ai:merged` from Codex security follow-ups merged into a project branch, and `/implement-plan-claude` step 9's checker waits for that label (a documented contract, §12.D). A already rejects every `/implement-plan-claude` phase, fix, and conformance PR, because their heads are not automation heads. The default branch already comes from the payload. Applied in: no code change (tests in phase 1 PR). Status: pending review
- AD-3 [plan, 2026-10-01] Narrow the shared `extract_repo_scoped_issue_refs_from_text`, or add a narrower sibling only for `issue_pr_status.yml`? — Picked: A — narrow the shared helper. Alternatives: B — a new sibling helper. Why: a comment URL is not a linked issue for any caller (the review identical-failure cap has the same defect). It is one regex boundary, and B would leave two meanings of "linked issue". Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Which fragments disqualify a URL? — Picked: A — any `#` right after the issue number, as the issue states. Alternatives: B — only `#issuecomment-…`. Why: the issue asks for any `#…` fragment; `/issues/N/`, `?query`, and punctuation keep matching. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-01] Where do the tests live? — Picked: A — a new module `tests/test_issue_pr_status_comment_url_links.py` registered in `ci.yml`, reusing the gate module's harness by path. Alternatives: B — append to `tests/test_issue_pr_status_target_branch_gate.py`. Why: PR #5643 is rewriting that file on the same base, and a separate module avoids conflicting edits, which the issue asks for. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-01] Should PRs closed without merging into a non-default branch also stop labelling `ai:closed` and closing their linked issue? — Picked: A — no, unchanged. Alternatives: B — gate them on the base too. Why: §5, the issue scopes change 1 to merges (`ai:merged`). After this change a comment URL no longer links an issue, which removes the incident shape for unmerged closes too. Applied in: no code change. Status: pending review
- AD-7 [plan, 2026-10-01] The issue asks to update the `agents.md` line for the status sync "if one describes this behaviour". — Picked: A — `agents.md` has no such line (its only `issue_pr_status.yml` line covers lessons ingestion), so update the README `issue_pr_status.yml` row instead. Alternatives: B — add a new `agents.md` line. Why: §5, §7. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": false, "label": null, "reason": "no skip label"}`.
- Coordination with #5619 / PR #5643: this plan does not edit `issue_pr_status.yml` or `tests/test_issue_pr_status_target_branch_gate.py`. The only shared file is the `README.md` `issue_pr_status.yml` row, where whichever PR lands second resolves a one-line merge.
