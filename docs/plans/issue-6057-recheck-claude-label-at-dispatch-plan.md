# Claude issue pickup: re-check the `ai:claude` claim at dispatch time

Source issue: shubhodeep1/coding-workflows#6057 (https://github.com/shubhodeep1/coding-workflows/issues/6057)
Base branch: main
Security pass: run

## Summary

Removing `ai:claude` from an issue does not stop the Claude issue pickup from starting it once the intake has queued it. Make the label the live claim: the pickup reads each queued issue before it starts a session and refuses (and closes the queue item) when the issue no longer carries `ai:claude`, carries `ai:codex`, or is closed. The intake's `authorize_target()` applies the same claim rule.

## Context

#6038 was queued (#6039) at 02:33Z, `ai:claude` was removed at 02:44Z to park it, and #6039 stayed eligible: only closing it by hand stopped the early start (issue #6057, evidence section).

What the code does today:

- `scripts/claude_issue_handoff.sh:76-79` claims the routed issue with `ai:claude`, but a failed label POST only logs `warn claim_label_failed` and the dispatch still goes out.
- `scripts/claude_issue_intake.sh:192-226` reads the live issue once and calls `authorize_target()` (`scripts/claude_issue_route.py:347-391`), which checks the dispatcher, the target, `state`, and the author. It never looks at labels. This runs at **queue** time.
- The pickup (`.claude/commands/claude-issue-pickup.md` steps 2-3) runs `claude_issue_route.py queue-pending --fetch-repo` (`_cmd_queue_pending`, `:1368-1413`), which reads only the queue and the producer-run bindings in coding-workflows, then starts a session per `pending` entry. It **never reads the target issue** (pickup Rules: "The pickup never reads target issues"). `/claude-issue-dispatch` never reads it either.

So the issue's premise that `authorize_target()` is "used by `/claude-issue-dispatch` and the pickup" is not right: it runs only at intake. Re-checking at dispatch time needs a read of the live issue in the pickup's script (AD-1, AD-2).

Other facts the design depends on:

- No workflow reacts to an `ai:claude` label being added: `ai-clarify.yml` triggers on `issues: opened` and `/reclarify` comments only. Re-queueing after a park is "add `ai:claude`, comment `/reclarify`" (route rule 5), not a `labeled` trigger (AD-7).
- The pickup must not comment on queue items: "a comment by the account starts this repo's `issue_comment` workflows, while an edit and a close start none" (pickup step 3.2). The dispatched close already records its outcome as a final body line (AD-5).
- The pickup session runs in coding-workflows behind the Claude Code Web proxy, which answers 403 for repositories not attached to the session (CLAUDE.md §23.A), so a consumer issue may be unreadable from the pickup (AD-2).

## Goals

- `authorize_target()` returns `{"authorized": false, "reason": "claude_label_removed", "needs_comments": false}` for an open issue without `ai:claude`, and `reason: "codex_label_added"` for one carrying `ai:codex`; with `ai:claude` and no `ai:codex` it behaves as today.
- `queue-pending --fetch-repo` reads each pending `issue` target once and moves the refused ones out of `pending` into a new `refused` list (reasons `claude_label_removed`, `codex_label_added`, `issue_closed`, `target_not_issue`), each with the `queue_issues` to close. A failed read leaves the entry in `pending` (fail-open) and is listed in `claim_check_failed`.
- The pickup closes every queue issue of a `refused` entry as `not_planned` with a final body line naming the reason, starts no session for it, and counts it in its report.
- The handoff never dispatches an issue it could not claim with `ai:claude`, so the label's absence reliably means "parked".
- "Remove `ai:claude` to park a queued issue" is documented in `.claude/commands/claude-issue-pickup.md`, `agents.md`, `README.md`, and a `changelog.d/` fragment.

## Non-goals

- `claude_pr_fix.v1` items (the §26.H sweep): no claim label applies to them; they are unchanged.
- A `labeled` trigger that re-queues an issue when `ai:claude` is added back (AD-7).
- Changing `/claude-issue-dispatch` (it never reads the issue; the pickup checks before following its step 2) (AD-10).
- Changing `/implement-issue-claude` step 2's "add `ai:claude` if missing" claim (a hand run or a resumed project still claims the issue).

## Constraints

- §6: no rename. `authorize_target()` keeps its `authorized` / `reason` / `needs_comments` keys (the issue text says `allowed`; AD-3). Existing reasons stay; the two new reason strings are unique in the module.
- §15: one new REST read per pending issue target, at most the pickup limit (20 by default, 30 max) per wake, documented in the new helper's docstring with the calls audited; no GraphQL (proxy 403). The intake adds no call: its existing issue read carries the labels.
- §18: no new script; the check is folded into the existing `queue-pending` subcommand the pickup already runs.
- §20: a changelog fragment (observable behaviour change).
- §28.C / protected paths: the phase edits `.claude/commands/claude-issue-pickup.md`, which has no `workflow-templates/.claude/` twin. Under the interim twin-first default, that file is not edited; its exact diff and sha256 go into the twin-sync blocker.
- §9: tabs in Python and shell; YAML untouched.

## Approach

1. **One claim rule, two call sites.** Add a pure helper `claim_label_refusal(issue) -> str` to `scripts/claude_issue_route.py`: `"codex_label_added"` when `ai:codex` is present (Codex wins, as in `route_issue()` rule 4), else `"claude_label_removed"` when `ai:claude` is absent, else `""`. `authorize_target()` calls it after the `issue_closed` check and before the author check, so an unclaimed issue is refused without reading comments.
2. **Dispatch-time read in the pickup's script.** Add `fetch_dispatch_targets(entries, gh_read)` (one `gh api repos/<repo>/issues/<N>` GET per distinct `issue` target in `pending`) and the pure `apply_dispatch_claim_check(result, targets)`, which moves each target that is closed, a pull request, or fails `claim_label_refusal()` from `pending` to `refused`, and lists unreadable targets in `claim_check_failed` while leaving them in `pending`. `_cmd_queue_pending` runs both after `queue_pending()` in `--fetch-repo` mode; `--issues-json` mode takes an optional `--targets-json` for tests and hand runs. `remaining`, `deferred`, and `catch_up_due` are unchanged.
3. **Pickup handling (no-twin `.claude/` file).** Step 2 documents `refused` and `claim_check_failed`; a new step 3 sub-step closes each refused entry's queue issues with `mcp__github__issue_write` (`state` `closed`, `state_reason` `not_planned`, body + `Refused: <reason> (<UTC timestamp>) by pickup <id>`), no comment; the report gains `refused <x>`; the Rules note that the pickup now reads each pending target once.
4. **Handoff claim is mandatory.** In `scripts/claude_issue_handoff.sh`, a failed `ai:claude` POST skips the dispatch and takes the existing failure path (`ai:claude-handoff-failed`, failure comment, Telegram ERROR).

Alternatives (see Auto-decisions): checking only at intake does not fix the bug; checking only in `/implement-issue-claude` starts a session first and cannot tell a hand run from a dispatched one; a per-repo label listing needs unbounded pagination; failing closed on an unreadable target would stall every consumer issue the pickup session cannot reach.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue fixes the scope, and the pieces are one behaviour (the claim rule, its two call sites, the pickup's handling, and the docs).

1. **Phase 1 — re-check the claim at dispatch time.**
   - Files: `scripts/claude_issue_route.py`, `scripts/claude_issue_handoff.sh`, `tests/test_claude_issue_route.py`, `README.md`, `agents.md`, `changelog.d/6057-park-queued-claude-issue.md`; `.claude/commands/claude-issue-pickup.md` through the twin-sync blocker (no twin).
   - Protected paths: `.claude/commands/claude-issue-pickup.md`.
   - Done: the acceptance tests below pass, the existing routing / intake / handoff / queue tests pass, and the pickup diff is in the blocker.
   - Production-safe at merge: until the pickup file is synced, refused entries are no longer in `pending`, so they are not started; they stay open and the watchdog flags them after 3 hours. The project's own hold keeps the phase PR from merging before the sync.
   - Rollback: revert the phase PR; the pickup file change is text only.

## Implementation Steps

1. `scripts/claude_issue_route.py`: add `claim_label_refusal()`; call it in `authorize_target()` after `issue_closed`; update the docstrings (module routing notes, `authorize_target`).
2. `scripts/claude_issue_route.py`: add `fetch_dispatch_targets()` and `apply_dispatch_claim_check()` with §15 docstrings; wire them into `_cmd_queue_pending` (`--fetch-repo`), add `--targets-json` for `--issues-json` mode, and emit `refused` and `claim_check_failed` (always present, empty lists when nothing applies).
3. `scripts/claude_issue_handoff.sh`: a failed claim POST sets the dispatch as failed and records why (log `error claim_label_failed`), skipping `build-dispatch`.
4. `tests/test_claude_issue_route.py`: default `ai:claude` label in the `_target()` helper and the intake stub's issue JSON; new tests for `authorize_target()` (no `ai:claude` → `claude_label_removed`; `ai:claude` → allowed; `ai:codex` added → `codex_label_added`; refused before comments are needed), for `apply_dispatch_claim_check()` / `fetch_dispatch_targets()` (refusal reasons, fail-open, pr_fix untouched, one read per target), for the CLI (`--fetch-repo` call log with the target read; `--targets-json`), for the intake refusing an unclaimed issue, and for the handoff not dispatching when the claim fails.
5. Docs: `README.md` "Claude issue implementer" (flow step 4, the Switching table row "Park a queued issue", failure-mode reasons), `agents.md` item 15, `changelog.d/6057-park-queued-claude-issue.md`.
6. `.claude/commands/claude-issue-pickup.md` (no twin): prepare the diff (step 2 outputs, step 3 refused handling, step 4 report, Rules, Tool Access) and its sha256 for the twin-sync blocker.

## Files & Modules

- `scripts/claude_issue_route.py`
- `scripts/claude_issue_handoff.sh`
- `tests/test_claude_issue_route.py`
- `README.md`
- `agents.md`
- `changelog.d/6057-park-queued-claude-issue.md` [new]
- `.claude/commands/claude-issue-pickup.md` (via the twin-sync blocker; no twin)

## Tests

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_claude_issue_route.py -q`
- The other suites that read these files: `tests/test_claude_pr_sweep.py` (uses `queue_pending`), `tests/test_claude_session_janitor.py`, and the repo's standard checks named in `agents.md` for changed scripts (`bash -n`, shellcheck where configured).

## Risks & Mitigations

- **A consumer issue the pickup cannot read** (proxy 403) is started as today (fail-open, listed in `claim_check_failed`). The intake's queue-time check still applies to it. Documented as a known limit.
- **An issue routed while the claim POST fails** is now a visible handoff failure instead of a silent intake refusal (AD-6).
- **Manual intake (`workflow_dispatch`) of an unlabelled issue** is now refused `claude_label_removed`; the operator adds `ai:claude` first (AD-8, documented).
- **Refused items before the pickup file sync** are not started and stay open until the watchdog flags them; the project holds its PR until the sync.

## Rollout

Ships on merge to `main`; the pickup picks it up on its next wake (it checks out `origin/main` each wake). Consumer repos receive the handoff change on the next `@stable` sync; until then their handoff behaves as before and the intake / pickup checks still apply.

## Auto-decisions

- AD-1 [plan, 2026-10-02] Where does the dispatch-time check run, given `authorize_target()` runs only at intake and the pickup never reads the issue? — Picked: A — one shared claim rule used by `authorize_target()` (intake) and by a new live read of each pending target in `queue-pending --fetch-repo` (pickup). Alternatives: B — `authorize_target()` only (queue time; does not fix #6038); C — `/implement-issue-claude` step 2 only (a session starts first, and a hand run looks the same). Why: only a read at pickup time sees a label removed after queueing, and the issue's acceptance tests target `authorize_target()`. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] How does the pickup read the targets (the issue assumed no new call)? — Picked: A — one REST `GET repos/<repo>/issues/<N>` per pending issue target (≤ the pickup limit per wake), fail-open on a failed read. Alternatives: B — one `issues?labels=ai:claude&state=open` listing per repo (unbounded pages); C — fail closed on a failed read. Why: exact and bounded; GraphQL is refused by the web proxy, and failing closed would stall every consumer issue the pickup session cannot reach. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Result key and reason names (the issue says `allowed: false`). — Picked: A — keep `authorized`; new reasons `claude_label_removed` and `codex_label_added`, `ai:codex` checked first. Alternatives: B — rename the key to `allowed`. Why: §6, the intake script reads `.authorized`. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-02] Should the pickup also refuse a closed target it reads anyway? — Picked: A — yes, reason `issue_closed` (also for a target that reads back as a pull request: `target_not_issue`). Alternatives: B — label reasons only. Why: same read, and it saves an Opus session that would stop at "issue closed — nothing to do". Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-02] How does the pickup record the refusal (the issue asks for a one-line comment)? — Picked: A — close the queue item `not_planned` with a final body line `Refused: <reason> (<UTC timestamp>) by pickup <id>`, no comment. Alternatives: B — a comment. Why: the pickup's rule forbids comments on queue items (a comment starts `issue_comment` workflows); the dispatched close already uses a body line. Applied in: phase 1 PR (pickup diff in the twin-sync blocker). Status: pending review
- AD-6 [plan, 2026-10-02] A routed issue whose `ai:claude` claim POST fails. — Picked: A — the handoff treats it as a handoff failure (no dispatch; existing failure label, comment, and Telegram ERROR). Alternatives: B — keep the warning (the intake would then refuse it silently on the issue). Why: the issue requires the label's absence to reliably mean "parked". Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-10-02] Re-queue after a park (the issue cites an "existing labeled trigger", which does not exist). — Picked: A — document "add `ai:claude` back, then comment `/reclarify`". Alternatives: B — add an `issues: labeled` trigger to `ai-clarify.yml` for every consumer. Why: §5; `/reclarify` already routes a labelled issue to Claude (rule 5). Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-10-02] Manual intake (`workflow_dispatch`, trigger `manual`) of an issue without `ai:claude`. — Picked: A — refuse it like any other unclaimed issue and document "add `ai:claude` first". Alternatives: B — exempt the `manual` trigger. Why: one rule for the claim; an exemption would bring the bug back for hand re-fires. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-10-02] `/claude-issue-dispatch` (named by the issue as a call site). — Picked: A — leave it unchanged. Alternatives: B — make it read the issue. Why: it never reads the issue by design, and the pickup now checks before following its step 2. Applied in: no code change. Status: pending review

## Notes

- `.claude/commands/claude-issue-pickup.md` has no `workflow-templates/.claude/` twin, so under the interim twin-first default (CLAUDE.md §28.C, until #4785) its diff and sha256 go into the twin-sync blocker and the phase PR is held until a supervising session copies it.
- Security-pass skip check: `{"skip": false, "label": null, "reason": "no skip label"}`.

## References

- Issue #6057; evidence: #6038, queue item #6039.
- `scripts/claude_issue_route.py:215-245` (`route_issue`), `:347-391` (`authorize_target`), `:1368-1413` (`_cmd_queue_pending`).
- `scripts/claude_issue_intake.sh:192-226`, `scripts/claude_issue_handoff.sh:76-79`.
- `.claude/commands/claude-issue-pickup.md` steps 2-4.
