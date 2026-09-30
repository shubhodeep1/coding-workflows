# Project checker waits on a held head instead of starting another review round

Source issue: shubhodeep1/coding-workflows#5667 (https://github.com/shubhodeep1/coding-workflows/issues/5667)
Base branch: main
Security pass: run

## Summary

The `/implement-plan-claude` project checker runs `check_in_status.py --pr N` (plain PR mode), which never reads `ai:claude-fix-claim` markers. A trusted `hold` claim on the PR's current head therefore does not stop it, and a review hand-off for that held head starts another review-round stage while the twin-sync blocker is still open. This plan makes plain PR mode report `state: held` (so `action: wait`) for a Claude-fixer PR whose current head carries a trusted `hold` claim, and states in the command that the hold goes on the pushed head in the same step as the blocker.

## Context

- Issue #5667 (owner-authored) lists repeated review-round stage sessions for PRs #5097, #5465, #5173, and #5511, each ending on a twin-sync blocker.
- What the PR timelines show (read 2026-09-30):
  - Every twin-first review-round stage **did** post a `hold` claim on the head it pushed, in the same step as its blocker. Examples: #5097 hold on `3ea22da` at 06:44Z, #5465 hold on `9ee0bb1` at 06:45Z, #5173 hold on `25db3d6` at 07:00Z, #5511 hold on `970adca` at 08:43Z.
  - The review workflow then reviews the held head and posts a hand-off for it. #5173 got a round-2 hand-off on the held head `25db3d6` at 07:34Z, 11 minutes before the twin sync `e0a7d2b` (07:45Z). For those 11 minutes the head was held and had a valid hand-off. #5465 (`9ee0bb1`, 07:18Z) and #5511 (`970adca`, 09:42Z) got the same kind of hand-off, but only after their sync had already moved the head.
  - `check_pr` (`.claude/scripts/check_in_status.py:236-279`) checks labels, then `_check_claude_fixer_pr`, then check runs. It never calls `read_fix_claims`, so a hand-off on a held head is `state: review-round` → `action: next_stage` + `review`. Only `--hand-back` mode (`check_pr_hand_back`, `:579-661`) honours holds.
  - The project checker for #5097 (`session_014L62sdmdQ6xVjdSYUu2nWq`) summarises its last turn as "created review-round stage, re-armed 60m check-in". So it keeps checking a wait after starting that wait's stage, even though its prompt says "end the turn without re-arming". That stray check-in is the one that meets the hand-off on the held head.
- Every review-round stage session named in the issue claimed a `[claude-twin-sync]` head (for example `session_01AdvtXnrnBiYhFaJBCxhyqN` claimed `6d973f2` on #5097). So those stages worked on the synced head, and there the hold was already lifted. Each of those rounds legitimately needed another twin sync because of twin-first (the #4785 sunset removes that cost).
- CLAUDE.md §26.H defines the `hold` claim: it "never expires on the same head; a push … lifts it". §28.C and `implement-plan-claude.md` step 4 (twin-first) already say the stage posts the hold and the twin-sync blocker instead of arming the wait.

## Goals

- `check_in_status.py --pr N` (plain mode, not `--terminal-only`) on an open PR whose head ref starts with `claude/implement-plan-` returns `{"done": false, "state": "held", "action": "wait", …}` when a trusted `hold` claim (`read_fix_claims`, the same trust rules as `--hand-back`) names the current head. This holds whatever labels, hand-offs, conflicts, or check runs the PR shows.
- A merged or closed PR still wins over a hold (terminal), exactly as in `--hand-back` mode.
- A push that moves the head lifts the hold with no other action.
- No new GitHub API call for a Claude-fixer PR: the one comment listing `_check_claude_fixer_pr` already reads is fetched once and reused. Non-fixer PRs still never read comments.
- `implement-plan-claude.md` (twin) says the hold goes on the head the stage pushed, in the same step as the blocker, and that the project checker keeps waiting on a held head.
- Tests cover the issue's case: a twin-first head with a hold and a later hand-off reports `held` / `wait`, not `next_stage`.

## Non-goals

- Parsing `<!-- ai:claude-blocked:v1 -->` comments on the source issue or the final PR (AD-1).
- Stopping the checker model from re-arming after it starts a stage. That is a separate prompt-compliance defect, recorded under Notes (AD-4).
- Reducing the number of twin syncs per review round (#4785).
- Archiving replaced blocked sessions (#4817) and the hold comment text (#5058).
- Changing `--hand-back` or `--terminal-only` behaviour, or the routing table.

## Constraints

- §6: no identifier is renamed or removed. `state: held` already exists in `--hand-back` output; plain mode gains it as a new value, and `route_verdict` routes every not-done verdict to `wait` already. New helper names must not collide with existing module names.
- §15: REST only, no new calls for Claude-fixer PRs (the comments listing moves earlier and is reused). The docstring's API budget is updated.
- §28.C twin-first (Q40 / #4948): every `.claude/**` change is made in the `workflow-templates/.claude/**` twin only and reaches `.claude/` through a `[claude-twin-sync]` commit. The phase PR stops at a hold claim plus the twin-sync blocker.
- §9: tabs in Python and Markdown code as the files already use.
- §20: one `changelog.d/` fragment (observable checker behaviour changes).
- §7: `agents.md` and `README.md` describe the checker's verdicts, so both are updated.

## Approach

In `check_pr`, after the merged / closed / `--terminal-only` returns, read `head` first. For a Claude-fixer head (`CLAUDE_FIXER_HEAD_PREFIX`), fetch the PR's issue comments once, run `read_fix_claims(comments, head_sha, now, trusted_logins=_fix_claim_trusted_logins(pr))`, and return `held` when its `claim.state` is `held`. Otherwise continue unchanged: blocking labels, then `_check_claude_fixer_pr(..., comments=<the same list>)`, then check runs.

The hold wins over blocking labels too, as in `--hand-back`. A `blocked` verdict would hand the PR back to the stage session that is itself waiting on the twin sync, so it would intervene on the held head.

Alternatives considered:
- **Parse the `ai:claude-blocked` comment on the source issue / final PR (the issue's first bullet).** `--pr` mode does not know the issue or final PR number, so this needs extra reads and free-text trust rules. The evidence shows every stage posted the hold, which is the machine-readable form of the same blocker. See AD-1.
- **Let a blocking label still win.** Rejected (AD-2).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for one standalone issue.

1. **Phase 1 — plain PR mode honours a hold on a Claude-fixer head.** protected paths: `.claude/scripts/check_in_status.py`, `.claude/commands/implement-plan-claude.md` (twin-first: edit `workflow-templates/.claude/scripts/check_in_status.py` and `workflow-templates/.claude/commands/implement-plan-claude.md`; the root copies arrive by `[claude-twin-sync]`).
   - Files: the two twins, `tests/test_check_in_status.py`, `CLAUDE.md` (§26.H sentence), `agents.md`, `README.md`, `changelog.d/5667-checker-waits-on-held-head.md` [new].
   - Done when: the new tests pass against the twin; `tests/test_check_in_status.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_implement_plan_claude_command.py`, and `tests/test_session_titles.py` pass except the template-parity checks, which stay red until the twin sync; ruff is clean on the twin.
   - Rollback: revert the phase PR. The script change is additive (one new not-done state in plain mode).

## Implementation Steps

1. `workflow-templates/.claude/scripts/check_in_status.py`:
   - `check_pr`: move `head` / `head_sha` / `head_ref` extraction above the blocking-label check. For a `claude/implement-plan-` head, validate the sha, fetch comments once (`gh_api_list`), and return `{"done": False, "state": "held", "head_sha", "claim", "reason"}` when `read_fix_claims(...)["claim"]["state"] == "held"`. Pass the fetched list to `_check_claude_fixer_pr(..., comments=...)`.
   - Module docstring: add the plain-mode hold rule to "Done waiting" and restate the Claude-fixer comment read in the API budget.
2. `workflow-templates/.claude/commands/implement-plan-claude.md`:
   - Check-in Loop → "What counts as done waiting", *PR* bullet: a trusted `hold` claim on the current head of a Claude-fixer PR is never done (`state: held`, `action: wait`) until a push moves the head, whatever else the PR shows; merged / closed still end the wait.
   - Step 4 twin-first, "A later stage …" bullet: the hold goes on the head the stage pushed, in the same step as the twin-sync blocker, and is what keeps the checker from starting another review round (#5667).
3. `tests/test_check_in_status.py`: load the twin as a second module and add the tests listed under Tests.
4. `CLAUDE.md` §26.H claims bullet: one sentence saying plain `--pr` mode (the `/implement-plan-claude` project checker) also reports `held` for a hold on the current head of a `claude/implement-plan-*` PR.
5. `agents.md` "Verdict helper" bullet and the claims bullet, and `README.md` claims paragraph: the same rule.
6. `changelog.d/5667-checker-waits-on-held-head.md` (`fixed`).

## Files & Modules

- `workflow-templates/.claude/scripts/check_in_status.py` (twin of `.claude/scripts/check_in_status.py`)
- `workflow-templates/.claude/commands/implement-plan-claude.md` (twin of `.claude/commands/implement-plan-claude.md`)
- `tests/test_check_in_status.py`
- `CLAUDE.md`
- `agents.md`
- `README.md`
- `changelog.d/5667-checker-waits-on-held-head.md` [new]
- `docs/implement-plan/issue-5667-checker-waits-on-held-head.md` [new] (progress log)

## Tests

Unit tests in `tests/test_check_in_status.py`, run against the twin module until the sync (then both copies are identical):
- Held head plus a findings hand-off for the same head → `held` / `wait` (the issue's case).
- Held head plus a conflict (`mergeable_state: dirty`) → `held`.
- Held head plus a blocking label → `held`.
- A hold on an older head, then a hand-off on the new head → `review-round` (a push lifts the hold).
- A hold posted by an untrusted login or association → ignored (`review-round`).
- A non-hold claim (`kind=review`) on the current head → does not block the hand-off.
- Merged / closed PR with a hold → `merged` / `closed`, and no comments read.
- Call budget: a held fixer PR costs exactly `pulls/N` + one comments listing, and a hand-off PR still reads comments once.
- `route_verdict({"done": False, "state": "held"}, "pr")` → `wait`.

Existing suites to run: `tests/test_check_in_status.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_claude_pr_sweep.py`, `tests/test_check_in_session_targeting.py`, `tests/test_implement_plan_claude_command.py`, `tests/test_session_titles.py`, `tests/test_claude_md_section_numbers.py`. The template-parity assertions stay red until the twin sync.

## Risks & Mitigations

- A stray hold leaves a project checker waiting indefinitely. Mitigation: only a trusted claim counts (PR author or `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`, owner / member / collaborator), any push lifts it, and the stage's blocker on the issue names what to do.
- A blocking label on a held head is no longer handed back while held. ACCEPTED: the head is waiting on a human, and the label is acted on after the push that lifts the hold.
- The fix has no effect until the `[claude-twin-sync]` lands on `main`, because checkers run the script from their checkout. ACCEPTED: this is the twin-first process.

## Rollout

Merges with the project's final PR into `main`. Checker sessions pick it up on their next checkout. A checker reused across the change refreshes `check_in_status.py` from `FETCH_HEAD` only when `action` is missing, so it runs the old copy until a new checker is created. Consumer repos receive the `.claude/` copy on the next `@stable` sync. No flag.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How should the project checker detect an unanswered blocker on a PR? — Picked: A — honour the trusted `hold` claim on the current head in plain `--pr` mode. Alternatives: B — also parse `<!-- ai:claude-blocked:v1 -->` comments on the source issue / final PR and compare them with later replies; C — only B. Why: every stage in the evidence posted the hold, a hold is trusted and machine-readable, and A needs no new API call; B needs the issue number, which `--pr` mode lacks, and trusts free text (§1, §5, §15). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Should a hold outrank a blocking label (`ai:review-blocked` / `ai:review-autofix-failed` / `ai:needs-human`) in plain mode? — Picked: A — yes, only merged / closed outrank a hold (as in `--hand-back`). Alternatives: B — labels still win and hand the PR back. Why: a hand-back on a held head would make the blocked stage intervene on a head that waits for a human. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Which heads does the plain-mode hold check cover? — Picked: A — only `claude/implement-plan-` heads (the Claude-fixer PRs the project checker waits on). Alternatives: B — every `claude/*` head. Why: non-fixer PRs never read comments today (`test_non_fixer_pr_never_reads_comments`), and plain mode is only used by the project checker. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Should this issue also stop the project checker from re-arming after it starts a stage (seen on `session_014L62sdmdQ6xVjdSYUu2nWq`)? — Picked: A — no; record it as a finding. Alternatives: B — add a deterministic guard in this phase. Why: §5 minimal change; the hold fix removes the held-head case, and a re-arm guard needs its own design (the script cannot tell that a stage already started). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] Where do the tests that exercise the new behaviour live while `.claude/` is not synced? — Picked: A — in `tests/test_check_in_status.py`, against the twin loaded as a second module. Alternatives: B — a new test file wired into `ci.yml`. Why: the issue names this file, and A needs no workflow edit. Applied in: phase 1 PR. Status: pending review

## Notes

- Finding (not fixed here, AD-4): project checker `session_014L62sdmdQ6xVjdSYUu2nWq` reports "created review-round stage, re-armed 60m check-in" after starting a stage, although its prompt's step 5 says to end the turn without re-arming. With this fix, a stray check-in on a held head waits. It can still start a second review round after the twin-sync push lifts the hold, before or beside the `/reclarify` resume.

## References

- Issue #5667; related #4817, #4785, #5058, #4948 (Q40 twin-first), #4622 (claim trust).
- PRs #5097, #5465, #5173, #5511 (evidence).
- `.claude/scripts/check_in_status.py`, `tests/test_check_in_status.py`, `tests/test_check_in_status_hand_back.py`.
