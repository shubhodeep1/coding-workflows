# Stream every PR comment in the merge hold gate

Source issue: shubhodeep1/coding-workflows#5566 (https://github.com/shubhodeep1/coding-workflows/issues/5566)
Base branch: claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims
Security pass: skip (ai:security: automation-produced issue)

## Summary

`scripts/claude_merge_hold_gate.py` reads a `claude/*` PR's comments through `check_in_status.gh_api_list`, which refuses after 10 pages. Anyone who can comment can push a PR past 1,000 comments, and then every gate run fails closed (`gate_unavailable`) and the PR can never auto-merge. This plan replaces that read with a streaming reader that walks every page, keeps only trusted claim-marker lines in memory, retries transient read failures, and fails closed only when a page really cannot be read.

## Context

- Issue #5566 (security audit, `STRIDE-Denial of Service`, medium, confidence 9/10) points at `scripts/claude_merge_hold_gate.py:166`: `check_in_status.gh_api_list(f"repos/{repo}/issues/{number}/comments")`.
- `gh_api_list` (`.claude/scripts/check_in_status.py:180-196`) stops at `MAX_PAGINATED_API_PAGES = 10` (`:107`) and raises `ReadError`. The gate turns that into exit 2, and both callers (`scripts/review_enable_auto_merge.sh:211-236` and the `merge_hold_gate_allows` function of the `deterministic-skip-merge` job in `.github/workflows/review_autofix.yml:1916-1937`) refuse the merge on exit 2.
- The gate was added by issue #5316 (`docs/plans/issue-5316-gate-auto-merge-on-hold-claims-plan.md`), which is still in flight on the base branch.
- The claim decision itself is `check_in_status.read_fix_claims` (`:514-567`): only comments by an OWNER / MEMBER / COLLABORATOR whose login is in `_fix_claim_trusted_logins(pr)` count, the body must hold exactly one line that fully matches `FIX_CLAIM_RE`, and the latest such claim on the head decides.

## Goals

- A PR with more than 1,000 comments (any number of pages) gets a real hold / no-hold decision from the gate instead of `gate_unavailable`.
- Memory held by the gate stays proportional to the trusted claim markers on the PR, not to the number or size of comments.
- A transient read failure on one page is retried (3 attempts in total, 1 s then 2 s back-off) before the gate fails closed.
- A page that cannot be read after the retries, or a page with the wrong shape, still fails closed (exit 2, `gate_unavailable`), as today.
- The hold decision is unchanged for every input the old reader could handle: same trust rules, same "exactly one marker line" rule, same "latest claim on the head decides" rule.

## Non-goals

- `check_in_status.gh_api_list` and its 10-page cap stay as they are, and so does every other caller of it (the §26 checker, `check_pr_hand_back`, the catch-all sweep). Those callers retry on the next check-in rather than refusing a merge, and `.claude/**` is a protected path (AD-5).
- Page-number pagination is kept. A comment deleted on an earlier page while the gate is reading can shift one comment past a page boundary; that behaviour predates this change and is shared with `check_in_status.py` (AD-6).
- No change to the callers, the log keys, the exit codes, or the JSON output fields.

## Constraints

- §1: security first, so the gate still fails closed on any read it cannot complete.
- §5: minimal change; only the comment read in the gate changes.
- §6: no identifier is renamed or removed. New names (`COMMENT_PAGE_SIZE`, `COMMENT_READ_ATTEMPTS`, `COMMENT_RETRY_DELAYS_SECONDS`, `_read_comment_page`, `_stream_claim_comments`) were checked against the gate module and the imported `check_in_status` names and do not collide.
- §9: tabs, opening braces not applicable (Python).
- §15: one API call per 100 comments, as before, plus at most 2 retries per failing page. No new endpoint, no GraphQL.
- §20: a `changelog.d/` fragment is required (security fix).
- §27: no workflow file changes.

## Approach

Add to `scripts/claude_merge_hold_gate.py`:

1. `_read_comment_page(check_in_status, path, page)` — one `GET <path>?per_page=100&page=<page>` through `check_in_status._gh_api_json`, retried on `ReadError` up to `COMMENT_READ_ATTEMPTS = 3` times with `COMMENT_RETRY_DELAYS_SECONDS = (1, 2)` between attempts. A non-array page or a non-object item raises `GateUnavailable` immediately (a shape error is not transient). After the last failed attempt it raises `GateUnavailable` naming the page and the attempt count.
2. `_stream_claim_comments(check_in_status, repo, number, trusted_logins)` — reads page 1, 2, 3, … until a page shorter than 100 items. For each comment it keeps a trimmed copy only when the author association is trusted, the login is in `trusted_logins`, and the body has at least one line that fully matches `check_in_status.FIX_CLAIM_RE`. The copy keeps `id`, `user.login`, `author_association`, `created_at`, and a body made of the matching lines only, so `read_fix_claims` sees the same marker count and makes the same decision. There is no page limit.
3. `evaluate` calls `_stream_claim_comments` instead of `gh_api_list` and passes its result to `read_fix_claims` unchanged.

Alternatives considered: raising `MAX_PAGINATED_API_PAGES` (only moves the refusal threshold, and it lives in a protected `.claude/` path); `gh api --paginate` (one subprocess, but a transient failure restarts the whole read and the output is held in memory at once).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the change is one function swap in one script plus its tests, and it cannot be split into independently useful parts.

1. **Phase 1 — stream the gate's comment read.** Files: `scripts/claude_merge_hold_gate.py`, `tests/test_claude_merge_hold_gate.py`, `README.md`, `agents.md`, `changelog.d/5566-merge-gate-comment-flood.md`. Done when the new tests pass, the existing gate tests pass unchanged, and a 1,050-comment PR gets `merge: true` (no hold) or `merge: false, skip_reason: hold_claim` (hold on page 11). Rollback: revert the PR; the gate goes back to the 10-page reader.

## Implementation Steps

Phase 1:
1. `scripts/claude_merge_hold_gate.py`: add the constants, `_read_comment_page`, and `_stream_claim_comments`; switch `evaluate` (line 166) to it; update the module docstring's API-call paragraph (retries, no page cap, trimmed memory).
2. `tests/test_claude_merge_hold_gate.py`: make the fake `gh` slice list payloads by `per_page` / `page`, and add a per-path "fail the first N calls" fixture option. Add tests: a hold on page 11 of 1,050 comments blocks; 1,050 comments without a hold allow; an untrusted flood carrying fake hold markers is ignored; a page that fails twice then succeeds is read (retry); a page that always fails is `gate_unavailable` after 3 attempts; a non-array page is `gate_unavailable` without a retry; the kept copies hold only marker lines (memory bound); a comment with two marker lines is still ignored. Patch the retry delay to zero in tests that exercise retries.
3. `README.md` (merge hold gate paragraph) and `agents.md` (the #5316 paragraph): state that the gate reads every comment page with no page cap and retries transient reads.
4. `changelog.d/5566-merge-gate-comment-flood.md`: `security` section entry per §20.

## Files & Modules

- `scripts/claude_merge_hold_gate.py`
- `tests/test_claude_merge_hold_gate.py`
- `README.md`
- `agents.md`
- `changelog.d/5566-merge-gate-comment-flood.md` [new]

## Tests

- Unit (`tests/test_claude_merge_hold_gate.py`, fake `gh` on `PATH`, and in-process calls with a stub `check_in_status`): the cases in step 2.
- Existing suites that must stay green: `tests/test_claude_merge_hold_gate.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_claude_pr_sweep.py`, `tests/test_check_in_session_targeting.py` (the `ci.yml` step at line 509).

## Risks & Mitigations

- A very large PR makes the gate slow (one call per 100 comments). Mitigation: the calls are the same ones the old reader made up to 1,000 comments; beyond that, reading is the point of the fix (the issue's recommendation). ACCEPTED under AD-2.
- Rate-limit use grows with a flood. ACCEPTED: bounded by the number of comments an attacker can post, which GitHub rate-limits too.
- Offset pagination can skip a comment when an earlier one is deleted mid-read. ACCEPTED under AD-6 (pre-existing and shared with `check_in_status.py`).

## Rollout

Lands on the #5316 project branch and reaches `main` with that project's final PR (#5323). Consumer repos receive it through the existing `@stable` sync of `scripts/`. No flag, no migration, no new env var.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Where does the fix live? — Picked: A — a streaming reader inside `scripts/claude_merge_hold_gate.py`. Alternatives: B — remove the cap in `.claude/scripts/check_in_status.py` `gh_api_list`; C — both. Why: the merge refusal is the gate's; B changes the checker and sweep too and edits a protected `.claude/` path. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Should the stream have any page limit? — Picked: A — no limit; the read ends at the first short page. Alternatives: B — a much higher cap that still fails closed. Why: any fixed cap brings the same denial of service back at a higher comment count, which the issue's recommendation rules out. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How is memory bounded? — Picked: A — keep only trusted-author comments with a claim-marker line, trimmed to the fields and marker lines `read_fix_claims` reads. Alternatives: B — keep every comment. Why: an attacker controls the untrusted comments, so only trusted markers may grow the kept set; trimming keeps the marker count, so the decision is unchanged. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Retry policy for a page read? — Picked: A — 3 attempts, 1 s then 2 s back-off, only for read failures (`ReadError`); shape errors fail closed at once. Alternatives: B — no retry; C — retry shape errors too. Why: a long stream makes one transient 5xx more likely; a malformed payload is not transient. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Fix the same 10-page cap in the checker and sweep? — Picked: A — no, out of scope; noted. Alternatives: B — also change `check_in_status.gh_api_list`. Why: those callers retry on the next check-in rather than refusing a merge, and the change would need the protected-path twin flow (§28.C). Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Replace page-number pagination to avoid a skip when a comment is deleted mid-read? — Picked: A — keep page numbers; record the limitation. Alternatives: B — a second full pass to confirm; C — move to GraphQL cursors. Why: pre-existing and shared with `check_in_status.py`; B doubles the calls on a flooded PR and C changes transport (§5, §15). Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #5566; parent tracker #3576
- Issue #5316, plan `docs/plans/issue-5316-gate-auto-merge-on-hold-claims-plan.md`, final PR #5323
