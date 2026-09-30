# Implement-Plan Log — gh api guard: unquoted expansions and fake heredoc markers can hide a flag or a call

- Plan: docs/plans/issue-5558-gh-api-guard-word-splitting-heredoc-plan.md
- Source issue: shubhodeep1/coding-workflows#5558
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5558-gh-api-guard-word-splitting-heredoc   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project started by the Claude issue dispatcher; single-phase plan written.

## Phases
1. [ ] Phase 1 — unquoted expansions and fake heredoc markers ask — protected paths: `.claude/hooks/gh_api_write_guard.py` (twin-first)
   - [ ] twin hook: `_mark_unquoted_expansions`, `_gh_api_word_can_split`, the ask in `evaluate()`, scanner body of `strip_heredoc_bodies`, docstrings
   - [ ] tests: unquoted-expansion asks, quoted no-decision cases, fake-heredoc asks, `strip_heredoc_bodies` units, observed commands moved (AD-5)
   - [ ] CLAUDE.md §23.D item 4 and §23.H, `agents.md`
   - [ ] `changelog.d/5558-gh-api-guard-word-splitting-heredoc.md`
   - [ ] coordination comments on #4786 and #4909 (AD-6)
   - Done when: both issue commands ask; `gh api "repos/a/b/pulls/$n" --jq .title` gets no decision; existing cases keep their outcome except those moved; the twin passes the guard tests in a scratch copy; parity holds after the twin sync.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which option closes (1) and (2)? — Picked: A — quote-aware per-word check: ask only when an expansion in a `gh api` word is unquoted, and make `strip_heredoc_bodies` ignore `<<` inside quotes. Alternatives: B — ask for any `$`/backtick in any `gh api` word, quoted or not; C — leave (1) to the allow list and fix only (2). Why: the issue marks A RECOMMENDED; it closes both holes and keeps double-quoted loop reads unchanged. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which unquoted expansions in a `gh api` word ask? — Picked: A — an unquoted, unescaped `$` in any form (`$X`, `${…}`, `$(…)`, `$((…))`, `$'…'`, `$"…"`), an unquoted backtick, and an unquoted brace expansion (`{a,b}`, `{1..3}`). Alternatives: B — `$` and backticks only; C — also pathname globs (`*`, `?`, `[`). Why: brace expansion splits one word into several with no environment (`--jq {.a,-XDELETE}`); globs are left out because `?` is unquoted in most query-string endpoints, so C would prompt on common reads, and a glob keeps its literal prefix. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should an unquoted expansion ask or deny? — Picked: A — ask, as the issue's acceptance says. Alternatives: B — deny with a reason that says to quote the word (like the #4891 malformed-jq deny), so an unattended session retries instead of waiting. Why: the command can be legitimate (a loop over IDs), a human may want to approve it, and the issue specifies ask. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Beyond quotes, which fake heredoc markers does the scanner reject? — Picked: A — also a here-string `<<<`, an escaped `\<<`, `<<` in a `#` comment, in arithmetic (`$((…))`, `((…))`), and in `${…}`; a heredoc whose operator line ends inside an open quote is not stripped. Alternatives: B — quotes only. Why: each is the same misread (the guard drops lines Bash runs) in the same function, and not stripping only adds asks (CLAUDE.md §12.B, latent bug in adjacent code). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] What happens to the observed stage-session commands with an unquoted `$n`, `$F`, or `$S` in a `gh api` word, which the tests record as not forced? — Picked: A — keep them verbatim, move them to the unquoted-expansion ask expectation, and add their double-quoted equivalents as not-forced cases. Alternatives: B — delete them. Why: they are real commands and the best regression cases; the quoted equivalents show the fix a session should make. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] How does this coordinate with #4786 and #4909, whose loop approval puts an unquoted `$VAR` in an endpoint? — Picked: A — §23.H states that a `gh api` word must double-quote an expansion, and the phase posts one comment on each issue saying a loop approval must keep this check (quote the loop variable, or exempt only a variable bound to literal tokens). Alternatives: B — exempt loop variables bound to literal tokens now. Why: neither issue is implemented, and a loop parser belongs to #4786 (§5). Applied in: phase 1 PR (docs) and comments on #4786, #4909. Status: pending review
- AD-7 [plan, 2026-09-30] Base branch while PR #4641 (same file) is still open? — Picked: A — `main`, as the issue names no integration branch; whichever of the two PRs merges second resolves the overlap. Alternatives: B — build on #4641's project branch. Why: the command builds on the branch the issue names, and this change does not depend on #4641's code. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher (routine `implement-issue #5558`), session session_017AtZFVEWrzpHRzMJDm3Yao, permission mode auto.
- Security pass: `security_pass_skip.py` returned `skip: false` (`no skip label`), so the plan keeps `Security pass: run`.
- Stale Routine sweep 2026-09-30: deleted 3 ended Routines (trig_01HzeqyvJ4GdMS65DmrzAcAT, trig_01AwaQR62qp8jvxsQFNXmYgS, trig_01VQzFLkrDRURdk2jH3weUT9).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
