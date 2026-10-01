# Implement-Plan Log — Stop treating issue-comment URLs as linked issues, and pin the non-target merge rule for Claude project PRs

- Plan: docs/plans/issue-5776-ignore-comment-url-issue-links-plan.md
- Source issue: shubhodeep1/coding-workflows#5776
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5776-ignore-comment-url-issue-links   Final PR: #5790 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5825
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Rqqfni66CCQPdBzVGK14Nh   safety net and hand-back: see the round-4 stage report (session_0118cTK7yVihfRpGo8noXVqq)
- Last updated: 2026-10-01
- Last note: review round 4 on PR #5825: fixed the consensus finding (the URL tail ran into an adjacent issue link, so `[one](…/issues/1?q=1),[two](…/issues/2)` yielded only 1, and a `#` on the second link dropped both) by breaking the line before each repo-scoped issue link; regression tests, doc comment, README row, changelog row, and plan updated; waiting on round 5 or merge.

## Phases
1. [ ] Phase 1 — narrow `extract_repo_scoped_issue_refs_from_text` so `/issues/N#…` URLs are not linked issues; runtime tests pinning the target-branch gate for Claude project PRs; ci.yml, README row, changelog fragment   — PR #5825 open (waiting); review rounds: 4; interventions: 0

## Conformance

## Security pass

## Validation

## Completion
- Final PR #5790 draft

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] The issue names no `Integration branch:`, but its requested change 1 is already built on the #4813 project branch, in the same `issue_pr_status.yml` block, which #4826 has not merged yet. Where should this project be built? — Picked: A — on `claude/implement-plan-issue-4813-close-sweep-target-branch-merges`, extending that gate. Alternatives: B — on `main`, re-implementing a label/lineage gate there; C — on `main`, helper narrowing only, leaving change 1 to #4826. Why: §5 says extend existing mechanisms and never compete with them. B duplicates #4813's gate and conflicts with #4826 and #5643 in the same block. C closes #5776 on `main` with change 1 still missing there. #5227 and #5619 used the same base. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Change 1 says to label only default-branch merges and managed children. The #4813 gate also labels (without closing) a standalone issue merged into its own declared `Integration branch:` / `Target branch:` from a same-repository automation head. Which rule? — Picked: A — keep the #4813 gate and pin it with tests. Alternatives: B — the literal rule, dropping the declared-integration-branch path. Why: B removes `ai:merged` from Codex security follow-ups merged into a project branch, and `/implement-plan-claude` step 9's checker waits for that label (a documented contract, §12.D). A already rejects every `/implement-plan-claude` phase, fix, and conformance PR, because their heads are not automation heads. The default branch already comes from the payload. Applied in: no code change (tests in phase 1 PR). Status: pending review
- AD-3 [plan, 2026-10-01] Narrow the shared `extract_repo_scoped_issue_refs_from_text`, or add a narrower sibling only for `issue_pr_status.yml`? — Picked: A — narrow the shared helper. Alternatives: B — a new sibling helper. Why: a comment URL is not a linked issue for any caller (the review identical-failure cap has the same defect). It is one regex boundary, and B would leave two meanings of "linked issue". Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Which fragments disqualify a URL? — Picked: A — any `#` right after the issue number, as the issue states. Alternatives: B — only `#issuecomment-…`. Why: the issue asks for any `#…` fragment; `/issues/N/`, `?query`, and punctuation keep matching. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-01] Where do the tests live? — Picked: A — a new module `tests/test_issue_pr_status_comment_url_links.py` registered in `ci.yml`, reusing the gate module's harness by path. Alternatives: B — append to `tests/test_issue_pr_status_target_branch_gate.py`. Why: PR #5643 is rewriting that file on the same base, and a separate module avoids conflicting edits, which the issue asks for. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-01] Should PRs closed without merging into a non-default branch also stop labelling `ai:closed` and closing their linked issue? — Picked: A — no, unchanged. Alternatives: B — gate them on the base too. Why: §5, the issue scopes change 1 to merges (`ai:merged`). After this change a comment URL no longer links an issue, which removes the incident shape for unmerged closes too. Applied in: no code change. Status: pending review
- AD-7 [plan, 2026-10-01] The issue asks to update the `agents.md` line for the status sync "if one describes this behaviour". — Picked: A — `agents.md` has no such line (its only `issue_pr_status.yml` line covers lessons ingestion), so update the README `issue_pr_status.yml` row instead. Alternatives: B — add a new `agents.md` line. Why: §5, §7. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:intervention] When a changelog or plan cites how many places call a shell helper, count invocation lines only; `type <helper> >/dev/null` availability guards sit beside each call and are not call sites. (files: changelog.d/5776-ignore-comment-url-issue-links.md, .github/workflows/review_autofix.yml)
- [source:intervention] A matcher that must reject URLs with a `#` fragment has to look past the path and query, not just the character after the id: GitHub notification links put `?notification_referrer_id=…` before `#issuecomment-…`. Match the whole URL tail, then drop matches that end in `#`, and read the id from its own position rather than the last number. (files: scripts/gh_helpers.sh)
- [source:intervention] A URL tail matcher that must see a later `#` should stop only at characters a URL cannot contain unencoded (whitespace, `<`, `>`, `#`); stopping at `(` or `)` to respect Markdown link syntax hides a fragment after a parenthesised query value. A Markdown link's closing `)` can safely join the tail when only a trailing `#` drops the match. (files: scripts/gh_helpers.sh)
- [source:intervention] A matcher whose URL tail may run through `)`, `,`, or `[` must still stop where the next link starts, or adjacent links merge into one match: split the text before each link (a line break, not a space, since `grep -o` consumes a trailing delimiter that the next match needs as its prefix), and test adjacent links with no whitespace between them. (files: scripts/gh_helpers.sh)

## Notes
- Issue progress comment: 5922355476.
- Local verification (2026-10-01): of the sweep of tests that load `gh_helpers.sh`, 542 passed and 1 failed: `test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean`, failing with `gawk: command not found` in the container, identical with the old helper.
- `security_pass_skip.py`: `{"skip": false, "label": null, "reason": "no skip label"}` → `Security pass: run`.
- Base check (2026-10-01): no merged PR with head `claude/implement-plan-issue-4813-close-sweep-target-branch-merges`; its final PR #4826 is an open draft into `main`.
- Round 1 (2026-10-01): the stage synced the project branch with the issue base (merge c319847, which brought in the #5617 unmerged-close gate; no conflict, gate and helper tests pass) and pushed it, which made PR #5825 conflict in the README `issue_pr_status.yml` row. Resolved by keeping the base's row and appending this project's #5776 sentence. Base check: still no merged PR with head `claude/implement-plan-issue-4813-close-sweep-target-branch-merges`.
- Round 1 also corrected the same stale count in the plan's Context section (four → five `review_autofix.yml` call sites), a proactive stale-doc fix.
- Round 2 (2026-10-01): six reviewers agreed the boundary only checked the character right after the issue number. Fixed in `extract_repo_scoped_issue_refs_from_text`; README row, changelog fragment, and the plan's Goals/Approach updated to match. Base check: still no merged PR with head `claude/implement-plan-issue-4813-close-sweep-target-branch-merges`; project branch already up to date with it.
- Round 3 (2026-10-01): six reviewers agreed the URL tail stopped at `(` or `)`, so `…/issues/12?q=(a)#issuecomment-1` still linked #12. The tail now runs through parentheses; doc comment, the plan's Approach sentence, and a new parentheses test (red on the round-2 helper, green now) updated. Base check: still no merged PR with head `claude/implement-plan-issue-4813-close-sweep-target-branch-merges`; project branch already up to date with it.
- Round 4 (2026-10-01): six reviewers agreed the widened tail swallowed an adjacent issue link (`[one](…/issues/1?q=1),[two](…/issues/2)` → `[1]`; with a `#` on the second link, `[]`). The helper now puts a line break before each repo-scoped `<repo>/issues/<n>` that follows a non-word character, so each link is matched on its own; new test `test_adjacent_issue_links_each_count_on_their_own` (red on the round-3 helper, green now). Rejected: the `grep -viE 'issues/[0-9]+.*#$'` filter concern, because the tail excludes `#`, so a fragment can only be a match's last character. Base check: still no merged PR with head `claude/implement-plan-issue-4813-close-sweep-target-branch-merges`; project branch already up to date with it.
