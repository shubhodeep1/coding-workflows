# Implement-Plan Log — Claude issue pickup: re-check the `ai:claude` claim at dispatch time

- Plan: docs/plans/issue-6057-recheck-claude-label-at-dispatch-plan.md
- Source issue: shubhodeep1/coding-workflows#6057
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-6057-recheck-claude-label-at-dispatch   Final PR: #6067 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (held for the twin sync; the stage `/reclarify` resumes arms the wait)
- Last updated: 2026-10-02
- Last note: phase 1 implemented and verified; PR opened with a hold claim; `.claude/commands/claude-issue-pickup.md` (no twin) waits for the twin-sync copy

## Phases
1. [ ] Phase 1 — re-check the `ai:claude` claim at dispatch time   — protected paths: .claude/commands/claude-issue-pickup.md (no twin); PR open, held for twin sync; review rounds: 0; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

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

## Lessons
- [source:plan-deviation] A gate that must hold "at dispatch time" needs a read at the moment of dispatch: checking only where the payload is first validated (the intake) leaves every later state change of the target invisible to the session starter. (files: scripts/claude_issue_route.py, .claude/commands/claude-issue-pickup.md)

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-02)
- `.claude/commands/claude-issue-pickup.md` has no `workflow-templates/.claude/` twin: its diff and sha256 go into the twin-sync blocker; the phase PR is held until a supervising session copies it.
- Pickup file after the sync: sha256 `e35c2ed22ca18988dc5276378faa1717262154f33dbe087177a552c92c7095d1` (the diff is in the twin-sync blocker comment on #6057).
- Verified 2026-10-02 in the phase session: `tests/test_claude_issue_route.py` 261 passed; the six suites that read the pickup file passed against a copy with the synced file (one copy-only failure: the `workflow-templates/CLAUDE.md` symlink is not preserved by the copy; the test passes in the checkout); ruff and `shellcheck --severity=error` clean.
- Plan deviation: `_dispatch_refusal` also refuses `target_repo_mismatch` (a transferred issue), the same family as AD-4's `target_not_issue`; it matches the intake's `authorize_target()` check.
