# Count Claude fix claims and holds only from the PR's author or the workflow account

Source issue: shubhodeep1/coding-workflows#4622 (https://github.com/shubhodeep1/coding-workflows/issues/4622)
Base branch: main
Security pass: skip (ai:security: automation-produced issue)

## Summary

The security audit (issue #4622, `STRIDE: Spoofing`, medium) found that
`read_fix_claims` in `.claude/scripts/check_in_status.py` trusts any
`ai:claude-fix-claim` marker whose comment has an `OWNER`, `MEMBER`, or
`COLLABORATOR` author association. Any collaborator can therefore comment a
well-formed `kind=hold` marker for a `claude/*` PR's current head, and the
CLAUDE.md §26 checker and the catch-all sweep then treat the PR as held
indefinitely, although no fixer hit the cap. The same collaborator can also
forge counted claims (`conflict` / `ci` / `blocked` on arbitrary heads) to push
`hand_backs` to the cap and force a real fixer into a hold.

Fix: a claim (hold included) counts only when its comment's author is one of
the identities that actually post claims, the PR's own author (the account
whose Claude sessions push and fix it, through the session proxy) or the
configured workflow account `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` (the `GH_PAT`
account the catch-all sweep posts its reservations with). The association
check stays as a second condition.

## Context

- `.claude/scripts/check_in_status.py:388-431` `read_fix_claims`: the only
  claim reader. Line 408 filters on `author_association` alone, line 413
  parses the marker, lines 417-420 count `hand_backs` and pick the latest claim
  on the current head, line 424 makes a `hold` never expire.
- `.claude/scripts/check_in_status.py:443-477` `check_pr_hand_back`: its only
  caller. It already holds the PR object (`pulls/N`), so the PR author's login
  (`pr["user"]["login"]`) costs no extra API call (§15).
- Writers of claims:
  - `.claude/commands/fix-claude-pr.md` step 3-5 and `/implement-plan-claude`
    [Claims](.claude/commands/implement-plan-claude.md#claims) post through
    `.claude/scripts/claude_fix_claim.py post` from Claude sessions. In Claude
    Code on the web every `gh` call is signed by the session proxy as the user
    who connected the GitHub App (CLAUDE.md §23.A), the same user whose
    sessions opened the `claude/*` PR.
  - `scripts/claude_pr_sweep.py:222-226` posts the sweep reservation
    (`sweep-run-<id>`) with `GH_PAT`. `.github/workflows/review_autofix_sweep.yml:341-355`
    sets `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` to the repo variable or, when it
    is empty, to the `GH_PAT` account's own login, so the sweep's own
    reservations are posted by exactly that login.
- The §26 checker (CLAUDE.md §26.B step 3, §26.C step 1), the
  `/implement-plan-claude` checker, and `/fix-claude-pr` step 1 already pass
  `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` to `check_in_status.py`.
- `workflow-templates/.claude/scripts/check_in_status.py` and
  `workflow-templates/.claude/commands/fix-claude-pr.md` are byte-identical
  mirrors; `tests/test_check_in_status_hand_back.py::test_template_copies_match`
  enforces it for the scripts.
- Docs that describe who may claim: CLAUDE.md §26.H ("Claims stop duplicate
  fixers", line ~1948), `agents.md` (~line 895), `README.md` "Claims" paragraph
  (~line 1341) and the `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` row (line 87).

## Goals

- A claim or hold posted by an account that is neither the PR's author nor the
  configured workflow account is ignored: it neither decides `claim` (`live` /
  `held`) nor counts toward `hand_backs`.
- Legitimate claims keep working unchanged: session claims (PR author), sweep
  reservations (workflow account), `--ignore-claim-by`, the lease, the cap, and
  a later claim lifting a hold.
- No new API calls (§15), no new env var, no change to the claim marker format.

## Non-goals

- No dedicated GitHub App identity for claims (see AD-1): web sessions cannot
  post as a bot.
- No expiry or cap-state condition for holds (AD-2): the documented contract
  "a hold never expires on the same head" stays.
- No change to hand-off or verdict authentication in `_check_claude_fixer_pr`.
- No change to `claude_fix_claim.py` behaviour (it only writes).

## Constraints

- §1: security first; fail closed. A PR with no readable author and no
  configured workflow login counts no claims.
- §5: minimal change set, in the one reader function and its one caller.
- §6: `read_fix_claims` keeps its name and positional parameters; the trusted
  logins arrive as a new keyword parameter. No identifier is renamed.
- §9: tabs in Python.
- §14/§26.H: the `.claude/` template mirrors under `workflow-templates/` change
  byte-for-byte with the originals.
- §15: reuse the PR object `check_pr_hand_back` already fetched.
- §20: security fix → one `changelog.d/` fragment with `<!-- changelog: security -->`.
- §7: CLAUDE.md §26.H, `agents.md`, and `README.md` describe the new rule.

## Approach

1. `check_in_status.py`: add `_fix_claim_trusted_logins(pr)` returning the
   casefolded logins whose claims count: the PR's `user.login` and
   `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` (each only when it is a non-empty
   string). Add a keyword parameter `trusted_logins: tuple[str, ...] = ()` to
   `read_fix_claims`; a comment counts only when its association is trusted
   **and** `user.login` casefolded is in `trusted_logins`. An empty tuple
   counts nothing (fail closed). `check_pr_hand_back` passes the helper's
   result. Update the module docstring and `read_fix_claims` docstring.
2. Copy the script to `workflow-templates/.claude/scripts/check_in_status.py`.
3. `claude_fix_claim.py` docstring and `fix-claude-pr.md` step 4 (both copies):
   one sentence saying a claim counts only from the PR's author or the
   workflow account.
4. Docs: CLAUDE.md §26.H, `agents.md`, `README.md` (Claims paragraph and the
   `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` row).
5. `changelog.d/4622-trusted-fix-claim-authors.md` (security).
6. Tests in `tests/test_check_in_status_hand_back.py`.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
fix is one reader function, its caller, their mirrors, docs, and tests, and
splitting it would ship docs that describe code not yet merged.

1. **Phase 1 — restrict claim authors to the PR author and the workflow account.**
   - Files: `.claude/scripts/check_in_status.py`,
     `workflow-templates/.claude/scripts/check_in_status.py`,
     `.claude/scripts/claude_fix_claim.py`,
     `workflow-templates/.claude/scripts/claude_fix_claim.py`,
     `.claude/commands/fix-claude-pr.md`,
     `workflow-templates/.claude/commands/fix-claude-pr.md`, `CLAUDE.md`,
     `agents.md`, `README.md`, `changelog.d/4622-trusted-fix-claim-authors.md`,
     `tests/test_check_in_status_hand_back.py`.
   - Done when: a collaborator's hold or claim that is not by the PR author or
     workflow account is ignored (tests), existing claim behaviour passes
     unchanged, template mirrors match, and `python3 -m pytest
     tests/test_check_in_status_hand_back.py tests/test_check_in_status.py
     tests/test_claude_pr_sweep.py tests/test_claude_md_section_numbers.py -q`
     passes.
   - Rollback: revert the phase PR.

## Implementation Steps

1. `.claude/scripts/check_in_status.py` ~388-431: `read_fix_claims(...,
   trusted_logins=())` login filter; new `_fix_claim_trusted_logins(pr)`;
   ~469 pass it from `check_pr_hand_back`. Docstring lines 39-54 mention the
   author rule.
2. Mirror to `workflow-templates/.claude/scripts/check_in_status.py`.
3. Docstring sentence in `claude_fix_claim.py` (both copies); step 4 sentence
   in `fix-claude-pr.md` (both copies).
4. CLAUDE.md §26.H "Claims stop duplicate fixers" bullet; `agents.md` claim
   bullet; `README.md` Claims paragraph and `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`
   row.
5. Changelog fragment.
6. Tests: give the `_pr` fixture an author and the `_claim` fixture that
   author; add tests for a forged hold, forged counted claims, the workflow
   account's reservation, a PR with no author, case-insensitive matching, and
   an empty workflow login.

## Files & Modules

- `.claude/scripts/check_in_status.py` (+ template mirror)
- `.claude/scripts/claude_fix_claim.py` (+ template mirror, docstring only)
- `.claude/commands/fix-claude-pr.md` (+ template mirror, one sentence)
- `CLAUDE.md`, `agents.md`, `README.md`
- `changelog.d/4622-trusted-fix-claim-authors.md`
- `tests/test_check_in_status_hand_back.py`

## Tests

- New unit tests (above) in `tests/test_check_in_status_hand_back.py`.
- Regression: `tests/test_check_in_status.py`, `tests/test_claude_pr_sweep.py`,
  `tests/test_check_in_status_hand_back.py::test_template_copies_match`,
  `tests/test_claude_md_section_numbers.py`, and the command-file tests
  (`tests/test_implement_plan_claude_command.py`,
  `tests/test_implement_issue_claude_command.py`) plus any test that reads
  `fix-claude-pr.md`.

## Risks & Mitigations

- A Claude session running under an account other than the PR's author (a
  teammate fixing someone else's PR) posts claims that no longer count, so the
  checker could hand the head to a second fixer. Mitigation: documented in
  `fix-claude-pr.md` and CLAUDE.md §26.H; the lease is 3 hours and the head
  moves on the first push; this is coordination loss, not a security loss.
- The §26 checker runs with `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` empty or set to
  its own proxy login, so it may not see sweep reservations when the `GH_PAT`
  account differs from the PR author. Mitigation: the sweep only reserves a
  head that has been due for 2 hours with no claim, the fixer it queues claims
  the head itself as the PR author's account, and the README row tells
  operators to set the repo variable.
- A PR whose author login is unreadable counts only the workflow account's
  claims (fail closed).

## Rollout

Takes effect on merge for this repo's checker, sweep, and sessions; consumer
repos receive the `.claude/` scripts and command on the next `@stable` sync.
No config change is required. Operators who run the sweep with a `GH_PAT`
account different from the Claude users should set the
`CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` repo variable, as the README already asks.

## Auto-decisions

- AD-1 [plan, 2026-09-27] Q1: Which accounts may post a claim or hold that counts? — Picked: A — an owner/member/collaborator comment whose author is the PR's author or the configured workflow account `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`. Alternatives: B — only a dedicated GitHub App bot, as for fixer verdicts; C — keep association-only and give holds a lease. Why: A closes the spoofing path for every collaborator who does not own the PR while keeping the two identities that actually post claims (session proxy user, sweep `GH_PAT`); B would silently ignore every session claim because web sessions cannot post as a bot (CLAUDE.md §23.A); C leaves the forgery in place for days. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Q2: Should a hold also need verified cap state (`cap_reached`) to count, as the issue's recommendation suggests? — Picked: A — no; bind holds by author identity only. Alternatives: B — honour a hold only when `cap_reached` is true. Why: `/fix-claude-pr` step 5 legitimately posts holds below the cap (an ambiguous conflict, no verdict bot, a CI failure that is not the PR's, `ai:needs-human`), so B would break documented dead-end holds; the author binding already stops forged holds and forged counted claims. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Q3: Add a new env var listing extra trusted claim logins? — Picked: A — no. Alternatives: B — add `CLAUDE_FIX_CLAIM_AUTHOR_LOGINS`. Why: §5 minimal change; the PR author and the workflow account cover every writer today, and an allowlist could re-open the hole if misconfigured. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] Q4: Compare logins exactly or case-insensitively? — Picked: A — case-insensitively (casefold). Alternatives: B — exact, as the hand-off author check does. Why: GitHub logins are case-insensitive and unique, so casefolding admits no other account while a mis-cased repo variable no longer drops the sweep's reservations. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Q5: Should claims from untrusted authors still count toward `hand_backs`? — Picked: A — no, the same author filter applies to counting. Alternatives: B — count them, filter only the live/held decision. Why: counted claims on arbitrary heads would otherwise let a collaborator push a PR to the cap and force a real fixer into a hold. Applied in: phase 1 PR. Status: pending review

## References

- Issue #4622; security audit tracker #3576
- `.claude/scripts/check_in_status.py`, `.claude/scripts/claude_fix_claim.py`
- `scripts/claude_pr_sweep.py`, `.github/workflows/review_autofix_sweep.yml`
- CLAUDE.md §23.A, §26.H
